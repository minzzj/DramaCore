"""tests/verify 专用 pytest 插件：把每条用例的结局落盘为三态结论证据。

三态映射（不得混淆）：
    passed   -> 通过（PASS）
    failed   -> 失败（FAIL）
    skipped  -> 未验证（UNVERIFIED，理由必须写在 skip reason 里）

落盘位置：tests/verify/_last_run.json
    {"nodeid": ..., "state": "PASS|FAIL|UNVERIFIED", "reason": ...}

这样 docs/VERIFY.md 的每一条结论都能追溯到一次真实运行，而不是人工复述。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_HERE = Path(__file__).resolve().parent
# 保证 `from _harness import ...` / `import check_contracts` 在任何 rootdir 下都可用
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_RESULTS: dict[str, dict] = {}


def _skip_reason(report) -> str:
    lr = getattr(report, "longrepr", None)
    try:
        if isinstance(lr, tuple) and len(lr) >= 3:
            return str(lr[2])
        return str(lr)
    except Exception:  # pragma: no cover - 防御性
        return "<无法解析 skip 原因>"


def pytest_runtest_logreport(report):
    rec = _RESULTS.setdefault(
        report.nodeid, {"nodeid": report.nodeid, "state": None, "reason": ""}
    )
    if report.when == "call":
        if report.passed:
            rec["state"] = "PASS"
        elif report.failed:
            rec["state"] = "FAIL"
            rec["reason"] = str(report.longrepr)[-4000:]
        elif report.skipped:
            rec["state"] = "UNVERIFIED"
            rec["reason"] = _skip_reason(report)
        return
    # setup 阶段失败/skip（例如 fixture 抛错、模块缺失）也要留证
    if rec["state"] is None:
        if report.failed:
            rec["state"] = "FAIL"
            rec["reason"] = str(report.longrepr)[-4000:]
        elif report.skipped:
            rec["state"] = "UNVERIFIED"
            rec["reason"] = _skip_reason(report)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    for rec in _RESULTS.values():
        if rec["state"] is None:
            rec["state"] = "UNVERIFIED"
            rec["reason"] = rec["reason"] or "<无 call 阶段记录>"
    counts = {"PASS": 0, "FAIL": 0, "UNVERIFIED": 0}
    for rec in _RESULTS.values():
        counts[rec["state"]] += 1
    payload = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "python": sys.version.split()[0],
        "counts": counts,
        "results": sorted(_RESULTS.values(), key=lambda r: r["nodeid"]),
    }
    out = _HERE / "_last_run.json"
    try:
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover
        print(f"[verify] 无法写入 {out}: {exc}")

    print(
        "\n[verify] 三态结论："
        f"通过={counts['PASS']} 失败={counts['FAIL']} 未验证={counts['UNVERIFIED']}"
    )
    if counts["UNVERIFIED"]:
        print("[verify] 未验证项（禁止在 VERIFY.md 里写成通过）：")
        for rec in sorted(_RESULTS.values(), key=lambda r: r["nodeid"]):
            if rec["state"] == "UNVERIFIED":
                print(f"  - {rec['nodeid']}: {rec['reason'][:160]}")
    print(f"[verify] 明细：{out}")
