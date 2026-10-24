"""段级审核（R_seg / R1a）与相邻边界验证（N1_EDGE）。

契约 §7：
- `review(segment, version)`：仅审核当前段当前版本，绑定 `AUDIT_{segment_id}_{version}`，
  R1a 四维 = 画面连贯 / 角色一致 / 动作合理 / 无闪烁。
- `verify_edge(seg_n, seg_n1)`：**只验边界，绝不触发 R_full**。

强制约束：
1. 版本不一致 -> `StaleSegmentVersion`（旧版本结论不得用于新版本）。
2. 没有评分素材 -> `ReviewMaterialMissing`，**绝不凭空造分数**（假成功零容忍）。
3. `audit_scope` 必须真实生效：scope=full 时不得执行段级审核。
4. R_full 只能在最终拼接后由 pipeline 执行一次；本模块**没有**任何调用它的路径，
   `run_full_review()` 一旦被调用即抛 `ReviewScopeViolation`（防误用）。
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from bettercomfyui.director.deps import CounterLike, IdFactory, default_ids
from bettercomfyui.director.errors import (
    DirectorDependencyError,
    EdgeVerificationError,
    ReviewMaterialMissing,
    ReviewScopeViolation,
    StaleSegmentVersion,
)
from bettercomfyui.director.events import EventRecorder
from bettercomfyui.director.plan import ensure_segment
from bettercomfyui.director.settings import cfg_get
from bettercomfyui.models.entities import Config, Segment, now_iso
from bettercomfyui.models.enums import AuditScope, ReviewState, SegmentStatus

__all__ = ["R1A_DIMENSIONS", "SegmentReviewer"]

#: R1a 四维（方案参数表六）。可由 `cfg.video_review.r1a_dimensions` 覆盖。
R1A_DIMENSIONS = ("画面连贯", "角色一致", "动作合理", "无闪烁")


class SegmentReviewer:
    """R_seg + N1_EDGE。"""

    def __init__(
        self,
        counters: CounterLike | None = None,
        *,
        cfg: Config | dict | Any = None,
        ids: IdFactory | None = None,
        scorer: Callable[[Segment, str], dict] | None = None,
        edge_scorer: Callable[[Segment, Segment], dict] | None = None,
        events: EventRecorder | None = None,
        project_id: str | None = None,
        full_review_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.counters = counters
        self.cfg = cfg
        self._ids = ids
        self.scorer = scorer
        self.edge_scorer = edge_scorer
        self.events = events if events is not None else EventRecorder(namespace="BETTERCOMFYUI")
        self.project_id = str(project_id or "")
        #: 保留引用仅为「证明从未被调用」；本类不会调用它。
        self.full_review_runner = full_review_runner
        self._full_review_calls = 0

    # -- 公共属性 ---------------------------------------------------------
    @property
    def ids(self) -> IdFactory:
        if self._ids is None:
            self._ids = default_ids()
        return self._ids

    @property
    def full_review_calls(self) -> int:
        """R_full 调用次数。段级审核与边界验证路径必须保持 0。"""
        return self._full_review_calls

    @property
    def audit_scope(self) -> AuditScope:
        scope = cfg_get(self.cfg, "video_review.audit_scope", AuditScope.BOTH)
        return scope if isinstance(scope, AuditScope) else AuditScope.parse(scope)

    @property
    def threshold(self) -> float:
        """**全局**通过阈值（单段未覆盖时用它）。"""
        return float(cfg_get(self.cfg, "video_review.threshold_segment", 70.0))

    def threshold_for(self, segment: Any) -> float:
        """该段**实际生效**的阈值：单段覆盖优先，否则全局。

        任务书第 4 节把 `threshold` 列为视频段字段，第 3 节 E 要求
        「单段覆盖必须显式记录」。**只把覆盖存下来是不够的** ——
        审核时若仍读全局值，覆盖就是个摆设（改了一点效果都没有，
        而审计日志里却写着"已覆盖"，这比不支持覆盖更糟）。

        所以这里必须真的查一次段的 overrides。
        """
        ov = getattr(segment, "overrides", None)
        if ov is not None and getattr(ov, "threshold", None) is not None:
            return float(ov.threshold)
        return self.threshold

    def audit_scope_for(self, segment: Any) -> AuditScope:
        """该段**实际生效**的审核范围（单段覆盖优先）。"""
        ov = getattr(segment, "overrides", None)
        if ov is not None and getattr(ov, "audit_scope", None) is not None:
            scope = ov.audit_scope
            return scope if isinstance(scope, AuditScope) else AuditScope.parse(scope)
        return self.audit_scope

    @property
    def dimensions(self) -> tuple[str, ...]:
        configured = cfg_get(self.cfg, "video_review.r1a_dimensions", None)
        if configured:
            return tuple(str(d) for d in configured)
        return R1A_DIMENSIONS

    def run_full_review(self, *args: Any, **kwargs: Any) -> Any:
        """唯一合法入口在 `pipeline/full_review.py`。此处调用即为作用域越界。"""
        self._full_review_calls += 1  # 记录误用次数，便于测试断言
        raise ReviewScopeViolation(
            "R_full 只能在最终整片拼接之后、S_OUT 之前执行一次；"
            "段级审核（R_seg）与相邻边界验证（N1_EDGE）禁止触发整片审核"
        )

    # -- R_seg ------------------------------------------------------------
    def review(
        self,
        segment: Segment,
        version: str,
        *,
        material: dict | None = None,
    ) -> dict:
        """R_seg：只审当前段的当前版本。

        调用约定（Lead pipeline 接线时踩过，务必遵守）：
        1. **先登记候选尝试并把 `segment.current_version` 设成待审版本**，再调用本方法；
           否则 `version != current_version` 会抛 `StaleSegmentVersion`（守卫是对的）。
        2. 本方法返回**深拷贝副本**（`result["segment"]`），调用方必须用它替换本地对象。
        3. 评分来源优先级：显式 `material` > 注入的 `scorer`；传了 `material` 就不会调用
           `scorer`（显式输入优先，避免两套分数打架）。
        4. 本方法不触碰任何 CNT 计数器，也**绝不会**触发 R_full。
        """
        segment = self._assert_segment(segment, "SegmentReviewer.review")
        if version != segment.current_version:
            raise StaleSegmentVersion(
                f"审核对象版本 {version} 与段 {segment.segment_id} 当前版本 "
                f"{segment.current_version} 不一致：旧版本审核结论不得用于新版本"
            )
        if self.audit_scope_for(segment) is AuditScope.FULL:
            raise ReviewScopeViolation(
                f"audit_scope=full：不执行段级审核（R_seg），只做最终整片审核"
            )
        dims = self._collect_dimensions(segment, version, material)
        score = round(sum(dims.values()) / len(dims), 6)
        threshold = self.threshold_for(segment)
        passed = score >= threshold
        audit_id = self.ids.audit_id(segment.segment_id, version)
        expected = f"AUDIT_{segment.segment_id}_{version}"
        if audit_id != expected:
            raise DirectorDependencyError(
                f"store.ids.audit_id 返回值 {audit_id!r} 与契约 §2 格式 {expected!r} 不一致"
            )
        review_cycle_id = self.ids.review_cycle_id("segment", segment.segment_id, version)
        # `SegmentStatus`（冻结枚举）没有「质量不合格」取值：通过 -> PASSED；
        # 未通过 -> 保持 REVIEWING，失败语义由 review_status=ReviewState.BLOCKED 承载
        # （质量不合格既不是技术失败 FAILED，也不是级联失败 CASCADE_FAILED）。
        status = SegmentStatus.PASSED if passed else SegmentStatus.REVIEWING
        review_state = ReviewState.ALLOWED if passed else ReviewState.BLOCKED

        updated = copy.deepcopy(segment)
        updated.status = status
        updated.review_status = review_state
        updated.updated_at = now_iso()
        # 把评分写回「当前版本对应的候选尝试」——兜底策略要靠它选最高分
        attempt = updated.current_attempt()
        if attempt is not None:
            attempt.score = score
            attempt.passed = passed
        updated.review_result = {
            "audit_id": audit_id,
            "review_cycle_id": review_cycle_id,
            "version": version,
            "dimensions": dict(dims),
            "score": score,
            "threshold": threshold,
            "passed": passed,
            "mode": "MODEL",
            "cascade_segment": bool(updated.cascade_marked),
            "reviewed_at": updated.updated_at,
        }
        self.events.emit(
            "SEGMENT_REVIEW_RESULT",
            stage="SEGMENT_REVIEW",
            object_id=segment.segment_id,
            version=version,
            detail={
                "audit_id": audit_id,
                "score": score,
                "threshold": threshold,
                "passed": passed,
                "dimensions": dict(dims),
            },
            severity="INFO" if passed else "WARN",
        )
        return {
            "audit_id": audit_id,
            "review_cycle_id": review_cycle_id,
            "segment_id": segment.segment_id,
            "version": version,
            "dimensions": dict(dims),
            "scores": dict(dims),
            "score": score,
            "threshold": threshold,
            "passed": passed,
            "status": status,
            "review_state": review_state,
            "scope": "segment",
            "segment": updated,
            "full_review_calls": self._full_review_calls,
        }

    # -- N1_EDGE ----------------------------------------------------------
    def verify_edge(
        self,
        seg_n: Segment,
        seg_n1: Segment,
        *,
        material: dict | None = None,
    ) -> dict:
        """只验证第 N 段与第 N+1 段的相邻边界；**不触发 R_full**。

        边界素材优先级：显式 `material` > 注入的 `edge_scorer`（同 `review`）。
        两段必须相邻（`seg_n1.segment_index == seg_n.segment_index + 1`）且各自是当前版本。
        """
        seg_n = self._assert_segment(seg_n, "verify_edge(seg_n)")
        seg_n1 = self._assert_segment(seg_n1, "verify_edge(seg_n1)")
        if seg_n1.segment_index != seg_n.segment_index + 1:
            raise EdgeVerificationError(
                f"边界验证要求相邻段：{seg_n.segment_id}(index={seg_n.segment_index}) 与 "
                f"{seg_n1.segment_id}(index={seg_n1.segment_index}) 不相邻"
            )
        declared = [
            d for d in (seg_n1.dependency_refs or []) if str(d).split("|")[0] == seg_n.segment_id
        ]
        raw = material
        if raw is None and self.edge_scorer is not None:
            raw = self.edge_scorer(seg_n, seg_n1)
        if raw is None:
            raise ReviewMaterialMissing(
                f"缺少 {seg_n.segment_id}->{seg_n1.segment_id} 的边界验证素材；"
                "禁止凭空给出边界通过结论"
            )
        score = _edge_score(raw)
        # 边界（N1）属于**两个段共有**：必须同时满足双方的阈值。
        # 取两者中更严的一个，而不是只看后者 —— 否则前段要求 95、
        # 后段用全局 70 时，边界会按 70 放行，前段的高标准被绕过。
        threshold = max(self.threshold_for(seg_n), self.threshold_for(seg_n1))
        passed = score >= threshold
        edge_id = self.ids.review_cycle_id(
            "EDGE_N1", f"{seg_n.segment_id}->{seg_n1.segment_id}", seg_n1.current_version
        )
        self.events.emit(
            "N1_EDGE_RESULT",
            stage="SEGMENT_REVIEW",
            object_id=seg_n1.segment_id,
            version=seg_n1.current_version,
            detail={
                "edge_id": edge_id,
                "upstream": seg_n.segment_id,
                "score": score,
                "threshold": threshold,
                "passed": passed,
                "declared_dependency": bool(declared),
                "scope": "edge_only",
            },
            severity="INFO" if passed else "WARN",
        )
        return {
            "edge_id": edge_id,
            "segment_n": seg_n.segment_id,
            "segment_n1": seg_n1.segment_id,
            "version_n": seg_n.current_version,
            "version_n1": seg_n1.current_version,
            "score": score,
            "threshold": threshold,
            "passed": passed,
            "scope": "edge_only",
            "degrade_flag": None,  # 降级由 CascadeController 决策（N1_FALLBACK）
            "declared_dependency": bool(declared),
            "full_review_calls": self._full_review_calls,
        }

    # -- 内部 -------------------------------------------------------------
    def _assert_segment(self, segment: Any, where: str) -> Segment:
        """兼容契约 §7 的 dict 调用方：统一还原为 Segment。"""
        return ensure_segment(segment, where=where)

    def _collect_dimensions(
        self, segment: Segment, version: str, material: dict | None
    ) -> dict[str, float]:
        raw = material
        if raw is None and self.scorer is not None:
            raw = self.scorer(segment, version)
        if raw is None:
            raise ReviewMaterialMissing(
                f"段 {segment.segment_id}@{version} 没有 R1a 评分素材："
                "必须注入 scorer 或显式传入 material；禁止伪造评分"
            )
        if not isinstance(raw, dict):
            raise ReviewMaterialMissing(f"评分素材必须是映射，收到 {type(raw).__name__}")
        payload = raw.get("dimensions") if isinstance(raw.get("dimensions"), dict) else raw
        expected_dims = self.dimensions
        missing = [d for d in expected_dims if d not in payload]
        if missing:
            raise ReviewMaterialMissing(
                f"R1a 评分素材缺少维度 {missing}；期望维度 {list(expected_dims)}"
            )
        dims: dict[str, float] = {}
        for name in expected_dims:
            value = payload[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ReviewMaterialMissing(f"维度 {name!r} 的分数必须是数值，收到 {value!r}")
            value = float(value)
            if not 0.0 <= value <= 100.0:
                raise ReviewMaterialMissing(f"维度 {name!r} 的分数必须落在 [0,100]，收到 {value}")
            dims[name] = value
        return dims


def _edge_score(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, dict)):
        raise ReviewMaterialMissing(f"边界验证素材必须是数值或映射，收到 {type(raw).__name__}")
    if isinstance(raw, (int, float)):
        score = float(raw)
    elif "score" in raw:
        score = float(raw["score"])
    elif isinstance(raw.get("dimensions"), dict):
        dims = raw["dimensions"]
        if not dims:
            raise ReviewMaterialMissing("边界验证 dimensions 不得为空")
        score = sum(float(v) for v in dims.values()) / len(dims)
    else:
        raise ReviewMaterialMissing(f"边界验证素材缺少 score/dimensions：{raw!r}")
    if not 0.0 <= score <= 100.0:
        raise ReviewMaterialMissing(f"边界验证分数必须落在 [0,100]，收到 {score}")
    return score
