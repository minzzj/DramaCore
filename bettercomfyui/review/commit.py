"""唯一最终提交点（方案第七部分·七「最终结果提交」+ 任务书第 6 节）。

`commit_final(cycle, *, store)` 用 `review_cycle_id + finalization_token` 做**条件更新**：
- 结论状态（ALLOWED / FALLBACK / BLOCKED / ABORTED）**恰好一个**提交者胜出；
  其余返回 `{"committed": False, "reason": "already_finalized"}`，且**不产生任何副作用**
  （不写 LOCK、不改版本状态、不触发下游）。
- **只有首次提交者**可以写 LOCK / 更新素材库 / 触发下游。
- `FAILED`（技术失败）**不是最终结论**：方案允许 `technical_recovery_resubmit` 恢复，
  因此 `commit_final` 拒绝提交它（`reason="state_not_conclusive:FAILED"`），也不占用 token。
- 迟到结果只能通过 `archive_late_result()` 归档为 ABORTED，**绝不改变当前版本状态**。

条件更新的载体（`FinalizationLedger`）：
- 显式给了 `ledger` / `ledger_path` → 落盘 CAS（跨进程安全），`durable=True`；
- 没给 → 进程内 CAS（同一进程内 32 线程仍然恰好一个胜出），`durable=False`（如实上报）。

第二道防线（不替代条件更新）：store 的 `set_lock()` 对相同
`(lock_source, content_hash, review_cycle_id)` 幂等、对不同结论拒绝；本模块在 claim 之前
额外只读检查目标版本是否已 LOCK，避免服务重启后重复写 LOCK。
"""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from ..models.entities import now_iso
from ..models.enums import AssetStatus, LockSource, ReviewState
from . import _bridge as bridge
from .errors import FinalizationError

_LOG = logging.getLogger(__name__)

#: 需要占用 finalization_token 的结论状态（恰好一个提交者胜出）
CONSENSIVE_STATES = frozenset(
    {ReviewState.ALLOWED, ReviewState.FALLBACK, ReviewState.BLOCKED, ReviewState.ABORTED}
)
#: 兼容别名（旧名）
CONCLUSIVE_STATES = CONSENSIVE_STATES
#: 会产生 LOCK 的状态
LOCK_STATES = frozenset({ReviewState.ALLOWED, ReviewState.FALLBACK})
#: 可恢复、因此绝不能占位的状态
NON_CONCLUSIVE_STATES = frozenset({ReviewState.FAILED})

_MEMORY_LOCK = threading.RLock()
_MEMORY_STATE: dict = {"cycles": {}, "orphan_archive": []}
_LEDGERS: dict[str, "FinalizationLedger"] = {}
_LEDGERS_LOCK = threading.RLock()


def reset_memory_ledger() -> None:
    """测试用：清空进程内 ledger（不影响落盘 ledger）。"""
    with _MEMORY_LOCK:
        _MEMORY_STATE["cycles"] = {}
        _MEMORY_STATE["orphan_archive"] = []


class FinalizationLedger:
    """`review_cycle_id + finalization_token` 的条件更新载体。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        clock: Callable[[], str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or now_iso
        self._timeout_s = float(timeout_s)
        self._lock = threading.RLock()

    @property
    def durable(self) -> bool:
        return self.path is not None

    # -- 读写 ---------------------------------------------------------------
    def _read(self) -> dict:
        if self.path is None:
            with _MEMORY_LOCK:
                return copy.deepcopy(_MEMORY_STATE)
        return bridge.read_json_strict(self.path)

    def _mutate(self, mutator: Callable[[dict], dict]) -> dict:
        if self.path is None:
            with _MEMORY_LOCK:
                updated = mutator(_MEMORY_STATE)
                if not isinstance(updated, dict):  # pragma: no cover - 防御
                    raise FinalizationError("FinalizationLedger mutator 必须返回 dict")
                return copy.deepcopy(updated)
        with self._lock:
            return bridge.locked_update_json(self.path, mutator, timeout_s=self._timeout_s)

    # -- 条件更新 -----------------------------------------------------------
    def claim(
        self,
        review_cycle_id: str,
        token: str,
        *,
        detail: dict | None = None,
    ) -> tuple[bool, dict | None]:
        """尝试占用该审核周期的最终提交权。返回 (是否胜出, 既有记录 or None)。"""
        if not review_cycle_id or not token:
            raise FinalizationError("claim 需要 review_cycle_id 与 finalization_token")
        outcome: dict[str, Any] = {}

        def mutator(data: dict) -> dict:
            cycles = data.setdefault("cycles", {})
            existing = cycles.get(review_cycle_id)
            if existing is not None:
                outcome["existing"] = existing
                return data
            entry = {
                "token": token,
                "committed_at": self._clock(),
                "state": None,
                "detail": dict(detail or {}),
                "late_results": [],
            }
            entry.update(dict(detail or {}))
            cycles[review_cycle_id] = entry
            outcome["entry"] = entry
            return data

        self._mutate(mutator)
        if "entry" in outcome:
            return True, outcome["entry"]
        return False, outcome.get("existing")

    def get(self, review_cycle_id: str) -> dict | None:
        data = self._read()
        return (data.get("cycles") or {}).get(review_cycle_id)

    def release(self, review_cycle_id: str, token: str, *, reason: str = "") -> bool:
        """释放提交权（仅用于「兜底接受补记」与「LOCK 写入失败」两种已记录场景）。"""
        outcome: dict[str, Any] = {}

        def mutator(data: dict) -> dict:
            cycles = data.get("cycles") or {}
            existing = cycles.get(review_cycle_id)
            if existing is None:
                outcome["released"] = False
                return data
            if token and existing.get("token") != token:
                outcome["released"] = False
                outcome["token_mismatch"] = True
                return data
            cycles.pop(review_cycle_id, None)
            outcome["released"] = True
            outcome["previous"] = existing
            return data

        self._mutate(mutator)
        if outcome.get("released"):
            _LOG.warning("释放最终提交权（已记录原因）：cycle=%s reason=%s", review_cycle_id, reason)
        return bool(outcome.get("released"))

    def record_late_result(self, review_cycle_id: str, entry: dict) -> dict:
        """归档一个迟到结果（**不创建提交权**，不会阻塞该周期未来的合法提交）。"""
        outcome: dict[str, Any] = {}

        def mutator(data: dict) -> dict:
            cycles = data.setdefault("cycles", {})
            record = dict(entry)
            if review_cycle_id in cycles:
                cycles[review_cycle_id].setdefault("late_results", []).append(record)
                outcome["target"] = "cycle"
            else:
                data.setdefault("orphan_archive", []).append(
                    {"review_cycle_id": review_cycle_id, **record}
                )
                outcome["target"] = "orphan_archive"
            return data

        self._mutate(mutator)
        return {"archived": True, "target": outcome.get("target"), "durable": self.durable}

    def snapshot(self) -> dict:
        return self._read()


def get_ledger(path: Path | str | None = None) -> FinalizationLedger:
    """按路径缓存 ledger（内存模式共享同一实例，保证同进程条件更新语义一致）。"""
    key = str(Path(path).resolve()) if path is not None else ":memory:"
    with _LEDGERS_LOCK:
        ledger = _LEDGERS.get(key)
        if ledger is None:
            ledger = FinalizationLedger(None if path is None else Path(path))
            _LEDGERS[key] = ledger
        return ledger


def ledger_for_cycle(cycle: dict) -> FinalizationLedger:
    ledger = cycle.get("ledger")
    if isinstance(ledger, FinalizationLedger):
        return ledger
    path = cycle.get("ledger_path")
    return get_ledger(path)


# ---------------------------------------------------------------------------
# 唯一提交点
# ---------------------------------------------------------------------------


def _emit(cycle: dict, event: str, *, severity: str, detail: dict) -> None:
    log = cycle.get("event_log")
    if log is None:
        return
    try:
        log.emit(
            event,
            stage=cycle.get("stage"),
            object_id=cycle.get("target_id"),
            version=cycle.get("version"),
            mode=cycle.get("mode"),
            severity=severity,
            detail=detail,
        )
    except Exception as exc:  # pragma: no cover - 事件日志绝不阻断主流程
        _LOG.warning("提交事件写入失败（不阻断主流程）：%s", exc)


def _safe_get_version(store: Any, asset_id: str, version: str) -> dict | None:
    if store is None:
        return None
    try:
        record = store.get_version(asset_id, version)
    except Exception as exc:
        _LOG.warning("读取版本元数据失败（按未知处理）：asset=%s %s err=%s", asset_id, version, exc)
        return None
    return record if isinstance(record, dict) else None


def _lock_version_of(cycle: dict) -> str:
    return str(cycle.get("lock_version") or cycle.get("version") or "")


def commit_final(cycle: dict, *, store: Any) -> dict:
    """唯一最终结果提交点。返回 committed / reason / lock_id / claim 等明细。"""
    if not isinstance(cycle, dict):
        raise FinalizationError(f"commit_final 需要 dict（ReviewRecord.to_dict()），实际 {type(cycle).__name__}")
    review_cycle_id = cycle.get("review_cycle_id")
    if not review_cycle_id or not str(review_cycle_id).strip():
        raise FinalizationError("commit_final 需要 review_cycle_id")
    review_cycle_id = str(review_cycle_id)

    try:
        state = ReviewState.parse(cycle.get("review_state"))
    except ValueError as exc:
        raise FinalizationError(f"commit_final 的 review_state 非法：{exc}") from exc
    if state is None:
        raise FinalizationError("commit_final 需要 review_state")

    base = {
        "review_cycle_id": review_cycle_id,
        "state": state,
        "version": _lock_version_of(cycle),
        "side_effects": [],
    }

    if state not in CONSENSIVE_STATES:
        reason = (
            "state_not_conclusive:FAILED（技术失败可 technical_recovery_resubmit 恢复，"
            "不得占用唯一提交权）"
            if state in NON_CONCLUSIVE_STATES
            else f"state_not_conclusive:{state.value}"
        )
        return {**base, "committed": False, "reason": reason, "claim": "not_attempted",
                "lock_id": None, "finalized_at": None}

    token = cycle.get("finalization_token")
    if not token:
        token = bridge.new_finalization_token()
        cycle.setdefault("finalization_token", token)
        token = cycle["finalization_token"]

    target_kind = str(cycle.get("target_kind") or "asset")
    asset_id = str(cycle.get("target_id") or "")
    lock_version = _lock_version_of(cycle)
    lock_source = None
    if cycle.get("lock_source") is not None:
        try:
            lock_source = LockSource.parse(cycle.get("lock_source"))
        except ValueError as exc:
            raise FinalizationError(f"lock_source 非法：{exc}") from exc

    needs_lock = state in LOCK_STATES
    manages_asset = target_kind == "asset"

    if needs_lock and not lock_version:
        raise FinalizationError("ALLOWED/FALLBACK 提交必须携带 version（LOCK 绑定 asset_id+version+content_hash）")
    if needs_lock and lock_source is None:
        return {**base, "committed": False, "reason": "missing_lock_source", "claim": "not_attempted",
                "lock_id": None, "finalized_at": None}
    if needs_lock and manages_asset and store is None:
        return {**base, "committed": False, "reason": "store_not_configured", "claim": "not_attempted",
                "lock_id": None, "finalized_at": None}

    # --- 第二道防线：只读检查目标版本是否已经 LOCK（防重启后重复写 LOCK） ---
    # 注意：命中这里时 committed 一律为 False，保证「恰好一个提交成功」的语义
    # （同结论重复提交 = already_finalized，不算一次新的提交）。
    if needs_lock and manages_asset:
        existing = _safe_get_version(store, asset_id, lock_version)
        if existing and str(existing.get("lock_status", "")).upper() == "LOCKED":
            same = (
                str(existing.get("lock_source") or "") == str(lock_source)
                and str(existing.get("review_cycle_id") or "") == review_cycle_id
            )
            lock_id_existing = existing.get("lock_id") or bridge.lock_id(asset_id, lock_version)
            if same:
                return {**base, "committed": False, "reason": "already_finalized",
                        "claim": "already_locked_idempotent",
                        "lock_id": lock_id_existing, "lock_source": lock_source,
                        "wrote_lock": False,
                        "finalized_at": existing.get("locked_at") or now_iso(),
                        "side_effects": [],
                        "detail": "该版本已由同一 review_cycle_id 写过相同结论的 LOCK（store 幂等），"
                                  "本次不重复提交、不产生副作用"}
            return {**base, "committed": False, "reason": "already_locked_in_store",
                    "claim": "not_attempted", "lock_id": lock_id_existing,
                    "lock_source": existing.get("lock_source"), "wrote_lock": False,
                    "finalized_at": None, "side_effects": [],
                    "detail": "该版本已有不同结论的 LOCK；要按新审核周期重新加锁必须先 clear_lock 并留痕"}

    # --- 条件更新：恰好一个提交者胜出 ---------------------------------------
    ledger = ledger_for_cycle(cycle)
    won, existing_claim = ledger.claim(
        review_cycle_id,
        token,
        detail={"state": state.value, "version": lock_version, "target_kind": target_kind,
                "target_id": asset_id, "mode": str(cycle.get("mode") or "")},
    )
    if not won:
        return {
            **base,
            "committed": False,
            "reason": "already_finalized",
            "claim": "already_finalized",
            "winner_token": (existing_claim or {}).get("token"),
            "winner_state": (existing_claim or {}).get("state"),
            "same_token": (existing_claim or {}).get("token") == token,
            "lock_id": (existing_claim or {}).get("lock_id"),
            "finalized_at": (existing_claim or {}).get("committed_at"),
            "durable": ledger.durable,
            "side_effects": [],
        }

    finalized_at = now_iso()
    writes: dict[str, Any] = {}
    warnings: list[str] = []
    lock_id: str | None = None
    side_effects: list[str] = []

    try:
        if state is ReviewState.ABORTED:
            # 迟到结果/取消：只归档结论，绝不改变当前版本状态
            side_effects.append("archive_only_no_version_write")
        elif not manages_asset:
            # 段级/整片级 LOCK 由对应模块负责；这里只做唯一提交声明
            side_effects.append("claim_only_non_asset_target")
        elif state is ReviewState.BLOCKED:
            if store is not None:
                store.set_version_status(
                    asset_id, lock_version, AssetStatus.BLOCKED,
                    review_cycle_id=review_cycle_id, review_state=state.value,
                    mode=str(cycle.get("mode") or ""), reason=str(cycle.get("reason") or ""),
                )
                writes["set_version_status"] = "BLOCKED"
                side_effects.append("set_version_status:BLOCKED")
        else:  # ALLOWED / FALLBACK
            content_hash = cycle.get("content_hash")
            if not content_hash:
                record = _safe_get_version(store, asset_id, lock_version)
                content_hash = (record or {}).get("content_hash")
            if not content_hash:
                ledger.release(review_cycle_id, token, reason="missing_content_hash")
                return {**base, "committed": False, "reason": "missing_content_hash",
                        "claim": "released", "lock_id": None, "finalized_at": None,
                        "detail": "LOCK 必须绑定真实 content_hash（R3）；拒绝用空指纹加锁"}
            store.set_lock(
                asset_id, lock_version,
                lock_source=lock_source, content_hash=content_hash,
                review_cycle_id=review_cycle_id,
            )
            lock_id = bridge.lock_id(asset_id, lock_version)
            writes["set_lock"] = lock_id
            side_effects.append(f"set_lock:{lock_id}")
            status = AssetStatus.ALLOWED if state is ReviewState.ALLOWED else AssetStatus.FALLBACK
            # 注意：不得回传 content_hash/version/file_path（store 禁止改写这三项）
            store.set_version_status(
                asset_id, lock_version, status,
                review_cycle_id=review_cycle_id, review_state=state.value,
                lock_source=str(lock_source),
                score=cycle.get("score"), mode=str(cycle.get("mode") or ""),
                reason=str(cycle.get("reason") or ""),
            )
            writes["set_version_status"] = status.value
            side_effects.append(f"set_version_status:{status.value}")
    except Exception as exc:
        if "set_lock" in writes:
            # LOCK 已写入：绝不能释放提交权（否则重试会写第二次 LOCK）
            warnings.append(f"LOCK 已写入但后续步骤失败（保留提交权，不重复写 LOCK）：{exc}")
            _LOG.error("提交后置步骤失败：cycle=%s err=%s", review_cycle_id, exc)
        else:
            ledger.release(review_cycle_id, token, reason=f"store_write_failed:{exc}")
            return {**base, "committed": False, "reason": "store_write_failed",
                    "claim": "released", "error": f"{type(exc).__name__}: {exc}",
                    "lock_id": None, "finalized_at": None, "durable": ledger.durable,
                    "side_effects": side_effects}

    # --- 只有首次提交者可以触发下游 ----------------------------------------
    downstream = cycle.get("downstream")
    downstream_triggered = False
    downstream_error: str | None = None
    if callable(downstream):
        downstream_triggered = True
        try:
            downstream(dict(cycle))
        except Exception as exc:
            downstream_error = f"{type(exc).__name__}: {exc}"
            warnings.append(f"下游触发失败（不回滚 LOCK）：{downstream_error}")
            _LOG.error("下游触发失败：cycle=%s err=%s", review_cycle_id, exc)

    _emit(cycle, "finalization_committed", severity="INFO", detail={
        "review_cycle_id": review_cycle_id, "state": state.value, "lock_id": lock_id,
        "version": lock_version, "lock_source": str(lock_source) if lock_source else None,
        "finalization_token": token, "store_writes": writes, "durable": ledger.durable,
    })

    return {
        **base,
        "committed": True,
        "reason": None,
        "claim": "won",
        "lock_id": lock_id,
        "lock_source": lock_source,
        "wrote_lock": bool(lock_id),
        "finalization_token": token,
        "finalized_at": finalized_at,
        "store_writes": writes,
        "warnings": warnings,
        "downstream_triggered": downstream_triggered,
        "downstream_error": downstream_error,
        "durable": ledger.durable,
        "side_effects": side_effects,
    }


def archive_late_result(
    cycle: dict,
    *,
    store: Any = None,
    ledger: FinalizationLedger | None = None,
    reason: str = "",
    attempt: dict | None = None,
) -> dict:
    """把迟到结果归档为 ABORTED。**绝不改变当前版本状态**（store 一次都不碰）。"""
    if not isinstance(cycle, dict):
        raise FinalizationError("archive_late_result 需要 dict")
    review_cycle_id = cycle.get("review_cycle_id")
    if not review_cycle_id:
        raise FinalizationError("archive_late_result 需要 review_cycle_id")
    review_cycle_id = str(review_cycle_id)
    ledger = ledger or ledger_for_cycle(cycle)
    entry = {
        "archived_at": now_iso(),
        "archive_state": ReviewState.ABORTED.value,
        "reason": reason or "迟到结果：周期已产生最终结论",
        "finalization_token": cycle.get("finalization_token"),
        "attempt": attempt,
        "wrote_version_state": False,
    }
    outcome = ledger.record_late_result(review_cycle_id, entry)
    _emit(cycle, "late_result_archived", severity="WARN", detail={
        "review_cycle_id": review_cycle_id,
        "archive_state": ReviewState.ABORTED.value,
        "reason": entry["reason"],
        "wrote_version_state": False,
        "archive_target": outcome.get("target"),
    })
    return {
        "archived": True,
        "archive_state": ReviewState.ABORTED.value,
        "reason": entry["reason"],
        "wrote_version_state": False,
        "store_calls": [],
        "target": outcome.get("target"),
        "durable": ledger.durable,
    }


__all__ = [
    "FinalizationLedger",
    "commit_final",
    "archive_late_result",
    "get_ledger",
    "ledger_for_cycle",
    "reset_memory_ledger",
    "CONSENSIVE_STATES",
    "CONCLUSIVE_STATES",
    "LOCK_STATES",
    "NON_CONCLUSIVE_STATES",
]
