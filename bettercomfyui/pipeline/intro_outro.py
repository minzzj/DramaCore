"""片头 / 片尾模块（ROUND55 专项，离线可测；纯函数 + ffmpeg + PIL）。

用户需求（2026-10-19）：「增加『片头片尾』选择能力，并且导演台优点要具备」。
本模块生成**静态标题卡/片尾卡**短视频（黑/渐变底、居中文字、淡入淡出、**带静音音轨**），
交给剪辑阶段拼在正片前后。不依赖 GPU/网络，只依赖 ffmpeg 与 Pillow。

公共 API::

    generate_intro_outro(
        workdir, *, mode, fmt_label="mp4（H.264）",
        width=1920, height=1088, fps=24,
        intro_seconds=3.0, outro_seconds=3.0,
        title="", subtitle="", credits="", logo_path=None,
        watermark_text="", warnings=None,
        outro_title="", font_path=None,
    ) -> {"intro": Path | None, "outro": Path | None,
          "audit": list[str], "warnings": list[str],
          "mode": str, "format": str}

UI 契约（Lead 将新增节点参数；本模块只提供函数，不改 JS）：
- ``片头片尾``  下拉：``无`` / ``片头`` / ``片尾`` / ``片头+片尾`` → 传 ``mode``
- ``片头文字``  → 传 ``title``（为空 → 占位标题「剧核 DramaCore」，审计记录「占位」）
- ``片尾文字``  → 传 ``outro_title``（为空 → 退回 ``subtitle``；再为空 → 占位「剧终」）
- ``片尾演职员`` → 传 ``credits``（为空 → 占位「剧核 DramaCore 出品」）
- 可选：``watermark_text`` 右下角小字、``logo_path`` 图片水印（PNG/JPG，读不到会告警忽略）

实现口径（**不伪造成功**）：
1. 优先 **ffmpeg drawtext** 渲染文字（Windows/Linux 需指定可用字体，避免 fontconfig 问题）；
   背景用 PIL 生成的渐变底 PNG（Pillow 不可用时退回纯色底）。
2. 字体不可用 / drawtext 失败 → 退回 **PIL 生成整张卡片 PNG 再编码**（审计记录实际路由）。
3. 两条路由都失败（或产物 probe 不合法）→ 对应卡片返回 ``None``，并向 ``warnings`` 写入
   可操作原因；绝不返回一个损坏/空文件冒充成功。
4. 文字为空只使用占位文案并写审计，不静默；mode=无 时不调用 ffmpeg。
"""

from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 枚举 / 常量
# ---------------------------------------------------------------------------
MODE_NONE = "无"
MODE_INTRO = "片头"
MODE_OUTRO = "片尾"
MODE_BOTH = "片头+片尾"
MODE_CHOICES: tuple[str, ...] = (MODE_NONE, MODE_INTRO, MODE_OUTRO, MODE_BOTH)

_MODE_ALIASES = {
    "": MODE_NONE, "无": MODE_NONE, "none": MODE_NONE, "no": MODE_NONE,
    "off": MODE_NONE, "关闭": MODE_NONE,
    "片头": MODE_INTRO, "intro": MODE_INTRO, "title": MODE_INTRO,
    "片尾": MODE_OUTRO, "outro": MODE_OUTRO, "credits": MODE_OUTRO,
    "片头+片尾": MODE_BOTH, "片头片尾": MODE_BOTH, "both": MODE_BOTH,
    "intro+outro": MODE_BOTH, "intro_outro": MODE_BOTH, "all": MODE_BOTH,
}

DEFAULT_TITLE = "剧核 DramaCore"
DEFAULT_OUTRO_TITLE = "剧终"
DEFAULT_CREDITS = "剧核 DramaCore 出品"

_WIDTH_LIMIT = (64, 8192)
_HEIGHT_LIMIT = (64, 8192)
_FPS_LIMIT = (1.0, 120.0)
_DURATION_LIMIT = (0.5, 120.0)

# Windows 优先中文字体；Linux 常见 DejaVu / Noto 兜底（找不到就走 PIL 路由）
FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
)

_FORMATS: dict[str, dict[str, Any]] = {
    "mp4": {
        "label": "MP4", "suffix": ".mp4",
        "video": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "aac", "-b:a", "96k"],
        "mux": ["-movflags", "+faststart"],
    },
    "mov": {
        "label": "MOV", "suffix": ".mov",
        "video": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "aac", "-b:a", "96k"],
        "mux": ["-movflags", "+faststart"],
    },
    "webm": {
        "label": "WebM", "suffix": ".webm",
        "video": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-row-mt", "1"],
        "audio": ["-c:a", "libopus", "-b:a", "64k"],
        "mux": [],
    },
}


# ---------------------------------------------------------------------------
# 基础工具（全部延迟导入，测试可只读导入本模块）
# ---------------------------------------------------------------------------
def _ffmpeg_exe() -> str:
    """ffmpeg 路径（复用 pipeline.core 的查找逻辑：PATH → imageio-ffmpeg）。"""
    from .core import _ffmpeg_exe as _core_ffmpeg

    return _core_ffmpeg()


def _probe(path: Path | str) -> dict:
    """探测产物（时长/音视频流）；失败抛异常，调用方转成 warnings。"""
    from .core import probe_media

    return probe_media(Path(path))


def _run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1200:]
        raise RuntimeError(f"{what}失败（ffmpeg exit {proc.returncode}）：{tail}")
    return proc


def _pil_available() -> bool:
    try:
        return bool(__import__("PIL"))
    except Exception:  # noqa: BLE001 - 没装 Pillow 时走纯色底/报告缺失
        return False


def normalize_mode(mode: Any) -> str:
    """把 UI 下拉/英文别名归一化为四个 mode 之一；未知值抛 ValueError（不猜）。"""
    key = str(mode or "").strip()
    if key in _MODE_ALIASES:
        return _MODE_ALIASES[key]
    lowered = key.lower()
    if lowered in _MODE_ALIASES:
        return _MODE_ALIASES[lowered]
    raise ValueError(
        f"不支持的片头片尾 mode：{mode!r}；允许值 {list(MODE_CHOICES)} 或 "
        "none/intro/outro/both")


def resolve_format(fmt_label: Any) -> dict:
    """输出格式标签 → 编码参数；未知格式明确报错（不静默降级）。"""
    text = str(fmt_label or "mp4").strip().lower()
    for key in ("mp4", "mov", "webm"):
        if text.startswith(key):
            return {"key": key, **_FORMATS[key]}
    if text in _FORMATS:
        return {"key": text, **_FORMATS[text]}
    raise ValueError(f"不支持的片头片尾输出格式：{fmt_label!r}（支持 mp4/mov/webm）")


def find_font_file(explicit: str | None = None) -> str | None:
    """查找可用字体：显式路径 → 内置候选 → None（None 时走 PIL 路由）。"""
    if explicit:
        p = Path(str(explicit))
        if p.is_file():
            return str(p)
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _even(value: Any, default: int, limits: tuple[int, int]) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = default
    lo, hi = limits
    number = max(lo, min(hi, number))
    if number % 2:
        number += 1
    return number


def _clamp_float(value: Any, default: float, limits: tuple[float, float]) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    lo, hi = limits
    if not math.isfinite(number):
        number = default
    return max(lo, min(hi, number))


# ---------------------------------------------------------------------------
# 文本排版（drawtext 与 PIL 两条路由共用）
# ---------------------------------------------------------------------------
def _collapse(text: Any) -> str:
    return " ".join(str(text or "").replace("\r", "\n").split())


def _wrapped_lines(text: str, *, max_chars: int) -> list[str]:
    """按字符宽度换行（中文无空格，按字数切；优先在空格/标点后断行）。"""
    text = _collapse(text)
    if not text:
        return []
    max_chars = max(4, int(max_chars))
    lines: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if len(current) >= max_chars:
            cut = current
            for punct in ("，", "。", "、", "；", "：", ",", ".", " ", "-", "|"):
                idx = current.rfind(punct)
                if idx >= max(2, max_chars // 2):
                    cut = current[: idx + 1]
                    current = current[idx + 1:]
                    break
            else:
                current = ""
            lines.append(cut.strip())
    if current.strip():
        lines.append(current.strip())
    return [line for line in lines if line]


def _block_sizes(height: int) -> dict[str, int]:
    return {
        "title": max(24, int(height / 8.5)),
        "subtitle": max(18, int(height / 16.0)),
        "credits": max(14, int(height / 22.0)),
        "watermark": max(12, int(height / 42.0)),
    }


def _text_layout(*, width: int, height: int,
                 blocks: list[tuple[str, str]]) -> list[dict]:
    """把 (文本, 样式) 块排成逐行像素坐标；返回 [{text,size,y,kind,block}]。"""
    sizes = _block_sizes(height)
    entries: list[dict] = []
    block_index = 0
    for text, kind in blocks:
        body = _collapse(text)
        if not body:
            block_index += 1
            continue
        size = sizes.get(kind, sizes["subtitle"])
        max_chars = max(6, int(width * 0.86 / max(1, size)))
        for line in _wrapped_lines(body, max_chars=max_chars):
            entries.append({"text": line, "size": size, "kind": kind,
                            "block": block_index})
        block_index += 1
    if not entries:
        return entries
    line_heights = [int(entry["size"] * 1.38) for entry in entries]
    block_gap = int(sizes["title"] * 0.45)
    total = sum(line_heights)
    for previous, current in zip(entries, entries[1:]):
        if previous["block"] != current["block"]:
            total += block_gap
    y = max(8, int((height - total) / 2))
    previous_block = entries[0]["block"]
    for entry, line_h in zip(entries, line_heights):
        if entry["block"] != previous_block:
            y += block_gap
            previous_block = entry["block"]
        entry["y"] = y
        y += line_h
    return entries


# ---------------------------------------------------------------------------
# 背景 / 卡片（PIL）
# ---------------------------------------------------------------------------
def _render_background(path: Path, *, width: int, height: int,
                       logo_path: str | None,
                       warnings: list[str]) -> Path | None:
    """渐变底图（PIL 不可用时返回 None，drawtext 路由会退回纯色底）。"""
    if not _pil_available():
        return None
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (8, 10, 16))
    draw = ImageDraw.Draw(image)
    top = (20, 27, 43)
    bottom = (4, 5, 9)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(round(top[i] * (1.0 - ratio) + bottom[i] * ratio))
                      for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    if logo_path:
        try:
            logo = Image.open(str(logo_path)).convert("RGBA")
            max_w = int(width * 0.22)
            max_h = int(height * 0.16)
            scale = min(max_w / max(1, logo.width), max_h / max(1, logo.height), 1.0)
            logo = logo.resize((max(1, int(logo.width * scale)),
                                max(1, int(logo.height * scale))))
            image.paste(logo, ((width - logo.width) // 2,
                               max(8, int(height * 0.14))), logo)
        except Exception as exc:  # noqa: BLE001 - logo 坏掉不静默
            warnings.append(f"片头片尾 logo 读取失败（已忽略 logo）：{type(exc).__name__}: {exc}")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _render_card_png(path: Path, *, width: int, height: int,
                     blocks: list[tuple[str, str]], font_file: str,
                     logo_path: str | None, watermark_text: str,
                     warnings: list[str]) -> Path:
    """PIL 整卡渲染（背景 + 文字 + 右下角水印）。字体不可用抛 RuntimeError。"""
    from PIL import Image, ImageDraw, ImageFont

    base = _render_background(path.with_name(path.stem + "_bg.png"),
                              width=width, height=height,
                              logo_path=logo_path, warnings=warnings)
    if base is not None:
        image = Image.open(base).convert("RGBA")
    else:
        image = Image.new("RGBA", (width, height), (8, 10, 16, 255))
    draw = ImageDraw.Draw(image)
    sizes = _block_sizes(height)
    entries = _text_layout(width=width, height=height, blocks=blocks)

    def _font(size: int):
        try:
            return ImageFont.truetype(font_file, int(size))
        except Exception as exc:  # noqa: BLE001 - 字体文件坏了要走 warnings
            raise RuntimeError(f"字体不可用（{font_file}）：{type(exc).__name__}: {exc}") from exc

    for entry in entries:
        font = _font(entry["size"])
        try:
            bbox = draw.textbbox((0, 0), entry["text"], font=font)
            text_w = bbox[2] - bbox[0]
        except Exception:  # noqa: BLE001 - 老 Pillow
            text_w = len(entry["text"]) * entry["size"]
        x = max(4, (width - text_w) // 2)
        # 柔和描边，深色背景可读性更好
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            draw.text((x + dx, entry["y"] + dy), entry["text"], font=font,
                      fill=(0, 0, 0, 190))
        draw.text((x, entry["y"]), entry["text"], font=font, fill=(255, 255, 255, 245))
    if watermark_text:
        _draw_watermark_pil(draw, _collapse(watermark_text), width, height, sizes,
                            font_factory=_font)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path)
    return path


def _draw_watermark_pil(draw: Any, text: str, width: int, height: int,
                        sizes: dict[str, int], *, font_factory) -> None:
    if not text:
        return
    try:
        font = font_factory(sizes["watermark"])
    except Exception:  # noqa: BLE001 - 水印失败不该拖垮整卡
        return
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:  # noqa: BLE001
        text_w, text_h = len(text) * sizes["watermark"], sizes["watermark"]
    margin = max(12, int(height / 60))
    draw.text((width - text_w - margin, height - text_h - margin * 2), text,
              font=font, fill=(235, 235, 235, 210))


# ---------------------------------------------------------------------------
# 编码（两条路由共用）
# ---------------------------------------------------------------------------
def _escape_filter_path(path: str | Path) -> str:
    """ffmpeg filter 参数里的路径转义（Windows 反斜杠/盘符冒号/单引号）。"""
    text = str(path).replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


def _encode_still_png(png: Path, out: Path, *, duration: float, fps: float,
                      fmt: dict, fade: float, warnings: list[str]) -> Path:
    """静态 PNG → 带静音音轨的短视频（PIL 路由）。"""
    ffmpeg = _ffmpeg_exe()
    suffix = Path(png).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        raise RuntimeError(f"卡片图格式不支持：{png}")
    fade_out_at = max(0.0, duration - fade)
    vf = (f"fade=t=in:st=0:d={fade:.3f},"
          f"fade=t=out:st={fade_out_at:.3f}:d={fade:.3f},format=yuv420p")
    cmd = [ffmpeg, "-y", "-v", "error",
           "-loop", "1", "-i", str(png),
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-t", f"{duration:.3f}", "-r", f"{fps:g}",
           "-vf", vf, *fmt["video"], *fmt["audio"], *fmt["mux"],
           "-shortest", str(out)]
    _run(cmd, what="片头片尾静态卡编码")
    return out


def _encode_drawtext(bg_png: Path | None, out: Path, *, width: int, height: int,
                     duration: float, fps: float, fmt: dict, fade: float,
                     font_file: str, entries: list[dict],
                     watermark_text: str, workdir: Path) -> Path:
    """ffmpeg drawtext 路由：背景 PNG（或纯色底）+ 逐行 drawtext + 淡入淡出。"""
    ffmpeg = _ffmpeg_exe()
    text_dir = Path(workdir) / "texts"
    text_dir.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    font_escaped = _escape_filter_path(font_file)
    for index, entry in enumerate(entries):
        text_file = text_dir / f"line_{index:03d}.txt"
        text_file.write_text(entry["text"], encoding="utf-8")
        text_escaped = _escape_filter_path(text_file)
        filters.append(
            "drawtext=fontfile='{}':textfile='{}':fontcolor=white:"
            "fontsize={}:x=(w-text_w)/2:y={}".format(
                font_escaped, text_escaped, int(entry["size"]), int(entry["y"])))
    if watermark_text:
        sizes = _block_sizes(height)
        wm_file = text_dir / "watermark.txt"
        wm_file.write_text(_collapse(watermark_text), encoding="utf-8")
        margin = max(12, int(height / 60))
        filters.append(
            "drawtext=fontfile='{}':textfile='{}':fontcolor=white@0.85:"
            "fontsize={}:x=w-text_w-{}:y=h-text_h-{}".format(
                font_escaped, _escape_filter_path(wm_file),
                int(sizes["watermark"]), margin, margin * 2))
    fade_out_at = max(0.0, duration - fade)
    filters.append(f"fade=t=in:st=0:d={fade:.3f}")
    filters.append(f"fade=t=out:st={fade_out_at:.3f}:d={fade:.3f}")
    filters.append("format=yuv420p")
    vf = ",".join(filters)
    cmd = [ffmpeg, "-y", "-v", "error"]
    if bg_png is not None:
        cmd += ["-loop", "1", "-i", str(bg_png)]
    else:
        cmd += ["-f", "lavfi", "-i",
                f"color=c=0x101820:s={width}x{height}:r={fps:g}"]
    cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{duration:.3f}", "-r", f"{fps:g}", "-vf", vf,
            *fmt["video"], *fmt["audio"], *fmt["mux"], "-shortest", str(out)]
    _run(cmd, what="片头片尾 drawtext 编码")
    return out


def _validate_output(path: Path, *, expected_seconds: float) -> dict:
    """产物校验：必须存在、有视频流、时长与配置接近；不合法抛异常。"""
    if not path.is_file():
        raise RuntimeError(f"产物不存在：{path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"产物为空文件：{path}")
    info = _probe(path)
    duration = float(info.get("duration_s") or 0.0)
    if not info.get("has_video"):
        raise RuntimeError(f"产物没有视频流：{path}（probe={info}）")
    if not info.get("has_audio"):
        raise RuntimeError(f"产物没有静音音轨：{path}（probe={info}）")
    if abs(duration - float(expected_seconds)) > max(0.5, float(expected_seconds) * 0.3):
        raise RuntimeError(
            f"产物时长异常：期望 {expected_seconds}s，实际 {duration}s（{path}）")
    return info


# ---------------------------------------------------------------------------
# 单张卡片
# ---------------------------------------------------------------------------
def _make_card(
    *,
    role: str,
    workdir: Path,
    duration: float,
    width: int,
    height: int,
    fps: float,
    blocks: list[tuple[str, str]],
    fmt: dict,
    font_file: str | None,
    logo_path: str | None,
    watermark_text: str,
    warnings: list[str],
    audit: list[str],
) -> Path | None:
    """生成一张卡片；优先 drawtext，失败退 PIL，双失败返回 None + warnings。"""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    label = "片头" if role == "intro" else "片尾"
    out = workdir / f"{role}_card{fmt['suffix']}"
    fade = max(0.12, min(0.5, duration / 4.0))
    errors: list[str] = []

    # 路由 A：ffmpeg drawtext（需要字体；背景图由 PIL 生成，PIL 缺失则纯色底）
    if font_file:
        entries = _text_layout(width=width, height=height, blocks=blocks)
        bg_png = None
        try:
            bg_png = _render_background(
                workdir / f"{role}_background.png", width=width, height=height,
                logo_path=logo_path, warnings=warnings)
        except Exception as exc:  # noqa: BLE001 - 背景失败仍可纯色底继续
            warnings.append(f"{label}背景图生成失败（改纯色底）：{type(exc).__name__}: {exc}")
        try:
            _encode_drawtext(bg_png, out, width=width, height=height,
                             duration=duration, fps=fps, fmt=fmt, fade=fade,
                             font_file=font_file, entries=entries,
                             watermark_text=watermark_text, workdir=workdir)
            info = _validate_output(out, expected_seconds=duration)
            audit.append(
                f"{label}卡：ffmpeg drawtext 路由，{duration:g}s，"
                f"{width}x{height}@{fps:g}fps，{info['duration_s']}s，"
                f"{out.stat().st_size} B → {out}")
            return out
        except Exception as exc:  # noqa: BLE001 - 退 PIL 路由，原因入审计
            errors.append(f"drawtext 路由失败：{type(exc).__name__}: {exc}")
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    # 路由 B：PIL 整卡 PNG → 编码（字体不可用或 drawtext 失败的兜底）
    if not _pil_available():
        errors.append("Pillow 不可用：无法走 PNG 兜底路由")
    else:
        try:
            png = _render_card_png(
                workdir / f"{role}_card.png", width=width, height=height,
                blocks=blocks, font_file=font_file or _fallback_font_or_raise(),
                logo_path=logo_path, watermark_text=watermark_text,
                warnings=warnings)
            _encode_still_png(png, out, duration=duration, fps=fps, fmt=fmt,
                              fade=fade, warnings=warnings)
            info = _validate_output(out, expected_seconds=duration)
            audit.append(
                f"{label}卡：PIL PNG 路由（字体={font_file or '兜底'}），{duration:g}s，"
                f"{width}x{height}@{fps:g}fps，{info['duration_s']}s，"
                f"{out.stat().st_size} B → {out}")
            return out
        except Exception as exc:  # noqa: BLE001
            errors.append(f"PIL PNG 路由失败：{type(exc).__name__}: {exc}")
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    warnings.append(f"{label}卡生成失败（返回空，不冒充成功）：" + "；".join(errors))
    return None


def _fallback_font_or_raise() -> str:
    font = find_font_file()
    if not font:
        raise RuntimeError(
            "未找到可用字体：请安装中文字体（如 msyh.ttc）或通过 font_path 显式指定")
    return font


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------
def generate_intro_outro(
    workdir: str | Path,
    *,
    mode: Any,
    fmt_label: str = "mp4（H.264）",
    width: int = 1920,
    height: int = 1088,
    fps: float = 24,
    intro_seconds: float = 3.0,
    outro_seconds: float = 3.0,
    title: str = "",
    subtitle: str = "",
    credits: str = "",
    logo_path: str | None = None,
    watermark_text: str = "",
    warnings: list[str] | None = None,
    outro_title: str = "",
    font_path: str | None = None,
) -> dict:
    """生成片头/片尾静态卡短视频（详见模块 docstring 的 UI 契约与失败口径）。

    返回 dict：``intro``/``outro``（Path|None）、``audit``（list[str] 可读审计）、
    ``warnings``（list[str]，与传入列表同一对象）、``mode``（归一化值）、
    ``format``（mp4/mov/webm）、``params``（实际生效尺寸/帧率/时长）。
    """
    warn = warnings if warnings is not None else []
    audit: list[str] = []
    normalized = normalize_mode(mode)
    fmt = resolve_format(fmt_label)
    workdir = Path(workdir)
    started = time.time()
    audit.append(
        "片头片尾：mode={} 格式={} 目标={}x{}@{}fps 片头={}s 片尾={}s".format(
            normalized, fmt["label"], width, height, fps,
            intro_seconds, outro_seconds))
    if normalized == MODE_NONE:
        audit.append("mode=无：不生成片头片尾（不调用 ffmpeg）")
        return {"intro": None, "outro": None, "audit": audit, "warnings": warn,
                "mode": normalized, "format": fmt["key"], "params": {
                    "width": int(width), "height": int(height), "fps": float(fps)}}

    out_w = _even(width, 1920, _WIDTH_LIMIT)
    out_h = _even(height, 1088, _HEIGHT_LIMIT)
    out_fps = _clamp_float(fps, 24.0, _FPS_LIMIT)
    intro_s = _clamp_float(intro_seconds, 3.0, _DURATION_LIMIT)
    outro_s = _clamp_float(outro_seconds, 3.0, _DURATION_LIMIT)
    if out_w != int(width) or out_h != int(height):
        audit.append(
            f"尺寸归一化：{width}x{height} → {out_w}x{out_h}（H.264 yuv420p 需偶数）")
    workdir.mkdir(parents=True, exist_ok=True)
    font_file = find_font_file(font_path)
    if not font_file:
        audit.append("未找到可用字体：drawtext 路由不可用，改用 PIL PNG 渲染（避免 fontconfig）")
    audit.append(f"字体：{font_file or '（无，PIL 兜底）'}")

    intro_path: Path | None = None
    outro_path: Path | None = None

    if normalized in (MODE_INTRO, MODE_BOTH):
        intro_title = _collapse(title)
        if not intro_title:
            intro_title = DEFAULT_TITLE
            audit.append(f"片头文字为空 → 占位标题「{DEFAULT_TITLE}」（不伪造用户输入）")
        blocks: list[tuple[str, str]] = [(intro_title, "title")]
        if _collapse(subtitle):
            blocks.append((_collapse(subtitle), "subtitle"))
        intro_path = _make_card(
            role="intro", workdir=workdir, duration=intro_s, width=out_w,
            height=out_h, fps=out_fps, blocks=blocks, fmt=fmt,
            font_file=font_file, logo_path=logo_path,
            watermark_text=_collapse(watermark_text),
            warnings=warn, audit=audit)

    if normalized in (MODE_OUTRO, MODE_BOTH):
        main_text = _collapse(outro_title) or _collapse(subtitle)
        if not main_text:
            main_text = DEFAULT_OUTRO_TITLE
            audit.append(
                f"片尾文字为空 → 占位标题「{DEFAULT_OUTRO_TITLE}」（不伪造用户输入）")
        credits_text = _collapse(credits)
        if not credits_text:
            credits_text = DEFAULT_CREDITS
            audit.append(
                f"片尾演职员为空 → 占位「{DEFAULT_CREDITS}」（不伪造用户输入）")
        blocks = [(main_text, "title"), (credits_text, "credits")]
        outro_path = _make_card(
            role="outro", workdir=workdir, duration=outro_s, width=out_w,
            height=out_h, fps=out_fps, blocks=blocks, fmt=fmt,
            font_file=font_file, logo_path=logo_path,
            watermark_text=_collapse(watermark_text),
            warnings=warn, audit=audit)

    audit.append(
        "片头片尾结果：intro={} outro={} 耗时={:.2f}s warnings={}".format(
            intro_path or "无", outro_path or "无",
            time.time() - started, len(warn)))
    return {
        "intro": intro_path,
        "outro": outro_path,
        "audit": audit,
        "warnings": warn,
        "mode": normalized,
        "format": fmt["key"],
        "params": {
            "width": out_w, "height": out_h, "fps": out_fps,
            "intro_seconds": intro_s, "outro_seconds": outro_s,
            "font_file": font_file,
        },
    }
