"""审核状态机（唯一通路：`REVIEW_TRANSITIONS`）。

方案第七部分·六：**每个「状态 + 事件」组合只允许一个目标状态**；未列出的组合一律拒绝。
本模块**绝不静默忽略**非法组合——静默忽略会把非法状态当成正常状态继续跑。

两条规格事实（已在测试中断言）：
1. `REVIEW_TRANSITIONS` 里不存在 `target == AWAITING_REVIEW` 的边（方案表格如此）。
   因此**人工审核周期的合法起点只能是「初始状态 = AWAITING_REVIEW」**，
   初始状态不属于转移；人工模式下确定性校验失败走
   `RECEIVED → start_review → VALIDATING → validation_failed → FAILED` 这条真实边。
2. 终态（`ReviewState.is_terminal`：ALLOWED / FALLBACK / FAILED / ABORTED）没有出边，
   任何事件都会抛 `IllegalTransition`。

「任意可运行状态 + 用户主动取消 → ABORTED」按 `REVIEW_ABORTABLE_STATES` 实现；
`BLOCKED` 在该集合内（它不是终态），终态不在集合内（终态不可再取消）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable

from ..models.entities import now_iso
from ..models.enums import REVIEW_ABORTABLE_STATES, REVIEW_TRANSITIONS, ReviewState
from .errors import IllegalTransition

_LOG = logging.getLogger(__name__)

#: 用户主动取消（方案转移表末行）
USER_CANCEL_EVENT = "user_cancel"
#: 兼容写法；统一记录为 USER_CANCEL_EVENT
USER_CANCEL_ALIASES = frozenset({"user_cancel", "cancel", "user_cancelled"})


def events_for_state(state: ReviewState) -> tuple[str, ...]:
    """返回某状态下 REVIEW_TRANSITIONS 允许的事件名（含取消）。"""
    names = [event for (src, event) in REVIEW_TRANSITIONS if src is state]
    if state in REVIEW_ABORTABLE_STATES:
        names.append(USER_CANCEL_EVENT)
    return tuple(sorted(names))


class ReviewCycle:
    """一个对象版本对应一个正式审核周期。状态转移必须走 `REVIEW_TRANSITIONS`。"""

    def __init__(
        self,
        review_cycle_id: str,
        *,
        state: ReviewState | str = ReviewState.RECEIVED,
        object_type: str | None = None,
        object_id: str | None = None,
        version: str | None = None,
        mode: Any = None,
        review_record_id: str | None = None,
        clock: Callable[[], str] | None = None,
        event_log: Any = None,
        context: dict | None = None,
        history: Iterable[dict] | None = None,
    ) -> None:
        if not isinstance(review_cycle_id, str) or not review_cycle_id.strip():
            raise ValueError("review_cycle_id 必须是非空字符串")
        self.review_cycle_id = review_cycle_id.strip()
        self._state = ReviewState.parse(state)
        if self._state is None:
            raise ValueError("state 不能为 None")
        self.object_type = object_type
        self.object_id = object_id
        self.version = version
        self.mode = mode
        self.review_record_id = review_record_id
        self.context: dict = dict(context or {})
        self._history: list[dict] = [dict(h) for h in (history or [])]
        self._clock = clock or now_iso
        self._events = event_log
        self._lock = threading.RLock()

    # -- 只读属性 -----------------------------------------------------------
    @property
    def state(self) -> ReviewState:
        with self._lock:
            return self._state

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def history(self) -> list[dict]:
        with self._lock:
            return [dict(h) for h in self._history]

    @property
    def last_event(self) -> str | None:
        with self._lock:
            return self._history[-1]["event"] if self._history else None

    def allowed_events(self) -> tuple[str, ...]:
        return events_for_state(self.state)

    def can_fire(self, event: str) -> bool:
        if not isinstance(event, str):
            return False
        event = event.strip()
        state = self.state
        if (state, event) in REVIEW_TRANSITIONS:
            return True
        return event in USER_CANCEL_ALIASES and state in REVIEW_ABORTABLE_STATES

    # -- 转移 ---------------------------------------------------------------
    def fire(self, event: str, **payload: Any) -> ReviewState:
        """执行状态转移；非法「状态 + 事件」组合抛 `IllegalTransition`。"""
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event 必须是非空字符串")
        event = event.strip()
        with self._lock:
            current = self._state
            target = REVIEW_TRANSITIONS.get((current, event))
            canonical = event
            if target is None and event in USER_CANCEL_ALIASES:
                if current in REVIEW_ABORTABLE_STATES:
                    target = ReviewState.ABORTED
                    canonical = USER_CANCEL_EVENT
                else:
                    raise IllegalTransition(
                        current,
                        event,
                        allowed=self.allowed_events(),
                        reason=(
                            f"{current.value} 不在可取消状态集合内（REVIEW_ABORTABLE_STATES）；"
                            "终态不允许再取消或改写"
                        ),
                    )
            if target is None:
                raise IllegalTransition(
                    current, event, allowed=self.allowed_events(), reason="REVIEW_TRANSITIONS 未定义该组合"
                )
            self._state = target
            entry = {
                "ts": self._clock(),
                "from": current.value,
                "event": canonical,
                "to": target.value,
                "payload": {k: v for k, v in payload.items()},
            }
            self._history.append(entry)
        self._emit_state_change(entry)
        return target

    def _emit_state_change(self, entry: dict) -> None:
        if self._events is None:
            return
        try:
            self._events.emit(
                "review_state_changed",
                object_id=self.object_id,
                version=self.version,
                mode=self.mode,
                detail={
                    "review_cycle_id": self.review_cycle_id,
                    "from": entry["from"],
                    "to": entry["to"],
                    "event": entry["event"],
                    "payload": entry["payload"],
                },
            )
        except Exception as exc:  # pragma: no cover - 事件日志绝不阻断主流程
            _LOG.warning("状态变化事件写入失败（不阻断主流程）：%s", exc)

    # -- 序列化 -------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "review_cycle_id": self.review_cycle_id,
                "state": self._state.value,
                "object_type": self.object_type,
                "object_id": self.object_id,
                "version": self.version,
                "mode": self.mode.value if hasattr(self.mode, "value") else self.mode,
                "review_record_id": self.review_record_id,
                "history": [dict(h) for h in self._history],
                "context": dict(self.context),
            }

    @classmethod
    def restore(
        cls,
        data: dict,
        *,
        clock: Callable[[], str] | None = None,
        event_log: Any = None,
    ) -> "ReviewCycle":
        if not isinstance(data, dict):
            raise ValueError("ReviewCycle.restore 需要 dict 快照")
        return cls(
            data.get("review_cycle_id"),
            state=data.get("state", ReviewState.RECEIVED),
            object_type=data.get("object_type"),
            object_id=data.get("object_id"),
            version=data.get("version"),
            mode=data.get("mode"),
            review_record_id=data.get("review_record_id"),
            clock=clock,
            event_log=event_log,
            context=data.get("context") or {},
            history=data.get("history") or [],
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<ReviewCycle {self.review_cycle_id} state={self.state.value}>"


__all__ = ["ReviewCycle", "USER_CANCEL_EVENT", "USER_CANCEL_ALIASES", "events_for_state"]
