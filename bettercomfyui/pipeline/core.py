"""整片拼接、成片封装、整片审核与交付（方案第七部分 · 二 与 · 九）。

流程位置（对照总流程图 cluster_SG6）：
    READY_TO_SPLICE -> SPLICE -> ENC(ENC1..ENC6) -> PACKAGE_READY
    -> R_full -> R1b -> FULL_DECISION -> S_OUT / FULL_POLICY -> OUT_CFG(OUT1..OUT4) -> OUT5

三条不可违反的规则（本模块是它们的最后一道闸门）：
1. **只有符合项目策略的片段才能进入拼接**；兜底段正常参与，但来源必须写进元数据，
   不允许在画面上伪装成正常通过（方案第七部分 · 二 第 6 条）。
2. **R_full 只在最终拼接之后、S_OUT 之前执行一次**，绝不因单段重生成反复触发。
3. **整片审核失败策略必须由配置显式选择**；没有配置就报错，
   不得隐式采用未记录的行为（方案第七部分 · 二 第 10 条）。

降级与兜底是**元数据事实**，不是界面提示：所有 `DegradeFlag` 都会进入
`MovieMetadata`，用户可据此定位兜底段并手动重发。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config, ConfigError
from ..models.entities import MovieMetadata, Segment, SegmentManifestItem
from ..models.enums import (
    DegradeFlag,
    FullReviewPolicy,
    SegmentSource,
    SegmentStatus,
)
from .errors import PipelineError

#: 允许进入最终拼接的段状态。
#: PASSED / FALLBACK_SELECTED 是「按策略可用」；EXCLUDED / FAILED / ABORTED 绝不能进拼接。
SPLICEABLE_STATUSES = frozenset(
    {SegmentStatus.PASSED, SegmentStatus.FALLBACK_SELECTED}
)


# ---------------------------------------------------------------------------
# 真实生成前置条件探测（**唯一真源**）
# ---------------------------------------------------------------------------

#: 真正的模型权重扩展名。用于把「权重」与「yaml 配置 / 占位文件」区分开。
WEIGHT_SUFFIXES = frozenset(
    {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft"}
)

#: 构成一次最小视频生成所必需的类别 -> 对应的 models/ 子目录
REQUIRED_MODEL_KINDS: dict[str, tuple[str, ...]] = {
    "扩散模型": ("diffusion_models", "unet", "checkpoints"),
    "文本编码器": ("text_encoders", "clip"),
    "VAE": ("vae",),
}

#: 低于此版本的 ComfyUI 装不了 MiniMaxH3 导演台系
MIN_COMFYUI_VERSION = "0.30.0"


def probe_model_kinds(models_dir: Path | str | None) -> dict:
    """检查 `models/` 下**构成一次最小视频生成所必需的三个类别**是否齐全。

    ## 为什么不满足于「数文件个数」

    实测踩到两次假阳性：

    1. 早期只排除 `put_*` 占位文件，把 `models/configs/` 下 11 个 yaml
       当成了模型，`doctor` 输出「模型权重文件数: 11」；
    2. 下载 LoRA 之后 `model_files` 变成 1，于是报「真实生成：环境看起来具备条件」
       —— 但那是 LoRA，**没有任何扩散模型**，根本出不了片。

    结论：**「有文件」不等于「能出片」**。
    `*.part`（下载中的临时文件）也必须排除，否则一个下到一半的模型会被算成可用。

    返回 `{"available": bool, "present": {类别: [文件]}, "missing": [类别],
          "files": [全部权重文件], "total": int}`。
    """
    present: dict[str, list[str]] = {k: [] for k in REQUIRED_MODEL_KINDS}
    all_files: list[str] = []
    if not models_dir:
        return {"available": False, "present": present,
                "missing": list(REQUIRED_MODEL_KINDS), "files": [], "total": 0}

    root = Path(models_dir)
    if not root.is_dir():
        return {"available": False, "present": present,
                "missing": list(REQUIRED_MODEL_KINDS), "files": [], "total": 0}

    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in WEIGHT_SUFFIXES:
            continue
        if f.name.startswith("put_") or f.name.endswith(".part"):
            continue
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        rel_str = str(rel)
        all_files.append(rel_str)
        top = rel.parts[0].lower() if rel.parts else ""
        for kind, dirs in REQUIRED_MODEL_KINDS.items():
            if top in dirs:
                present[kind].append(rel_str)
                break

    missing = [k for k, v in present.items() if not v]
    return {
        "available": not missing,
        "present": present,
        "missing": missing,
        "files": all_files,
        "total": len(all_files),
    }


#: 真实 H3 生成的显存下限（MiB）。
#: 依据：H3 六段生产实测峰值约 22,016 MiB；权重（扩散模型 + 34GB 级文本编码器 + VAE）
#: 需要同时驻留，低于 ~24GiB 的卡无法跑真实生成（16GB「勉强」也不应对外宣称可用）。
H3_MIN_VRAM_MIB = 24_000


def evaluate_compute_devices(
    devices: Any, *, min_vram_mib: int = H3_MIN_VRAM_MIB
) -> dict:
    """根据计算设备清单（ComfyUI `/system_stats` 的 `devices`）判断能否真实跑 H3。

    **「有权重 + 版本够」不等于「能出片」**：还必须有一块显存足够的加速卡。
    实测假阳性：本机权重齐全、ComfyUI 0.41，但 ComfyUI 跑在 `--cpu`、
    唯一 GPU 是 GTX 1050 Ti 4GB —— 面板却显示「真实生成可用：是」。

    返回 ``{"available", "device", "device_type", "vram_mib", "min_vram_mib", "reason"}``。
    解析不出设备/显存时**不猜**：一律按不可用处理并写清原因。
    """
    result: dict[str, Any] = {
        "available": False,
        "device": None,
        "device_type": None,
        "vram_mib": None,
        "min_vram_mib": int(min_vram_mib),
        "reason": "",
    }
    if not devices:
        result["reason"] = "未获取到计算设备清单（/system_stats devices 为空）——真实生成不可用"
        return result
    accelerators = [
        d
        for d in devices
        if isinstance(d, dict) and str(d.get("type", "")).strip().lower() not in ("", "cpu")
    ]
    if not accelerators:
        result["reason"] = "只有 CPU 设备（ComfyUI 以 --cpu 启动或无可用加速器）——真实 H3 生成不可用"
        return result
    best = max(accelerators, key=lambda d: int(d.get("vram_total") or 0))
    result["device"] = str(best.get("name") or "")
    result["device_type"] = str(best.get("type") or "")
    total = best.get("vram_total")
    if isinstance(total, (int, float)) and total > 0:
        result["vram_mib"] = int(total // (1024 * 1024))
    vram = result["vram_mib"]
    if vram is None:
        result["reason"] = "无法读取显存容量 —— 不猜，按真实生成不可用处理"
        return result
    if vram < int(min_vram_mib):
        result["reason"] = (
            f"显存 {vram} MiB < H3 最低要求 {int(min_vram_mib)} MiB"
            "（34GB 级权重无法驻留）——真实生成不可用"
        )
        return result
    result["available"] = True
    result["reason"] = f"设备 {result['device']}（{vram} MiB）满足 H3 最低显存要求"
    return result


# ---------------------------------------------------------------------------
# 拼接
# ---------------------------------------------------------------------------


@dataclass
class SpliceInput:
    """一段待拼接的视频及其审核来源信息。"""

    segment_id: str
    segment_version: str
    video_path: Path
    source: SegmentSource = SegmentSource.NORMAL
    degrade_flags: list[DegradeFlag] = field(default_factory=list)
    audit_id: str | None = None
    final_score: float | None = None
    asset_refs: list[dict] = field(default_factory=list)
    prompt_id: str | None = None
    cnt2: int = 0
    cnt3: int = 0
    cascade_status: str | None = None


def select_final_versions(segments: list[Segment]) -> list[Segment]:
    """挑选可进入拼接的段，按 `segment_index` 排序。

    拒绝进入的条件（任一命中即排除，并在结果中可见）：
    - `status` 不在 SPLICEABLE_STATUSES
    - `candidate_version` 为空（没有确定要拼的版本）
    - `review_status` 不是 ALLOWED / FALLBACK

    兜底段（FALLBACK_SELECTED）**正常参与拼接**，不额外打视觉标记；
    其兜底事实由 `degrade_flags` 与 `source` 承载。
    """
    ok: list[Segment] = []
    for seg in sorted(segments, key=lambda s: s.segment_index):
        if seg.status not in SPLICEABLE_STATUSES:
            continue
        if not seg.candidate_version:
            continue
        ok.append(seg)
    return ok


def build_splice_inputs(segments: list[Segment], *, project_dir: Path) -> list[SpliceInput]:
    """把已选定的段转换成拼接输入。文件不存在必须报错，绝不跳过。"""
    inputs: list[SpliceInput] = []
    for seg in select_final_versions(segments):
        attempt = seg.current_attempt()
        if attempt is None or not attempt.video_path:
            raise PipelineError(
                f"段 {seg.segment_id} 状态为 {seg.status.value} 但找不到版本 "
                f"{seg.candidate_version} 的视频文件记录；拒绝静默跳过（否则会产出缺段的成片）"
            )
        video = Path(attempt.video_path)
        if not video.is_absolute():
            video = project_dir / video
        if not video.exists():
            raise PipelineError(
                f"段 {seg.segment_id} 的段文件不存在：{video}；"
                "不存在的段绝不允许静默跳过"
            )
        inputs.append(
            SpliceInput(
                segment_id=seg.segment_id,
                segment_version=seg.candidate_version,
                video_path=video,
                source=seg.source,
                degrade_flags=list(seg.degrade_flags),
                audit_id=attempt.attempt_id,
                final_score=attempt.score,
                asset_refs=list(seg.asset_refs),
                prompt_id=attempt.job_id,
            )
        )
    return inputs


def _ffmpeg_exe() -> str:
    """定位 ffmpeg。优先 PATH，其次 imageio-ffmpeg 自带二进制。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # pragma: no cover - 依赖可选包
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - 需要给出可操作错误
        raise PipelineError(
            "找不到 ffmpeg：既不在 PATH，也无法从 imageio-ffmpeg 获取。"
            "请安装 ffmpeg 或 `pip install imageio-ffmpeg`。"
        ) from exc


def _run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1500:]
        raise PipelineError(f"{what} 失败（exit {proc.returncode}）：{tail}")
    return proc


def splice(
    inputs: list[SpliceInput],
    *,
    workdir: Path,
    out_path: Path | None = None,
    ffmpeg_runner: Callable[..., Any] | None = None,
) -> Path:
    """按顺序合并段视频。使用 ffmpeg concat demuxer（流复制，不重编码）。

    `ffmpeg_runner` 可注入用于离线测试；为 None 时调用真实 ffmpeg。
    """
    if not inputs:
        raise PipelineError("拼接输入为空：没有可拼接的段，拒绝产出空成片")

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_path) if out_path else workdir / "spliced.mp4"

    listing = workdir / "concat_list.txt"
    # concat demuxer 要求路径转义单引号；并统一使用正斜杠避免 Windows 反斜杠歧义
    lines = []
    for item in inputs:
        p = str(item.video_path.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{p}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        _ffmpeg_exe() if ffmpeg_runner is None else "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        str(out_path),
    ]
    if ffmpeg_runner is not None:
        ffmpeg_runner(cmd, what="拼接")
    else:
        _run(cmd, what="拼接")
    return out_path


# ---------------------------------------------------------------------------
# 封装
# ---------------------------------------------------------------------------


def encode(
    src: Path,
    cfg: Config,
    *,
    out_path: Path,
    subtitle_file: Path | None = None,
    watermark_file: Path | None = None,
    watermark_position: str = "br",
    audio_source: Path | None = None,
    bgm_file: Path | None = None,
    ffmpeg_runner: Callable[..., Any] | None = None,
) -> Path:
    """成片封装：音频拼接 / 转场 / 字幕 / 水印 / 格式 / 编码 / CRF。

    实际能力以已验证工具为准（方案明确要求）。**未实现的选项必须显式报错**，
    不能静默忽略用户设置 —— 静默忽略等于骗用户。

    字幕与水印现已**真正实现**（之前只会抛错）。但它们是**双开关**语义：

    * `subtitle=False`（默认）-> 不加字幕，忽略 `subtitle_file`；
    * `subtitle=True` 且**没给** `subtitle_file` -> 抛错（不能假装加了）；
    * `subtitle=True` 且给了文件 -> 真正烧进画面（`-vf subtitles=`）。

    水印同理。位置用 `watermark_position`：`tl/tr/bl/br`（四角）。
    """
    src = Path(src)
    if not src.exists():
        raise PipelineError(f"待封装文件不存在：{src}")

    enc = cfg.encode
    codec_map = {"H.264": "libx264", "H.265": "libx265"}
    if enc.codec not in codec_map:
        raise ConfigError(f"不支持的编码：{enc.codec}")
    container_map = {"MP4": ".mp4", "MOV": ".mov", "WebM": ".webm"}
    if enc.format not in container_map:
        raise ConfigError(f"不支持的输出格式：{enc.format}")

    # ---- 字幕：开启就必须给文件，否则报错（不假装） ----
    if enc.subtitle:
        if subtitle_file is None:
            raise PipelineError(
                "subtitle=true 但未提供 subtitle_file。\n"
                "字幕需要 .srt/.ass 文件；拒绝静默忽略该设置"
                "（否则用户以为加了字幕但成片没有）。"
            )
        subtitle_file = Path(subtitle_file)
        if not subtitle_file.is_file():
            raise PipelineError(f"字幕文件不存在：{subtitle_file}")
    else:
        subtitle_file = None

    # ---- 水印：同理 ----
    if enc.watermark:
        if watermark_file is None:
            raise PipelineError(
                "watermark=true 但未提供 watermark_file（水印图片路径）。"
                "拒绝静默忽略该设置。"
            )
        watermark_file = Path(watermark_file)
        if not watermark_file.is_file():
            raise PipelineError(f"水印图片不存在：{watermark_file}")
    else:
        watermark_file = None

    if enc.transition not in ("none", "fade"):
        raise ConfigError(f"不支持的转场：{enc.transition}")

    # ---- 音频（图一 ENC1「音频自动拼接可手动调」）----
    # `encode.audio_join` 此前**配了没人读**（ROUND18 发现）。
    # 这里把它变成真实行为，三种取值互斥：
    #
    #   auto   （默认）：保留各段音轨的拼接结果 —— 即原来的行为；
    #   mute          ：丢掉音轨（`-an`），成片无声；
    #   manual        ：用调用方给的 `audio_source` **整条替换**音轨，
    #                   没给就报错（绝不静默忽略 —— 那等于骗用户"换了音频"）。
    audio_join_mode = str(getattr(enc, "audio_join", "auto") or "auto").strip().lower()
    if audio_join_mode not in ("auto", "mute", "manual"):
        raise ConfigError(
            f"不支持的 encode.audio_join：{enc.audio_join!r}"
            "（只能是 auto / mute / manual）"
        )
    replace_audio: Path | None = None
    if audio_join_mode == "manual":
        if audio_source is None:
            raise PipelineError(
                "encode.audio_join=manual 但未提供 audio_source（整条音轨文件）。\n"
                "“手动调音频”意味着由调用方指定音轨；拒绝静默回退到自动拼接。"
            )
        replace_audio = Path(audio_source)
        if not replace_audio.is_file():
            raise PipelineError(f"audio_source 不存在：{replace_audio}")
    elif audio_source is not None:
        # auto/mute 下传了音轨文件属于**矛盾输入**，明确报错而不是悄悄忽略
        raise PipelineError(
            f"encode.audio_join={audio_join_mode} 与传入的 audio_source 冲突：\n"
            "要用指定音轨请把 audio_join 设为 manual；要自动拼接请不要传 audio_source。"
        )

    # ---- 背景音乐（图一 SG4 `O7`「背景音乐，后续需提进日程」）----
    # 取舍：传了 `bgm_file` 就混音（存在即意图，不加多余布尔开关）；
    # BGM 比成片短且 `bgm_loop=True` -> `-stream_loop -1` + `-shortest` 循环补齐；
    # 音量/淡入淡出/ducking 全部来自 `encode.*`，**不再有"配了不读"**。
    bgm_path: Path | None = None
    if bgm_file is not None:
        bgm_path = Path(bgm_file)
        if not bgm_path.is_file():
            raise PipelineError(f"背景音乐文件不存在：{bgm_path}")
        _vol = float(getattr(enc, "bgm_volume", 0.35))
        if not (0.0 <= _vol <= 1.0):
            raise ConfigError(f"encode.bgm_volume 必须在 0.0-1.0 之间，实际 {_vol}")
        if float(getattr(enc, "bgm_fade", 1.0)) < 0:
            raise ConfigError("encode.bgm_fade 不能为负数")

    # ---- 用 filter_complex 组合：字幕烧字 + 水印叠加 + 转场 + 背景音乐 ----
    ffmpeg = _ffmpeg_exe() if ffmpeg_runner is None else "ffmpeg"
    out_path = Path(out_path)

    # 输入顺序固定：0=成片, 1=水印(可选), 2=替换音轨(可选), 3=背景音乐(可选)。
    # `_build_video_filter_complex` 假定水印是 [1:v]，所以水印必须排第一。
    cmd = [ffmpeg, "-y", "-i", str(src)]
    _next_index = 1
    if watermark_file is not None:
        cmd += ["-i", str(watermark_file)]
        _next_index += 1
    replace_audio_index: int | None = None
    if replace_audio is not None:
        replace_audio_index = _next_index
        cmd += ["-i", str(replace_audio)]
        _next_index += 1
    bgm_index: int | None = None
    if bgm_path is not None:
        if bool(getattr(enc, "bgm_loop", True)):
            cmd += ["-stream_loop", "-1"]     # 无限循环，由 -shortest 按视频长度截断
        cmd += ["-i", str(bgm_path)]
        bgm_index = _next_index

    has_video_complex = subtitle_file is not None or watermark_file is not None

    if has_video_complex or bgm_index is not None or enc.transition == "fade":
        parts: list[str] = []
        if has_video_complex:
            parts.append(_build_video_filter_complex(
                subtitle_file, watermark_file, watermark_position, enc))
        elif enc.transition == "fade":
            parts.append("[0:v]fade=t=in:st=0:d=0.5[vout]")
        else:
            parts.append("[0:v]null[vout]")
        video_map = "[vout]"

        audio_map: str | None = None
        if bgm_index is not None:
            audio_parts, audio_out = _build_bgm_audio_graph(
                audio_join_mode=audio_join_mode,
                replace_audio_index=replace_audio_index,
                bgm_index=bgm_index,
                enc=enc,
                duration_s=_safe_duration(src),
            )
            parts.extend(audio_parts)
            audio_map = f"[{audio_out}]"
        elif audio_join_mode != "mute":
            audio_map = "0:a?"

        cmd += ["-filter_complex", ";".join(parts), "-map", video_map]
        if audio_map:
            cmd += ["-map", audio_map]
    else:
        if enc.transition == "fade":
            cmd += ["-vf", "fade=t=in:st=0:d=0.5",
                    "-af", "afade=t=in:st=0:d=0.5,afade=t=out:st=-0.5:d=0.5"]

    cmd += [
        "-c:v",
        codec_map[enc.codec],
        "-crf",
        str(enc.crf),
    ]

    # 音频三态（图一 ENC1）：auto 保留拼接音轨 / mute 丢掉 / manual 整条替换。
    # 有 BGM 时音频已在上面 filter_complex 里混好并 `-map [aout]`，
    # 这里只处理**没有 BGM** 的情况。
    if bgm_index is None:
        if audio_join_mode == "mute":
            cmd += ["-an"]
        elif audio_join_mode == "manual":
            cmd += ["-map", "0:v:0", "-map", f"{replace_audio_index}:a:0"]

    cmd += ["-c:a", "aac"]

    if bgm_path is not None and bool(getattr(enc, "bgm_loop", True)):
        # 循环补齐的 BGM 必须按视频长度截断，否则成片会被拉长
        cmd += ["-shortest"]

    cmd += [
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    if ffmpeg_runner is not None:
        ffmpeg_runner(cmd, what="封装")
    else:
        _run(cmd, what="封装")
    return out_path


def _safe_duration(path: Path) -> float | None:
    """尽力取时长，取不到返回 None（调用方必须能容忍取不到）。"""
    try:
        info = probe_media(path)
        value = float(info.get("duration_s") or 0.0)
        return value if value > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _build_bgm_audio_graph(
    *,
    audio_join_mode: str,
    replace_audio_index: int | None,
    bgm_index: int,
    enc,
    duration_s: float | None,
) -> tuple[list[str], str]:
    """构造背景音乐混音滤镜图。返回 `(滤镜段列表, 输出标签)`（标签不带方括号）。

    音频链形状：

        前景（原声 0:a / 指定音轨 N:a） ─┐
                                        ├─ amix -> [aout]
        BGM ─ volume -> 淡入/淡出 ──────┘
              （可选 sidechaincompress 用前景做 sidechain 实现 ducking）

    `audio_join=mute` 时没有前景，BGM 单独成轨（成片只有配乐、无原声）。
    """
    volume = float(getattr(enc, "bgm_volume", 0.35))
    fade = max(0.0, float(getattr(enc, "bgm_fade", 1.0)))
    ducking = bool(getattr(enc, "bgm_ducking", False))
    parts: list[str] = []

    bgm_filters = [f"volume={volume:.4f}"]
    if fade > 0:
        bgm_filters.append(f"afade=t=in:st=0:d={fade:.3f}")
        if duration_s is not None and duration_s > fade:
            bgm_filters.append(
                f"afade=t=out:st={duration_s - fade:.3f}:d={fade:.3f}")
    parts.append(f"[{bgm_index}:a]" + ",".join(bgm_filters) + "[bgmv]")

    if audio_join_mode == "mute":
        return parts, "bgmv"

    fg_label = (f"{replace_audio_index}:a" if replace_audio_index is not None else "0:a")
    if ducking:
        # 用前景做 sidechain：前景响时把 BGM 压下去
        parts.append(
            "[bgmv][fg]sidechaincompress="
            "threshold=0.05:ratio=8:attack=20:release=400[bgmduck]")
        parts.append(f"[{fg_label}]anull[fg]")
        parts.append(
            "[fg][bgmduck]amix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0[aout]")
    else:
        parts.append(
            f"[{fg_label}][bgmv]amix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0[aout]"
        )
    return parts, "aout"


def pos_br(position: str) -> str:
    return {"tl": "10:10", "tr": "W-w-10:10",
            "bl": "10:H-h-10", "br": "W-w-10:H-h-10"}.get(position, "W-w-10:H-h-10")


def _build_video_filter_complex(
    subtitle_file: Path | None,
    watermark_file: Path | None,
    watermark_position: str,
    enc,
) -> str:
    """构造 `-filter_complex` 链：字幕烧字 -> 水印叠加 -> 淡入。"""
    # ffmpeg filter 语法里反斜杠与冒号需要转义，否则 Windows 路径会被解析错
    def esc(p: Path) -> str:
        return str(p.resolve()).replace("\\", "/").replace(":", r"\:")

    parts: list[str] = []
    if subtitle_file is not None:
        parts.append(f"subtitles='{esc(subtitle_file)}'")
    if enc.transition == "fade":
        parts.append("fade=t=in:st=0:d=0.5")
    video_chain = ",".join(parts)

    if watermark_file is None:
        return f"[0:v]{video_chain}[vout]" if video_chain else "[0:v]null[vout]"

    pos = pos_br(watermark_position)
    if video_chain:
        return f"[0:v]{video_chain}[vbase];[vbase][1:v]overlay={pos}[vout]"
    return f"[0:v][1:v]overlay={pos}[vout]"


# ---------------------------------------------------------------------------
# 整片审核
# ---------------------------------------------------------------------------


def full_review(
    movie: Path,
    cfg: Config,
    *,
    scorer: Callable[[Path, list[str]], dict[str, float]] | None = None,
    call_counter: list[int] | None = None,
) -> dict:
    """R_full：整片级审核，R1b 四维：叙事流畅 / 音画同步 / 节奏合理 / 无明显跳变。

    **只在最终拼接之后、S_OUT 之前执行一次。** `call_counter` 用于在测试中断言
    它确实只被调用一次（N1 单段重生成绝不触发本函数）。

    `scorer` 为可注入的评分实现；为 None 时必须显式报错，
    不允许用随机数或常量冒充「审核通过」。
    """
    movie = Path(movie)
    if not movie.exists():
        raise PipelineError(f"整片审核失败：文件不存在 {movie}")
    if call_counter is not None:
        call_counter.append(1)

    if scorer is None:
        raise PipelineError(
            "整片审核未配置评分模型（scorer=None）。"
            "拒绝用占位分数冒充审核结果 —— 这会把未审核的成片说成已通过。"
            "请配置本地评分模型（方案要求视频/图片审核全部本地完成）。"
        )

    dims = list(cfg.video_review.r1b_dimensions)
    if not dims:
        raise ConfigError("video_review.r1b_dimensions 不能为空")

    raw = scorer(movie, dims)
    missing = [d for d in dims if d not in raw]
    if missing:
        raise PipelineError(f"整片审核评分缺少维度：{missing}；期望维度 {dims}")

    scores = {d: float(raw[d]) for d in dims}
    if any(not (0.0 <= s <= 100.0) for s in scores.values()):
        raise PipelineError(f"整片审核评分必须落在 [0,100]：{scores}")

    final = sum(scores.values()) / len(scores)
    threshold = float(cfg.video_review.threshold_full)
    failure_reasons: list[str] = []
    if final < threshold:
        failure_reasons.append(f"整片评分 {final:.2f} 低于阈值 {threshold}")
    # 硬约束：H3 成片必须带音频。音画同步=0 表示「无音轨/无法测量」，
    # 不允许被其它维度平均后判通过（2026-10-18 目标机回归发现：无音轨视频
    # (100+0+97+100)/4=74.3 > 70 曾被判通过，等于把缺音轨的成片说成合格）。
    if "音画同步" in scores and scores["音画同步"] <= 0.0:
        failure_reasons.append(
            "音画同步为 0：无音轨或音画时长无法测量（H3 成片必须有音频，不允许平均抵消）")
    return {
        "r1b_scores": scores,
        "final_score": final,
        "threshold": threshold,
        "passed": not failure_reasons,
        "failure_reason": "；".join(failure_reasons),
    }


def apply_full_policy(result: dict, cfg: Config) -> dict:
    """整片审核失败后的处置。**必须由配置显式选择**，否则报错。

    - BLOCK_OUTPUT：保留检查点，等待重新配置或人工处理
    - DEGRADE_OUTPUT：记录 FULL_REVIEW_DEGRADED、整片评分和失败原因，允许交付
    """
    if result.get("passed"):
        return {
            "action": "proceed",
            "degrade_flags": [],
            "delivered_with_degradation": False,
        }

    policy = cfg.video_review.full_review_policy
    if policy is None:
        raise ConfigError(
            "video_review.full_review_policy 未显式配置："
            "整片审核未通过时必须明确选择 BLOCK_OUTPUT 或 DEGRADE_OUTPUT，"
            "不得隐式采用未记录的行为。"
        )
    if policy is FullReviewPolicy.BLOCK_OUTPUT:
        return {
            "action": "block",
            "degrade_flags": [],
            "delivered_with_degradation": False,
            "message": "保留检查点，等待重新配置或人工处理",
        }
    return {
        "action": "degrade",
        "degrade_flags": [DegradeFlag.FULL_REVIEW_DEGRADED],
        "delivered_with_degradation": True,
        "message": result.get("failure_reason", ""),
    }


# ---------------------------------------------------------------------------
# 交付
# ---------------------------------------------------------------------------


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _probe_with_ffmpeg(path: Path) -> dict:
    """用 ffmpeg 读取媒体信息（无需 ffprobe）。

    注意：ffmpeg 在「只读取信息」时返回码不是 0（没有输出文件），
    因此**不能**用 returncode 判断成败，必须解析 stderr。
    解析不到 Duration 就如实抛错，不返回 None 冒充成功。
    """
    import re

    proc = subprocess.run(
        [_ffmpeg_exe(), "-i", str(path), "-hide_banner"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    stderr = proc.stderr or ""

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not m:
        # 文件扩展名对但内容不是合法媒体 —— 必须报错，绝不能当作通过
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else "(无 stderr)"
        raise PipelineError(
            f"交付校验失败：{path} 无法解析为合法媒体（ffmpeg: {tail[:200]}）"
        )
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), float(m.group(3))
    duration = hours * 3600 + minutes * 60 + seconds

    has_video = "Video:" in stderr
    has_audio = "Audio:" in stderr
    vcodec = None
    acodec = None
    vm = re.search(r"Video:\s*([A-Za-z0-9_\-]+)", stderr)
    if vm:
        vcodec = vm.group(1)
    am = re.search(r"Audio:\s*([A-Za-z0-9_\-]+)", stderr)
    if am:
        acodec = am.group(1)

    return {
        "duration_s": duration,
        "has_audio": has_audio,
        "has_video": has_video,
        "video_codec": vcodec,
        "audio_codec": acodec,
    }


def probe_media(path: Path, *, ffprobe_runner: Callable[..., Any] | None = None) -> dict:
    """输出前基本信息校验：文件存在、可读、时长、是否有音轨。

    方案要求「输出前检查文件存在、可读取、时长/音轨等基本信息」。
    探测失败必须报错，不得当作通过。

    这是**交付前的最后一道闸门**：如果这里只检查「文件存在」，
    那么一个 0 字节或损坏的文件也会被当成成功交付。
    """
    path = Path(path)
    if not path.exists():
        raise PipelineError(f"交付校验失败：文件不存在 {path}")
    try:
        if path.stat().st_size < 4096:
            _ = path.read_bytes()      # 小文件直接完整读，验证真的可读
        else:
            _ = _read_head(path)       # 大文件读头部，避免整文件进内存
    except OSError as exc:
        raise PipelineError(f"交付校验失败：文件不可读 {path}：{exc}") from exc
    if path.stat().st_size == 0:
        raise PipelineError(f"交付校验失败：文件为空 {path}")

    if ffprobe_runner is not None:
        info = ffprobe_runner(path)
    else:
        info = _probe_with_ffmpeg(path)

    duration = info.get("duration_s")
    if duration is None or duration <= 0:
        raise PipelineError(
            f"交付校验失败：{path} 的时长为 {duration!r}，不是有效媒体"
        )
    # 方案要求「检查时长/音轨等基本信息」；整片交付必须有时长，音轨按配置说明
    return {
        "exists": True,
        "readable": True,
        "size_bytes": path.stat().st_size,
        "duration_s": round(float(duration), 3),
        "has_audio": info.get("has_audio"),
        "has_video": info.get("has_video"),
        "video_codec": info.get("video_codec"),
        "audio_codec": info.get("audio_codec"),
        "probed": True,
    }


def _read_head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(4096)


def write_output(
    movie: Path,
    cfg: Config,
    *,
    project_dir: Path,
    metadata: MovieMetadata,
    workdir: Path | None = None,
    ffprobe_runner: Callable[..., Any] | None = None,
) -> dict:
    """S_OUT + OUT_CFG + OUT1..OUT4。

    - OUT1 输出目录：output/项目名/日期/剧本名
    - OUT2 文件名：项目名/剧本名/日期/版本
    - OUT3 元数据：剧本/模型/参数/时间/版本/来源/审核分数/降级标记
    - OUT4 封面：默认第一帧，可指定

    交付后记录路径、元数据、审核状态与项目状态。
    """
    from ..models.entities import now_iso

    movie = Path(movie)
    checks = probe_media(movie, ffprobe_runner=ffprobe_runner)
    project_dir = Path(project_dir)
    workdir = Path(workdir) if workdir else project_dir / "output"

    date_str = now_iso()[:10]
    script_name = metadata.script_id or "script"
    display = metadata.project_display_name or metadata.project_id
    out_dir = workdir / display / date_str / script_name
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = "".join(Path(movie).suffixes[-1:]) or ".mp4"
    final_name = f"{display}_{script_name}_{date_str}_v{metadata.workflow_version}{ext}"
    final_movie = out_dir / final_name
    shutil.copy2(movie, final_movie)

    metadata.final_output_path = str(final_movie)
    metadata.movie_content_hash = _sha256_of_file(final_movie)
    metadata.output_time = now_iso()
    metadata.chapters = list(metadata.chapters)
    for item in metadata.segments:
        if not isinstance(item, SegmentManifestItem):
            raise PipelineError(f"metadata.segments 元素必须是 SegmentManifestItem：{item!r}")

    # ---- mock / 未验证边界：必须随成片一起落盘，不能只存在当次 stdout ----
    # 任务书第 9 节末句要求「必须明确区分真实集成与 mock 测试」。
    # 理由：成片文件（mp4）在磁盘上**无法**区分「模型生成」与「占位媒体」，
    # 来源若只打印在控制台，下次有人拿到 mp4 + metadata 就会误当真实产出。
    if not metadata.generation_provenance:
        raise PipelineError(
            "拒绝写出 metadata：generation_provenance 为空。\n"
            "成片必须声明生成来源（至少 is_mock / executor），否则下游无法区分"
            "「真实模型生成」与「占位媒体」。这是任务书第 9 节的硬要求。"
        )
    if "is_mock" not in metadata.generation_provenance:
        raise PipelineError(
            "generation_provenance 必须包含 is_mock 布尔字段；"
            f"实际收到：{metadata.generation_provenance!r}"
        )

    prov = dict(metadata.generation_provenance)
    prov.setdefault("unverified", [])
    prov.setdefault("executor", "unknown")
    prov.setdefault("reason", "")
    if prov["is_mock"]:
        # 给人一眼能看到的警告，放在 metadata 顶层显眼位置
        prov["warning"] = (
            "⚠️ 本成片由占位（mock）路径产出，**不是**真实模型生成结果。"
            "不得作为真实生成已通过的证据。"
        )

    meta_path = out_dir / f"{final_movie.stem}.metadata.json"
    payload = metadata.to_dict()
    payload["generation_provenance"] = prov
    payload["is_mock_output"] = bool(prov["is_mock"])
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cover_path: Path | None = None
    if cfg.encode.cover == "first_frame":
        first = next(
            (s.video_path for s in metadata.segments if s.video_path), None
        )
        if first and Path(first).exists():
            cover_path = out_dir / f"{final_movie.stem}.cover.jpg"
            cmd = [
                _ffmpeg_exe(),
                "-y",
                "-i",
                str(first),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(cover_path),
            ]
            try:
                _run(cmd, what="封面提取")
            except PipelineError:
                # 封面属于附加产物：失败不得让已完成的成片交付失败，但要如实标记
                cover_path = None

    return {
        "out_path": str(final_movie),
        "metadata_path": str(meta_path),
        "cover_path": str(cover_path) if cover_path else None,
        "checks": checks,
        "content_hash": metadata.movie_content_hash,
    }


__all__ = [
    "SPLICEABLE_STATUSES",
    "SpliceInput",
    "select_final_versions",
    "build_splice_inputs",
    "splice",
    "encode",
    "full_review",
    "apply_full_policy",
    "probe_media",
    "write_output",
]
