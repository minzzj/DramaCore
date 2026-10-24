"""执行器抽象层（契约 §7）。

设计红线（任务书第 6/7 节 + R8）：
- 能力必须**如实声明**：`capabilities()` 必须给出 `verified: bool` 与 `notes`；
  未在本机验证过的能力一律 `verified=False`。
- mock 产出必须可识别（`capabilities()["mock"] is True` 且 `JobState.mock is True`），
  任何 mock 产出都不得被当成真实生成。
- 技术失败必须带明确的 `ErrorClass`；`JobStatus.UNKNOWN` 表示「无法判定」，
  **绝不允许被当成成功**。

与契约的差异说明（附加字段，均有默认值，不破坏契约调用方）：
- `JobState` 增加 `mock: bool = False`：契约 §7 未定义 mock 标记字段，而任务书第 9 节
  要求「不能把模拟结果说成真实生成已通过」。该字段只做加法，默认 `False`。
- `capabilities()` 字典增加 `mock` / `reachable` / `verified_reason` 等键：
  契约 §7 只规定最小键集，本模块在最小键集**之外**追加，不改变已有键语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bettercomfyui.models.enums import ErrorClass, JobStatus

__all__ = [
    "CAPABILITY_KEYS",
    "EXECUTOR_CAPABILITY_MIN_KEYS",
    "Executor",
    "ExecutorCapabilityError",
    "ExecutorError",
    "ExecutorUnavailable",
    "JobRef",
    "JobState",
    "assert_not_mock",
    "assert_real_capabilities",
    "is_mock_capabilities",
    "validate_capabilities",
]

#: 契约 §7 规定的最小能力键集（不得缺项）。
EXECUTOR_CAPABILITY_MIN_KEYS: tuple[str, ...] = (
    "image",
    "video",
    "first_last_frame",
    "tail_frames",
    "verified",
    "notes",
)

#: 兼容别名。
CAPABILITY_KEYS = EXECUTOR_CAPABILITY_MIN_KEYS


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ExecutorError(RuntimeError):
    """执行器异常。必须携带 `ErrorClass`，供技术重试分类使用。"""

    default_error_class = ErrorClass.COMFY_WORKFLOW

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        cls = self.default_error_class if error_class is None else error_class
        self.error_class: ErrorClass = ErrorClass.parse(cls)
        self.detail: dict[str, Any] = dict(detail or {})

    def to_job_state(self, *, job_id: str | None = None) -> "JobState":
        """把异常映射为 `JobStatus.FAILED` 的 `JobState`（绝不映射为 UNKNOWN/成功）。"""
        detail = {"job_id": job_id} if job_id else {}
        detail.update(self.detail)
        return JobState(
            status=JobStatus.FAILED,
            progress=None,
            outputs=[],
            error=str(self),
            error_class=self.error_class,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "error_class": str(self.error_class),
            "detail": dict(self.detail),
        }


class ExecutorCapabilityError(ExecutorError):
    """能力声明不合法 / 能力不足 / 拿 mock 冒充真实。"""

    default_error_class = ErrorClass.INPUT_VALIDATION


class ExecutorUnavailable(ExecutorError):
    """执行器不可达（连接失败、节点缺失等）。"""

    default_error_class = ErrorClass.COMFY_WORKFLOW


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobRef:
    job_id: str
    executor: str


@dataclass(frozen=True)
class JobState:
    status: JobStatus
    progress: float | None = None
    outputs: list[Path] = field(default_factory=list)
    error: str | None = None
    error_class: ErrorClass | None = None
    #: 附加字段：True 表示产出为 mock 占位物，绝不是真实生成结果。
    mock: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobStatus.parse(self.status))
        if self.error_class is not None:
            object.__setattr__(self, "error_class", ErrorClass.parse(self.error_class))
        object.__setattr__(
            self, "outputs", [p if isinstance(p, Path) else Path(str(p)) for p in (self.outputs or [])]
        )
        if self.progress is not None:
            p = float(self.progress)
            if p != p or p < 0.0 or p > 1.0:  # NaN / 越界
                raise ValueError(f"progress 必须是 [0,1] 内的数值或 None，收到 {self.progress!r}")
            object.__setattr__(self, "progress", p)
        # 不变量 1：成功状态不得携带错误分类。
        if self.status is JobStatus.SUCCEEDED and self.error_class is not None:
            raise ValueError("JobStatus.SUCCEEDED 不得携带 error_class")
        # 不变量 2：技术失败必须显式分类（禁止无分类的 FAILED）。
        if self.status is JobStatus.FAILED and self.error_class is None:
            raise ValueError("JobStatus.FAILED 必须携带明确的 ErrorClass（禁止无分类失败）")
        # 不变量 3：UNKNOWN 必须带说明，禁止无信息的 UNKNOWN。
        if self.status is JobStatus.UNKNOWN and not self.error:
            raise ValueError("JobStatus.UNKNOWN 必须携带 error 说明，禁止静默 UNKNOWN")

    @property
    def is_success(self) -> bool:
        """只有 SUCCEEDED 才是成功。UNKNOWN 永远不是成功。"""
        return self.status is JobStatus.SUCCEEDED

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )

    @property
    def is_technical_failure(self) -> bool:
        """技术失败：FAILED / CANCELLED / UNKNOWN（UNKNOWN 也是失败，不是成功）。"""
        return self.status in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.UNKNOWN)


@runtime_checkable
class Executor(Protocol):
    """执行器协议（契约 §7）。"""

    name: str

    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef: ...

    def poll(self, ref: JobRef) -> JobState: ...

    def cancel(self, ref: JobRef) -> bool: ...

    def capabilities(self) -> dict: ...


# ---------------------------------------------------------------------------
# 能力声明校验（R8）
# ---------------------------------------------------------------------------


def validate_capabilities(caps: Any) -> dict:
    """校验能力声明是否如实、完整。不合法直接抛错，绝不静默补齐默认值。"""
    if not isinstance(caps, dict):
        raise ExecutorCapabilityError(f"capabilities() 必须返回 dict，收到 {type(caps).__name__}")
    missing = [k for k in EXECUTOR_CAPABILITY_MIN_KEYS if k not in caps]
    if missing:
        raise ExecutorCapabilityError(
            "capabilities() 缺少契约必需字段：" + ", ".join(missing),
            detail={"missing": missing},
        )
    for key in ("image", "video", "first_last_frame", "verified"):
        if not isinstance(caps[key], bool):
            raise ExecutorCapabilityError(f"capabilities()[{key!r}] 必须是 bool，收到 {caps[key]!r}")
    tf = caps["tail_frames"]
    if tf is not None and not isinstance(tf, int):
        raise ExecutorCapabilityError(f"capabilities()['tail_frames'] 必须是 int 或 None，收到 {tf!r}")
    notes = caps["notes"]
    if not isinstance(notes, str) or not notes.strip():
        raise ExecutorCapabilityError(
            "capabilities()['notes'] 必须是非空字符串：需写明未验证原因与前置条件"
        )
    if caps["verified"] is True and caps.get("mock") is True:
        raise ExecutorCapabilityError(
            "mock 执行器不得声明 verified=True（mock 产出不是真实生成）",
            detail={"mock": True},
        )
    return caps


def is_mock_capabilities(caps: dict) -> bool:
    """capabilities 是否明确标注为 mock。"""
    validate_capabilities(caps)
    return bool(caps.get("mock") is True)


def assert_real_capabilities(caps: dict, *, purpose: str) -> dict:
    """要求「真实生成能力」的场合调用：mock 或未验证一律拒绝。"""
    validate_capabilities(caps)
    if caps.get("mock") is True:
        raise ExecutorCapabilityError(
            f"{purpose} 需要真实生成能力，但该执行器声明 mock=True（mock 产出不得冒充真实生成）",
            detail={"mock": True, "notes": caps.get("notes")},
        )
    if caps.get("verified") is not True:
        raise ExecutorCapabilityError(
            f"{purpose} 需要已验证的真实生成能力，但该执行器 verified=False；"
            f"前置条件见 notes：{caps.get('notes')}",
            detail={"verified": False, "notes": caps.get("notes")},
        )
    return caps


def assert_not_mock(state: JobState, *, purpose: str) -> JobState:
    """断言产出不是 mock（用于任何要声称「真实生成」的下游路径）。"""
    if state.mock:
        raise ExecutorCapabilityError(
            f"{purpose} 收到 mock 产出（JobState.mock=True），不得当作真实生成结果",
            detail={"outputs": [str(p) for p in state.outputs]},
        )
    return state
