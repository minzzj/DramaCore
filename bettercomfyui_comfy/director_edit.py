"""剧核 · 单节点成片后处理（SG6）：拼接/转场/字幕/水印/封面/格式/元数据（OUT1–OUT4）。

全部走本项目 `bettercomfyui/pipeline/core.py` 的已验证能力（splice/encode/write_output），
不自建一套编码器：
- 拼接：`core.splice`（concat demuxer 流复制）；选了「淡入淡出」时用 ffmpeg `xfade`/
  `acrossfade` 做**段间交叉淡化**（0.5s），再交给 pipeline 封装。
- 字幕：节点把各段提示词转 SRT → `core.encode(subtitle_file=...)` 真烧录（不是假开关）。
- 水印：节点用 PIL 生成文字水印 PNG → `core.encode(watermark_file=..., watermark_position=...)`
  真叠加。
- 格式/编码/CRF：`core.encode` 支持 MP4/MOV(H.264/H.265)；WebM（VP9/Opus）本模块用 ffmpeg
  直接实现（pipeline.encode 只登记 H.264/H.265，不伪装支持）。
- 封面/元数据：`core.write_output`（OUT1 目录、OUT2 命名、OUT3 MovieMetadata、OUT4 首帧封面）。
- 选「走 Finalize 剪辑」且没有高级选项时，仍走 `BetterComfyUIFinalize`（保留自检 JSON），
  随后用 `write_output` 补齐 OUT3/OUT4，两条路径都不会静默降级。

失败口径：任何一步失败都抛异常或写入 `warnings`，绝不假装字幕/水印/转场已生效。
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_FADE_SECONDS = 0.5
DEFAULT_WATERMARK_TEXT = "剧核 DramaCore"

_FORMAT_MAP = {
    "mp4": "MP4",
    "mov": "MOV",
    "webm": "WebM",
}
_CONTAINER_EXT = {"MP4": ".mp4", "MOV": ".mov", "WebM": ".webm"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _ffmpeg() -> str:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    return _ffmpeg_exe()


def _run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{what}失败（ffmpeg exit={proc.returncode}）："
                           f"{(proc.stderr or '')[-800:]}")
    return proc


def probe_media(path: Path | str) -> dict:
    """探媒体信息（走 pipeline.probe_media；失败给最小兜底并带 error）。"""
    try:
        from bettercomfyui.pipeline.core import probe_media as _probe

        data = _probe(Path(path))
        if isinstance(data, dict):
            return data
        return {"raw": data}
    except Exception as exc:  # noqa: BLE001 - 探测失败要可见
        return {"error": f"{type(exc).__name__}: {exc}"}


def resolve_format(label: str) -> str:
    """节点「输出格式」标签 → pipeline encode.format。未知值明确报错（不静默降级）。"""
    text = str(label or "").strip().lower()
    for key, value in _FORMAT_MAP.items():
        if text.startswith(key):
            return value
    raise ValueError(f"未实现的输出格式：{label!r}（支持 mp4/mov/webm）")


def container_suffix(fmt: str) -> str:
    return _CONTAINER_EXT.get(str(fmt), ".mp4")


# ---------------------------------------------------------------------------
# 字幕 / 水印素材生成
# ---------------------------------------------------------------------------
def build_subtitle_entries(
    prompts: list[str],
    *,
    segment_seconds: float,
    start_offset_s: float = 0.0,
    max_chars: int = 42,
) -> list[dict]:
    """按「每段一条字幕」生成时间条目（提示词截断到一行，便于阅读）。"""
    entries: list[dict] = []
    cursor = max(0.0, float(start_offset_s))
    for idx, prompt in enumerate(prompts):
        text = " ".join(str(prompt or "").split())
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        begin = cursor
        end = cursor + max(0.5, float(segment_seconds))
        entries.append({"index": idx + 1, "start": begin, "end": end, "text": text})
        cursor = end
    return entries


def write_srt(entries: list[dict], path: Path | str) -> Path:
    """写 SRT（UTF-8；时间格式 HH:MM:SS,mmm）。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    def _ts(seconds: float) -> str:
        total_ms = int(round(max(0.0, float(seconds)) * 1000))
        h, rem = divmod(total_ms, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    for idx, entry in enumerate(entries, start=1):
        lines.append(str(idx))
        lines.append(f"{_ts(entry['start'])} --> {_ts(entry['end'])}")
        lines.append(str(entry.get("text") or ""))
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/arial.ttf",
)


def write_watermark_png(text: str, path: Path | str, *, width: int = 560,
                        height: int = 140, font_size: int = 44) -> Path:
    """生成透明背景文字水印 PNG（白字 + 轻描边，四角叠加用）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"缺少 Pillow，无法生成水印图：{type(exc).__name__}: {exc}") from exc

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = None
    for font_path in _FONT_CANDIDATES:
        try:
            if Path(font_path).is_file():
                font = ImageFont.truetype(font_path, int(font_size))
                break
        except Exception:  # noqa: BLE001 - 换下一个字体
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size=int(font_size))
        except Exception:  # noqa: BLE001 - 老 Pillow
            font = ImageFont.load_default()
    content = " ".join(str(text or DEFAULT_WATERMARK_TEXT).split()) or DEFAULT_WATERMARK_TEXT
    # 描边 + 填充，保证亮背景也能看清
    for dx, dy in ((-2, -2), (2, -2), (-2, 2), (2, 2)):
        draw.text((10 + dx, 38 + dy), content, font=font, fill=(0, 0, 0, 170))
    draw.text((10, 38), content, font=font, fill=(255, 255, 255, 230))
    # 右下角放一个小圆点标识，避免整图透明被误判为空
    draw.ellipse((width - 26, 12, width - 12, 26), fill=(255, 215, 0, 210))
    canvas.save(out)
    return out


# ---------------------------------------------------------------------------
# 拼接：流复制（pipeline.splice）/ 交叉淡化（ffmpeg xfade）
# ---------------------------------------------------------------------------
def splice_segments(seg_files: list[Path | str], *, workdir: Path, fade_seconds: float = 0.0,
                    warnings: list[str] | None = None) -> Path:
    """按顺序拼接段视频。fade_seconds>0 → 段间交叉淡化；否则 pipeline.splice 流复制。"""
    warnings = warnings if warnings is not None else []
    files = [Path(f) for f in seg_files]
    if not files:
        raise ValueError("拼接输入为空：没有段视频")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if len(files) == 1:
        return files[0]
    if fade_seconds and float(fade_seconds) > 0:
        try:
            out = workdir / "spliced_fade.mp4"
            _crossfade(files, out, float(fade_seconds), warnings=warnings)
            return out
        except Exception as exc:  # noqa: BLE001 - 转场失败要可见，退回流复制
            warnings.append(
                f"⚠️ 淡入淡出转场失败，已退回流复制拼接（不冒充转场成功）："
                f"{type(exc).__name__}: {exc}")
    from bettercomfyui.pipeline.core import SpliceInput, splice

    inputs = [
        SpliceInput(segment_id=f"seg{i + 1}", segment_version="v1", video_path=file)
        for i, file in enumerate(files)
    ]
    return splice(inputs, workdir=workdir, out_path=workdir / "spliced.mp4")


def _crossfade(files: list[Path], out_path: Path, fade: float, *,
               warnings: list[str]) -> Path:
    """ffmpeg xfade + acrossfade 段间交叉淡化（所有段都有音轨才做音频淡化）。"""
    infos = [probe_media(f) for f in files]
    durations = []
    for file, info in zip(files, infos):
        dur = info.get("duration_s") or info.get("duration")
        if not dur:
            raise RuntimeError("无法读取段时长，不能做转场：" + str(file))
        durations.append(float(dur))
    has_audio = [bool(info.get("has_audio")) for info in infos]
    use_audio = all(has_audio)
    if not use_audio:
        warnings.append("⚠️ 转场拼接：存在无音轨段 → 输出丢弃音轨（如实记录）")
    cmd = [_ffmpeg(), "-y", "-v", "error"]
    for file in files:
        cmd += ["-i", str(file)]
    parts: list[str] = []
    # 视频 xfade 链
    prev = "[0:v]"
    offset = 0.0
    for i in range(len(files) - 1):
        offset += durations[i] - fade
        if offset <= 0:
            raise RuntimeError(
                "段 " + str(i + 1) + " 太短（" + f"{durations[i]:.3f}" +
                "s）做不了 " + f"{fade}" + "s 转场")
        label = f"[vx{i + 1}]"
        parts.append(
            f"{prev}[{i + 1}:v]xfade=transition=fade:duration={fade:.3f}:"
            f"offset={offset:.3f}{label}")
        prev = label
    video_out = prev
    audio_map: str | None = None
    if use_audio:
        aprev = "[0:a]"
        for i in range(len(files) - 1):
            label = f"[ax{i + 1}]"
            parts.append(f"{aprev}[{i + 1}:a]acrossfade=d={fade:.3f}:c1=tri:c2=tri{label}")
            aprev = label
        audio_map = aprev
    cmd += ["-filter_complex", ";".join(parts), "-map", video_out]
    if audio_map:
        cmd += ["-map", audio_map]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path),
    ]
    _run(cmd, what="交叉淡化转场")
    if not out_path.is_file():
        raise RuntimeError("交叉淡化转场结束但无输出文件")
    return out_path


# ---------------------------------------------------------------------------
# 封装 + 输出（pipeline.encode / write_output）
# ---------------------------------------------------------------------------
def _load_cfg():
    """读项目配置（config/default.yaml + user.yaml）；repo 根按插件包位置推导。"""
    import os

    from bettercomfyui.config import load_config

    root = Path(os.environ.get("BETTERCOMFYUI_HOME", "") or Path(__file__).resolve().parent.parent)
    return load_config(repo_root=root)


def encode_movie(
    src: Path | str,
    *,
    fmt: str,
    crf: int,
    subtitle_file: Path | None,
    watermark_file: Path | None,
    workdir: Path,
    audio_join: str = "auto",
    warnings: list[str] | None = None,
) -> tuple[Path, dict]:
    """按格式封装：MP4/MOV 走 pipeline.core.encode；WebM 用 ffmpeg VP9/Opus 实现。"""
    warnings = warnings if warnings is not None else []
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    fmt = str(fmt)
    if fmt in ("MP4", "MOV"):
        from bettercomfyui.pipeline.core import encode as pipeline_encode

        cfg = _load_cfg()
        cfg = copy.deepcopy(cfg)
        cfg.encode.format = fmt
        cfg.encode.codec = "H.264"
        cfg.encode.crf = int(crf)
        cfg.encode.transition = "none"  # 段间转场已在上一步完成，避免双重处理
        cfg.encode.subtitle = bool(subtitle_file is not None)
        cfg.encode.watermark = bool(watermark_file is not None)
        cfg.encode.audio_join = str(audio_join or "auto")
        out = workdir / f"encoded{container_suffix(fmt)}"
        path = pipeline_encode(
            src, cfg, out_path=out,
            subtitle_file=subtitle_file, watermark_file=watermark_file,
            watermark_position="br")
        return Path(path), {"engine": "pipeline.core.encode", "format": fmt}
    if fmt == "WebM":
        out = workdir / "encoded.webm"
        vf_parts = []
        if subtitle_file is not None:
            escaped = str(subtitle_file).replace("\\", "/").replace(":", "\\:")
            vf_parts.append(f"subtitles='{escaped}'")
        if watermark_file is not None:
            vf_parts.append(f"movie={watermark_file} [wm];[in][wm]overlay=W-w-16:H-h-16[out]")
        cmd = [_ffmpeg(), "-y", "-v", "error", "-i", str(src)]
        if watermark_file is not None:
            cmd += ["-i", str(watermark_file)]
        cmd += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(max(0, min(63, int(crf)))),
                "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"]
        if subtitle_file is not None and watermark_file is None:
            cmd += ["-vf", vf_parts[0]]
        elif watermark_file is not None and subtitle_file is None:
            cmd += ["-filter_complex",
                    "[0:v][1:v]overlay=W-w-16:H-h-16[out]", "-map", "[out]",
                    "-map", "0:a?"]
        elif subtitle_file is not None and watermark_file is not None:
            warnings.append("⚠️ WebM 同时字幕+水印：当前实现只烧录水印（字幕未叠加，如实记录）")
            cmd += ["-filter_complex",
                    "[0:v][1:v]overlay=W-w-16:H-h-16[out]", "-map", "[out]",
                    "-map", "0:a?"]
        cmd += [str(out)]
        _run(cmd, what="WebM(VP9/Opus) 封装")
        return out, {"engine": "ffmpeg libvpx-vp9", "format": "WebM",
                     "warnings": list(warnings[-2:])}
    raise ValueError(f"未实现的输出格式：{fmt}")


def write_movie_output(
    movie: Path | str,
    *,
    project_id: str,
    project_display_name: str,
    project_dir: Path,
    script_id: str,
    manifest: list[dict] | None,
    metadata_extra: dict | None = None,
    provenance: dict | None = None,
    cover: bool = True,
) -> dict:
    """OUT1–OUT4：调用 `pipeline.core.write_output` 落目录/命名/MovieMetadata/封面。"""
    from bettercomfyui.models.entities import MovieMetadata

    from .director_longform import manifest_to_metadata_items

    cfg = _load_cfg()
    cfg = copy.deepcopy(cfg)
    cfg.encode.cover = "first_frame" if cover else "none"
    metadata = MovieMetadata(
        project_id=project_id,
        project_display_name=project_display_name or project_id,
        script_id=script_id or None,
        workflow_version=str((metadata_extra or {}).get("workflow_version") or "round46"),
        segments=manifest_to_metadata_items(manifest or []),
        extra=dict(metadata_extra or {}),
        generation_provenance=dict(provenance or {}),
    )
    from bettercomfyui.pipeline.core import write_output

    result = write_output(
        Path(movie),
        cfg,
        project_dir=Path(project_dir),
        metadata=metadata,
        workdir=Path(project_dir) / "output",
    )
    return result


# ---------------------------------------------------------------------------
# 一键收尾
# ---------------------------------------------------------------------------
def finish_movie(
    *,
    seg_files: list[Path | str],
    work_dir: Path | str,
    project_dir: Path | str,
    project_id: str,
    project_display_name: str,
    output_filename: str,
    options: dict,
    manifest: list[dict] | None,
    metadata_extra: dict | None = None,
    provenance: dict | None = None,
    use_finalize: bool = True,
    audit: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """剪辑收尾（SG6）：Finalize 或 pipeline 拼接封装 + OUT1–OUT4 元数据。

    options: {format_label, crf, transition_label, subtitle_label, watermark_label,
              cover_label, watermark_text, fade_seconds, audio_join}
    """
    audit = audit if audit is not None else []
    warnings = warnings if warnings is not None else []
    work_dir = Path(work_dir)
    project_dir = Path(project_dir)
    files = [Path(f) for f in seg_files]
    report: dict[str, Any] = {"executed": False, "steps": [], "warnings": []}
    fmt = resolve_format(str(options.get("format_label") or "mp4"))
    transition = str(options.get("transition_label") or "无")
    subtitle = not str(options.get("subtitle_label") or "无").startswith("无")
    watermark = not str(options.get("watermark_label") or "无").startswith("无")
    cover = not str(options.get("cover_label") or "无").startswith("无")
    crf = int(options.get("crf") or 18)
    advanced = bool(subtitle or watermark or fmt != "MP4" or not transition.startswith("无"))

    final_source: Path | None = None
    try:
        if use_finalize and not advanced and files:
            from .finalize_nodes import BetterComfyUIFinalize

            fin = BetterComfyUIFinalize().run(
                项目=project_display_name,
                段视频路径列表="\n".join(str(f) for f in files),
                输出文件名=output_filename,
                编码CRF=crf,
                完整性检查="完整扫描",
                计划段数=len(files),
                每段计划时长_秒=float(options.get("segment_seconds") or 0.0),
            )
            values = fin.get("result") if isinstance(fin, dict) else fin
            if not isinstance(values, (list, tuple)) or not values:
                raise RuntimeError(f"Finalize 返回结构异常：{type(fin).__name__}")
            final_source = Path(str(values[0]))
            report["finalize"] = {
                "executed": True,
                "integrity": _safe_json(values[1]) if len(values) > 1 else {},
                "quality": _safe_json(values[2]) if len(values) > 2 else {},
                "audit": str(values[3]) if len(values) > 3 else "",
            }
            report["steps"].append(f"Finalize 拼接封装 → {final_source}")
            audit.append(f"Finalize 剪辑：{len(files)} 段 → {final_source}")
        else:
            fade_seconds = float(options.get("fade_seconds") or DEFAULT_FADE_SECONDS) \
                if not transition.startswith("无") else 0.0
            spliced = splice_segments(files, workdir=work_dir,
                                      fade_seconds=fade_seconds, warnings=warnings)
            report["steps"].append(
                f"拼接 → {spliced}（转场={'淡入淡出' if fade_seconds else '无'}）")
            subtitle_file = None
            if subtitle:
                entries = build_subtitle_entries(
                    list(options.get("prompts") or []),
                    segment_seconds=float(options.get("segment_seconds") or 0.0))
                subtitle_file = write_srt(entries, work_dir / "subtitle.srt")
                report["subtitle_file"] = str(subtitle_file)
                report["steps"].append(f"字幕 → {subtitle_file}（{len(entries)} 条，真烧录）")
            watermark_file = None
            if watermark:
                watermark_file = write_watermark_png(
                    str(options.get("watermark_text") or DEFAULT_WATERMARK_TEXT),
                    work_dir / "watermark.png")
                report["watermark_file"] = str(watermark_file)
                report["steps"].append(f"水印 → {watermark_file}（右下角真叠加）")
            encoded, enc_report = encode_movie(
                spliced, fmt=fmt, crf=crf, subtitle_file=subtitle_file,
                watermark_file=watermark_file, workdir=work_dir,
                audio_join=str(options.get("audio_join") or "auto"), warnings=warnings)
            report["encode"] = enc_report
            report["steps"].append(f"封装 → {encoded}（{fmt}/CRF{crf}）")
            final_source = encoded
        if final_source is None:
            raise RuntimeError("剪辑收尾没有产出成片文件")

        # OUT1–OUT4：目录/命名/MovieMetadata/封面
        out = write_movie_output(
            final_source,
            project_id=project_id,
            project_display_name=project_display_name,
            project_dir=project_dir,
            script_id=str((metadata_extra or {}).get("script_id") or "script"),
            manifest=manifest,
            metadata_extra=metadata_extra,
            provenance=provenance,
            cover=bool(cover),
        )
        final_path = Path(str(out.get("out_path") or final_source))
        report.update({
            "executed": True,
            "final_path": str(final_path),
            "output": {
                "out_path": out.get("out_path"),
                "metadata_path": out.get("metadata_path"),
                "cover_path": out.get("cover_path"),
                "checks": out.get("checks"),
                "content_hash": out.get("content_hash"),
            },
            "cover_requested": cover,
            "warnings": list(warnings),
        })
        if cover:
            audit.append(f"封面：{out.get('cover_path') or '未生成（write_output 未返回封面，如实记录）'}")
        audit.append(f"成片输出（OUT1–OUT3）：{final_path}")
        audit.append(f"元数据：{out.get('metadata_path')}")
        return report
    except Exception as exc:  # noqa: BLE001 - 收尾失败必须抛出，由节点决定降级路径
        report["executed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise


def _safe_json(text: Any) -> Any:
    try:
        return json.loads(str(text))
    except Exception:  # noqa: BLE001
        return str(text)


def simple_options_summary(options: dict) -> dict:
    keys = ("format_label", "crf", "transition_label", "subtitle_label",
            "watermark_label", "cover_label", "watermark_text", "fade_seconds")
    return {key: options.get(key) for key in keys}
