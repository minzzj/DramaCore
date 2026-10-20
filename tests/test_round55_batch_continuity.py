# -*- coding: utf-8 -*-
"""ROUND55-3：跨批尾帧参考增强（近似，不冒充原生 pin）的单元测试。

覆盖：
1) 纯函数（无需 ffmpeg）：`plan_batch_continuity` / `suggest_overlap_frames` 的契约、
   首批无尾帧、上限 1..3、enabled=False、不改输入、**绝不出现 native pin 标记**；
2) ffmpeg 实测（无 ffmpeg 自动 skip）：造 0.5s 小视频验证 `extract_tail_frames`
   的数量/尺寸/顺序、缺失文件与坏文件的告警、`build_cross_batch_refs` 的返回契约。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from bettercomfyui.pipeline import batch_continuity as bc

HAS_FFMPEG = bc.ffmpeg_available()
needs_ffmpeg = pytest.mark.skipif(
    not HAS_FFMPEG,
    reason="系统无 ffmpeg（PATH 与 imageio-ffmpeg 均不可用），抽帧相关测试跳过",
)


# ---------------------------------------------------------------------------
# 测试辅助：造小视频 / 探测尺寸
# ---------------------------------------------------------------------------
def _make_video(path: Path, *, fps: int = 24, frames: int = 12,
                size: str = "64x48") -> Path:
    """用 ffmpeg lavfi 造一个 0.5s 左右的小视频（无 ffmpeg 时测试已 skip）。"""
    exe = bc.ffmpeg_exe()
    assert exe, "ffmpeg 不可用"
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
        "-t", f"{frames / fps:.3f}",
        "-frames:v", str(frames),
        "-pix_fmt", "yuv420p",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, f"造测试视频失败：{proc.stderr[-400:]}"
    assert path.is_file() and path.stat().st_size > 0
    return path


def _probe_size(path: Path) -> tuple[int, int] | None:
    exe = bc.ffmpeg_exe()
    proc = subprocess.run([str(exe), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", proc.stderr or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _make_three_videos(tmp_path: Path) -> list[Path]:
    seg_dir = tmp_path / "segs"
    return [_make_video(seg_dir / f"seg_{i:03d}.mp4") for i in range(1, 4)]


# ---------------------------------------------------------------------------
# 1) 纯函数：suggest_overlap_frames
# ---------------------------------------------------------------------------
def test_suggest_overlap_frames_basic() -> None:
    assert bc.suggest_overlap_frames(24, 500) == 12
    assert bc.suggest_overlap_frames(30, 500) == 15
    assert bc.suggest_overlap_frames(24, 1000) == 24
    assert bc.suggest_overlap_frames(24, 0) == 0
    assert isinstance(bc.suggest_overlap_frames(), int)


def test_suggest_overlap_frames_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        bc.suggest_overlap_frames(0, 500)
    with pytest.raises(ValueError):
        bc.suggest_overlap_frames(-24, 500)
    with pytest.raises(ValueError):
        bc.suggest_overlap_frames(24, -1)


# ---------------------------------------------------------------------------
# 2) 纯函数：plan_batch_continuity
# ---------------------------------------------------------------------------
def test_plan_batch_continuity_contract() -> None:
    batches = [(0, 2), (2, 4), (4, 5)]
    plan = bc.plan_batch_continuity(batches)

    assert plan["enabled"] is True
    assert plan["method"] == bc.METHOD == "tail_frames_approx"
    assert plan["continuity_mode"] == bc.CONTINUITY_MODE == "batch_tail_ref_approx"
    assert plan["native_pin"] is False
    assert plan["max_refs"] == 3
    assert len(plan["batches"]) == 3
    # 首批没有上一批 → 不挂尾帧参考
    first = plan["batches"][0]
    assert first["batch_index"] == 1
    assert first["segments"] == [1, 2]
    assert first["tail_ref_count"] == 0
    assert first["cross_batch_approx"] is False
    assert first["native_pin"] is False
    # 后续批：取上一批末 1-3 段
    assert plan["batches"][1]["tail_ref_count"] == 2
    assert plan["batches"][2]["tail_ref_count"] == 2
    assert all(r["native_pin"] is False for r in plan["batches"])
    assert plan["expected_tail_refs"] == 4
    assert plan["approx_batches"] == 2
    assert plan["audit"], "必须给出可审计说明"
    joined = "\n".join(plan["audit"])
    assert "tail_frames_approx" in joined
    assert "不是 vendor 原生 22 帧 pin" in joined


def test_plan_batch_continuity_tail_ref_capped_at_three() -> None:
    # 上一批 10 段，也只取最后 3 段尾帧
    plan = bc.plan_batch_continuity([(0, 10), (10, 12)])
    assert plan["batches"][0]["tail_ref_count"] == 0
    assert plan["batches"][1]["tail_ref_count"] == bc.MAX_TAIL_REFS == 3
    # 截断/上限不得变成 native
    assert plan["batches"][1]["continuity_mode"] == "batch_tail_ref_approx"
    assert plan["batches"][1]["native_pin"] is False


def test_plan_batch_continuity_disabled() -> None:
    plan = bc.plan_batch_continuity([(0, 3), (3, 6), (6, 9)], enabled=False)
    assert plan["enabled"] is False
    assert plan["method"] == "disabled"
    assert plan["continuity_mode"] == "disabled"
    assert all(r["tail_ref_count"] == 0 for r in plan["batches"])
    assert all(r["cross_batch_approx"] is False for r in plan["batches"])
    assert all(r["native_pin"] is False for r in plan["batches"])
    assert plan["expected_tail_refs"] == 0


def test_plan_batch_continuity_accepts_dict_batches() -> None:
    plan = bc.plan_batch_continuity(
        [{"start": 0, "end": 2}, {"segments": [3, 4]}]
    )
    assert [r["segments"] for r in plan["batches"]] == [[1, 2], [3, 4]]
    assert plan["batches"][1]["tail_ref_count"] == 2


def test_plan_batch_continuity_is_pure_and_never_native() -> None:
    batches = [(0, 2), (2, 5)]
    snapshot = json.dumps(batches, sort_keys=True)
    plan = bc.plan_batch_continuity(batches)
    assert json.dumps(batches, sort_keys=True) == snapshot, "纯函数不得修改入参"

    # 审计文本里可以"提到" continuityFromPrev（说明不写它），但批次结构里绝不能把它当字段写出去
    batches_blob = json.dumps(plan["batches"], ensure_ascii=False)
    assert "continuityFromPrev" not in batches_blob
    assert "native_pin\": true" not in batches_blob.lower()
    assert all(r["native_pin"] is False for r in plan["batches"])
    assert all(r["continuity_mode"] in ("batch_tail_ref_approx", "none_first_batch")
               for r in plan["batches"])


def test_plan_batch_continuity_empty_and_invalid() -> None:
    empty = bc.plan_batch_continuity([])
    assert empty["batches"] == []
    assert empty["expected_tail_refs"] == 0
    with pytest.raises(ValueError):
        bc.plan_batch_continuity([(2, 2)])
    with pytest.raises(ValueError):
        bc.plan_batch_continuity([(-1, 2)])


# ---------------------------------------------------------------------------
# 3) 无 ffmpeg 也能跑：空输入 / 参数校验的诚实降级
# ---------------------------------------------------------------------------
def test_build_cross_batch_refs_empty_input_is_honest() -> None:
    warnings: list[str] = []
    info = bc.build_cross_batch_refs([], Path("unused_round55_out"), warnings=warnings)
    assert info["refs"] == []
    assert info["method"] == "tail_frames_approx"
    assert info["native_pin"] is False
    assert info["ref_count"] == 0
    assert info["audit"], "空输入也要有可审计说明"
    assert any("跨批" in a or "尾帧" in a for a in info["audit"])


def test_extract_tail_frames_count_validation() -> None:
    with pytest.raises(ValueError):
        bc.extract_tail_frames([], Path("unused_round55_out"), count=0)
    with pytest.raises(ValueError):
        bc.extract_tail_frames([], Path("unused_round55_out"), count=-1)


# ---------------------------------------------------------------------------
# 4) ffmpeg 实测：抽尾帧数量/尺寸/顺序/告警
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_extract_tail_frames_one_per_segment(tmp_path: Path) -> None:
    videos = _make_three_videos(tmp_path)
    warnings: list[str] = []
    refs = bc.extract_tail_frames(videos, tmp_path / "tailrefs", count=3, warnings=warnings)

    assert len(refs) == 3, f"应每个末段各抽 1 帧，实际 {refs}"
    assert all(p.is_file() and p.stat().st_size > 0 for p in refs)
    assert [p.name for p in refs] == sorted(p.name for p in refs), "顺序应稳定（旧→新）"
    assert warnings == [], f"正常抽帧不应有告警：{warnings}"
    for p in refs:
        assert _probe_size(p) == (64, 48), f"尾帧尺寸应为测试视频 64x48：{p}"


@needs_ffmpeg
def test_extract_tail_frames_count_takes_last_segments(tmp_path: Path) -> None:
    videos = _make_three_videos(tmp_path)
    refs = bc.extract_tail_frames(videos, tmp_path / "tailrefs2", count=2)
    assert len(refs) == 2
    assert "seg_002" in refs[0].name and "seg_003" in refs[1].name


@needs_ffmpeg
def test_extract_tail_frames_count_clamped_and_warned(tmp_path: Path) -> None:
    videos = _make_three_videos(tmp_path)
    warnings: list[str] = []
    refs = bc.extract_tail_frames(videos, tmp_path / "tailrefs3", count=9,
                                  warnings=warnings)
    assert len(refs) == bc.MAX_TAIL_REFS == 3
    assert any("上限" in w for w in warnings), f"count 超上限必须告警：{warnings}"


@needs_ffmpeg
def test_extract_tail_frames_missing_file_warns(tmp_path: Path) -> None:
    videos = _make_three_videos(tmp_path)
    missing = tmp_path / "segs" / "seg_999.mp4"
    warnings: list[str] = []
    refs = bc.extract_tail_frames(videos + [missing], tmp_path / "tailrefs4",
                                  count=3, warnings=warnings)
    assert len(refs) == 3
    assert any("不存在/为空" in w or "跳过" in w for w in warnings), warnings


@needs_ffmpeg
def test_extract_tail_frames_bad_video_warns_not_silent(tmp_path: Path) -> None:
    good = _make_video(tmp_path / "segs" / "seg_001.mp4")
    bad = tmp_path / "segs" / "seg_002_broken.mp4"
    bad.write_bytes(b"not a video file")
    warnings: list[str] = []
    refs = bc.extract_tail_frames([good, bad], tmp_path / "tailrefs5",
                                  count=2, warnings=warnings)
    assert len(refs) >= 1, "好文件仍应抽出尾帧"
    assert warnings, "坏文件抽帧失败必须告警，不能静默"


@needs_ffmpeg
def test_build_cross_batch_refs_real_files(tmp_path: Path) -> None:
    videos = _make_three_videos(tmp_path)
    warnings: list[str] = []
    info = bc.build_cross_batch_refs(videos, tmp_path / "refs_b1",
                                     max_refs=3, warnings=warnings)

    assert set(["refs", "audit", "method"]).issubset(info)
    assert info["method"] == "tail_frames_approx"
    assert info["native_pin"] is False
    assert info["approx"] is True
    assert info["ref_count"] == len(info["refs"]) == 3
    assert all(isinstance(p, Path) and p.is_file() for p in info["refs"])
    joined = "\n".join(info["audit"])
    assert "native_pin=False" in joined
    assert "不是" in joined and "原生" in joined
    assert warnings == []


@needs_ffmpeg
def test_build_cross_batch_refs_max_refs_clamped(tmp_path: Path) -> None:
    videos = _make_three_videos(tmp_path)
    warnings: list[str] = []
    info = bc.build_cross_batch_refs(videos, tmp_path / "refs_b2",
                                     max_refs=99, warnings=warnings)
    assert len(info["refs"]) <= bc.MAX_TAIL_REFS
    assert any("上限" in w for w in warnings)
