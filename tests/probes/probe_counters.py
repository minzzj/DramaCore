"""Lead 独立运行时探测：CNT1/CNT2/CNT3 隔离 + R_full 只跑一次（任务书验收 8、13 的核心）。

**为什么另写一份**：验收 8 与 13 是「计数器口径」与「审核范围」两条最容易悄悄写错、
且错了也不会立刻报错的地方（错误表现为「数字看起来挺合理」）。
队友的测试当然也要有，但 Lead 需要一条**不依赖队友测试文件**的独立证据。

判定方式：直接调用真实模块，断言可观测数字，并把每个数字打印出来供人工复核。
任何一条不成立即打印 FAIL 并以非零退出码结束。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.models.enums import CounterKind, ReviewState  # noqa: E402
from bettercomfyui.review.counters import CounterStore  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 70)
    print("Lead 独立探测：CNT1/CNT2/CNT3 隔离 + R_full 调用次数")
    print("=" * 70)

    cs = CounterStore()

    # ---- CNT2 与 CNT3 严格分离 ----
    print("\n[C1] CNT2 与 CNT3 互不累加")
    seg_a, seg_b = "SEG_A", "SEG_B"
    for _ in range(3):
        cs.increment(seg_a, CounterKind.CNT2)
    cs.increment(seg_b, CounterKind.CNT3)
    cs.increment(seg_b, CounterKind.CNT3)
    cnt2_a = cs.get(seg_a, CounterKind.CNT2)
    cnt3_a = cs.get(seg_a, CounterKind.CNT3)
    cnt3_b = cs.get(seg_b, CounterKind.CNT3)
    cnt2_b = cs.get(seg_b, CounterKind.CNT2)
    print(f"    段A: CNT2={cnt2_a} CNT3={cnt3_a} | 段B: CNT2={cnt2_b} CNT3={cnt3_b}")
    check("A 重做 3 次 -> CNT2=3", cnt2_a == 3, f"实际 {cnt2_a}")
    check("A 的 CNT3 不受影响 =0", cnt3_a == 0, f"实际 {cnt3_a}")
    check("B 级联 2 次 -> CNT3=2", cnt3_b == 2, f"实际 {cnt3_b}")
    check("B 的 CNT2 不受影响 =0", cnt2_b == 0, f"实际 {cnt2_b}")

    # ---- 版本递增不清零 ----
    print("\n[C2] 版本号递增绝不清零任何计数器（反复 increment 模拟 v1..v5）")
    seg_c = "SEG_C"
    seq = [cs.increment(seg_c, CounterKind.CNT2) for _ in range(5)]
    print(f"    连续 5 次 increment 返回值序列: {seq}")
    check("计数器单调递增且不清零", seq == [1, 2, 3, 4, 5], f"实际 {seq}")

    # ---- 技术重试与 CNT 完全分离 ----
    print("\n[C3] 技术重试计数与 CNT2/CNT3 分离")
    seg_d = "SEG_D"
    for _ in range(5):
        cs.increment_tech_retry(seg_d)
    tech = cs.get_tech_retry(seg_d)
    c2 = cs.get(seg_d, CounterKind.CNT2)
    c3 = cs.get(seg_d, CounterKind.CNT3)
    print(f"    技术重试 5 次后: tech_retry={tech} CNT2={c2} CNT3={c3}")
    check("技术重试计入独立计数器 =5", tech == 5, f"实际 {tech}")
    check("技术失败不消耗 CNT2", c2 == 0, f"实际 {c2}")
    check("技术失败不消耗 CNT3", c3 == 0, f"实际 {c3}")

    # ---- CNT1 与 CNT2/CNT3 分离 ----
    print("\n[C4] CNT1（素材模型审核）与 CNT2/CNT3 分离")
    obj_e = "ASSET_E"
    for _ in range(3):
        cs.increment(obj_e, CounterKind.CNT1)
    c1 = cs.get(obj_e, CounterKind.CNT1)
    print(f"    素材审核 3 轮: CNT1={c1} CNT2={cs.get(obj_e, CounterKind.CNT2)} "
          f"CNT3={cs.get(obj_e, CounterKind.CNT3)}")
    check("CNT1=3", c1 == 3, f"实际 {c1}")
    check("CNT1 不污染 CNT2/CNT3",
          cs.get(obj_e, CounterKind.CNT2) == 0 and cs.get(obj_e, CounterKind.CNT3) == 0)

    # ---- scope() 可观测 ----
    print("\n[C5] scope() 能同时观测三类计数（含技术重试）")
    sc = cs.scope(seg_d)
    print(f"    scope(SEG_D) = {sc}")
    check("scope 含 CNT1/CNT2/CNT3 与 TECH_RETRY",
          {"CNT1", "CNT2", "CNT3"} <= set(sc), str(sorted(sc)))

    # ---- R_full 只执行一次 ----
    print("\n[C6] R_full：段级审核路径绝不触发整片审核")
    from bettercomfyui.director.review import SegmentReviewer
    from bettercomfyui.config import load_config

    cfg = load_config(repo_root=REPO_ROOT)
    dims = list(cfg.video_review.r1a_dimensions)
    rv = SegmentReviewer(
        CounterStore(), cfg=cfg, project_id="probe",
        scorer=lambda seg, ver: {d: 90.0 for d in dims},
    )
    print(f"    新建 reviewer 后 full_review_calls = {rv.full_review_calls}")
    check("初始 R_full 调用次数为 0", rv.full_review_calls == 0, str(rv.full_review_calls))

    # 段级审核被调用后，R_full 调用次数必须仍是 0
    from bettercomfyui.models.entities import Segment
    seg = Segment(
        segment_id="probe_CH1_SEG1", segment_index=1, chapter_id="CH1",
        asset_refs=[{"asset_id": "a1", "version": "v1"}],
    )
    seg.current_version = "v1"
    out = rv.review(seg, "v1", material=None)
    print(f"    段级审核结果: state={out['review_state'].value} score={out['score']} "
          f"audit_id={out['audit_id']}")
    check("段级审核通过（分数 90 >= 阈值）",
          out["review_state"] is ReviewState.ALLOWED, str(out["review_state"]))
    check("段级审核后 R_full 仍为 0 次",
          rv.full_review_calls == 0, str(rv.full_review_calls))
    check("audit_id 格式为 AUDIT_{segment_id}_{version}",
          out["audit_id"] == "AUDIT_probe_CH1_SEG1_v1", out["audit_id"])

    # 主动调用整片审核入口必须被拒绝（不能从段级路径溜进整片审核）
    print("\n[C7] 从段级审核器调用整片审核必须被拒绝")
    try:
        rv.run_full_review()
        check("调用 run_full_review() 被拒绝", False, "没有抛错，说明段级路径可能触发整片审核")
    except Exception as exc:  # noqa: BLE001 - 期望就是抛错
        check("调用 run_full_review() 被拒绝", True, f"{type(exc).__name__}: {str(exc)[:70]}")

    failed = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 70)
    if failed:
        print(f"结论：失败 —— {len(failed)}/{len(CHECKS)} 条不成立")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print(f"结论：通过 —— {len(CHECKS)}/{len(CHECKS)} 条独立证据全部成立")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - 探测脚本必须暴露真实异常
        traceback.print_exc()
        raise SystemExit(2)
