"""单段参数覆盖（任务书第 3 节 E + 第 4 节）的回归测试。

任务书原文两处：

> 全局参数统一注入；**单段覆盖必须显式记录。**（第 3 节 E）
> `audit_scope`、`threshold`、`retry_limit`、`timeout`（第 4 节，列在**视频段**字段里）

要点是"**显式**"：隐含覆盖是排查灾难 —— 某个段审核松了、重试多了，
事后只能靠人回忆"当时是不是我改过这一段"。所以这里钉住四件事：

1. 不覆盖时语义明确（`None` = 沿用全局，不是"没实现"）；
2. 覆盖生效且**逐键**合并（只改步数不能把分辨率/fps 一起丢掉）；
3. 审核参数覆盖**必须写明原因**，否则报错；
4. 覆盖能被 `describe()` 说清楚，并真的进段计划。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.config import Config  # noqa: E402
from bettercomfyui.director.review import R1A_DIMENSIONS  # noqa: E402
from bettercomfyui.models.entities import Segment, SegmentOverrides  # noqa: E402
from bettercomfyui.models.enums import AuditScope  # noqa: E402


# ---------------------------------------------------------------------------
# 1) 不覆盖：语义明确
# ---------------------------------------------------------------------------


def test_empty_overrides_means_use_global():
    ov = SegmentOverrides()
    assert ov.is_empty()
    assert ov.overridden_fields() == []
    assert ov.review_overridden_fields() == []
    assert "无覆盖" in ov.describe()


def test_resolve_returns_global_values_when_no_override():
    cfg = Config()
    eff, changed = SegmentOverrides().resolve(cfg)
    assert changed == []
    assert eff["audit_scope"] == cfg.video_review.audit_scope
    assert eff["threshold"] == cfg.video_review.threshold_segment
    assert eff["retry_limit"] == cfg.video_review.cnt2_limit
    assert eff["timeout_s"] == cfg.global_.task_timeout_s


# ---------------------------------------------------------------------------
# 2) 覆盖生效 + 逐键合并
# ---------------------------------------------------------------------------


def test_review_override_takes_effect():
    cfg = Config()
    ov = SegmentOverrides(audit_scope="full", threshold=88.5, retry_limit=7,
                          timeout_s=1200, reason="本段动作幅度大，需要更严")
    eff, changed = ov.resolve(cfg)
    assert eff["audit_scope"] is AuditScope.FULL
    assert eff["threshold"] == 88.5
    assert eff["retry_limit"] == 7
    assert eff["timeout_s"] == 1200
    assert set(changed) == {"audit_scope", "threshold", "retry_limit", "timeout_s"}


def test_partial_override_keeps_other_globals():
    """只覆盖一个字段时，其余必须仍取全局值（不能整体替换）。"""
    cfg = Config()
    ov = SegmentOverrides(threshold=90.0, reason="只调阈值")
    eff, changed = ov.resolve(cfg)
    assert changed == ["threshold"]
    assert eff["threshold"] == 90.0
    assert eff["audit_scope"] == cfg.video_review.audit_scope, "audit_scope 不应被连带改动"
    assert eff["retry_limit"] == cfg.video_review.cnt2_limit


def test_generation_params_merge_is_per_key():
    """生成参数**逐键**合并：只改步数不能把分辨率/fps 一起丢掉。"""
    cfg = Config()
    ov = SegmentOverrides(generation_params={"steps": 40})
    merged = ov.resolve_generation(cfg.director)
    assert merged["steps"] == 40, "被覆盖的键应生效"
    # 未覆盖的键必须沿用全局
    assert merged["resolution"] == cfg.director.resolution
    assert merged["fps"] == cfg.director.fps
    assert merged["segment_seconds"] == cfg.director.segment_seconds


def test_generation_only_override_does_not_require_reason():
    """生成参数覆盖只要求可追踪（params_source 标记），不强制写原因。

    强制写原因会让脚本里每个微调都要写小作文，反而促使人绕过机制。
    """
    ov = SegmentOverrides(generation_params={"cfg": 5.0})
    assert ov.review_overridden_fields() == []
    assert "generation_params" in ov.overridden_fields()
    assert ov.is_empty() is False


# ---------------------------------------------------------------------------
# 3) 审核参数覆盖必须写明原因
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"audit_scope": "segment"},
        {"threshold": 50.0},
        {"retry_limit": 1},
        {"timeout_s": 60.0},
    ],
)
def test_review_override_without_reason_is_rejected(kw):
    with pytest.raises(ValueError, match="reason"):
        SegmentOverrides(**kw)


def test_review_override_with_reason_is_accepted():
    ov = SegmentOverrides(threshold=95.0, reason="本段是高潮镜头，必须更高标准")
    assert "高潮" in ov.describe()


# ---------------------------------------------------------------------------
# 4) 非法值当场报错（不要等执行时才炸）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"threshold": -1.0},
        {"threshold": 101.0},
        {"retry_limit": -1},
        {"timeout_s": 0},
        {"timeout_s": -5},
    ],
)
def test_invalid_values_rejected_at_construction(kw):
    with pytest.raises(ValueError):
        SegmentOverrides(reason="测试", **kw)


def test_non_dict_generation_params_rejected():
    with pytest.raises(TypeError, match="generation_params"):
        SegmentOverrides(generation_params=["steps"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5) describe 能说清楚（审计要用）
# ---------------------------------------------------------------------------


def test_describe_mentions_every_overridden_field():
    ov = SegmentOverrides(audit_scope="both", threshold=77.0,
                          generation_params={"steps": 30}, reason="测试原因", by="model")
    text = ov.describe()
    for token in ("audit_scope", "threshold", "generation_params", "测试原因", "model"):
        assert token in text, f"describe() 没提到 {token}：{text}"


# ---------------------------------------------------------------------------
# 6) Segment 集成 + 序列化往返
# ---------------------------------------------------------------------------


def test_segment_accepts_overrides_dict_and_object():
    s1 = Segment(segment_id="s1", segment_index=1, chapter_id="C")
    assert isinstance(s1.overrides, SegmentOverrides)
    assert s1.overrides.is_empty()

    s2 = Segment(segment_id="s2", segment_index=2, chapter_id="C",
                 overrides={"threshold": 80.0, "reason": "从 dict 构造"})
    assert isinstance(s2.overrides, SegmentOverrides)
    assert s2.overrides.threshold == 80.0

    with pytest.raises(TypeError, match="overrides"):
        Segment(segment_id="s3", segment_index=3, chapter_id="C", overrides="nope")


def test_segment_overrides_round_trip_through_dict():
    ov = SegmentOverrides(audit_scope="full", threshold=66.0,
                          generation_params={"steps": 12}, reason="往返测试")
    data = ov.to_dict()
    back = SegmentOverrides.from_dict(data)
    assert back.audit_scope is AuditScope.FULL
    assert back.threshold == 66.0
    assert back.generation_params == {"steps": 12}
    assert back.reason == "往返测试"


def test_segment_plan_carries_review_overrides_end_to_end():
    """**端到端**：剧本里声明 `review_overrides` -> 段计划里真的带上生效值。

    这条用真实的 `plan_segments` 跑，不是 grep 源码
    （grep 只能证明"写了这几个字"，证明不了"真的生效"）。
    """
    from bettercomfyui.director.plan import plan_segments

    from test_director_fakes import FakeIds, locked_assets_for, make_config

    PID = "0123456789abcdef0123456789abcdef"
    script = {
        "title": "覆盖测试",
        "chapters": [{
            "chapter_id": "CH001",
            "segments": [
                {
                    "segment_index": 1,
                    "prompt": "普通段：沿用全局",
                    "asset_refs": ["A1"],
                },
                {
                    "segment_index": 2,
                    "prompt": "高潮段：更严标准",
                    "asset_refs": ["A1"],
                    "review_overrides": {"threshold": 91.0, "retry_limit": 6},
                    "override_reason": "本段是高潮镜头，必须更高标准",
                    "generation_params": {"steps": 40},
                },
            ],
        }],
    }
    cfg = make_config()
    plan = plan_segments(
        script, cfg,
        locked_assets=locked_assets_for(("A1", "v3")),
        ids=FakeIds(),
        project_id=PID,
    )
    assert len(plan) == 2

    s1, s2 = plan
    # 第一段：没有覆盖
    assert s1.overrides.is_empty()
    eff1, changed1 = s1.overrides.resolve(cfg)
    assert changed1 == []
    assert eff1["threshold"] == cfg.video_review.threshold_segment

    # 第二段：覆盖生效
    assert not s2.overrides.is_empty()
    eff2, changed2 = s2.overrides.resolve(cfg)
    assert eff2["threshold"] == 91.0, f"覆盖没生效：{eff2}"
    assert eff2["retry_limit"] == 6
    assert set(changed2) == {"threshold", "retry_limit", "generation_params"}
    assert "高潮" in s2.overrides.describe()

    # 生成参数逐键合并：只改 steps，其余沿用全局
    merged = s2.overrides.resolve_generation(cfg.director)
    assert merged["steps"] == 40
    assert merged["resolution"] == cfg.director.resolution, "未覆盖的键被连带改动了"

    # 计划文件要能自解释：生效后的审核参数写进了 generation_params
    gp = s2.generation_params
    assert gp["params_source"] == "segment_override"
    assert gp["overrides"] == {"steps": 40}
    assert gp["review_overrides_effective"]["threshold"] == 91.0


def test_segment_plan_requires_reason_for_review_overrides():
    """剧本里声明审核覆盖却不写原因 -> 计划阶段就要报错（不许带病进入生成）。"""
    from bettercomfyui.director.errors import SegmentPlanError
    from bettercomfyui.director.plan import plan_segments

    from test_director_fakes import FakeIds, locked_assets_for, make_config

    script = {
        "title": "缺原因",
        "chapters": [{
            "chapter_id": "CH001",
            "segments": [{
                "segment_index": 1,
                "prompt": "x",
                "asset_refs": ["A1"],
                "review_overrides": {"threshold": 95.0},   # 故意不给 override_reason
            }],
        }],
    }
    with pytest.raises(SegmentPlanError, match="override_reason"):
        plan_segments(
            script, make_config(),
            locked_assets=locked_assets_for(("A1", "v3")),
            ids=FakeIds(),
            project_id="0123456789abcdef0123456789abcdef",
        )


def test_generation_params_override_alone_does_not_require_reason():
    """只覆盖生成参数（不碰审核）时不应强制写原因 —— 否则机制会被绕过。"""
    from bettercomfyui.director.plan import plan_segments

    from test_director_fakes import FakeIds, locked_assets_for, make_config

    script = {
        "title": "只改生成参数",
        "chapters": [{
            "chapter_id": "CH001",
            "segments": [{
                "segment_index": 1,
                "prompt": "x",
                "asset_refs": ["A1"],
                "generation_params": {"resolution": "720P"},
            }],
        }],
    }
    plan = plan_segments(
        script, make_config(),
        locked_assets=locked_assets_for(("A1", "v3")),
        ids=FakeIds(),
        project_id="0123456789abcdef0123456789abcdef",
    )
    assert plan[0].generation_params["resolution"] == "720P"
    assert plan[0].overrides.review_overridden_fields() == []


# ---------------------------------------------------------------------------
# 7) 消费者：覆盖必须真的改变判定结果
# ---------------------------------------------------------------------------


def _reviewer(*, scorer=None, cfg=None):
    """按项目既有方式构造 SegmentReviewer（构造签名是 keyword-only）。"""
    from bettercomfyui.director.review import SegmentReviewer

    from test_director_fakes import FakeCounterStore, FakeIds, make_config

    return SegmentReviewer(
        FakeCounterStore(),
        cfg=cfg or make_config(),
        ids=FakeIds(),
        scorer=scorer,
        project_id="P",
    )


def test_reviewer_uses_segment_threshold_override():
    """**关键**：段级阈值覆盖必须真的被审核器读到。

    只把覆盖存下来是不够的 —— 审核时若仍读全局值，覆盖就是个摆设：
    改了一点效果都没有，而审计日志里却写着"已覆盖"。
    这比不支持覆盖更糟（用户以为改生效了）。
    """
    from test_director_fakes import make_config, make_segment

    cfg = make_config()
    reviewer = _reviewer(cfg=cfg)

    plain = make_segment("SEG1")
    strict = make_segment("SEG2", overrides=SegmentOverrides(
        threshold=95.0, reason="本段是高潮镜头，必须更高标准"))

    assert reviewer.threshold_for(plain) == cfg.video_review.threshold_segment
    assert reviewer.threshold_for(strict) == 95.0, (
        "审核器没有读到段级阈值覆盖 —— 覆盖形同虚设"
    )


def test_reviewer_uses_segment_audit_scope_override():
    """段级 audit_scope 覆盖同样必须生效（它决定要不要做段级审核）。"""
    from test_director_fakes import make_config, make_segment

    cfg = make_config()
    reviewer = _reviewer(cfg=cfg)

    plain = make_segment("SEG1")
    forced_full = make_segment("SEG2", overrides=SegmentOverrides(
        audit_scope="full", reason="这一段并入整片一起审"))

    assert reviewer.audit_scope_for(plain) == cfg.video_review.audit_scope
    assert reviewer.audit_scope_for(forced_full) is AuditScope.FULL


def test_segment_threshold_override_changes_verdict():
    """端到端判定：同一分数下，覆盖了高阈值的段必须**不通过**。"""
    from test_director_fakes import fixed_scorer, make_config, make_attempt, make_segment

    cfg = make_config()
    reviewer = _reviewer(scorer=fixed_scorer(80), cfg=cfg)
    material = {"dimensions": {d: 80.0 for d in R1A_DIMENSIONS}}

    # 全局阈值 70 -> 80 分通过
    ok_seg = make_segment("SEG1", current_version="v2",
                          attempts=[make_attempt("SEG1", "v2", 2)])
    r_ok = reviewer.review(ok_seg, "v2", material=material)
    assert r_ok["passed"] is True, r_ok

    # 该段覆盖阈值为 95 -> 同一个 80 分必须不通过
    strict_seg = make_segment(
        "SEG2", current_version="v2",
        attempts=[make_attempt("SEG2", "v2", 2)],
        overrides=SegmentOverrides(threshold=95.0, reason="本段必须更高标准"),
    )
    r_strict = reviewer.review(strict_seg, "v2", material=material)
    assert r_strict["passed"] is False, (
        f"段级阈值 95 没有拦住 80 分 —— 覆盖没生效：{r_strict}"
    )
    assert r_strict["threshold"] == 95.0


def test_boundary_check_uses_the_stricter_threshold():
    """边界（N1）取两段中**更严**的阈值。

    否则前段要求 95、后段用全局 70 时，边界会按 70 放行，前段的高标准被绕过。
    """
    from test_director_fakes import make_config, make_segment

    cfg = make_config()
    reviewer = _reviewer(cfg=cfg)

    n = make_segment("SEG1", overrides=SegmentOverrides(
        threshold=95.0, reason="前段更高标准"))
    n1 = make_segment("SEG2")   # 用全局阈值

    assert reviewer.threshold_for(n) == 95.0
    assert reviewer.threshold_for(n1) == cfg.video_review.threshold_segment
    # 边界取更严者
    assert max(reviewer.threshold_for(n), reviewer.threshold_for(n1)) == 95.0


def test_boundary_rejects_score_between_global_and_strict():
    """端到端：85 分在「前段 95 / 后段 70」的边界上必须**不通过**。

    如果实现只看后段（或只看全局），85 分会通过 —— 前段的高标准被绕过。
    """
    from bettercomfyui.director.review import R1A_DIMENSIONS

    from test_director_fakes import make_attempt, make_config, make_segment

    cfg = make_config()
    reviewer = _reviewer(cfg=cfg)

    n = make_segment("SEG1", 1, current_version="v2",
                     attempts=[make_attempt("SEG1", "v2", 2)],
                     overrides=SegmentOverrides(
                         threshold=95.0, reason="前段更高标准"))
    n1 = make_segment("SEG2", 2, current_version="v2",
                      attempts=[make_attempt("SEG2", "v2", 1)])

    result = reviewer.verify_edge(n, n1, material={"dimensions": {"边界": 85.0}})
    assert result["passed"] is False, (
        f"边界得分 85 在阈值 {result.get('threshold')} 下不应通过：{result}"
    )
    assert result["threshold"] == 95.0, (
        f"边界阈值应取两段中更严的 95，实际 {result['threshold']}"
    )
