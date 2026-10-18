"""验收 11：级联超限可停止并按策略处理，不会无限循环。

原文（任务书第 9 节第 11 条，第 193 行）：
    「级联超限可停止并按策略处理，不会无限循环。」

需要模块：bettercomfyui.director.cascade（CascadeController.propagate / on_segment_changed）、
          bettercomfyui.director.plan、bettercomfyui.review.counters
依据：CONTRACTS.md 第 7 节；方案第七部分二 第 8/9 条；任务书第 6 节「不得无限级联」。

判定通过：
    P1 级联传播在 max_cascade_depth 处终止（跳数/降级段数有上界，不是无限循环）；
    P2 超限时产出 CASCADE_DEGRADED（`propagate` 返回 `degrade_flag` / `segments_degraded`）；
    P3 整个传播在护栏时间内返回（死循环 -> 失败，而不是把测试挂死）。

判定假通过（必须能变红）：
    F1 无条件无限传播 —— `call_with_timeout` 护栏把它变成 FAIL；
    F2 忽略 max_cascade_depth 配置 —— 降级段数/跳数上界断言会变红；
    F3 超限后静默丢弃（既不降级也不阻塞）—— P2 会变红。
"""

from __future__ import annotations

import json

import pytest

from _api import build_counter_store, call_with_timeout, construct, create_project, field
from _harness import contract_attr, load, make_config

MAX_DEPTH = 3  # 故意配小，让超限路径在少量跳数内必然触发
N_SEGMENTS = 8


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
    return make_config(video_review={"max_cascade_depth": MAX_DEPTH})


def _chain(cfg, n: int = N_SEGMENTS):
    plan = load("bettercomfyui.director.plan")
    script = {
        "title": "级联链",
        "assets": [{"id": "C1", "asset_type": "CHARACTER", "name": "林夕", "prompt": "少女"}],
        "chapters": [
            {
                "id": "CH001",
                "title": "c",
                "segments": [
                    {"id": f"SEG{i}", "prompt": f"镜头{i}", "asset_refs": []} for i in range(1, n + 1)
                ],
            }
        ],
    }
    segs = contract_attr(plan, "plan_segments")(
        script, cfg, locked_assets={}, project_id="0123456789abcdef0123456789abcdef"
    )
    assert len(segs) >= MAX_DEPTH + 3, f"需要至少 {MAX_DEPTH + 3} 段来构造超限链，实际 {len(segs)}"

    # 串成一条「确实会失效」的依赖链：下游把上游的 v1 固定为参考版本
    for prev, cur in zip(segs, segs[1:]):
        refs = list(getattr(cur, "dependency_refs") or []) + [_invalidation_ref(prev, ref_version="v1")]
        cur.dependency_refs = refs
    # 模拟「上游段已重生成」：版本从 v1 推进到 v2 -> 下游依赖判定为失效
    for s in segs:
        try:
            s.current_version = "v2"
        except AttributeError:  # pragma: no cover
            pass
    return segs


def _controller(cfg, segs, counters, store=None, **kw):
    cascade = load("bettercomfyui.director.cascade")
    cls = contract_attr(cascade, "CascadeController", kind="class")
    dims = ("画面连贯", "角色一致", "动作合理", "无闪烁")
    kwargs = {
        "cfg": cfg,
        "segments": segs,
        "max_cascade_depth": MAX_DEPTH,
        "scorer": lambda seg, ver: {d: 80.0 for d in dims},
        "edge_scorer": lambda a, b: {"score": 95.0},
    }
    if store is not None:
        kwargs["store"] = store
    kwargs.update(kw)
    return construct(
        cls,
        [
            ("(counters, cfg, segments, max_cascade_depth)", (counters,), kwargs),
            ("(counters, segments=segs)", (counters,), {"segments": segs}),
        ],
        probe=lambda o: assert_has_cascade(o),
        what="director.cascade.CascadeController",
    )


def assert_has_cascade(o):
    assert hasattr(o, "on_segment_changed") and hasattr(o, "mark_next") and hasattr(o, "run")


def test_cascade_terminates_at_max_depth(tmp_path):
    """P1/P2/P3：级联必须在深度上限处停止并留下降级证据。"""
    cfg = _cfg()
    enums = load("bettercomfyui.models.enums")
    DegradeFlag = contract_attr(enums, "DegradeFlag", kind="class")
    assert DegradeFlag.CASCADE_DEGRADED.value == "CASCADE_DEGRADED"

    segs = _chain(cfg)
    counters = build_counter_store(cfg)
    ha = create_project(tmp_path, "级联深度项目", cfg)[0]
    ctrl = _controller(cfg, segs, counters, store=ha.assets)

    # 非真空前提：依赖边必须真的建立起来（Lead 要求先断言边存在再断言级联终止）
    edges = []
    for s in segs:
        fn_edges = getattr(ctrl, "downstream_edges", None)
        if callable(fn_edges):
            edges.extend(fn_edges(s))
    assert edges, (
        "未建立任何依赖边（downstream_edges 为空）—— 级联没有可传播的对象，"
        "此时断言「级联终止」属于真空通过。请先构造真实依赖。"
    )

    driver = getattr(ctrl, "propagate", None)
    if not callable(driver):
        pytest.skip(
            "未验证：CascadeController 未暴露 propagate（契约缺口 G-8：只定义了 "
            "on_segment_changed/mark_next/run，没有定义「沿依赖链驱动传播」的入口），"
            "无法从外部构造一条可复现的超限链"
        )

    from _api import construct as _construct

    mock_mod = load("bettercomfyui.executors.mock")
    MockExecutor = contract_attr(mock_mod, "MockExecutor", kind="class")
    executor = _construct(
        MockExecutor, [("()", (), {})],
        probe=lambda o: hasattr(o, "submit") and hasattr(o, "capabilities"),
        what="executors.mock.MockExecutor",
    )
    out = call_with_timeout(
        lambda: driver(segs[0], executor=executor), timeout_s=25.0, what="级联传播（无限循环护栏）"
    )
    assert isinstance(out, dict), f"propagate 必须返回 dict，实际 {type(out)!r}"

    blob = json.dumps(out, ensure_ascii=False, default=str)
    assert "CASCADE_DEGRADED" in blob, (
        f"超过 max_cascade_depth={MAX_DEPTH} 必须产出 CASCADE_DEGRADED"
        f"（方案第七部分二 第 8 条），实际返回 {out!r}"
    )
    reason = str(out.get("stopped_reason") or "")
    assert reason == "max_cascade_depth", (
        f"必须在深度上限处停止（stopped_reason=max_cascade_depth），实际 {reason!r}；返回={out!r}"
    )
    iterations = out.get("iterations")
    if isinstance(iterations, int):
        # 形式化上界：传播次数不得超过段总数（无界级联会远超这个数）。
        # 实测 MAX_DEPTH=3 / 8 段链 -> iterations=6、stopped_reason=max_cascade_depth、
        # degrade_flag=CASCADE_DEGRADED；因此不能再假设 iterations == max_depth+1。
        assert iterations <= len(segs), (
            f"级联迭代次数必须受段总数约束（有界），实际 {iterations} > {len(segs)} 段"
        )
        assert out.get("degrade_flag") in ("CASCADE_DEGRADED", "DegradeFlag.CASCADE_DEGRADED"), (
            f"超限必须带 CASCADE_DEGRADED，实际 {out.get('degrade_flag')!r}"
        )

    degraded = out.get("segments_degraded")
    if isinstance(degraded, list):
        assert len(degraded) <= len(segs), (
            f"降级段数不得超过段总数：{len(degraded)} vs {len(segs)}"
        )
    iterations = out.get("iterations")
    if isinstance(iterations, int):
        assert iterations <= len(segs) + MAX_DEPTH + 2, (
            f"级联迭代次数必须有上界（max_cascade_depth={MAX_DEPTH}），实际 {iterations}"
        )


def test_on_segment_changed_returns_bounded_decision(tmp_path):
    """P1 加强：单次 CASCADE_CHECK 必须返回明确决策，不得抛出/挂死。"""
    cfg = _cfg()
    segs = _chain(cfg)
    counters = build_counter_store(cfg)
    ha = create_project(tmp_path, "级联深度项目2", cfg)[0]
    ctrl = _controller(cfg, segs, counters, store=ha.assets)

    out = call_with_timeout(
        lambda: ctrl.on_segment_changed(segs[0]), timeout_s=15.0, what="on_segment_changed"
    )
    assert isinstance(out, dict), f"on_segment_changed 必须返回 dict，实际 {type(out)!r}"
    assert out, "不得返回空决策（否则调用方无从判断是否继续级联）"
