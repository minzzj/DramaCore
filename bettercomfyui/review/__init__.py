"""review 层公开接口（负责人：teammate `review-lock`）。

对外只暴露 CONTRACTS §6 定义的名字 + 必要的诊断入口：

- `ReviewCycle` / `IllegalTransition`：状态机（唯一通路 `REVIEW_TRANSITIONS`）
- `CounterStore`：CNT1/CNT2/CNT3（绑定逻辑任务 ID，不绑定版本号）
- `AssetReviewer`：素材版本审核（人工/模型、超时切换、CNT1、仲裁、唯一提交）
- `arbitrate`：H_arb（平局模型胜出）
- `commit_final` / `archive_late_result`：唯一最终提交点与迟到结果归档
- `NotificationService`：通知投递（失败只写 WARN，绝不阻断主流程）
- `ReviewEventLog`：namespace=REVIEW 的事件适配层（不自己另建日志文件）
"""

from __future__ import annotations

from .arbiter import arbitrate
from .asset_review import NON_ACTIVITY_KINDS, VALID_HUMAN_OP_KINDS, AssetReviewer
from .commit import (
    CONSENSIVE_STATES,
    CONCLUSIVE_STATES,
    LOCK_STATES,
    NON_CONCLUSIVE_STATES,
    FinalizationLedger,
    archive_late_result,
    commit_final,
    get_ledger,
    ledger_for_cycle,
    reset_memory_ledger,
)
from .config import resolve_review_params, timeout_seconds, validate_review_params
from .counters import CounterStore
from .errors import (
    ArbitrationError,
    CandidateLimitReached,
    CounterError,
    FinalizationError,
    IllegalCounterReset,
    IllegalTransition,
    ModelRoundAlreadyOpen,
    NotificationError,
    ReviewConfigError,
    ReviewError,
    ReviewTechnicalError,
    StoreBridgeError,
)
from .events import (
    EVENT_FIELDS,
    NAMESPACE,
    ReviewEventLog,
    require_store_events,
    reset_store_events_cache,
    store_events_available,
)
from .notify import (
    CHANNEL_ALIASES,
    CallableChannel,
    LogChannel,
    NotificationService,
    UnimplementedChannel,
    WebhookChannel,
    resolve_channel_name,
)
from .state import USER_CANCEL_EVENT, ReviewCycle, events_for_state

__all__ = [
    # 状态机
    "ReviewCycle",
    "IllegalTransition",
    "USER_CANCEL_EVENT",
    "events_for_state",
    # 计数器
    "CounterStore",
    "IllegalCounterReset",
    "CounterError",
    "ModelRoundAlreadyOpen",
    "CandidateLimitReached",
    # 审核
    "AssetReviewer",
    "VALID_HUMAN_OP_KINDS",
    "NON_ACTIVITY_KINDS",
    "resolve_review_params",
    "validate_review_params",
    "timeout_seconds",
    # 仲裁 / 提交
    "arbitrate",
    "ArbitrationError",
    "commit_final",
    "archive_late_result",
    "FinalizationLedger",
    "get_ledger",
    "ledger_for_cycle",
    "reset_memory_ledger",
    "CONSENSIVE_STATES",
    "CONCLUSIVE_STATES",
    "LOCK_STATES",
    "NON_CONCLUSIVE_STATES",
    "FinalizationError",
    # 通知 / 事件
    "NotificationService",
    "NotificationError",
    "CallableChannel",
    "UnimplementedChannel",
    "WebhookChannel",
    "LogChannel",
    "resolve_channel_name",
    "CHANNEL_ALIASES",
    "ReviewEventLog",
    "NAMESPACE",
    "EVENT_FIELDS",
    "require_store_events",
    "store_events_available",
    "reset_store_events_cache",
    # 配置 / 基类
    "ReviewConfigError",
    "ReviewError",
    "ReviewTechnicalError",
    "StoreBridgeError",
]
