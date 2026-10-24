"""专项核查：真实 vs mock 边界（task-6 第 6 项）。

任务书第 9 节第 199 行原文：
    「若真实视频模型暂不可用，必须明确区分真实集成与 mock 测试，
      不能把模拟结果说成真实生成已通过。」

本文件只做一件事：证明「mock 结果有没有可能被当成真实结果」。
四条判据：
    M1 所有执行器的 capabilities 必须如实声明 verified，且 verified=True 时必须能追溯证据；
    M2 MockExecutor 必须显式标注 mock=True；
    M3 代码里不得存在「except -> 静默回退到 MockExecutor」的模式（假成功的典型写法）；
    M4 除 executors/mock.py 外，任何模块引用 MockExecutor 都必须显式来自配置选择
       （即同文件出现 mock/executor 相关的配置读取），不得隐式兜底。
"""

from __future__ import annotations

import json
import re

import pytest

from _harness import REPO_ROOT, contract_attr, load, load_optional


def _executor_classes():
    out = []
    for modname in (
        "bettercomfyui.executors.mock",
        "bettercomfyui.executors.comfyui_http",
        "bettercomfyui.executors.director_nodes",
        "bettercomfyui.executors.base",
    ):
        mod = load_optional(modname)
        if mod is None:
            continue
        for name in dir(mod):
            obj = getattr(mod, name)
            if isinstance(obj, type) and hasattr(obj, "capabilities"):
                out.append((modname, name, obj))
    return out


def test_m1_all_executors_declare_verified_honestly():
    """M1：能力声明必须含 verified；声称 True 时必须能追溯。

    executors/ 尚未实现时 -> **未验证**（不得当成通过，也不得判失败）。
    """
    from _harness import load_optional as _lo

    rows = []
    if _lo("bettercomfyui.executors.mock") is None and _lo("bettercomfyui.executors.comfyui_http") is None:
        pytest.skip(
            "未验证：executors/ 尚无实现（mock.py / comfyui_http.py / director_nodes.py 均不存在），"
            "本轮没有任何执行器能力声明可核查"
        )
    for modname, name, cls in _executor_classes():
        if name in ("Executor", "Protocol"):
            continue
        inst = None
        for kwargs in ({}, {"endpoint": "http://127.0.0.1:1"}, {"url": "http://127.0.0.1:1"}):
            try:
                inst = cls(**kwargs)
                break
            except Exception:  # noqa: BLE001
                continue
        if inst is None:
            rows.append({"executor": f"{modname}.{name}", "constructible": False})
            continue
        try:
            caps = inst.capabilities()
        except Exception as exc:  # noqa: BLE001
            rows.append({"executor": f"{modname}.{name}", "error": f"{type(exc).__name__}: {exc}"})
            continue
        assert isinstance(caps, dict), f"{name}.capabilities() 必须返回 dict"
        assert "verified" in caps, f"{name}.capabilities() 必须含 verified 字段（R8）"
        assert "notes" in caps, f"{name}.capabilities() 必须含 notes 字段（R8）"
        rows.append({"executor": f"{modname}.{name}", "verified": caps["verified"], "mock": caps.get("mock")})
        if caps["verified"] is True:
            assert str(caps["notes"]).strip(), f"{name} 声称 verified=True 但 notes 为空"
    assert rows, "已探测到执行器模块却没有任何带 capabilities() 的类"
    print("M1_ROWS=" + json.dumps(rows, ensure_ascii=False))


def test_m2_mock_executor_is_labeled():
    """M2：MockExecutor 必须标注 mock=True。"""
    mod = load("bettercomfyui.executors.mock")
    cls = contract_attr(mod, "MockExecutor", kind="class")
    inst = cls()
    caps = inst.capabilities()
    assert caps.get("mock") is True, f"MockExecutor.capabilities() 必须 mock=True，实际 {caps!r}"


def test_m3_no_silent_mock_fallback():
    """M3：不得存在 except 之后静默回退到 MockExecutor 的写法。"""
    findings = []
    for p in (REPO_ROOT / "bettercomfyui").rglob("*.py"):
        if p.name == "mock.py" and p.parent.name == "executors":
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if "MockExecutor(" not in line:
                continue
            window = lines[max(0, i - 6) : i + 2]
            if any(re.match(r"\s*except\b", w) for w in window):
                findings.append(f"{p.relative_to(REPO_ROOT)}:{i + 1}: {line.strip()}")
    assert not findings, (
        "发现「异常 -> 静默回退到 MockExecutor」的假成功写法（任务书第 7 节：不得静默失败）："
        + json.dumps(findings, ensure_ascii=False)
    )


def test_m4_mock_usage_outside_mock_module_is_config_driven():
    """M4：executors/mock.py 之外**实例化** MockExecutor 必须显式来自配置选择。

    `__init__.py` 的 re-export 不算实例化，不参与本判据。
    """
    findings = []
    for p in (REPO_ROOT / "bettercomfyui").rglob("*.py"):
        if p.name == "mock.py" and p.parent.name == "executors":
            continue
        if p.name == "__init__.py":
            continue  # 纯 re-export，不构成使用
        text = p.read_text(encoding="utf-8", errors="replace")
        if "MockExecutor(" not in text:
            continue
        has_config_gate = re.search(
            r"(mock|executor|provider|backend|engine)\w*\s*[=!]=\s*[\"'](mock|true)[\"']|cfg\.[\w.]*(mock|executor|provider)",
            text,
            re.IGNORECASE,
        )
        if not has_config_gate:
            findings.append(str(p.relative_to(REPO_ROOT)))
    assert not findings, (
        "以下文件实例化了 MockExecutor 却看不到任何配置门控 —— 存在隐式使用 mock 的风险"
        "（必须由显式配置选择）：" + json.dumps(findings, ensure_ascii=False)
    )


def test_m5_docs_do_not_claim_real_generation(tmp_path):
    """M5：文档里不得出现「模拟结果 = 真实生成已通过」的表述（报告式检查）。

    必须区分「禁止性表述」（严禁把 mock 说成真实）与「事实性声明」（真实生成已通过）。
    前者是正确写法，不能误报。
    """
    target_docs = list((REPO_ROOT / "docs").glob("*.md"))
    if not target_docs:
        pytest.skip("未验证：docs/ 下暂无文档，无法核查真实/mock 表述")

    NEGATION = re.compile(r"(严禁|禁止|不得|不能|不可|绝不|not |never|尚未|未验证)")
    suspicious = []
    for p in target_docs:
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            # 引用块（>）与表格行（|）里的文字是在**描述/禁止**这个反模式，不是本项目的声明
            if stripped.startswith(("|", ">")):
                continue
            if not re.search(r"mock|模拟|占位", line, re.IGNORECASE):
                continue
            if not re.search(r"(真实生成|真实集成|实拍).{0,12}(已通过|已验证|通过)", line):
                continue
            if NEGATION.search(line):
                continue
            suspicious.append(f"{p.relative_to(REPO_ROOT)}:{i}: {stripped}")
    assert not suspicious, (
        "文档中出现「真实生成已通过」类表述且未加 mock 限定（任务书第 9 节第 199 行）："
        + json.dumps(suspicious, ensure_ascii=False)
    )
