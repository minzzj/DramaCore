"""验收 21：阶段化显存调度（用户要求的 5090 32G 最优解）。

## 用户原话需求

> 视频全生成完之后再把视频生成的模型卸载掉，再次换成审核模型，统一审核之后
> 不合格的再统一生成。这样最大程度的可以使用我的 5090 32g 的显存，
> 同时还保证了效率和质量并行。以避免使用 A 卡生成视频模型 B 卡辅助的高消费决策。

## 判定通过

    P1 阶段顺序严格为 PROMPT -> ASSET_GEN -> VIDEO_GEN -> VIDEO_REVIEW -> REGEN -> ENCODE；
    P2 **同一时刻只有一个模型驻留**（`max_resident_gb` 等于单模型最大需求，不是累加）；
    P3 每次换模型都**先卸载**，且卸载结果被记录；
    P4 **批量语义**：卸载次数与"模型切换次数"一致，**不随段数增长**
       （这一点是"效率"的关键 —— 逐段交替会让加载次数 = 段数）；
    P5 显存预算超限 -> **拒绝加载并抛错**（不靠"应该够"赌运气）；
    P6 未注入卸载函数时，报告里**如实写明"没有真正释放显存"**；
    P7 卸载后**未核验到显存下降** -> 记 `UNLOAD_NOT_VERIFIED` 违规，不假装成功；
    P8 `director.save_vram_all=True` -> 每个阶段结束都卸载（哪怕同模型）；
    P9 `from_config` 真的读 `director.save_vram_all` / `director.vram_budget_gb`；
    P10 单阶段异常不中断整个调度，但要记在该阶段并让 `ok=False`。

## 判定假通过（必须能变红）

    F1 去掉 `_switch_to` 里的卸载 -> P3 变红；
    F2 让预算门禁失效 -> P5 变红；
    F3 未注入卸载时谎报 `verified=True` -> P6 变红；
    F4 让 `save_vram_all` 不生效 -> P8 变红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.pipeline.phases import (  # noqa: E402
    STAGE_ORDER,
    Stage,
    VramPhaseScheduler,
    default_stage_plan,
)


class FakeVram:
    """模拟显存：加载模型占用，卸载释放。**不用真 GPU 也能验证调度逻辑。**"""

    def __init__(self, budget_gb: float = 32.0) -> None:
        self.budget_gb = budget_gb
        self.used = 0.0
        self.loads: list[str] = []
        self.unload_calls = 0
        self.verify_drop = True   # 切成 False 可模拟"卸载了但显存没降"

    def load(self, gb: float, name: str) -> None:
        self.used += gb
        self.loads.append(name)

    def unload(self) -> dict:
        self.unload_calls += 1
        before = self.used
        if self.verify_drop:
            self.used = 0.0
        return {
            "ok": True, "status": 200,
            "verified": self.used < before,
            "before": {"used_gb": before},
            "after": {"used_gb": self.used},
            "reason": "fake",
        }

    def snapshot(self) -> dict:
        return {"used_gb": self.used, "total_gb": self.budget_gb}


def _handlers(plan: list[Stage], vram: FakeVram) -> dict:
    out = {}
    for st in plan:
        def make(stage=st):
            def handler():
                if stage.model:
                    vram.load(stage.resident_gb, stage.model)
                return {"stage": stage.name}
            return handler
        out[st.name] = make()
    return out


# ---------------------------------------------------------------------------
# P1 / P2 / P3 / P4
# ---------------------------------------------------------------------------


def test_p1_stage_order_matches_requirement():
    """阶段顺序必须严格按用户描述。"""
    plan = default_stage_plan()
    assert tuple(s.name for s in plan) == STAGE_ORDER


def test_p2_only_one_model_resident_at_a_time():
    """**核心**：最大同时驻留 = 单个模型最大值，而不是累加。"""
    plan = default_stage_plan(asset_gb=8.0, video_gb=19.5, review_gb=8.0)
    vram = FakeVram(32.0)
    sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot,
                             budget_gb=32.0)
    rep = sch.run(plan, _handlers(plan, vram))
    assert rep["ok"], rep.get("violations")
    biggest = max(s.resident_gb for s in plan)
    assert rep["max_resident_gb"] == biggest, (
        f"最大驻留 {rep['max_resident_gb']} 应为单模型最大 {biggest} "
        "（累加说明没有真正卸载）"
    )


def test_p3_every_model_switch_unloads_first():
    """换模型**必须先卸载**并记录。

    注意：默认计划里每阶段 `unload_after=True`，阶段一结束就卸了，
    此时"切换前"自然不需要再卸（`from=None`）。
    所以要验证**切换路径本身**的卸载，得用 `unload_after=False` 的计划 ——
    这样模型会一直驻留，换模型时就必须先卸。
    """
    plan = [
        Stage("ASSET_GEN", model="asset_model", resident_gb=8.0, unload_after=False),
        Stage("VIDEO_GEN", model="video_model", resident_gb=19.5, unload_after=False),
        Stage("VIDEO_REVIEW", model="review_model", resident_gb=6.0, unload_after=False),
    ]
    vram = FakeVram(32.0)
    sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot, budget_gb=32.0)
    rep = sch.run(plan, _handlers(plan, vram))

    switches = [s for s in rep["switches"] if s.get("unload")]
    assert switches, "换模型时没有卸载记录"
    # 第一次加载（None->asset_model）不需要卸载；后两次换模型必须卸载
    assert len(switches) == 2, f"应卸载 2 次（每次换模型），实际 {len(switches)}"
    for sw in switches:
        assert sw["unload"].get("ok") is True
        assert sw["unload"].get("verified") is True, f"卸载未核验: {sw}"


def test_p4_unload_count_independent_of_segment_count():
    """**效率关键**：卸载次数只与模型切换次数有关，**不随段数增长**。

    如果改成"逐段生成→逐段审核"，段数 10 就会产生 20 次切换；
    批量阶段化后，10 段与 1 段的卸载次数**完全一样**。
    """
    plan = default_stage_plan()

    def run_with_segments(n: int) -> int:
        vram = FakeVram(32.0)
        handlers = _handlers(plan, vram)
        # 把 VIDEO_GEN 换成"批量生成 n 段"，仍然只加载一次模型
        original = handlers["VIDEO_GEN"]

        def batch(n=n):
            original()
            return {"segments": n}
        handlers["VIDEO_GEN"] = batch
        sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot,
                                 budget_gb=32.0)
        sch.run(plan, handlers)
        return vram.unload_calls

    assert run_with_segments(1) == run_with_segments(10), (
        "段数变化影响了卸载次数 —— 说明又在逐段交替加载/卸载"
    )


# ---------------------------------------------------------------------------
# P5 / P6 / P7 / P8 / P9 / P10
# ---------------------------------------------------------------------------


def test_p5_budget_exceeded_refuses_to_load():
    """单阶段超出显存预算必须拒绝，而不是赌"应该够"。"""
    plan = [Stage("VIDEO_GEN", model="huge", resident_gb=40.0)]
    vram = FakeVram(32.0)
    sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot, budget_gb=32.0)
    rep = sch.run(plan, _handlers(plan, vram))
    assert rep["ok"] is False
    kinds = {v["kind"] for v in rep["violations"]}
    assert "VRAM_BUDGET_EXCEEDED" in kinds, rep["violations"]


def test_p6_no_unload_function_is_reported_honestly():
    """没注入卸载函数时必须如实说明"没有真正释放"，不能假装。"""
    plan = default_stage_plan()
    vram = FakeVram()
    sch = VramPhaseScheduler(unload=None, vram=vram.snapshot, budget_gb=32.0)
    rep = sch.run(plan, _handlers(plan, vram))
    assert rep["unload_available"] is False
    assert any("没有真正释放显存" in n for n in rep["notes"]), rep["notes"]


def test_p7_unverified_unload_is_a_violation():
    """卸载后没核验到显存下降 -> 记违规，不假装成功。"""
    plan = [Stage("ASSET_GEN", model="a", resident_gb=8.0),
            Stage("VIDEO_GEN", model="b", resident_gb=19.5)]
    vram = FakeVram(32.0)
    vram.verify_drop = False      # 卸载了，但显存没降（模拟引用未释放）
    sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot, budget_gb=32.0)
    rep = sch.run(plan, _handlers(plan, vram))
    kinds = {v["kind"] for v in rep["violations"]}
    assert "UNLOAD_NOT_VERIFIED" in kinds, rep["violations"]
    assert rep["ok"] is False


def test_p8_save_vram_all_forces_unload_every_stage():
    """`save_vram_all=True` -> 每个阶段结束都卸载。"""
    plan = [Stage("PROMPT", model=None),
            Stage("ASSET_GEN", model="a", resident_gb=4.0, unload_after=False),
            Stage("VIDEO_GEN", model="a", resident_gb=4.0, unload_after=False)]
    vram = FakeVram(32.0)
    sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot,
                             budget_gb=32.0, save_vram_all=True)
    sch.run(plan, _handlers(plan, vram))
    # 两阶段都 unload_after=False，但 save_vram_all 强制每阶段卸载
    assert vram.unload_calls >= 2, f"只卸载了 {vram.unload_calls} 次"


def test_p9_from_config_reads_director_settings():
    """`from_config` 必须真的读 `director.save_vram_all` / `vram_budget_gb`。"""
    from bettercomfyui.config import load_config

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.director.save_vram_all = True
    cfg.director.vram_budget_gb = 30.0
    sch = VramPhaseScheduler.from_config(cfg)
    assert sch.save_vram_all is True
    assert sch.budget_gb == 30.0

    cfg.director.save_vram_all = False
    cfg.director.vram_budget_gb = 0.0
    sch2 = VramPhaseScheduler.from_config(cfg)
    assert sch2.save_vram_all is False
    assert sch2.budget_gb == 0.0


def test_p10_stage_error_does_not_abort_scheduler():
    """单阶段异常要记下来，但不让整个调度崩掉。"""
    plan = [Stage("ASSET_GEN", model="a", resident_gb=4.0),
            Stage("VIDEO_GEN", model="b", resident_gb=8.0)]
    vram = FakeVram(32.0)
    sch = VramPhaseScheduler(unload=vram.unload, vram=vram.snapshot, budget_gb=32.0)

    def boom():
        raise RuntimeError("生成失败")

    handlers = {"ASSET_GEN": boom, "VIDEO_GEN": lambda: {"ok": 1}}
    rep = sch.run(plan, handlers)
    assert rep["ok"] is False
    assert any(s["stage"] == "ASSET_GEN" and s["ok"] is False for s in rep["stages"])
    # 第二段仍然被执行了
    assert any(s["stage"] == "VIDEO_GEN" and s["ok"] is True for s in rep["stages"])
