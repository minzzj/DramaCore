"""验收 36：成片质量审核 CLI（一条命令回答：剪辑过吗/完整吗/质量高吗）。

用户要求：SSH 到机后要检查「有没有经过剪辑、视频是否完整、质量是否高」。
本验收锁住 scripts/quality_audit.py 的口径：

    P1 正常成片（2 段拼接）→ 退出码 0，结论「通过」，完整性/段数核对通过；
    P2 纯色+无音轨占位视频 → 退出码 1，原因含「占位/无音轨」，不冒充通过；
    P3 计划段数与实际不符 → 退出码 1，原因含「段数不一致」；
    P4 报告 Markdown+JSON 落盘，含完整性/剪辑性/质量/占位四节，且注明边界。

诚实边界：质量分是本地启发式（真实帧统计，非语义模型）；语义质量需人工/语义模型。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "quality_audit.py"


def _ffmpeg() -> str:
    sys.path.insert(0, str(REPO_ROOT))
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    return _ffmpeg_exe()


def _make_av(path: Path, seconds: float, *, fps: int = 24) -> Path:
    cmd = [_ffmpeg(), "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-c:a", "aac", "-b:a", "96k", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：ffmpeg 合成失败：{(proc.stderr or '')[-200:]}")
    return path


def _make_color(path: Path, seconds: float = 2.0) -> Path:
    cmd = [_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
           "-i", f"color=c=gray:s=320x240:d={seconds}:r=24",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：ffmpeg 合成失败：{(proc.stderr or '')[-200:]}")
    return path


def _concat(segments: list[Path], out: Path) -> Path:
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in segments),
                       encoding="utf-8")
    proc = subprocess.run([_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
                           "-i", str(listing), "-c", "copy", str(out)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：拼接失败：{(proc.stderr or '')[-200:]}")
    return out


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("quality_audit")
    segs = [_make_av(tmp / "s1.mp4", 2.0), _make_av(tmp / "s2.mp4", 3.0)]
    final = _concat(segs, tmp / "final.mp4")
    return {"tmp": tmp, "segments": segs, "final": final}


def _run(video: Path, *extra: str) -> tuple[int, dict, str]:
    out = video.with_name(video.stem + "_report.md")
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--video", str(video), *extra,
         "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    report_json = out.with_suffix(".json")
    data = json.loads(report_json.read_text(encoding="utf-8")) if report_json.is_file() else {}
    return proc.returncode, data, out.read_text(encoding="utf-8") if out.is_file() else ""


def test_p1_good_final_passes(clips):
    code, data, text = _run(
        clips["final"],
        "--segments", *[str(p) for p in clips["segments"]],
        "--expect-segments", "2", "--expect-duration", "5.0",
        "--score-threshold", "0")
    assert code == 0, f"正常拼接成片应通过，实际 {code}：{data.get('reasons')}"
    assert data["verdict"] == "通过"
    assert data["duration_check"]["ok"] is True
    assert data["segment_check"]["ok"] is True
    assert data["editing"]["black"]["segments"] <= 1
    assert data["placeholder_flags"] == []


def test_p2_placeholder_video_fails(clips):
    gray = _make_color(clips["tmp"] / "gray.mp4")
    code, data, _text = _run(gray)
    assert code == 1, "占位视频必须判不通过"
    assert data["verdict"] == "不通过"
    joined = "；".join(data["reasons"])
    assert "占位" in joined or "无音轨" in joined or "近单色" in joined


def test_p3_segment_count_mismatch_fails(clips):
    code, data, _text = _run(
        clips["final"],
        "--segments", *[str(p) for p in clips["segments"]],
        "--expect-segments", "5",
        "--score-threshold", "0")
    assert code == 1, "计划段数不符必须判不通过"
    assert any("段数不一致" in r for r in data["reasons"])


def test_p4_report_has_all_sections_and_limits(clips):
    _code, data, text = _run(
        clips["final"],
        "--segments", *[str(p) for p in clips["segments"]],
        "--score-threshold", "0")
    for section in ("完整性", "剪辑性", "质量", "占位", "边界说明"):
        assert section in text, f"报告缺少章节：{section}"
    assert "不检测语义质量" in text
    assert "quick" in json.dumps(data.get("limits", []), ensure_ascii=False).lower() or \
           "快速模式" in text
    assert "verdict" in data and "checked_at" in data
