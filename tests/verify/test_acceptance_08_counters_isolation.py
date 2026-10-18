"""验收 8：CNT1/CNT2/CNT3 与技术重试计数互不混淆。

原文（任务书第 9 节第 8 条，第 190 行）：
    「CNT1/CNT2/CNT3 与技术重试计数互不混淆。」

需要模块：bettercomfyui.review.counters（CounterStore）、
          bettercomfyui.models.enums（CounterKind / ErrorClass / JobStatus）、
          bettercomfyui.executors.base（Executor / JobRef / JobState）、
          bettercomfyui.director.generate（SegmentGenerator）
依据：CONTRACTS.md 第 7 节 CNT2/CNT3 边界表；R4；
     方案第七部分二 第 5 条；任务书第 6 节（错误分类与计数分离）。

判定通过：
    P1 CounterStore 三个计数器互不影响（各自 +1 不得改动其它两个）；
    P2 版本号递增不清零计数器（v1->v2 之后 CNT2 保持累计）；
    P3 技术失败（ErrorClass != QUALITY_REVIEW）连续 5 次后：CNT2 == 0 且 CNT3 == 0，
       独立技术重试计数 == 5。

判定假通过（必须能变红）：
    F1 用一个计数器冒充三个（互相累加）—— P1 会变红；
    F2 每次新版本把计数器归零 —— P2 会变红；
    F3 技术失败也算质量重做 —— P3 会变红；
    F4 技术重试计数根本没实现（恒为 0）—— P3 会变红（要求精确等于 5）。
"""

from __future__ import annotations

import pytest

from _api import (
    asset_id_of,
    build_counter_store,
    construct,
    create_asset,
    create_project,
    field,
    make_asset_file,
    plan_segments_for_verify,
)
from _harness import contract_attr, load, make_config


def _kinds():
    enums = load("bettercomfyui.models.enums")
    return (
        contract_attr(enums, "CounterKind", kind="class"),
        contract_attr(enums, "ErrorClass", kind="class"),
        contract_attr(enums, "JobStatus", kind="class"),
    )


def test_three_counters_are_independent():
    """P1：三个计数器互不累加。"""
    CounterKind, _ec, _js = _kinds()
    cs = build_counter_store(make_config())

    for _ in range(2):
        cs.increment("OBJ_1", CounterKind.CNT1)
    for _ in range(3):
        cs.increment("OBJ_1", CounterKind.CNT2)
    cs.increment("OBJ_1", CounterKind.CNT3)

    assert cs.get("OBJ_1", CounterKind.CNT1) == 2, "CNT1 被其它计数器污染"
    assert cs.get("OBJ_1", CounterKind.CNT2) == 3, "CNT2 被其它计数器污染"
    assert cs.get("OBJ_1", CounterKind.CNT3) == 1, "CNT3 被其它计数器污染"

    # 不同逻辑对象的计数器也必须隔离（绑定逻辑任务 ID）
    cs.increment("OBJ_2", CounterKind.CNT2)
    assert cs.get("OBJ_1", CounterKind.CNT2) == 3, "计数器必须在逻辑对象之间隔离"
    assert cs.get("OBJ_2", CounterKind.CNT2) == 1


def test_version_increment_does_not_reset_counters(tmp_path):
    """P2：v1->v2 过程中 CNT2 必须保持累计（方案第七部分二 第 4 条）。"""
    CounterKind, _ec, _js = _kinds()
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "计数项目", cfg)
    ea = create_asset(ha, name="角色A")
    aid = asset_id_of(ea)
    cs = build_counter_store(cfg, ha.assets)

    f1 = make_asset_file(tmp_path, "v1.png", b"V1")
    f2 = make_asset_file(tmp_path, "v2.png", b"V2")
    ha.assets.add_version(aid, source="AI", file_path=f1, status="DRAFT")
    for _ in range(3):
        cs.increment(aid, CounterKind.CNT2)
    assert cs.get(aid, CounterKind.CNT2) == 3

    ha.assets.add_version(aid, source="AI", file_path=f2, status="DRAFT")  # 版本递增
    assert cs.get(aid, CounterKind.CNT2) == 3, "版本号递增绝不能清零 CNT2（R4）"

    cs.increment(aid, CounterKind.CNT2)
    assert cs.get(aid, CounterKind.CNT2) == 4, "版本递增后计数必须继续累计"


def _failing_executor(JobStatus, ErrorClass):
    base = load("bettercomfyui.executors.base")
    JobRef = contract_attr(base, "JobRef", kind="class")
    JobState = contract_attr(base, "JobState", kind="class")

    class AlwaysFails:
        """技术失败执行器：submit 成功但 poll 永远 FAILED（模拟节点异常/工作流错误）。"""

        name = "AlwaysFails"

        def __init__(self):
            self.polls = 0

        def submit(self, workflow, params, *, idempotency_key):
            return JobRef(job_id=f"job-{idempotency_key}", executor=self.name)

        def poll(self, ref):
            self.polls += 1
            return JobState(
                status=JobStatus.FAILED,
                progress=None,
                outputs=[],
                error="模拟节点异常",
                error_class=ErrorClass.COMFY_WORKFLOW,
            )

        def cancel(self, ref):
            return True

        def capabilities(self):
            # 本用例要验的是「技术失败不消耗质量计数」，
            # 因此执行器必须声明具备视频能力（否则会被能力门禁正确拒绝，测的就不是技术失败了）
            return {
                "image": True,
                "video": True,
                "first_last_frame": True,
                "tail_frames": 22,
                "verified": False,
                "notes": "验证用执行器：声明视频能力但执行总是技术失败",
            }

    return AlwaysFails()


def _segment(cfg, ha):
    """用被测实现自己的段计划 API 造段（generate 只接受 entities.Segment）。"""
    script = {
        "title": "t",
        "assets": [{"id": "C1", "asset_type": "CHARACTER", "name": "林夕", "prompt": "少女"}],
        "chapters": [
            {"id": "CH001", "title": "c", "segments": [{"id": "SEG1", "prompt": "林夕走进教室", "asset_refs": []}]}
        ],
    }
    segs = plan_segments_for_verify(script, cfg, project_id=ha.project_id, as_dicts=False)
    assert segs, "plan_segments 必须返回至少一段"
    return segs[0]


def test_technical_failure_does_not_consume_cnt2_cnt3(tmp_path):
    """P3：技术失败 5 次 -> CNT2/CNT3 均为 0，技术重试计数 == 5。"""
    CounterKind, ErrorClass, JobStatus = _kinds()
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "技术失败项目", cfg)
    ea = create_asset(ha, name="角色A")
    aid = asset_id_of(ea)
    f1 = make_asset_file(tmp_path, "v1.png", b"TECH-FAIL")
    ha.assets.add_version(aid, source="AI", file_path=f1, status="DRAFT")

    gen_mod = load("bettercomfyui.director.generate")
    SegmentGenerator = contract_attr(gen_mod, "SegmentGenerator", kind="class")
    cs = build_counter_store(cfg, ha.assets)

    def probe(o):
        assert hasattr(o, "generate"), "SegmentGenerator 必须有 generate"

    ctor_candidates = [
        ("(store, counters, cfg=cfg)", (ha.assets, cs), {"cfg": cfg}),
        ("(store, cfg=cfg)", (ha.assets,), {"cfg": cfg}),
        ("(store, counters, cfg)", (ha.assets, cs, cfg), {}),
        ("(cfg)", (cfg,), {}),
    ]
    generator = construct(
        SegmentGenerator, ctor_candidates, probe=probe, what="director.generate.SegmentGenerator"
    )

    segment = _segment(cfg, ha)
    executor = _failing_executor(JobStatus, ErrorClass)

    results = []
    for _ in range(5):
        try:
            results.append(generator.generate(segment, executor=executor))
        except Exception as exc:  # noqa: BLE001 - 技术失败允许抛错，但必须分类
            results.append({"raised": f"{type(exc).__name__}: {exc}"})

    def _pick(d, keys):
        for k in keys:
            if isinstance(d, dict) and k in d:
                return d[k]
        return None

    tech = [_pick(r, ("tech_retry", "tech_retry_count", "technical_retries", "tech_retry_count_total")) for r in results]
    tech_val = next((t for t in reversed(tech) if isinstance(t, int)), None)
    assert tech_val == 5, (
        f"技术重试计数必须独立累加到 5（每次 generate 一次），实际 {tech_val!r}；"
        f"返回样本={results[0]!r}"
    )

    # 计数器必须可读，且读到的必须是「被测对象自己的」计数器 —— 否则 0 是假证据
    reader, label = _cnt_reader(generator, results, cs)
    if reader is None:
        pytest.skip(
            f"未验证：无法读取 SegmentGenerator 的 CNT2/CNT3（{label}）。"
            "缺少前置条件：generate() 返回值带 cnt2/cnt3 字段，或 SegmentGenerator 暴露"
            "与自身状态绑定的 CounterStore。禁止自行 new 一个 CounterStore 后把 0 当成证据。"
        )
    seg_id = str(field(segment, "segment_id"))
    cnt2 = reader(seg_id, CounterKind.CNT2)
    cnt3 = reader(seg_id, CounterKind.CNT3)
    assert cnt2 == 0, f"技术失败绝不消耗 CNT2（R4），实际 CNT2={cnt2}（来源：{label}）"
    assert cnt3 == 0, f"技术失败绝不消耗 CNT3（R4），实际 CNT3={cnt3}（来源：{label}）"

    # 错误必须被分类，不得当成质量不合格
    classes = [_pick(r, ("error_class", "errorClass")) for r in results]
    assert any(
        (getattr(c, "value", c) == "COMFY_WORKFLOW") for c in classes if c is not None
    ), f"技术失败必须带 ErrorClass.COMFY_WORKFLOW，实际 {classes!r}"


def _cnt_reader(generator, results, cs):
    """返回 (reader(obj_id, kind)->int, 来源说明)；无法取得真实读数时 (None, 原因)。"""
    # 1) generate() 返回值直接带 cnt2/cnt3（最可靠：就是被测对象自己的读数）
    for r in reversed(results):
        if isinstance(r, dict) and ("cnt2" in r or "CNT2" in r):
            key2 = "cnt2" if "cnt2" in r else "CNT2"
            key3 = "cnt3" if "cnt3" in r else "CNT3"

            def from_result(obj_id, kind, _r=r, _k2=key2, _k3=key3):
                name = getattr(kind, "value", str(kind))
                return int(_r.get(_k2 if name == "CNT2" else _k3, 0))

            return from_result, "generate() 返回值"

    # 2) 生成器暴露与自身状态绑定的计数器
    for attr in ("counters", "counter_store", "_counters"):
        obj = getattr(generator, attr, None)
        if obj is not None and hasattr(obj, "get"):
            return (lambda obj_id, kind: obj.get(obj_id, kind)), f"generator.{attr}"

    # 3) 只认「构造时被注入」的实例：本用例构造候选里传过 cs，
    #    但无法确认实现是否真的用了它，因此不作为证据。
    return None, "generate() 返回值无 cnt2/cnt3 字段，且 SegmentGenerator 未暴露计数器读取入口"
