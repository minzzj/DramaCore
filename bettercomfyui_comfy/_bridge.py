"""把 Better ComfyUI 编排层接入 ComfyUI 的桥接层。

**为什么需要它**

本插件是 ComfyUI 自定义节点包；真正的业务逻辑在 `bettercomfyui/` 编排包里。
两者可能处于完全不同的位置：

* 开发/本仓库：`<repo>/bettercomfyui_comfy/` 与 `<repo>/bettercomfyui/` 平级；
* 已安装到 ComfyUI：插件在 `ComfyUI/custom_nodes/bettercomfyui_comfy/`，
  而编排包可能装在 venv 的 site-packages，或仍在本仓库。

因此这里提供**带可操作报错的定位逻辑**，而不是脆弱的固定相对路径。

**绝不静默失败**：找不到编排包时抛 `BridgeError` 并列出尝试过的路径与修复方法。
静默降级成「节点存在但点了没反应」是本项目明令禁止的假成功。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

__all__ = ["BridgeError", "load_orchestrator", "orchestrator_available", "describe_bridge"]

#: 环境变量可强制指定编排包所在目录（用于非标准部署）
ENV_ORCH_ROOT = "BETTERCOMFYUI_HOME"

_ORCH_PACKAGE = "bettercomfyui"
_bridge_state: dict = {"root": None, "error": None, "tried": []}


class BridgeError(RuntimeError):
    """找不到或无法导入编排包。信息必须面向使用者且可操作。"""


def _candidate_roots() -> list[Path]:
    """按可能性从高到低列出候选仓库根目录。"""
    here = Path(__file__).resolve().parent          # .../bettercomfyui_comfy
    roots: list[Path] = []

    env = os.environ.get(ENV_ORCH_ROOT)
    if env:
        roots.append(Path(env))

    # 1) 同仓库平级：<repo>/bettercomfyui/
    roots.append(here.parent)
    # 2) 插件目录内部（若把编排包一起拷进来）
    roots.append(here)
    # 3) ComfyUI/custom_nodes/bettercomfyui_comfy/ -> 再上一级或两级
    roots.append(here.parent.parent)
    roots.append(here.parent.parent.parent)

    # 去重且保持顺序
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        try:
            key = r.resolve()
        except OSError:
            key = r
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _try_root(root: Path) -> bool:
    """尝试把 root 加入 sys.path 并导入编排包。成功返回 True。"""
    pkg_dir = root / _ORCH_PACKAGE
    if not (pkg_dir / "__init__.py").is_file():
        return False
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        importlib.import_module(_ORCH_PACKAGE)
    except Exception:  # noqa: BLE001 - 记下原因，继续试下一个候选
        _bridge_state["tried"].append(f"{root} （目录存在但导入失败）")
        return False
    return True


def load_orchestrator(*, force: bool = False):
    """返回已导入的 `bettercomfyui` 包。失败抛 `BridgeError`。

    结果会被缓存，重复调用是廉价的。
    """
    if not force:
        cached = sys.modules.get(_ORCH_PACKAGE)
        if cached is not None and _bridge_state["root"] is not None:
            return cached
        if _bridge_state["error"] is not None and _bridge_state["root"] is None:
            raise BridgeError(_bridge_state["error"])

    _bridge_state["tried"] = []

    # 已在 sys.modules（例如被其他插件先导入）
    existing = sys.modules.get(_ORCH_PACKAGE)
    if existing is not None and getattr(existing, "__file__", None):
        _bridge_state["root"] = Path(existing.__file__).resolve().parent.parent
        _bridge_state["error"] = None
        return existing

    for root in _candidate_roots():
        if _try_root(root):
            _bridge_state["root"] = root
            _bridge_state["error"] = None
            return sys.modules[_ORCH_PACKAGE]

    tried = "\n".join(f"  - {t}" for t in _bridge_state["tried"]) or "  （无候选目录包含 bettercomfyui/）"
    msg = (
        "Better ComfyUI 插件已加载，但找不到业务编排包 `bettercomfyui`，节点无法工作。\n"
        "已尝试的候选位置：\n"
        f"{tried}\n"
        "修复方法（任选其一）：\n"
        "  1) 在 ComfyUI 的 Python 环境里安装编排包："
        "`pip install bettercomfyui`（或从本仓库 `pip install -e .`）；\n"
        f"  2) 设置环境变量 {ENV_ORCH_ROOT} 指向包含 `bettercomfyui/` 的目录；\n"
        "  3) 把 `bettercomfyui/` 目录整体复制到 "
        "`ComfyUI/custom_nodes/bettercomfyui_comfy/` 内部。\n"
        "注意：本插件**不会**在没有编排包的情况下假装可用。"
    )
    _bridge_state["error"] = msg
    raise BridgeError(msg)


def orchestrator_available() -> tuple[bool, str]:
    """不抛异常的可用性探测，供节点在构造期给出友好提示。"""
    try:
        load_orchestrator()
        return True, ""
    except BridgeError as exc:
        return False, str(exc)


def describe_bridge() -> dict:
    """诊断信息，供 doctor 类节点与日志使用。"""
    ok, err = orchestrator_available()
    pkg = sys.modules.get(_ORCH_PACKAGE)
    return {
        "available": ok,
        "error": err,
        "orchestrator_root": str(_bridge_state["root"]) if _bridge_state["root"] else None,
        "orchestrator_file": getattr(pkg, "__file__", None),
        "version": getattr(pkg, "__version__", None) if pkg else None,
        "python": sys.version.split()[0],
        "env_override": os.environ.get(ENV_ORCH_ROOT),
    }
