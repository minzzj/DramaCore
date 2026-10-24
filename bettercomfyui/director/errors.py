"""director/ 异常定义。

分类原则：
- 契约/门禁类错误（R3 未 LOCK、依赖缺失、深度超限）必须**显式抛出**，不得降级为返回值。
- 技术失败由执行器层表达（`ErrorClass`），director 只做分类与计数，不吞错。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CascadeDependencyMissing",
    "CascadePolicyError",
    "DirectorConfigError",
    "DirectorDependencyError",
    "DirectorError",
    "EdgeVerificationError",
    "LockGateError",
    "ReviewMaterialMissing",
    "ReviewScopeViolation",
    "SegmentPlanError",
    "StaleSegmentVersion",
]


class DirectorError(RuntimeError):
    """director 层基类异常。"""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})


class DirectorDependencyError(DirectorError):
    """依赖未注入 / 无法解析（store.ids、review.CounterStore 等）。"""


class DirectorConfigError(DirectorError):
    """配置缺失或非法（不允许隐式默认）。"""


class SegmentPlanError(DirectorError):
    """段计划输入不合法：缺字段、引用无效、重复 ID。绝不返回部分结果。"""


class LockGateError(DirectorError):
    """R3 门禁：引用了未经 resolve_lock() 通过的素材版本。"""


class StaleSegmentVersion(DirectorError):
    """审核对象版本与当前段版本不一致：旧版本结论不得用于新版本。"""


class ReviewMaterialMissing(DirectorError):
    """缺少审核素材（评分维度/边界指纹）。禁止凭空造分数。"""


class ReviewScopeViolation(DirectorError):
    """审核范围越界：段级/边界审核不得触发 R_full。"""


class CascadeDependencyMissing(DirectorError):
    """级联段找不到上游依赖，无法做相邻边界验证。"""


class CascadePolicyError(DirectorConfigError):
    """级联超限策略非法/未显式配置。"""


class EdgeVerificationError(DirectorError):
    """相邻边界验证输入非法（非相邻段、版本不一致等）。"""
