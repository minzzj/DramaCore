"""review 层异常。

设计原则：所有异常都携带足够上下文（状态、事件、允许值），便于日志定位；
且**绝不**用异常之外的方式表达「非法」——review 层不允许静默忽略非法组合。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..models.enums import ErrorClass, ReviewState

__all__ = [
    "ReviewError",
    "IllegalTransition",
    "ArbitrationError",
    "CounterError",
    "IllegalCounterReset",
    "ModelRoundAlreadyOpen",
    "CandidateLimitReached",
    "FinalizationError",
    "NotificationError",
    "ReviewConfigError",
    "ReviewTechnicalError",
    "StoreBridgeError",
]


def _name(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, ReviewState):
        return value.value
    return str(value)


class ReviewError(Exception):
    """review 层异常基类。"""


class IllegalTransition(ReviewError):
    """非法「状态 + 事件」组合：不允许静默忽略。

    方案第七部分·六：每个「状态 + 事件」组合只允许一个目标状态。
    """

    def __init__(
        self,
        from_state: Any,
        event: Any,
        *,
        allowed: Iterable[str] | None = None,
        reason: str | None = None,
    ) -> None:
        self.from_state = from_state
        self.event = event
        self.allowed = tuple(allowed or ())
        self.reason = reason
        message = f"非法状态转移：{_name(from_state)} --({event})--> 无目标状态"
        if reason:
            message += f"；原因：{reason}"
        if self.allowed:
            message += f"；当前状态允许的事件：{', '.join(self.allowed)}"
        else:
            message += "；当前状态不允许任何事件（终态或未定义）"
        super().__init__(message)


class ArbitrationError(ReviewError):
    """H_arb 仲裁输入不完整/不合法（缺少统一后端时间戳等）。"""


class CounterError(ReviewError):
    """计数器使用错误。"""


class IllegalCounterReset(CounterError):
    """非法重置：唯一合法场景是「人工超时切换到模型模式时 CNT1 归零」。"""


class ModelRoundAlreadyOpen(CounterError):
    """同一审核周期内已有一轮模型审核在进行，禁止重复开启（避免重复计数）。"""


class CandidateLimitReached(CounterError):
    """CNT1 已达到模型候选上限，不得再创建新的模型候选版本。"""

    def __init__(self, message: str, *, best_candidate: dict | None = None) -> None:
        self.best_candidate = best_candidate
        super().__init__(message)


class FinalizationError(ReviewError):
    """最终提交（commit_final）相关错误。"""


class NotificationError(ReviewError):
    """通知渠道自身错误；由 NotificationService 捕获并降级为 WARN 日志。"""


class ReviewConfigError(ReviewError, ValueError):
    """review 配置缺失/非法。"""


class ReviewTechnicalError(ReviewError):
    """技术失败（ErrorClass != QUALITY_REVIEW），绝不消耗任何 CNT。"""

    def __init__(self, message: str, *, error_class: ErrorClass = ErrorClass.MODEL_API) -> None:
        self.error_class = error_class
        super().__init__(message)


class StoreBridgeError(ReviewError):
    """store 桥接层错误（回退文件锁/原子写失败、元数据损坏等）。"""
