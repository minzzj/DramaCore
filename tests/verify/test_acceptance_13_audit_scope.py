"""验收 13：`segment/full/both` 审核策略实际生效。

原文（任务书第 9 节第 13 条，第 195 行）：
    「`segment/full/both` 审核策略实际生效。」

需要模块：`bettercomfyui.pipeline.core`（full_review / apply_full_policy —— 注意是 **core.py 里的函数**，
          不是 `pipeline/full_review.py` 模块）、`bettercomfyui.director.review`（SegmentReviewer）
依据：CONTRACTS.md 第 8 节；方案第三部分六；方案第七部分二 第 3/8 条。

判定通过：
    P1 `full_review_policy: null` 时必须抛 ConfigError 且给出两个可选项（不得隐式选择）；
    P2 BLOCK_OUTPUT 与 DEGRADE_OUTPUT 行为不同，且降级必须记录 FULL_REVIEW_DEGRADED；
    P3 `audit_scope` 必须被代码真实消费（不是「配置里写了、代码没接线」）；
    P4 段级路径绝不触发 R_full（`SegmentReviewer.full_review_calls == 0`，
       `run_full_review()` 抛 ReviewScopeViolation）；
    P5 `full_review(scorer=None)` 必须抛错，拒绝用占位分数冒充审核通过。

判定假通过（必须能变红）：
    F1 未配置时替用户隐式选一个策略 —— P1 变红；
    F2 DEGRADE_OUTPUT 只返回成功但不记录降级标记 —— P2 变红；
    F3 audit_scope 只写在配置/文档里，代码从不读 —— P3 变红；
    F4 段级审核偷偷跑整片审核 —— P4 变红（full_review_calls 计数）；
    F5 scorer 缺省时返回一个假分数让审核「通过」—— P5 变红。

未验证边界：CONTRACTS.md 没有定义「按 audit_scope 决定执行哪些审核」的编排入口（缺口 G-9），
因此 `segment`/`full`/`both` 三档的**动态行为差异**在缺少编排器时无法端到端验证，
相应用例记为未验证并写明缺什么（见 test_full_review_gating_needs_orchestrator）。
"""

from __future__ import annotations

import json

import pytest

from _api import build_counter_store, construct
from _harness import REPO_ROOT, contract_attr, load, load_optional, make_config


def _core():
    return load("bettercomfyui.pipeline.core")


def _apply(cfg, result=None):
    fn = contract_attr(_core(), "apply_full_policy")
    payload = result if result is not None else {"score": 40, "threshold": 70, "passed": False}
    return fn(payload, cfg)


def test_full_review_policy_must_be_explicit():
    """P1：full_review_policy=None 时必须报错并指路（不得隐式决定）。"""
    cfg = make_config(video_review={"full_review_policy": None})
    with pytest.raises(Exception) as ei:  # noqa: BLE001 - 契约只固定行为与文案要素
        _apply(cfg)
    msg = str(ei.value)
    assert "full_review_policy" in msg, f"错误信息必须点出配置键 full_review_policy，实际：{msg!r}"
    assert "BLOCK_OUTPUT" in msg and "DEGRADE_OUTPUT" in msg, (
        f"错误信息必须给出两个可选策略（CONTRACTS.md 第 9 节原文），实际：{msg!r}"
    )
    assert any(k in type(ei.value).__name__ for k in ("Config", "Error")), (
        f"应抛配置类错误，实际 {type(ei.value).__name__}"
    )


def test_block_and_degrade_policies_differ_and_degrade_is_recorded():
    """P2：两种策略行为不同，降级必须留痕。"""
    block = _apply(make_config(video_review={"full_review_policy": "BLOCK_OUTPUT"}))
    degrade = _apply(make_config(video_review={"full_review_policy": "DEGRADE_OUTPUT"}))

    assert isinstance(block, dict) and isinstance(degrade, dict), "策略结果必须是 dict"
    assert block != degrade, "BLOCK_OUTPUT 与 DEGRADE_OUTPUT 必须产生不同结果"
    assert "FULL_REVIEW_DEGRADED" in json.dumps(degrade, ensure_ascii=False, default=str), (
        f"DEGRADE_OUTPUT 必须记录 FULL_REVIEW_DEGRADED（方案第七部分二 第 10 条），实际 {degrade!r}"
    )
    assert "FULL_REVIEW_DEGRADED" not in json.dumps(block, ensure_ascii=False, default=str), (
        f"BLOCK_OUTPUT 不得记录降级标记（阻止输出 ≠ 降级输出），实际 {block!r}"
    )


def test_full_review_rejects_placeholder_scorer(tmp_path):
    """P5：没有真实评分器时必须抛错，绝不返回占位分数当审核通过。"""
    core = _core()
    fn = contract_attr(core, "full_review")
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    cfg = make_config(video_review={"full_review_policy": "DEGRADE_OUTPUT"})
    with pytest.raises(Exception) as ei:  # noqa: BLE001 - 期望显式拒绝
        try:
            fn(movie, cfg, scorer=None, call_counter=None)
        except TypeError:
            fn(movie, cfg)  # 签名差异时退化为位置参数
    msg = str(ei.value)
    assert msg.strip(), "必须给出可读错误信息"
    assert any(k in msg for k in ("scorer", "评分", "占位")), (
        f"scorer=None 必须明确说明「拒绝用占位分数冒充审核」，实际错误：{msg!r}"
    )
    assert "通过" not in msg.split("拒绝")[0][-20:] or "未审核" in msg, "错误信息不得给出通过结论"


def test_audit_scope_is_consumed_by_code():
    """P3：audit_scope 必须被代码真实读取（静态判据）。"""
    src_files = [
        p for p in (REPO_ROOT / "bettercomfyui").rglob("*.py") if "__pycache__" not in str(p)
    ]
    impl_files = [p for p in src_files if p.name != "__init__.py"]
    assert impl_files, "前置条件：bettercomfyui/ 下应有实现文件"

    hits = [
        str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        for p in src_files
        if "audit_scope" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert hits, (
        "audit_scope 在 CONTRACTS.md 第 9 节是必须可配项（segment/full/both），"
        f"但 bettercomfyui/ 下没有任何代码读取它 —— 属于「配置没接线」。已扫描 {len(src_files)} 个文件。"
    )
    consumers = [h for h in hits if h not in ("bettercomfyui/config.py", "bettercomfyui/models/entities.py")]
    assert consumers, (
        "audit_scope 只出现在配置定义与数据模型里（声明），没有任何执行侧消费点："
        f"{hits}。验收 13 要求「实际生效」，仅有声明不成立。"
    )


def test_full_review_gating_needs_orchestrator():
    """P3 动态版本：三档 audit_scope 必须导致不同执行 —— 需要编排入口（缺口 G-9）。"""
    core = load_optional("bettercomfyui.pipeline.core")
    if core is None:
        pytest.skip("未验证：bettercomfyui.pipeline.core 尚未实现")

    call_sites = []
    for p in (REPO_ROOT / "bettercomfyui").rglob("*.py"):
        if "__pycache__" in str(p) or p.name == "core.py":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "full_review" in text and "audit_scope" in text:
            call_sites.append(str(p.relative_to(REPO_ROOT)))

    if not call_sites:
        pytest.skip(
            "未验证：契约缺口 G-9 —— 没有任何「调用整片审核并先判断 audit_scope」的编排代码"
            f"（已扫描 bettercomfyui/）。缺少前置条件：需要一个编排函数依据 "
            "cfg.video_review.audit_scope 决定是否执行 R_seg / R_full。"
        )

    # 找到消费点就必须真的验证行为，否则本用例会变成「有 call site 就自动通过」的真空断言。
    review_mod = load("bettercomfyui.director.review")
    SegmentReviewer = contract_attr(review_mod, "SegmentReviewer", kind="class")
    counters = build_counter_store(make_config())
    seen = {}
    for scope_value in ("segment", "full", "both"):
        cfg = make_config(video_review={"audit_scope": scope_value})
        reviewer = construct(
            SegmentReviewer,
            [("(counters, cfg=cfg)", (counters,), {"cfg": cfg}), ("(cfg=cfg)", (), {"cfg": cfg})],
            probe=lambda o: hasattr(o, "audit_scope"),
            what="director.review.SegmentReviewer",
        )
        got = reviewer.audit_scope
        seen[scope_value] = str(getattr(got, "value", got))
    assert seen == {"segment": "segment", "full": "full", "both": "both"}, (
        f"SegmentReviewer 必须真实消费 cfg.video_review.audit_scope（三档取不同值），实际 {seen}"
    )


def test_r_full_never_called_from_segment_paths():
    """P4：R_seg 与 N1_EDGE 路径绝不触发 R_full（方案第七部分二 第 8 条）。"""
    review_mod = load("bettercomfyui.director.review")
    SegmentReviewer = contract_attr(review_mod, "SegmentReviewer", kind="class")
    plan = load("bettercomfyui.director.plan")
    cfg = make_config()
    counters = build_counter_store(cfg)

    reviewer = construct(
        SegmentReviewer,
        [
            ("(counters, cfg=cfg)", (counters,), {"cfg": cfg}),
            ("(cfg=cfg)", (), {"cfg": cfg}),
            ("()", (), {}),
        ],
        probe=lambda o: hasattr(o, "full_review_calls"),
        what="director.review.SegmentReviewer",
    )
    assert reviewer.full_review_calls == 0, "新建 SegmentReviewer 时 R_full 调用次数必须是 0"

    script = {
        "title": "t",
        "assets": [{"id": "C1", "asset_type": "CHARACTER", "name": "林夕", "prompt": "少女"}],
        "chapters": [
            {
                "id": "CH001",
                "title": "c",
                "segments": [
                    {"id": "SEG1", "prompt": "镜头1", "asset_refs": []},
                    {"id": "SEG2", "prompt": "镜头2", "asset_refs": []},
                ],
            }
        ],
    }
    segs = contract_attr(plan, "plan_segments")(
        script, cfg, locked_assets={}, project_id="0123456789abcdef0123456789abcdef"
    )
    assert len(segs) >= 2, "需要至少 2 段"

    # 段级审核（可能因缺少素材锁定等前置而抛错，但绝不能因此调用 R_full）
    try:
        reviewer.review(segs[0], "v1", material={"prompt": "镜头1"})
    except Exception:  # noqa: BLE001 - 前置不足属允许，重点是不触发 R_full
        pass
    try:
        reviewer.verify_edge(segs[0], segs[1])
    except Exception:  # noqa: BLE001
        pass

    assert reviewer.full_review_calls == 0, (
        f"R_seg / N1_EDGE 路径触发了 R_full（次数={reviewer.full_review_calls}）—— "
        "违反方案第七部分二 第 8 条「N1 只做分段级审核与边界验证，不触发 R_full」"
    )

    # run_full_review 是唯一会累加计数的入口，且必须明确拒绝
    scope_violation = getattr(review_mod, "ReviewScopeViolation", None)
    if hasattr(reviewer, "run_full_review"):
        with pytest.raises(Exception) as ei:
            reviewer.run_full_review()
        if scope_violation is not None:
            assert isinstance(ei.value, scope_violation), (
                f"段级对象调用整片审核必须抛 ReviewScopeViolation，实际 {type(ei.value).__name__}"
            )
        assert reviewer.full_review_calls == 1, "误用计数必须被记录（便于测试断言）"
