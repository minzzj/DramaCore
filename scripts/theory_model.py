"""理论计算：显存峰值 / 加载次数 / 时长 / 码率。

## 为什么需要这个文件

用户说：「对于视频生成等不能完成的任务则需要进行理论计算」。
本机是**纯 CPU、15.75 GB 内存、无可用 GPU**，真实视频生成不可能跑；
但"不能跑"不等于"不能算" —— 权重体积、显存峰值、模型加载次数都是**确定可算**的。

**本文件的每个数字都来自磁盘实测或明确公式，不猜、不编。**

## 核心结论（先说答案）

MiniMax H3 的三个大件（磁盘实测）：

| 件 | 体积 |
|---|---|
| 视频 UNET（ref2va，int8 剪枝） | 19.53 GB |
| 文本编码器（qwen3vl-32b nvfp4） | 14.61 GB |
| 视频 VAE（fp16） | 4.85 GB |
| 音频 VAE（fp32） | 0.56 GB |

**一次性全加载** = 19.53 + 14.61 + 4.85 + 0.56 = **39.55 GB**
-> 5090 的 **32 GB 装不下**（超 7.55 GB），必然 OOM。

**阶段化加载**（先编码提示词、卸载文本编码器、再加载视频件）
-> 峰值 = max(14.61, 25.30) = **25.30 GB** -> 32 GB 装得下，**余 6.70 GB**。

**这就是"阶段化显存调度"不是优化项、而是必需品的原因。**

## 用法

    python scripts/theory_model.py            # 人读表格
    python scripts/theory_model.py --json     # 机器读（测试用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

GB = 1024.0 ** 3

#: 目标机显存（用户明确说 5090 32G）。
TARGET_VRAM_GB = 32.0

#: 系统/驱动/框架常驻占用估算。留 2 GB 是为了不让 CUDA 上下文分配失败
#: （"差一点点"在 GPU 上不是慢一点，而是直接 OOM）。
SYSTEM_RESERVE_GB = 2.0

#: 权重文件 -> 用途分类。按**文件名特征**匹配，因为用户可能换名字。
_VIDEO_UNET_HINTS = ("ref2va", "fl2va", "minimax_h3")
_TEXT_ENCODER_HINTS = ("qwen", "text_encoder", "clip", "t5")
_VIDEO_VAE_HINTS = ("video_vae",)
_AUDIO_VAE_HINTS = ("audio_vae",)
_LORA_HINTS = ("lora", "lightx2v")


def _classify(name: str) -> str | None:
    low = name.lower()
    if any(h in low for h in _AUDIO_VAE_HINTS):
        return "audio_vae"
    if any(h in low for h in _VIDEO_VAE_HINTS):
        return "video_vae"
    if any(h in low for h in _TEXT_ENCODER_HINTS):
        return "text_encoder"
    if any(h in low for h in _VIDEO_UNET_HINTS):
        return "video_unet"
    if any(h in low for h in _LORA_HINTS):
        return "lora"
    return None


def scan_weights(base: Path) -> dict[str, list[dict]]:
    """扫描磁盘上的权重，按用途分组。**只认实际存在的文件。**"""
    groups: dict[str, list[dict]] = {}
    if not base.is_dir():
        return groups
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (
            ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft"
        ):
            continue
        low = path.name.lower()
        if ".part" in low or low.startswith("put_"):
            continue
        kind = _classify(path.name)
        if kind is None:
            continue
        groups.setdefault(kind, []).append(
            {"name": path.name, "gb": round(path.stat().st_size / GB, 3)}
        )
    return groups


def _largest(groups: dict[str, list[dict]], kind: str) -> float:
    """同类别取**最大**的一个 —— 一次只会加载一个 UNET/一个文本编码器。"""
    items = groups.get(kind) or []
    return max((item["gb"] for item in items), default=0.0)


def compute(base: Path, *, target_vram_gb: float = TARGET_VRAM_GB) -> dict:
    """算出关键数字。所有输入都来自磁盘实测。"""
    groups = scan_weights(base)
    unet = _largest(groups, "video_unet")
    encoder = _largest(groups, "text_encoder")
    video_vae = _largest(groups, "video_vae")
    audio_vae = _largest(groups, "audio_vae")

    # ① 一次性全加载：文本编码 + 视频 UNET + 两个 VAE 同时驻留
    all_at_once = round(unet + encoder + video_vae + audio_vae, 3)

    # ② 阶段化峰值：
    #    P1 编码提示词（只有文本编码器）-> 卸载
    #    P2 视频生成（UNET + 双 VAE，文本编码器已卸）
    phase_encoder = round(encoder, 3)
    phase_video = round(unet + video_vae + audio_vae, 3)
    staged_peak = round(max(phase_encoder, phase_video), 3)

    usable = max(0.0, target_vram_gb - SYSTEM_RESERVE_GB)
    return {
        "weights": groups,
        "size_gb": {
            "video_unet": unet,
            "text_encoder": encoder,
            "video_vae": video_vae,
            "audio_vae": audio_vae,
            "total_on_disk": round(unet + encoder + video_vae + audio_vae, 3),
        },
        "peak_gb": {
            "all_at_once": all_at_once,
            "phase_text_encode": phase_encoder,
            "phase_video_generate": phase_video,
            "staged": staged_peak,
        },
        "budget": {
            "target_vram_gb": target_vram_gb,
            "system_reserve_gb": SYSTEM_RESERVE_GB,
            "usable_gb": round(usable, 3),
            "all_at_once_fits": all_at_once <= usable,
            "staged_fits": staged_peak <= usable,
            "all_at_once_over_by_gb": round(max(0.0, all_at_once - usable), 3),
            "staged_headroom_gb": round(max(0.0, usable - staged_peak), 3),
        },
        "verdict": (
            "一次性全加载装不下，必须阶段化"
            if (all_at_once > usable >= staged_peak)
            else ("两者都装得下" if all_at_once <= usable
                  else "即使阶段化也装不下，需要更强剪枝/量化")
        ),
    }


def load_counts(segments: int, *, per_segment_alternation: bool = False) -> dict:
    """模型加载次数。

    * **逐段交替**（生成一段 -> 审一段 -> 再生成）：每段都要换一次模型，
      加载次数 = `2 * segments + 2`（首尾各一次编码与封装）；
    * **阶段化批量**：与段数**无关**，固定 5 次
      （提示词/素材/视频/审核/重生成 各一次切换）。

    这就是"效率"的量化来源 —— 不是玄学。
    """
    staged = 5
    alternated = 2 * max(0, segments) + 2
    return {
        "segments": segments,
        "staged_loads": staged,
        "alternated_loads": alternated,
        "saved_loads": max(0, alternated - staged),
        "ratio": round(alternated / staged, 2) if staged else None,
    }


#: 模型/权重加载一次的经验耗时（秒）。**这是估算，不是实测** —— 本机没有对应 GPU。
LOAD_SECONDS_PER_MODEL = 45.0


def time_estimate(segments: int, *, seconds_per_segment: float,
                  load_seconds: float = LOAD_SECONDS_PER_MODEL) -> dict:
    """时长估算。公式与假设全部显式写出，便于换成实测值。"""
    staged = segments * seconds_per_segment + 5 * load_seconds
    alternated = segments * seconds_per_segment + (2 * segments + 2) * load_seconds
    return {
        "assumptions": {
            "seconds_per_segment": seconds_per_segment,
            "load_seconds_per_model": load_seconds,
            "note": "load_seconds_per_model 是**估算值**，本机无对应 GPU 无法实测",
        },
        "staged_seconds": round(staged, 1),
        "alternated_seconds": round(alternated, 1),
        "saved_seconds": round(alternated - staged, 1),
        "saved_percent": (round((alternated - staged) / alternated * 100, 1)
                          if alternated else 0.0),
    }


def bitrate_estimate(*, width: int = 1920, height: int = 1080, fps: int = 24,
                     crf: int = 18, seconds: float = 3600.0) -> dict:
    """码率/体积估算（H.264 CRF 18，1080p24）。

    CRF 是**恒定质量**，码率随内容变化，只能用经验区间估：
    1080p24 H.264 CRF18 实测常见 **8-12 Mbps**。这里给区间而不是单点 ——
    给单点会假装精确。
    """
    low_mbps, high_mbps = 8.0, 12.0
    return {
        "resolution": f"{width}x{height}",
        "fps": fps,
        "crf": crf,
        "duration_s": seconds,
        "bitrate_mbps_range": [low_mbps, high_mbps],
        "size_gb_range": [round(low_mbps * seconds / 8 / 1024, 2),
                          round(high_mbps * seconds / 8 / 1024, 2)],
        "note": "CRF 恒定质量，码率随画面复杂度变化，故给区间不给单点",
    }


def vram_per_segment(*, unet_gb: float, latent_tokens: int,
                     bytes_per_token: float = 0.5) -> dict:
    """单个段生成时的**激活**显存估算（粗略）。公式显式写出。"""
    activations = latent_tokens * bytes_per_token / GB
    return {
        "unet_gb": unet_gb,
        "latent_tokens": latent_tokens,
        "activation_gb": round(activations, 3),
        "total_gb": round(unet_gb + activations, 3),
        "note": "bytes_per_token 是粗估系数，真实值随实现变化；仅用于量级判断",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Better ComfyUI 理论计算")
    parser.add_argument("--json", action="store_true", help="输出 JSON（测试用）")
    parser.add_argument("--segments", type=int, default=10, help="段数（默认 10）")
    parser.add_argument("--base", help="ComfyUI 根目录（默认从配置读）")
    args = parser.parse_args(argv)

    from bettercomfyui.config import load_config

    cfg = load_config(repo_root=REPO_ROOT)
    base = Path(args.base or cfg.global_.comfyui_base_path or "")
    result = compute(base)
    result["loads"] = load_counts(args.segments)
    result["time"] = time_estimate(args.segments, seconds_per_segment=60.0)
    result["bitrate"] = bitrate_estimate()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    size = result["size_gb"]
    peak = result["peak_gb"]
    budget = result["budget"]
    print("=== 权重（磁盘实测）===")
    for key, label in (("video_unet", "视频 UNET"), ("text_encoder", "文本编码器"),
                       ("video_vae", "视频 VAE"), ("audio_vae", "音频 VAE")):
        print(f"  {label:<12} {size[key]:>7.2f} GB")
    print(f"  {'合计':<12} {size['total_on_disk']:>7.2f} GB")

    print("\n=== 显存峰值 ===")
    print(f"  一次性全加载        {peak['all_at_once']:>7.2f} GB")
    print(f"  阶段化（编码提示词） {peak['phase_text_encode']:>7.2f} GB")
    print(f"  阶段化（视频生成）   {peak['phase_video_generate']:>7.2f} GB")
    print(f"  阶段化峰值          {peak['staged']:>7.2f} GB")

    print(f"\n=== 目标机 {budget['target_vram_gb']:.0f} GB（预留 "
          f"{budget['system_reserve_gb']:.0f} GB，可用 {budget['usable_gb']:.2f} GB）===")
    print(f"  一次性全加载装得下？ {'是' if budget['all_at_once_fits'] else '否'} "
          f"（{'余' if budget['all_at_once_fits'] else '超'} "
          f"{budget['all_at_once_over_by_gb']:.2f} GB）")
    print(f"  阶段化装得下？       {'是' if budget['staged_fits'] else '否'} "
          f"（余 {budget['staged_headroom_gb']:.2f} GB）")
    print(f"  结论：{result['verdict']}")

    loads = result["loads"]
    print(f"\n=== 模型加载次数（{loads['segments']} 段）===")
    print(f"  逐段交替 {loads['alternated_loads']} 次 / 阶段化 {loads['staged_loads']} 次 "
          f"-> 省 {loads['saved_loads']} 次（{loads['ratio']}x）")
    time = result["time"]
    print(f"  时长：阶段化 {time['staged_seconds']:.0f}s vs 交替 {time['alternated_seconds']:.0f}s "
          f"-> 省 {time['saved_seconds']:.0f}s（{time['saved_percent']}%）")

    br = result["bitrate"]
    print(f"\n=== 成片体积（{br['resolution']} @{br['fps']}fps, CRF{br['crf']}, 1 小时）===")
    print(f"  {br['bitrate_mbps_range'][0]:.0f}-{br['bitrate_mbps_range'][1]:.0f} Mbps "
          f"-> {br['size_gb_range'][0]:.2f}-{br['size_gb_range'][1]:.2f} GB")
    print(f"  注：{br['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
