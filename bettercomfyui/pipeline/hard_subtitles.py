"""硬字幕：**程序化时间轴** + ASS 生成 + ffmpeg 烧录 + 抽帧 OCR 自检。

用户要求（ROUND55）：硬字幕用「程序方法」直接加入成片，避免模型/ASR 自由发挥导致
文字漂移；文字识别（OCR）**只做烧录后的校验**，不参与生成。

设计边界（务必保持）：
* 时间轴**全部由本模块程序生成**：输入段/台词列表 → 句读切分 → 按可见字符宽度
  分配时长 → 帧网格对齐（cumulative round，24fps 累计误差 < 1 帧）；
* 中英混排换行：每行 ≤14 个汉字宽度（ASCII 记 0.5，即 ≤28 英文字符），单屏 ≤2 行；
* 烧录用 ffmpeg `ass` 滤镜；Windows 字体回退 msyh（微软雅黑）→ DengXian（等线）→
  SimHei/SimSun，Linux 回退 WenQuanYi/Noto；字体文件缺失时降级并写 warnings，不静默；
* OCR 仅做校验：有 tesseract 或 easyocr 才跑，否则返回 ``available=False`` 并写明原因，
  **不阻断**、不抛错；
* 不依赖任何模型/网络/GPU，可直接在 pytest 中用 ffmpeg 造 2s 视频验证。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "CJK_MAX_PER_LINE",
    "LATIN_MAX_PER_LINE",
    "MAX_LINES_PER_SCREEN",
    "Cue",
    "SubtitleError",
    "ass_time",
    "build_srt",
    "burn_hard_subtitles",
    "build_ass",
    "build_timeline",
    "entries_from_text",
    "find_ffmpeg",
    "paginate_text",
    "pick_font",
    "split_sentences",
    "text_width",
    "verify_with_ocr",
    "wrap_line",
    "srt_time",
    "write_ass",
    "write_srt",
]

#: 每行最多 14 个「汉字宽度」（ASCII 记 0.5 宽 → 最多 28 个英文字符）。
CJK_MAX_PER_LINE = 14
LATIN_MAX_PER_LINE = 28
#: 单屏最多 2 行；超过则拆成下一条字幕（不缩小字号、不叠字）。
MAX_LINES_PER_SCREEN = 2

#: 句读切分字符（中英句末标点；省略号「……」视为句内停顿，不切句）。
_SENTENCE_END = set("。！？!?；;")
#: 换行时优先断开的字符（英文空格/连字符）。
_BREAK_CHARS = set(" \t-—、，,")


class SubtitleError(RuntimeError):
    """硬字幕生成/烧录失败（调用方必须看到真实原因，不静默降级）。"""


@dataclass(frozen=True)
class Cue:
    """一条字幕：帧区间 + 已换行的文本行（帧号左闭右开）。"""

    start_frame: int
    end_frame: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def start_s(self, fps: float = 24.0) -> float:
        return self.start_frame / float(fps)

    def end_s(self, fps: float = 24.0) -> float:
        return self.end_frame / float(fps)

    def to_dict(self, fps: float = 24.0) -> dict[str, Any]:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_s": round(self.start_s(fps), 3),
            "end_s": round(self.end_s(fps), 3),
            "lines": list(self.lines),
        }


# ---------------------------------------------------------------------------
# 字符宽度 / 句读切分 / 换行分页
# ---------------------------------------------------------------------------


def _is_wide(ch: str) -> bool:
    """是否按「汉字宽度=1」计（CJK/全角/日韩/表情等）。"""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return True
    return ord(ch) >= 0x2E80


def text_width(text: str) -> float:
    """可见宽度：汉字/全角 = 1，ASCII 等 = 0.5（用于 14字/28英文 的统一度量）。"""
    return sum(1.0 if _is_wide(ch) else 0.5 for ch in text)


def split_sentences(text: str) -> list[str]:
    """按句读切分（保留标点）；无标点的长句原样返回一条。"""
    raw = str(text or "")
    if "\n" in raw or "\r" in raw:
        # 换行本身就是句读边界（先按行切，再对每行做标点切分）
        pieces: list[str] = []
        for line in re.split(r"[\r\n]+", raw):
            pieces.extend(split_sentences(line))
        return [piece for piece in pieces if piece]
    normalized = re.sub(r"\s+", " ", raw).strip()
    if not normalized:
        return []
    sentences: list[str] = []
    buf: list[str] = []
    for ch in normalized:
        buf.append(ch)
        if ch in _SENTENCE_END:
            piece = "".join(buf).strip()
            if piece:
                sentences.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def wrap_line(text: str, *, max_cjk: int = CJK_MAX_PER_LINE) -> list[str]:
    """把一句文本按可见宽度换行（每行 ≤max_cjk 汉字宽）。"""
    text = str(text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    current: list[str] = []
    width = 0.0

    def flush() -> None:
        nonlocal current, width
        piece = "".join(current).strip()
        if piece:
            lines.append(piece)
        current, width = [], 0.0

    for ch in text:
        ch_w = 1.0 if _is_wide(ch) else 0.5
        if width + ch_w > max_cjk + 1e-6 and current:
            # 英文尽量在空格/连字符处断行：回退到最近的可断点
            if not _is_wide(ch) and ch not in _BREAK_CHARS:
                cut = -1
                for idx in range(len(current) - 1, -1, -1):
                    if current[idx] in _BREAK_CHARS:
                        cut = idx
                        break
                if cut >= 0:
                    carry = current[cut + 1:]
                    current = current[:cut + 1]
                    flush()
                    current = carry
                    width = sum(1.0 if _is_wide(c) else 0.5 for c in current)
                else:
                    flush()
            else:
                flush()
        current.append(ch)
        width += ch_w
    flush()
    return lines


def paginate_text(text: str, *, max_lines: int = MAX_LINES_PER_SCREEN) -> list[tuple[str, ...]]:
    """一句/一段文本 → 若干屏（每屏 ≤max_lines 行）。"""
    pages: list[tuple[str, ...]] = []
    for sentence in split_sentences(text):
        lines = wrap_line(sentence)
        for i in range(0, len(lines), max_lines):
            chunk = tuple(lines[i:i + max_lines])
            if chunk:
                pages.append(chunk)
    return pages


def _page_weight(page: Sequence[str]) -> float:
    return max(1.0, sum(text_width(line) for line in page))


# ---------------------------------------------------------------------------
# 程序化时间轴（帧网格对齐）
# ---------------------------------------------------------------------------


@dataclass
class _Page:
    lines: tuple[str, ...]
    entry_index: int
    weight: float


def _entry_pages(entry: Any) -> list[tuple[str, ...]]:
    if isinstance(entry, str):
        return paginate_text(entry)
    if isinstance(entry, dict):
        text = str(entry.get("text") or entry.get("line") or entry.get("dialogue") or "")
        return paginate_text(text)
    raise SubtitleError(f"字幕条目类型不支持：{type(entry).__name__}（只接受 str 或 dict）")


def _entry_budget_frames(entry: Any, fps: float) -> int | None:
    """显式时长/帧数（dict 可选 duration_s/frames）；str 返回 None。"""
    if not isinstance(entry, dict):
        return None
    if entry.get("duration_s") is not None:
        return max(1, int(round(float(entry["duration_s"]) * fps)))
    if entry.get("frames") is not None:
        return max(1, int(entry["frames"]))
    if entry.get("start_s") is not None and entry.get("end_s") is not None:
        return max(1, int(round((float(entry["end_s"]) - float(entry["start_s"])) * fps)))
    return None


def _allocate_frames(weights: list[float], budget: int) -> list[int]:
    """把 budget 帧按权重分配给若干条，保证整数、总和=budget、每条≥1 帧。"""
    count = len(weights)
    if count == 0:
        return []
    if budget < count:
        budget = count
    total = sum(max(w, 0.01) for w in weights)
    frames: list[int] = []
    consumed = 0
    for idx, weight in enumerate(weights):
        if idx == count - 1:
            piece = budget - consumed
        else:
            piece = int(round(budget * max(weight, 0.01) / total))
            piece = max(1, piece)
            # 给剩余条目留至少 1 帧
            piece = min(piece, budget - consumed - (count - idx - 1))
        frames.append(max(1, piece))
        consumed += frames[-1]
    # 数值保护：多退少补到末尾
    diff = budget - sum(frames)
    frames[-1] += diff
    if frames[-1] < 1:
        frames[-1] = 1
    return frames


def build_timeline(
    entries: Sequence[Any],
    *,
    fps: float = 24.0,
    total_duration_s: float | None = None,
    total_frames: int | None = None,
    min_cue_s: float = 0.5,
    max_cue_s: float = 10.0,
) -> list[Cue]:
    """把段/台词列表程序化为字幕后时间轴（帧网格，cumulative round）。

    参数
    ----
    entries:
        ``str`` = 一句/一段台词（自动句读切分 + 换行 + 分屏）；
        ``dict`` = 可选 ``{"text": str, "duration_s": float}`` / ``{"frames": int}``，
        用于按「每段时间轴」分配（无显式时长的条目按字符权重分享剩余时长）。
    fps: 帧率（帧网格对齐；24fps 下累计误差 < 1 帧）。
    total_duration_s / total_frames: 目标总时长；给定时最后一条正好落在总帧数上。
    min_cue_s / max_cue_s: 单条字幕最短/最长时长（仅对"模型预算"做约束，不改变帧网格）。

    返回 ``list[Cue]``：帧区间连续、无重叠，最后一条 end_frame == 总帧数（给定时）。
    """
    frame_rate = float(fps or 24.0)
    if frame_rate <= 0:
        raise SubtitleError(f"fps 必须 > 0，收到 {fps!r}")
    total_frames_explicit = int(total_frames) if total_frames else None
    if total_frames_explicit is None and total_duration_s is not None:
        total_frames_explicit = int(round(float(total_duration_s) * frame_rate))
    if total_frames_explicit is not None and total_frames_explicit <= 0:
        raise SubtitleError(f"总帧数必须 > 0，收到总时长={total_duration_s!r}s / 帧数={total_frames!r}")

    # 1) 每条目 → 分屏页 + 权重 + 预算
    groups: list[dict[str, Any]] = []
    pages_all: list[_Page] = []
    for entry_index, entry in enumerate(entries or []):
        pages = _entry_pages(entry)
        if not pages:
            continue
        budget = _entry_budget_frames(entry, frame_rate)
        page_objs = [_Page(lines=p, entry_index=entry_index, weight=_page_weight(p)) for p in pages]
        groups.append({"entry_index": entry_index, "pages": page_objs, "budget": budget})
        pages_all.extend(page_objs)
    if not pages_all:
        raise SubtitleError("没有可用的字幕文本（entries 为空或全部无文字）")

    # 2) 计算每条目分到的帧预算
    explicit_total = sum(g["budget"] for g in groups if g["budget"])
    implicit_groups = [g for g in groups if not g["budget"]]
    group_budgets: dict[int, int] = {}
    if total_frames_explicit is not None:
        remaining = total_frames_explicit - explicit_total
        if remaining < len(implicit_groups):
            # 显式时长已经吃满/超过总时长：按比例压缩（保证总帧数一致）
            scale = total_frames_explicit / max(1, explicit_total)
            for idx, group in enumerate(groups):
                if group["budget"]:
                    group_budgets[idx] = max(len(group["pages"]), int(round(group["budget"] * scale)))
            remaining = total_frames_explicit - sum(group_budgets.values())
        weights = [sum(p.weight for p in group["pages"]) for group in implicit_groups]
        frames = _allocate_frames(weights, max(len(implicit_groups), remaining)) if implicit_groups else []
        for group, count in zip(implicit_groups, frames):
            group_budgets[group["entry_index"]] = count
        for group in groups:
            if group["budget"]:
                group_budgets.setdefault(group["entry_index"], int(group["budget"]))
        # 兜底：总帧数对齐
        delta = total_frames_explicit - sum(group_budgets.values())
        if delta != 0:
            last_key = groups[-1]["entry_index"]
            group_budgets[last_key] = max(len(groups[-1]["pages"]), group_budgets[last_key] + delta)
    else:
        for group in groups:
            budget = group["budget"]
            if not budget:
                # 未给总时长：按每屏 1.5s 估算（min/max 约束），只用于"能跑"，精确对齐请传总时长
                per_page = max(1, int(round(1.5 * frame_rate)))
                budget = per_page * len(group["pages"])
            min_frames = max(1, int(round(min_cue_s * frame_rate)))
            max_frames = max(min_frames, int(round(max_cue_s * frame_rate)) * len(group["pages"]))
            group_budgets[group["entry_index"]] = int(min(max_frames, max(min_frames, budget)))

    # 3) 条目内部按页权重分配 → cumulative round 对齐帧网格
    cues: list[Cue] = []
    cursor = 0
    for group in groups:
        pages = group["pages"]
        budget = group_budgets[group["entry_index"]]
        weights = [p.weight for p in pages]
        page_frames = _allocate_frames(weights, budget)
        # 每条至少 1 帧，且总预算不超；cumulative round 保证误差 <1 帧
        offset = 0
        for page, frame_count in zip(pages, page_frames):
            offset += frame_count
            end_frame = cursor + offset
            cues.append(Cue(start_frame=cursor + offset - frame_count,
                            end_frame=end_frame, lines=tuple(page.lines)))
        cursor += budget
    # 强制首尾连续 + 总帧数精确
    if total_frames_explicit is not None and cues:
        cues = [Cue(c.start_frame, c.end_frame, c.lines) for c in cues]
        if cues[-1].end_frame != total_frames_explicit:
            last = cues[-1]
            cues[-1] = Cue(last.start_frame, total_frames_explicit, last.lines)
    return [c for c in cues if c.end_frame > c.start_frame]


# ---------------------------------------------------------------------------
# ASS 生成 / 时间格式
# ---------------------------------------------------------------------------


def ass_time(frame: int, fps: float = 24.0) -> str:
    """帧号 → ASS 时间戳 H:MM:SS.cc（以帧为唯一真源，1/100 秒仅显示）。"""
    total_cs = int(round(int(frame) * 100.0 / float(fps or 24.0)))
    hours, rem = divmod(total_cs, 360000)
    minutes, rem = divmod(rem, 6000)
    seconds, centis = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _ass_escape(text: str) -> str:
    return (str(text or "")
            .replace("\\", r"\\")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("\n", r"\N"))


def build_ass(
    cues: Sequence[Cue],
    *,
    video_width: int = 1920,
    video_height: int = 1080,
    font_family: str = "Microsoft YaHei",
    font_size: int = 42,
    margin_v: int = 64,
    alignment: int = 2,
    style_name: str = "DramaCoreHardSub",
    bold: bool = False,
    outline: int = 2,
) -> str:
    """Cue 列表 → ASS 文本（白字黑边、底部居中；样式名可与 force_style 配合）。"""
    if not cues:
        raise SubtitleError("build_ass 需要至少一条 Cue")
    width = max(16, int(video_width or 1920))
    height = max(16, int(video_height or 1080))
    header = [
        "[Script Info]",
        "; 剧核 / DramaCore 硬字幕（程序化时间轴，非模型生成）",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {style_name},{font_family},{int(font_size)},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H64000000,{-1 if bold else 0},0,0,0,100,100,0,0,1,{int(outline)},0,{int(alignment)},40,40,{int(margin_v)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for cue in cues:
        text = _ass_escape("\n".join(cue.lines))
        events.append(
            f"Dialogue: 0,{ass_time(cue.start_frame)},{ass_time(cue.end_frame)},"
            f"{style_name},,0,0,0,,{text}"
        )
    return "\n".join(header + events) + "\n"


def write_ass(cues: Sequence[Cue], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_ass(cues), encoding="utf-8")
    return target


def srt_time(frame: int, fps: float = 24.0) -> str:
    """帧号 → SRT 时间戳 HH:MM:SS,mmm（与帧网格同源，仅供兼容 .srt 消费方）。"""
    total_ms = int(round(int(frame) * 1000.0 / float(fps or 24.0)))
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt(cues: Sequence[Cue], *, fps: float = 24.0) -> str:
    """Cue 列表 → SRT 文本（与 ASS 同一套程序化帧网格；供不支持 ASS 的播放器/工具）。"""
    if not cues:
        raise SubtitleError("build_srt 需要至少一条 Cue")
    blocks = []
    for index, cue in enumerate(cues, start=1):
        text = "\n".join(cue.lines)
        blocks.append(f"{index}\n{srt_time(cue.start_frame, fps)} --> "
                      f"{srt_time(cue.end_frame, fps)}\n{text}\n")
    return "\n".join(blocks)


def write_srt(cues: Sequence[Cue], path: Path | str, *, fps: float = 24.0) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_srt(cues, fps=fps), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# ffmpeg / 字体
# ---------------------------------------------------------------------------


def find_ffmpeg() -> str:
    """定位 ffmpeg：环境变量 → PATH → imageio-ffmpeg 自带二进制。"""
    env = os.environ.get("FFMPEG_BIN") or os.environ.get("FFMPEG")
    if env and Path(env).is_file():
        return env
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # imageio-ffmpeg 是仓库已有依赖（review/local_scorer 同款策略）
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise SubtitleError(
            "找不到 ffmpeg：请把 ffmpeg 加入 PATH、设置环境变量 FFMPEG_BIN，"
            f"或安装 imageio-ffmpeg（{type(exc).__name__}: {exc}）"
        ) from exc


#: 字体文件名片段 → ASS 字体族名（libass 需要 family name）。
_FONT_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("msyhbd", "Microsoft YaHei"),
    ("msyh", "Microsoft YaHei"),
    ("dengxian", "DengXian"),
    ("deng", "DengXian"),
    ("simhei", "SimHei"),
    ("simsun", "SimSun"),
    ("wqy-zenhei", "WenQuanYi Zen Hei"),
    ("wqy-microhei", "WenQuanYi Micro Hei"),
    ("notosanscjk", "Noto Sans CJK SC"),
    ("noto sans cjk", "Noto Sans CJK SC"),
    ("sourcehansans", "Source Han Sans SC"),
)

#: 各平台候选字体文件（按优先级）。
_WINDOWS_FONT_CANDIDATES = ("msyh.ttc", "msyhbd.ttc", "Deng.ttf", "DengXian.ttf",
                            "simhei.ttf", "simsun.ttc")
_LINUX_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


def _family_for_font(path: Path) -> str | None:
    name = path.name.lower()
    for fragment, family in _FONT_FAMILY_HINTS:
        if fragment in name:
            return family
    return None


def pick_font(
    font_path: str | Path | None = None,
    warnings: list[str] | None = None,
) -> tuple[Path | None, str | None]:
    """选字体：显式 font_path → 平台默认回退（msyh/DengXian/WenQuanYi/Noto）。

    返回 ``(字体文件路径, ASS 字体族名)``；找不到时返回 ``(None, None)`` 并写 warning
    （调用方降级为 libass 默认字体，不阻断烧录）。
    """
    warnings = warnings if warnings is not None else []
    explicit = Path(font_path).expanduser() if font_path else None
    if explicit is not None:
        if explicit.is_file():
            return explicit, _family_for_font(explicit)
        warnings.append(f"指定字体文件不存在：{explicit}；改用系统字体回退")

    candidates: list[Path] = []
    if os.name == "nt":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend(windir / name for name in _WINDOWS_FONT_CANDIDATES)
    else:
        candidates.extend(Path(p) for p in _LINUX_FONT_CANDIDATES)
    # 兜底：常见用户字体目录（Linux/macOS）
    for extra_dir in ("/usr/share/fonts", "/usr/local/share/fonts",
                      str(Path.home() / ".fonts"), "/System/Library/Fonts"):
        base = Path(extra_dir)
        if base.is_dir():
            candidates.extend(sorted(base.rglob("*.tt[fc]"))[:20])
    for candidate in candidates:
        try:
            if candidate.is_file():
                family = _family_for_font(candidate)
                if family:
                    return candidate, family
        except OSError:
            continue
    warnings.append(
        "未找到中文字体（msyh/DengXian/WenQuanYi/Noto），硬字幕将使用 libass 默认字体；"
        "中文可能显示为方框，可通过 font_path 指定字体文件"
    )
    return None, None


# ---------------------------------------------------------------------------
# 烧录
# ---------------------------------------------------------------------------


def _probe_media(path: Path) -> dict[str, Any]:
    """ffmpeg -i 解析时长/分辨率/fps（不依赖 ffprobe 二进制）。"""
    proc = subprocess.run(
        [find_ffmpeg(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    info: dict[str, Any] = {"duration_s": None, "width": None, "height": None, "fps": None}
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        h, m, s = match.groups()
        info["duration_s"] = int(h) * 3600 + int(m) * 60 + float(s)
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if match:
        info["width"], info["height"] = int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if match:
        info["fps"] = float(match.group(1))
    return info


def _style_opts(style: str) -> tuple[bool, int]:
    """「白字黑边」等样式简写 → (是否粗体, 描边宽度)。样式写入 ASS 本体。"""
    text = str(style or "")
    bold = bool("粗" in text or "bold" in text.lower())
    outline = 3 if ("描边" in text or "outline" in text.lower()) else 2
    return bold, outline


def burn_hard_subtitles(
    video_in: Path | str,
    entries: Sequence[Any],
    video_out: Path | str,
    *,
    font_path: str | Path | None = None,
    font_size: int = 42,
    margin_v: int = 64,
    alignment: int = 2,
    style: str = "白字黑边",
    warnings: list[str] | None = None,
    fps: float | None = None,
    total_duration_s: float | None = None,
    video_width: int | None = None,
    video_height: int | None = None,
    crf: int = 18,
    preset: str = "veryfast",
) -> Path:
    """用 ffmpeg 把程序化字幕**烧录**进视频（硬字幕，不可关闭、不漂移）。

    参数
    ----
    video_in / video_out: 输入/输出视频路径（输出建议 .mp4）。
    entries: 字幕条目列表，元素为 ``str``（台词）或 ``dict``（可带 ``duration_s``/``frames``）。
    font_path: 字体文件；缺省按平台回退（msyh → DengXian → SimHei/SimSun / WenQuanYi / Noto）。
    font_size / margin_v / alignment / style: ASS 样式（白字黑边，底部居中）。
    warnings: 传入列表时追加非致命提示（字体回退、OCR 等由调用方自行记录）。
    fps / total_duration_s / video_width / video_height: 显式给定可跳过探测。
    crf / preset: 重编码参数（硬字幕必须重编码，不能流复制）。

    返回 ``Path(video_out)``；任何失败抛 :class:`SubtitleError`，不返回半成品。
    """
    warnings = warnings if warnings is not None else []
    src = Path(video_in)
    dst = Path(video_out)
    if not src.is_file():
        raise SubtitleError(f"输入视频不存在：{src}")
    if src.resolve() == dst.resolve():
        raise SubtitleError("输出视频不能与输入相同（硬字幕需重编码，请给新的输出路径）")
    if not entries:
        raise SubtitleError("硬字幕 entries 为空：请提供字幕台词（每行一句或 dict 列表）")

    meta = _probe_media(src)
    if not meta.get("duration_s"):
        raise SubtitleError(f"无法解析输入视频时长（文件损坏或不是视频）：{src}")
    frame_rate = float(fps or meta.get("fps") or 24.0)
    duration = float(total_duration_s or meta["duration_s"])
    width = int(video_width or meta.get("width") or 1920)
    height = int(video_height or meta.get("height") or 1080)

    cues = build_timeline(entries, fps=frame_rate, total_duration_s=duration)
    if not cues:
        raise SubtitleError("字幕时间轴为空（没有可烧录的文本）")

    font_file, font_family = pick_font(font_path, warnings)
    bold, outline = _style_opts(style)

    dst.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="dc_hardsub_", dir=str(dst.parent)))
    try:
        ass_path = workdir / "subs.ass"
        ass_path.write_text(
            build_ass(cues, video_width=width, video_height=height,
                      font_family=font_family or "Microsoft YaHei",
                      font_size=font_size, margin_v=margin_v, alignment=alignment,
                      bold=bold, outline=outline),
            encoding="utf-8",
        )
        # 字体文件复制进工作目录 + fontsdir=. → 路径不含 Windows 盘符冒号，
        # 避免 ffmpeg 滤镜参数转义问题（这是 Windows 上最容易踩的坑）。
        if font_file is not None:
            try:
                shutil.copy2(font_file, workdir / f"dcfont{font_file.suffix.lower()}")
            except OSError as exc:  # noqa: BLE001 - 复制失败降级为系统默认字体
                warnings.append(f"字体文件复制失败（改用 libass 默认字体）：{exc}")
        # ass 滤镜：样式（字体/字号/描边/位置）已写进 ASS 本体；fontsdir 指向工作目录
        vf = "ass=subs.ass:fontsdir=."
        cmd = [
            find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src.resolve()), "-vf", vf,
            "-c:v", "libx264", "-preset", str(preset), "-crf", str(int(crf)),
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
            str(dst.resolve()),
        ]
        proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise SubtitleError(
                "ffmpeg 烧录硬字幕失败（exit={}）：{}\n"
                "常见原因：字体缺失/ASS 路径含特殊字符/输入无视频流。".format(
                    proc.returncode, stderr[-1500:] or "(无 stderr)")
            )
        if not dst.is_file() or dst.stat().st_size <= 0:
            raise SubtitleError(f"ffmpeg 结束但没有输出文件：{dst}")
        out_meta = _probe_media(dst)
        if not out_meta.get("duration_s"):
            raise SubtitleError(f"输出视频不可探测（可能为空/损坏）：{dst}")
        if abs(float(out_meta["duration_s"]) - duration) > max(0.5, duration * 0.02):
            raise SubtitleError(
                f"烧录后时长异常：期望 {duration:.2f}s，实际 {out_meta['duration_s']:.2f}s")
        return dst
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# OCR 自检（仅校验；缺依赖则 skip）
# ---------------------------------------------------------------------------


def _ocr_engine() -> tuple[str | None, str | None]:
    """探测 OCR 引擎：tesseract CLI → easyocr。返回 (engine, reason)。"""
    exe = shutil.which("tesseract")
    if exe:
        return "tesseract", exe
    try:  # easyocr 体积大，按需 import
        import easyocr  # type: ignore  # noqa: F401

        return "easyocr", None
    except Exception:  # noqa: BLE001 - 没装就 skip，不报错
        return None, "未安装 OCR 依赖（tesseract 或 easyocr），烧录后文字校验跳过（skip）"


def _extract_frame(video: Path, t_s: float, out_png: Path) -> bool:
    cmd = [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, float(t_s)):.3f}", "-i", str(video),
           "-frames:v", "1", str(out_png)]
    proc = subprocess.run(cmd, capture_output=True)
    return proc.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0


def _norm_ocr_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def _tesseract_read(image: Path, exe: str) -> str:
    try:
        proc = subprocess.run(
            [exe, str(image), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if proc.returncode != 0:
            # 缺中文语言包时退回 eng，避免整次校验失败
            proc = subprocess.run(
                [exe, str(image), "stdout", "-l", "eng", "--psm", "6"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
        return proc.stdout or ""
    except Exception:  # noqa: BLE001 - OCR 失败按"未识别"处理，不阻断
        return ""


def verify_with_ocr(
    video: Path | str,
    cues: Sequence[Cue],
    *,
    keywords: Sequence[str] | None = None,
    max_cues: int = 4,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """烧录后抽帧 OCR 自检（**仅校验**，不参与字幕生成、不阻断流程）。

    返回：``{"available": bool, "engine": str|None, "checked": int, "matched": int,
    "ratio": float, "samples": [...], "warnings": [...]}``。
    无 tesseract/easyocr 时 ``available=False`` + ``reason``（写明 skip 原因）。
    """
    warnings = warnings if warnings is not None else []
    video_path = Path(video)
    engine, engine_reason = _ocr_engine()
    if engine is None:
        return {"available": False, "engine": None, "checked": 0, "matched": 0,
                "ratio": 0.0, "samples": [], "reason": engine_reason,
                "warnings": warnings}
    if not video_path.is_file() or not cues:
        warnings.append("OCR 自检跳过：视频不存在或字幕为空")
        return {"available": False, "engine": engine, "checked": 0, "matched": 0,
                "ratio": 0.0, "samples": [], "reason": "视频不存在或字幕为空",
                "warnings": warnings}

    picks = list(cues)[:max(1, int(max_cues))]
    samples: list[dict[str, Any]] = []
    matched = 0
    checked = 0
    tmpdir = Path(tempfile.mkdtemp(prefix="dc_ocr_", dir=str(video_path.parent)))
    try:
        for idx, cue in enumerate(picks):
            midpoint = cue.start_s() + max(0.05, (cue.end_s() - cue.start_s()) / 2.0)
            png = tmpdir / f"frame_{idx:02d}.png"
            if not _extract_frame(video_path, midpoint, png):
                warnings.append(f"OCR 抽帧失败（cue {idx}，t={midpoint:.2f}s）")
                continue
            if engine == "tesseract":
                text = _tesseract_read(png, engine_reason or "tesseract")
            else:  # pragma: no cover - easyocr 环境相关，CI 通常没有
                try:
                    import easyocr  # type: ignore

                    reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
                    text = " ".join(reader.readtext(str(png), detail=0))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"easyocr 识别失败：{type(exc).__name__}: {exc}")
                    text = ""
            checked += 1
            expected = keywords[idx] if keywords and idx < len(keywords) else "".join(cue.lines[0])
            expected_norm = _norm_ocr_text(expected)[:6]
            text_norm = _norm_ocr_text(text)
            hit = bool(expected_norm) and (expected_norm in text_norm
                                           or (len(set(expected_norm) & set(text_norm))
                                               / max(1, len(set(expected_norm))) >= 0.6))
            matched += 1 if hit else 0
            samples.append({"cue": cue.to_dict(), "expected": expected_norm,
                            "ocr_text": text_norm[:80], "matched": hit})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    ratio = round(matched / checked, 3) if checked else 0.0
    if not checked:
        warnings.append("OCR 自检未抽到有效帧")
    elif ratio < 0.6:
        warnings.append(
            f"OCR 自检命中率偏低（{matched}/{checked}={ratio:.0%}）：字幕可能被遮挡/裁切，"
            "或 OCR 中文语言包缺失（tesseract 需 chi_sim），仅提示不阻断")
    return {"available": True, "engine": engine, "checked": checked, "matched": matched,
            "ratio": ratio, "samples": samples, "reason": None, "warnings": warnings}


# ---------------------------------------------------------------------------
# 输入解析（Finalize 节点集成用）
# ---------------------------------------------------------------------------


def entries_from_text(raw: str) -> list[Any]:
    """把节点输入文本解析成 entries：

    * JSON 数组：``["台词1", {"text": "台词2", "duration_s": 2.0}]``；
    * 否则按行：每行一句台词（空行忽略）。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SubtitleError(f"字幕台词 JSON 解析失败：{exc.msg} (line {exc.lineno})") from exc
        if isinstance(data, dict):
            data = data.get("entries") or data.get("lines") or []
        if not isinstance(data, list):
            raise SubtitleError("字幕台词 JSON 顶层需为数组或 {entries:[...]}")
        return data
    return [line.strip() for line in text.splitlines() if line.strip()]
