"""单一大节点：剧本 → 成片（全流程，一个节点完成整条链）。

用户硬要求（2026-10-18）：
- **只有一个节点**：剧本进、成片出；中间素材/审核/剪辑等节点不暴露。
- **导演台全功能、不删减**：完整 timeline_data（时间轴 v5：segments/global/refs/首尾帧/
  refVideos/refAudios）、六种任务模式（t2v/i2v/fl2v/r2v/v2v/rv2v）、段间连续性
  （上一段尾部 22 帧含音频 pin 进下一段开头，生成后裁前缀 → 前 22 帧=上一段后 22 帧）、
  context/motion context、SelfLift/Refine/FaceRefine/SemanticBridge 内部开关、
  显存策略、分段导出；参数多而全，不为简洁删功能。
- **不依赖外部自定义节点**：导演台核心源码已 vendor 进本包；模型用 ComfyUI 自带 loader。
- **失败要说清楚**：缺模型/缺参数/显存不足/参考文件缺失都抛可操作错误，不假装成功。
- 素材库参考图（身份，只挂已 LOCK）与尾部 22 帧（动作/镜头/音频辅助）**叠加**生效（V3/R3）。

全流程：
1. 复用本包 `BetterComfyUIEverything`：剧本 → 一次 API（或占位）→ E1–E5 提示词；
2. 节点内部：图像模型生素材图（多候选轮）→ 入库（版本+指纹）→ 素材审核（CNT1/LOCK/兜底）
   → 只把**已 LOCK** 的素材图作为 r2v 参考图挂进导演台时间轴（未过审绝不挂，R3）；
3. 按全部导演台参数构造 timeline_data → vendored 导演台出片（含音频、段间连续、可选二采）；
4. 分段 mp4 → 本项目 `BetterComfyUIFinalize`（pipeline.splice/encode）拼接封装 + 自检；
5. 段级 R_seg + 整片 R_full 审核（修复 tuple 解析）→ 导出项目 output 与 ComfyUI output。
"""

from __future__ import annotations

import gc
import json
import os
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

_CATEGORY = "BetterComfyUI/单节点"

_DEFAULT_SCRIPT = (
    "第一章 雪夜\n"
    "林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
    "林秀在桌边坐下，窗外雪落无声，灯影摇曳。"
)

_NEGATIVE = "低质量，模糊，变形，水印，文字，多手多脚"

_FULL_POLICIES = ["按项目配置", "阻止输出", "降级输出"]
_VIDEO_REVIEW_SCOPES = ["段级 + 整片（推荐）", "仅段级 R_seg", "仅整片 R_full"]


# ---------------------------------------------------------------------------
# ComfyUI 环境探测（非 ComfyUI 环境下返回空/默认，不抛错，便于 pytest）
# ---------------------------------------------------------------------------
def _filename_list(kind: str) -> list[str]:
    try:
        import folder_paths  # type: ignore

        return list(folder_paths.get_filename_list(kind) or [])
    except Exception:  # noqa: BLE001
        return []


def _input_dir() -> Path | None:
    try:
        import folder_paths  # type: ignore

        return Path(folder_paths.get_input_directory())
    except Exception:  # noqa: BLE001
        return None


def _samplers() -> list[str]:
    try:
        import comfy.samplers  # type: ignore

        return list(comfy.samplers.KSampler.SAMPLERS)
    except Exception:  # noqa: BLE001
        return ["euler", "res_multistep"]


def _schedulers() -> list[str]:
    try:
        import comfy.samplers  # type: ignore

        return list(comfy.samplers.KSampler.SCHEDULERS)
    except Exception:  # noqa: BLE001
        return ["simple", "normal"]


def _task_type_options() -> list[str]:
    try:
        from .director_runtime import task_type_options

        return task_type_options()
    except Exception:  # noqa: BLE001
        return ["r2v — 参考主体生视频(Reference to Video)"]


def _default_task_type() -> str:
    try:
        from .director_runtime import default_task_type_label

        return default_task_type_label()
    except Exception:  # noqa: BLE001
        return "r2v — 参考主体生视频(Reference to Video)"


def _face_detector_options() -> list[str]:
    try:
        from .vendor.h3_director.director.face_refine.pack import detector_choices

        return list(detector_choices())
    except Exception:  # noqa: BLE001
        return [""]


def _ffmpeg() -> str:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    return _ffmpeg_exe()


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _default_from_spec(decl: Any) -> Any:
    dtype = decl[0]
    meta = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
    if isinstance(dtype, list):
        return meta.get("default", dtype[0] if dtype else "")
    if dtype == "STRING":
        return meta.get("default", "")
    if dtype == "INT":
        return meta.get("default", 0)
    if dtype == "FLOAT":
        return meta.get("default", 0.0)
    if dtype == "BOOLEAN":
        return meta.get("default", False)
    return None


def _invoke(node_obj: Any, method: str, explicit: dict, cls: Any = None):
    """按当前 ComfyUI 方法签名/INPUT_TYPES 默认值调用节点方法（跨版本适配）。"""
    import inspect

    specs: dict[str, Any] = {}
    if cls is not None:
        try:
            spec = cls.INPUT_TYPES() or {}
            specs = {**dict(spec.get("required") or {}), **dict(spec.get("optional") or {})}
        except Exception:  # noqa: BLE001
            specs = {}
    sig = inspect.signature(getattr(node_obj, method))
    kwargs: dict[str, Any] = {}
    for name in sig.parameters:
        if name == "self":
            continue
        if name in explicit:
            kwargs[name] = explicit[name]
        elif name in specs:
            default = _default_from_spec(specs[name])
            if default is not None:
                kwargs[name] = default
    return getattr(node_obj, method)(**kwargs)


def _loader_call(loader_cls: Any, method: str, name_key: str, name_value: str,
                 *, inputs: dict | None = None, overrides: dict | None = None,
                 first: bool = True):
    """按 loader 的 INPUT_TYPES 补齐 widget 参数后调用（适配不同 ComfyUI 版本）。"""
    required = (loader_cls.INPUT_TYPES() or {}).get("required") or {}
    kwargs: dict[str, Any] = {}
    provided = dict(inputs or {})
    for key, decl in required.items():
        if key in provided:
            kwargs[key] = provided[key]
            continue
        default = _default_from_spec(decl)
        if default is not None:
            kwargs[key] = default
    kwargs[name_key] = name_value
    if overrides:
        kwargs.update(overrides)
    result = getattr(loader_cls(), method)(**kwargs)
    if first and isinstance(result, tuple):
        return result[0]
    return result


def _vram_reset() -> bool:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _vram_peak() -> dict:
    try:
        import torch

        if torch.cuda.is_available():
            return {
                "peak_allocated_mb": round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1),
                "peak_reserved_mb": round(torch.cuda.max_memory_reserved() / (1024 * 1024), 1),
                "device": torch.cuda.get_device_name(0),
            }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": "CUDA 不可用"}


def _normalize_audio(audio: Any, *, merge: bool = True) -> dict | None:
    """导演台 audio 是「每段一个」的列表（OUTPUT_IS_LIST）；merge=True 时合并成单个。"""
    if not audio:
        return None
    if isinstance(audio, dict):
        return audio
    if isinstance(audio, (list, tuple)):
        items = [a for a in audio if isinstance(a, dict) and a.get("waveform") is not None]
        if not items:
            return None
        if len(items) == 1 or not merge:
            return items[0]
        import torch

        rate = int(items[0].get("sample_rate") or 0)
        try:
            merged = torch.cat([a["waveform"] for a in items], dim=-1)
        except Exception:  # noqa: BLE001
            merged = items[0]["waveform"]
        return {"waveform": merged, "sample_rate": rate}
    return None


def _write_wav(audio: Any, path: Path) -> bool:
    audio = _normalize_audio(audio)
    if not audio:
        return False
    waveform = audio.get("waveform")
    rate = int(audio.get("sample_rate") or 0)
    if waveform is None or not rate:
        return False
    try:
        data = waveform.detach().float().cpu()
        if data.dim() == 3:
            data = data[0]
        if data.dim() != 2:
            return False
        samples = data.clamp(-1.0, 1.0) * 32767.0
        raw = samples.numpy().astype("int16").T.reshape(-1)
        channels = int(data.shape[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(raw.tobytes())
        return True
    except Exception:  # noqa: BLE001
        return False


def _encode_movie(frames: Any, audio: Any, path: Path, fps: float) -> dict:
    import numpy as np
    import torch

    if isinstance(frames, (list, tuple)):
        frames = torch.cat([f for f in frames if f is not None], dim=0)
    if not isinstance(frames, torch.Tensor) or frames.dim() != 4:
        raise RuntimeError(f"导演台输出的画面张量形状异常：{getattr(frames, 'shape', type(frames))}")
    total, height, width, _channels = frames.shape
    if total <= 0:
        raise RuntimeError("导演台输出 0 帧：无法编码成片。")

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bcu_one_stop_") as tmp:
        tmp_dir = Path(tmp)
        wav = tmp_dir / "audio.wav"
        has_audio = _write_wav(audio, wav)
        cmd = [
            _ffmpeg(), "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
            "-r", str(fps), "-i", "pipe:0",
        ]
        if has_audio:
            cmd += ["-i", str(wav)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd += [str(path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        assert proc.stdin is not None
        step = 48
        for start in range(0, total, step):
            chunk = frames[start:start + step].cpu().numpy()
            chunk = (np.clip(chunk, 0.0, 1.0) * 255.0).astype("uint8")
            proc.stdin.write(chunk.tobytes())
        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg 编码失败（exit={code}）：{stderr[-600:]}")
    return {"frames": int(total), "width": int(width), "height": int(height),
            "fps": float(fps), "has_audio": has_audio,
            "duration_s": round(total / float(fps), 3), "path": str(path)}


def _image_tensor(png: Path):
    import numpy as np
    import torch
    from PIL import Image

    with Image.open(png) as im:
        arr = np.asarray(im.convert("RGB")).astype("float32") / 255.0
    return torch.from_numpy(arr)[None, ...]


def _save_tensor_png(image: Any, path: Path) -> Path:
    import numpy as np
    from PIL import Image

    tensor = image
    if hasattr(tensor, "detach"):
        tensor = tensor.detach().float().cpu()
        if tensor.dim() == 4:
            tensor = tensor[0]
        arr = tensor.numpy()
    else:
        arr = np.asarray(tensor)
    if arr.max() <= 1.0:
        arr = (arr * 255.0).clip(0, 255)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr.astype("uint8")).save(path)
    return path


def _generate_image(model: Any, clip: Any, vae: Any, *, prompt: str, negative: str,
                    width: int, height: int, steps: int, cfg: float, seed: int,
                    sampler: str = "euler", scheduler: str = "normal"):
    from nodes import CLIPTextEncode, EmptyLatentImage, KSampler, VAEDecode

    positive = _invoke(CLIPTextEncode(), "encode", {"clip": clip, "text": prompt}, CLIPTextEncode)[0]
    neg = _invoke(CLIPTextEncode(), "encode", {"clip": clip, "text": negative}, CLIPTextEncode)[0]
    latent = _invoke(EmptyLatentImage(), "generate",
                     {"width": int(width), "height": int(height), "batch_size": 1},
                     EmptyLatentImage)[0]
    samples = _invoke(KSampler(), "sample", {
        "model": model, "positive": positive, "negative": neg,
        "latent_image": latent, "seed": int(seed), "steps": int(steps),
        "cfg": float(cfg), "sampler_name": sampler, "scheduler": scheduler,
        "denoise": 1.0,
    }, KSampler)[0]
    image = _invoke(VAEDecode(), "decode", {"samples": samples, "vae": vae}, VAEDecode)[0]
    return image


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text


# ===========================================================================

# ---------------------------------------------------------------------------
# ROUND49：审核/上下文结构化输出辅助（纯函数，便于单测；不依赖 ComfyUI）
# ---------------------------------------------------------------------------
def _count_assets_by_type(payload: dict) -> dict:
    """E1–E5 数量：assets_by_type 各类别条数（供 UI 直接渲染）。"""
    counts: dict[str, int] = {}
    for asset_type, items in (payload.get("assets_by_type") or {}).items():
        counts[str(asset_type)] = len(items or [])
    return counts


def _build_project_context(payload: dict, 项目: str, project_id: str) -> dict:
    """项目/素材库上下文：项目名/ID、已 LOCK 清单、清单注入、库登记统计（项目续写证据）。"""
    meta = payload.get("meta") or {}
    refs = payload.get("references") or {}
    locked = refs.get("locked_assets") or []
    return {
        "display_name": str(项目 or ""),
        "project_id": str(project_id or ""),
        "mode": meta.get("mode"),
        "api_calls": meta.get("api_calls"),
        "manifest_injected": bool(meta.get("manifest_injected")),
        "library": dict(meta.get("library") or {}),
        "locked_assets": [
            {"asset_id": a.get("asset_id"), "type": a.get("type"),
             "name": a.get("name"), "version": a.get("version")}
            for a in locked
        ],
        "locked_count": len(locked),
        "max_refs": refs.get("max_refs"),
        "note": ("同项目继续生成：沿用现有素材库（不清空、不改 project_id）"
                 if project_id else "未解析到项目：本次不注入/不登记素材库"),
    }


def _map_video_segment_scores(seg_obj: Any) -> list[dict]:
    """视频审核 R_seg 原始 JSON → UI 可直接渲染的段列表。

    P1 修复：审核节点历史版本只写 segment_index/dimensions；这里两种键都兼容，
    并补齐 index/scores/threshold/passed/CNT2/CNT3/fallback/degrade 等字段。
    """
    if not isinstance(seg_obj, dict):
        return []
    rows: list[dict] = []
    for raw in seg_obj.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("index")
        if idx is None:
            idx = raw.get("segment_index")
        dims = raw.get("scores")
        if dims is None:
            dims = raw.get("dimensions")
        counters = raw.get("counters") or {}
        if not isinstance(counters, dict):
            counters = {}
        rows.append({
            "index": int(idx) if isinstance(idx, (int, float)) else idx,
            "segment_index": idx,
            "score": raw.get("score"),
            "scores": dict(dims) if isinstance(dims, dict) else {},
            "dimensions": dict(dims) if isinstance(dims, dict) else {},
            "threshold": raw.get("threshold"),
            "passed": raw.get("passed"),
            "score_source": raw.get("score_source"),
            "file": raw.get("file"),
            "fallback": raw.get("fallback"),
            "degrade": list(raw.get("degrade") or []),
            "counters": counters,
            "cnt2": counters.get("CNT2"),
            "cnt3": counters.get("CNT3"),
            "too_short": bool(raw.get("too_short")),
        })
    rows.sort(key=lambda r: (r["index"] is None, r["index"] if isinstance(r["index"], int) else 10**6))
    return rows


def _parse_need_regenerate(need: Any) -> list[int]:
    """视频审核「需重生成段」字符串（如 "2,3" / "无"）→ 段号数组。"""
    import re

    if isinstance(need, (list, tuple)):
        return [int(x) for x in need if str(x).strip().isdigit()]
    text = str(need or "").strip()
    if not text or text in ("无", "None"):
        return []
    return [int(x) for x in re.findall(r"\d+", text)]


def _asset_review_summary(asset_rows: list[dict], *, threshold: Any,
                          review_mode: str, generated: int,
                          fallback: int = 0) -> dict:
    """素材审核汇总：生成/LOCK/FALLBACK/平均分/逐素材状态（给 UI 与审计末尾一行）。"""
    scores = [float(r["score"]) for r in asset_rows
              if isinstance(r.get("score"), (int, float))]
    locked = [r for r in asset_rows if str(r.get("lock_status")).upper() == "LOCKED"]
    blocked = [r for r in asset_rows if str(r.get("lock_status")).upper() != "LOCKED"]
    return {
        "generated": int(generated),
        "locked": len(locked),
        "blocked": len(blocked),
        "fallback": int(fallback),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "best_score": max(scores) if scores else None,
        "score_count": len(scores),
        "threshold": threshold,
        "review_mode": review_mode,
        "states": [
            {"asset_id": r.get("asset_id"), "name": r.get("name"),
             "type": r.get("type"), "version": r.get("version"),
             "state": r.get("state"), "lock_status": r.get("lock_status"),
             "score": r.get("score")}
            for r in asset_rows
        ],
    }


def _review_threshold_override(threshold: Any):
    """素材审核阈值运行时注入：patch `review.asset_review.resolve_review_params`。

    返回 restore 回调。注入失败（配置对象不可写等）由调用方如实记录并回退配置阈值，
    绝不静默假装按用户阈值审核。
    """
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return None, False
    if value < 0:
        return None, False
    try:
        from bettercomfyui.review import asset_review as ar

        orig = ar.resolve_review_params

        def patched(config=None, *args, **kwargs):
            params = orig(config, *args, **kwargs)
            setattr(params, "score_threshold", value)
            return params

        ar.resolve_review_params = patched

        def restore() -> None:
            ar.resolve_review_params = orig

        return restore, True
    except Exception:  # noqa: BLE001 - 注入不可用时由调用方记录说明
        return None, False


def _prompt_choices() -> list[str]:
    """系统提示词预置标签（供 INPUT_TYPES 使用）。"""
    from .everything_node import SYSTEM_PROMPT_CHOICES

    return list(SYSTEM_PROMPT_CHOICES)


class BetterComfyUIOneStop:
    """一个大节点：剧本进、成片出（提示词/素材/审核/全功能导演台/剪辑/段与整片审核全内部）。"""

    CATEGORY = _CATEGORY
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("成片路径", "审核报告", "提示词JSON", "审计报告", "时间轴JSON", "导演台报告")
    DESCRIPTION = (
        "单节点全流程：剧本 → 一次 API（或占位）→ 全部提示词 → 素材生图+入库+审核(LOCK/兜底) → "
        "参考图挂载 → 全功能导演台（完整 timeline/六模式/首尾帧/ref2v/refVideos/refAudios/"
        "段间连续 22 帧/context-motion-context/SelfLift/Refine/FaceRefine/显存策略/分段导出）"
        "→ Finalize 拼接封装 → 段级 R_seg + 整片 R_full 审核 → 网页可播放。"
        "不依赖外部自定义节点；模型按文件名在节点内部加载。"
    )

    @classmethod
    def INPUT_TYPES(cls):
        unets = _filename_list("diffusion_models") or _filename_list("unet")
        clip_names = _filename_list("text_encoders") or _filename_list("clip")
        vae_names = _filename_list("vae")
        lora_names = [""] + _filename_list("loras")
        ckpts = _filename_list("checkpoints")
        default_ckpt = next((c for c in ckpts if "RealVis" in c), ckpts[0] if ckpts else "")
        detectors = _face_detector_options()
        return {
            "required": {
                # ---------------- A. 剧本与项目 ----------------
                "剧本": ("STRING", {"multiline": True, "default": _DEFAULT_SCRIPT,
                                    "tooltip": "一整段剧本。整条链只允许 1 次 API 调用。"}),
                "项目": ("STRING", {"default": "剧核试用",
                                    "tooltip": "项目显示名或 32 位 id；素材库与输出都写到这里。"}),
                "输出文件名": ("STRING", {"default": "大节点成片.mp4"}),
                # ---------------- 模型 ----------------
                "视频UNET": (unets or [""], {"tooltip": "MiniMax H3 UNET（ComfyUI diffusion_models）"}),
                "CLIP名": (clip_names or [""], {"tooltip": "CLIPLoader type=minimax（qwen3vl）"}),
                "视频VAE": (vae_names or [""], {"tooltip": "minimax_h3_video_vae"}),
                "音频VAE": (vae_names or [""], {"tooltip": "minimax_h3_audio_vae（r2v/v2v/rv2v 必需）"}),
                "视频LoRA": (lora_names, {"default": ""}),
                "LoRA强度": ("FLOAT", {"default": 0.75, "min": 0.0, "max": 2.0, "step": 0.01}),
                "图像模型": (ckpts or [""], {"default": default_ckpt,
                                            "tooltip": "素材生图用的 SDXL checkpoint"}),
                "生成素材图": ("BOOLEAN", {"default": True,
                                          "tooltip": "True = 节点内部生成素材参考图并走审核 LOCK；"
                                                     "False = 只用提示词出片（无参考图）"}),
                "素材最多生成个数": ("INT", {"default": 3, "min": 0, "max": 9}),
                "素材审核轮数": ("INT", {"default": 3, "min": 1, "max": 10,
                                        "tooltip": "同一素材最多生成几个候选版本送审（CNT1）；"
                                                   "达上限按规范从候选里选最高分兜底 FALLBACK 并 LOCK。"}),
                "素材审核模式": (["模型审核（本地启发式）", "人工通过（演示：模拟操作者）"],
                                 {"default": "模型审核（本地启发式）"}),
                "素材审核阈值": ("INT", {"default": -1, "min": -1, "max": 100,
                    "tooltip": "素材审核通过阈值：-1=跟随配置（config.asset_review.score_threshold，默认70）；"
                               ">=0 时运行时注入本次素材审核（不改库源码），并在审计记录实际生效阈值"}),
                "复用已锁定素材": ("BOOLEAN", {"default": True,
                                              "tooltip": "项目里已有 LOCKED 版本时直接复用，不重复生成。"}),
                "图像分辨率": ("INT", {"default": 1024, "min": 256, "max": 2048, "step": 64}),
                "图像步数": ("INT", {"default": 20, "min": 1, "max": 100}),
                "图像CFG": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 30.0, "step": 0.1}),
                # ---------------- B. 导演台：任务/时间轴 ----------------
                "任务类型": (_task_type_options(), {"default": _default_task_type(),
                                                    "tooltip": "六种任务模式 + mixed。默认 r2v（参考主体生视频）。"}),
                "编辑模式": (["自动（按任务类型）", "全局", "分段"], {"default": "自动（按任务类型）"}),
                "时间轴JSON": ("STRING", {"multiline": True, "default": "",
                                          "tooltip": "高级：完整 timeline_data 覆盖（留空=按参数自动构造）。"
                                                     "填了则完全以 JSON 为准，素材参考图不会自动注入。"}),
                "首帧图片路径": ("STRING", {"default": "",
                                            "tooltip": "i2v/fl2v 首帧绝对路径；留空=用第一个已 LOCK 素材图"}),
                "尾帧图片路径": ("STRING", {"default": "",
                                            "tooltip": "fl2v 尾帧绝对路径；留空=用第二个已 LOCK 素材图（若有）"}),
                "源视频路径": ("STRING", {"default": "",
                                          "tooltip": "v2v/rv2v 源视频绝对路径（视频编辑必需）"}),
                "参考视频路径": ("STRING", {"default": "",
                                            "tooltip": "r2v 参考视频（<Video 1>）绝对路径，可选"}),
                "参考音频路径": ("STRING", {"default": "",
                                            "tooltip": "r2v 参考音频（<Audio 1>）绝对路径，可选"}),
                # ---------------- C. 生成尺寸/采样 ----------------
                "每段时长_秒": ("FLOAT", {"default": 5.167, "min": 1.0, "max": 30.0, "step": 0.1}),
                "段数": ("INT", {"default": 1, "min": 1, "max": 360,
                                  "tooltip": "显式段数；与「目标总时长_分钟」取较大者（上限 360 段≈1 小时@10s）"}),
                "目标总时长_分钟": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 180.0,
                                               "step": 1.0,
                                               "tooltip": "0=不启用；>0 时按 目标分钟×60/每段时长 自动算段数（V4 一小时整片）"}),
                "每批段数": ("INT", {"default": 8, "min": 1, "max": 64,
                                     "tooltip": "长片分批生成：每批段数（显存安全）；=或>总段数 则单批"}),
                "断点续跑": ("BOOLEAN", {"default": True,
                                           "tooltip": "同参数重跑时跳过已完成批次（参数指纹变化自动作废）"}),
                "批次间卸载模型": ("BOOLEAN", {"default": True,
                                               "tooltip": "每批结束 unload 模型+清 CUDA 缓存（显存最省；每批多一次加载）"}),
                "帧率": ("FLOAT", {"default": 24.0, "min": 8.0, "max": 60.0, "step": 0.01}),
                "宽度": ("INT", {"default": 864, "min": 64, "max": 4096, "step": 32}),
                "高度": ("INT", {"default": 480, "min": 64, "max": 4096, "step": 32}),
                "参考最长边": ("INT", {"default": 864, "min": 64, "max": 4096, "step": 32}),
                "步数": ("INT", {"default": 6, "min": 1, "max": 200}),
                "采样器": (_samplers(), {"default": "euler"}),
                "调度器": (_schedulers(), {"default": "simple"}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "噪声偏移_视频": ("FLOAT", {"default": 12.0, "min": 0.01, "max": 100.0,
                                           "step": 0.01, "tooltip": "MiniMaxH3SigmaShift shift_video"}),
                "噪声偏移_音频": ("FLOAT", {"default": 3.0, "min": 0.01, "max": 100.0,
                                           "step": 0.01, "tooltip": "MiniMaxH3SigmaShift shift_audio"}),
                # ---------------- D. 段间连续性（核心） ----------------
                "段间连续": ("BOOLEAN", {"default": True,
                                         "tooltip": "「段间引导」主开关：上一段尾部帧+音频 pin 进下一段开头，"
                                                    "生成后裁前缀 → 下一段前 N 帧=上一段后 N 帧。"}),
                "连续帧数": ([5, 22, 39, 56], {"default": 22,
                                             "tooltip": "motion context 帧数，官方基线 22（H3 网格 5/22/39/56）"}),
                "连续模式": (["guide 引导（官方 motion context）",
                             "continue 引导+重绘（latent 续写）"],
                           {"default": "guide 引导（官方 motion context）"}),
                "重绘幅度": ("FLOAT", {"default": 0.10, "min": 0.0, "max": 1.0, "step": 0.01,
                                       "tooltip": "仅 continue 模式生效（vendor continuityRedraw）"}),
                "保完整尾部": ("BOOLEAN", {"default": True,
                                           "tooltip": "continuityKeepTail：保留对齐余量，不裁回 UI 长度"}),
                "每段引用上段": ("BOOLEAN", {"default": True,
                                             "tooltip": "每段 continuityFromPrev（首段永远不 pin）"}),
                "音频上下文帧数": ("INT", {"default": 24, "min": 0, "max": 60,
                                           "tooltip": "官方示例 audio=24 配 video=22（运行时注入 vendor 常量）"}),
                # ---------------- E. 显存/缓存策略 ----------------
                "显存档位": (["自动", "低（最省）", "中", "高（最快）"], {
                    "default": "自动",
                    "tooltip": "JP6：低=缩小批次+批间卸载；中=默认；高=关段间清理、大批次"}),
                "剪枝模型": ("BOOLEAN", {"default": True,
                                          "tooltip": "JP7：批次间卸载模型并清缓存（等效显存剪枝；模型文件本身的剪枝版需用户选择）"}),
                "tiled采样": ("INT", {"default": 0, "min": 0, "max": 16,
                                       "tooltip": "O5：>1 时尝试 vendor 空间分块采样接口；当前 vendor 执行器自建 sampler，未接线会明确记录"}),
                "显存清理_段间": ("BOOLEAN", {"default": True}),
                "显存清理_二采前": ("BOOLEAN", {"default": False}),
                "显存清理_修脸前": ("BOOLEAN", {"default": False}),
                "缓存编码": (["raw", "ffv1"], {"default": "raw"}),
                "缓存目录名": ("STRING", {"default": "",
                                          "tooltip": "分段缓存目录名；留空=只用节点 id"}),
                "音频模式": (["generate 生成（原生音画）", "source 沿用源视频音频", "mute 静音"],
                             {"default": "generate 生成（原生音画）"}),
                "导出原始帧": ("BOOLEAN", {"default": False}),
                "输出修脸前": ("BOOLEAN", {"default": False}),
                # ---------------- F. 可选增强（内部开关，不需连线） ----------------
                "启用SelfLift": ("BOOLEAN", {"default": False,
                                             "tooltip": "渐进一采：低分辨率前缀 + 3D lift + 高分辨率尾段（euler）"}),
                "SelfLift高分步数": ("INT", {"default": 8, "min": 1, "max": 64}),
                "SelfLift低分比例": ("FLOAT", {"default": 0.5, "min": 0.25, "max": 1.0, "step": 0.05}),
                "SelfLift过渡步": ("INT", {"default": 2, "min": 1, "max": 200}),
                "启用Refine": ("BOOLEAN", {"default": False,
                                           "tooltip": "每段二采（同尺寸 refine / 放大后二采）"}),
                "Refine模式": (["refine 同尺寸二采", "upscale 放大后二采"],
                               {"default": "refine 同尺寸二采"}),
                "Refine次数": ("INT", {"default": 1, "min": 1, "max": 3}),
                "Refine目标百万像素": ("FLOAT", {"default": 2.0, "min": 0.1, "max": 16.0, "step": 0.1}),
                "Refine放大方式": (["h3_latent", "latent", "model", "image"],
                                   {"default": "h3_latent"}),
                "启用FaceRefine": ("BOOLEAN", {"default": False,
                                               "tooltip": "成片解码后跟踪人脸→重采样→贴回（需检测器可用）"}),
                "人脸检测器": (detectors, {"default": detectors[0] if detectors else ""}),
                "修脸重绘幅度": ("FLOAT", {"default": 0.4, "min": 0.02, "max": 1.0, "step": 0.01}),
                "启用SemanticBridge": ("BOOLEAN", {"default": False,
                                                   "tooltip": "cond-token 学生 MLP（需自备 adapter 权重）"}),
                "语义桥适配器": ("STRING", {"default": ""}),
                "语义桥alpha": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.01}),
                # ---------------- G. 剪辑与输出 ----------------
                "走Finalize剪辑": ("BOOLEAN", {"default": True,
                                               "tooltip": "True = 分段 mp4 → 本项目 BetterComfyUIFinalize"
                                                          "（pipeline.splice/encode+自检）拼接封装；"
                                                          "False = 节点内 ffmpeg 直编整片"}),
                "剪辑CRF": ("INT", {"default": 18, "min": 0, "max": 51}),
                "转场": (["无", "淡入淡出"], {"default": "无",
                                        "tooltip": "ENC2：段间 xfade 交叉淡化 0.5s（真实现，失败会明确记录）"}),
                "字幕": (["无", "自动字幕"], {"default": "无",
                                        "tooltip": "ENC3：把各段提示词生成 SRT 并真烧录到画面"}),
                "水印": (["无", "文字水印"], {"default": "无",
                                        "tooltip": "ENC4：右下角文字水印 PNG（真叠加）"}),
                "水印文字": ("STRING", {"default": "剧核 DramaCore"}),
                "封面帧": (["无", "第一帧"], {"default": "第一帧",
                                        "tooltip": "OUT4：write_output 生成 first_frame 封面 jpg（默认第一帧，可选关闭）"}),
                "输出格式": (["mp4（H.264）", "mov（H.264）", "webm（VP9/Opus）"],
                             {"default": "mp4（H.264）",
                              "tooltip": "ENC5：mp4/mov 走 pipeline.encode；webm 用 ffmpeg VP9/Opus"}),
                # ---------------- H. 运行/审核阈值 ----------------
                "系统提示词选择": (["跟随面板配置（推荐）"] + list(_prompt_choices()), {
                    "default": "跟随面板配置（推荐）",
                    "tooltip": "跟随面板：用右侧「剧核」面板保存的默认系统提示词；也可在节点上强制指定。"}),
                "自定义系统提示词": ("STRING", {"multiline": True, "default": "",
                    "tooltip": "非空时覆盖系统提示词选择（只对真实 API 模式生效）。"}),
                "输出语言": (["跟随面板配置（推荐）", "中文", "英文"], {
                    "default": "跟随面板配置（推荐）"}),
                "分镜粒度": (["跟随面板配置（推荐）", "按镜头", "按场景"], {
                    "default": "跟随面板配置（推荐）"}),
                "使用占位结果": ("BOOLEAN", {"default": False}),
                "审核阈值": ("INT", {"default": 70, "min": 0, "max": 100,
                                     "tooltip": "整片本地启发式均分阈值"}),
                "段阈值覆盖": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 100.0}),
                "整片阈值覆盖": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 100.0}),
                "整片失败策略": (_FULL_POLICIES, {"default": _FULL_POLICIES[0]}),
                "边界相似度阈值": ("FLOAT", {"default": 55.0, "min": 0.0, "max": 100.0, "step": 1.0}),
                "自动返工": ("BOOLEAN", {"default": True,
                                          "tooltip": "SG5 U：段不过→自动重生成该段→重审（CNT2）；关闭则只输出需重生成清单"}),
                "CNT2上限": ("INT", {"default": 1, "min": 1, "max": 5,
                                       "tooltip": "每段质量返工上限（达上限取最高分候选，不假成功）"}),
                "级联返工": ("BOOLEAN", {"default": True,
                                           "tooltip": "N1/CNT3：返工段的后一段重新生成并级联验证（上限 CNT3上限）"}),
                "CNT3上限": ("INT", {"default": 1, "min": 0, "max": 5}),
            },
            "optional": {
                "api_key": ("STRING", {"default": ""}),
                "model": ("STRING", {"default": ""}),
                "api_base": ("STRING", {"default": ""}),
                "统一风格": ("STRING", {"multiline": True, "default": ""}),
                "出片": ("BOOLEAN", {"default": True}),
            },
        }

    # ==================================================================
    def run(
        self,
        剧本: str,
        项目: str,
        输出文件名: str,
        视频UNET: str,
        CLIP名: str,
        视频VAE: str,
        音频VAE: str,
        视频LoRA: str = "",
        LoRA强度: float = 0.75,
        图像模型: str = "",
        生成素材图: bool = True,
        素材最多生成个数: int = 3,
        素材审核轮数: int = 3,
        素材审核模式: str = "模型审核（本地启发式）",
        素材审核阈值: int = -1,
        复用已锁定素材: bool = True,
        图像分辨率: int = 1024,
        图像步数: int = 20,
        图像CFG: float = 6.0,
        任务类型: str = "",
        编辑模式: str = "自动（按任务类型）",
        时间轴JSON: str = "",
        首帧图片路径: str = "",
        尾帧图片路径: str = "",
        源视频路径: str = "",
        参考视频路径: str = "",
        参考音频路径: str = "",
        每段时长_秒: float = 5.167,
        段数: int = 1,
        目标总时长_分钟: float = 0.0,
        每批段数: int = 8,
        断点续跑: bool = True,
        批次间卸载模型: bool = True,
        帧率: float = 24.0,
        宽度: int = 864,
        高度: int = 480,
        参考最长边: int = 864,
        步数: int = 6,
        采样器: str = "euler",
        调度器: str = "simple",
        cfg: float = 1.0,
        seed: int = 0,
        噪声偏移_视频: float = 12.0,
        噪声偏移_音频: float = 3.0,
        段间连续: bool = True,
        连续帧数: int = 22,
        连续模式: str = "guide 引导（官方 motion context）",
        重绘幅度: float = 0.10,
        保完整尾部: bool = True,
        每段引用上段: bool = True,
        音频上下文帧数: int = 24,
        显存档位: str = "自动",
        剪枝模型: bool = True,
        tiled采样: int = 0,
        显存清理_段间: bool = True,
        显存清理_二采前: bool = False,
        显存清理_修脸前: bool = False,
        缓存编码: str = "raw",
        缓存目录名: str = "",
        音频模式: str = "generate 生成（原生音画）",
        导出原始帧: bool = False,
        输出修脸前: bool = False,
        启用SelfLift: bool = False,
        SelfLift高分步数: int = 8,
        SelfLift低分比例: float = 0.5,
        SelfLift过渡步: int = 2,
        启用Refine: bool = False,
        Refine模式: str = "refine 同尺寸二采",
        Refine次数: int = 1,
        Refine目标百万像素: float = 2.0,
        Refine放大方式: str = "h3_latent",
        启用FaceRefine: bool = False,
        人脸检测器: str = "",
        修脸重绘幅度: float = 0.4,
        启用SemanticBridge: bool = False,
        语义桥适配器: str = "",
        语义桥alpha: float = 0.15,
        走Finalize剪辑: bool = True,
        剪辑CRF: int = 18,
        转场: str = "无",
        字幕: str = "无",
        水印: str = "无",
        水印文字: str = "剧核 DramaCore",
        封面帧: str = "无",
        输出格式: str = "mp4（H.264）",
        系统提示词选择: str = "跟随面板配置（推荐）",
        自定义系统提示词: str = "",
        输出语言: str = "跟随面板配置（推荐）",
        分镜粒度: str = "跟随面板配置（推荐）",
        使用占位结果: bool = False,
        审核阈值: int = 70,
        段阈值覆盖: float = -1.0,
        整片阈值覆盖: float = -1.0,
        整片失败策略: str = "按项目配置",
        边界相似度阈值: float = 55.0,
        自动返工: bool = True,
        CNT2上限: int = 1,
        级联返工: bool = True,
        CNT3上限: int = 1,
        api_key: str = "",
        model: str = "",
        api_base: str = "",
        统一风格: str = "",
        出片: bool = True,
    ):
        from . import director_runtime as dr

        started = time.time()
        audit: list[str] = ["=== 剧核单节点全流程（剧本→成片·全功能导演台）审计 ==="]
        timings: dict[str, float] = {}
        warnings: list[str] = []
        unimplemented: list[str] = []
        _vram_reset()

        # ---- SG6 选项（v0.4：转场/字幕/水印/封面/格式已实现，见 director_edit）----
        from . import director_edit as de
        from . import director_longform as lf

        _fmt = de.resolve_format(str(输出格式))
        _edit_options = {
            "format_label": str(输出格式), "crf": int(剪辑CRF),
            "transition_label": str(转场), "subtitle_label": str(字幕),
            "watermark_label": str(水印), "cover_label": str(封面帧),
            "watermark_text": str(水印文字 or "剧核 DramaCore"),
            "fade_seconds": de.DEFAULT_FADE_SECONDS,
            "segment_seconds": float(每段时长_秒),
            "audio_join": "mute" if str(音频模式).lower().startswith("mute") else "auto",
        }
        audit.append(
            "SG6 剪辑配置：格式={} CRF={} 转场={} 字幕={} 水印={} 封面={}".format(
                _fmt, int(剪辑CRF), 转场, 字幕, 水印, 封面帧))
        if tiled采样 and int(tiled采样) > 1:
            unimplemented.append(f"tiled采样 {tiled采样} 块（vendor 执行器自建 sampler，运行时未接线）")
            warnings.append(
                f"⚠️ tiled采样={tiled采样}：vendor 执行器内部自建 sampler 对象，当前版本无法在不改 "
                "vendor 源码的前提下注入 → 已记录为未接线（不冒充生效）")

        # ---- 素材审核阈值：-1=跟随配置；记录实际生效值（不假接）----
        try:
            from bettercomfyui.config import load_config as _load_cfg_r49

            _cfg_r49 = _load_cfg_r49(repo_root=self._repo_root())
            _cfg_asset_threshold = float(
                getattr(_cfg_r49.asset_review, "score_threshold", 70.0))
        except Exception as exc:  # noqa: BLE001 - 配置读取失败要说明
            _cfg_asset_threshold = 70.0
            warnings.append(f"读取配置素材审核阈值失败（用默认70）：{type(exc).__name__}: {exc}")
        _asset_threshold_effective = (
            int(素材审核阈值) if int(素材审核阈值) >= 0 else int(_cfg_asset_threshold))
        audit.append(
            "素材审核阈值：{}（{}；配置值={}）".format(
                _asset_threshold_effective,
                "节点参数" if int(素材审核阈值) >= 0 else "跟随配置",
                _cfg_asset_threshold))

        # ---- 1) 剧本 → 全部提示词（复用总控台；全程最多 1 次 API）----
        from .everything_node import BetterComfyUIEverything

        t0 = time.time()
        # 面板默认系统提示词/输出语言/分镜粒度 → 真实传进总控台（P0-2）
        from bettercomfyui.config import load_config

        from .everything_node import SYSTEM_PROMPT_CHOICES

        try:
            _cfg = load_config(repo_root=self._repo_root())
            _panel_key = str(getattr(_cfg.prompt, "system_prompt_template", "") or "")
            _panel_lang = str(getattr(_cfg.prompt, "output_language", "") or "中文")
            _panel_gran = str(getattr(_cfg.prompt, "story_granularity", "") or "按镜头")
        except Exception as exc:  # noqa: BLE001 - 读配置失败要写审计，不静默
            _panel_key, _panel_lang, _panel_gran = "", "中文", "按镜头"
            warnings.append(f"读取面板默认提示词配置失败（已用内置默认）：{type(exc).__name__}: {exc}")
        if str(系统提示词选择).startswith("跟随"):
            _sys_label = next(
                (label for label, key in SYSTEM_PROMPT_CHOICES.items() if key == _panel_key),
                next(iter(SYSTEM_PROMPT_CHOICES)))
            _custom_prompt = "" if _panel_key in SYSTEM_PROMPT_CHOICES.values() else _panel_key
        else:
            _sys_label = str(系统提示词选择)
            _custom_prompt = str(自定义系统提示词 or "")
        if str(输出语言).startswith("跟随"):
            _lang = "中文" if str(_panel_lang).lower() in ("zh", "中文", "chinese") else "英文"
        else:
            _lang = str(输出语言)
        if str(分镜粒度).startswith("跟随"):
            _gran = "按镜头" if str(_panel_gran).lower() in ("shot", "按镜头", "shot_by_shot") else "按场景"
        else:
            _gran = str(分镜粒度)
        audit.append(
            f"提示词配置：系统模板={_sys_label} | 自定义={'有' if _custom_prompt else '无'} | "
            f"输出语言={_lang} | 分镜粒度={_gran}（面板默认={'是' if str(系统提示词选择).startswith('跟随') else '否'}）")
        _groups, _script, prompt_json, _asset_lines, everything_audit = BetterComfyUIEverything().run(
            剧本=剧本, 项目=项目, 统一风格=统一风格, 每段时长_秒=float(每段时长_秒),
            系统提示词选择=_sys_label, 自定义系统提示词=_custom_prompt,
            输出语言=_lang, 分镜粒度=_gran,
            # ROUND49：审核模式/阈值真实传给总控台（影响素材清单提示与审计口径）
            素材审核模式=("人工审核" if str(素材审核模式).startswith("人工") else "模型审核"),
            素材审核阈值=int(_asset_threshold_effective),
            写入素材库=True, 启用素材库=True,
            使用占位结果=bool(使用占位结果), api_key=api_key, model=model, api_base=api_base,
        )
        timings["提示词生成_s"] = round(time.time() - t0, 2)
        payload = json.loads(prompt_json)
        video_prompts = list(payload.get("video_prompts") or [])[:max(1, int(段数))]
        if not video_prompts:
            raise RuntimeError("剧本拆解没有产出任何视频段：请检查剧本或提示词模板。")
        project_id = str((payload.get("meta") or {}).get("project_id") or "")
        project_dir = self._project_dir(project_id)
        project_context = _build_project_context(payload, 项目, project_id)
        asset_type_counts = _count_assets_by_type(payload)
        audit.append(f"提示词模式：{payload.get('meta', {}).get('mode') or '未知'} "
                     f"(API 调用 {project_context.get('api_calls')} 次)")
        audit.append(f"素材库：复用已 LOCK {project_context['locked_count']} 张"
                     f"（项目 {项目 or project_id[:8] or '未解析'}）"
                     + ("" if project_context["locked_count"] else "——身份参考图将为 0，"
                                                                   "请先生成素材并通过审核 LOCK"))
        audit.append("项目上下文：project_id={} display={} 清单注入={} 库登记={}".format(
            project_id or "（无）", 项目 or "（无）", project_context.get("manifest_injected"),
            project_context.get("library")))
        audit.append("E1–E5 素材提示词数量：" + json.dumps(asset_type_counts, ensure_ascii=False))
        audit.append(f"段数：{len(video_prompts)} / 每段 {每段时长_秒}s / {宽度}x{高度}@{帧率}fps")
        audit.extend(line for line in str(everything_audit).splitlines() if line.strip())

        if not 出片:
            out = project_dir / "output" / "仅提示词.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(prompt_json, encoding="utf-8")
            audit.append("出片=False：只产出提示词，未调用图像/视频模型。")
            return {"ui": {"text": ["\n".join(audit)]},
                    "result": (str(out), "未审核（只出提示词）", prompt_json,
                               "\n".join(audit), "", "")}

        # ---- 2) 素材生图（多候选轮）→ 入库 → 审核 LOCK/兜底 ----
        asset_report: dict[str, Any] = {
            "enabled": bool(生成素材图 and 图像模型), "created": [], "locked": [],
            "blocked": [], "reused": [], "refs_attached": 0, "errors": [],
            "rounds": [], "review_mode": 素材审核模式,
            "review_threshold": int(素材审核阈值),
            "review_summary": {}, "assets": [],
        }
        asset_tensors: list[Any] = []
        asset_paths: list[str] = []
        if 生成素材图 and 图像模型:
            try:
                t0 = time.time()
                asset_report = self._generate_and_review_assets(
                    payload=payload, project_id=project_id, project_dir=project_dir,
                    ckpt=图像模型, limit=int(素材最多生成个数),
                    rounds=int(素材审核轮数), review_mode=str(素材审核模式),
                    review_threshold=int(_asset_threshold_effective),
                    reuse_locked=bool(复用已锁定素材),
                    resolution=int(图像分辨率), steps=int(图像步数), cfg=float(图像CFG),
                    seed=int(seed), audit=audit, warnings=warnings,
                )
                timings["素材生图审核_s"] = round(time.time() - t0, 2)
                asset_tensors = list(asset_report.pop("_tensors", []))
                asset_paths = list(asset_report.pop("_paths", []))
            except Exception as exc:  # noqa: BLE001 - 素材失败不静默，写进审计但继续出片
                asset_report["errors"].append(f"{type(exc).__name__}: {exc}")
                audit.append(f"⚠️ 素材生图/审核失败（继续出片，无参考图）：{type(exc).__name__}: {exc}")
            finally:
                self._free_models()
        else:
            if bool(复用已锁定素材) and int(project_context.get("locked_count") or 0) > 0:
                try:
                    t0 = time.time()
                    asset_report = self._reuse_locked_assets(
                        payload=payload, project_id=project_id,
                        limit=int(素材最多生成个数), audit=audit, warnings=warnings)
                    timings["素材复用_s"] = round(time.time() - t0, 2)
                    asset_tensors = list(asset_report.pop("_tensors", []))
                    asset_paths = list(asset_report.pop("_paths", []))
                    audit.append(
                        f"素材生图：未启用；已从项目库复用 LOCKED {len(asset_tensors)} 张作为参考图"
                        "（续写模式，不重新生成，避免人物漂移）")
                except Exception as exc:  # noqa: BLE001
                    asset_report["errors"].append(f"{type(exc).__name__}: {exc}")
                    audit.append(f"⚠️ 复用项目素材失败（继续出片，无参考图）：{type(exc).__name__}: {exc}")
            else:
                audit.append("素材生图：未启用（无图像模型或 生成素材图=False）")
        _asum = asset_report.get("review_summary") or {}
        audit.append(
            "素材结论：生成 {} / 复用LOCKED {} / LOCKED {} (FALLBACK {}) / 未LOCK {} / "
            "平均分 {} / 阈值 {} / 挂参考图 {}".format(
                _asum.get("generated", len(asset_report.get("created", []))),
                len(asset_report.get("reused", [])),
                _asum.get("locked", len(asset_report.get("locked", []))),
                _asum.get("fallback", 0),
                _asum.get("blocked", len(asset_report.get("blocked", []))),
                _asum.get("avg_score"), _asum.get("threshold"), len(asset_tensors))
        )
        for row in asset_report.get("rounds", []):
            audit.append(
                f"  素材候选 {row.get('name')} {row.get('version')}: state={row.get('state')} "
                f"score={row.get('score')} 画质={ (row.get('scores') or {}).get('画质') } "
                f"构图={ (row.get('scores') or {}).get('构图') }"
            )
        vram_after_assets = _vram_peak()

        # ---- 3) 构造完整 timeline_data（全功能导演台入参）----
        task_type = str(任务类型 or _default_task_type())
        task_key = dr.resolve_task_key(task_type)
        seg_plan = lf.plan_segment_count(
            requested_segments=int(段数), target_minutes=float(目标总时长_分钟),
            segment_seconds=float(每段时长_秒), max_segments=360)
        seg_count = int(seg_plan["chosen_segments"])
        audit.append(
            "长片段数规划：显式段数={} 目标时长={}min → 自动段数={}；采用 {} 段（约 {:.2f} 分钟）{}".format(
                seg_plan["requested_segments"], seg_plan["target_minutes"],
                seg_plan["auto_segments_from_target"], seg_count,
                seg_plan["planned_minutes"],
                "（超上限已截断）" if seg_plan["capped_by_max_segments"] else ""))
        _base_prompts = [str(text) for text in video_prompts]
        if not _base_prompts:
            raise RuntimeError("剧本拆解没有产出任何视频段：请检查剧本或提示词模板。")
        prompts = [_base_prompts[i % len(_base_prompts)] for i in range(seg_count)]
        negative = _NEGATIVE
        continuity_enabled = bool(段间连续) and seg_count >= 2
        overlap = int(dr.snap_context_frames(连续帧数))
        cont_mode = "continue" if "continue" in str(连续模式).lower() else "guide"
        audio_mode = str(音频模式).split(" ", 1)[0].strip().lower()
        if audio_mode not in ("generate", "source", "mute"):
            audio_mode = "generate"
        # P0-4 修复：统一用 exportMode=all，导演台返回全部帧后由本节点按段切分；
        # segments 模式会把分段 mp4 落盘、images_out 只给剩余片段（实测 2 段只回 126 帧）
        export_mode = "all"
        out_cfg = dr.output_config(
            width=int(宽度), height=int(高度), ref_max_size=int(参考最长边),
            export_mode=export_mode, audio_mode=audio_mode,
            continuity_enabled=continuity_enabled,
            continuity_overlap_frames=overlap,
            continuity_mode=cont_mode, continuity_redraw=float(重绘幅度),
            continuity_keep_tail=bool(保完整尾部),
        )

        timeline_json = ""
        timeline_source = "auto"
        timeline_obj: dict | None = None
        if str(时间轴JSON or "").strip():
            try:
                timeline_obj = dr.normalize_timeline_json(str(时间轴JSON))
            except Exception as exc:  # noqa: BLE001 - 明确报错，不执行错误时间轴
                raise RuntimeError(f"时间轴JSON 校验失败：{type(exc).__name__}: {exc}") from exc
            timeline_json = json.dumps(timeline_obj, ensure_ascii=False)
            timeline_source = "user_json"
            seg_specs: list[dict] = []
            audit.append("时间轴：使用用户提供的完整 timeline_data 覆盖（素材参考图不自动注入；"
                         "用户时间轴不做分批，单次执行）")
        else:
            segments: list[dict] = []
            # 任务模式的输入媒体（全部由节点内部登记到 ComfyUI input）
            first_image_ref = None
            end_image_ref = None
            ref_videos: list[dict] = []
            ref_audios: list[dict] = []
            source_video = None
            try:
                if task_key in ("i2v", "fl2v"):
                    start_path = str(首帧图片路径 or "").strip() or (
                        asset_paths[0] if asset_paths else "")
                    if not start_path:
                        raise RuntimeError(
                            f"任务={task_key} 需要首帧图片：请填「首帧图片路径」，"
                            "或启用素材生图并至少 LOCK 一张素材图。"
                        )
                    first_image_ref = dr.image_ref(start_path)
                    end_path = str(尾帧图片路径 or "").strip() or (
                        asset_paths[1] if len(asset_paths) > 1 else "")
                    if end_path:
                        end_image_ref = dr.image_ref(end_path, index=1)
                if task_key in ("v2v", "rv2v"):
                    src = str(源视频路径 or "").strip()
                    if not src:
                        raise RuntimeError(f"任务={task_key} 需要「源视频路径」（视频编辑）")
                    source_video = dr.register_input_file(src)
                if str(参考视频路径 or "").strip():
                    ref_videos = [dr.video_ref(str(参考视频路径).strip(), index=0)]
                if str(参考音频路径 or "").strip():
                    ref_audios = [dr.audio_ref(str(参考音频路径).strip(), index=0)]
                base_refs = [
                    dr.image_ref(path, index=idx) for idx, path in enumerate(asset_paths)
                ]
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"参考媒体登记失败（{type(exc).__name__}: {exc}）；"
                    "请检查路径是否存在、ComfyUI input 目录是否可写。"
                ) from exc

            for idx, prompt in enumerate(prompts):
                seg: dict[str, Any] = {
                    "prompt": prompt,
                    "duration_sec": float(每段时长_秒),
                    "task_type": task_type,
                    "negative_prompt": negative,
                    "continuityFromPrev": bool(每段引用上段) and idx > 0,
                    "frames": int(dr.duration_to_frames(float(每段时长_秒), float(帧率))),
                }
                if task_key == "t2v":
                    seg["taskType"] = task_type
                elif task_key == "i2v":
                    # i2v：首帧作为 genImage（ImageToVideo 的 first_frame），不挂 r2v refs
                    seg["genImage"] = first_image_ref
                    seg["refs"] = []
                elif task_key == "fl2v":
                    seg["startImage"] = first_image_ref
                    seg["endImage"] = end_image_ref
                elif task_key == "r2v":
                    seg["refs"] = [dict(r) for r in base_refs]
                    if idx >= len(seg["refs"]):
                        seg["refs"] = list(seg["refs"])
                elif task_key in ("v2v", "rv2v"):
                    if task_key == "rv2v":
                        seg["refs"] = [dict(r) for r in base_refs]
                else:  # mixed：每段沿用全局任务（用户可用时间轴JSON完全自定义）
                    seg["refs"] = [dict(r) for r in base_refs]
                if ref_videos and task_key == "r2v":
                    seg["refVideos"] = [dict(v) for v in ref_videos]
                if ref_audios and task_key == "r2v":
                    seg["refAudios"] = [dict(a) for a in ref_audios]
                segments.append(seg)

            timeline_json = dr.build_timeline_json(
                task_type=task_type, segments=segments,
                global_prompt=prompts[0] if prompts else "",
                width=int(宽度), height=int(高度), frame_rate=float(帧率),
                ref_max_size=int(参考最长边), output=out_cfg,
                negative_prompt=negative, source_video=source_video,
            )
            seg_specs = list(segments)
            audit.append(f"时间轴：自动构造（task={task_key}，{len(segments)} 段，"
                         f"每段参考图 {len(base_refs)} 张，refVideos {len(ref_videos)}，"
                         f"refAudios {len(ref_audios)}）")
        tl_summary = dr.timeline_summary(timeline_json)
        total_frames = int(tl_summary.get("totalFrames") or 0)
        audit.append(
            f"导演台时间轴：mode={tl_summary.get('timelineMode')} edit={tl_summary.get('editMode')} "
            f"段={tl_summary.get('segmentCount')} 总帧={total_frames} "
            f"连续性={tl_summary['output'].get('continuityEnabled')}"
            f"/{tl_summary['output'].get('continuityOverlapFrames')}f"
            f"({tl_summary['output'].get('continuityMode')}) 音轨={tl_summary['output'].get('audioMode')}"
        )

        # ---- 4) 可选包（SelfLift/Refine/FaceRefine/SemanticBridge）内部生成 ----
        packs = {
            "selflift": dr.build_selflift_pack(
                bool(启用SelfLift), highres_steps=int(SelfLift高分步数),
                lowres_scale=float(SelfLift低分比例), transition_step=int(SelfLift过渡步)),
            "refine": dr.build_refine_pack(
                bool(启用Refine), mode="upscale" if "upscale" in str(Refine模式).lower() else "refine",
                passes=int(Refine次数), megapixels=float(Refine目标百万像素),
                upscale_method=str(Refine放大方式)),
            "face_refine": dr.build_face_refine_pack(
                bool(启用FaceRefine), detector=str(人脸检测器 or ""),
                denoise=float(修脸重绘幅度)),
            "semantic_bridge": dr.build_semantic_bridge_pack(
                bool(启用SemanticBridge), adapter=str(语义桥适配器 or ""),
                alpha=float(语义桥alpha)),
        }
        pack_summary = dr.optional_pack_summary(**packs)
        audit.append(
            "可选包：SelfLift={} Refine={} FaceRefine={} SemanticBridge={}".format(
                pack_summary["selflift"], pack_summary["refine"],
                pack_summary["face_refine"], pack_summary["semantic_bridge"])
        )
        audio_ctx = dr.install_audio_context_frames(int(音频上下文帧数))
        audit.append(f"音频上下文帧数：{audio_ctx['applied']}（注入模块 {audio_ctx['modules']}；"
                     f"vendor 默认 24）")
        if 启用SemanticBridge and not pack_summary["semantic_bridge"]:
            warnings.append("⚠️ 启用SemanticBridge 但未填「语义桥适配器」→ 未生效（需自备权重）")

        # ---- 5) 长片分批生成（V4：自动分段/多批/断点续跑/显存档位/剪枝）----
        seg_dir = project_dir / "_work" / f"onestop_seg_{int(time.time())}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        ctx = {
            "seg_specs": seg_specs, "task_type": task_type, "task_key": task_key,
            "global_prompt": pts0(prompts), "prompts": prompts,
            "out_cfg": out_cfg, "negative": negative,
            "时间轴JSON": str(时间轴JSON or ""),
            "project_dir": project_dir, "project_id": project_id, "项目": 项目,
            "seg_dir": seg_dir, "audit": audit, "warnings": warnings, "timings": timings,
            "packs": packs, "models": None, "tail_ref": None, "reports": [],
            "batch_rows": [],
            "视频UNET": 视频UNET, "CLIP名": CLIP名, "视频VAE": 视频VAE,
            "音频VAE": 音频VAE, "视频LoRA": 视频LoRA, "LoRA强度": float(LoRA强度),
            "宽度": int(宽度), "高度": int(高度), "参考最长边": int(参考最长边),
            "帧率": float(帧率), "每段时长_秒": float(每段时长_秒),
            "步数": int(步数), "采样器": str(采样器), "调度器": str(调度器),
            "cfg": float(cfg), "seed": int(seed),
            "噪声偏移_视频": float(噪声偏移_视频), "噪声偏移_音频": float(噪声偏移_音频),
            "显存清理_段间": bool(显存清理_段间),
            "显存清理_二采前": bool(显存清理_二采前),
            "显存清理_修脸前": bool(显存清理_修脸前),
            "缓存编码": str(缓存编码), "缓存目录名": str(缓存目录名 or ""),
            "导出原始帧": bool(导出原始帧), "输出修脸前": bool(输出修脸前),
            "每批段数": int(每批段数), "断点续跑": bool(断点续跑),
            "批次间卸载模型": bool(批次间卸载模型),
            "显存档位": str(显存档位), "剪枝模型": bool(剪枝模型),
            "段间连续": bool(段间连续), "连续帧数": int(连续帧数),
            "连续模式": cont_mode, "每段引用上段": bool(每段引用上段),
            "自动返工": bool(自动返工), "CNT2上限": int(CNT2上限),
            "级联返工": bool(级联返工), "CNT3上限": int(CNT3上限),
            "段阈值覆盖": float(段阈值覆盖), "边界相似度阈值": float(边界相似度阈值),
        }
        t0 = time.time()
        gen = self._generate_batches(ctx)
        timings["长片分批生成_s"] = round(time.time() - t0, 2)
        timings["出片_s"] = timings["长片分批生成_s"]
        seg_files: list[str] = list(gen["seg_files"])
        batch_rows = list(gen["batch_rows"])
        reports_text = list(gen["reports"])
        frame_counts = list(gen["frame_counts"])
        vram_after_generate = _vram_peak()
        report = "\n\n".join(reports_text)
        cont_ev = dr.continuity_evidence(report)
        vram_after_load = _vram_peak()
        audit.append(
            "段间连续实证：ON={} mode={} overlap={}f keep_tail={} pin段={} 硬切段={}".format(
                cont_ev["continuity_enabled_in_plan"], cont_ev["mode"],
                cont_ev["overlap_frames"], cont_ev["keep_tail"],
                cont_ev["pinned_segments"], cont_ev["hard_cut_segments"]))
        for line in cont_ev["evidence_lines"]:
            audit.append(f"  [导演台] {line}")
        if 段间连续 and seg_count >= 2 and not cont_ev["continuity_enabled_in_plan"]:
            warnings.append("⚠️ 段间连续已勾选但导演台报告显示未启用：请检查时间轴 continuityEnabled")
        for note in cont_ev["skip_notes"]:
            warnings.append(f"⚠️ 段间连续被跳过：{note}")
        audit.append(f"长片生成：{len(seg_files)} 段 / {len(batch_rows)} 批；"
                     f"总耗时 {timings['长片分批生成_s']}s；检查点={gen.get('checkpoint') or '未启用'}")
        for row in batch_rows:
            audit.append("  批次{} 段{}-{} 状态={} 耗时={}s{}".format(
                row.get("batch_index", 0) + 1, row["segments"][0], row["segments"][1],
                row.get("status"), row.get("elapsed_s"),
                "（复用检查点）" if row.get("reused") else ""))
        ctx["frame_counts"] = frame_counts
        ctx["batch_rows"] = batch_rows

        # ---- 5b) 自动返工/级联（CNT2/CNT3）先于最终剪辑：R_seg 只审段 ----
        rework = self._auto_rework(
            ctx=ctx, seg_files=list(seg_files), project_id=project_id,
            project_dir=project_dir, audit=audit, warnings=warnings)
        timings["自动返工_s"] = float(rework.get("elapsed_s") or 0.0)
        seg_files = list(rework.get("seg_files") or seg_files)
        for line in (["自动返工禁用：" + str(rework.get("note") or "")] if rework.get("note") else []):
            audit.append(line)
        if rework.get("rounds"):
            audit.append("自动返工轮次：" + json.dumps(rework["rounds"], ensure_ascii=False)[:600])

        # ---- 5c) SG6 剪辑 + OUT1–OUT4 元数据（Finalize 或 pipeline，失败明确降级）----
        from . import director_longform as lf2

        _manifest_specs = seg_specs or [
            {"prompt": prompts[i % len(prompts)] if prompts else "",
             "duration_sec": float(每段时长_秒), "task_type": task_type}
            for i in range(len(seg_files))]
        seg_manifest = lf2.build_segment_manifest(
            project_id=project_id, segment_specs=_manifest_specs, seg_files=seg_files,
            cnt2=rework.get("cnt2"), cnt3=rework.get("cnt3"),
            fallback_details=rework.get("fallback"))
        metadata_extra = {
            "script_id": str((payload.get("meta") or {}).get("project_id") or "script")[:24],
            "workflow_version": "round46",
            "node_version": "v0.4-longform-rework",
            "task_type": task_type, "task_key": task_key,
            "width": int(宽度), "height": int(高度), "fps": float(帧率),
            "segment_seconds": float(每段时长_秒), "segments": len(seg_files),
            "steps": int(步数), "cfg": float(cfg), "seed": int(seed),
            "sampler": str(采样器), "scheduler": str(调度器),
            "unet": 视频UNET, "clip": CLIP名, "video_vae": 视频VAE,
            "audio_vae": 音频VAE, "lora": 视频LoRA,
            "continuity": cont_ev, "longform_batches": batch_rows,
            "auto_rework": {"rounds": rework.get("rounds"),
                            "cnt2": rework.get("cnt2"), "cnt3": rework.get("cnt3"),
                            "fallback": rework.get("fallback")},
        }
        provenance = {
            "is_mock": bool(使用占位结果),
            "executor": "BetterComfyUIOneStop + vendored MiniMaxH3Director",
            "reason": "占位提示词模式（无真实 API Key，提示词由模板生成）" if 使用占位结果
            else "真实提示词模式（API 或本地提示词模型）",
            "unverified": list(warnings)[:50],
        }
        t0 = time.time()
        try:
            edit = de.finish_movie(
                seg_files=seg_files, work_dir=project_dir / "_work" / f"edit_{int(time.time())}",
                project_dir=project_dir, project_id=project_id,
                project_display_name=str(项目), output_filename=str(输出文件名),
                options={**_edit_options, "prompts": prompts}, manifest=seg_manifest,
                metadata_extra=metadata_extra, provenance=provenance,
                use_finalize=bool(走Finalize剪辑), audit=audit, warnings=warnings)
            final_path = Path(edit["final_path"])
            finalize_report = edit
            timings["Finalize剪辑_s" if 走Finalize剪辑 and not edit.get("encode") else "pipeline封装_s"] = round(
                time.time() - t0, 2)
            audit.append(f"剪辑收尾：{final_path}（steps={len(edit.get('steps') or [])}）")
        except Exception as exc:  # noqa: BLE001 - 收尾失败必须明确降级，不冒充转场/字幕生效
            warnings.append(f"⚠️ SG6 剪辑收尾失败（退回流复制拼接+项目 output，"
                            f"转场/字幕/水印未生效，如实记录）：{type(exc).__name__}: {exc}")
            work_fb = project_dir / "_work" / f"fallback_{int(time.time())}"
            fb = de.splice_segments([Path(f) for f in seg_files], workdir=work_fb,
                                    warnings=warnings)
            out_dir = project_dir / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            final_path = out_dir / (str(输出文件名) if str(输出文件名).lower().endswith(".mp4")
                                    else str(输出文件名) + ".mp4")
            import shutil

            shutil.copy2(fb, final_path)
            finalize_report = {"executed": False, "error": f"{type(exc).__name__}: {exc}",
                               "fallback": "splice_segments+copy"}
        meta = _parse_media(final_path)
        vram_after_encode = _vram_peak()

        # ---- 7) 段级 R_seg + 整片 R_full 审核（tuple 解析已修复）----
        # 完整性：导演台会按 H3 帧网格取整（如 5.167s → 141 帧），不能用计划秒数硬比；
        # 用真实生成帧数作为校验基准，同时把计划/实际差异写进报告与审计（不静默）。
        plan_seconds = len(prompts) * float(每段时长_秒)
        generated_seconds = round(float(meta.get("frames") or 0) / max(1e-6, float(帧率)), 3)
        if abs(generated_seconds - plan_seconds) > 0.1:
            audit.append(
                f"时长说明：计划 {plan_seconds:.2f}s，导演台按帧网格实际生成 {generated_seconds:.2f}s"
                f"（{meta.get('frames')} 帧）——完整性按实际帧数校验")
        expected_segments = max(1, int(seg_count))
        if timeline_source == "user_json":
            expected_segments = max(1, int(tl_summary.get("segmentCount")
                                           or len(seg_files) or 1))
        actual_segments = len(seg_files) if seg_files else 1
        integrity, quality = self._review(
            final_path, meta, expected_s=generated_seconds,
            threshold=int(审核阈值), audit=audit,
            continuity_enhanced=bool(continuity_enabled),
            expected_segments=expected_segments,
            actual_segments=actual_segments,
            plan_seconds=plan_seconds,
        )
        video_review = self._video_review(
            final_path, 项目, float(每段时长_秒), audit,
            审核范围=_VIDEO_REVIEW_SCOPES[0],
            段阈值覆盖=float(段阈值覆盖), 整片阈值覆盖=float(整片阈值覆盖),
            整片失败策略=str(整片失败策略), 边界相似度阈值=float(边界相似度阈值),
            project_id=project_id,
            本次是级联重做=bool(rework.get("cnt3") and any(rework["cnt3"])),
        )
        # ROUND49：返工/CNT2/CNT3/兜底 结构化给 UI（与整片审核同一对象）
        video_review["rework_rounds"] = list(rework.get("rounds") or [])
        video_review["cnt2"] = list(rework.get("cnt2") or [])
        video_review["cnt3"] = list(rework.get("cnt3") or [])
        video_review["fallback"] = list(rework.get("fallback") or [])
        video_review["fallback_pass"] = list(rework.get("fallback_pass") or [])
        vram_peak_all = _vram_peak()
        # Q_OUT：最终 R_seg 分数回填分段清单（segment_id/版本/素材/段文件/分数/CNT2/CNT3）
        seg_reviews = []
        for row in (video_review.get("segment_scores") or []):
            seg_reviews.append({"score": row.get("score"), "scores": row.get("scores"),
                                "passed": row.get("passed"),
                                "version": row.get("version") or "v1"})
        _manifest_specs2 = seg_specs or [
            {"prompt": prompts[i % len(prompts)] if prompts else "",
             "duration_sec": float(每段时长_秒), "task_type": task_type}
            for i in range(len(seg_files))]
        seg_manifest = lf.build_segment_manifest(
            project_id=project_id, segment_specs=_manifest_specs2, seg_files=seg_files,
            segment_reviews=seg_reviews, cnt2=rework.get("cnt2"),
            cnt3=rework.get("cnt3"), fallback_details=rework.get("fallback"))
        manifest_path = project_dir / "review" / f"segment_manifest_{int(time.time())}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(seg_manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        audit.append(f"分段清单（Q_OUT）：{manifest_path}")
        # ROUND49：时间轴摘要补「每段时长/连续性开关+overlap+pin 段」供 UI 直接渲染
        tl_summary["segment_seconds"] = float(每段时长_秒)
        tl_summary["task_type"] = task_type
        tl_summary["continuity"] = dict(cont_ev)
        tl_summary["continuity_config"] = {
            "enabled": bool(段间连续),
            "effective": bool(continuity_enabled),
            "overlap_frames_requested": int(连续帧数),
            "overlap_frames_snapped": int(overlap),
            "mode": cont_mode,
            "pinned_segments": list(cont_ev.get("pinned_segments") or []),
            "hard_cut_segments": list(cont_ev.get("hard_cut_segments") or []),
        }

        # ---- 8) 报告落盘 + ComfyUI 预览导出 ----
        duration_expected = len(prompts) * float(每段时长_秒)
        report_obj = {
            "node": "BetterComfyUIOneStop",
            "node_version": "v0.5-review-data-context",
            "project_id": project_id,
            "project_context": project_context,
            "task_type": task_type,
            "task_key": task_key,
            "prompts": {
                "mode": (payload.get("meta") or {}).get("mode"),
                "api_calls": (payload.get("meta") or {}).get("api_calls"),
                "api_call_limit": 1,
                "assets_by_type": asset_type_counts,
                "assets_total": sum(asset_type_counts.values()),
                "segments": prompts,
            },
            "timeline_source": timeline_source,
            "timeline": timeline_json,
            "timeline_summary": tl_summary,
            "continuity": cont_ev,
            "continuity_config": {
                "enabled": bool(段间连续),
                "effective": bool(continuity_enabled),
                "overlap_frames_requested": int(连续帧数),
                "overlap_frames_snapped": int(overlap),
                "mode": cont_mode,
                "redraw": float(重绘幅度),
                "keep_tail": bool(保完整尾部),
                "per_segment_from_prev": bool(每段引用上段),
                "audio_context_frames": int(音频上下文帧数),
                "audio_context_injection": audio_ctx,
            },
            "optional_packs": pack_summary,
            "assets": asset_report,
            "finalize": finalize_report,
            "video_meta": meta,
            "integrity": integrity,
            "quality": quality,
            "video_review": video_review,
            # ROUND49：UI 汇总卡（素材审核分数/状态 + 视频分段审核 + 返工 + 结论）
            "review_summary": {
                "assets": asset_report.get("review_summary") or {},
                "video": {
                    "verdict": video_review.get("verdict"),
                    "segments_total": len(video_review.get("segment_scores") or []),
                    "segments_passed": sum(
                        1 for r in (video_review.get("segment_scores") or [])
                        if r.get("passed")),
                    "need_regenerate": video_review.get("need_regenerate_segments") or [],
                    "full": video_review.get("full") or {},
                },
                "rework": {
                    "enabled": bool(自动返工),
                    "rounds": video_review.get("rework_rounds") or [],
                    "cnt2": video_review.get("cnt2") or [],
                    "cnt3": video_review.get("cnt3") or [],
                    "fallback": video_review.get("fallback") or [],
                    "fallback_pass": video_review.get("fallback_pass") or [],
                },
                "integrity": {"passed": integrity.get("passed")},
                "quality": {"average": quality.get("average"),
                            "threshold": quality.get("threshold"),
                            "passed": quality.get("passed")},
            },
            "timings": timings,
            "vram": {
                "after_assets": vram_after_assets,
                "after_load": vram_after_load,
                "after_generate": vram_after_generate,
                "after_encode": vram_after_encode,
                "peak": vram_peak_all,
            },
            "generation": {"seconds": timings.get("出片_s"),
                           "frames": int(sum(frame_counts)) if frame_counts
                           else int(meta.get("frames") or 0),
                           "expected_duration_s": round(duration_expected, 3)},
            "unimplemented_features": unimplemented,
            "longform": {
                "segment_plan": seg_plan,
                "batch_plan": gen.get("batch_plan"),
                "batch_rows": batch_rows,
                "checkpoint": gen.get("checkpoint"),
                "resume_enabled": bool(断点续跑),
                "batch_size": int(每批段数),
                "vram_tier": str(显存档位),
                "pruned_model": bool(剪枝模型),
                "tiled_sampling": int(tiled采样),
            },
            "auto_rework": rework,
            "edit": {k: v for k, v in finalize_report.items() if k != "audit"},
            "output": (finalize_report.get("output") if isinstance(finalize_report, dict) else None),
            "segment_manifest": seg_manifest,
            "segment_manifest_path": str(manifest_path),
            "warnings": warnings,
            "raw_director_report": str(report)[:20000],
            "audit": "\n".join(audit),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        review_dir = project_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        review_path = review_dir / f"one_stop_{ts}.json"
        review_path.write_text(json.dumps(report_obj, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        timeline_path = review_dir / f"timeline_{ts}.json"
        timeline_path.write_text(timeline_json, encoding="utf-8")
        (project_dir / "output").mkdir(parents=True, exist_ok=True)
        (project_dir / "output" / "提示词.json").write_text(prompt_json, encoding="utf-8")
        vram_peak = vram_peak_all.get("peak_reserved_mb")

        comfy_export = self._export_to_comfyui(final_path)
        if comfy_export:
            audit.append(f"ComfyUI 预览：{comfy_export['subfolder']}/{comfy_export['filename']}")
        self._free_models()

        verdict = "通过" if integrity.get("passed") and quality.get("passed") else "不通过"
        audit.append(
            f"审核结论：{verdict}（完整性={integrity.get('passed')}，"
            f"质量均分={quality.get('average')}，阈值={审核阈值}；"
            f"视频审核={video_review.get('verdict')}）"
        )
        audit.append(
            f"段级 R_seg：{str(video_review.get('segment_scores'))[:400]}"
        )
        audit.append(f"整片 R_full：{json.dumps(video_review.get('full'), ensure_ascii=False)[:600]}")
        for line in warnings:
            audit.append(line)
        _assets_sum = asset_report.get("review_summary") or {}
        audit.append(
            "素材审核汇总：生成 {} / LOCK {}（FALLBACK {}）/ 平均分 {} / 阈值 {} / 状态 {}".format(
                _assets_sum.get("generated", 0), _assets_sum.get("locked", 0),
                _assets_sum.get("fallback", 0), _assets_sum.get("avg_score"),
                _assets_sum.get("threshold"),
                "；".join(f"{x.get('name')}={x.get('state')}({x.get('score')})"
                          for x in (_assets_sum.get("states") or [])) or "无"))
        _seg_rows = video_review.get("segment_scores") or []
        audit.append(
            "段级审核汇总：{} 段，通过 {}，需重生成 {}；整片 R_full={}（策略={}）".format(
                len(_seg_rows),
                sum(1 for r in _seg_rows if r.get("passed")),
                video_review.get("need_regenerate_segments") or "无",
                (video_review.get("full") or {}).get("passed"),
                (video_review.get("full") or {}).get("policy")))
        audit.append(f"证据：{review_path}")
        audit.append(f"时间轴：{timeline_path}")
        audit.append(f"显存峰值（reserved）：{vram_peak} MB")
        audit.append(f"总耗时：{round(time.time() - started, 2)}s | 分项：{timings}")
        ui = {"text": ["\n".join(audit)]}
        # 前端渲染契约（ui-agent）：完整审核报告 JSON；键缺失时前端显示「待运行」
        ui["dramacore_report"] = report_obj
        if comfy_export:
            _suffix = Path(str(final_path)).suffix.lower()
            _mime = {".mov": "video/quicktime", ".webm": "video/webm"}.get(
                _suffix, "video/h264-mp4")
            ui["images"] = [comfy_export]
            ui["animated"] = [True]
            ui["gifs"] = [dict(comfy_export, format=_mime)]
        return {"ui": ui,
                "result": (str(final_path),
                           json.dumps(report_obj, ensure_ascii=False, indent=2),
                           prompt_json, "\n".join(audit), timeline_json, str(report))}


    # ==================================================================
    # 长片分批执行（V4/Q_OUT）：自动分段 → 多批生成 → 断点续跑 → 逐段 mp4
    # ==================================================================
    def _load_video_models(self, 视频UNET, CLIP名, 视频VAE, 音频VAE, 视频LoRA, LoRA强度):
        from nodes import CLIPLoader, UNETLoader, VAELoader

        model_obj = _loader_call(UNETLoader, "load_unet", "unet_name", 视频UNET)
        clip_obj = _loader_call(CLIPLoader, "load_clip", "clip_name", CLIP名,
                                overrides={"type": "minimax"})
        video_vae_obj = _loader_call(VAELoader, "load_vae", "vae_name", 视频VAE)
        audio_vae_obj = _loader_call(VAELoader, "load_vae", "vae_name", 音频VAE)
        if 视频LoRA:
            from nodes import LoraLoaderModelOnly

            model_obj = _loader_call(
                LoraLoaderModelOnly, "load_lora_model_only", "lora_name", 视频LoRA,
                inputs={"model": model_obj},
                overrides={"strength_model": float(LoRA强度)})
        return model_obj, clip_obj, video_vae_obj, audio_vae_obj

    def _execute_timeline(self, ctx, timeline_json, *, unique_id, seed_value):
        """执行一次导演台（单批或单段返工共用；参数全部来自 ctx）。"""
        from . import director_runtime as dr
        from .vendor.h3_director.nodes.director import MiniMaxH3Director

        model_obj, clip_obj, video_vae_obj, audio_vae_obj = ctx["models"]
        tsum = dr.timeline_summary(timeline_json)
        total_frames = int(tsum.get("totalFrames") or 0)
        return MiniMaxH3Director().execute(
            model=model_obj, video_vae=video_vae_obj, audio_vae=audio_vae_obj,
            clip=clip_obj, task_type=ctx["task_type"], global_prompt=ctx["global_prompt"],
            frame_rate=float(ctx["帧率"]), width=int(ctx["宽度"]), height=int(ctx["高度"]),
            ref_max_size=int(ctx["参考最长边"]), total_frames=total_frames,
            timeline_data=timeline_json, unique_id=unique_id,
            i2v_groups=None, r2v_groups=None,
            semantic_bridge=ctx["packs"]["semantic_bridge"],
            selflift=ctx["packs"]["selflift"], refine=ctx["packs"]["refine"],
            face_refine=ctx["packs"]["face_refine"], sigmas=None,
            steps=int(ctx["步数"]), sampler=str(ctx["采样器"]),
            scheduler=str(ctx["调度器"]), cfg=float(ctx["cfg"]), seed=int(seed_value),
            shift_video=float(ctx["噪声偏移_视频"]), shift_audio=float(ctx["噪声偏移_音频"]),
            clear_vram_between_segments=bool(ctx["显存清理_段间"]),
            clear_vram_before_refine=bool(ctx["显存清理_二采前"]),
            clear_vram_before_face_refine=bool(ctx["显存清理_修脸前"]),
            cache_frames_codec=str(ctx["缓存编码"]),
            export_source_images=bool(ctx["导出原始帧"]),
            export_pre_face_refine=bool(ctx["输出修脸前"]),
            cache_name=(str(ctx["缓存目录名"] or "") or f"onestop_{int(time.time() * 1000)}"),
        )

    def _generate_batches(self, ctx):
        from . import director_longform as lf

        audit, warnings = ctx["audit"], ctx["warnings"]
        specs = ctx["seg_specs"]
        total = len(specs)
        fps = float(ctx["帧率"])
        user_tl = str(ctx.get("时间轴JSON") or "").strip()
        results = {"seg_files": [], "batch_rows": [], "reports": [],
                   "frame_counts": [], "timings": {}, "checkpoint": ""}
        if user_tl:
            plan = [(0, total)]
        else:
            size = max(1, int(ctx["每批段数"]))
            tier = str(ctx.get("显存档位") or "自动")
            if tier.startswith("低"):
                size = min(size, 4)
            elif tier.startswith("高"):
                size = max(size, 16)
            plan = lf.plan_batches(total, size)
        fingerprint = lf.parameter_fingerprint({
            "prompts": [s.get("prompt") for s in specs],
            "frames": [s.get("frames") for s in specs],
            "width": ctx["宽度"], "height": ctx["高度"], "fps": fps,
            "seed": ctx["seed"], "steps": ctx["步数"], "cfg": ctx["cfg"],
            "unet": ctx["视频UNET"], "clip": ctx["CLIP名"],
            "vae": ctx["视频VAE"], "avae": ctx["音频VAE"], "lora": ctx["视频LoRA"],
            "continuity": [ctx["段间连续"], ctx["连续帧数"], ctx["连续模式"]],
            "batch_size": ctx["每批段数"], "task": ctx["task_type"],
        })
        state = {"batches": {}}
        if ctx["断点续跑"]:
            ck = lf.load_checkpoint(ctx["project_dir"], fingerprint)
            state["batches"] = dict(ck.get("batches") or {})
            audit.append(f"断点续跑：{ck.get('note')}")
        else:
            audit.append("断点续跑=False：本次全量重跑")
        seg_files: list[str | None] = [None] * total
        audit.append("长片分批：{}（共 {} 批，每批≤{} 段）".format(
            "；".join(f"批次{i + 1}=段{a + 1}-{b}" for i, (a, b) in enumerate(plan)),
            len(plan), max(b - a for a, b in plan)))
        ctx["batch_rows"] = []
        for bi, (start, end) in enumerate(plan):
            reuse = (lf.reuse_batch_files(state["batches"].get(str(bi)) or {})
                     if ctx["断点续跑"] else None)
            if reuse:
                seg_files[start:end] = reuse
                audit.append(f"批次{bi + 1}/{len(plan)}：检查点命中，跳过段{start + 1}-{end}")
                ctx["batch_rows"].append({"batch_index": bi, "segments": [start + 1, end],
                                          "status": "reused", "reused": True,
                                          "seg_files": reuse})
                continue
            t0 = time.time()
            if ctx.get("models") is None:
                ctx["models"] = self._load_video_models(
                    ctx["视频UNET"], ctx["CLIP名"], ctx["视频VAE"], ctx["音频VAE"],
                    ctx["视频LoRA"], ctx["LoRA强度"])
            batch = [dict(s) for s in specs[start:end]]
            for j, s in enumerate(batch):
                gi = start + j
                s["continuityFromPrev"] = bool(
                    ctx["每段引用上段"] and gi > 0 and j > 0 and not user_tl)
                s.setdefault("frames", int(round(float(ctx["每段时长_秒"]) * fps)))
                if j == 0 and gi > 0:
                    s["continuityFromPrev"] = False
                    tail = ctx.get("tail_ref")
                    if ctx["task_key"] == "r2v" and tail:
                        from . import director_runtime as dr

                        refs = list(s.get("refs") or [])
                        refs.append(dr.image_ref(tail, index=len(refs)))
                        s["refs"] = refs
                        warnings.append(
                            f"⚠️ 跨批衔接：批次{bi + 1}首段用上一批尾帧作额外参考图"
                            "（近似，非 vendor 原生 22 帧 pin；跨批连续如实标注）")
            if user_tl:
                # 用户完整时间轴覆盖：单次执行，段数取时间轴 segmentCount（不做分批）
                from . import director_runtime as dr

                tl = user_tl
                _tlsegs = max(1, int(dr.timeline_summary(user_tl).get("segmentCount") or 1))
                if len(seg_files) != _tlsegs:
                    seg_files = [None] * _tlsegs
                    total = _tlsegs
                end = total
                n_exp = _tlsegs
            else:
                from . import director_runtime as dr

                tl = dr.build_timeline_json(
                    task_type=ctx["task_type"], segments=batch,
                    global_prompt=ctx["global_prompt"],
                    width=int(ctx["宽度"]), height=int(ctx["高度"]), frame_rate=fps,
                    ref_max_size=int(ctx["参考最长边"]), output=ctx["out_cfg"],
                    negative_prompt=ctx["negative"])
                n_exp = end - start
            imgs, aud, _ofps, fc, _src, rep, _pre, _pf = self._execute_timeline(
                ctx, tl, unique_id=f"bcu-onestop-b{bi}",
                seed_value=int(ctx["seed"]) + bi * 100003)
            ctx["reports"].append(str(rep))
            results["frame_counts"].append(int(fc or 0))
            segs, auds = self._split_segments(imgs, aud)
            if len(segs) == 1 and n_exp > 1:
                combined = segs[0]
                tot = int(getattr(combined, "shape", [0])[0] or 0)
                if tot >= n_exp * 5:
                    per = tot // n_exp
                    segs = [combined[i * per:(i + 1) * per] for i in range(n_exp)]
            if not segs:
                raise RuntimeError(f"批次{bi + 1} 导演台没有返回画面张量")
            for k in range(min(n_exp, len(segs))):
                item_audio = auds[k] if k < len(auds) else _normalize_audio(aud)
                out = ctx["seg_dir"] / f"seg_{start + k + 1:03d}.mp4"
                _encode_movie(segs[k], item_audio, out, fps)
                seg_files[start + k] = str(out)
            if end < total and ctx["task_key"] == "r2v":
                try:
                    ctx["tail_ref"] = str(lf.extract_last_frame(
                        Path(seg_files[end - 1]), ctx["seg_dir"] / f"tail_b{bi + 1}.png"))
                except Exception as exc:  # noqa: BLE001 - 跨批尾帧失败要可见
                    warnings.append(f"⚠️ 跨批尾帧提取失败（下一批少一张近似衔接参考图）：{exc}")
            state["batches"][str(bi)] = {
                "status": "done", "reused": False,
                "seg_files": [str(seg_files[i]) for i in range(start, end)],
                "elapsed_s": round(time.time() - t0, 2),
                "report_first_line": str(rep).splitlines()[0] if rep else "",
            }
            if ctx["断点续跑"]:
                lf.save_checkpoint(ctx["project_dir"], fingerprint, state)
            ctx["batch_rows"].append(dict(state["batches"][str(bi)],
                                          batch_index=bi, segments=[start + 1, end]))
            audit.append("批次{}/{}：段{}-{} 完成（{}s）{}".format(
                bi + 1, len(plan), start + 1, end,
                state["batches"][str(bi)]["elapsed_s"],
                "" if (state["batches"][str(bi)]["report_first_line"] == "") else
                " | " + state["batches"][str(bi)]["report_first_line"]))
            if end < total and (ctx["批次间卸载模型"] or ctx["剪枝模型"]
                                or str(ctx.get("显存档位") or "").startswith("低")):
                self._free_models()
                ctx["models"] = None
                audit.append(f"批次{bi + 1}结束：已卸载模型/清 CUDA 缓存（显存档位={ctx.get('显存档位')}，剪枝模型={ctx['剪枝模型']}）")
        missing = [i + 1 for i, f in enumerate(seg_files) if not f or not Path(f).is_file()]
        if missing:
            raise RuntimeError(
                f"长片生成未完成：段 {missing} 没有产出文件（检查点已保存，可用同参数重跑续跑）")
        results["seg_files"] = [str(f) for f in seg_files]
        results["batch_rows"] = ctx["batch_rows"]
        results["reports"] = list(ctx["reports"])
        results["batch_plan"] = ([[1, len(seg_files)]] if user_tl
                                 else [[a + 1, b] for a, b in plan])
        results["checkpoint"] = (str(lf.checkpoint_path(ctx["project_dir"]))
                                 if ctx["断点续跑"] else "")
        return results

    def _regen_one_segment(self, *, ctx, index, attempt, old_file, work, cascade=False):
        from . import director_runtime as dr

        spec = dict(ctx["seg_specs"][index])
        orig = str(spec.get("prompt") or "")
        hint = ("保持角色身份/服装一致，动作自然连贯，避免跳变、闪烁与重复帧"
                if not cascade else "衔接上一段的动作/镜头/光线，保持角色与画风一致，避免跳变")
        spec["prompt"] = orig if "返工要求" in orig else f"{orig}\n返工要求：{hint}"
        spec["continuityFromPrev"] = bool(not cascade and index > 0)
        if cascade and index > 0 and ctx["task_key"] == "r2v" and Path(old_file).is_file():
            pass  # 级联段用自身时间轴重建（上一段结果在成片里，不再 pin）
        tl = dr.build_timeline_json(
            task_type=ctx["task_type"], segments=[spec],
            global_prompt=str(spec.get("prompt") or ""),
            width=int(ctx["宽度"]), height=int(ctx["高度"]), frame_rate=float(ctx["帧率"]),
            ref_max_size=int(ctx["参考最长边"]), output=ctx["out_cfg"],
            negative_prompt=ctx["negative"])
        imgs, aud, _fps, _fc, _src, rep, _pre, _pf = self._execute_timeline(
            ctx, tl, unique_id=f"bcu-onestop-rw-{index}",
            seed_value=int(ctx["seed"]) + index * 997 + attempt * 131)
        segs, _auds = self._split_segments(imgs, aud)
        if not segs:
            raise RuntimeError(f"返工段{index + 1} 导演台没有返回画面")
        out = work / f"{'cascade' if cascade else 'rework'}_seg{index + 1}_a{attempt}.mp4"
        _encode_movie(segs[0], _normalize_audio(aud), out, float(ctx["帧率"]))
        return out

    def _auto_rework(self, *, ctx, seg_files, project_id, project_dir, audit, warnings):
        """SG5 U/REGEN/CNT2 + N1/CNT3 级联：段不过 → 自动重生成该段 → 重审。"""
        t0 = time.time()
        from . import director_edit as de

        total = len(seg_files)
        if not (ctx.get("seg_specs") or []):
            return {"enabled": bool(ctx["自动返工"]), "cnt2": [0] * total,
                    "cnt3": [0] * total, "rounds": [], "fallback": [],
                    "regen_files": {}, "cascade": {"enabled": False, "regen": []},
                    "policy": {}, "seg_files": list(seg_files),
                    "note": "时间轴JSON 覆盖模式不分段规格：自动返工需按时段规格重建时间轴，"
                            "该模式不自动返工（只输出 R_seg 需重生成清单，如实记录）"}
        result = {"enabled": bool(ctx["自动返工"]),
                  "cnt2": [0] * total, "cnt3": [0] * total,
                  "rounds": [], "fallback": [], "regen_files": {},
                  "cascade": {"enabled": bool(ctx["级联返工"]), "regen": []},
                  "policy": {"cnt2_limit": int(ctx["CNT2上限"]),
                             "cnt3_limit": int(ctx["CNT3上限"]),
                             "max_cascade_depth": 1},
                  "seg_files": list(seg_files)}
        if not ctx["自动返工"]:
            result["note"] = "自动返工=关闭：只输出需重生成清单（CNT2/CNT3 不触发）"
            return result
        work = Path(project_dir) / "_work" / f"rework_{int(time.time())}"
        work.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, int(ctx["CNT2上限"]) + 1):
            preview = de.splice_segments(
                [Path(f) for f in seg_files], workdir=work / f"preview_a{attempt}",
                warnings=warnings)
            rev = self._video_review(
                preview, ctx["项目"], float(ctx["每段时长_秒"]), audit,
                审核范围="仅段级 R_seg", project_id=project_id,
                本次是级联重做=False,
                段阈值覆盖=float(ctx["段阈值覆盖"]),
                边界相似度阈值=float(ctx["边界相似度阈值"]))
            need_text = str(rev.get("need_regenerate") or "无")
            need = ([int(x) for x in re.findall(r"\d+", need_text)]
                    if need_text and need_text not in ("无", "None") else [])
            result["rounds"].append({"attempt": attempt, "need": need,
                                     "verdict": rev.get("verdict")})
            audit.append(f"自动返工第{attempt}轮：R_seg 结论={rev.get('verdict')} "
                         f"需重生成段={need or '无'}")
            if not need:
                break
            if ctx.get("models") is None:
                ctx["models"] = self._load_video_models(
                    ctx["视频UNET"], ctx["CLIP名"], ctx["视频VAE"], ctx["音频VAE"],
                    ctx["视频LoRA"], ctx["LoRA强度"])
            reworked: list[int] = []
            for i1 in need:
                i = i1 - 1
                if i < 0 or i >= total:
                    continue
                if result["cnt2"][i] >= int(ctx["CNT2上限"]):
                    continue
                try:
                    new_file = self._regen_one_segment(
                        ctx=ctx, index=i, attempt=attempt, old_file=seg_files[i],
                        work=work)
                except Exception as exc:  # noqa: BLE001 - 返工失败要如实记录
                    warnings.append(f"⚠️ 段{i + 1} 自动返工失败（保留原段）："
                                    f"{type(exc).__name__}: {exc}")
                    continue
                result["cnt2"][i] += 1
                reworked.append(i)
                seg_rev = self._video_review(
                    Path(new_file), ctx["项目"], float(ctx["每段时长_秒"]), audit,
                    审核范围="仅段级 R_seg", project_id=project_id,
                    段阈值覆盖=float(ctx["段阈值覆盖"]),
                    边界相似度阈值=float(ctx["边界相似度阈值"]))
                score = None
                rows = seg_rev.get("segment_scores") or []
                if rows:
                    score = rows[0].get("score")
                result["regen_files"].setdefault(str(i), []).append(
                    {"file": str(new_file), "score": score, "attempt": attempt})
                best = max(result["regen_files"][str(i)],
                           key=lambda c: (float(c.get("score") or -1), -int(c.get("attempt") or 0)))
                seg_files[i] = best["file"]
                audit.append(f"返工 CNT2 段{i + 1} 第{result['cnt2'][i]}轮："
                             f"score={score} → 采用最高分候选 {best['file']}")
            # N1/CNT3：返工段的后一段级联重新生成（边界被改变）
            if ctx["级联返工"] and reworked:
                for i in sorted(reworked):
                    j = i + 1
                    if j >= total or result["cnt3"][j] >= int(ctx["CNT3上限"]):
                        continue
                    try:
                        new_next = self._regen_one_segment(
                            ctx=ctx, index=j, attempt=attempt + 10,
                            old_file=seg_files[j], work=work, cascade=True)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"⚠️ 段{j + 1} 级联返工失败（保留原段）："
                                        f"{type(exc).__name__}: {exc}")
                        continue
                    result["cnt3"][j] += 1
                    seg_files[j] = str(new_next)
                    result["cascade"]["regen"].append(
                        {"segment_index": j + 1, "upstream": i + 1,
                         "file": str(new_next), "cnt3": result["cnt3"][j]})
                    audit.append(f"级联返工 CNT3 段{j + 1}（上游段{i + 1}变化）→ {new_next}")
            self._free_models()
            ctx["models"] = None
        # 返工上限用完后做一次最终复核（只审不返工）：区分「已修好」与「仍不过→兜底」
        try:
            final_preview = de.splice_segments(
                [Path(f) for f in seg_files],
                workdir=work / "final_verify", warnings=warnings)
            final_rev = self._video_review(
                final_preview, ctx["项目"], float(ctx["每段时长_秒"]), audit,
                审核范围="仅段级 R_seg", project_id=project_id,
                本次是级联重做=bool(any(result["cnt3"])),
                段阈值覆盖=float(ctx["段阈值覆盖"]),
                边界相似度阈值=float(ctx["边界相似度阈值"]))
            final_need_text = str(final_rev.get("need_regenerate") or "无")
            final_need = ([int(x) for x in re.findall(r"\d+", final_need_text)]
                          if final_need_text and final_need_text not in ("无", "None") else [])
            result["rounds"].append({"attempt": len(result["rounds"]) + 1,
                                     "need": final_need,
                                     "verdict": final_rev.get("verdict"),
                                     "final_verify": True})
        except Exception as exc:  # noqa: BLE001 - 复核失败如实记录，按最后一轮 need 判定
            warnings.append(f"⚠️ 返工后最终复核失败（按上一轮 need 判定兜底）："
                            f"{type(exc).__name__}: {exc}")
            final_need = list(result["rounds"][-1].get("need") or []) if result["rounds"] else []
        last_need = set(final_need)
        result["fallback"] = [
            {"segment_index": i + 1, "cnt2": result["cnt2"][i], "file": seg_files[i],
             "note": "达 CNT2 上限仍被 R_seg 标记，取候选最高分（REVIEW_FALLBACK，不冒充通过）"}
            for i in range(total)
            if result["cnt2"][i] >= int(ctx["CNT2上限"]) and (i + 1) in last_need]
        result["fallback_pass"] = [
            {"segment_index": i + 1, "cnt2": result["cnt2"][i],
             "note": "返工后已不再被 R_seg 标记（按最高分候选采用）"}
            for i in range(total)
            if result["cnt2"][i] > 0 and (i + 1) not in last_need]
        result["seg_files"] = list(seg_files)
        result["elapsed_s"] = round(time.time() - t0, 2)
        return result

    # ==================================================================
    def _split_segments(self, images_out: Any, audio_out: Any) -> tuple[list[Any], list[Any]]:
        """把导演台输出拆成「每段一个 IMAGE 张量 + 每段 audio」。"""
        segs: list[Any] = []
        if isinstance(images_out, (list, tuple)):
            for item in images_out:
                if item is None:
                    continue
                if hasattr(item, "dim") and item.dim() == 4:
                    segs.append(item)
                elif isinstance(item, (list, tuple)):
                    for sub in item:
                        if sub is not None:
                            segs.append(sub)
        elif hasattr(images_out, "dim"):
            segs = [images_out]
        audios: list[Any] = []
        if isinstance(audio_out, (list, tuple)):
            for item in audio_out:
                if isinstance(item, dict) and item.get("waveform") is not None:
                    audios.append(item)
        if not segs:
            raise RuntimeError("导演台没有返回任何画面张量：无法编码成片。")
        return segs, audios

    # ==================================================================
    def _reuse_locked_assets(self, *, payload: dict, project_id: str, limit: int,
                             audit: list[str], warnings: list[str]) -> dict:
        """生成素材图=False 时：从项目素材库复用已 LOCKED 素材作为参考图（续写/防漂移）。

        不生成、不审核、不改库状态，只读取；找不到的素材跳过并在审计说明。
        """
        from bettercomfyui.store.assets import AssetStore
        from bettercomfyui.store.paths import ProjectPaths

        report: dict[str, Any] = {
            "enabled": False, "created": [], "locked": [], "blocked": [], "reused": [],
            "refs_attached": 0, "errors": [], "rounds": [], "_tensors": [], "_paths": [],
            "reuse_only": True, "review_summary": {}, "assets": [],
        }
        try:
            root = self._project_root()
            paths = ProjectPaths(root=root, project_id=project_id)
            paths.ensure_layout()
            store = AssetStore(paths)
        except Exception as exc:  # noqa: BLE001 - 读库失败要可见，不静默
            warnings.append(f"复用素材库失败（不挂参考图继续出片）：{type(exc).__name__}: {exc}")
            return report

        candidates: list[dict] = []
        for asset_type, items in (payload.get("assets_by_type") or {}).items():
            if asset_type in ("OTHER", "SHOT"):
                continue
            for item in items or []:
                candidates.append({"asset_type": asset_type, "name": item.get("name") or "未命名"})
        scores: list[float] = []
        for cand in candidates[:max(0, int(limit))]:
            try:
                found = store.find_by_normalized_name(cand["name"]) if cand["name"] else []
                if not found:
                    continue
                aid = str(found[0]["asset_id"])
                locked_ver = store.latest_locked_version(aid)
                if not locked_ver:
                    continue
                ver_row = store.get_version(aid, locked_ver) or {}
                f = ver_row.get("file_path")
                if not f:
                    continue
                abs_path = Path(str(f))
                if not abs_path.is_absolute():
                    abs_path = paths.absolute_from_relative(str(f))
                if not abs_path.is_file():
                    continue
                report["_tensors"].append(_image_tensor(abs_path))
                report["_paths"].append(str(abs_path))
                score = ver_row.get("score")
                if score is None:
                    score = ver_row.get("avg_score")
                try:
                    if score is not None:
                        scores.append(float(score))
                except (TypeError, ValueError):
                    score = None
                row = {"asset_id": aid, "type": cand["asset_type"], "name": cand["name"],
                       "version": locked_ver, "png": str(abs_path), "state": "LOCKED",
                       "lock_status": "LOCKED", "score": score, "reused": True}
                report["locked"].append(row)
                report["reused"].append(row)
                report["assets"].append(row)
                audit.append(f"  素材 {cand['name']} {locked_ver} 已 LOCKED → 复用（项目库，不重新生成）")
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"{type(exc).__name__}: {exc}")
                warnings.append(f"复用素材 {cand.get('name')} 失败（跳过）：{type(exc).__name__}: {exc}")
        # 名称匹配不到时：用项目库其它 LOCKED 素材补足参考图
        # （换章节后 E1–E5 名字可能变化；用户选旧项目的目的就是复用素材库防人物漂移）
        limit_n = max(0, int(limit))
        if len(report["_paths"]) < limit_n:
            try:
                used = {r.get("asset_id") for r in report["locked"]}
                for asset in store.list_assets():
                    if len(report["_paths"]) >= limit_n:
                        break
                    aid = str(asset.get("asset_id") or "")
                    if not aid or aid in used:
                        continue
                    locked_ver = store.latest_locked_version(aid)
                    if not locked_ver:
                        continue
                    ver_row = store.get_version(aid, locked_ver) or {}
                    f = ver_row.get("file_path")
                    if not f:
                        continue
                    abs_path = Path(str(f))
                    if not abs_path.is_absolute():
                        abs_path = paths.absolute_from_relative(str(f))
                    if not abs_path.is_file():
                        continue
                    score = ver_row.get("score")
                    if score is None:
                        score = ver_row.get("avg_score")
                    try:
                        if score is not None:
                            scores.append(float(score))
                    except (TypeError, ValueError):
                        score = None
                    row = {"asset_id": aid, "type": str(asset.get("asset_type") or ""),
                           "name": str(asset.get("name") or "未命名"),
                           "version": locked_ver, "png": str(abs_path), "state": "LOCKED",
                           "lock_status": "LOCKED", "score": score, "reused": True,
                           "fallback_pick": True}
                    report["_tensors"].append(_image_tensor(abs_path))
                    report["_paths"].append(str(abs_path))
                    report["locked"].append(row)
                    report["reused"].append(row)
                    report["assets"].append(row)
                    audit.append(
                        f"  素材 {row['name']} {locked_ver} 已 LOCKED → 复用（名称未匹配，项目库补足参考图）")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"项目库补足参考图失败（跳过）：{type(exc).__name__}: {exc}")
        avg = round(sum(scores) / len(scores), 2) if scores else None
        report["review_summary"] = {
            "generated": 0, "locked": len(report["locked"]), "blocked": 0,
            "fallback": 0, "avg_score": avg, "threshold": None,
            "states": [{"name": r["name"], "state": "LOCKED", "score": r["score"]}
                       for r in report["locked"]],
        }
        return report

    def _generate_and_review_assets(
        self, *, payload: dict, project_id: str, project_dir: Path, ckpt: str,
        limit: int, rounds: int, review_mode: str, review_threshold: int = -1,
        reuse_locked: bool = True,
        resolution: int, steps: int, cfg: float, seed: int,
        audit: list[str], warnings: list[str],
    ) -> dict:
        """素材生图 → 入库（版本+指纹）→ 素材审核（CNT1/LOCK/兜底）→ 返回已 LOCK 图张量。

        审核口径（vendor/本项目状态机，不伪造）：
        - 模型审核（本地启发式）单轮不及格 → BLOCKED（可重试）；同一素材最多 rounds 个候选版本；
        - 达到 CNT1 上限（配置 model_candidate_limit=3）→ 按规范从候选里选最高分兜底 FALLBACK，
          并在被选中版本上 LOCK（这就是「素材审核→LOCK→挂参考图」的自动化路径）；
        - 真实分数/状态全部写进审计与审核 JSON，不把 FALLBACK 伪装成 ALLOWED。
        """
        from nodes import CheckpointLoaderSimple

        from bettercomfyui.store.assets import AssetStore
        from bettercomfyui.store.paths import ProjectPaths

        root = self._project_root()
        paths = ProjectPaths(root=root, project_id=project_id)
        paths.ensure_layout()
        store = AssetStore(paths)

        candidates: list[dict] = []
        for asset_type, items in (payload.get("assets_by_type") or {}).items():
            if asset_type in ("OTHER", "SHOT"):
                continue
            for item in items or []:
                candidates.append({"asset_type": asset_type, "name": item.get("name") or "未命名",
                                   "prompt": item.get("prompt") or ""})
        candidates = [c for c in candidates if c["prompt"].strip()][:max(0, int(limit))]
        empty = {"enabled": True, "created": [], "locked": [], "blocked": [], "reused": [],
                 "refs_attached": 0, "errors": [], "rounds": [], "_tensors": [], "_paths": []}
        if not candidates:
            return empty

        audit.append(f"素材生图：加载图像模型 {ckpt}，待处理 {len(candidates)} 张（审核轮数≤{rounds}）")
        raw = _loader_call(CheckpointLoaderSimple, "load_checkpoint", "ckpt_name", ckpt,
                           first=False)
        img_model, img_clip, img_vae = raw[0], raw[1], raw[2]

        created, locked, blocked, errors, tensors, ok_paths = [], [], [], [], [], []
        created_paths: dict[str, list[str]] = {}
        review_rows: list[dict] = []
        asset_rows: list[dict] = []
        # ROUND49：素材审核阈值注入（不改库源码；不可用时如实告警并回退配置阈值）
        _restore_thr, _thr_ok = _review_threshold_override(review_threshold)
        _thr_note_done = False
        if int(review_threshold) >= 0 and not _thr_ok and not _thr_note_done:
            _thr_note_done = True
            warnings.append(
                f"⚠️ 素材审核阈值 {int(review_threshold)} 运行时注入不可用（review 配置接口变化）："
                "本次按项目配置阈值审核；可把阈值写入 config/user.yaml 的 "
                "asset_review.score_threshold 后重跑")
        for idx, cand in enumerate(candidates):
            asset = None
            png: Path | None = None
            try:
                found = store.find_by_normalized_name(cand["name"]) if cand["name"] else []
                asset = found[0] if found else None
                if asset is None:
                    asset = store.create_asset(
                        asset_type=cand["asset_type"], name=cand["name"], source="AI",
                        library_scope="PROJECT", note=cand["prompt"])
                aid = str(asset["asset_id"])
                # 复用已 LOCKED 版本（同一项目重跑不重复生成/审核）
                if reuse_locked:
                    locked_ver = store.latest_locked_version(aid)
                    if locked_ver:
                        ver_row = store.get_version(aid, locked_ver) or {}
                        f = ver_row.get("file_path")
                        if f:
                            abs_path = Path(str(f))
                            if not abs_path.is_absolute():
                                abs_path = paths.absolute_from_relative(str(f))
                            if abs_path.is_file():
                                tensors.append(_image_tensor(abs_path))
                                ok_paths.append(str(abs_path))
                                locked.append({"asset_id": aid, "version": locked_ver,
                                               "png": str(abs_path), "reused": True})
                                audit.append(f"  素材 {cand['name']} {locked_ver} 已 LOCKED → 复用")
                                continue
                locked_ver = None
                for rnd in range(max(1, int(rounds))):
                    t0 = time.time()
                    image = _generate_image(
                        img_model, img_clip, img_vae,
                        prompt=cand["prompt"] + ("，细节丰富，高质量" if rnd else ""),
                        negative=_NEGATIVE, width=int(resolution), height=int(resolution),
                        steps=int(steps) + rnd * 4, cfg=float(cfg),
                        seed=int(seed) + idx * 97 + rnd,
                    )
                    png = project_dir / "assets" / (
                        f"{aid}_{int(time.time())}_r{rnd + 1}.png")
                    _save_tensor_png(image, png)
                    created_paths.setdefault(aid, []).append(str(png))
                    version = store.add_version(aid, source="AI", file_path=png,
                                                status="PENDING_REVIEW")
                    ver = str(version.get("version") or version.get("version_number") or "v1")
                    created.append({"asset_id": aid, "type": cand["asset_type"],
                                    "name": cand["name"], "version": ver,
                                    "png": str(png), "round": rnd + 1})
                    audit.append(
                        f"  素材 {cand['asset_type']} {cand['name']} → {ver} 候选{rnd + 1} "
                        f"（{round(time.time() - t0, 1)}s）")
                    # ---- 素材审核（完整状态机：CNT1 / 唯一提交 / LOCK / FALLBACK）----
                    from .nodes import BetterComfyUIAssetReview

                    action = ("人工通过" if str(review_mode).startswith("人工")
                              else "模型审核（本地启发式）")
                    rev = BetterComfyUIAssetReview().run(
                        project_id=project_id, action=action, asset_id=aid, version=ver)
                    values = rev.get("result") or () if isinstance(rev, dict) else ()
                    state = str(values[0]) if len(values) > 0 else ""
                    score_payload = _safe_json(str(values[1])) if len(values) > 1 else {}
                    lock_info = str(values[2]) if len(values) > 2 else ""
                    score = score_payload.get("score") if isinstance(score_payload, dict) else None
                    scores = score_payload.get("scores") if isinstance(score_payload, dict) else None
                    reason = score_payload.get("reason") if isinstance(score_payload, dict) else None
                    review_rows.append({
                        "asset_id": aid, "name": cand["name"], "version": ver,
                        "round": rnd + 1, "state": state, "score": score,
                        "scores": scores, "reason": reason, "lock_info": lock_info[:200],
                    })
                    audit.append(f"  素材审核：{state} 评分={score} "
                                 f"{('画质/构图=' + str(scores)) if scores else ''}")
                    if state in ("ALLOWED", "FALLBACK"):
                        locked_ver = ver
                        break
                    if state in ("FAILED", "ABORTED"):
                        break
                lock_ver = locked_ver or store.latest_locked_version(aid)
                if lock_ver:
                    ver_row = store.get_version(aid, lock_ver) or {}
                    f = ver_row.get("file_path")
                    abs_path = None
                    if f:
                        abs_path = Path(str(f))
                        if not abs_path.is_absolute():
                            abs_path = paths.absolute_from_relative(str(f))
                    if abs_path is None or not abs_path.is_file():
                        # 兜底锁到的是更早候选：用本资产记录过的 png 反查
                        for p in created_paths.get(aid, []):
                            if Path(p).is_file():
                                abs_path = Path(p)
                    if abs_path is not None and abs_path.is_file():
                        tensors.append(_image_tensor(abs_path))
                        ok_paths.append(str(abs_path))
                        locked.append({"asset_id": aid, "version": lock_ver,
                                       "png": str(abs_path),
                                       "state": [r for r in review_rows
                                                 if r["version"] == lock_ver][-1]["state"]
                                       if any(r["version"] == lock_ver for r in review_rows)
                                       else "LOCKED"})
                        audit.append(f"  素材审核 → LOCKED {lock_ver}（挂参考图）")
                    else:
                        blocked.append({"asset_id": aid, "version": lock_ver,
                                        "state": "LOCKED_BUT_FILE_MISSING"})
                        warnings.append(f"⚠️ 素材 {cand['name']} {lock_ver} 已 LOCK 但文件缺失，不挂图")
                else:
                    last_state = review_rows[-1]["state"] if review_rows else "BLOCKED"
                    blocked.append({"asset_id": aid, "version": "latest", "state": last_state})
                    audit.append(f"  素材审核：{last_state} → 未 LOCK（不挂参考图）")
                    warnings.append(
                        f"⚠️ 素材 {cand['name']} 多轮候选仍未 LOCK（{last_state}）："
                        "本地启发式阈值/配置因素；未过审素材绝不挂进导演组（R3）")
                # ROUND49：逐素材结构化行（score/state/lock_status/version/scores/说明）
                _rounds_asset = [r for r in review_rows if r.get("asset_id") == aid]
                _scores_asset = [float(r["score"]) for r in _rounds_asset
                                 if isinstance(r.get("score"), (int, float))]
                _lock_ver_final = store.latest_locked_version(aid)
                _last_round = _rounds_asset[-1] if _rounds_asset else {}
                asset_rows.append({
                    "asset_id": aid,
                    "type": cand.get("asset_type"),
                    "name": cand.get("name"),
                    "version": _lock_ver_final or _last_round.get("version"),
                    "versions": [r.get("version") for r in _rounds_asset],
                    "rounds": len(_rounds_asset),
                    "score": round(max(_scores_asset), 2) if _scores_asset else None,
                    "avg_score": (round(sum(_scores_asset) / len(_scores_asset), 2)
                                  if _scores_asset else None),
                    "scores": _last_round.get("scores"),
                    "state": _last_round.get("state") or "FAILED",
                    "lock_status": "LOCKED" if _lock_ver_final else "UNLOCKED",
                    "fallback": any(str(r.get("state")) == "FALLBACK" for r in _rounds_asset),
                    "note": _last_round.get("reason") or "本地启发式评分/阈值判定",
                })
            except Exception as exc:  # noqa: BLE001 - 单个素材失败不拖垮全链，但如实记录
                errors.append(f"{cand.get('asset_type')} {cand.get('name')}: "
                              f"{type(exc).__name__}: {exc}")
                asset_rows.append({
                    "asset_id": locals().get("aid"),
                    "type": cand.get("asset_type"), "name": cand.get("name"),
                    "version": None, "versions": [], "rounds": 0,
                    "score": None, "avg_score": None, "scores": None,
                    "state": "FAILED", "lock_status": "UNLOCKED", "fallback": False,
                    "note": f"{type(exc).__name__}: {exc}",
                })
                audit.append(f"  ⚠️ 素材失败：{type(exc).__name__}: {exc}")
                warnings.append(f"⚠️ 素材 {cand.get('name')} 失败：{type(exc).__name__}: {exc}")
        if _restore_thr:
            _restore_thr()
        _fallback_n = sum(1 for row in asset_rows if row.get("fallback"))
        _summary = _asset_review_summary(
            asset_rows, threshold=(int(review_threshold) if int(review_threshold) >= 0
                                   else None),
            review_mode=review_mode, generated=len(created), fallback=_fallback_n)
        return {
            "enabled": True, "created": created, "locked": locked, "blocked": blocked,
            "reused": [row for row in locked if row.get("reused")],
            "refs_attached": 0, "errors": errors, "rounds": review_rows,
            "assets": asset_rows,
            "review_summary": _summary,
            "threshold_used": (int(review_threshold) if int(review_threshold) >= 0 else None),
            "threshold_injected": bool(_thr_ok),
            "_tensors": tensors, "_paths": ok_paths,
        }

    # ==================================================================
    def _video_review(self, final_path: Path, 项目: str, 每段时长_秒: float,
                      audit: list[str], *, 审核范围: str = _VIDEO_REVIEW_SCOPES[0],
                      段阈值覆盖: float = -1.0, 整片阈值覆盖: float = -1.0,
                      整片失败策略: str = _FULL_POLICIES[0],
                      边界相似度阈值: float = 55.0,
                      project_id: str = "",
                      本次是级联重做: bool = False) -> dict:
        """段级 R_seg + 整片 R_full 审核（复用本包视频审核节点，真实分数）。

        修复 P0-1：`BetterComfyUIVideoReview.run()` 直接返回 5 元组（结论/段JSON/
        整片JSON/需重生成/审计），旧代码按 dict `.get("result")` 解析会 AttributeError。
        这里同时兼容 dict 与 tuple 两种返回。
        """
        from .review_nodes import BetterComfyUIVideoReview

        try:
            result = BetterComfyUIVideoReview().run(
                项目=项目, 视频文件路径=str(final_path), 每段时长_秒=float(每段时长_秒),
                审核范围=审核范围, 段阈值覆盖=float(段阈值覆盖),
                整片阈值覆盖=float(整片阈值覆盖), 整片失败策略=整片失败策略,
                边界相似度阈值=float(边界相似度阈值),
                本次是级联重做=bool(本次是级联重做),
            )
            values = result.get("result") if isinstance(result, dict) else result
            if not isinstance(values, (list, tuple)):
                raise RuntimeError(f"视频审核返回结构异常：{type(result).__name__}")
            verdict = str(values[0]) if len(values) > 0 else ""
            seg_json = str(values[1]) if len(values) > 1 else ""
            full_json = str(values[2]) if len(values) > 2 else ""
            need = str(values[3]) if len(values) > 3 else ""
            summary = str(values[4]) if len(values) > 4 else ""
            seg_obj = _safe_json(seg_json)
            full_obj = _safe_json(full_json)
            audit.append(f"视频审核：{verdict}")
            for line in summary.splitlines():
                if line.strip():
                    audit.append(f"  {line.strip()}")
            # ROUND49 P1：R_seg 结构化（兼容旧 segment_index/dimensions 键）
            segment_scores = _map_video_segment_scores(seg_obj)
            need_segments = _parse_need_regenerate(need)
            # 边界相似度（第 N 段尾帧 vs 第 N+1 段首帧）来自本项目 review 历史
            edges = self._read_edge_history(项目, project_id=project_id)
            if edges.get("edges"):
                audit.append(f"边界相似度：{json.dumps(edges['edges'], ensure_ascii=False)}")
            return {
                "verdict": verdict,
                "segments": segment_scores,
                "segment_scores": segment_scores,
                "segment_review": seg_obj,
                "full": full_obj,
                "full_review": full_obj,
                "need_regenerate": need,
                "need_regenerate_segments": need_segments,
                "edges": edges.get("edges") or {},
                "rework_rounds": [],
                "cnt2": [],
                "cnt3": [],
                "fallback": [],
                "audit": summary,
                "error": None,
                "return_type": type(result).__name__,
            }
        except Exception as exc:  # noqa: BLE001 - 审核失败要报告，不能假装通过
            audit.append(f"⚠️ 视频审核失败（如实记录，不冒充通过）：{type(exc).__name__}: {exc}")
            return {"verdict": f"审核失败：{type(exc).__name__}: {exc}", "error": str(exc),
                    "segment_scores": [], "full": {}, "edges": {}}

    @staticmethod
    def _read_edge_history(项目: str, *, project_id: str = "") -> dict:
        """读项目 review/video_reviews.json（含段间边界相似度实证）。"""
        try:
            root = BetterComfyUIOneStop._project_root()
            candidates: list[Path] = []
            if project_id:
                candidates.append(root / project_id)
            for d in sorted(root.glob("*")):
                if d.is_dir() and d not in candidates:
                    candidates.append(d)
            for d in candidates:
                f = d / "review" / "video_reviews.json"
                if f.is_file():
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return data
        except Exception:  # noqa: BLE001 - 证据读取失败不影响审核结论
            pass
        return {}

    # ==================================================================
    @staticmethod
    def _repo_root() -> Path:
        """编排包根目录（BETTERCOMFYUI_HOME 优先，否则本插件的上级目录）。"""
        env_home = os.environ.get("BETTERCOMFYUI_HOME", "").strip()
        return Path(env_home) if env_home else Path(__file__).resolve().parent.parent

    @staticmethod
    def _project_root() -> Path:
        from bettercomfyui.config import load_config

        repo = BetterComfyUIOneStop._repo_root()
        cfg = load_config(repo_root=repo)
        root = Path(str(cfg.global_.project_root or "projects"))
        if not root.is_absolute():
            root = repo / root
        return root

    @classmethod
    def _project_dir(cls, project_id: str) -> Path:
        root = cls._project_root()
        return root / project_id if project_id else root / "_one_stop_tmp"

    @staticmethod
    def _review(final_path: Path, meta: dict, *, expected_s: float,
                threshold: int, audit: list[str],
                continuity_enhanced: bool = False,
                expected_segments: int = 1,
                actual_segments: int = 1,
                plan_seconds: float = 0.0) -> tuple[dict, dict]:
        from bettercomfyui.review import local_scorer

        # 段间连续会让每段多保留对齐余量（keep_tail），时长容忍度如实放宽并在报告中注明
        tolerance = max(0.6, expected_s * 0.03) + (1.0 if continuity_enhanced else 0.0)
        integrity = {
            "executed": True,
            "final": str(final_path),
            "frames": meta.get("frames"),
            "duration_s": meta.get("duration_s"),
            "expected_s": round(float(expected_s), 3),
            "duration_tolerance_s": round(tolerance, 3),
            "duration_ok": abs(float(meta.get("duration_s") or 0) - float(expected_s)) <= tolerance,
            "has_audio": bool(meta.get("has_audio")),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "fps": meta.get("fps"),
            "placeholder_flags": [] if meta.get("has_audio") else ["无音轨（疑似占位/音频丢失）"],
        }
        # P0-6 缺段硬校验：导演台/编码链路少出段时必须判不通过（防缺段假成功）
        integrity["expected_segments"] = int(expected_segments)
        integrity["actual_segments"] = int(actual_segments)
        integrity["segment_count_ok"] = bool(int(actual_segments) >= int(expected_segments))
        expected_duration = float(plan_seconds or expected_s)
        actual_duration = float(meta.get("duration_s") or 0.0)
        integrity["plan_duration_s"] = round(expected_duration, 3)
        integrity["duration_ratio_ok"] = bool(
            expected_duration <= 0 or actual_duration >= expected_duration * 0.7)
        if not integrity["segment_count_ok"]:
            audit.append(
                f"⚠️ 缺段：计划 {expected_segments} 段，实际编码 {actual_segments} 段 → 完整性不通过")
        if not integrity["duration_ratio_ok"]:
            audit.append(
                f"⚠️ 时长不足：计划 {expected_duration:.2f}s，实际 {actual_duration:.2f}s（<70%）"
                " → 完整性不通过")
        integrity["passed"] = bool(
            integrity["duration_ok"] and integrity["has_audio"]
            and integrity["segment_count_ok"] and integrity["duration_ratio_ok"])
        try:
            scores = local_scorer.video_scores(final_path)
        except Exception as exc:  # noqa: BLE001 - 评不出来要如实报告
            scores = {"error": f"{type(exc).__name__}: {exc}"}
        numeric = [float(v) for v in scores.values() if isinstance(v, (int, float))]
        avg = round(sum(numeric) / len(numeric), 2) if numeric else 0.0
        quality = {
            "scores": scores,
            "average": avg,
            "threshold": int(threshold),
            "passed": bool(numeric) and avg >= float(threshold)
            and float(scores.get("音画同步", 0) or 0) > 0,
            "score_source": "local_heuristic",
        }
        if not quality["passed"]:
            if float(scores.get("音画同步", 0) or 0) <= 0:
                audit.append("⚠️ 质量不通过：音画同步=0（无音轨/音频丢失），按硬约束判不通过。")
            else:
                audit.append(f"⚠️ 质量未过阈值：平均 {avg} < {threshold}（本地启发式）。")
        return integrity, quality

    @staticmethod
    def _export_to_comfyui(final_path: Path) -> dict | None:
        try:
            import shutil

            import folder_paths  # type: ignore

            out_root = Path(folder_paths.get_output_directory())
            sub = "剧核成片"
            target_dir = out_root / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / final_path.name
            counter = 1
            while target.exists():
                target = target_dir / f"{final_path.stem}_{counter}{final_path.suffix}"
                counter += 1
            shutil.copy2(final_path, target)
            return {"filename": target.name, "subfolder": sub, "type": "output"}
        except Exception:  # noqa: BLE001 - 非 ComfyUI 环境不阻断
            return None

    @staticmethod
    def _free_models() -> None:
        try:
            import comfy.model_management as mm  # type: ignore

            mm.unload_all_models()
            mm.soft_empty_cache()
        except Exception:  # noqa: BLE001
            pass
        gc.collect()


def pts0(prompts: list[str]) -> str:
    return prompts[0] if prompts else ""


def _parse_media(path: Path) -> dict:
    """ffmpeg -i 解析时长/宽高/fps/音轨（不依赖 ffprobe）。"""
    import re

    try:
        proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        text = (proc.stderr or "") + (proc.stdout or "")
    except Exception:  # noqa: BLE001
        text = ""
    info: dict[str, Any] = {"frames": None, "duration_s": None,
                            "has_audio": "Audio:" in text, "width": None,
                            "height": None, "fps": None, "path": str(path)}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        h, mi, s = m.groups()
        info["duration_s"] = round(int(h) * 3600 + int(mi) * 60 + float(s), 3)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        info["fps"] = float(m.group(1))
    if info["duration_s"] and info["fps"]:
        info["frames"] = int(round(info["duration_s"] * info["fps"]))
    return info


NODE_CLASS_MAPPINGS = {"BetterComfyUIOneStop": BetterComfyUIOneStop}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIOneStop": "剧核单节点（剧本→成片：素材+全功能导演台+审核+剪辑全内部）",
}
