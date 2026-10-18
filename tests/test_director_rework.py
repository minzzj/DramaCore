"""director/rework.py：CNT2 专门计数、达上限兜底、技术失败不计数、级联分派。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.director.errors import DirectorDependencyError, DirectorError  # noqa: E402
from bettercomfyui.director.generate import SegmentGenerator  # noqa: E402
from bettercomfyui.director.review import SegmentReviewer  # noqa: E402
from bettercomfyui.director.rework import (  # noqa: E402
    ReworkController,
    select_highest_score_candidate,
)
from bettercomfyui.models.enums import (  # noqa: E402
    CounterKind,
    DegradeFlag,
    ErrorClass,
    SegmentSource,
    SegmentStatus,
)

from test_director_fakes import (  # noqa: E402
    FailingExecutor,
    FakeAssetStore,
    FakeCounterStore,
    FakeIds,
    ScriptedExecutor,
    fixed_scorer,
    make_attempt,
    make_config,
    make_segment,
)

SEG = "P_CH1_SEG1"


def build(tmp_path, *, cfg=None, scorer=None, executor=None):
    counters = FakeCounterStore()
    store = FakeAssetStore()
    store.add_lock("A1", "v1")
    ids = FakeIds()
    cfg = cfg or make_config()
    generator = SegmentGenerator(store, counters, cfg=cfg, ids=ids, project_id="P")
    reviewer = SegmentReviewer(counters, cfg=cfg, ids=ids, scorer=scorer or fixed_scorer(80))
    rework = ReworkController(
        counters, cfg=cfg, ids=ids, generator=generator, reviewer=reviewer, project_id="P"
    )
    # 初始候选 v1 已经生成但审核不合格（R_seg BLOCKED）
    segment = make_segment(
        SEG,
        1,
        current_version="v1",
        attempts=[make_attempt(SEG, "v1", 1, score=40.0, passed=False, content_hash="sha256:" + "1" * 64)],
        status=SegmentStatus.REVIEWING,
        asset_refs=[{"asset_id": "A1", "version": "v1"}],
    )
    return rework, counters, generator, reviewer, segment


# ---------------------------------------------------------------------------
# CNT2 单独计数
# ---------------------------------------------------------------------------


def test_three_reworks_increment_cnt2_only(tmp_path):
    rework, counters, _, _, segment = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs")
    versions = []
    for _ in range(3):
        result = rework.handle_quality_failure(
            segment, {"score": 40, "threshold": 70}, executor=executor
        )
        assert result["counter"] == "CNT2"
        assert result["outcome"] == "REGENERATED_PASSED"
        segment = result["segment"]
        versions.append(result["version"])
    assert versions == ["v2", "v3", "v4"]  # 不含初始版本
    assert counters.get(SEG, CounterKind.CNT2) == 3
    assert counters.get(SEG, CounterKind.CNT3) == 0
    assert counters.increments_of(CounterKind.CNT2) == 3
    assert counters.increments_of(CounterKind.CNT3) == 0
    assert counters.resets == []


def test_version_increase_never_resets_cnt2(tmp_path):
    """方案硬要求：计数器不得因版本号递增而清零。"""
    rework, counters, _, _, segment = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs")
    seen = []
    for _ in range(3):
        segment = rework.handle_quality_failure(segment, {"score": 40}, executor=executor)["segment"]
        seen.append((segment.current_version, counters.get(SEG, CounterKind.CNT2)))
    assert seen == [("v2", 1), ("v3", 2), ("v4", 3)]
    assert counters.get(SEG, CounterKind.CNT2) == 3
    assert counters.resets == []


def test_cnt2_limit_selects_highest_score_candidate(tmp_path):
    rework, counters, _, _, segment = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs")
    for _ in range(3):
        segment = rework.handle_quality_failure(segment, {"score": 40}, executor=executor)["segment"]
    result = rework.handle_quality_failure(segment, {"score": 40}, executor=executor)
    assert result["outcome"] == "REVIEW_FALLBACK"
    assert result["selected"]["version"] == "v2"  # 同分取更早版本（确定性）
    assert result["selected"]["score"] == 80.0
    assert result["source"] is SegmentSource.REVIEW_FALLBACK
    assert result["degrade_flag"] is DegradeFlag.FALLBACK_DEGRADED
    assert result["deliverable"] is True  # 兜底段正常参与拼接，但元数据保留来源
    assert result["cnt2"] == 3  # 达上限不再增加
    assert counters.get(SEG, CounterKind.CNT3) == 0
    assert len(executor.submits) == 3  # 没有第 4 次生成


def test_limit_without_candidates_is_not_faked(tmp_path):
    rework, counters, _, _, segment = build(tmp_path)
    counters.increment(SEG, CounterKind.CNT2)
    counters.increment(SEG, CounterKind.CNT2)
    counters.increment(SEG, CounterKind.CNT2)
    segment.attempts = []  # 没有任何带评分的候选
    with pytest.raises(DirectorError) as err:
        rework.handle_quality_failure(segment, {"score": 10}, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert "禁止伪造兜底结果" in str(err.value)


# ---------------------------------------------------------------------------
# 技术失败：不消耗 CNT2
# ---------------------------------------------------------------------------


def test_technical_failures_do_not_consume_cnt2(tmp_path):
    rework, counters, _, _, segment = build(tmp_path)
    executor = FailingExecutor(error_class=ErrorClass.MODEL_API)
    for _ in range(5):
        result = rework.handle_quality_failure(segment, {"score": 40}, executor=executor)
        assert result["outcome"] == "TECH_FAILED"
    assert counters.get(SEG, CounterKind.CNT2) == 0
    assert counters.get(SEG, CounterKind.CNT3) == 0
    assert counters.increments == []
    assert executor.attempts == 5


# ---------------------------------------------------------------------------
# FAIL_KIND 分派：级联段绝不进 CNT2
# ---------------------------------------------------------------------------


class CascadeStub:
    def __init__(self):
        self.calls = []

    def run(self, segment, *, executor):
        self.calls.append(segment.segment_id)
        return {"outcome": "N1_CLEAN", "cnt3": 1, "status": SegmentStatus.PASSED}


def test_cascade_segment_is_delegated_to_cnt3_channel(tmp_path):
    rework, counters, _, _, segment = build(tmp_path)
    stub = CascadeStub()
    rework.cascade = stub
    segment.cascade_marked = True
    result = rework.handle_quality_failure(segment, {"score": 30}, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["counter"] == "CNT3" and result["delegated"] is True
    assert stub.calls == [SEG]
    assert counters.get(SEG, CounterKind.CNT2) == 0
    assert counters.increments == []


def test_cascade_segment_without_cascade_controller_raises(tmp_path):
    rework, _, _, _, segment = build(tmp_path)
    segment.cascade_marked = True
    with pytest.raises(DirectorDependencyError):
        rework.handle_quality_failure(segment, {"score": 30}, executor=ScriptedExecutor(tmp_path / "jobs"))


def test_is_cascade_segment_detection():
    assert ReworkController.is_cascade_segment(make_segment("S", cascade_marked=True)) is True
    assert (
        ReworkController.is_cascade_segment(
            make_segment("S", status=SegmentStatus.NEEDS_CASCADE_CHECK)
        )
        is True
    )
    assert (
        ReworkController.is_cascade_segment(
            make_segment("S", source=SegmentSource.CASCADE_FALLBACK)
        )
        is True
    )
    assert ReworkController.is_cascade_segment(make_segment("S")) is False


# ---------------------------------------------------------------------------
# U：旧版本结论失效 + 兜底选择
# ---------------------------------------------------------------------------


def test_old_versions_are_invalidated_and_reported(tmp_path):
    seen = []
    rework, _, _, _, segment = build(tmp_path)
    rework.source_invalidator = seen.append
    rework.handle_quality_failure(segment, {"score": 40}, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert len(seen) == 1
    assert seen[0]["invalidated_versions"] == ["v1"]
    assert "旧版本审核结论失效" in seen[0]["note"]


def test_select_highest_score_candidate_is_deterministic():
    segment = make_segment(
        SEG,
        attempts=[
            make_attempt(SEG, "v2", 2, score=70.0),
            make_attempt(SEG, "v3", 3, score=95.0),
            make_attempt(SEG, "v4", 4, score=95.0),
            make_attempt(SEG, "v5", 5),
        ],
    )
    best = select_highest_score_candidate(segment)
    assert best["version"] == "v3"  # 同分取更早版本
    assert select_highest_score_candidate(make_segment("X")) is None
