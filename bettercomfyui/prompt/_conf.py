"""prompt 层内部的配置读取薄层（私有模块，不属于对外契约）。

为什么需要它：``Config`` 类由 Lead 独占实现（CONTRACTS §9），且
``models/entities.py::PromptParams`` 只覆盖了部分键。prompt 层因此不假设
配置对象的确切类型，统一通过本模块读取：

* ``dict``（含嵌套 dict）：按 ``"prompt.model_provider"`` 先做嵌套查找，
  失败后按**扁平点号键** ``{"prompt.model_provider": ...}`` 查找；
* 对象（``Config`` / ``PromptParams`` / dataclass / SimpleNamespace）：
  先走属性链 ``cfg.prompt.model_provider``，再走 ``cfg.get(path)``，
  再走 ``cfg.to_dict()/as_dict()/model_dump()`` 转出的 dict。

读取失败返回 ``default``；**必填键的缺失由调用方抛 GatewayError 并列出键名**，
本模块不替调用方做静默兜底。
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

__all__ = [
    "cfg_get",
    "cfg_get_any",
    "cfg_missing",
    "cfg_str",
    "cfg_int",
    "cfg_float",
    "cfg_bool",
    "resolve_env_value",
    "ENV_PATTERN",
]

#: 支持 ``${ENV:NAME}`` 与 ``${NAME}`` 两种写法，避免在配置样例里写真实密钥。
ENV_PATTERN = re.compile(r"^\$\{(?:ENV:)?([A-Za-z_][A-Za-z0-9_]*)\}$")

_MISSING = object()


def _as_dict(obj: Any) -> dict | None:
    if isinstance(obj, dict):
        return obj
    for method in ("to_dict", "as_dict", "model_dump", "dict"):
        func = getattr(obj, method, None)
        if callable(func):
            try:
                value = func()
            except Exception:  # pragma: no cover - 防御性
                continue
            if isinstance(value, dict):
                return value
    return None


def _lookup_dict(data: dict, path: str) -> Any:
    parts = path.split(".")
    node: Any = data
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
            continue
        break
    else:
        return node
    if path in data:
        return data[path]
    return _MISSING


def cfg_get(cfg: Any, path: str, default: Any = None) -> Any:
    """按点号路径读取配置；读不到返回 ``default``。"""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        value = _lookup_dict(cfg, path)
        return default if value is _MISSING else value

    node: Any = cfg
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part, _MISSING)
        else:
            node = getattr(node, part, _MISSING)
        if node is _MISSING:
            break
    if node is not _MISSING:
        return node

    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            value = getter(path, _MISSING)
        except TypeError:
            value = _MISSING
        if value is not _MISSING:
            return value

    data = _as_dict(cfg)
    if data is not None:
        value = _lookup_dict(data, path)
        if value is not _MISSING:
            return value
    return default


def cfg_get_any(cfg: Any, paths: Sequence[str], default: Any = None) -> Any:
    """按顺序尝试多个候选键，返回第一个「有值」的结果（None/空串视为无值）。"""
    for path in paths:
        value = cfg_get(cfg, path, None)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def resolve_env_value(value: Any) -> Any:
    """把 ``"${ENV:NAME}"`` 展开为环境变量值；非字符串原样返回。

    环境变量缺失时**抛 KeyError**（由调用方转成 GatewayError），
    避免把空 key 当成「已配置」。
    """
    if not isinstance(value, str):
        return value
    m = ENV_PATTERN.match(value.strip())
    if not m:
        return value
    name = m.group(1)
    if name not in os.environ:
        raise KeyError(name)
    return os.environ[name]


def cfg_missing(cfg: Any, paths: Iterable[str]) -> list[str]:
    """返回缺失（None 或空白字符串）的键名列表，顺序与入参一致。"""
    missing: list[str] = []
    for path in paths:
        value = cfg_get(cfg, path, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(path)
    return missing


def cfg_str(cfg: Any, path: str, default: str | None = None) -> str | None:
    value = cfg_get(cfg, path, None)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    return str(value)


def cfg_int(cfg: Any, path: str, default: int) -> int:
    value = cfg_get(cfg, path, None)
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def cfg_float(cfg: Any, path: str, default: float) -> float:
    value = cfg_get(cfg, path, None)
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def cfg_bool(cfg: Any, path: str, default: bool) -> bool:
    value = cfg_get(cfg, path, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "on", "1", "是", "开"):
            return True
        if text in ("false", "no", "off", "0", "否", "关"):
            return False
    return default
