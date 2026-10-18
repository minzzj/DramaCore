"""验收 14：第三方节点缺失/不兼容时明确报错，不出现假成功。

原文（任务书第 9 节第 14 条，第 196 行）：
    「第三方节点缺失/不兼容时明确报错，不出现假成功。」

需要模块：executors.base（ExecutorError/JobRef/JobState）、executors.comfyui_http、
          executors.director_nodes
依据：CONTRACTS.md 第 7 节 R8；任务书第 7 节（节点缺失或不兼容时给出明确错误，不得静默失败）。

判定通过：
    P1 连不上 ComfyUI 时**必须显式报错**：要么抛 `ExecutorError(error_class=COMFY_WORKFLOW)`，
       要么返回 `JobStatus.FAILED + ErrorClass.COMFY_WORKFLOW`；**绝不能 SUCCEEDED，也不能把
       UNKNOWN 当成功**；
    P2 未验证能力必须 `verified=False` 且 notes 非空（R8）；
    P3 第三方导演节点缺失时必须点名缺失节点并抛 `DirectorNodeUnavailable`，绝不退化到 mock；
    P4 执行器里不得存在「裸 pass」式静默吞异常。

判定假通过（必须能变红）：
    F1 连接失败返回 SUCCEEDED/UNKNOWN 且声称有产出 —— P1 会变红；
    F2 capabilities 里 verified=True 却没有真实能力 —— P2 会变红；
    F3 节点缺失返回空结果而不报错 —— P3 会变红；
    F4 `except: pass` 吞掉提交/轮询失败 —— P4 会变红。

契约偏差（第 15 节已记录，不计为验收失败）：
    CONTRACTS.md §7 写的是「连接失败 → JobStatus.FAILED + ErrorClass.COMFY_WORKFLOW」，
    实现选择「submit/poll 抛 ExecutorError(error_class=COMFY_WORKFLOW)，由调用方映射」，
    并在 `comfyui_http.py:6` 的模块 docstring 中写明。两种都满足验收 14 的
    「明确报错、不出现假成功」，但契约文本需与实现对齐（MAJOR，需 Lead 二选一）。
"""

from __future__ import annotations

import re
import time

import pytest

from _harness import REPO_ROOT, contract_attr, load

DEAD_URL = "http://127.0.0.1:1"  # 必然拒绝连接（保留端口，不对应任何服务）
POLL_DEADLINE_S = 20.0


def _enums():
    enums = load("bettercomfyui.models.enums")
    return (
        contract_attr(enums, "JobStatus", kind="class"),
        contract_attr(enums, "ErrorClass", kind="class"),
    )


def _base():
    base = load("bettercomfyui.executors.base")
    return contract_attr(base, "ExecutorError", kind="class")


def _http_executor(url: str = DEAD_URL):
    mod = load("bettercomfyui.executors.comfyui_http")
    cls = contract_attr(mod, "ComfyUIHttpExecutor", kind="class")
    for label, args, kwargs in [
        ("(base_url=url)", (), {"base_url": url}),
        ("(url)", (url,), {}),
        ("(endpoint=url)", (), {"endpoint": url}),
        ("(url, timeout=2)", (url, 2), {}),
    ]:
        try:
            return cls(*args, **kwargs)
        except Exception:  # noqa: BLE001 - 继续探测
            continue
    raise AssertionError(
        "无法构造 ComfyUIHttpExecutor（CONTRACTS.md 未定义构造签名，契约缺口 G-6）"
    )


def _explicit_failure_of(exc: BaseException) -> str:
    """把异常转成「是否显式失败 + 分类」的判定。"""
    cls = _base()
    assert isinstance(exc, cls), (
        f"连不上 ComfyUI 必须抛 ExecutorError 子类（显式分类），实际 {type(exc).__name__}: {exc}"
    )
    ec = getattr(exc, "error_class", None)
    return str(getattr(ec, "value", ec) or "")


def test_connection_failure_is_explicit_not_fake_success():
    """P1：连不上 ComfyUI 必须显式失败分类，绝不返回 UNKNOWN/成功。"""
    JobStatus, ErrorClass = _enums()
    ex = _http_executor()

    try:
        ref = ex.submit({"1": {"class_type": "KSampler", "inputs": {}}}, {}, idempotency_key="verify-1")
    except Exception as exc:  # noqa: BLE001 - 抛错是允许的「明确报错」路径
        kind = _explicit_failure_of(exc)
        assert kind == ErrorClass.COMFY_WORKFLOW.value, (
            f"连接失败必须分类为 COMFY_WORKFLOW，实际 error_class={kind!r}；异常：{exc}"
        )
        assert str(exc).strip(), "必须带可读错误信息（不得静默）"
        return

    assert ref is not None, "submit 未抛错时必须返回 JobRef"
    deadline = time.time() + POLL_DEADLINE_S
    state = None
    while time.time() < deadline:
        try:
            state = ex.poll(ref)
        except Exception as exc:  # noqa: BLE001 - 同样是允许的明确报错路径
            kind = _explicit_failure_of(exc)
            assert kind == ErrorClass.COMFY_WORKFLOW.value, f"轮询失败必须分类，实际 {kind!r}"
            return
        if state is not None and state.status in (
            JobStatus.FAILED,
            JobStatus.SUCCEEDED,
            JobStatus.CANCELLED,
        ):
            break
        time.sleep(0.2)

    assert state is not None, "poll 必须返回 JobState"
    assert state.status is not JobStatus.SUCCEEDED, (
        f"连不上 {DEAD_URL} 却报告 SUCCEEDED = 假成功（outputs={state.outputs}）"
    )
    if state.status is JobStatus.UNKNOWN:
        assert state.error_class is not None and str(state.error or "").strip(), (
            "UNKNOWN 必须带 error_class 与说明，且调用方必须按失败处理（禁止当成功）"
        )
        assert not state.outputs, "UNKNOWN 时不得声称有输出文件"
        return
    assert state.status is JobStatus.FAILED, f"连接失败必须是 FAILED，实际 {state.status}"
    assert state.error_class is ErrorClass.COMFY_WORKFLOW, (
        f"连接失败必须分类为 COMFY_WORKFLOW，实际 {state.error_class}"
    )
    assert state.error, "失败必须带可读错误信息（不得静默）"


def test_capabilities_declare_unverified():
    """P2：R8 —— 未在本机验证的能力必须 verified=False 且 notes 解释原因。"""
    ex = _http_executor()
    caps = ex.capabilities()
    assert isinstance(caps, dict), f"capabilities 必须返回 dict，实际 {type(caps)!r}"
    for key in ("image", "video", "first_last_frame", "tail_frames", "verified", "notes"):
        assert key in caps, f"capabilities 必须包含契约字段 {key!r}（CONTRACTS.md 第 7 节），实际 {sorted(caps)}"
    if caps["verified"] is True:
        pytest.fail(
            "本机 ComfyUI 无模型权重与所需节点，ComfyUIHttpExecutor 的 verified 必须为 False"
            f"（R8），实际 capabilities={caps!r}"
        )
    assert str(caps["notes"]).strip(), "verified=False 时 notes 必须写明原因与前置条件"


# ---------------------------------------------------------------------------
# 本用例的**覆盖边界**（补记，2026-10-17）
# ---------------------------------------------------------------------------
# 上面的 test_director_node_missing_raises_explicit_error 用的是 DEAD_URL，
# 它证明的是「连不上时明确报错」——**不是**「节点检测逻辑正确」。
#
# 实测确认：在那条用例里 nodes_available 为 False，
# 并不能说明节点没装，只能说明那个 dead URL 上没有节点。
# 本机实际上已装好三个导演台包并注册了 20 个 MiniMaxH3* 节点。
#
# 因此正向路径（节点装了能被认出来 / require_nodes 放行 / 版本现场可读）
# 由 `tests/test_director_nodes_live.py` 覆盖，它直连 127.0.0.1:8188，
# 无服务时 skip 并标注「未验证」。
#
# 那条用例还确认了一个容易混的点：
# **节点可用 ≠ 能力已验证** —— 本机节点齐全但无权重，verified 仍必须是 False。


def test_director_node_missing_raises_explicit_error():
    """P3：第三方导演节点缺失时必须点名并抛明确错误，绝不退化到 mock。"""
    mod = load("bettercomfyui.executors.director_nodes")
    cls = contract_attr(mod, "DirectorNodeExecutor", kind="class")
    err_cls = getattr(mod, "DirectorNodeUnavailable", None)
    assert isinstance(err_cls, type), "director_nodes 必须定义 DirectorNodeUnavailable（明确错误类型）"

    inst = None
    for args, kwargs in [((), {"base_url": DEAD_URL}), ((DEAD_URL,), {}), ((), {})]:
        try:
            inst = cls(*args, **kwargs)
            break
        except Exception as exc:  # noqa: BLE001 - 构造期报错也要明说
            msg = str(exc)
            assert any(k in msg for k in ("ChainDirector", "AIMixer", "SmartDirector", "H3", "缺失", "未安装")), (
                f"构造期报错必须点出缺失的导演节点名，实际：{msg!r}"
            )
            return
    assert inst is not None, "无法构造 DirectorNodeExecutor"

    # P3a：require_nodes 是显式门禁
    if hasattr(inst, "require_nodes"):
        with pytest.raises(err_cls) as ei:
            inst.require_nodes()
        msg = str(ei.value)
        assert any(
            k in msg for k in ("ChainDirector", "AIMixer", "SmartDirector", "H3")
        ), f"错误信息必须点名缺失的导演节点（含候选类名），实际：{msg!r}"
        assert "custom_nodes" in msg or "安装" in msg, "必须给出可操作的修复指引"

        # P3b：submit 也必须先验节点，绝不静默退化
        with pytest.raises(err_cls):
            inst.submit({"1": {}}, {}, idempotency_key="verify-nodes")

    # P3c：capabilities 必须如实标注未验证 + 节点不可用
    caps = inst.capabilities()
    assert caps.get("verified") is False, f"节点未安装时 verified 必须为 False（R8），实际 {caps!r}"
    assert caps.get("mock") is not True, "DirectorNodeExecutor 不得把自己标成 mock（那是另一种假象）"
    assert str(caps.get("notes", "")).strip(), "verified=False 必须说明原因与前置条件"
    if "nodes_available" in caps:
        assert caps["nodes_available"] is False, (
            f"本机未安装这些节点，nodes_available 必须为 False，实际 {caps['nodes_available']}"
        )


def test_cancel_failure_returns_false_not_fake_success():
    """P4：cancel 失败必须返回 False（不得假装取消成功）。"""
    ex = _http_executor()
    ref = None
    try:
        ref = ex.submit({"1": {}}, {}, idempotency_key="verify-cancel")
    except Exception:  # noqa: BLE001 - submit 抛错路径
        base = load("bettercomfyui.executors.base")
        JobRef = contract_attr(base, "JobRef", kind="class")
        ref = JobRef(job_id="dead-job", executor="ComfyUIHttpExecutor")
    result = ex.cancel(ref)
    assert result is False, f"连不上 ComfyUI 时 cancel 必须返回 False（不得假装成功），实际 {result!r}"


def test_no_silent_swallow_in_executors():
    """P4：执行器里不得出现「裸 pass」式静默吞异常。

    注：`except ExecutorError: return False/None` 属于**失败闭合**（拒绝假装成功），
    不计为静默失败；只有 `except ...: pass`（不留任何痕迹）才是温床。
    """
    bad = []
    for p in (REPO_ROOT / "bettercomfyui" / "executors").rglob("*.py"):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            if not re.match(r"^\s*except\b.*:\s*$", line):
                continue
            nxt = lines[i] if i < len(lines) else ""
            if re.match(r"^\s*pass\s*$", nxt):
                bad.append(f"{p.relative_to(REPO_ROOT)}:{i}: {line.strip()} -> pass")
    assert not bad, f"执行器存在裸 pass 静默吞异常（假成功温床）：{bad}"
