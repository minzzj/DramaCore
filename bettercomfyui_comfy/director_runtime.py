"""剧核 · 单节点导演台运行时（全功能时间轴 / 段间连续 / 可选包 / 证据）。

本模块只**调用** `vendor/h3_director/` 的源码，不改 vendor：
- 时间轴 timeline_data 按 `vendor/.../nodes/director_common.py::default_timeline_json`
  与 `director/pack.py`（timeline v5 段结构）构造；
- 段间连续走 vendor 原生路径：`timeline.output.continuityEnabled / continuityOverlapFrames /
  continuityMode / continuityRedraw / continuityKeepTail` + 每段 `continuityFromPrev`
  （解析见 `director/segment_continuity.py`，执行见 `director/executor_core.py` 的
  motion-context pin + 前缀裁切；官档默认 22 帧，档位 5/22/39/56）。
- SelfLift / Refine / FaceRefine / SemanticBridge 四个可选项用 vendor 的 pack 构造函数
  在节点内部直接生成，不依赖外部连线。
- 证据：`timeline_summary()` 给出可审计的时间轴摘要；`continuity_evidence()`
  从导演台 report 文本里提取「段间连续 ON / motion context Nf / Pin from prev #x」等实证行。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

# 段间连续档位（vendor h3_motion_context.CONTEXT_FRAME_CHOICES）
CONTEXT_FRAME_CHOICES: tuple[int, ...] = (5, 22, 39, 56)
DEFAULT_CONTEXT_FRAMES = 22
DEFAULT_AUDIO_CONTEXT_FRAMES = 24

_TASK_KEYS = ("t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v", "mixed")


# ---------------------------------------------------------------------------
# vendor 引用（全部延迟导入，保证没装 ComfyUI 时也能被 pytest import）
# ---------------------------------------------------------------------------
def task_type_options() -> list[str]:
    """导演台原生任务类型下拉（完整标签，含中英）。"""
    from .vendor.h3_director.lib.task_prompts import task_type_combo_options

    options, _meta = task_type_combo_options()
    return [str(x) for x in options]


def default_task_type_label() -> str:
    """默认 r2v（参考主体生视频）完整标签。"""
    for label in task_type_options():
        if resolve_task_key(label) == "r2v":
            return label
    return "r2v"


def resolve_task_key(task_type: str) -> str:
    from .vendor.h3_director.lib.task_prompts import resolve_task_key as _resolve

    return str(_resolve(str(task_type or "r2v")))


def duration_to_frames(seconds: float, frame_rate: float = 24.0) -> int:
    """秒 → MiniMax H3 17k+5 帧网格（vendor 同一实现，保证时间轴与执行器一致）。"""
    try:
        from .vendor.h3_director.director.fl2v_timeline import _duration_to_minimax_frames

        return int(_duration_to_minimax_frames(float(seconds), float(frame_rate)))
    except Exception:  # noqa: BLE001 - 无 torch 的本地测试环境：同公式本地实现
        a = max(0.1, float(seconds or 0.1))
        rate = max(1.0, float(frame_rate or 24.0))
        n = max(5, int(round(a * rate)))
        return n + ((5 - (n % 17)) % 17)


def snap_context_frames(raw: Any) -> int:
    """把用户输入吸附到 5/22/39/56 档位（vendor snap_context_frames 同一算法）。"""
    try:
        from .vendor.h3_director.director.h3_motion_context import (
            snap_context_frames as _snap,
        )

        return int(_snap(raw))
    except Exception:  # noqa: BLE001 - 无 torch 的本地测试环境：同算法本地实现
        try:
            n = int(raw or DEFAULT_CONTEXT_FRAMES)
        except (TypeError, ValueError):
            n = DEFAULT_CONTEXT_FRAMES
        return int(min(CONTEXT_FRAME_CHOICES, key=lambda g: (abs(g - n), -g)))


def install_audio_context_frames(frames: int) -> dict:
    """音频上下文帧数（默认 24）。

    vendor 的 executors 以模块级常量 `DEFAULT_AUDIO_CONTEXT_FRAMES` 传入
    motion-context；这里做**运行时**配置注入（不修改 vendor 源文件），
    并把实际生效值写回审计。返回 {"applied": n, "modules": [...], "ok": bool}。
    """
    n = max(0, min(60, int(frames)))
    touched: list[str] = []
    errors: list[str] = []
    modules = (
        "bettercomfyui_comfy.vendor.h3_director.director.h3_motion_context",
        "bettercomfyui_comfy.vendor.h3_director.director.h3_latent_continue",
        "bettercomfyui_comfy.vendor.h3_director.director.executor_core",
    )
    for name in modules:
        try:
            mod = __import__(name, fromlist=["*"])
            if hasattr(mod, "DEFAULT_AUDIO_CONTEXT_FRAMES"):
                setattr(mod, "DEFAULT_AUDIO_CONTEXT_FRAMES", n)
                touched.append(name.rsplit(".", 1)[-1])
        except Exception as exc:  # noqa: BLE001 - 没装 comfy 的本地测试环境
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return {"applied": n, "modules": touched, "ok": bool(touched), "errors": errors}


def _input_dir() -> Path | None:
    try:
        import folder_paths  # type: ignore

        return Path(folder_paths.get_input_directory())
    except Exception:  # noqa: BLE001 - 本地测试/文档环境
        env = os.environ.get("BETTERCOMFYUI_INPUT_DIR", "").strip()
        if env:
            return Path(env)
        return None


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _b64_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "png")
    return f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")


def register_input_file(path: str | Path, *, subdir: str = "bcu_onestop") -> dict:
    """把本地文件登记到 ComfyUI input 目录；返回 timeline 引用 dict。

    优先复制成相对路径（vendor 全部 loader 都按 input 目录解析；缓存/预览都正常）；
    若 input 目录不可写（权限/测试机），退回 base64 内嵌（vendor `_load_refs` 支持
    `imageB64`），保证远端仍能跑。绝不因为登记失败而静默丢参考素材。
    """
    src = Path(str(path)).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"参考文件不存在：{src}")
    safe = "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in src.name)[:48]
    rel_name = f"{_sha(src)}_{safe}"
    out_dir = _input_dir()
    errors: list[str] = []
    if out_dir is not None:
        try:
            target_dir = out_dir / subdir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / rel_name
            if not target.is_file():
                shutil.copy2(src, target)
            return {
                "kind": "file",
                "rel": f"{subdir}/{rel_name}",
                "fileName": target.name,
                "subfolder": subdir,
                "type": "input",
                "absolute": str(target),
                "source": str(src),
                "errors": errors,
            }
        except Exception as exc:  # noqa: BLE001 - 回退 base64，但把原因带出去
            errors.append(f"{type(exc).__name__}: {exc}")
    # 回退：base64（图片）；非图片则抛错（视频/音频必须能落盘）
    suffix = src.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return {
            "kind": "b64",
            "b64": _b64_data_uri(src),
            "fileName": src.name,
            "rel": "",
            "source": str(src),
            "errors": errors,
        }
    raise RuntimeError(f"无法把参考文件登记进 ComfyUI input 目录：{src}（{errors}）")


def _apply_common(dst: dict, info: dict) -> dict:
    for key in ("fileName", "subfolder", "type"):
        if info.get(key):
            dst[key] = info[key]
    return dst


def image_ref(path: str | Path, *, index: int = 0) -> dict:
    """参考图引用（r2v `refs[]` / i2v `genImage` / fl2v `startImage`...）。"""
    info = register_input_file(path)
    ref: dict[str, Any] = {"index": int(index)}
    if info["kind"] == "file":
        ref["imageFile"] = info["rel"]
    else:
        ref["imageB64"] = info["b64"]
    _apply_common(ref, info)
    return ref


def video_ref(path: str | Path, *, index: int = 0) -> dict:
    """参考视频引用（r2v `refVideos[]` / v2v `videoClips[]`）。"""
    info = register_input_file(path)
    ref: dict[str, Any] = {"index": int(index), "videoFile": info["rel"]}
    _apply_common(ref, info)
    return ref


def audio_ref(path: str | Path, *, index: int = 0) -> dict:
    """参考音频引用（r2v `refAudios[]`）。"""
    info = register_input_file(path)
    ref: dict[str, Any] = {"index": int(index), "audioFile": info["rel"]}
    _apply_common(ref, info)
    return ref


# ---------------------------------------------------------------------------
# 时间轴构造（timeline v5：与 director/pack.py 的 segment 结构一致）
# ---------------------------------------------------------------------------
def output_config(
    *,
    width: int,
    height: int,
    ref_max_size: int,
    export_mode: str = "all",
    audio_mode: str = "generate",
    max_export_frames: int = 0,
    ref_image_size: str = "match",
    continuity_enabled: bool = True,
    continuity_overlap_frames: int = DEFAULT_CONTEXT_FRAMES,
    continuity_mode: str = "guide",
    continuity_redraw: float = 0.10,
    continuity_keep_tail: bool = True,
) -> dict:
    """导演台 output 面板（含全部连续性开关，vendor 原生解析）。"""
    mode = str(continuity_mode or "guide").strip().lower()
    if mode not in ("guide", "continue"):
        mode = "guide"
    audio = str(audio_mode or "generate").strip().lower()
    if audio not in ("generate", "source", "mute"):
        audio = "generate"
    export = str(export_mode or "all").strip().lower()
    if export not in ("all", "segments"):
        export = "all"
    return {
        "mode": "fixed",
        "longEdge": int(ref_max_size),
        "width": int(width),
        "height": int(height),
        "maxExportFrames": max(0, int(max_export_frames)),
        "exportMode": export,
        "audioMode": audio,
        "refImageSize": str(ref_image_size or "match"),
        "continuityEnabled": bool(continuity_enabled),
        "continuityOverlapFrames": int(snap_context_frames(continuity_overlap_frames)),
        "continuityMode": mode,
        "continuityRedraw": float(max(0.0, min(1.0, float(continuity_redraw)))),
        "continuityKeepTail": bool(continuity_keep_tail),
    }


def _segment_row(
    *,
    index: int,
    start: int,
    frames: int,
    duration_sec: float,
    prompt: str,
    task_type: str,
    negative_prompt: str = "",
    refs: list[dict] | None = None,
    ref_audios: list[dict] | None = None,
    ref_videos: list[dict] | None = None,
    gen_image: dict | None = None,
    start_image: dict | None = None,
    end_image: dict | None = None,
    continuity_from_prev: bool = True,
    ref_image_size: str = "match",
) -> dict:
    row: dict[str, Any] = {
        "id": f"s{index}",
        "start": int(start),
        "length": int(frames),
        "frameCount": int(frames),
        "durationSec": float(duration_sec),
        "prompt": str(prompt or ""),
        "negativePrompt": str(negative_prompt or ""),
        "taskType": str(task_type or ""),
        "refs": list(refs or []),
        "refAudios": list(ref_audios or []),
        "refVideos": list(ref_videos or []),
        "continuityFromPrev": bool(continuity_from_prev),
        "refImageSize": str(ref_image_size or "match"),
        "loras": [],
        "genImage": dict(gen_image) if gen_image else {"imageFile": ""},
        "imageFile": (gen_image or {}).get("imageFile", "") if gen_image else "",
        "startImage": dict(start_image) if start_image else None,
        "endImage": dict(end_image) if end_image else None,
    }
    return row


def build_timeline_json(
    *,
    task_type: str,
    segments: list[dict] | None,
    global_prompt: str = "",
    width: int = 864,
    height: int = 480,
    frame_rate: float = 24.0,
    ref_max_size: int = 864,
    output: dict | None = None,
    negative_prompt: str = "",
    source_video: dict | None = None,
    global_refs: list[dict] | None = None,
    global_ref_audios: list[dict] | None = None,
) -> str:
    """构造完整 timeline_data JSON（导演台原生 schema）。

    segments: 每项支持
      {prompt, duration_sec, task_type?, negative_prompt?, refs[], refAudios[],
       refVideos[], genImage(ref), startImage(ref), endImage(ref), continuityFromPrev}
    """
    task_key = resolve_task_key(task_type)
    # 与 vendor director/pack.py 相同的 timelineMode 判定
    if task_key == "fl2v":
        timeline_mode = "fl2v"
    elif task_key in ("v2v", "rv2v"):
        timeline_mode = "video"
    else:
        timeline_mode = "prompt_batch"

    out = dict(output or output_config(width=width, height=height, ref_max_size=ref_max_size))
    out.setdefault("mode", "fixed")
    out["width"] = int(out.get("width") or width)
    out["height"] = int(out.get("height") or height)
    out["longEdge"] = int(out.get("longEdge") or ref_max_size)

    fps = float(frame_rate or 24.0)
    rows: list[dict] = []
    shots: list[dict] = []
    cursor = 0
    for idx, seg in enumerate(segments or []):
        seg = dict(seg or {})
        duration = float(seg.get("duration_sec") or 5.167)
        frames = int(
            seg.get("frameCount") or seg.get("frames") or duration_to_frames(duration, fps)
        )
        start = int(seg["start"]) if seg.get("start") is not None else cursor
        row = _segment_row(
            index=idx, start=start, frames=frames, duration_sec=duration,
            prompt=str(seg.get("prompt") or ""),
            task_type=str(seg.get("task_type") or task_type),
            negative_prompt=str(seg.get("negative_prompt") or negative_prompt),
            refs=seg.get("refs"),
            ref_audios=seg.get("refAudios"),
            ref_videos=seg.get("refVideos"),
            gen_image=seg.get("genImage"),
            start_image=seg.get("startImage"),
            end_image=seg.get("endImage"),
            continuity_from_prev=bool(seg.get("continuityFromPrev", True)),
        )
        rows.append(row)
        cursor = start + frames
        if timeline_mode == "fl2v":
            shots.append(
                {
                    "id": row["id"],
                    "durationSec": row["durationSec"],
                    "prompt": row["prompt"],
                    "negativePrompt": row["negativePrompt"],
                    "continuityFromPrev": row["continuityFromPrev"],
                    "loras": [],
                    "startImage": seg.get("startImage"),
                    "endImage": seg.get("endImage"),
                }
            )

    if not rows:
        frames = duration_to_frames(5.167, fps)
        rows = [
            _segment_row(
                index=0, start=0, frames=frames, duration_sec=5.167,
                prompt=global_prompt, task_type=task_type,
                negative_prompt=negative_prompt,
            )
        ]
        cursor = frames

    total_frames = int(rows[-1]["start"] + rows[-1]["length"])
    video_block = {
        "fileName": "", "videoFile": "", "subfolder": "", "type": "input",
        "frames": [], "frameMap": [],
    }
    video_clips: list[dict] = []
    if isinstance(source_video, dict) and source_video.get("rel"):
        block = {
            "fileName": source_video.get("fileName") or "",
            "videoFile": source_video["rel"],
            "subfolder": source_video.get("subfolder") or "",
            "type": "input",
            "frames": [],
            "frameMap": [],
        }
        video_block = block
        video_clips = [dict(block)]

    timeline: dict[str, Any] = {
        "version": 5,
        "timelineMode": timeline_mode,
        "editMode": "global" if timeline_mode == "video" else "segment",
        "frameRate": fps,
        "totalFrames": total_frames,
        "width": int(width),
        "height": int(height),
        "refMaxSize": int(ref_max_size),
        "output": out,
        "videoClips": video_clips,
        "video": video_block,
        "global": {
            "taskType": task_type,
            "prompt": str(global_prompt or ""),
            "refs": list(global_refs or []),
            "refAudios": list(global_ref_audios or []),
            "refVideos": [],
            "referenceVideo": {},
            "continuousReference": False,
            "loras": [],
        },
        "segments": rows,
        "runSelectEnabled": False,
        "runSelection": [],
    }
    if timeline_mode == "fl2v":
        timeline["shots"] = shots
        timeline["keyframes"] = []
    return json.dumps(timeline, ensure_ascii=False)


def normalize_timeline_json(raw: str) -> dict:
    """用户自定义 timeline_data：解析 + 必填字段兜底 + 规则校验（失败要能定位）。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("时间轴JSON 为空")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"时间轴JSON 不是合法 JSON（{exc.msg}，line {exc.lineno} col {exc.colno}）"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"时间轴JSON 顶层必须是 object，实际 {type(data).__name__}")
    data.setdefault("version", 5)
    data.setdefault("frameRate", 24)
    data.setdefault("global", {})
    if not isinstance(data.get("global"), dict):
        data["global"] = {}
    data.setdefault("output", {})
    if not isinstance(data.get("output"), dict):
        data["output"] = {}
    segs = data.get("segments")
    if not isinstance(segs, list):
        segs = []
    if not segs:
        raise ValueError("时间轴JSON 至少要有一个 segments[] 分段")
    fps = float(data.get("frameRate") or 24)
    cursor = 0
    fixed: list[dict] = []
    for idx, seg in enumerate(segs):
        if not isinstance(seg, dict):
            raise ValueError(f"segments[{idx}] 不是 object")
        row = dict(seg)
        row.setdefault("id", f"s{idx}")
        row.setdefault("prompt", "")
        frames = int(row.get("length") or row.get("frameCount") or row.get("frames") or 0)
        if frames <= 0:
            frames = duration_to_frames(float(row.get("durationSec") or 5.167), fps)
        row["length"] = frames
        row.setdefault("frameCount", frames)
        if "start" not in row:
            row["start"] = cursor
        cursor = int(row["start"]) + frames
        row.setdefault("taskType", "")
        row.setdefault("refs", [])
        fixed.append(row)
    data["segments"] = fixed
    data.setdefault("totalFrames", cursor)
    out = data["output"]
    out.setdefault("mode", "fixed")
    out.setdefault("exportMode", "all")
    out.setdefault("audioMode", "generate")
    out.setdefault("refImageSize", "match")
    out.setdefault("maxExportFrames", 0)
    out.setdefault("continuityEnabled", False)
    out.setdefault("continuityOverlapFrames", DEFAULT_CONTEXT_FRAMES)
    out.setdefault("continuityMode", "guide")
    out.setdefault("continuityRedraw", 0.10)
    out.setdefault("continuityKeepTail", True)
    data.setdefault("timelineMode", "")
    data.setdefault("editMode", "global")
    data.setdefault("videoClips", [])
    data.setdefault("video", {})
    return data


def timeline_summary(timeline_json: str) -> dict:
    """时间轴摘要（审计/报告用；不执行、不加载模型）。"""
    data = json.loads(timeline_json)
    out = data.get("output") or {}
    segs = data.get("segments") or []
    seg_rows = []
    for seg in segs:
        seg_rows.append(
            {
                "id": seg.get("id"),
                "start": seg.get("start"),
                "length": seg.get("length"),
                "durationSec": seg.get("durationSec"),
                "taskType": seg.get("taskType"),
                "refs": len(seg.get("refs") or []),
                "refAudios": len(seg.get("refAudios") or []),
                "refVideos": len(seg.get("refVideos") or []),
                "genImage": bool(
                    (seg.get("genImage") or {}).get("imageFile")
                    or (seg.get("genImage") or {}).get("imageB64")
                ),
                "startImage": bool(seg.get("startImage")),
                "endImage": bool(seg.get("endImage")),
                "continuityFromPrev": seg.get("continuityFromPrev"),
                "prompt": str(seg.get("prompt") or "")[:120],
            }
        )
    return {
        "version": data.get("version"),
        "timelineMode": data.get("timelineMode"),
        "editMode": data.get("editMode"),
        "frameRate": data.get("frameRate"),
        "totalFrames": data.get("totalFrames"),
        "width": out.get("width"),
        "height": out.get("height"),
        "refMaxSize": data.get("refMaxSize"),
        "output": {
            "mode": out.get("mode"),
            "exportMode": out.get("exportMode"),
            "audioMode": out.get("audioMode"),
            "refImageSize": out.get("refImageSize"),
            "maxExportFrames": out.get("maxExportFrames"),
            "continuityEnabled": out.get("continuityEnabled"),
            "continuityOverlapFrames": out.get("continuityOverlapFrames"),
            "continuityMode": out.get("continuityMode"),
            "continuityRedraw": out.get("continuityRedraw"),
            "continuityKeepTail": out.get("continuityKeepTail"),
        },
        "globalTaskType": (data.get("global") or {}).get("taskType"),
        "globalPrompt": str((data.get("global") or {}).get("prompt") or "")[:160],
        "videoClips": len(data.get("videoClips") or []),
        "segmentCount": len(segs),
        "segments": seg_rows,
        "shots": len(data.get("shots") or []),
    }


# ---------------------------------------------------------------------------
# 可选包（SelfLift / Refine / FaceRefine / SemanticBridge）——单节点内部开关
# ---------------------------------------------------------------------------
def build_selflift_pack(
    enabled: bool,
    *,
    highres_steps: int = 8,
    transition_step: int = 2,
    lowres_scale: float = 0.50,
    split_mode: str = "highres_steps",
    sampler_mode: str = "euler",
    native_low_carry: bool = True,
    latent_upsample: str = "bilinear",
) -> dict | None:
    """SelfLift 渐进一采包（不连线，节点内部直接生成；未启用返回 None）。"""
    if not enabled:
        return None
    from .vendor.h3_director.director.selflift.pack import pack_selflift

    return pack_selflift(
        split_mode=split_mode,
        highres_steps=int(highres_steps),
        transition_step=int(transition_step),
        lowres_scale=float(lowres_scale),
        sampler_mode=sampler_mode,
        native_low_carry=bool(native_low_carry),
        latent_upsample=latent_upsample,
        model_hires=None,
    )


def build_refine_pack(
    enabled: bool,
    *,
    mode: str = "refine",
    passes: int = 1,
    megapixels: float = 2.0,
    upscale_method: str = "h3_latent",
    sampler: str = "",
    confirm_first_pass: bool = False,
    upscale_model: Any = None,
    sample_model: Any = None,
) -> dict | None:
    """Refine 二采包（同尺寸 refine / 放大后二采）。未启用返回 None。"""
    if not enabled:
        return None
    from .vendor.h3_director.director.refine_pack import pack_refine

    return pack_refine(
        mode=mode,
        passes=int(passes),
        megapixels=float(megapixels),
        upscale_method=upscale_method,
        sampler=sampler,
        sample_model=sample_model,
        upscale_model=upscale_model,
        confirm_first_pass=bool(confirm_first_pass),
    )


def build_face_refine_pack(
    enabled: bool,
    *,
    detector: str = "",
    confidence: float = 0.35,
    crop_factor: float = 2.5,
    denoise: float = 0.40,
    steps: int = 8,
) -> dict | None:
    """FaceRefine 修脸包。未启用返回 None。"""
    if not enabled:
        return None
    from .vendor.h3_director.director.face_refine.pack import (
        detector_choices,
        pack_face_refine,
    )

    choices = detector_choices()
    det = str(detector or "").strip()
    if det not in choices:
        det = choices[0] if choices else ""
    return pack_face_refine(
        detector=det,
        confidence=float(confidence),
        crop_factor=float(crop_factor),
        denoise=float(denoise),
        steps=int(steps),
    )


def build_semantic_bridge_pack(
    enabled: bool,
    *,
    adapter: str = "",
    alpha: float = 0.15,
    magnitude_match: bool = True,
) -> dict | None:
    """SemanticBridge cond-token 学生包（需用户自备 adapter 权重）。未启用/无权重返回 None。"""
    if not enabled:
        return None
    name = str(adapter or "").strip()
    if not name:
        return None
    from .vendor.h3_director.director.semantic_bridge import pack_semantic_bridge

    return pack_semantic_bridge(
        adapter=name, alpha=float(alpha), magnitude_match=bool(magnitude_match)
    )


def optional_pack_summary(
    *,
    selflift: dict | None,
    refine: dict | None,
    face_refine: dict | None,
    semantic_bridge: dict | None,
) -> dict:
    return {
        "selflift": bool(selflift and selflift.get("enabled")),
        "selflift_params": selflift,
        "refine": bool(refine and refine.get("enabled")),
        "refine_params": refine,
        "face_refine": bool(face_refine and face_refine.get("enabled")),
        "face_refine_params": face_refine,
        "semantic_bridge": bool(semantic_bridge and semantic_bridge.get("enabled")),
        "semantic_bridge_params": semantic_bridge,
    }


# ---------------------------------------------------------------------------
# 段间连续性证据（从导演台 report 文本提取，供报告/验收勾选）
# ---------------------------------------------------------------------------
def continuity_evidence(report: str) -> dict:
    """解析导演台 report，证明「段间连续 ON + motion context Nf + pin 了哪些段」。

    这些文本全部来自 vendor `director/plan.py::plan_summary` 与
    `executor_core` 的段进度行，是**执行器自己写的实证**，不是本模块编的。
    """
    text = str(report or "")
    lines = [line for line in text.splitlines() if line.strip()]
    on = "Segment continuity: ON" in text
    off_explicit = "Segment continuity: OFF" in text
    overlap = None
    mode = None
    keep_tail = None
    match = re.search(r"Segment continuity: ON \(([a-z_+]+) motion context (\d+)f([^)]*)\)", text)
    if match:
        mode = match.group(1)
        overlap = int(match.group(2))
        keep_tail = "keep full" in (match.group(3) or "")
    pinned: list[int] = []
    match = re.search(r"Pin from prev:[ \t]*#([0-9][0-9, \t#]*)", text)
    if match:
        pinned = sorted({int(x) for x in re.findall(r"\d+", match.group(1))})
    hard_cut: list[int] = []
    match = re.search(r"Hard cut \(per-segment off\):[ \t]*#([0-9][0-9, \t#]*)", text)
    if match:
        hard_cut = sorted({int(x) for x in re.findall(r"\d+", match.group(1))})
    pin_lines = [
        line.strip()
        for line in lines
        if ("pin←prev" in line or "hard cut" in line) and line.strip().startswith("#")
    ]
    skip_lines = [
        line.strip()
        for line in lines
        if "上一段无有效缓存" in line or "已跳过段间引导" in line or "skipped" in line
    ]
    timing_lines = [
        line.strip()
        for line in lines
        if line.strip().startswith("Segment ") and "timing:" in line
    ]
    return {
        "continuity_enabled_in_plan": bool(on),
        "continuity_off_explicit": bool(off_explicit),
        "mode": mode,
        "overlap_frames": overlap,
        "keep_tail": keep_tail,
        "pinned_segments": pinned,
        "hard_cut_segments": hard_cut,
        "pin_lines": pin_lines[:20],
        "skip_notes": skip_lines[:10],
        "segment_timing_lines": timing_lines[:20],
        "evidence_lines": [
            line.strip()
            for line in lines
            if "Segment continuity" in line or "Pin from prev" in line
        ][:10],
    }


def output_config_summary(output: dict) -> dict:
    keys = (
        "mode", "exportMode", "audioMode", "refImageSize", "maxExportFrames",
        "continuityEnabled", "continuityOverlapFrames", "continuityMode",
        "continuityRedraw", "continuityKeepTail", "width", "height", "longEdge",
    )
    return {key: output.get(key) for key in keys}
