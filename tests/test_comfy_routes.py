"""**真正调用**插件 HTTP 处理器并校验响应。

为什么必须单独写这个文件（血的教训）：

`tests/test_comfy_plugin_load.py` 只验证了「模块能导入」，于是
`h_doctor` 里一个 `NameError: __version__`（重构时漏掉的全局名）**完全没有被测试发现** ——
直到把插件真的装进 ComfyUI、发起真实 HTTP 请求，服务器才返回 500。
面板调用 `/bettercomfyui/doctor` 是第一件事，它挂了 = 用户一打开就看到报错。

教训：**「能导入」不等于「能调用」**。路由处理器必须被真实调用一次。

本文件用 `aiohttp.test_utils` 直接跑处理器（不需要起服务器），
对每个路由同时断言：状态码、内容类型、以及**关键字段的存在与语义**。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

aiohttp = pytest.importorskip("aiohttp", reason="未验证：需要 aiohttp 才能调用处理器")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402


@pytest.fixture(scope="module")
def routes_mod():
    mod = importlib.import_module("bettercomfyui_comfy.routes")
    return mod


def _call(handler, *, body: dict | None = None, method: str = "GET"):
    """同步调用异步处理器并返回 (status, payload_dict)。"""
    kwargs = {"method": method, "path": "/bettercomfyui/test"}
    if body is not None:
        req = make_mocked_request(method, "/bettercomfyui/test", headers={
            "Content-Type": "application/json",
        })

        async def _json():
            return body

        req.json = _json  # type: ignore[method-assign]
    else:
        req = make_mocked_request(method, "/bettercomfyui/test")

    resp = asyncio.run(handler(req))
    assert isinstance(resp, web.Response), f"处理器返回了 {type(resp).__name__}，不是 web.Response"
    raw = resp.body.decode("utf-8") if isinstance(resp.body, (bytes, bytearray)) else (resp.text or "")
    payload = json.loads(raw) if raw else None
    return resp.status, payload


# ---------------------------------------------------------------------------
# /bettercomfyui/doctor —— 面板打开后的第一个请求
# ---------------------------------------------------------------------------


def test_doctor_handler_returns_200_and_full_payload(routes_mod):
    """`h_doctor` 必须返回 200 且含全套字段。

    这条直接针对实测抓到的 `NameError: __version__`（导致真实服务器 500）。
    """
    status, payload = _call(routes_mod.h_doctor)
    assert status == 200, f"doctor 返回 {status}：{payload}"
    assert isinstance(payload, dict)

    for key in (
        "plugin_version", "node_count", "node_names", "phase2_exposed_in_ui",
        "orchestrator_available", "ffmpeg_available", "real_generation_available",
        "compute",
    ):
        assert key in payload, f"doctor 响应缺少字段 {key}"

    # 「权重/版本够」不等于「能出片」：/doctor 必须给出计算设备判定与原因，
    # 否则面板会显示「真实生成可用：是」而实际跑不了（审计 R1 实测假阳性）。
    compute = payload["compute"] or {}
    assert "available" in compute and "reason" in compute, compute
    if compute.get("available") is not True:
        assert str(compute.get("reason") or "").strip(), (
            "计算设备不可用时必须写明原因，不能只给 False")

    assert payload["plugin_version"], "plugin_version 不能为空"
    assert payload["node_count"] >= 6, payload["node_count"]
    assert "BetterComfyUIRun" in payload["node_names"]
    assert payload["phase2_exposed_in_ui"] is False, "第二阶段不得在 UI 暴露"


def test_doctor_reports_orchestrator_available(routes_mod):
    """编排包就在本仓库里，doctor 必须报告可用。"""
    status, payload = _call(routes_mod.h_doctor)
    assert status == 200
    assert payload["orchestrator_available"] is True, payload.get("orchestrator_error")


def test_doctor_reports_ffmpeg_available(routes_mod):
    """本机 ffmpeg 走 imageio-ffmpeg，doctor 必须报告可用（拼接/封装依赖它）。"""
    status, payload = _call(routes_mod.h_doctor)
    assert status == 200
    assert payload["ffmpeg_available"] is True, payload.get("ffmpeg_error")


def test_doctor_never_claims_real_generation_on_this_machine(routes_mod):
    """本机没有模型权重，`real_generation_available` 必须是 False。

    这条是「不许假成功」的核心断言：一旦有人为了让界面好看把这里写死 True，
    用户会以为可以出片，然后拿到一堆占位媒体。
    """
    status, payload = _call(routes_mod.h_doctor)
    assert status == 200
    assert payload["real_generation_available"] is False, (
        "本机无模型权重，不得报告真实生成可用"
    )


# ---------------------------------------------------------------------------
# 其他路由
# ---------------------------------------------------------------------------


def test_projects_handler_returns_list(routes_mod):
    status, payload = _call(routes_mod.h_projects)
    assert status == 200, payload
    assert "projects" in payload
    assert isinstance(payload["projects"], list)


def test_minimal_workflow_handler_serves_valid_workflow(routes_mod):
    status, payload = _call(routes_mod.h_minimal_workflow)
    assert status == 200, payload
    assert isinstance(payload.get("nodes"), list) and payload["nodes"], "工作流没有 nodes"


def test_phase2_status_is_hidden(routes_mod):
    status, payload = _call(routes_mod.h_phase2_status)
    assert status == 200, payload
    assert payload["exposed_in_ui"] is False
    assert payload["phase2"]["exposed_in_ui"] is False


def test_project_open_requires_identifier(routes_mod):
    """既不给 id 也不给名字时必须 400，不能默默创建一个项目。"""
    status, payload = _call(routes_mod.h_project_open, body={}, method="POST")
    assert status == 400, f"期望 400，实际 {status}：{payload}"
    assert "project_id" in payload.get("error", "")


def test_project_open_creates_and_reopens(routes_mod, tmp_path, monkeypatch):
    """创建后能用返回的 id 再次打开（幂等）。"""
    from bettercomfyui.config import load_config

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(routes_mod, "_orchestrator_helpers", lambda: (
        lambda: REPO_ROOT, lambda c: Path(c.global_.project_root)
    ))
    # load_config 需要返回我们的临时配置
    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        mod = real_import(name, *a, **kw)
        return mod

    monkeypatch.setattr(importlib, "import_module", fake_import)

    status, payload = _call(
        routes_mod.h_project_open, body={"project_name": "面板测试项目"}, method="POST"
    )
    assert status == 200, payload
    pid = payload["project_id"]
    assert pid

    status2, payload2 = _call(
        routes_mod.h_project_open, body={"project_id": pid}, method="POST"
    )
    assert status2 == 200, payload2
    assert payload2["project_id"] == pid


def test_run_handler_rejects_bad_executor(routes_mod):
    status, payload = _call(
        routes_mod.h_run,
        body={"project_id": "x", "executor": "nonsense"},
        method="POST",
    )
    assert status == 400, payload
    assert "executor" in payload.get("error", "")


def test_run_handler_requires_project_id(routes_mod):
    status, payload = _call(routes_mod.h_run, body={}, method="POST")
    assert status == 400, payload


def test_all_declared_handlers_are_callable(routes_mod):
    """每个 `h_*` 处理器都必须是可调用的协程函数（防漏定义/防写错名）。"""
    names = [n for n in dir(routes_mod) if n.startswith("h_")]
    assert len(names) >= 6, f"处理器太少：{names}"
    for n in names:
        fn = getattr(routes_mod, n)
        assert asyncio.iscoroutinefunction(fn), f"{n} 不是协程函数"


def test_register_routes_reports_reason_when_unavailable(routes_mod):
    """没有 ComfyUI server 时必须返回 False **并给出原因**，不得静默。"""
    # 本测试进程里没有 ComfyUI 的 server 模块，所以应当是失败路径
    ok = routes_mod.register_routes()
    if not ok:
        assert routes_mod.ROUTES_ERROR, "注册失败但没有给出原因"
        assert isinstance(routes_mod.ROUTES_ERROR, str)


def test_route_module_has_no_bare_dunder_version(routes_mod):
    """**静态守卫**：`routes.py` 里不得出现裸 `__version__`。

    这个 bug 出现过两次（都是被陈旧副本覆盖所致）：`h_doctor` 直接引用
    `__version__`，而该名字在本模块里并不存在 → `/bettercomfyui/doctor` 返回 **500**，
    而面板打开后的第一个请求恰好就是它。启动日志完全看不出问题。

    用 AST 扫描比"调用一次处理器"更可靠：即使某个处理器暂时不可调用（例如缺依赖），
    这条静态检查依然能抓到漏掉的全局名。
    """
    import ast
    import builtins
    import inspect

    src = inspect.getsource(routes_mod)
    tree = ast.parse(src)

    # 收集模块级已定义的名字 + 真正的内置名
    module_names: set[str] = set(dir(builtins)) | {"__file__", "__name__", "__package__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    module_names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])

    offenders: list[str] = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        local = set(module_names)
        for a in list(fn.args.args) + list(fn.args.kwonlyargs):
            local.add(a.arg)
        if fn.args.vararg:
            local.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            local.add(fn.args.kwarg.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
            if isinstance(n, ast.arg):
                local.add(n.arg)
            if isinstance(n, ast.ExceptHandler) and n.name:
                local.add(n.name)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
                local.add(n.name)
        for n in ast.walk(fn):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id not in local):
                offenders.append(f"{fn.name}() 第 {n.lineno} 行引用了未定义的全局名 {n.id!r}")

    assert not offenders, (
        "routes.py 里存在未定义的全局名（会让对应路由返回 500）：\n  "
        + "\n  ".join(sorted(set(offenders)))
    )
