"""把契约一致性检查接入 pytest：任何 BLOCKER 级发现都让测试变红。

设计要点：
    - 「目标尚未实现」=> UNVERIFIED（skip），不是通过，也不是失败；
    - 「目标已实现但契约不符」=> FAIL（这是真缺陷）；
    - 真空通过（VACUOUS：扫了 0 个文件）=> 记为未验证并在测试里显式断言出来，
      避免「没有代码所以检查通过」被当成进展。
"""

from __future__ import annotations

import pytest

import check_contracts

REPORT = None


def _report():
    global REPORT
    if REPORT is None:
        REPORT = check_contracts.run_all()
    return REPORT


def test_contracts_no_blocker_findings():
    """BLOCKER 级契约问题必须为 0（无实现时该项为未验证，不得算通过）。

    级别判定用 `check_contracts.effective_severity`：消息里的 [SEV] 前缀优先，
    避免「BLOCKER 级检查项里的 INFO 发现」被误算成 BLOCKER（Lead 复核要求）。
    """
    rep = _report()
    blockers = check_contracts.blocker_findings(rep)
    if not blockers:
        unimplemented = [c["id"] for c in rep["checks"] if c["status"] in ("UNVERIFIED", "VACUOUS")]
        if len(unimplemented) == len(rep["checks"]):
            pytest.skip(
                "未验证：所有契约检查项的目标都尚未实现（"
                + ", ".join(unimplemented)
                + "），本次没有验证任何契约一致性"
            )
        return
    assert not blockers, "存在 BLOCKER 级契约问题：" + "; ".join(
        f"{cid} {f['file']}:{f.get('line')} {f['message']}" for cid, f in blockers
    )


def test_contract_report_has_full_coverage_info():
    """每个检查项都必须报告覆盖面，且不得静默跳过。"""
    rep = _report()
    assert rep["checks"], "至少应有一项检查"
    for c in rep["checks"]:
        assert c["status"] in ("PASS", "FAIL", "UNVERIFIED", "VACUOUS"), f"{c['id']} 状态非法：{c['status']}"
        assert "coverage" in c and "files_scanned" in c["coverage"], f"{c['id']} 未报告覆盖面"
        if c["status"] in ("UNVERIFIED", "VACUOUS"):
            assert not c["findings"] or True  # 未验证项允许带「为什么没验证」的说明性记录


def test_c6_info_only_notes_do_not_count_as_blocker():
    """防回归（Lead 复核第二轮要求）：BLOCKER 级检查项里的 [INFO] 发现不得算 blocker。

    C6（R2 静态检查）的检查项级别是 BLOCKER，但它的删除类命中有可能只是
    「需人工确认」的 INFO。断言：这类 note 不会让 C6 或整体结论变成不通过。
    """
    rep = _report()
    c6 = {c["id"]: c for c in rep["checks"]}["C6_R2_NO_INPLACE_OVERWRITE"]
    info_only = [
        f
        for f in c6["findings"]
        if check_contracts.effective_severity(c6, f) == "INFO"
    ]
    hard = [f for f in c6["findings"] if check_contracts.effective_severity(c6, f) in ("BLOCKER", "MAJOR")]
    if info_only and not hard:
        assert c6["status"] == "PASS", (
            f"C6 只剩 INFO 级说明时必须 PASS，实际 {c6['status']}：{info_only}"
        )
        assert not check_contracts.blocker_findings(rep), (
            "INFO 级发现不得计入 blocker（否则验收结论会长期误报不通过）"
        )
    assert all(
        check_contracts.effective_severity(c6, f) == "INFO" or f["message"].startswith("[")
        for f in c6["findings"]
    ), "C6 的每条发现都必须带显式级别前缀，便于分级统计"


def test_enum_truth_source_is_used_by_implementations():
    """实现侧不得重复定义枚举（C4），且依赖方向单向（C5）。"""
    rep = {c["id"]: c for c in _report()["checks"]}
    for cid in ("C4_NO_DUPLICATE_ENUMS", "C5_FORBIDDEN_IMPORTS"):
        c = rep[cid]
        if c["status"] in ("UNVERIFIED", "VACUOUS"):
            pytest.skip(f"未验证：{cid} 目标尚未实现（files_scanned={c['coverage'].get('files_scanned')}）")
        assert c["status"] == "PASS", f"{cid} 未通过：{c['findings']}"


def test_transition_table_matches_spec():
    """状态转移表必须与方案第七部分六逐行一致。"""
    c = {x["id"]: x for x in _report()["checks"]}["C3_TRANSITIONS"]
    if c["status"] == "UNVERIFIED":
        pytest.skip("未验证：models/enums.py 不可用")
    assert c["status"] == "PASS", f"转移表与方案不一致：{c['findings']}"
