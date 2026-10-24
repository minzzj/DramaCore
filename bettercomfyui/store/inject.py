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

__all__ = ["build_existing_asset_manifest", "build_manifest_alias_map"]

_HEADER = (
    "[已有素材清单 | 由程序从素材库读取，是权威来源，未列出即为不存在 | "
    "列：类型 / 序号 / 名称 / 锁定状态 / 复用ID]"
)
_HEADER_LEGACY = (
    "[已有素材清单 | 由程序从素材库读取，是权威来源，未列出即为不存在 | "
    "列：类型 / 序号 / 名称 / 锁定状态]"
)
_PUBLIC_TAG = "[PUBLIC]"


def _iter_manifest_entries(assets: list[dict]):
    """按稳定顺序产出 ``(asset, alias)``；alias = A01/A02/…（跨运行稳定，仅追加不重排）。"""
    counters: dict[str, int] = {}
    for idx, asset in enumerate(assets, start=1):
        asset_type = str(asset.get("asset_type"))
        counters[asset_type] = counters.get(asset_type, 0) + 1
        yield asset, counters[asset_type], f"A{idx:02d}"


def build_manifest_alias_map(store: AssetStore, *, library_scope=None) -> dict[str, dict]:
    """复用 ID 别名表：``A07 -> {asset_id, asset_type, name, locked}``（与清单行末 ID 一一对应）。"""
    scope = None if library_scope is None else parse_enum(LibraryScope, library_scope, kind="library_scope")
    assets = store.list_assets(scope=scope)
    out: dict[str, dict] = {}
    for asset, _seq, alias in _iter_manifest_entries(assets):
        out[alias] = {
            "asset_id": str(asset.get("asset_id") or ""),
            "asset_type": str(asset.get("asset_type") or ""),
            "name": str(asset.get("name") or ""),
            "locked": _locked_summary(asset).startswith("LOCKED"),
        }
    return out


def build_existing_asset_manifest(store: AssetStore, *, library_scope=None,
                                  with_ids: bool = False) -> str:
    """生成「已有素材清单」文本；素材库为空返回 ``""``。

    ``library_scope`` 为 ``None`` 时包含项目库与公共库（公共库行带 ``[PUBLIC]``）。
    ``with_ids=True`` 时每行末尾追加 ``[A07]`` 形式的复用 ID（供分镜 reused_asset_ids 引用）。
    输出是**确定性**的：同一素材库内容两次调用逐字节相同（按创建时间与 ID 排序）。
    """
    scope = None if library_scope is None else parse_enum(LibraryScope, library_scope, kind="library_scope")
    assets = store.list_assets(scope=scope)
    if not assets:
        return ""

    lines = [_HEADER if with_ids else _HEADER_LEGACY]
    for asset, seq, alias in _iter_manifest_entries(assets):
        asset_type = str(asset.get("asset_type"))
        name = str(asset.get("name") or "")
        line = f"{asset_type}\t{seq:02d}\t{name}\t{_locked_summary(asset)}"
        if asset.get("library_scope") == LibraryScope.PUBLIC.value:
            line = f"{line}\t{_PUBLIC_TAG}"
        if with_ids:
            line = f"{line}\t[{alias}]"
        lines.append(line)
    return "\n".join(lines)
