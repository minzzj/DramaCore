"""回归：整片审核 R1b「音画同步」必须是**真实测量**，不能是常量占位分。

## 为什么要有这个文件（审计 R1 发现）

`local_scorer.video_scores()` 历史实现里：

    if info["has_audio"]:
        sync = 100.0 if duration > 0 else 0.0
        sync = 80.0          # ← 无条件覆盖成常量 80

只要文件"有音轨"，音画同步恒为 80 分（阈值默认 70 → 永远通过），
无论音频被截断、错位还是与视频时长对不上。这是「用常量冒充审核通过」。

修复后：真实测量视频流/音频流各自时长（`-c copy -f null` 取最后时间戳），
时长差先扣一帧容差，再按长流 10% 归零线性映射。本文件锁住该契约：

    P1 音视频等长的成片：同步分高（≥90）；
    P2 音频被截断（4s 视频 + 1s 音频）：同步分低（≤30），与 P1 拉开 ≥50 分；
    P3 无音轨：同步分 0（不编分）；
    P4 同步分不是常量：P1/P2 两个真实输入必须得到不同分数。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402


def _ffmpeg() -> str:
    try:
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        return _ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"未验证：没有可用的 ffmpeg 二进制：{type(exc).__name__}: {exc}")


def _make_av(path: Path, *, video_s: float, audio_s: float | None, fps: int = 10) -> Path:
    """用 lavfi 合成可复现的测试视频；audio_s=None 表示无音轨。"""
    cmd = [
        _ffmpeg(), "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate={fps}:duration={video_s}",
    ]
    if audio_s is not None:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_s}"]
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：本机 ffmpeg 合成测试视频失败：{(proc.stderr or '')[-300:]}")
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("av_sync")
    return {
        "matched": _make_av(tmp / "matched.mp4", video_s=3, audio_s=3),
        "desync": _make_av(tmp / "desync.mp4", video_s=4, audio_s=1),
        "noaudio": _make_av(tmp / "noaudio.mp4", video_s=2, audio_s=None),
    }


def test_audio_video_sync_is_measured_not_constant(clips):
    from bettercomfyui.review import local_scorer

    matched = local_scorer.video_scores(clips["matched"])["音画同步"]
    desync = local_scorer.video_scores(clips["desync"])["音画同步"]

    assert matched >= 90.0, f"音视频等长的成片音画同步分过低：{matched}（测量失真）"
    assert desync <= 30.0, (
        f"音频只有 1s、视频 4s 却给 {desync} 分 —— 音画同步没有真实测量")
    assert matched - desync >= 50.0, (
        f"两种真实不同的输入只差 {matched - desync} 分，仍像常量（历史缺陷是恒 80）")
    assert matched != 80.0 or desync != 80.0, "同步分不得是历史常量 80"


def test_no_audio_track_scores_zero(clips):
    from bettercomfyui.review import local_scorer

    scores = local_scorer.video_scores(clips["noaudio"])
    assert scores["音画同步"] == 0.0, (
        f"无音轨时必须如实给 0 分，实际 {scores['音画同步']}")


def test_stream_end_measurement_reports_missing_stream(clips):
    """缺流必须返回 None，绝不能编一个时长。"""
    from bettercomfyui.review import local_scorer

    assert local_scorer._stream_end_seconds(clips["noaudio"], "0:a:0") is None
    video_end = local_scorer._stream_end_seconds(clips["noaudio"], "0:v:0")
    assert video_end is not None and 1.5 <= video_end <= 2.5, (
        f"视频流最后时间戳测量异常：{video_end}")
