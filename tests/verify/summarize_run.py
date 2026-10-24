"""汇总一次验证运行的结论，供 docs/VERIFY.md 引用（避免人工复述）。

用法：
    python tests/verify/summarize_run.py

读取：
    tests/verify/_last_run.json        （由 conftest.py 落盘的三态结论）
    tests/verify/_contracts_report.json（由 check_contracts.py 落盘）
    tests/verify/_mutation_report.json （由 mutation_check.py 落盘，可选）

输出：
    1) 验收清单（15 条任务书原文 + 1 条接缝回归）逐条三态结论
    2) 契约检查状态与发现清单
    3) 变异测试结果（存活 = 假通过风险）
    4) 明确列出「未验证」项及缺少的前置条件
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: ⚠️ 唯一真相在 `_harness.ACCEPTANCE_ITEMS`。
#: 这里曾**复制了一份**，结果 16 条验收加进去时只改了一处，
#: 元测试读的是 `_harness` 那份 -> 报「存在未登记的验收文件编号：[16]」。
#: 双份真相必然漂移，所以改成导入。
from _harness import ACCEPTANCE_ITEMS  # noqa: E402


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _acceptance_of(nodeid: str) -> int | None:
    m = re.search(r"test_acceptance_(\d{2})_", nodeid)
    return int(m.group(1)) if m else None


def main() -> int:
    run = _load(HERE / "_last_run.json")
    contracts = _load(HERE / "_contracts_report.json")
    mutation = _load(HERE / "_mutation_report.json")

    print("=" * 78)
    print("Better ComfyUI 独立验证 · 三态结论汇总")
    print("=" * 78)

    if run is None:
        print("未验证：尚未运行过 tests/verify（缺少 _last_run.json）")
        print("运行方式：python -m pytest tests/ -v")
        return 1

    print(f"运行时间：{run.get('generated_at')}  Python {run.get('python')}")
    counts = run.get("counts", {})
    print(
        f"总体：通过={counts.get('PASS',0)} 失败={counts.get('FAIL',0)} "
        f"未验证={counts.get('UNVERIFIED',0)}"
    )

    by_item: dict[int, dict[str, int]] = {}
    unverified: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for r in run.get("results", []):
        item = _acceptance_of(r["nodeid"])
        if item is None:
            continue
        bucket = by_item.setdefault(item, {"PASS": 0, "FAIL": 0, "UNVERIFIED": 0})
        bucket[r["state"]] = bucket.get(r["state"], 0) + 1
        if r["state"] == "UNVERIFIED":
            unverified.append((r["nodeid"], (r.get("reason") or "")[:400]))
        elif r["state"] == "FAIL":
            failed.append((r["nodeid"], (r.get("reason") or "")[:400]))

    print("\n-- 任务书第 9 节 15 条验收清单 · 逐条三态 --")
    for i in range(1, 16):
        b = by_item.get(i)
        if b is None:
            state = "未验证（无用例结果）"
            detail = "没有产生任何用例结果"
        elif b.get("FAIL"):
            state = f"失败（{b['FAIL']} 项）"
            detail = ""
        elif b.get("PASS") and not b.get("UNVERIFIED"):
            state = f"通过（{b['PASS']} 项）"
            detail = ""
        elif b.get("PASS"):
            state = f"部分通过（通过 {b['PASS']} / 未验证 {b['UNVERIFIED']}）"
            detail = ""
        else:
            state = f"未验证（{b.get('UNVERIFIED', 0)} 项）"
            detail = ""
        print(f"  {i:>2}. [{state}] {ACCEPTANCE_ITEMS[i]} {detail}")

    if failed:
        print("\n-- 失败项（必须修） --")
        for nid, reason in failed:
            print(f"  ✘ {nid}\n     {reason.splitlines()[0] if reason else ''}")
    if unverified:
        print("\n-- 未验证项（禁止写成通过） --")
        for nid, reason in unverified:
            first = reason.splitlines()[0] if reason else "<无原因>"
            print(f"  ? {nid}\n     {first}")

    if contracts:
        print("\n-- 契约一致性检查 --")
        print(f"  检查项状态：{contracts.get('statuses')}")
        print(f"  发现统计：{contracts.get('totals')}")
        for c in contracts.get("checks", []):
            if c["status"] in ("FAIL", "UNVERIFIED", "VACUOUS"):
                print(f"  [{c['status']}] {c['id']} — {c['title']}")
                for f in c["findings"][:4]:
                    loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
                    print(f"       {loc} :: {f['message'][:160]}")
    else:
        print("\n契约一致性检查：未验证（缺少 _contracts_report.json，请运行 check_contracts.py）")

    if mutation:
        print("\n-- 变异测试（假通过探测） --")
        print(f"  总计={mutation.get('total')} 杀死={mutation.get('killed')} 存活={mutation.get('survived')}")
        for r in mutation.get("results", []):
            if r["result"] != "KILLED":
                print(f"  [{r['result']}] {r.get('id')} {r.get('file')} :: {r.get('test')}")
    else:
        print("\n变异测试：未执行（缺少 _mutation_report.json）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
