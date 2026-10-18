"""R1 落地：把「已有素材清单」注入系统提示词（docs/CONTRACTS.md 第 4 节）。

R1 的核心是**存在性判断只能由程序做**：本模块从 ``meta.json`` 读出真实素材库，
生成一段纯文本清单；AI 只负责「按提示词新增素材」，不得输出「已存在/不存在」结论。
因此注入文本自身也要说明「这是程序给出的权威清单」。

清单列（契约规定的四要素）：**类型 + 序号 + 名称 + 锁定状态**。
公共库（``LibraryScope.PUBLIC``）的行会显式追加 ``[PUBLIC]`` 标记 —— R5 要求
公共库的使用必须显式标注，不能与项目库混淆。

素材库为空 -> 返回空字符串，调用方据此走 A1B 的「空」分支跳过注入。
"""

from __future__ import annotations

from ..models.enums import LibraryScope, LockStatus
from .assets import AssetStore
from .errors import parse_enum
from .ids import version_sort_key

__all__ = ["build_existing_asset_manifest"]

_HEADER = (
    "[已有素材清单 | 由程序从素材库读取，是权威来源，未列出即为不存在 | "
    "列：类型 / 序号 / 名称 / 锁定状态]"
)
_PUBLIC_TAG = "[PUBLIC]"


def _locked_summary(asset: dict) -> str:
    """锁定状态描述：``LOCKED v2 HUMAN_PASS`` 或 ``UNLOCKED``。"""
    locked = [
        version
        for version in asset.get("versions", [])
        if version.get("lock_status") == LockStatus.LOCKED.value
    ]
    if not locked:
        return "UNLOCKED"
    newest = max(locked, key=lambda item: version_sort_key(item.get("version", "v0")))
    source = newest.get("lock_source") or "UNKNOWN"
    return f"LOCKED {newest.get('version')} {source}"


def build_existing_asset_manifest(store: AssetStore, *, library_scope=None) -> str:
    """生成「已有素材清单」文本；素材库为空返回 ``""``。

    ``library_scope`` 为 ``None`` 时包含项目库与公共库（公共库行带 ``[PUBLIC]``）。
    输出是**确定性**的：同一素材库内容两次调用逐字节相同（按创建时间与 ID 排序）。
    """
    scope = None if library_scope is None else parse_enum(LibraryScope, library_scope, kind="library_scope")
    assets = store.list_assets(scope=scope)
    if not assets:
        return ""

    lines = [_HEADER]
    counters: dict[str, int] = {}
    for asset in assets:
        asset_type = str(asset.get("asset_type"))
        counters[asset_type] = counters.get(asset_type, 0) + 1
        seq = counters[asset_type]
        name = str(asset.get("name") or "")
        line = f"{asset_type}\t{seq:02d}\t{name}\t{_locked_summary(asset)}"
        if asset.get("library_scope") == LibraryScope.PUBLIC.value:
            line = f"{line}\t{_PUBLIC_TAG}"
        lines.append(line)
    return "\n".join(lines)
