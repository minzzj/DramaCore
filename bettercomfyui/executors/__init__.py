"""执行器包：统一提交/轮询/取消/能力声明（契约 §7）。

依赖方向：`executors` 只依赖 `models.enums`，不依赖 store/prompt/review/director/pipeline。
"""

from __future__ import annotations

from bettercomfyui.executors.base import (
    CAPABILITY_KEYS,
    EXECUTOR_CAPABILITY_MIN_KEYS,
    Executor,
    ExecutorCapabilityError,
    ExecutorError,
    ExecutorUnavailable,
    JobRef,
    JobState,
    assert_not_mock,
    assert_real_capabilities,
    is_mock_capabilities,
    validate_capabilities,
)
from bettercomfyui.executors.comfyui_http import (
    DEFAULT_BASE_URL,
    ComfyUIHttpExecutor,
    UrllibTransport,
    meets_min_comfyui,
    read_comfyui_version,
)
from bettercomfyui.executors.director_nodes import (
    DIRECTOR_NODE_MAP,
    DirectorNodeExecutor,
    DirectorNodeUnavailable,
)
from bettercomfyui.executors.mock import MockExecutor

__all__ = [
    "CAPABILITY_KEYS",
    "EXECUTOR_CAPABILITY_MIN_KEYS",
    "DEFAULT_BASE_URL",
    "read_comfyui_version",
    "meets_min_comfyui",
    "DIRECTOR_NODE_MAP",
    "ComfyUIHttpExecutor",
    "DirectorNodeExecutor",
    "DirectorNodeUnavailable",
    "Executor",
    "ExecutorCapabilityError",
    "ExecutorError",
    "ExecutorUnavailable",
    "JobRef",
    "JobState",
    "MockExecutor",
    "UrllibTransport",
    "assert_not_mock",
    "assert_real_capabilities",
    "is_mock_capabilities",
    "validate_capabilities",
]
