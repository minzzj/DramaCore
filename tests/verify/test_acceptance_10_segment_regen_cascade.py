"""验收 10：不合格视频段可局部重做；受影响后续段进入衔接复核。

原文（任务书第 9 节第 10 条，第 192 行）：
    「不合格视频段可局部重做；受影响后续段进入衔接复核。」

需要模块：bettercomfyui.director.plan（plan_segments）、director.cascade（CascadeController）、
          review.counters（CounterStore）、executors.mock（MockExecutor）
依据：CONTRACTS.md 第 7 节；方案第七部分二 第 4/7/8/9 条；R5（段 ID 必须含 project_id）。

实测要点（供报告引用）：
    * `plan_segments` 必须显式传 `project_id`，否则抛 SegmentPlanError（R5 落地）；
    * `SegmentGenerator.generate` / `CascadeController` 只接受 `models.entities.Segment` 对象，
      传 dict 会被拒（实现里明确拒绝平行结构，防止绕过 asset_refs 的 R3 校验）。

判定通过：
    P1 段计划每段都带 segment_id / segment_index / prompt / asset_refs / generation_params /
       first_frame / last_frame / dependency_refs（契约第 7 节逐项列举）；
    P2 mark_next 只为受影响的后续段建立 Y2 待验证标记；
    P3 N1（run）使 CNT3 +1 且 CNT2 完全不变（R4 分离）；
    P4 未 LOCK 的素材被引用进生成时必须被拒绝（R3 门禁）。

判定假通过（必须能变红）：
    F1 无条件标记全部后续段 —— P2 会变红；
    F2 N1 用了 CNT2 计数 —— P3 会变红；
    F3 段计划字段缺失却仍然返回 —— P1 会变红；
    F4 generate 放行未 LOCK 素材 —— P4 会变红。
"""

from __future__ import annotations

import pytest

from _api import (
    build_counter_store,
    construct,
    create_project,
    field,
    make_asset_file,
    plan_segments_for_verify,
)
from _harness import contract_attr, load, make_config

N_SEGMENTS = 4


def _invalidation_ref(prev, *, ref_version: str = "v1") -> dict:
    """构造一条**可判定失效**的依赖边。

    实测判据（director/plan.py::parse_dependency_ref / cascade.py::_is_invalidated）：
    依赖类别必须属于 CONTENT_DEPENDENCY_KINDS，且 `ref_version` != 上游 `current_version`
    时才判为失效（facet_changed / version_changed_no_facet）。裸 segment_id 依赖
    只会得到 no_evidence，级联不会传播 —— 那样断言「级联终止」就是真空通过。
    """
    cascade_mod = load("bettercomfyui.director.cascade")
    kinds = list(getattr(cascade_mod, "CONTENT_DEPENDENCY_KINDS", None) or ["reference_frame"])
    return {
        "segment_id": field(prev, "segment_id"),
        "kind": kinds[0],
        "ref_version": ref_version,
    }


def _cfg():
    return make_config(video_review={"max_cascade_depth": 8, "cnt2_limit": 3, "cnt3_limit": 3})


def _script(n: int = N_SEGMENTS) -> dict:
    return {
        "title": "第一章",
        "assets": [{"id": "CHAR_01", "asset_type": "CHARACTER", "name": "林夕", "prompt": "黑长直发少女"}],
        "chapters": [
            {
                "id": "CH001",
                "title": "第一章",
                "segments": [
                    {"id": f"SEG{i}", "prompt": f"第{i}个镜头：林夕走过教室", "asset_refs": []}
                    for i in range(1, n + 1)
                ],
            }
        ],
    }


def _segments(cfg, n: int = N_SEGMENTS, as_dicts: bool = False, chain: bool = True):
    """建段计划；chain=True 时显式串成依赖链（级联必须按依赖传播）。"""
    segs = plan_segments_for_verify(_script(n), cfg, as_dicts=as_dicts)
    assert isinstance(segs, list) and len(segs) >= 2, f"plan_segments 必须返回至少 2 段，实际 {segs!r}"
    if chain:
        for prev, cur in zip(segs, segs[1:]):
            refs = list(field(cur, "dependency_refs") or []) + [_invalidation_ref(prev, ref_version="v1")]
            if isinstance(cur, dict):
                cur["dependency_refs"] = refs
            else:
                cur.dependency_refs = refs
        for s in segs:
            if not isinstance(s, dict):
                try:
                    s.current_version = "v2"  # 模拟上游已重生成
                except AttributeError:  # pragma: no cover
                    pass
    return segs


def _cascade(cfg, segments, counters, tmp_path):
    """构造级联控制器。

    实测：`CascadeController` 内部的 SegmentGenerator 需要 store/lock_resolver
    （R3 门禁依赖），否则构造期就抛 DirectorDependencyError —— 这是正确行为。
    """
    ha = create_project(tmp_path, "级联项目", cfg)[0]
    gen_mod = load("bettercomfyui.director.generate")
    SegmentGenerator = contract_attr(gen_mod, "SegmentGenerator", kind="class")
    generator = construct(
        SegmentGenerator,
        [("(store, counters, cfg=cfg)", (ha.assets, counters), {"cfg": cfg})],
        probe=lambda o: hasattr(o, "generate"),
        what="director.generate.SegmentGenerator",
    )
    review_mod = load("bettercomfyui.director.review")
    SegmentReviewer = contract_attr(review_mod, "SegmentReviewer", kind="class")
    reviewer = construct(
        SegmentReviewer,
        [
            (
                "(counters, cfg=cfg, scorer=...)",
                (counters,),
                {
                    "cfg": cfg,
                    "scorer": lambda seg, ver: {
                        d: 80.0 for d in ("画面连贯", "角色一致", "动作合理", "无闪烁")
                    },
                    "edge_scorer": lambda a, b: {"score": 95.0},
                },
            ),
            ("(counters, cfg=cfg)", (counters,), {"cfg": cfg}),
        ],
        probe=lambda o: hasattr(o, "review"),
        what="director.review.SegmentReviewer",
    )
    cascade = load("bettercomfyui.director.cascade")
    cls = contract_attr(cascade, "CascadeController", kind="class")
    return construct(
        cls,
        [
            (
                "(counters, cfg, segments, generator, reviewer, store)",
                (counters,),
                {
                    "cfg": cfg,
                    "segments": segments,
                    "generator": generator,
                    "reviewer": reviewer,
                    "store": ha.assets,
                },
            ),
        ],
        probe=lambda o: hasattr(o, "on_segment_changed") and hasattr(o, "run"),
        what="director.cascade.CascadeController",
    )


def _mock_executor():
    mod = load("bettercomfyui.executors.mock")
    cls = contract_attr(mod, "MockExecutor", kind="class")
    return construct(
        cls,
        [("()", (), {}), ("(workdir=.)", (), {"workdir": "."})],
        probe=lambda o: hasattr(o, "submit") and hasattr(o, "poll") and hasattr(o, "capabilities"),
        what="executors.mock.MockExecutor",
    )


def test_plan_segments_contains_contracted_fields():
    """P1：段计划字段齐全（缺字段会让级联判断失去依据）。"""
    cfg = _cfg()
    segments = _segments(cfg, as_dicts=True)
    required = [
        "segment_id",
        "segment_index",
        "prompt",
        "asset_refs",
        "generation_params",
        "first_frame",
        "last_frame",
        "dependency_refs",
    ]
    for s in segments:
        assert isinstance(s, dict), f"as_dicts=True 时每段必须是 dict，实际 {type(s)!r}"
        missing = [k for k in required if k not in s]
        assert not missing, (
            f"契约第 7 节要求每段记录 {required}，段 {s.get('segment_id')} 缺 {missing}"
        )
        assert isinstance(s["asset_refs"], list), "asset_refs 必须是列表（含具体版本）"
        assert str(s["segment_id"]).startswith("0123456789abcdef"), (
            f"段 ID 必须包含 project_id（R5），实际 {s['segment_id']!r}"
        )


def test_mark_next_only_marks_affected_segments(tmp_path):
    """P2：mark_next 只为受影响的段建立 Y2 标记。"""
    cfg = _cfg()
    segments = _segments(cfg)
    counters = build_counter_store(cfg)
    ctrl = _cascade(cfg, segments, counters, tmp_path)

    enums = load("bettercomfyui.models.enums")
    SegmentStatus = contract_attr(enums, "SegmentStatus", kind="class")

    changed = segments[0]
    out = ctrl.on_segment_changed(changed)
    assert isinstance(out, dict), f"on_segment_changed 必须返回 dict，实际 {type(out)!r}"

    marked = ctrl.mark_next(changed)
    assert isinstance(marked, dict), f"mark_next 必须返回 dict（Y2），实际 {type(marked)!r}"
    blob = str(marked)
    assert SegmentStatus.NEEDS_CASCADE_CHECK.value in blob or "NEEDS_CASCADE_CHECK" in blob, (
        f"Y2 必须把第 N+1 段标为 NEEDS_CASCADE_CHECK，实际 {marked!r}"
    )

    # 隔了 2 段以上的远段不应被标记（级联按依赖传播，不是无脑全标）
    changed_index = int(field(changed, "segment_index", 0) or 0)
    far = [s for s in segments if int(field(s, "segment_index", 0) or 0) > changed_index + 1]
    for s in far:
        for attr in ("status", "segment_status"):
            val = field(s, attr)
            assert "NEEDS_CASCADE_CHECK" not in str(val), (
                f"不受影响的远段 {field(s, 'segment_id')} 不应被标记（级联必须按依赖传播）"
            )


def test_n1_increments_cnt3_not_cnt2(tmp_path):
    """P3：N1 级联重生成只加 CNT3，CNT2 完全不变（R4）。"""
    cfg = _cfg()
    segments = _segments(cfg)
    enums = load("bettercomfyui.models.enums")
    CounterKind = contract_attr(enums, "CounterKind", kind="class")
    counters = build_counter_store(cfg)
    ctrl = _cascade(cfg, segments, counters, tmp_path)

    seg = segments[1]
    seg_id = str(field(seg, "segment_id"))
    executor = _mock_executor()

    cnt2_before = counters.get(seg_id, CounterKind.CNT2)
    ctrl.mark_next(segments[0])  # 先建立级联关系
    result = ctrl.run(seg, executor=executor)

    cnt3_after = counters.get(seg_id, CounterKind.CNT3)
    cnt2_after = counters.get(seg_id, CounterKind.CNT2)
    assert isinstance(result, dict), f"run 必须返回 dict，实际 {type(result)!r}"
    assert cnt3_after == 1, f"N1 级联重生成必须使 CNT3 +1，实际 {cnt3_after}（返回 {result!r}）"
    assert cnt2_after == cnt2_before, f"N1 不得改动 CNT2（R4），{cnt2_before} -> {cnt2_after}"


def test_unlocked_asset_is_rejected_by_generate(tmp_path):
    """P4：未 LOCK 的素材版本被引用进生成时必须被拒绝（R3 门禁）。"""
    cfg = _cfg()
    ha = create_project(tmp_path, "R3项目", cfg)[0]
    make_asset_file(tmp_path, "a.png", b"unlocked")

    gen_mod = load("bettercomfyui.director.generate")
    SegmentGenerator = contract_attr(gen_mod, "SegmentGenerator", kind="class")
    counters = build_counter_store(cfg)
    generator = construct(
        SegmentGenerator,
        [
            ("(store, counters, cfg=cfg)", (ha.assets, counters), {"cfg": cfg}),
            ("(store, cfg=cfg)", (ha.assets,), {"cfg": cfg}),
        ],
        probe=lambda o: hasattr(o, "generate"),
        what="director.generate.SegmentGenerator",
    )

    segments = _segments(cfg)
    seg = segments[0]
    # 把段的素材引用换成一个从未入库的素材版本
    try:
        seg.asset_refs = [{"asset_id": "不存在的素材", "version": "v1"}]
    except AttributeError:  # pragma: no cover
        pass

    guard = getattr(generator, "assert_assets_locked", None)
    with pytest.raises(Exception) as ei:
        if callable(guard):
            guard(seg)
        else:
            generator.generate(seg, executor=_mock_executor())
    assert str(ei.value).strip(), "R3 违背必须给出可读错误信息"
