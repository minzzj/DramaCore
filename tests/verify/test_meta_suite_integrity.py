"""验证套件自检（meta）：防止「验证本身是假的」。

为什么必须有这个文件：
    task-6 的核心纪律是「禁止把未验证写成通过」。如果验证套件自己被写成
    断言恒真的空壳，那么任何结论都不可信。本文件用可执行断言检查：

    S1 验收清单（15 条任务书原文 + 1 条接缝回归）每条都有对应测试文件；
    S2 每个验收文件至少有一个真实断言，且不是恒真断言；
    S3 「模块缺失 -> 未验证（skip）」与「模块存在但契约不符 -> 失败（fail）」
       两条路径的行为确实不同（否则 skip 会吞掉真缺陷）；
    S4 check_contracts.py 的每项检查都报告了扫描覆盖面（拒绝真空通过）；
    S5 三态结论插件确实在运行（_last_run.json 会被写出）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import _harness
from _harness import ACCEPTANCE_ITEMS, REPO_ROOT, contract_attr

VERIFY_DIR = Path(__file__).resolve().parent


def _acceptance_files() -> dict[int, Path]:
    found = {}
    for p in sorted(VERIFY_DIR.glob("test_acceptance_*.py")):
        m = re.match(r"test_acceptance_(\d{2})_", p.name)
        if m:
            found[int(m.group(1))] = p
    return found


def test_s1_all_acceptance_items_have_a_test_file():
    """S1：验收清单逐条对应到测试文件（任务书 15 条 + 接缝 1 条）。"""
    files = _acceptance_files()
    missing = sorted(set(ACCEPTANCE_ITEMS) - set(files))
    assert not missing, f"缺少验收用例文件：{missing}（编号应与 ACCEPTANCE_ITEMS 一致）"
    extra = sorted(set(files) - set(ACCEPTANCE_ITEMS))
    assert not extra, f"存在未登记的验收文件编号：{extra}"


def test_s2_each_acceptance_file_has_real_assertions():
    """S2：每个验收文件必须有真实断言，且不得只有恒真断言。"""
    trivial = re.compile(r"assert\s+(True|1|not\s+False)\b")
    problems = []
    for num, p in sorted(_acceptance_files().items()):
        text = p.read_text(encoding="utf-8")
        tree = ast.parse(text)
        n_assert = sum(isinstance(n, ast.Assert) for n in ast.walk(tree))
        n_tests = sum(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
            for n in ast.walk(tree)
        )
        n_trivial = len(trivial.findall(text))
        if n_tests == 0:
            problems.append(f"{p.name}: 没有 test_* 函数")
        if n_assert == 0:
            problems.append(f"{p.name}: 没有任何 assert（空测试）")
        if n_trivial:
            problems.append(f"{p.name}: 出现恒真断言 {n_trivial} 处")
        if n_assert and n_tests and n_assert < n_tests:
            problems.append(f"{p.name}: {n_tests} 个用例只有 {n_assert} 个断言（疑似部分用例无断言）")
    assert not problems, "验收用例存在空壳/恒真风险：" + "; ".join(problems)


def test_s3_skip_and_fail_paths_are_distinct(tmp_path):
    """S3：模块不存在 -> skip（未验证）；模块存在但契约不符 -> 失败。

    这是整个套件可信度的地基：如果两者混同，真缺陷会被伪装成「未验证」。
    """
    # (a) 不存在的模块 -> skip
    with pytest.raises(BaseException) as ei:
        _harness.load("bettercomfyui.__definitely_not_exist__")
    assert ei.type.__name__ in ("Skipped", "OutcomeException"), (
        f"缺失模块必须触发 pytest.skip，实际 {ei.type!r}"
    )

    # (b) 已存在模块缺成员 -> AssertionError（失败，不是 skip）
    mod = _harness.load("bettercomfyui.models.enums")
    with pytest.raises(AssertionError):
        contract_attr(mod, "__no_such_member__")

    # (c) 已存在模块的内部 ImportError 必须冒泡为失败（不得被当成未验证）
    #     注意：不往 bettercomfyui/ 写任何文件（那是队友的独占范围），
    #     而是在临时目录里造一个「内部缺依赖」的模块包。
    import sys  # noqa: PLC0415

    probe_root = tmp_path / "probe_pkg_root"
    pkg = probe_root / "bcui_verify_probe_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "broken.py").write_text("import a_module_that_does_not_exist_xyz\n", encoding="utf-8")
    sys.path.insert(0, str(probe_root))
    try:
        with pytest.raises(BaseException) as ei2:
            _harness.load("bcui_verify_probe_pkg.broken")
        assert ei2.type.__name__ not in ("Skipped", "OutcomeException"), (
            "实现内部缺依赖必须判失败，不得被 skip 吞掉（否则真缺陷会被伪装成未验证）"
        )
    finally:
        sys.path.remove(str(probe_root))
        for name in [n for n in list(sys.modules) if n.startswith("bcui_verify_probe_pkg")]:
            del sys.modules[name]


def test_s4_contract_checks_report_coverage():
    """S4：契约检查脚本必须报告扫描覆盖面，真空通过要显式标注。"""
    import check_contracts  # noqa: PLC0415

    report = check_contracts.run_all()
    assert "checks" in report, "报告必须含 checks"
    assert report["checks"], "至少应有一项检查"
    for c in report["checks"]:
        assert "id" in c and "severity" in c and "findings" in c, f"检查项字段不全：{c}"
        assert "coverage" in c, f"检查 {c['id']} 未报告扫描覆盖面（无法识别真空通过）"
        assert isinstance(c["coverage"], dict), f"检查 {c['id']} 的 coverage 必须是 dict"
        assert "files_scanned" in c["coverage"], f"检查 {c['id']} 的 coverage 缺少 files_scanned"


def test_s5_three_state_plugin_is_active():
    """S5：三态结论插件必须在运行（否则 VERIFY.md 的结论无法追溯）。"""
    assert _harness.REPO_ROOT.is_dir()
    conftest_path = VERIFY_DIR / "conftest.py"
    assert conftest_path.is_file(), "tests/verify/conftest.py 必须存在（三态结论落盘）"
    text = conftest_path.read_text(encoding="utf-8")
    for token in ("PASS", "FAIL", "UNVERIFIED", "pytest_sessionfinish"):
        assert token in text, f"conftest.py 缺少三态结论机制要素：{token}"


def test_s6_no_unconditional_skip_at_module_level():
    """S6：不得在模块层无条件 skip（那会让整份用例永远不执行）。

    只检查**顶格**（第 0 列）的 skip —— 函数内部的 skip 是「未验证」的正常表达方式。
    """
    bad = []
    for p in sorted(VERIFY_DIR.glob("test_*.py")):
        for i, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if raw != raw.lstrip():  # 有缩进 = 在函数/类内部
                continue
            line = raw
            if re.match(r"^(pytest\.skip|skip)\(", line.strip()) or re.match(
                r"^raise\s+(pytest\.)?skip", line.strip()
            ):
                bad.append(f"{p.name}:{i}")
    assert not bad, f"模块层无条件 skip 会让用例永不执行：{bad}"
