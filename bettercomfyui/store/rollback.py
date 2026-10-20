# -*- coding: utf-8 -*-
"""ROUND55-7：素材版本回滚模块（只读校验 + LOCK 迁移 + 审计，不删除历史版本）。

需求（Lead ROUND55 专项 7）
-------------------------
素材库已有「只追加 + LOCK 绑定 asset_id/version/content_hash」的版本机制
（``bettercomfyui/store/assets.py``），但没有「回滚到旧版本」入口。本模块提供：

1. :func:`list_asset_versions`：列出一个素材（或全项目）每个版本的可展示信息
   （版本号/创建时间/审核状态/LOCK 状态/文件是否存在/哈希校验是否通过/分数）；
2. :func:`rollback_asset`：把「当前 LOCK」恢复为 ``target_version``：
   * 目标版本必须存在，且磁盘文件指纹与版本元数据声明的 ``content_hash`` 一致；
     不一致默认**拒绝**（``require_hash_ok=True``），错误信息明确到版本/声明值/实际值/文件路径；
   * 回滚后把其它版本的 LOCK 撤销（``clear_lock`` 留痕，不删除任何版本/文件），
     并确保目标版本是 LOCKED（目标原本未 LOCK 时需要 ``revalidate_lock=True`` 显式授权重新 LOCK）；
   * 写项目级审计日志 ``projects/<project_id>/_work/rollback_log.jsonl``（JSON Lines，追加写）；
   * 返回 ``audit`` 字段，明确「旧结论失效 / 需重新生成 / 需重新挂参考图」——绝不假装后续无需动作。
3. 供 Lead 注册 HTTP 路由（``/dramacore/rollback/list|apply``）的函数级契约与离线 CLI ``main()``。
   路由与 UI 不在本模块实现（Lead/UI 智能体集成）。

设计边界（如实记录，不夸大）
--------------------------
* **不删除任何历史版本**：只改 LOCK 状态（``clear_lock``/``set_lock``），版本目录与文件原样保留。
* **哈希门禁**：回滚前重算磁盘 SHA-256 并与版本元数据比对；不一致默认拒绝。因为「内容被替换后
  回滚」等于用一个未授权的旧文件冒充 LOCK（R3），必须拒绝而不是静默放行。
* **旧结论失效**：回滚恢复的是「旧版本的历史 LOCK 记录」，它**不是**一次新的审核结论。
  所有引用该素材的段（反查 ``AssetStore.referencing_segments``）需要重新生成/重挂参考图并重跑
  视频审核；本模块把这些写进 ``audit`` 与日志，不把回滚伪装成「无需返工」。
* **并发**：本模块对同一项目用文件锁 ``_work/rollback.lock`` 串行化「回滚」操作，避免两个回滚
  互相覆盖 LOCK；但它不能阻止其它模块（审核提交 set_lock / clear_lock）在同一时刻写 LOCK。
  调用方（路由层）应在回滚前后复核 ``latest_locked_version``，并在审核流水线并发的阶段避免回滚。
* **兼容**：只依赖 store 层公开 API（AssetStore / ProjectPaths / errors / ids / lockfile），
  不改 ``one_stop_node.py``、``nodes.py``、UI，也不改 store 现有文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models.enums import LockSource, LockStatus
from .assets import AssetStore
from .atomic import lock_path_for
from .errors import (
    AssetNotFoundError,
    ContentHashMismatchError,
    StoreError,
    VersionNotFoundError,
    parse_enum,
)
from .ids import content_hash_of, format_version, parse_content_hash, version_sort_key
from .lockfile import file_lock
from .paths import ProjectPaths

__all__ = [
    "RollbackError",
    "list_asset_versions",
    "rollback_asset",
    "main",
    "ROLLBACK_LOG_RELPATH",
]

#: 项目内审计日志相对路径（相对 ``projects/<project_id>/``）。
ROLLBACK_LOG_RELPATH = ("_work", "rollback_log.jsonl")

#: 回滚失败错误码（供路由层映射 HTTP 状态码/UI 展示）。
ERR_TARGET_NOT_FOUND = "TARGET_VERSION_NOT_FOUND"
ERR_TARGET_NOT_LOCKED = "TARGET_VERSION_NOT_LOCKED"
ERR_LOCK_RECORD_INCONSISTENT = "TARGET_LOCK_RECORD_INCONSISTENT"
ERR_FILE_MISSING = "TARGET_FILE_MISSING"
ERR_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
ERR_AMBIGUOUS_ASSET = "AMBIGUOUS_ASSET"
ERR_POSTCONDITION = "POSTCONDITION_FAILED"
ERR_IO = "IO_ERROR"

_DEFAULT_ROLLBACK_LOCK_TIMEOUT_S = 30.0


class RollbackError(StoreError):
    """回滚业务错误（带机器可读 ``code``，供 HTTP 路由/UI 使用）。"""

    def __init__(self, message: str, *, code: str = "ROLLBACK_ERROR", **context: Any) -> None:
        super().__init__(message, code=code, **context)
        self.rollback_code = code


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _compact_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _project_root_from_config() -> Path:
    """默认项目根：环境变量 ``BETTERCOMFYUI_HOME`` 优先，否则仓库根 + 配置 project_root。"""
    env_home = str(os.environ.get("BETTERCOMFYUI_HOME", "") or "").strip()
    repo = Path(env_home) if env_home else Path(__file__).resolve().parents[2]
    try:
        from bettercomfyui.config import load_config  # noqa: PLC0415

        cfg = load_config(repo_root=repo)
        root = Path(str(getattr(getattr(cfg, "global_", None), "project_root", None) or "projects"))
    except Exception:  # noqa: BLE001 - 配置读失败时退回 repo/projects（仍项目隔离）
        root = Path("projects")
    return (repo / root) if not root.is_absolute() else root


def _resolve_paths(project_id: str, root: Path | str | None) -> ProjectPaths:
    projects_root = Path(root) if root is not None else _project_root_from_config()
    return ProjectPaths(root=projects_root, project_id=str(project_id))


def _log_path(paths: ProjectPaths) -> Path:
    return paths.project_dir.joinpath(*ROLLBACK_LOG_RELPATH)


def _append_log(paths: ProjectPaths, record: dict[str, Any]) -> Path:
    """追加写回滚审计日志（JSON Lines，文件锁 + fsync）。日志失败不静默吞掉？"""
    target = _log_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with file_lock(lock_path_for(target), timeout_s=_DEFAULT_ROLLBACK_LOCK_TIMEOUT_S):
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    return target


def _resolve_asset(store: AssetStore, asset_id_or_name: str) -> dict[str, Any]:
    """按 asset_id 精确匹配；否则按名称（归一化）匹配；多匹配按歧义拒绝。"""
    query = str(asset_id_or_name or "").strip()
    if not query:
        raise RollbackError("asset_id_or_name 不能为空", code=ERR_AMBIGUOUS_ASSET)
    asset = store.get_asset(query)
    if asset is not None:
        return asset
    matches = store.find_by_normalized_name(query)
    if not matches:
        raise AssetNotFoundError(
            "素材不存在（既不是 asset_id 也没有同名素材）",
            asset_id=query,
            project_id=store.project_id,
        )
    if len(matches) > 1:
        raise RollbackError(
            f"素材名称「{query}」匹配到 {len(matches)} 个素材，拒绝猜测；请改用 asset_id 回滚",
            code=ERR_AMBIGUOUS_ASSET,
            candidates=[m.get("asset_id") for m in matches],
        )
    return matches[0]


def _hash_view(version: dict[str, Any], paths: ProjectPaths | None) -> dict[str, Any]:
    """计算某个版本文件的「声明哈希 / 实际哈希 / 是否存在」，不抛错（供 list 展示）。"""
    declared = str(version.get("content_hash") or "")
    out: dict[str, Any] = {
        "file_path": version.get("file_path"),
        "file_exists": False,
        "hash_declared": declared,
        "hash_actual": None,
        "hash_ok": None,
        "hash_error": None,
    }
    try:
        if paths is None:
            raise StoreError("缺少项目路径上下文，无法解析版本文件")
        abs_path = paths.absolute_from_relative(version.get("file_path"))
        out["file_exists"] = bool(abs_path.is_file())
        if out["file_exists"]:
            actual = content_hash_of(abs_path)
            out["hash_actual"] = actual
            out["hash_ok"] = bool(declared and actual == declared)
        else:
            out["hash_ok"] = False
    except Exception as exc:  # noqa: BLE001 - 展示接口不因单条异常整体失败
        out["hash_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _pick_score(version: dict[str, Any]) -> Any:
    extra = version.get("extra") or {}
    for key in ("avg_score", "score", "review_score", "final_score"):
        if key in extra:
            return extra[key]
    scores = extra.get("scores")
    if isinstance(scores, dict) and scores:
        numeric = [float(v) for v in scores.values() if isinstance(v, (int, float))]
        if numeric:
            return round(sum(numeric) / len(numeric), 3)
    return None


def _version_row(
    asset: dict[str, Any],
    version: dict[str, Any],
    paths: ProjectPaths | None,
    *,
    current_locked_version: str | None,
    human_label: str | None = None,
) -> dict[str, Any]:
    hv = _hash_view(version, paths)
    lock_status = str(version.get("lock_status") or "")
    row = {
        "asset_id": asset.get("asset_id"),
        "asset_type": asset.get("asset_type"),
        "name": asset.get("name"),
        "human_label": human_label,
        "version": version.get("version"),
        "created_at": version.get("created_at"),
        "status": version.get("status"),
        "lock_status": lock_status,
        "locked": lock_status == LockStatus.LOCKED.value,
        "lock_source": version.get("lock_source"),
        "review_cycle_id": version.get("review_cycle_id"),
        "review_history_count": len(version.get("review_history") or []),
        "superseded_by": version.get("superseded_by"),
        "score": _pick_score(version),
        "is_current_lock": bool(current_locked_version and version.get("version") == current_locked_version),
    }
    row.update(hv)
    return row


# ---------------------------------------------------------------------------
# 公开 API 1：列出素材版本
# ---------------------------------------------------------------------------
def list_asset_versions(
    project_id: str,
    asset_id_or_name: str | None = None,
    *,
    root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """列出素材库版本（UI 展示用）。

    参数
    ----
    project_id: 项目 id（32 位 hex，由 ProjectPaths 校验）。
    asset_id_or_name: 素材 asset_id 或显示名；``None`` 表示列出**整个项目**所有素材的版本。
    root: 项目根目录（``projects/`` 根）；默认按 ``BETTERCOMFYUI_HOME``/仓库配置解析。
          显式传入便于测试/离线 CLI（不改变生产默认）。

    返回
    ----
    ``list[dict]``，每行一个版本，字段：
      asset_id/asset_type/name、version、created_at、status、lock_status/locked、
      lock_source/review_cycle_id/review_history_count/superseded_by、score、
      file_path/file_exists/hash_declared/hash_actual/hash_ok/hash_error、is_current_lock。
    排序：素材（创建时间, asset_id）→ 版本（v 数字升序）。**只读，不写任何文件。**
    """
    paths = _resolve_paths(project_id, root)
    store = AssetStore(paths)
    index = store.load_index()
    assets: list[dict[str, Any]]
    if asset_id_or_name is None:
        assets = list(index.get("assets") or [])
        assets.sort(key=lambda a: (a.get("created_at") or "", a.get("asset_id") or ""))
    else:
        assets = [_resolve_asset(store, asset_id_or_name)]

    rows: list[dict[str, Any]] = []
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        try:
            current = store.latest_locked_version(asset_id)
        except StoreError:
            current = None
        try:
            label = store.human_label(asset_id)
        except Exception:  # noqa: BLE001 - 展示字段失败不影响列举
            label = None
        versions = list(asset.get("versions") or [])
        versions.sort(key=lambda v: version_sort_key(str(v.get("version") or "")))
        for version in versions:
            rows.append(
                _version_row(
                    asset, version, paths,
                    current_locked_version=current, human_label=label,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# 公开 API 2：回滚
# ---------------------------------------------------------------------------
def rollback_asset(
    project_id: str,
    asset_id_or_name: str,
    target_version: str,
    *,
    operator: str = "",
    reason: str = "",
    require_hash_ok: bool = True,
    revalidate_lock: bool = True,
    lock_source: str = LockSource.HUMAN_PASS.value,
    review_cycle_id: str | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """把「当前 LOCK」恢复为 ``target_version``（版本级、带哈希门禁与审计，不删除历史版本）。

    行为
    ----
    1. 解析素材（asset_id 精确优先，其次显示名；歧义拒绝）；
    2. 目标版本必须存在；目标文件必须存在；磁盘 SHA-256 必须等于版本元数据
       ``content_hash``（``require_hash_ok=True`` 时不一致直接抛 ``ContentHashMismatchError``）；
    3. 目标版本未 LOCKED 时：``revalidate_lock=False`` → 拒绝（错误码
       ``TARGET_VERSION_NOT_LOCKED``，明确提示需要显式重新 LOCK）；
       为 ``True`` 时按 ``lock_source``（默认 HUMAN_PASS=操作者手工回滚）重新 LOCK，
       使用 ``review_cycle_id``（默认自动生成 ``rollback:<operator>:<UTC时间>``）留痕；
    4. 撤销**其它**版本的 LOCK（``clear_lock(reason=...)`` 留痕，不删除版本/文件），
       保证 ``latest_locked_version`` == 目标版本；
    5. 追加写项目级 ``_work/rollback_log.jsonl``（成功/拒绝都记，含 from→to、哈希、操作者、原因）；
    6. 返回 ``audit``：``old_conclusions_invalidated=True``、``requires_regeneration=True``、
       ``requires_reference_remount=True``、``affected_segments``（反查引用）、``next_actions``。

    风险与并发
    ----------
    * 回滚恢复的是**历史 LOCK 记录**，不是新审核结论；旧结论失效由 ``audit`` 显式声明；
    * 同一项目的回滚用 ``_work/rollback.lock`` 串行化；与审核流水线并发的 set_lock/clear_lock
      需调用方在路由层做「回滚前后复核 latest_locked_version + 冲突返回 409」；
    * 日志写失败会抛出（不静默）——审计缺失不能让回滚看起来成功。
    """
    paths = _resolve_paths(project_id, root)
    paths.ensure_layout()
    rollback_lock = paths.project_dir / "_work" / "rollback.lock"
    rollback_lock.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(lock_path_for(rollback_lock), timeout_s=_DEFAULT_ROLLBACK_LOCK_TIMEOUT_S):
        store = AssetStore(paths)
        asset = _resolve_asset(store, asset_id_or_name)
        asset_id = str(asset.get("asset_id") or "")
        asset_name = asset.get("name")
        target_ver = str(target_version or "").strip()
        if not target_ver:
            raise RollbackError("target_version 不能为空", code=ERR_TARGET_NOT_FOUND)
        target = store.get_version(asset_id, target_ver)
        if target is None:
            _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                        target_version=target_ver, code=ERR_TARGET_NOT_FOUND,
                        message="目标版本不存在")
            raise VersionNotFoundError(
                "回滚失败：目标版本不存在", asset_id=asset_id, version=target_ver
            )
        target_ver = str(target.get("version") or target_ver)

        # ---- 2) 文件存在 + 哈希门禁（R3：LOCK 授权的必须是被声明的那些字节） ----
        try:
            abs_path = paths.absolute_from_relative(target.get("file_path"))
        except Exception as exc:  # noqa: BLE001
            _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                        target_version=target_ver, code=ERR_IO, message=f"目标版本路径越界：{exc}")
            raise RollbackError(
                f"回滚失败：目标版本文件路径无法解析（R5）：{exc}", code=ERR_IO
            ) from exc
        if not abs_path.is_file():
            _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                        target_version=target_ver, code=ERR_FILE_MISSING,
                        message="目标版本文件不在磁盘上")
            raise RollbackError(
                f"回滚失败：目标版本 {target_ver} 的文件不在磁盘上：{abs_path}（历史版本文件丢失，"
                "无法证明回滚内容与 LOCK 声明一致）",
                code=ERR_FILE_MISSING,
                file_path=str(abs_path),
            )
        declared = parse_content_hash(target.get("content_hash"))
        actual = content_hash_of(abs_path)
        hash_ok = actual == declared
        warnings: list[str] = []
        if not hash_ok:
            if require_hash_ok:
                _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                            target_version=target_ver, code=ERR_HASH_MISMATCH,
                            message=f"哈希不一致 declared={declared} actual={actual}",
                            declared_hash=declared, actual_hash=actual)
                raise ContentHashMismatchError(
                    "回滚被拒绝：目标版本磁盘文件的实际指纹与版本元数据声明不一致（R3）。"
                    f"目标版本 {target_ver}：声明 {declared}，实际 {actual}，文件 {abs_path}。"
                    "内容被替换/篡改的旧版本不能冒充 LOCK 授权；请确认文件来源后走新版本审核。",
                    asset_id=asset_id,
                    version=target_ver,
                    declared_hash=declared,
                    actual_hash=actual,
                    file_path=str(abs_path),
                )
            warnings.append(
                f"⚠️ require_hash_ok=False：目标版本哈希不一致仍继续（声明 {declared} / 实际 {actual}）"
                "——该回滚不满足 R3，禁止用于生成"
            )

        locks_before = sorted(store.locked_versions(asset_id), key=version_sort_key)
        from_version = locks_before[-1] if locks_before else None
        target_was_locked = bool(target.get("lock_status") == LockStatus.LOCKED.value)

        # ---- 3) 目标版本 LOCK 状态处理 ----
        relocked = False
        cycle = str(review_cycle_id or "").strip()
        if not target_was_locked:
            if not revalidate_lock:
                _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                            target_version=target_ver, code=ERR_TARGET_NOT_LOCKED,
                            message="目标版本未 LOCK 且 revalidate_lock=False")
                raise RollbackError(
                    f"回滚失败：目标版本 {target_ver} 当前不是 LOCKED。"
                    "回滚只能恢复已有 LOCK 的版本；如确认要以本文件的哈希重新授权，"
                    "请显式传 revalidate_lock=True（会在审计日志留下 lock_source/review_cycle_id）。",
                    code=ERR_TARGET_NOT_LOCKED,
                    asset_id=asset_id,
                    version=target_ver,
                )
            cycle = cycle or f"rollback:{operator or 'unknown'}:{_compact_ts()}"
            store.set_lock(
                asset_id,
                target_ver,
                lock_source=lock_source,
                content_hash=actual,
                review_cycle_id=cycle,
            )
            relocked = True
        else:
            resolved = store.resolve_lock(asset_id, target_ver)
            if resolved is None:
                _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                            target_version=target_ver, code=ERR_LOCK_RECORD_INCONSISTENT,
                            message="目标版本 LOCK 记录不自洽（resolve_lock=None）")
                raise RollbackError(
                    f"回滚失败：目标版本 {target_ver} 标记为 LOCKED，但 LOCK 记录不自洽"
                    "（缺 lock_source/review_cycle_id 或指纹无法解析）——拒绝把不自洽的 LOCK 当作授权。",
                    code=ERR_LOCK_RECORD_INCONSISTENT,
                    asset_id=asset_id,
                    version=target_ver,
                )
            cycle = str(resolved.get("review_cycle_id") or cycle or "")

        # ---- 4) 撤销其它版本的 LOCK（保留版本/文件，仅改 LOCK 状态并留痕） ----
        cleared: list[str] = []
        for version in locks_before:
            if version == target_ver:
                continue
            store.clear_lock(
                asset_id,
                version,
                reason=f"rollback->{target_ver} operator={operator or '-'} reason={reason or '-'}",
            )
            cleared.append(version)

        # ---- 5) 后置校验：目标必须是当前唯一/最新 LOCK ----
        locks_after = sorted(store.locked_versions(asset_id), key=version_sort_key)
        latest = store.latest_locked_version(asset_id)
        if latest != target_ver:
            err = RollbackError(
                f"回滚后置校验失败：latest_locked_version={latest!r}，期望 {target_ver!r}"
                "（可能有并发写者，请重试或检查审核流水线）",
                code=ERR_POSTCONDITION,
                locks_after=locks_after,
            )
            _reject_log(paths, project_id=project_id, asset=asset, operator=operator, reason=reason,
                        target_version=target_ver, code=ERR_POSTCONDITION, message=str(err),
                        locks_after=locks_after)
            raise err

        # ---- 6) 旧结论失效审计 + 引用段反查 ----
        try:
            refs = store.referencing_segments(asset_id)
        except StoreError:
            refs = []
        affected = sorted(
            {
                str(r.get("segment_id"))
                for r in refs
                if isinstance(r, dict) and r.get("segment_id")
            }
        )
        audit = {
            "stale": True,
            "old_conclusions_invalidated": True,
            "requires_regeneration": True,
            "requires_reference_remount": True,
            "reason": (
                "回滚恢复的是历史版本的 LOCK 记录，不是新的审核结论；"
                "引用该素材的段在旧版本 LOCK 下产出的成片结论已失效，"
                "必须重新挂参考图并重跑视频审核（R_seg/R_full），必要时重新送审该版本。"
            ),
            "target_was_locked_before": target_was_locked,
            "relocked": relocked,
            "historical_versions_deleted": 0,
            "versions_preserved": len(asset.get("versions") or []),
            "affected_segments": affected,
            "affected_segment_count": len(affected),
            "next_actions": [
                "重新生成所有引用该素材的段（旧 LOCK 下的段/成片结论已失效）",
                "重挂素材库参考图后重跑视频审核（R_seg 段级 + R_full 整片一次）",
                "若目标版本的审核结论早于当前标准，建议重新送审该版本后再用于生成",
            ],
        }
        record = {
            "at": _now_iso(),
            "status": "ok",
            "project_id": project_id,
            "asset_id": asset_id,
            "asset_name": asset_name,
            "operator": operator,
            "reason": reason,
            "from_version": from_version,
            "to_version": target_ver,
            "locks_before": locks_before,
            "locks_after": locks_after,
            "cleared_locks": cleared,
            "relocked": relocked,
            "lock_source": lock_source if relocked else (target.get("lock_source") or None),
            "review_cycle_id": cycle or None,
            "content_hash": actual,
            "hash_ok": hash_ok,
            "require_hash_ok": bool(require_hash_ok),
            "revalidate_lock": bool(revalidate_lock),
            "audit": audit,
            "warnings": warnings,
        }
        log_path = _append_log(paths, record)
        return {
            "ok": True,
            "code": "OK",
            "project_id": project_id,
            "asset_id": asset_id,
            "asset_name": asset_name,
            "target_version": target_ver,
            "from_version": from_version,
            "changed": bool(cleared or relocked or (from_version != target_ver)),
            "relocked": relocked,
            "cleared_locks": cleared,
            "locks_before": locks_before,
            "locks_after": locks_after,
            "content_hash": actual,
            "file_path": str(abs_path),
            "hash_ok": hash_ok,
            "review_cycle_id": cycle or None,
            "audit": audit,
            "log_path": str(log_path),
            "warnings": warnings,
        }


def _reject_log(
    paths: ProjectPaths,
    *,
    project_id: str,
    asset: dict[str, Any],
    operator: str,
    reason: str,
    target_version: str,
    code: str,
    message: str,
    **extra: Any,
) -> None:
    """拒绝尝试也写审计日志（记录「谁在何时试图回滚到什么版本、为什么被拒」）。"""
    record = {
        "at": _now_iso(),
        "status": "rejected",
        "project_id": project_id,
        "asset_id": asset.get("asset_id"),
        "asset_name": asset.get("name"),
        "operator": operator,
        "reason": reason,
        "target_version": target_version,
        "error_code": code,
        "error": message,
        **extra,
    }
    try:
        _append_log(paths, record)
    except Exception as exc:  # noqa: BLE001 - 日志失败不能掩盖原始业务错误
        import logging

        logging.getLogger(__name__).warning("写入 rollback_log 失败（拒绝记录丢失）：%s", exc)


# ---------------------------------------------------------------------------
# 离线 CLI（供人工验证/脚本化；不做网络/GPU）
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """离线 CLI：``python -m bettercomfyui.store.rollback --project-id X [--list|--target-version v1]``。

    退出码：0=成功；2=回滚被拒（StoreError/业务错误）；3=参数/未知错误。
    """
    parser = argparse.ArgumentParser(
        prog="python -m bettercomfyui.store.rollback",
        description="素材版本回滚（只读列举 / 哈希门禁回滚 / 审计日志）",
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--asset", default=None, help="asset_id 或素材显示名；--list 时可省略")
    parser.add_argument("--target-version", default=None, help="回滚目标版本，如 v2")
    parser.add_argument("--operator", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--root", default=None, help="projects/ 根目录（默认按配置解析）")
    parser.add_argument("--lock-source", default=LockSource.HUMAN_PASS.value)
    parser.add_argument("--review-cycle-id", default=None)
    parser.add_argument("--no-require-hash-ok", action="store_true", help="危险：允许哈希不一致回滚")
    parser.add_argument("--no-revalidate-lock", action="store_true", help="目标未 LOCK 时拒绝（默认重新 LOCK）")
    parser.add_argument("--list", action="store_true", help="只列举版本")
    args = parser.parse_args(argv)

    try:
        if args.list or not args.target_version:
            rows = list_asset_versions(args.project_id, args.asset, root=args.root)
            print(json.dumps({"ok": True, "project_id": args.project_id, "versions": rows},
                             ensure_ascii=False, indent=1))
            return 0
        result = rollback_asset(
            args.project_id,
            args.asset,
            args.target_version,
            operator=args.operator,
            reason=args.reason,
            require_hash_ok=not args.no_require_hash_ok,
            revalidate_lock=not args.no_revalidate_lock,
            lock_source=args.lock_source,
            review_cycle_id=args.review_cycle_id,
            root=args.root,
        )
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    except StoreError as exc:
        print(json.dumps({"ok": False, "code": getattr(exc, "code", "STORE_ERROR"),
                          "error": f"{type(exc).__name__}: {exc}",
                          "context": getattr(exc, "context", {})},
                         ensure_ascii=False, indent=1), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 退错要可见
        print(json.dumps({"ok": False, "code": "UNEXPECTED",
                          "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False, indent=1), file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
