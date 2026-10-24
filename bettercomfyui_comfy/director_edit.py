"""剧核 · 单节点成片后处理（SG6）：拼接/转场/字幕/水印/封面/格式/元数据（OUT1–OUT4）。

全部走本项目 `bettercomfyui/pipeline/core.py` 的已验证能力（splice/encode/write_output），
不自建一套编码器：
- 拼接：`core.splice`（concat demuxer 流复制）；选了转场时用 ffmpeg `xfade`/
  `acrossfade` 做段间转场（ROUND55 扩展：无/淡入淡出/叠化/黑场/白场/左滑/右滑/上滑/
  下滑/擦除/圆开/圆闭/像素化/模糊/推近；支持逐接缝 `transition_plan`、可配时长、
  pin 段硬切、写时长账本与逐处审计；未知/不支持标签回退 fade 并告警），再交给 pipeline 封装。
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
import re
import subprocess
from functools import lru_cache
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
# 转场（ROUND55 扩展）：标签解析 / 时长钳制 / 单遍 xfade / 混合逐遍合成 / 审计
#
# 设计取舍（显式，不静默）：
# * 只认 ffmpeg `xfade` 真正支持的标签：运行时解析 `ffmpeg -h filter=xfade`；
#   本机不支持的标签（如 xfade 无 zoomout）**回退 fade 并写 warnings**，绝不假装生效。
# * 「无」= 硬切，不进 xfade；全硬切走 `core.splice` 流复制（零重编码）。
# * 全部接缝都有转场 → 单遍 xfade 链（一次编码，质量最好）；
#   硬切与转场混合 → 逐遍合成（每遍重编码，但能保证每处转场真的生效）。
# * 时长账本：xfade 会重叠相邻段，输出时长 = Σ段时长 − Σ转场时长；
#   每处转场的输出帧号/时长/标签/原因写进 audit 与 report；任何回退都记录。
# ---------------------------------------------------------------------------
DEFAULT_TRANSITION_SECONDS = 0.5
MIN_TRANSITION_SECONDS = 0.1
MAX_TRANSITION_SECONDS = 2.0
# 转场总重叠不得超过总时长的 30%（且至少允许 1s），避免成片被转场"吃掉"太多
MAX_TOTAL_OVERLAP_FRACTION = 0.30
MAX_TOTAL_OVERLAP_FLOOR_S = 1.0

# 中文/英文标签 → ffmpeg xfade 标签；None = 无转场（硬切）。"拉远" 在 xfade 中无对应
# 实现（只有 zoomin），解析时会回退 fade + 告警，这是如实标注而非静默降级。
TRANSITION_ALIASES: dict[str, str | None] = {
    "无": None, "硬切": None, "不转场": None, "none": None, "cut": None, "off": None,
    "淡入淡出": "fade", "淡入": "fade", "fade": "fade",
    "叠化": "dissolve", "交叉叠化": "dissolve", "溶解": "dissolve",
    "crossfade": "dissolve", "dissolve": "dissolve",
    "黑场": "fadeblack", "黑场过渡": "fadeblack", "淡出黑": "fadeblack",
    "fadeblack": "fadeblack", "black": "fadeblack",
    "白场": "fadewhite", "白场过渡": "fadewhite", "fadewhite": "fadewhite",
    "左滑": "slideleft", "slideleft": "slideleft",
    "右滑": "slideright", "slideright": "slideright",
    "上滑": "slideup", "slideup": "slideup",
    "下滑": "slidedown", "slidedown": "slidedown",
    "擦除左": "wipeleft", "wipeleft": "wipeleft",
    "擦除右": "wiperight", "wiperight": "wiperight",
    "擦除上": "wipeup", "wipeup": "wipeup",
    "擦除下": "wipedown", "wipedown": "wipedown",
    "圆开": "circleopen", "circleopen": "circleopen",
    "圆闭": "circleclose", "circleclose": "circleclose",
    "像素化": "pixelize", "pixelize": "pixelize",
    "模糊": "hblur", "hblur": "hblur",
    "推近": "zoomin", "zoomin": "zoomin", "zoom": "zoomin",
    "拉远": "zoomout", "zoomout": "zoomout",
}

# xfade 标签 → 统一中文名（审计/报告用）
CANONICAL_TRANSITION_LABELS: dict[str, str] = {
    "fade": "淡入淡出", "dissolve": "叠化", "fadeblack": "黑场", "fadewhite": "白场",
    "slideleft": "左滑", "slideright": "右滑", "slideup": "上滑", "slidedown": "下滑",
    "wipeleft": "擦除左", "wiperight": "擦除右", "wipeup": "擦除上", "wipedown": "擦除下",
    "circleopen": "圆开", "circleclose": "圆闭", "pixelize": "像素化", "hblur": "模糊",
    "zoomin": "推近", "zoomout": "拉远", "hblur2": "模糊",
}

# 给节点下拉用的建议顺序（只输出本机 xfade 真正支持的中文标签）
TRANSITION_LABEL_ORDER: tuple[str, ...] = (
    "无", "淡入淡出", "叠化", "黑场", "白场",
    "左滑", "右滑", "上滑", "下滑",
    "擦除左", "擦除右", "擦除上", "擦除下",
    "圆开", "圆闭", "像素化", "模糊", "推近", "拉远",
)

# 解析失败时的兜底清单（ffmpeg 4.3+ xfade 标准标签；实测本机 ffmpeg 7.1 支持全部）
_FALLBACK_XFADE_TAGS: frozenset[str] = frozenset(
    "fade wipeleft wiperight wipeup wipedown slideleft slideright slideup slidedown "
    "circlecrop rectcrop distance fadeblack fadewhite radial smoothleft smoothright "
    "smoothup smoothdown circleopen circleclose vertopen vertclose horzopen horzclose "
    "dissolve pixelize diagtl diagtr diagbl diagbr hlslice hrslice vuslice vdslice "
    "hblur fadegrays wipetl wipetr wipebl wipebr squeezeh squeezev zoomin fadefast "
    "fadeslow hlwind hrwind vuwind vdwind coverleft coverright coverup coverdown "
    "revealleft revealright revealup revealdown".split()
)

_NOT_FOUND = object()


@lru_cache(maxsize=1)
def available_xfade_tags() -> frozenset[str]:
    """本机 ffmpeg 的 xfade transition 标签集合（解析 `-h filter=xfade`；失败用兜底清单）。"""
    try:
        proc = subprocess.run([_ffmpeg(), "-hide_banner", "-h", "filter=xfade"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        text = (proc.stdout or "") + (proc.stderr or "")
        tags = set(re.findall(r"^\s{4,}([a-z]+)\s+-?\d+\s+\.\.FV", text, flags=re.MULTILINE))
        tags.discard("custom")
        if len(tags) >= 10:
            return frozenset(tags)
    except Exception:  # noqa: BLE001 - 探测失败用兜底清单，行为可复现
        pass
    return _FALLBACK_XFADE_TAGS


def available_transition_labels() -> list[str]:
    """返回本机可用的中文转场标签（供节点下拉；"无" 恒可用）。"""
    tags = available_xfade_tags()
    labels = ["无"]
    for label in TRANSITION_LABEL_ORDER[1:]:
        tag = TRANSITION_ALIASES.get(label)
        if tag and tag in tags:
            labels.append(label)
    return labels


def canonical_transition_label(tag: str | None) -> str:
    """xfade 标签 → 中文名；标签为 None/'' → 无。"""
    if not tag:
        return "无"
    return CANONICAL_TRANSITION_LABELS.get(str(tag), str(tag))


def _lookup_alias_tag(label: str | None) -> object:
    """纯别名查表：返回映射值 / None（无转场）/ _NOT_FOUND（未知）。不做任何回退。"""
    text = " ".join(str(label or "").split())
    if not text:
        return None
    mapped: object = TRANSITION_ALIASES.get(text, _NOT_FOUND)
    if mapped is _NOT_FOUND:
        mapped = TRANSITION_ALIASES.get(text.lower(), _NOT_FOUND)
    if mapped is _NOT_FOUND:
        for alias in sorted(TRANSITION_ALIASES, key=len, reverse=True):
            if alias and alias in text:
                mapped = TRANSITION_ALIASES[alias]
                break
    return mapped


def resolve_transition(label: str | None, *,
                       warnings: list[str] | None = None) -> str:
    """转场标签（中文/英文）→ 本机 xfade 标签；"无" 返回 ""。

    未知标签 / 本机不支持 → 回退 "fade" 并写 warnings（不静默降级）。
    """
    warnings = warnings if warnings is not None else []
    text = " ".join(str(label or "").split())
    if not text:
        return ""
    mapped: object = TRANSITION_ALIASES.get(text, _NOT_FOUND)
    if mapped is _NOT_FOUND:
        mapped = TRANSITION_ALIASES.get(text.lower(), _NOT_FOUND)
    if mapped is _NOT_FOUND:
        for alias in sorted(TRANSITION_ALIASES, key=len, reverse=True):
            if alias and alias in text:
                mapped = TRANSITION_ALIASES[alias]
                break
    if mapped is _NOT_FOUND:
        warnings.append(f"⚠️ 未知转场标签 {label!r} → 已回退「淡入淡出」(fade)，不静默忽略")
        return "fade"
    if mapped is None:
        return ""
    tag = str(mapped)
    if tag not in available_xfade_tags():
        warnings.append(
            f"⚠️ 本机 ffmpeg xfade 不支持转场 {label!r}（映射 {tag}）→ 已回退「淡入淡出」"
            f"(fade)，不静默忽略")
        return "fade"
    return tag


def _clamp_transition_seconds(requested: float, *, seg_a: float, seg_b: float,
                              warnings: list[str], cut_index: int) -> float:
    """钳制转场时长到 0.1–2s，且不超过相邻较短段时长的 90%（xfade offset 必须为正）。"""
    try:
        value = float(requested)
    except (TypeError, ValueError):
        warnings.append(f"⚠️ 转场[{cut_index + 1}] 时长 {requested!r} 无法解析 → 用默认 "
                        f"{DEFAULT_TRANSITION_SECONDS}s")
        value = DEFAULT_TRANSITION_SECONDS
    if value < MIN_TRANSITION_SECONDS:
        warnings.append(f"⚠️ 转场[{cut_index + 1}] 时长 {value:.2f}s 过短 → 钳制到 "
                        f"{MIN_TRANSITION_SECONDS}s")
        value = MIN_TRANSITION_SECONDS
    if value > MAX_TRANSITION_SECONDS:
        warnings.append(f"⚠️ 转场[{cut_index + 1}] 时长 {value:.2f}s 过长 → 钳制到 "
                        f"{MAX_TRANSITION_SECONDS}s")
        value = MAX_TRANSITION_SECONDS
    cap = max(MIN_TRANSITION_SECONDS, min(seg_a, seg_b) * 0.9)
    if value > cap:
        warnings.append(f"⚠️ 转场[{cut_index + 1}] 时长 {value:.2f}s 超过相邻段 90%"
                        f"（{cap:.2f}s）→ 钳制，避免 offset 为负")
        value = round(cap, 3)
    return round(max(MIN_TRANSITION_SECONDS, value), 3)


def _normalize_transition_plan(
    n_files: int,
    durations: list[float],
    *,
    transition: str | None,
    transition_plan: Any,
    fade_seconds: float,
    transition_seconds: float | None,
    warnings: list[str],
) -> list[dict]:
    """把「统一标签 / 每处 per-cut 列表 / 旧 fade_seconds」归一化成逐接缝计划。

    返回 list[dict]，长度 = n_files-1，字段：
        cut_index / requested / transition(中文) / xfade / duration / reason / fallback
    """
    n_cuts = max(0, n_files - 1)
    if n_cuts == 0:
        return []
    # 默认标签：显式 transition 优先；旧接口 fade_seconds>0 视为淡入淡出；否则无
    if transition is None:
        if fade_seconds and float(fade_seconds) > 0:
            default_label = "淡入淡出"
        else:
            default_label = "无"
    else:
        default_label = str(transition)
    if transition_seconds is not None:
        default_seconds = float(transition_seconds)
    elif fade_seconds and float(fade_seconds) > 0:
        default_seconds = float(fade_seconds)
    else:
        default_seconds = DEFAULT_TRANSITION_SECONDS

    entries: dict[int, dict] = {}
    raw_plan = transition_plan
    if isinstance(raw_plan, str):
        default_label = raw_plan
        raw_plan = None
    elif isinstance(raw_plan, dict):
        raw_plan = [raw_plan]
    if raw_plan:
        if not isinstance(raw_plan, (list, tuple)):
            warnings.append(f"⚠️ transition_plan 类型 {type(raw_plan).__name__} 不受理"
                            "（应为 list）→ 已忽略，使用统一标签")
            raw_plan = []
        for pos, entry in enumerate(raw_plan):
            if isinstance(entry, str):
                idx, label, seconds, reason = pos, entry, None, "每处计划（标签列表）"
            elif isinstance(entry, dict):
                raw_index = entry.get("cut_index", entry.get("index", pos))
                try:
                    idx = int(raw_index)
                except (TypeError, ValueError):
                    warnings.append(f"⚠️ transition_plan 第 {pos} 项 cut_index={raw_index!r} "
                                    "无法解析 → 已跳过该项")
                    continue
                label = (entry.get("transition") or entry.get("label")
                         or entry.get("type") or "无")
                seconds = entry.get("duration", entry.get("seconds"))
                reason = str(entry.get("reason") or "每处计划指定")
            else:
                warnings.append(f"⚠️ transition_plan 第 {pos} 项类型 {type(entry).__name__} "
                                "不受理 → 已跳过")
                continue
            if idx < 0 or idx >= n_cuts:
                warnings.append(f"⚠️ transition_plan 的 cut_index={idx} 越界（有效 0.."
                                f"{n_cuts - 1}）→ 已跳过该项")
                continue
            if idx in entries:
                warnings.append(f"⚠️ transition_plan 的 cut_index={idx} 重复 → 后者覆盖前者")
            entries[idx] = {"label": str(label), "seconds": seconds, "reason": reason}

    cuts: list[dict] = []
    for i in range(n_cuts):
        entry = entries.get(i)
        label = entry["label"] if entry else default_label
        reason = entry["reason"] if entry else ("统一转场" if str(default_label).strip()
                                                not in ("", "无", "none", "None") else "硬切")
        tag = resolve_transition(label, warnings=warnings)
        if not tag:
            cuts.append({"cut_index": i, "requested": str(label), "transition": "无",
                         "xfade": "", "duration": 0.0, "reason": reason,
                         "fallback": False})
            continue
        # 纯查表判断「请求的标签是否真的就是最终标签」（resolve 本身会回退，不能用它）
        requested_tag = _lookup_alias_tag(str(label))
        fell_back = requested_tag is _NOT_FOUND or (
            requested_tag is not None and str(requested_tag) != tag)
        requested_seconds = entry["seconds"] if entry and entry.get("seconds") is not None \
            else default_seconds
        seconds = _clamp_transition_seconds(
            requested_seconds, seg_a=durations[i], seg_b=durations[i + 1],
            warnings=warnings, cut_index=i)
        cuts.append({"cut_index": i, "requested": str(label),
                     "transition": canonical_transition_label(tag), "xfade": tag,
                     "duration": seconds, "reason": reason,
                     "fallback": bool(fell_back)})

    # 全局重叠上限：转场总重叠不该把成片"吃掉"太多（时长账本要健康）
    total_overlap = sum(c["duration"] for c in cuts)
    if total_overlap > 0:
        total_segments = sum(durations)
        allowed = max(MAX_TOTAL_OVERLAP_FLOOR_S, total_segments * MAX_TOTAL_OVERLAP_FRACTION)
        if total_overlap > allowed:
            factor = allowed / total_overlap
            warnings.append(
                f"⚠️ 转场总重叠 {total_overlap:.2f}s 超过总时长可用上限 {allowed:.2f}s "
                f"→ 全部转场时长按 {factor:.2f}× 等比缩短（避免成片被转场严重缩短）")
            for cut in cuts:
                if cut["duration"] > 0:
                    cut["duration"] = round(max(MIN_TRANSITION_SECONDS,
                                                cut["duration"] * factor), 3)
    return cuts


def _durations_and_audio(files: list[Path], warnings: list[str]) -> tuple[list[float], list[bool]]:
    durations: list[float] = []
    audios: list[bool] = []
    for file in files:
        info = probe_media(file)
        dur = info.get("duration_s") or info.get("duration")
        if not dur or float(dur) <= 0:
            raise RuntimeError("无法读取段时长，不能做转场：" + str(file))
        durations.append(round(float(dur), 3))
        audios.append(bool(info.get("has_audio")))
    return durations, audios


@lru_cache(maxsize=64)
def _fps_of(path_str: str) -> float:
    """探测视频帧率（xfade 输入要求 CFR；这里只用于审计帧号，探测失败按 24fps 记）。"""
    try:
        proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path_str],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        text = (proc.stderr or "") + (proc.stdout or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
        if match:
            value = float(match.group(1))
            return value if value > 0 else 24.0
    except Exception:  # noqa: BLE001
        pass
    return 24.0


def _build_duration_ledger(durations: list[float], cuts: list[dict], fps: float) -> dict:
    """输出时间轴账本：每段在输出中的起点、每处接缝的起止帧号、预期总时长。"""
    starts: list[float] = [0.0]
    cut_records: list[dict] = []
    cursor = float(durations[0]) if durations else 0.0
    for i, cut in enumerate(cuts):
        fade = float(cut.get("duration") or 0.0)
        cut_start = max(0.0, cursor - fade)
        cut_records.append({
            "cut_index": i,
            "output_start_s": round(cut_start, 3),
            "before_frame": int(round(cut_start * fps)),
            "after_frame": int(round((cut_start + fade) * fps)),
            "duration_frames": int(round(fade * fps)),
        })
        starts.append(round(cut_start, 3))
        cursor = cursor + float(durations[i + 1]) - fade
    return {
        "fps": fps,
        "segment_start_s": starts,
        "expected_duration_s": round(cursor, 3),
        "overlap_total_s": round(sum(float(c.get("duration") or 0.0) for c in cuts), 3),
        "cut_frames": cut_records,
    }


_X264_OUT_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def _render_video_single_pass(files: list[Path], cuts: list[dict], out_path: Path,
                              *, durations: list[float]) -> Path:
    """视频单遍 xfade 链（不含音频）：所有接缝都是转场时使用（一次编码）。

    为什么会「音画分离」：实测本机 ffmpeg 7.1 在「xfade 视频链 + acrossfade 音频链」
    同时进一张 filter_complex 时，短片段（如 4×0.5s、0.2s 转场）会出现视频流正常、
    AAC 编码器收不到帧的失败；拆成「视频一遍 + 音频一遍 + 封装」后稳定通过
    （两种链路都已单测覆盖）。代价只是多一次无损封装，不是降级。
    """
    cmd = [_ffmpeg(), "-y", "-v", "error"]
    for file in files:
        cmd += ["-i", str(file)]
    parts: list[str] = []
    prev_v = "[0:v]"
    acc = float(durations[0])
    for i, cut in enumerate(cuts):
        tag = str(cut["xfade"])
        fade = float(cut["duration"])
        offset = round(acc - fade, 3)
        if offset <= 0:
            raise RuntimeError(f"第 {i + 1} 处转场 offset={offset} 非正（段太短 / 转场太长）")
        parts.append(f"{prev_v}[{i + 1}:v]xfade=transition={tag}:duration={fade:.3f}:"
                     f"offset={offset:.3f}[vx{i + 1}]")
        prev_v = f"[vx{i + 1}]"
        acc = acc + float(durations[i + 1]) - fade
    cmd += ["-filter_complex", ";".join(parts), "-map", prev_v, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)]
    _run(cmd, what="单遍转场视频合成")
    if not Path(out_path).is_file():
        raise RuntimeError("单遍转场视频合成结束但无输出文件")
    return Path(out_path)


def _render_video_pairwise(files: list[Path], cuts: list[dict], out_path: Path,
                           *, durations: list[float], workdir: Path,
                           warnings: list[str]) -> Path:
    """视频逐遍合成（不含音频）：硬切 + 转场混合时使用，每遍输入都是独立文件。"""
    workdir = Path(workdir)
    acc_path = files[0]
    acc_dur = float(durations[0])
    for i, cut in enumerate(cuts):
        next_file = files[i + 1]
        last = i == len(cuts) - 1
        dst = Path(out_path) if last else workdir / f"pair_{i + 1}.mp4"
        fade = float(cut.get("duration") or 0.0)
        cmd = [_ffmpeg(), "-y", "-v", "error", "-i", str(acc_path), "-i", str(next_file)]
        if fade > 0:
            if fade >= acc_dur - 0.02:
                new_fade = round(max(MIN_TRANSITION_SECONDS, acc_dur * 0.5), 3)
                warnings.append(
                    f"⚠️ 转场[{i + 1}] 时长 {fade:.2f}s ≥ 累加片长 {acc_dur:.2f}s → "
                    f"缩短为 {new_fade:.2f}s（逐遍合成安全钳制）")
                fade = new_fade
                cut["duration"] = new_fade
            parts = [f"[0:v][1:v]xfade=transition={cut['xfade']}:duration={fade:.3f}:"
                     f"offset={round(acc_dur - fade, 3):.3f}[v]"]
            new_dur = acc_dur + float(durations[i + 1]) - fade
        else:
            parts = ["[0:v][1:v]concat=n=2:v=1:a=0[v]"]
            new_dur = acc_dur + float(durations[i + 1])
        cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
        _run(cmd, what=f"逐遍转场视频合成（第 {i + 1}/{len(cuts)} 处）")
        if not dst.is_file():
            raise RuntimeError(f"逐遍合成第 {i + 1} 处没有输出文件")
        acc_path = dst
        acc_dur = new_dur
    warnings.append(
        "⚠️ 本次为「硬切+转场混合」逐遍合成：每处接缝都会重编码一次（画质略低于单遍），"
        "该取舍已如实记录，不是静默降级")
    return acc_path


def _render_audio_mixed(files: list[Path], cuts: list[dict], out_path: Path) -> Path:
    """音频单独一遍：转场接缝 acrossfade、硬切接缝 concat（视频已单独渲染）。"""
    cmd = [_ffmpeg(), "-y", "-v", "error"]
    for file in files:
        cmd += ["-i", str(file)]
    parts: list[str] = []
    prev_a = "[0:a]"
    for i, cut in enumerate(cuts):
        if float(cut.get("duration") or 0.0) > 0:
            parts.append(f"{prev_a}[{i + 1}:a]acrossfade=d={float(cut['duration']):.3f}"
                         f":c1=tri:c2=tri[ax{i + 1}]")
        else:
            parts.append(f"{prev_a}[{i + 1}:a]concat=n=2:v=0:a=1[ax{i + 1}]")
        prev_a = f"[ax{i + 1}]"
    cmd += ["-filter_complex", ";".join(parts), "-map", prev_a,
            "-c:a", "aac", "-b:a", "192k", str(out_path)]
    _run(cmd, what="转场音频合成")
    if not Path(out_path).is_file():
        raise RuntimeError("转场音频合成结束但无输出文件")
    return Path(out_path)


def _mux_audio(video: Path, audio: Path, out_path: Path) -> Path:
    """把视频遍与音频遍无损封装（视频流复制，不二次编码）。"""
    cmd = [_ffmpeg(), "-y", "-v", "error", "-i", str(video), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
           "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out_path)]
    _run(cmd, what="转场音画封装")
    if not Path(out_path).is_file():
        raise RuntimeError("转场音画封装结束但无输出文件")
    return Path(out_path)


def _stream_copy_splice(files: list[Path], workdir: Path) -> Path:
    from bettercomfyui.pipeline.core import SpliceInput, splice

    inputs = [
        SpliceInput(segment_id=f"seg{i + 1}", segment_version="v1", video_path=file)
        for i, file in enumerate(files)
    ]
    return splice(inputs, workdir=workdir, out_path=workdir / "spliced.mp4")


def splice_segments(
    seg_files: list[Path | str],
    *,
    workdir: Path,
    fade_seconds: float = 0.0,
    transition: str | None = None,
    transition_plan: list[Any] | dict | str | None = None,
    transition_seconds: float | None = None,
    warnings: list[str] | None = None,
    audit: list[str] | None = None,
    report: dict | None = None,
) -> Path:
    """按顺序拼接段视频，支持逐接缝转场（ROUND55 扩展）。

    参数（全部 keyword-only，向后兼容旧调用）：
      * ``fade_seconds``：旧接口；>0 等价于所有接缝「淡入淡出」，时长 = 该值。
      * ``transition``：统一转场标签（中文/英文；"无" = 硬切）。
      * ``transition_plan``：逐接缝计划，长度应为段数-1：
          - ``list[str]``：第 i 项 = 第 i 处接缝标签；
          - ``list[dict]``：``{"cut_index": i, "transition": "叠化",
            "duration": 0.4, "reason": "..."}``（cut_index 从 0 开始，
            指"第 i 段与第 i+1 段之间"；缺省项用统一标签）。
      * ``transition_seconds``：统一转场时长（会被钳制到 0.1–2s 且 ≤相邻段 90%）。
      * ``warnings``：追加告警（未知标签回退、时长钳制、引擎回退……都会写进来）。
      * ``audit``：追加「每处转场前后帧号/时长/标签/原因」的人类可读审计行。
      * ``report``：结构化报告 dict（引擎、时长账本、逐接缝明细、回退事实）。

    返回拼接后的 mp4 路径；失败会自动回退（逐遍 → 流复制）并如实记录 warnings，
    绝不假装转场生效。
    """
    warnings = warnings if warnings is not None else []
    audit = audit if audit is not None else []
    out_report: dict = report if report is not None else {}
    files = [Path(f) for f in seg_files]
    if not files:
        raise ValueError("拼接输入为空：没有段视频")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if len(files) == 1:
        info = probe_media(files[0])
        out_report.update({"executed": True, "engine": "single_segment",
                           "segments": 1, "cut_count": 0,
                           "actual_duration_s": info.get("duration_s"),
                           "cuts": [], "audit_lines": ["单段输入：无接缝，不涉及转场"],
                           "warnings": list(warnings)})
        audit.append("拼接：单段输入（无接缝，不涉及转场）")
        return files[0]

    durations, audios = _durations_and_audio(files, warnings)
    use_audio = all(audios)
    if not use_audio:
        warnings.append("⚠️ 转场拼接：存在无音轨段 → 输出丢弃音轨（如实记录，不冒充有声）")
    fps = _fps_of(str(files[0]))
    cuts = _normalize_transition_plan(
        len(files), durations, transition=transition, transition_plan=transition_plan,
        fade_seconds=fade_seconds, transition_seconds=transition_seconds,
        warnings=warnings)

    warnings[:] = list(dict.fromkeys(warnings))  # 去重：同一未知标签只提醒一次

    engine = "copy"
    audio_engine = "copy"
    output: Path | None = None
    falling_back = False
    if any(cut["xfade"] for cut in cuts):
        video_path = workdir / "transitions_video.mp4"
        all_transitions = all(cut["xfade"] for cut in cuts)
        video_ok = False
        if all_transitions:
            try:
                output = _render_video_single_pass(files, cuts, video_path,
                                                   durations=durations)
                engine = "xfade_chain"
                video_ok = True
            except Exception as exc:  # noqa: BLE001 - 单遍失败必须可见并尝试逐遍
                warnings.append(f"⚠️ 单遍转场视频合成失败 → 尝试逐遍合成（不静默）: "
                                f"{type(exc).__name__}: {exc}")
                falling_back = True
        if not video_ok:
            try:
                output = _render_video_pairwise(
                    files, cuts, video_path, durations=durations,
                    workdir=workdir, warnings=warnings)
                engine = "pairwise"
                video_ok = True
            except Exception as exc:  # noqa: BLE001 - 逐遍也失败则退流复制
                warnings.append(f"⚠️ 逐遍转场视频合成失败 → 退回流复制拼接，转场未生效"
                                f"（不冒充）: {type(exc).__name__}: {exc}")
                falling_back = True
                output = None
        if video_ok:
            if use_audio:
                audio_path = workdir / "transitions_audio.m4a"
                try:
                    _render_audio_mixed(files, cuts, audio_path)
                    output = _mux_audio(Path(output), audio_path,
                                        workdir / "spliced_transitions.mp4")
                    audio_engine = "acrossfade_concat"
                except Exception as exc:  # noqa: BLE001 - 音频失败要如实降级
                    warnings.append(
                        f"⚠️ 转场音频合成失败 → 输出无音轨（视频转场仍生效，如实记录）: "
                        f"{type(exc).__name__}: {exc}")
                    audio_engine = "none_audio_failed"
                    use_audio = False
            else:
                audio_engine = "none"
    if output is None:
        output = _stream_copy_splice(files, workdir)
        engine = "copy_fallback" if falling_back or any(c["xfade"] for c in cuts) else "copy"
        audio_engine = "copy"

    actual_info = probe_media(output)
    ledger = _build_duration_ledger(durations, cuts, fps)
    actual_duration = actual_info.get("duration_s")
    fallback_all = engine == "copy_fallback"
    cut_records: list[dict] = []
    audit_lines: list[str] = []
    header = (f"转场拼接：{len(files)} 段 / {len(cuts)} 处接缝，引擎={engine}"
              f"（音轨={'保留' if use_audio else '丢弃'}）")
    if fallback_all:
        header += "；⚠️ 转场合成失败已退回流复制（所有转场未生效）"
    audit_lines.append(header)
    for i, cut in enumerate(cuts):
        ledger_cut = ledger["cut_frames"][i]
        applied = bool(cut["xfade"]) and not fallback_all
        record = {
            "cut_index": i,
            "from_segment": i + 1,
            "to_segment": i + 2,
            "requested": cut["requested"],
            "transition": cut["transition"],
            "xfade": cut["xfade"] or None,
            "duration_s": float(cut["duration"]) if applied else 0.0,
            "reason": cut["reason"],
            "applied": applied,
            "mode": "xfade" if applied else ("cut" if not cut["xfade"] else "lost_fallback"),
            "fallback_lost": bool(cut["xfade"]) and fallback_all,
            "fallback": bool(cut.get("fallback")),
            **ledger_cut,
        }
        cut_records.append(record)
        if applied:
            audit_lines.append(
                f"转场[{i + 1}/{len(cuts)}] {record['transition']}({record['xfade']}) "
                f"{record['duration_s']:.2f}s | 输出帧 {record['before_frame']}→"
                f"{record['after_frame']}（{record['duration_frames']} 帧） | "
                f"原因：{record['reason']}")
        elif not cut["xfade"]:
            audit_lines.append(
                f"硬切[{i + 1}/{len(cuts)}] 无转场 | 输出帧 {record['before_frame']}"
                f"→{record['after_frame']} | 原因：{record['reason']}")
        else:
            audit_lines.append(
                f"⚠️ 转场[{i + 1}/{len(cuts)}] 计划 {record['requested']} 未生效"
                f"（回退流复制） | 原因：{record['reason']}")
    audit_lines.append(
        f"时长账本：Σ段 {sum(durations):.2f}s − 重叠 {ledger['overlap_total_s']:.2f}s "
        f"= 预期 {ledger['expected_duration_s']:.2f}s；实测 "
        f"{float(actual_duration or 0.0):.2f}s")
    audit.extend(audit_lines)

    out_report.update({
        "executed": True,
        "engine": engine,
        "audio_engine": audio_engine,
        "fallback": fallback_all,
        "fell_back_from_chain": bool(falling_back),
        "segments": len(files),
        "cut_count": len(cuts),
        "fps": fps,
        "use_audio": use_audio,
        "segment_durations_s": durations,
        "sum_segment_duration_s": round(sum(durations), 3),
        "overlap_total_s": ledger["overlap_total_s"],
        "expected_duration_s": ledger["expected_duration_s"],
        "actual_duration_s": actual_duration,
        "duration_delta_s": (round(float(actual_duration) - ledger["expected_duration_s"], 3)
                             if actual_duration else None),
        "output": str(output),
        "cuts": cut_records,
        "duration_ledger": ledger,
        "warnings": list(warnings),
        "audit_lines": audit_lines,
    })
    return output


def _crossfade(files: list[Path], out_path: Path, fade: float, *,
               warnings: list[str]) -> Path:
    """旧内部接口兼容：所有接缝均匀「淡入淡出」（由 splice_segments 统一实现）。"""
    durations, audios = _durations_and_audio(files, warnings)
    cuts = _normalize_transition_plan(
        len(files), durations, transition="淡入淡出", transition_plan=None,
        fade_seconds=0.0, transition_seconds=float(fade), warnings=warnings)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    video_only = out_path.with_name(out_path.stem + "_video_only.mp4")
    _render_video_single_pass(files, cuts, video_only, durations=durations)
    if all(audios):
        audio_only = out_path.with_name(out_path.stem + "_audio.m4a")
        try:
            _render_audio_mixed(files, cuts, audio_only)
            return _mux_audio(video_only, audio_only, out_path)
        except Exception as exc:  # noqa: BLE001 - 音频失败如实告警
            warnings.append(f"⚠️ 交叉淡化音频合成失败 → 输出无音轨（如实记录）: "
                            f"{type(exc).__name__}: {exc}")
    import os as _os

    _os.replace(video_only, out_path)
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

    options: {format_label, crf, transition_label, transition_plan,
              transition_seconds, subtitle_label, watermark_label, cover_label,
              watermark_text, fade_seconds, audio_join}

    ROUND55 转场扩展（向后兼容旧键）：
      * ``transition_label``：统一转场中文/英文标签（"无"/"淡入淡出"/"叠化"/"黑场"/
        "白场"/"左滑"/"右滑"/"上滑"/"下滑"/擦除/圆开/像素化/模糊/推近…）；
      * ``transition_plan``：逐接缝计划（list[str] 或 list[dict]），由
        ``bettercomfyui.pipeline.transition_planner`` 生成也可直接手写；
      * ``transition_seconds``：统一转场时长（钳制 0.1–2s，≤相邻段 90%）；
      * ``fade_seconds``：旧接口，>0 等价于所有接缝「淡入淡出」。
    ``report["transition"]`` 会写入引擎/时长账本/逐接缝帧号等结构化审计。
    """
    audit = audit if audit is not None else []
    warnings = warnings if warnings is not None else []
    work_dir = Path(work_dir)
    project_dir = Path(project_dir)
    files = [Path(f) for f in seg_files]
    report: dict[str, Any] = {"executed": False, "steps": [], "warnings": []}
    fmt = resolve_format(str(options.get("format_label") or "mp4"))
    transition = str(options.get("transition_label") or "无")
    transition_plan = options.get("transition_plan") or None
    transition_seconds = options.get("transition_seconds")
    subtitle = not str(options.get("subtitle_label") or "无").startswith("无")
    watermark = not str(options.get("watermark_label") or "无").startswith("无")
    cover = not str(options.get("cover_label") or "无").startswith("无")
    crf = int(options.get("crf") or 18)
    advanced = bool(subtitle or watermark or fmt != "MP4" or not transition.startswith("无")
                    or bool(transition_plan))

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
            if use_finalize and advanced:
                warnings.append(
                    "⚠️ 走Finalize剪辑=True，但本次启用了转场/字幕/水印/WebM 等高级项 → "
                    "Finalize 节点被绕过，由 director_edit 拼接封装（完整性自检改由后续 R_seg/R_full "
                    "与元数据检查承担；此为如实记录，不是静默忽略）")
                audit.append("Finalize：因高级项被绕过（转场/字幕/水印/WebM），已改用 director_edit 拼接封装")
                report["finalize_bypassed"] = True
            fade_seconds = float(options.get("fade_seconds") or DEFAULT_FADE_SECONDS) \
                if not transition.startswith("无") else 0.0
            transition_report: dict[str, Any] = {}
            resolved_transition = resolve_transition(transition, warnings=warnings)
            if transition_plan or resolved_transition:
                plan_seconds = float(transition_seconds) if transition_seconds is not None \
                    else float(fade_seconds or DEFAULT_TRANSITION_SECONDS)
                spliced = splice_segments(
                    files, workdir=work_dir, transition=transition,
                    transition_plan=transition_plan, transition_seconds=plan_seconds,
                    warnings=warnings, audit=audit, report=transition_report)
                used = ", ".join(
                    f"{c['transition']}({c['duration_s']:.2f}s)"
                    for c in transition_report.get("cuts", []) if c.get("applied"))
                report["steps"].append(
                    f"拼接 → {spliced}（转场引擎={transition_report.get('engine')}；"
                    f"生效={used or '无'}）")
            else:
                spliced = splice_segments(files, workdir=work_dir, fade_seconds=0.0,
                                          warnings=warnings, audit=audit,
                                          report=transition_report)
                report["steps"].append(f"拼接 → {spliced}（无转场，流复制）")
            report["transition"] = transition_report
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
    keys = ("format_label", "crf", "transition_label", "transition_plan",
            "transition_seconds", "subtitle_label",
            "watermark_label", "cover_label", "watermark_text", "fade_seconds")
    return {key: options.get(key) for key in keys}
