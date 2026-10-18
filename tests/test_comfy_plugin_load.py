"""按 **ComfyUI 的真实导入上下文**加载插件包。

与其他测试的区别（这是关键）：

* `tests/test_comfy_plugin.py` 以 `import bettercomfyui_comfy` 加载 —— 这是「开发态」；
* **ComfyUI 实际是以 `import custom_nodes.bettercomfyui_comfy` 加载的** ——
  父包名不同（`custom_nodes` vs 无），任何依赖 `__package__` 父层级的代码
  在两种语境下行为可能不同（本项目已经踩过一次相对导入的坑）。

因此本文件把插件目录拷进一个临时的 `custom_nodes/` 结构里，再按 ComfyUI 的方式
导入，专门验证「装上之后真的能跑」。

**同时也验证「缺编排包」场景**：ComfyUI 用户可能只拷了插件没装编排包，
这时插件**必须仍能加载**（节点可见、点击后给出可操作报错），
而不是让整个 ComfyUI 崩掉或让节点消失得无影无踪。
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = REPO_ROOT / "bettercomfyui_comfy"


@pytest.fixture()
def comfy_like_tree(tmp_path: Path) -> Path:
    """搭一个 ComfyUI 形态的目录：<tmp>/custom_nodes/bettercomfyui_comfy/。"""
    custom_nodes = tmp_path / "custom_nodes"
    custom_nodes.mkdir()
    (custom_nodes / "__init__.py").write_text("", encoding="utf-8")  # ComfyUI 目录本身是包
    shutil.copytree(PACK_DIR, custom_nodes / "bettercomfyui_comfy",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp_path


def _run_in_subprocess(code: str, *, cwd: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """在**独立进程**里跑，避免污染本测试进程的 sys.modules / sys.path。"""
    import os

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(REPO_ROOT)          # 让编排包 bettercomfyui 可导入
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )


def test_pack_loads_under_comfyui_module_context(comfy_like_tree: Path):
    """以 `custom_nodes.bettercomfyui_comfy` 加载必须成功且注册出节点。"""
    proc = _run_in_subprocess(
        """
        import sys, json
        sys.path.insert(0, ".")
        import custom_nodes.bettercomfyui_comfy as pack
        out = {
            "package": pack.__name__,
            "nodes": sorted(pack.NODE_CLASS_MAPPINGS),
            "display_names": len(pack.NODE_DISPLAY_NAME_MAPPINGS),
            "web_dir": str(pack.WEB_DIRECTORY),
            "phase2_exposed": pack.PHASE2_NODES_EXPOSED,
        }
        print("RESULT=" + json.dumps(out, ensure_ascii=False))
        """,
        cwd=comfy_like_tree,
    )
    assert proc.returncode == 0, f"以 ComfyUI 方式加载失败：\n{proc.stdout}\n{proc.stderr}"
    assert "RESULT=" in proc.stdout, f"没有输出结果：\n{proc.stdout}\n{proc.stderr}"

    import json

    payload = json.loads(proc.stdout.split("RESULT=", 1)[1].strip().splitlines()[0])
    assert payload["package"] == "custom_nodes.bettercomfyui_comfy", payload["package"]
    assert len(payload["nodes"]) >= 6, f"注册节点太少：{payload['nodes']}"
    assert payload["display_names"] == len(payload["nodes"])
    assert payload["web_dir"] == "./web"
    assert payload["phase2_exposed"] is False


def test_routes_helpers_work_under_both_contexts(comfy_like_tree: Path):
    """`_pkgn()` 必须给出**插件包**名，且动态子模块导入必须真的能用。

    这是实测抓到的真 bug 的回归：曾把 `__package__` 再 rsplit 一级得到
    `custom_nodes`，于是 `_mod("nodes")` 去 import `custom_nodes.nodes`，
    插件装进 ComfyUI 后所有面板路由都会失败。
    """
    proc = _run_in_subprocess(
        """
        import sys, json
        sys.path.insert(0, ".")
        import custom_nodes.bettercomfyui_comfy.routes as r
        m = r._mod("nodes")
        print("RESULT=" + json.dumps({"pkgn": r._pkgn(), "nodes_mod": m.__name__}))
        """,
        cwd=comfy_like_tree,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "RESULT=" in proc.stdout, proc.stdout

    import json as _json

    payload = _json.loads(proc.stdout.split("RESULT=", 1)[1].strip().splitlines()[0])
    assert payload["pkgn"] == "custom_nodes.bettercomfyui_comfy", (
        f"_pkgn() 返回 {payload['pkgn']!r}，应为 'custom_nodes.bettercomfyui_comfy'"
    )
    assert payload["nodes_mod"] == "custom_nodes.bettercomfyui_comfy.nodes", (
        f"动态导入到了错误的模块：{payload['nodes_mod']!r}"
    )


def test_plugin_still_loads_without_orchestrator(comfy_like_tree: Path):
    """**只装插件、没装编排包**时：插件必须仍能加载，节点仍可见。

    用户视角：节点出现 -> 点一下 -> 得到「缺编排包 + 怎么装」的可操作提示。
    绝不允许：整个 ComfyUI 崩掉，或节点静默消失。
    """
    proc = _run_in_subprocess(
        """
        import sys, json
        sys.path.insert(0, ".")
        # 把编排包挡住：模拟用户没装 bettercomfyui
        import builtins
        real_import = builtins.__import__
        def blocked(name, *a, **kw):
            if name == "bettercomfyui" or name.startswith("bettercomfyui."):
                raise ModuleNotFoundError(f"No module named '{name}'", name=name)
            return real_import(name, *a, **kw)
        builtins.__import__ = blocked

        import custom_nodes.bettercomfyui_comfy as pack
        info = pack.describe_bridge_info() if hasattr(pack, "describe_bridge_info") else None
        print("RESULT=" + json.dumps({
            "nodes": sorted(pack.NODE_CLASS_MAPPINGS),
            "node_count": len(pack.NODE_CLASS_MAPPINGS),
        }, ensure_ascii=False))
        """,
        cwd=comfy_like_tree,
    )
    assert proc.returncode == 0, (
        "缺编排包时插件加载失败 —— ComfyUI 会看到「节点不见了」而不是可操作报错。\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "RESULT=" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"
    import json

    payload = json.loads(proc.stdout.split("RESULT=", 1)[1].strip().splitlines()[0])
    assert payload["node_count"] >= 6, "缺编排包时节点仍必须注册（否则用户无从得知哪里错了）"


def test_node_error_message_is_actionable_without_orchestrator(comfy_like_tree: Path):
    """缺编排包时，点节点必须得到**可操作**错误（含修复指引），而不是静默空返回。"""
    proc = _run_in_subprocess(
        """
        import sys, json
        sys.path.insert(0, ".")
        import builtins
        real_import = builtins.__import__
        def blocked(name, *a, **kw):
            if name == "bettercomfyui" or name.startswith("bettercomfyui."):
                raise ModuleNotFoundError(f"No module named '{name}'", name=name)
            return real_import(name, *a, **kw)
        builtins.__import__ = blocked

        import custom_nodes.bettercomfyui_comfy as pack
        node = pack.NODE_CLASS_MAPPINGS["BetterComfyUIDoctor"]()
        res = node.run()
        text = res["result"][0]
        print("RESULT=" + json.dumps({"text": text}, ensure_ascii=False))
        """,
        cwd=comfy_like_tree,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    import json

    text = json.loads(proc.stdout.split("RESULT=", 1)[1].strip().splitlines()[0])["text"]
    assert "编排包" in text, f"体检输出未说明问题：{text[:300]}"
    # 必须给出修复方法，而不是只说「失败」
    assert ("pip install" in text) or ("BETTERCOMFYUI_HOME" in text) or ("复制" in text), (
        f"错误信息不可操作（没说怎么修）：{text[:400]}"
    )


def test_manifest_declares_web_directory_relative(comfy_like_tree: Path):
    """`WEB_DIRECTORY` 必须是相对路径（ComfyUI 会用 `os.path.join(模块目录, 它)`）。"""
    proc = _run_in_subprocess(
        """
        import sys, json
        sys.path.insert(0, ".")
        import custom_nodes.bettercomfyui_comfy as pack
        print("RESULT=" + json.dumps({"web": pack.WEB_DIRECTORY}))
        """,
        cwd=comfy_like_tree,
    )
    assert proc.returncode == 0, proc.stderr
    import json

    web = json.loads(proc.stdout.split("RESULT=", 1)[1].strip().splitlines()[0])["web"]
    assert not Path(web).is_absolute(), f"WEB_DIRECTORY 必须是相对路径，实际 {web}"
    assert web.startswith("."), f"WEB_DIRECTORY 应写成 './web' 形式，实际 {web}"
