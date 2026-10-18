"""配置读取适配层。

`Config` 类由 Lead 独占实现（契约 §9），本模块只做**宽松、只读**访问：
- 支持 `Mapping`（嵌套 dict）
- 支持对象属性链（`cfg.video_review.cnt2_limit`）
- 支持带 `get("a.b")` 方法的对象
绝不在缺配置时静默取硬编码值：`required=True` 时缺项直接抛 `DirectorConfigError`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bettercomfyui.director.errors import DirectorConfigError

__all__ = ["cfg_get", "cfg_section"]

_MISSING = object()

#: `models.entities.Config` 的字段名是 `global_`（避开 Python 关键字），
#: 但契约 §9 的配置路径写作 `global.*`。这里做显式别名，避免静默取不到值。
_ALIASES = {"global": "global_"}


def _walk_mapping(cfg: Mapping, path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _walk_attrs(cfg: Any, path: str) -> Any:
    node = cfg
    for part in path.split("."):
        name = part if hasattr(node, part) else _ALIASES.get(part, part)
        if not hasattr(node, name):
            return _MISSING
        node = getattr(node, name)
    return node


def cfg_get(cfg: Any, path: str, default: Any = None, *, required: bool = False) -> Any:
    """按点分路径读取配置；找不到时返回 default（required=True 则抛错）。"""
    if cfg is None:
        if required:
            raise DirectorConfigError(f"缺少配置项 {path}（cfg 为 None）")
        return default

    value = _MISSING
    if isinstance(cfg, Mapping):
        value = _walk_mapping(cfg, path)
    if value is _MISSING:
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                value = getter(path, _MISSING)
            except TypeError:
                value = _MISSING
            if value is _MISSING:
                # 某些实现只接受单参数 get
                try:
                    value = getter(path)
                except Exception:  # noqa: BLE001 - 第三方 Config 行为未知
                    value = _MISSING
    if value is _MISSING:
        value = _walk_attrs(cfg, path)
    if value is _MISSING or value is None:
        # 允许一段一层地回退：如 cfg.get("video_review") 再取 cnt2_limit
        head, _, tail = path.partition(".")
        if tail:
            section = cfg_get(cfg, head, None)
            if section is not None:
                nested = cfg_get(section, tail, _MISSING)
                if nested is not _MISSING and nested is not None:
                    return nested
        if required:
            raise DirectorConfigError(f"缺少配置项 {path}（不允许隐式默认值）")
        return default
    return value


def cfg_section(cfg: Any, path: str) -> dict:
    """读取配置段并规范化为 dict；缺段返回空 dict。"""
    value = cfg_get(cfg, path, None)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    as_dict = getattr(value, "__dict__", None)
    if isinstance(as_dict, dict):
        return {k: v for k, v in as_dict.items() if not k.startswith("_")}
    raise DirectorConfigError(f"配置段 {path} 不是映射类型：{type(value).__name__}")
