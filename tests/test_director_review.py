"""director/review.py：R_seg 绑定、R1a 四维、版本一致性、N1_EDGE 不触发 R_full。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.director.errors import (  # noqa: E402
    EdgeVerificationError,
    ReviewMaterialMissing,
    ReviewScopeViolation,
    StaleSegmentVersion,
)
from bettercomfyui.director.review import R1A_DIMENSIONS, SegmentReviewer  # noqa: E402
from bettercomfyui.models.enums import AuditScope, ReviewState, SegmentStatus  # noqa: E402

from test_director_fakes import (  # noqa: E402
    FakeCounterStore,
    FakeIds,
    fixed_scorer,
    make_attempt,
    make_config,
    make_segment,
)

SEG = "P_CH1_SEG1"
SEG_NEXT = "P_CH1_SEG2"


class FullReviewSpy:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return {"score": 0}


def make_reviewer(segment, *, scorer=None, edge_scorer=None, cfg=None, spy=None):
    counters = FakeCounterStore()
    reviewer = SegmentReviewer(
        counters,
        cfg=cfg or make_config(),
        ids=FakeIds(),
        scorer=scorer,
        edge_scorer=edge_scorer,
        project_id="P",
        full_review_runner=spy,
    )
    return reviewer


def seg_v2():
    return make_segment(
        SEG,
        1,
        current_version="v2",
        attempts=[make_attempt(SEG, "v2", 2, content_hash="sha256:" + "c" * 64)],
    )


# ---------------------------------------------------------------------------
# R_seg
# ---------------------------------------------------------------------------


def test_review_binds_audit_id_and_scores_dimensions():
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(80))
    result = reviewer.review(seg_v2(), "v2")
    assert result["audit_id"] == f"AUDIT_{SEG}_v2"
    assert result["review_cycle_id"] == f"segment:{SEG}:v2"
    assert set(result["dimensions"]) == set(R1A_DIMENSIONS)
    assert result["score"] == 80.0
    assert result["threshold"] == 70.0
    assert result["passed"] is True
    assert result["status"] is SegmentStatus.PASSED
    assert result["review_state"] is ReviewState.ALLOWED
    assert result["segment"].status is SegmentStatus.PASSED
    # 评分写回当前候选尝试（兜底选最高分依赖它）
    attempt = result["segment"].current_attempt()
    assert attempt is not None and attempt.score == 80.0 and attempt.passed is True


def test_review_below_threshold_is_blocked_not_failed():
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(50))
    result = reviewer.review(seg_v2(), "v2")
    assert result["passed"] is False
    # 质量不合格 != 技术失败：status 保持 REVIEWING，失败语义在 review_status
    assert result["status"] is SegmentStatus.REVIEWING
    assert result["review_state"] is ReviewState.BLOCKED
    assert result["segment"].review_status is ReviewState.BLOCKED


def test_stale_version_is_rejected():
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(80))
    with pytest.raises(StaleSegmentVersion):
        reviewer.review(seg_v2(), "v1")


def test_missing_material_never_fabricates_score():
    reviewer = make_reviewer(seg_v2())
    with pytest.raises(ReviewMaterialMissing):
        reviewer.review(seg_v2(), "v2")


def test_material_can_be_passed_explicitly():
    reviewer = make_reviewer(seg_v2())
    result = reviewer.review(seg_v2(), "v2", material={"dimensions": {d: 75 for d in R1A_DIMENSIONS}})
    assert result["passed"] is True and result["score"] == 75.0


def test_missing_dimension_is_rejected():
    reviewer = make_reviewer(seg_v2(), scorer=lambda s, v: {"画面连贯": 90})
    with pytest.raises(ReviewMaterialMissing) as err:
        reviewer.review(seg_v2(), "v2")
    assert "缺少维度" in str(err.value)


@pytest.mark.parametrize("bad", [120, -1, "高分", True])
def test_invalid_scores_are_rejected(bad):
    reviewer = make_reviewer(seg_v2(), scorer=lambda s, v: {d: bad for d in R1A_DIMENSIONS})
    with pytest.raises(ReviewMaterialMissing):
        reviewer.review(seg_v2(), "v2")


def test_audit_scope_full_skips_segment_review():
    cfg = make_config(**{"video_review.audit_scope": AuditScope.FULL})
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(80), cfg=cfg)
    with pytest.raises(ReviewScopeViolation):
        reviewer.review(seg_v2(), "v2")


def test_threshold_is_configurable():
    cfg = make_config(**{"video_review.threshold_segment": 85.0})
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(80), cfg=cfg)
    assert reviewer.review(seg_v2(), "v2")["passed"] is False


# ---------------------------------------------------------------------------
# N1_EDGE
# ---------------------------------------------------------------------------


def seg_pair():
    n = make_segment(SEG, 1, current_version="v2", attempts=[make_attempt(SEG, "v2", 2)])
    n1 = make_segment(
        SEG_NEXT,
        2,
        current_version="v3",
        attempts=[make_attempt(SEG_NEXT, "v3", 3)],
        dependency_refs=[f"{SEG}|last_frame|-|v2"],
    )
    return n, n1


def test_verify_edge_passes_and_binds_edge_id():
    reviewer = make_reviewer(seg_v2(), edge_scorer=lambda a, b: {"score": 88})
    n, n1 = seg_pair()
    edge = reviewer.verify_edge(n, n1)
    assert edge["passed"] is True
    assert edge["edge_id"] == f"EDGE_N1:{SEG}->{SEG_NEXT}:v3"
    assert edge["scope"] == "edge_only"
    assert edge["declared_dependency"] is True
    assert edge["degrade_flag"] is None  # 降级由 CascadeController 决定


def test_verify_edge_failure():
    reviewer = make_reviewer(seg_v2(), edge_scorer=lambda a, b: {"score": 40})
    n, n1 = seg_pair()
    assert reviewer.verify_edge(n, n1)["passed"] is False


def test_verify_edge_requires_adjacency():
    reviewer = make_reviewer(seg_v2(), edge_scorer=lambda a, b: {"score": 88})
    n = make_segment(SEG, 1, current_version="v2")
    far = make_segment("P_CH1_SEG9", 9, current_version="v1")
    with pytest.raises(EdgeVerificationError):
        reviewer.verify_edge(n, far)


def test_verify_edge_requires_material():
    reviewer = make_reviewer(seg_v2())
    n, n1 = seg_pair()
    with pytest.raises(ReviewMaterialMissing):
        reviewer.verify_edge(n, n1)


def test_verify_edge_accepts_dimension_material():
    reviewer = make_reviewer(seg_v2())
    n, n1 = seg_pair()
    edge = reviewer.verify_edge(n, n1, material={"dimensions": {"边界": 90, "动作": 80}})
    assert edge["score"] == 85.0 and edge["passed"] is True


# ---------------------------------------------------------------------------
# R_full 作用域红线
# ---------------------------------------------------------------------------


def test_segment_review_and_edge_never_trigger_full_review():
    spy = FullReviewSpy()
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(80), edge_scorer=lambda a, b: {"score": 90}, spy=spy)
    reviewer.review(seg_v2(), "v2")
    n, n1 = seg_pair()
    reviewer.verify_edge(n, n1)
    assert spy.calls == 0
    assert reviewer.full_review_calls == 0


def test_run_full_review_is_a_scope_violation():
    reviewer = make_reviewer(seg_v2(), scorer=fixed_scorer(80))
    with pytest.raises(ReviewScopeViolation):
        reviewer.run_full_review("movie.mp4")
    assert reviewer.full_review_calls == 1  # 误用被记录


def test_review_module_does_not_import_pipeline():
    source = (Path(__file__).parents[1] / "bettercomfyui" / "director" / "review.py").read_text(
        encoding="utf-8"
    )
    assert "bettercomfyui.pipeline" not in source
    assert "import pipeline" not in source
