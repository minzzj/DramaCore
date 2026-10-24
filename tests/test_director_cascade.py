"""director/cascade.py：CNT2/CNT3 严格分离、级联终止、只传播受影响段、N1 不触发 R_full。

本文件是 task-5 的核心验收文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.director.cascade import CASCADE_POLICIES, CascadeController  # noqa: E402
from bettercomfyui.director.errors import (  # noqa: E402
    CascadeDependencyMissing,
    CascadePolicyError,
    SegmentPlanError,
)
from bettercomfyui.director.generate import SegmentGenerator  # noqa: E402
from bettercomfyui.director.plan import encode_dependency_ref  # noqa: E402
from bettercomfyui.director.review import SegmentReviewer  # noqa: E402
from bettercomfyui.director.rework import ReworkController  # noqa: E402
from bettercomfyui.models.enums import (  # noqa: E402
    CounterKind,
    DegradeFlag,
    SegmentSource,
    SegmentStatus,
)

from test_director_fakes import (  # noqa: E402
    FailingExecutor,
    FakeAssetStore,
    FakeCounterStore,
    FakeIds,
    ScriptedExecutor,
    countdown_edge_scorer,
    fixed_scorer,
    make_attempt,
    make_config,
    make_segment,
)

PREFIX = "P_CH1_SEG"


class FullReviewSpy:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):  # pragma: no cover - 只用于断言从未被调用
        self.calls += 1
        return {}


def build(tmp_path, *, n=3, cfg=None, edge=None, scorer=None, prefix=PREFIX, ref_facet="stale"):
    """构造长度为 n 的线性依赖链：S_i 依赖 S_{i-1}（reference_frame）。"""
    counters = FakeCounterStore()
    store = FakeAssetStore()
    store.add_lock("A1", "v1")
    cfg = cfg or make_config()
    ids = FakeIds()
    generator = SegmentGenerator(store, counters, cfg=cfg, ids=ids, project_id="P")
    spy = FullReviewSpy()
    reviewer = SegmentReviewer(
        counters,
        cfg=cfg,
        ids=ids,
        scorer=scorer or fixed_scorer(80),
        edge_scorer=edge or (lambda a, b: {"score": 90}),
        full_review_runner=spy,
    )
    cascade = CascadeController(
        counters, cfg=cfg, ids=ids, generator=generator, reviewer=reviewer, project_id="P"
    )
    segments = []
    for i in range(n):
        sid = f"{prefix}{i}"
        deps = []
        if i > 0:
            deps = [encode_dependency_ref(f"{prefix}{i - 1}", "reference_frame", ref_facet, "v1")]
        segments.append(
            make_segment(
                sid,
                i + 1,
                current_version="v2",
                attempts=[make_attempt(sid, "v2", 2, content_hash="sha256:" + f"{i:064d}")],
                dependency_refs=deps,
                asset_refs=[{"asset_id": "A1", "version": "v1"}],
            )
        )
    cascade.set_segments(segments)
    return {
        "cascade": cascade,
        "counters": counters,
        "generator": generator,
        "reviewer": reviewer,
        "segments": segments,
        "spy": spy,
        "ids": ids,
    }


# ---------------------------------------------------------------------------
# 1) CNT3 独立性：3 次级联重做 -> CNT3=3，CNT2 一动不动
# ---------------------------------------------------------------------------


def test_cascade_three_reworks_increment_cnt3_only(tmp_path):
    env = build(tmp_path, n=2, edge=lambda a, b: {"score": 10})  # 边界永不通过
    cascade, counters = env["cascade"], env["counters"]
    target = env["segments"][1]
    result = cascade.run(target, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["outcome"] == "N1_FALLBACK"
    assert result["iterations"] == 3  # 循环次数 = CNT3 上限（硬终止）
    assert counters.get(target.segment_id, CounterKind.CNT3) == 3
    assert counters.get(target.segment_id, CounterKind.CNT2) == 0
    assert counters.increments_of(CounterKind.CNT3) == 3
    assert counters.increments_of(CounterKind.CNT2) == 0
    assert result["source"] is SegmentSource.CASCADE_FALLBACK
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert result["selected"]["version"] == "v3"  # 同分取更早的重生成候选
    assert result["deliverable"] is True
    # 只有 3 次重生成，没有第 4 次
    assert len(env["generator"].events.events("SEGMENT_GENERATED")) == 3


def test_n1_clean_increments_cnt3_once(tmp_path):
    env = build(tmp_path, n=2)
    cascade, counters = env["cascade"], env["counters"]
    target = env["segments"][1]
    result = cascade.run(target, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["outcome"] == "N1_CLEAN"
    assert result["status"] is SegmentStatus.PASSED
    assert counters.get(target.segment_id, CounterKind.CNT3) == 1
    assert counters.get(target.segment_id, CounterKind.CNT2) == 0
    assert cascade.pending_verifications() == []
    assert cascade.all_segments_done() is True
    assert result["degrade_flag"] is DegradeFlag.NONE


def test_cnt3_loop_is_bounded_even_if_segment_review_always_fails(tmp_path):
    """R_seg 一直不合格时循环也必须在 CNT3 上限处停止（不得无限循环）。"""
    env = build(tmp_path, n=2, scorer=fixed_scorer(30))
    cascade = env["cascade"]
    executor = ScriptedExecutor(tmp_path / "jobs")
    result = cascade.run(env["segments"][1], executor=executor)
    assert result["iterations"] == 3
    assert len(executor.submits) == 3
    assert env["counters"].get(env["segments"][1].segment_id, CounterKind.CNT3) == 3


def test_cnt3_limit_reached_before_entry_goes_straight_to_fallback(tmp_path):
    env = build(tmp_path, n=2)
    counters, cascade = env["counters"], env["cascade"]
    target = env["segments"][1]
    target.attempts[0].score = 88.0  # 已有带评分的候选
    for _ in range(3):
        counters.increment(target.segment_id, CounterKind.CNT3)
    executor = ScriptedExecutor(tmp_path / "jobs")
    result = cascade.run(target, executor=executor)
    assert result["outcome"] == "N1_FALLBACK"
    assert result["iterations"] == 0
    assert result["selected"]["version"] == "v2"
    assert executor.submits == []  # 达上限后不再提交生成
    assert counters.get(target.segment_id, CounterKind.CNT3) == 3  # 不再增加


def test_fallback_without_any_candidate_is_explicitly_not_deliverable(tmp_path):
    env = build(tmp_path, n=2)
    counters, cascade = env["counters"], env["cascade"]
    target = env["segments"][1]
    for _ in range(3):
        counters.increment(target.segment_id, CounterKind.CNT3)
    result = cascade.run(target, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["outcome"] == "N1_FALLBACK_NO_CANDIDATE"
    assert result["selected"] is None
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert result["deliverable"] is False  # 无候选时绝不伪造兜底结果


# ---------------------------------------------------------------------------
# 2) 两者交替 5 轮互不影响
# ---------------------------------------------------------------------------


def test_cnt2_and_cnt3_alternate_five_rounds_without_crosstalk(tmp_path):
    cfg = make_config(**{"video_review.cnt2_limit": 10, "video_review.cnt3_limit": 10})
    env = build(tmp_path, n=2, cfg=cfg)
    counters, cascade = env["counters"], env["cascade"]
    ids = env["ids"]
    rework = ReworkController(
        counters,
        cfg=cfg,
        ids=ids,
        generator=env["generator"],
        reviewer=env["reviewer"],
        project_id="P",
    )
    normal = make_segment(
        "P_CH1_SEG9",
        9,
        current_version="v1",
        attempts=[make_attempt("P_CH1_SEG9", "v1", 1, score=40.0, passed=False)],
        status=SegmentStatus.REVIEWING,
        asset_refs=[{"asset_id": "A1", "version": "v1"}],
    )
    cascade_target = env["segments"][1]

    for _ in range(5):
        out = rework.handle_quality_failure(normal, {"score": 40}, executor=ScriptedExecutor(tmp_path / "jobs"))
        normal = out["segment"]
        assert out["counter"] == "CNT2"
        run = cascade.run(cascade_target, executor=ScriptedExecutor(tmp_path / "jobs"))
        assert run["outcome"] == "N1_CLEAN"
        cascade_target = run["segment"]

    assert counters.get("P_CH1_SEG9", CounterKind.CNT2) == 5
    assert counters.get("P_CH1_SEG9", CounterKind.CNT3) == 0
    assert counters.get(cascade_target.segment_id, CounterKind.CNT3) == 5
    assert counters.get(cascade_target.segment_id, CounterKind.CNT2) == 0
    # 只增不减：没有任何 reset
    assert counters.resets == []
    assert counters.keys_touched() == {"P_CH1_SEG9", cascade_target.segment_id}


def test_cnt2_does_not_include_cascade_segment(tmp_path):
    env = build(tmp_path, n=2)
    result = env["cascade"].run(env["segments"][1], executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["cnt2"] == 0
    assert env["counters"].increments_of(CounterKind.CNT2) == 0


# ---------------------------------------------------------------------------
# 3) 级联终止：max_cascade_depth + 策略
# ---------------------------------------------------------------------------


def test_long_chain_stops_at_max_cascade_depth_with_degrade(tmp_path):
    """每段变化都影响下一段的长链：必须在深度上限处停止并产出 CASCADE_DEGRADED。"""
    cfg = make_config(**{"video_review.max_cascade_depth": 4})
    env = build(tmp_path, n=10, cfg=cfg)
    cascade = env["cascade"]
    executor = ScriptedExecutor(tmp_path / "jobs")
    result = cascade.propagate(env["segments"][0], executor=executor)

    assert result["stopped_reason"] == "max_cascade_depth"
    assert result["iterations"] == 5  # root(depth0) + 4 层传播 == max_depth+1
    assert result["iterations"] <= result["max_cascade_depth"] + 1
    assert result["max_cascade_depth"] == 4
    assert result["segments_marked"] == [f"{PREFIX}{i}" for i in (1, 2, 3, 4)]
    assert result["segments_degraded"] == [f"{PREFIX}5"]  # 被截断的下一段明确降级
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert result["deliverable"] is True  # DEGRADE_OUTPUT 策略
    assert result["human_required"] is False
    # 只有前 4 段被重生成，远端的段完全没被触碰（禁止无条件传播）
    assert result["segments_regenerated"] == [f"{PREFIX}{i}" for i in (1, 2, 3, 4)]
    assert len(executor.submits) == 4


def test_chain_shorter_than_limit_terminates_naturally(tmp_path):
    env = build(tmp_path, n=3)
    result = env["cascade"].propagate(env["segments"][0], executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["stopped_reason"] == "all_done"
    assert result["iterations"] == 3
    assert result["all_segments_done"] is True
    assert result["pending"] == []
    assert result["degrade_flag"] is DegradeFlag.NONE
    assert result["deliverable"] is True


def test_very_long_chain_is_bounded(tmp_path):
    """200 段长链 + max_depth=8：迭代次数必须有上界，绝不做无界传播。"""
    cfg = make_config(**{"video_review.max_cascade_depth": 8})
    env = build(tmp_path, n=200, cfg=cfg)
    result = env["cascade"].propagate(env["segments"][0], executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["stopped_reason"] == "max_cascade_depth"
    assert result["iterations"] == 9
    assert result["iterations"] < result["iteration_guard"]
    assert len(result["segments_regenerated"]) == 8


@pytest.mark.parametrize(
    "policy,deliverable,human",
    [("DEGRADE_OUTPUT", True, False), ("BLOCK_OUTPUT", False, False), ("REQUEST_HUMAN", False, True)],
)
def test_depth_limit_policy_is_explicit(tmp_path, policy, deliverable, human):
    cfg = make_config(**{"video_review.max_cascade_depth": 2})
    env = build(tmp_path, n=6, cfg=cfg)
    result = env["cascade"].propagate(
        env["segments"][0], executor=ScriptedExecutor(tmp_path / "jobs"), policy=policy
    )
    assert result["policy"] == policy
    assert result["deliverable"] is deliverable
    assert result["human_required"] is human
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED


def test_invalid_policy_is_rejected(tmp_path):
    env = build(tmp_path, n=2)
    with pytest.raises(CascadePolicyError):
        env["cascade"].propagate(
            env["segments"][0], executor=ScriptedExecutor(tmp_path / "jobs"), policy="MAYBE"
        )
    assert "DEGRADE_OUTPUT" in CASCADE_POLICIES


def test_on_segment_changed_blocks_at_depth_limit(tmp_path):
    cfg = make_config(**{"video_review.max_cascade_depth": 2})
    env = build(tmp_path, n=5, cfg=cfg)
    check = env["cascade"].on_segment_changed(env["segments"][2], depth=2)
    assert check["depth_exceeded"] is True
    assert check["affected"] == []  # 超限后不允许再标记
    assert check["blocked"] == [f"{PREFIX}3"]
    assert check["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert check["deliverable"] is True


# ---------------------------------------------------------------------------
# 4) 只传播真正受影响的段
# ---------------------------------------------------------------------------


def _pair(kind, ref_facet, ref_version, upstream_last_frame="tail.mp4", upstream_hash="sha256:" + "9" * 64):
    up = make_segment(
        "UP",
        1,
        current_version="v2",
        attempts=[
            make_attempt("UP", "v2", 2, content_hash=upstream_hash, last_frame_path=upstream_last_frame)
        ],
    )
    dep = make_segment(
        "DOWN",
        2,
        current_version="v2",
        attempts=[make_attempt("DOWN", "v2", 2)],
        dependency_refs=[encode_dependency_ref("UP", kind, ref_facet, ref_version)],
    )
    return up, dep


def test_unchanged_facet_produces_no_y2(tmp_path):
    """参考帧指纹未变 -> 依赖未受影响 -> 不得产生新的 Y2 标记。"""
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up, dep = _pair("last_frame", "tail.mp4", "v1")  # 记录指纹 == 当前指纹
    cascade.set_segments([up, dep])
    check = cascade.on_segment_changed(up)
    assert check["affected"] == []
    assert check["cascade_id"] is None
    assert [s["reason"] for s in check["skipped"]] == ["facet_unchanged"]
    assert cascade.pending_verifications() == []


def test_changed_facet_produces_y2(tmp_path):
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up, dep = _pair("last_frame", "old_tail.mp4", "v1")
    cascade.set_segments([up, dep])
    check = cascade.on_segment_changed(up)
    assert [a["segment_id"] for a in check["affected"]] == ["DOWN"]
    assert check["affected"][0]["reason"] == "facet_changed"
    assert check["cascade_id"].startswith("CASCADE_UP_")


def test_no_evidence_does_not_propagate(tmp_path):
    """既无指纹也无版本证据 -> 不得标记（规则 9：只有确实失效才传播）。"""
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up, dep = _pair("last_frame", None, None)
    cascade.set_segments([up, dep])
    check = cascade.on_segment_changed(up)
    assert check["affected"] == []
    assert [s["reason"] for s in check["skipped"]] == ["no_evidence"]


def test_version_change_without_facet_is_weak_evidence(tmp_path):
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up, dep = _pair("reference_frame", None, "v1")  # 上游已到 v2
    cascade.set_segments([up, dep])
    check = cascade.on_segment_changed(up)
    assert [a["segment_id"] for a in check["affected"]] == ["DOWN"]
    assert check["affected"][0]["reason"] == "version_changed_no_facet"


def test_prompt_dependency_is_not_invalidated_by_version_bump(tmp_path):
    """prompt 维度不属于内容维度：仅版本变化不得触发级联。"""
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up, dep = _pair("prompt", None, "v1")
    cascade.set_segments([up, dep])
    assert cascade.on_segment_changed(up)["affected"] == []


def test_cascade_mark_can_be_disabled_by_config(tmp_path):
    cfg = make_config(**{"video_review.cascade_mark_enabled": False})
    env = build(tmp_path, n=2, cfg=cfg)
    up, dep = _pair("reference_frame", None, "v1")
    env["cascade"].set_segments([up, dep])
    check = env["cascade"].on_segment_changed(up)
    assert check["affected"] == []
    assert check["disabled"] == "cascade_mark_disabled"


def test_unknown_dependency_kind_is_rejected(tmp_path):
    env = build(tmp_path, n=1)
    up = make_segment("UP", 1, current_version="v1")
    with pytest.raises(SegmentPlanError):
        env["cascade"].on_segment_changed(
            up, downstream=[{"segment_id": "DOWN", "kind": "不是维度"}]
        )


# ---------------------------------------------------------------------------
# 5) Y2 / CNT3 作用域
# ---------------------------------------------------------------------------


def test_mark_next_creates_independent_cnt3_scope_without_counting(tmp_path):
    env = build(tmp_path, n=1)
    cascade, counters = env["cascade"], env["counters"]
    a = make_segment("A", 1)
    b = make_segment("B", 2)
    cascade.set_segments([a, b])
    mark_a = cascade.mark_next(a, cascade_id="CASCADE_ROOT_1")
    mark_b = cascade.mark_next(b, cascade_id="CASCADE_ROOT_1")
    assert mark_a["created"] is True
    assert mark_a["cnt3_scope"] == "CNT_A_CNT3"
    assert mark_b["cnt3_scope"] == "CNT_B_CNT3"
    assert mark_a["cnt3_incremented"] is False
    assert counters.increments == []  # 建计数器作用域 ≠ 计数
    assert a.status is SegmentStatus.NEEDS_CASCADE_CHECK and a.cascade_marked is True
    assert cascade.pending_verifications() == ["A", "B"]
    assert cascade.all_segments_done() is False
    # 幂等：重复标记不产生第二个作用域
    again = cascade.mark_next(a, cascade_id="CASCADE_ROOT_2")
    assert again["created"] is False and again["reason"] == "already_marked"


def test_cnt3_scopes_are_per_segment(tmp_path):
    cfg = make_config(**{"video_review.cnt3_limit": 3})
    env = build(tmp_path, n=3, cfg=cfg, edge=lambda a, b: {"score": 10})
    counters, cascade = env["counters"], env["cascade"]
    for target in env["segments"][1:]:
        cascade.run(target, executor=ScriptedExecutor(tmp_path / "jobs"))
    ids = [s.segment_id for s in env["segments"][1:]]
    assert [counters.get(sid, CounterKind.CNT3) for sid in ids] == [3, 3]
    assert counters.get(ids[0], CounterKind.CNT2) == 0


# ---------------------------------------------------------------------------
# 6) N1 不触发 R_full；技术失败行为
# ---------------------------------------------------------------------------


def test_cascade_never_triggers_full_review(tmp_path):
    cfg = make_config(**{"video_review.max_cascade_depth": 3})
    env = build(tmp_path, n=6, cfg=cfg)
    env["spy"].calls = 0
    result = env["cascade"].propagate(env["segments"][0], executor=ScriptedExecutor(tmp_path / "jobs"))
    assert env["reviewer"].full_review_calls == 0
    assert env["spy"].calls == 0  # 整片审核调用次数 = 0
    assert result["full_review_calls"] == 0


def test_cascade_technical_failure_does_not_increment_cnt3(tmp_path):
    env = build(tmp_path, n=2)
    cascade, counters = env["cascade"], env["counters"]
    target = env["segments"][1]
    result = cascade.run(target, executor=FailingExecutor())
    assert result["outcome"] == "TECH_FAILED"
    assert counters.get(target.segment_id, CounterKind.CNT3) == 0
    assert counters.get(target.segment_id, CounterKind.CNT2) == 0
    assert counters.increments == []
    assert result["deliverable"] is False  # 未验证通过不得声称可交付


def test_propagate_stops_on_technical_failure(tmp_path):
    env = build(tmp_path, n=5)
    result = env["cascade"].propagate(env["segments"][0], executor=FailingExecutor())
    assert result["stopped_reason"] == "technical_failure"
    assert result["deliverable"] is False
    assert env["counters"].increments_of(CounterKind.CNT3) == 0
    assert all(
        env["counters"].get(s.segment_id, CounterKind.CNT2) == 0 for s in env["segments"]
    )


def test_cascade_segment_without_upstream_raises(tmp_path):
    env = build(tmp_path, n=1)
    with pytest.raises(CascadeDependencyMissing):
        env["cascade"].run(env["segments"][0], executor=ScriptedExecutor(tmp_path / "jobs"))


def test_iteration_guard_is_finite(tmp_path):
    env = build(tmp_path, n=10)
    guard = env["cascade"]._iteration_guard(8)
    assert guard == (8 + 1) * 10 * 2 + 4
    assert guard > 9


# ---------------------------------------------------------------------------
# 7) 重复依赖边去重 + 缺少执行器时的有界终止
# ---------------------------------------------------------------------------


def test_duplicate_edges_are_deduped_with_strongest_evidence(tmp_path):
    """同一 (下游段, 维度) 出现多条边时只保留证据最强的一条，避免重复级联。"""
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up = make_segment(
        "UP", 1, current_version="v2", attempts=[make_attempt("UP", "v2", 2, content_hash="sha256:" + "e" * 64)]
    )
    down = make_segment("DOWN", 2, current_version="v1")
    down.dependency_refs = [
        "UP|reference_frame",  # 无任何证据
        encode_dependency_ref("UP", "reference_frame", None, "v1"),  # 版本证据（上游已 v2）
    ]
    cascade.set_segments([up, down])
    check = cascade.on_segment_changed(up)
    assert [a["segment_id"] for a in check["affected"]] == ["DOWN"]
    assert len(check["affected"]) == 1
    assert check["affected"][0]["reason"] == "version_changed_no_facet"
    assert check["skipped"] == []  # 弱证据边被强证据边取代，不再单列


def test_propagate_without_executor_stops_bounded_with_degrade(tmp_path):
    """没有可执行器 = 无法完成 N1：必须有界停止 + 降级证据，绝不假装完成。"""
    env = build(tmp_path, n=4)
    cascade = env["cascade"]
    cascade.get(f"{PREFIX}0").current_version = "v2"  # 模拟上游已发生变化
    result = cascade.propagate(env["segments"][0])  # 不传 executor
    assert result["stopped_reason"] == "cascade_unavailable"
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert result["deliverable"] is False
    assert result["iterations"] <= 2  # 立刻停止，不可能无界传播
    assert result["segments_degraded"] == [f"{PREFIX}1"]
    assert env["counters"].increments_of(CounterKind.CNT3) == 0  # 未真正重生成 -> 不计 CNT3


def test_propagate_converts_missing_dependency_into_explicit_stop(tmp_path):
    """有执行器但缺 R3 store：必须转成显式终止而不是崩溃或静默成功。"""
    cfg = make_config()
    counters = FakeCounterStore()
    cascade = CascadeController(counters, cfg=cfg, ids=FakeIds(), project_id="P")
    up = make_segment(
        "UP",
        1,
        current_version="v2",
        attempts=[make_attempt("UP", "v2", 2, content_hash="sha256:" + "e" * 64)],
    )
    down = make_segment("DOWN", 2, current_version="v1")
    down.dependency_refs = [encode_dependency_ref("UP", "reference_frame", None, "v1")]
    cascade.set_segments([up, down])
    result = cascade.propagate(up, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["stopped_reason"] == "cascade_unavailable"
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert result["deliverable"] is False
    assert counters.increments_of(CounterKind.CNT3) == 0
    assert counters.increments_of(CounterKind.CNT2) == 0


def test_propagate_with_unchanged_facet_does_nothing(tmp_path):
    """根段该维度指纹未变 -> 一次 CASCADE_CHECK 后就结束，不产生任何 Y2/降级。"""
    env = build(tmp_path, n=1)
    cascade = env["cascade"]
    up = make_segment(
        "UP",
        1,
        current_version="v2",
        attempts=[
            make_attempt(
                "UP", "v2", 2, content_hash="sha256:" + "f" * 64, last_frame_path="tail.mp4"
            )
        ],
    )
    down = make_segment(
        "DOWN",
        2,
        current_version="v2",
        dependency_refs=[encode_dependency_ref("UP", "last_frame", "tail.mp4", "v2")],
    )
    cascade.set_segments([up, down])
    result = cascade.propagate(up, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["stopped_reason"] == "all_done"
    assert result["iterations"] == 1
    assert result["segments_marked"] == []
    assert result["degrade_flag"] is DegradeFlag.NONE
    assert result["deliverable"] is True
