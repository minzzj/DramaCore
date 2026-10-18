"""CNT1/CNT2/CNT3 计数器（R4 落地）。

口径（方案第七部分·一·4 / 二·4·5 + 参数表三/六）：
- 计数器**绑定逻辑任务 ID**（素材条目 ID / 逻辑段 ID），不绑定版本号：
  版本号递增绝不清零任何计数器。
- CNT1 = 素材模型审核候选轮次；每次**实际执行**模型审核前计数一次。
  人工审核次数不进 CNT1；人工超时切换到模型模式时 CNT1 从 0 开始（唯一合法重置）。
- CNT2 / CNT3 由 director/ 使用，review 层不重置、不累加。
- 技术失败（ErrorClass != QUALITY_REVIEW）绝不消耗任何 CNT：
  `rollback_increment` 只用于「执行前已计数、随后技术失败」的回滚。

`reset()` 只允许 `CounterKind.CNT1`；对 CNT2/CNT3 调用 reset 一律抛 IllegalCounterReset
（契约 §6 明确「其余任何地方调用 reset 都是缺陷」）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..models.enums import CounterKind
from ._bridge import cnt_id as _cnt_id
from .errors import CounterError, IllegalCounterReset

_LOG = logging.getLogger(__name__)

#: 独立技术重试计数的键前缀（与 CNT1/2/3 完全分离，R4）
TECH_RETRY_SCOPE = "TECH"


class CounterStore:
    """线程安全的计数器存储。绑定逻辑任务 ID，不绑定版本号。"""

    def __init__(self, *, event_log=None, clock=None) -> None:
        self._counts: dict[str, int] = {}
        self._tech: dict[str, int] = {}
        self._lock = threading.RLock()
        self._events = event_log
        self._clock = clock

    # -- 工具 ---------------------------------------------------------------
    @staticmethod
    def _kind(kind: Any) -> CounterKind:
        try:
            parsed = CounterKind.parse(kind)
        except ValueError as exc:
            raise CounterError(str(exc)) from exc
        if parsed is None:
            raise CounterError("CounterKind 不能为 None")
        return parsed

    @staticmethod
    def _obj_id(obj_id: Any) -> str:
        if obj_id is None or not str(obj_id).strip():
            raise CounterError("obj_id 必须是非空字符串（计数器绑定逻辑任务 ID，不绑定版本号）")
        return str(obj_id)

    def key(self, obj_id: str, kind: Any) -> str:
        """返回计数器 ID：`CNT_{对象ID}_{计数类型}`。"""
        return _cnt_id(self._obj_id(obj_id), self._kind(kind))

    def _tech_key(self, obj_id: str) -> str:
        """独立技术重试计数的 key。

        注意：`TECH` **不是** `CounterKind`（枚举只有 CNT1/CNT2/CNT3），因此这里不能走
        `store.ids.cnt_id()`——store 会按 CounterKind 强校验并抛 StoreError（director-exec 实测报出）。
        技术重试计数与 CNT 完全分离，key 直接拼装，语义由常量 `TECH_RETRY_SCOPE` 固定。
        """
        return f"CNT_{self._obj_id(obj_id)}_{TECH_RETRY_SCOPE}"

    # -- CNT ----------------------------------------------------------------
    def increment(self, obj_id: str, kind: Any) -> int:
        counter_kind = self._kind(kind)
        key = self.key(obj_id, counter_kind)
        with self._lock:
            value = self._counts.get(key, 0) + 1
            self._counts[key] = value
        self._emit(
            "counter_incremented",
            key,
            {"kind": counter_kind.value, "obj_id": self._obj_id(obj_id), "value": value},
        )
        return value

    def get(self, obj_id: str, kind: Any) -> int:
        key = self.key(obj_id, kind)
        with self._lock:
            return int(self._counts.get(key, 0))

    def reset(self, obj_id: str, kind: Any) -> None:
        """唯一合法重置场景：人工超时切换到模型模式时 CNT1 从 0 开始。"""
        counter_kind = self._kind(kind)
        if counter_kind is not CounterKind.CNT1:
            raise IllegalCounterReset(
                f"非法重置 {counter_kind.value}：只有「人工超时切换到模型模式时 CNT1 归零」"
                "是合法重置场景（方案第七部分·一·3；CONTRACTS §6）。"
                "CNT2/CNT3 与版本号递增都不清零。"
            )
        key = self.key(obj_id, counter_kind)
        with self._lock:
            self._counts[key] = 0
        self._emit(
            "counter_reset",
            key,
            {"kind": counter_kind.value, "obj_id": self._obj_id(obj_id), "value": 0},
        )

    def rollback_increment(self, obj_id: str, kind: Any) -> int:
        """回滚一次「执行前计数」。

        只允许用于技术失败（`ErrorClass != QUALITY_REVIEW`）：方案要求每次实际执行模型审核前
        计数一次，而技术失败不消耗任何 CNT，因此必须回滚。质量不合格绝不回滚。
        """
        counter_kind = self._kind(kind)
        key = self.key(obj_id, counter_kind)
        with self._lock:
            value = int(self._counts.get(key, 0))
            if value <= 0:
                raise CounterError(
                    f"{counter_kind.value} 当前为 {value}，无法回滚（回滚必须对应一次已执行的计数）"
                )
            value -= 1
            self._counts[key] = value
        self._emit(
            "counter_rollback",
            key,
            {"kind": counter_kind.value, "obj_id": self._obj_id(obj_id), "value": value},
        )
        return value

    def scope(self, obj_id: str) -> dict[str, int]:
        """返回某逻辑任务的全部计数（含技术重试），互不累加。"""
        return {
            **{k.value: self.get(obj_id, k) for k in CounterKind},
            "TECH_RETRY": self.get_tech_retry(obj_id),
        }

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(set(self._counts) | set(self._tech))

    # -- 独立技术重试计数（R4：与 CNT 完全分离） ----------------------------
    def increment_tech_retry(self, obj_id: str) -> int:
        key = self._tech_key(obj_id)
        with self._lock:
            value = self._tech.get(key, 0) + 1
            self._tech[key] = value
        self._emit(
            "tech_retry_incremented",
            key,
            {"obj_id": self._obj_id(obj_id), "value": value},
        )
        return value

    def get_tech_retry(self, obj_id: str) -> int:
        key = self._tech_key(obj_id)
        with self._lock:
            return int(self._tech.get(key, 0))

    # -- 快照/恢复 ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {"counts": dict(self._counts), "tech_retry": dict(self._tech)}

    def restore(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise CounterError("CounterStore.restore 需要 dict 快照")
        with self._lock:
            self._counts = {str(k): int(v) for k, v in (data.get("counts") or {}).items()}
            self._tech = {str(k): int(v) for k, v in (data.get("tech_retry") or {}).items()}

    # -- 内部 ---------------------------------------------------------------
    def _emit(self, event: str, counter_id: str, detail: dict) -> None:
        if self._events is None:
            return
        try:
            self._events.emit(event, detail={"cnt_id": counter_id, **detail})
        except Exception as exc:  # pragma: no cover - 事件日志绝不阻断
            _LOG.warning("计数事件写入失败（不阻断主流程）：%s", exc)


__all__ = ["CounterStore", "TECH_RETRY_SCOPE"]
