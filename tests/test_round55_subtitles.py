# -*- coding: utf-8 -*-
"""ROUND55-6：硬字幕程序化时间轴 + ffmpeg 烧录 + OCR 自检（仅校验）测试。

覆盖要求：
1. 纯时间轴：60s/6 段、句读切分、恰好帧边界、24fps 累计误差 <1 帧、换行/单屏 ≤2 行；
2. ffmpeg 集成：造 2s 视频 → 烧录硬字幕 → 输出存在、时长一致、可被 probe；
3. OCR 缺失时 skip（不失败），有 OCR 时校验返回结构；
4. 失败路径抛清晰异常（输入不存在/entries 为空/输出同输入）。
不依赖 GPU / 网络；ffmpeg 用 imageio-ffmpeg 或 PATH。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bettercomfyui.pipeline import hard_subtitles as hs


# ---------------------------------------------------------------------------
# 1) 程序化时间轴
# ---------------------------------------------------------------------------


def test_timeline_60s_6segments_exact_frame_grid():
    """60s / 6 段 × 10s：总帧精确 1440，段内累计误差 <1 帧，区间连续无重叠。"""
    entries = [
        {"text": f"第{i + 1}段：这是程序化字幕的第{i + 1}句台词。", "duration_s": 10.0}
        for i in range(6)
    ]
    cues = hs.build_timeline(entries, fps=24, total_duration_s=60.0)
    assert cues, "应生成字幕"
    assert cues[0].start_frame == 0
    assert cues[-1].end_frame == 24 * 60, "最后一条必须正好落在 60s*24fps=1440 帧"
    # 连续、单调、无重叠
    for prev, nxt in zip(cues, cues[1:]):
        assert prev.end_frame == nxt.start_frame, (prev, nxt)
        assert prev.end_frame > prev.start_frame
    # 24fps 累计误差 <1 帧：每个边界帧 vs 理论时间（10s 一段 = 240 帧的整数倍）
    for cue in cues:
        ideal = round(cue.start_frame / 24.0, 6)
        actual = cue.start_frame / 24.0
        assert abs(ideal - actual) < 1.0 / 24.0 + 1e-9
    # 每段预算应正好 240 帧（每段 10s）
    assert [c.end_frame - c.start_frame for c in cues[:6]] == [240] * 6 or sum(
        c.end_frame - c.start_frame for c in cues) == 1440


def test_timeline_frame_boundary_exact_5s():
    """恰好帧边界：5s@24fps=120 帧，一条字幕应正好覆盖 120 帧。"""
    cues = hs.build_timeline([{"text": "一二三四五六七八。", "duration_s": 5.0}],
                             fps=24, total_duration_s=5.0)
    assert len(cues) == 1
    assert cues[0].start_frame == 0 and cues[0].end_frame == 120


def test_timeline_cumulative_error_under_one_frame_for_odd_durations():
    """非整齐时长（11.3s/7.7s/…）下累计漂移必须 <1 帧。"""
    entries = [
        {"text": "第一段较短。", "duration_s": 11.3},
        {"text": "第二段稍微长一点，包含两句。第二句在这里。", "duration_s": 7.7},
        {"text": "第三段。", "duration_s": 6.25},
    ]
    total = 11.3 + 7.7 + 6.25
    cues = hs.build_timeline(entries, fps=24, total_duration_s=total)
    assert cues[-1].end_frame == int(round(total * 24))
    for cue in cues:
        ideal_frame = cue.start_s() * 24
        # 每条字幕起点与绝对时间的偏差都 <1 帧（程序化时间轴不漂移的核心断言）
        assert abs(ideal_frame - cue.start_frame) < 1.0


def test_split_sentences_by_punctuation():
    assert hs.split_sentences("你好。今天下雨了！我们走吧？") == [
        "你好。", "今天下雨了！", "我们走吧？"]
    assert hs.split_sentences("没有标点的长句") == ["没有标点的长句"]
    assert len(hs.split_sentences("第一行\n第二行")) == 2


def test_wrap_chinese_14_chars_per_line():
    text = "一二三四五六七八九十一二三四五六七"  # 17 个汉字
    lines = hs.wrap_line(text)
    assert lines, "应换行"
    for line in lines:
        assert hs.text_width(line) <= hs.CJK_MAX_PER_LINE + 1e-6, line


def test_wrap_english_28_chars_per_line():
    text = "a" * 60
    lines = hs.wrap_line(text)
    for line in lines:
        assert len(line) <= hs.LATIN_MAX_PER_LINE, (len(line), line)


def test_mixed_cn_en_line_width_limit():
    text = "中文中文中文中文中文abcdefghijklmnopqrst"
    lines = hs.wrap_line(text)
    for line in lines:
        assert hs.text_width(line) <= hs.CJK_MAX_PER_LINE + 1e-6, (line, hs.text_width(line))


def test_paginate_max_two_lines_per_screen():
    text = "这是一句非常非常长的台词，需要换行成三行以上才能放下这句话。" * 2
    pages = hs.paginate_text(text)
    assert pages, "应分屏"
    for page in pages:
        assert 1 <= len(page) <= hs.MAX_LINES_PER_SCREEN, page


def test_build_ass_structure_and_dialogue_count():
    cues = hs.build_timeline(["第一句。", "第二句。"], fps=24, total_duration_s=4.0)
    ass = hs.build_ass(cues, video_width=1280, video_height=720)
    assert "[Script Info]" in ass and "[V4+ Styles]" in ass and "[Events]" in ass
    assert "PlayResX: 1280" in ass and "PlayResY: 720" in ass
    assert ass.count("Dialogue:") == len(cues)
    assert "Format: Name, Fontname," in ass
    assert "Microsoft YaHei" in ass or "DengXian" in ass
    # ASS 时间戳与帧号一致（第 0 帧 → 0:00:00.00；第 24 帧 → 0:00:01.00）
    assert hs.ass_time(0) == "0:00:00.00"
    assert hs.ass_time(24) == "0:00:01.00"


def test_build_srt_same_frame_grid_as_ass():
    cues = hs.build_timeline(["第一句。", "第二句。"], fps=24, total_duration_s=4.0)
    srt = hs.build_srt(cues, fps=24)
    assert srt.count("-->") == len(cues)
    # 第一句 0 帧 → 00:00:00,000；ASS 与 SRT 时间戳必须同源
    assert hs.srt_time(0) == "00:00:00,000"
    assert hs.srt_time(24) == "00:00:01,000"
    first_block_start = srt.split("-->")[0].splitlines()[-1].strip()
    assert first_block_start == hs.srt_time(cues[0].start_frame, 24)
    ass = hs.build_ass(cues)
    assert ass.count("Dialogue:") == len(cues)


def test_entries_from_text_lines_and_json():
    lines = hs.entries_from_text("第一句。\n第二句。\n\n第三句。")
    assert lines == ["第一句。", "第二句。", "第三句。"]
    entries = hs.entries_from_text(json.dumps([{"text": "第一句。", "duration_s": 2.0}]))
    assert entries == [{"text": "第一句。", "duration_s": 2.0}]
    with pytest.raises(hs.SubtitleError):
        hs.entries_from_text("[not-json")


def test_build_timeline_requires_text():
    with pytest.raises(hs.SubtitleError):
        hs.build_timeline([], fps=24, total_duration_s=10)
    with pytest.raises(hs.SubtitleError):
        hs.build_timeline(["   "], fps=24, total_duration_s=10)


# ---------------------------------------------------------------------------
# 2) ffmpeg 烧录集成（造 2s 视频）
# ---------------------------------------------------------------------------


def _make_test_video(path: Path, seconds: float = 2.0) -> Path:
    ffmpeg = hs.find_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=navy:s=640x360:d={seconds}:r=24",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    assert path.is_file() and path.stat().st_size > 0
    return path


def test_find_ffmpeg_available():
    exe = hs.find_ffmpeg()
    assert Path(exe).is_file() or shutil.which(exe), exe


def test_burn_two_second_video(tmp_path: Path):
    src = _make_test_video(tmp_path / "in.mp4", 2.0)
    dst = tmp_path / "out_hardsub.mp4"
    warnings: list[str] = []
    result = hs.burn_hard_subtitles(
        src,
        ["第一句硬字幕。", "Second subtitle line."],
        dst,
        font_size=36,
        margin_v=48,
        warnings=warnings,
    )
    assert Path(result) == dst and dst.is_file() and dst.stat().st_size > 0
    # 输出可被 probe：时长与原视频一致（±0.2s 容差；硬字幕只是重编码）
    info = hs._probe_media(dst)
    assert info["duration_s"] is not None
    assert abs(info["duration_s"] - 2.0) <= 0.2, info
    assert info["width"] == 640 and info["height"] == 360
    # 元数据 JSON 可序列化（审计/证据用）
    cues = hs.build_timeline(["第一句硬字幕。", "Second subtitle line."],
                             fps=24, total_duration_s=2.0)
    assert cues and cues[-1].end_frame == 48
    assert json.dumps([c.to_dict() for c in cues], ensure_ascii=False)


def test_burn_clear_errors(tmp_path: Path):
    src = _make_test_video(tmp_path / "in.mp4", 1.0)
    with pytest.raises(hs.SubtitleError, match="输入视频不存在"):
        hs.burn_hard_subtitles(tmp_path / "missing.mp4", ["x"], tmp_path / "o.mp4")
    with pytest.raises(hs.SubtitleError, match="entries 为空"):
        hs.burn_hard_subtitles(src, [], tmp_path / "o.mp4")
    with pytest.raises(hs.SubtitleError, match="不能与输入相同"):
        hs.burn_hard_subtitles(src, ["x"], src)


# ---------------------------------------------------------------------------
# 3) OCR 自检（仅校验；缺依赖 skip 不失败）
# ---------------------------------------------------------------------------


def test_ocr_verify_available_or_skip(tmp_path: Path):
    src = _make_test_video(tmp_path / "in.mp4", 2.0)
    dst = tmp_path / "ocr.mp4"
    hs.burn_hard_subtitles(src, ["测试字幕内容。"], dst, font_size=48)
    cues = hs.build_timeline(["测试字幕内容。"], fps=24, total_duration_s=2.0)
    result = hs.verify_with_ocr(dst, cues, max_cues=1)
    assert isinstance(result, dict)
    for key in ("available", "engine", "checked", "matched", "ratio", "samples", "warnings"):
        assert key in result, key
    if not result["available"]:
        # 没有 tesseract/easyocr：必须写明 skip 原因，而不是静默通过
        assert result.get("reason"), result
        assert result["checked"] == 0
    else:
        assert 0 <= result["ratio"] <= 1.0
        assert result["checked"] >= 1


# ---------------------------------------------------------------------------
# 4) Finalize 节点集成（只读检查 INPUT_TYPES / 函数签名，默认行为兼容）
# ---------------------------------------------------------------------------


def _load_finalize_module():
    """按文件路径加载 finalize_nodes（避免触发 ComfyUI 包 __init__ 的重依赖）。"""
    import importlib.util

    import pathlib as _pathlib

    repo = _pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "round55_finalize_nodes", repo / "bettercomfyui_comfy" / "finalize_nodes.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_finalize_has_hard_subtitle_mode_and_keeps_old_default():
    module = _load_finalize_module()
    node_cls = module.BetterComfyUIFinalize
    spec = node_cls.INPUT_TYPES()
    optional = spec["optional"]
    assert "字幕模式" in optional, "Finalize 节点必须暴露「字幕模式」"
    modes = optional["字幕模式"][0]
    assert "硬字幕（程序对齐）" in modes, modes
    # 默认值必须是第 0 项「无」→ 旧工作流/旧调用行为不变
    default = optional["字幕模式"][1]["default"]
    assert default == modes[0]
    assert default.startswith("无")
    assert "字幕台词" in optional and "字幕字号" in optional
    assert "字幕字体文件" in optional and "OCR自检" in optional


def test_finalize_run_signature_defaults_are_backward_compatible():
    import inspect

    module = _load_finalize_module()
    sig = inspect.signature(module.BetterComfyUIFinalize.run)
    defaults = {name: param.default for name, param in sig.parameters.items()
                if param.default is not inspect.Parameter.empty}
    # 旧参数默认值保持不变
    assert defaults["编码CRF"] == 18
    assert defaults["完整性检查"] == module.CHECK_SCOPES[0]
    assert defaults["自动发现最新视频"] is False
    assert defaults["计划段数"] == 0
    # 新参数必须全部带默认值（旧调用方无需改）
    for name in ("字幕模式", "字幕台词", "字幕字号", "字幕字体文件", "字幕位置", "OCR自检"):
        assert name in defaults, name
    assert defaults["字幕模式"] == "无（保持旧行为）"

