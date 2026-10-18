"""段质量不合格返工流程（流程图 cluster_SG5：FAIL_KIND / CNT2_CHECK / U / REGEN / CNT2 / Y / Y_SRC）。

CNT2/CNT3 边界（R4 落地，逐条对照契约 §7 表格）：
| 场景 | 计数 |
|---|---|
| 当前问题段质量不合格 -> REGEN | CNT2 + 1 |
| 级联段 N1 重生成 | CNT3 + 1（由 CascadeController 负责） |
| API 超时 / 节点异常 / 文件损坏 | 都不计，只加技术重试计数 |
| 版本号递增 | 都不清零 |

因此本模块：
- 只在**重生成成功产出新版本之后** `CNT2 + 1`；
- 技术失败分支**直接返回**，不触碰任何 CNT；
- 级联段一律委派 `CascadeController`（走 CNT3，绝不累加到 CNT2）。
"""

from __future__ import annotations

from typing import Any

from bettercomfyui.director.deps import CounterLike, IdFactory, default_ids
from bettercomfyui.director.errors import DirectorDependencyError, DirectorError
from bettercomfyui.director.augment import build_rework_prompt
from bettercomfyui.director.events import EventRecorder
from bettercomfyui.director.plan import ensure_segment
from bettercomfyui.director.settings import cfg_get
from bettercomfyui.models.entities import Segment, now_iso
from bettercomfyui.models.enums import CounterKind, DegradeFlag, SegmentSource, SegmentStatus

__all__ = ["ReworkController"]


class ReworkController:
    """普通问题段（CNT2）与级联段（CNT3）的返工分派器。"""

    def __init__(
        self,
        counters: CounterLike,
        *,
        cfg: Any = None,
        ids: IdFactory | None = None,
        generator: Any | None = None,
        reviewer: Any | None = None,
        cascade: Any | None = None,
        events: EventRecorder | None = None,
        asset_store: Any | None = None,
        project_id: str | None = None,
        source_invalidator: Any | None = None,
    ) -> None:
        if counters is None:
            raise DirectorDependencyError(
                "ReworkController 必须注入 review.CounterStore（CNT 唯一真源）"
            )
        self.counters = counters
        self.cfg = cfg
        self._ids = ids
        self.generator = generator
        self.reviewer = reviewer
        self.cascade = cascade
        self.events = events if events is not None else EventRecorder(namespace="CASCADE")
        self.project_id = str(project_id or "")
        #: 可选：把「旧版本审核结论失效」落到 store（director 不直接写素材库）
        self.source_invalidator = source_invalidator
        #: 可选：素材库，用于把 `asset_id` 反查成 `@类型_序号_名称` 标记。
        #: 不给也能工作 —— 标记会退化成 `@标签`（信息略少，但仍有效）。
        self.asset_store = asset_store

    # -- 配置 -------------------------------------------------------------
    @property
    def ids(self) -> IdFactory:
        if self._ids is None:
            self._ids = default_ids()
        return self._ids

    @property
    def cnt2_limit(self) -> int:
        return int(cfg_get(self.cfg, "video_review.cnt2_limit", 3))

    @property
    def cnt3_limit(self) -> int:
        return int(cfg_get(self.cfg, "video_review.cnt3_limit", 3))

    # -- FAIL_KIND --------------------------------------------------------
    @staticmethod
    def is_cascade_segment(segment: Segment) -> bool:
        """FAIL_KIND：当前对象是否为级联段？"""
        if segment.cascade_marked:
            return True
        if segment.status is SegmentStatus.NEEDS_CASCADE_CHECK:
            return True
        return segment.source in (SegmentSource.CASCADE_FALLBACK,)

    def handle_quality_failure(
        self,
        segment: Segment,
        review: dict,
        *,
        executor: Any,
        is_cascade_segment: bool | None = None,
    ) -> dict:
        if not isinstance(segment, Segment):
            segment = ensure_segment(segment, where="ReworkController.handle_quality_failure")
        cascade_segment = (
            self.is_cascade_segment(segment)
            if is_cascade_segment is None
            else bool(is_cascade_segment)
        )
        seg_id = segment.segment_id
        self.events.emit(
            "SEGMENT_QUALITY_FAILURE",
            stage="SEGMENT_REVIEW",
            object_id=seg_id,
            version=segment.current_version,
            severity="WARN",
            detail={
                "is_cascade_segment": cascade_segment,
                "score": (review or {}).get("score"),
                "threshold": (review or {}).get("threshold"),
            },
        )
        if cascade_segment:
            # 级联问题段 -> CNT3_CHECK / N1 / CNT3（绝不进 CNT2）
            if self.cascade is None:
                raise DirectorDependencyError(
                    f"段 {seg_id} 是级联段，但未注入 CascadeController："
                    "级联返工必须走 CNT3 独立通道"
                )
            run = self.cascade.run(segment, executor=executor)
            return {
                "counter": "CNT3",
                "delegated": True,
                "outcome": run.get("outcome"),
                "segment_id": seg_id,
                "cascade": run,
                "cnt2": int(self.counters.get(seg_id, CounterKind.CNT2)),
                "cnt3": int(self.counters.get(seg_id, CounterKind.CNT3)),
            }
        return self._rework_normal(segment, review=review, executor=executor)

    # -- X：提示词加料（图一 SG5）----------------------------------------
    def _augment_rework_prompt(self, segment: Segment, review: dict) -> dict:
        """返工前给提示词加料，并把加料结果写回段（可追溯）。

        写回方式（**都可查**，不覆盖掉原始意图）：
        * `segment.generation_params["prompt"]` = 加料后的提示词（生成时用它）；
        * `segment.generation_params["prompt_original_before_augment"]` = 原文；
        * 发 `REWORK_PROMPT_AUGMENTED` 事件，带上加了哪些维度/素材。

        加料失败**不得阻断返工** —— 记 WARN 事件后按原文继续（返工本身更重要）。
        """
        try:
            mode = str(cfg_get(self.cfg, "video_review.rework_prompt_augment", "both"))
            result = build_rework_prompt(
                segment, review or {},
                mode=mode,
                resolver=self._asset_resolver,
            )
        except Exception as exc:  # noqa: BLE001 - 加料失败不能拖垮返工
            self.events.emit(
                "REWORK_PROMPT_AUGMENT_FAILED",
                stage="SEGMENT_REVIEW",
                object_id=getattr(segment, "segment_id", None),
                severity="WARN",
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
            return {"mode": "error", "changed": False, "error": str(exc)}

        if result.get("changed"):
            params = dict(getattr(segment, "generation_params", None) or {})
            params.setdefault("prompt_original_before_augment", result["original"])
            params["prompt"] = result["augmented"]
            segment.generation_params = params
            self.events.emit(
                "REWORK_PROMPT_AUGMENTED",
                stage="SEGMENT_REVIEW",
                object_id=segment.segment_id,
                version=segment.current_version,
                detail={
                    "mode": result["mode"],
                    "failed_dimensions": [d[0] for d in result["added_dimensions"]],
                    "asset_markers": result["added_markers"],
                },
            )
        return result

    def _asset_resolver(self, asset_id: str) -> dict | None:
        """把 asset_id 解析成 `{name, asset_type}`，用于生成 `@类型_序号_名称`。

        没有注入 store 时返回 None —— 标记会退化成 `@标签`，仍然是有效信息。
        """
        store = getattr(self, "asset_store", None)
        if store is None:
            return None
        try:
            for entry in store.list_assets():
                if entry.get("asset_id") == asset_id:
                    return {"name": entry.get("name"),
                            "asset_type": entry.get("asset_type")}
        except Exception:  # noqa: BLE001
            return None
        return None

    # -- CNT2_CHECK / U / REGEN / CNT2 / Y / Y_SRC -------------------------
    def _rework_normal(
        self, segment: Segment, *, review: dict, executor: Any
    ) -> dict:
        if self.generator is None or self.reviewer is None:
            raise DirectorDependencyError(
                "普通段返工需要注入 generator 与 reviewer（缺失则不得假装返工）"
            )
        seg_id = segment.segment_id
        cnt2 = int(self.counters.get(seg_id, CounterKind.CNT2))
        limit = self.cnt2_limit
        if cnt2 >= limit:
            # Y：从候选中取最高分；Y_SRC：来源记为 REVIEW_FALLBACK，正常拼接并写元数据
            best = select_highest_score_candidate(segment)
            if best is None:
                raise DirectorError(
                    f"段 {seg_id} 已达 CNT2 上限 {limit}，但没有任何带评分的候选版本："
                    "禁止伪造兜底结果",
                    detail={"segment_id": seg_id, "cnt2": cnt2},
                )
            self.events.emit(
                "CNT2_LIMIT_REACHED",
                stage="SEGMENT_REVIEW",
                object_id=seg_id,
                version=best.get("version"),
                severity="WARN",
                detail={"cnt2": cnt2, "limit": limit, "selected": best},
            )
            return {
                "counter": "CNT2",
                "outcome": "REVIEW_FALLBACK",
                "segment_id": seg_id,
                "cnt2": cnt2,
                "cnt3": int(self.counters.get(seg_id, CounterKind.CNT3)),
                "selected": best,
                "source": SegmentSource.REVIEW_FALLBACK,
                "degrade_flag": DegradeFlag.FALLBACK_DEGRADED,
                "deliverable": True,
                "y2": None,
            }

        # U：旧版本审核结论失效；仅重生成当前问题段
        invalidation = self._invalidate_old_versions(segment)

        # X（图一 SG5「提示词自动加相关提示词和 @素材」）：
        # 返工前把**失败维度**和**必须锚定的素材**加进提示词。
        # 修前这里把同一段提示词原样再喂一遍，模型没有任何新信息，
        # CNT2 三次很快烧完只能取最高分兜底 —— 加料才是"重做"与"重复"的区别。
        augmentation = self._augment_rework_prompt(segment, review)

        gen = self.generator.generate(
            segment, executor=executor, source=SegmentSource.REGENERATED
        )
        if gen.get("technical_failure"):
            # 技术失败：只加技术重试计数，绝不动 CNT2/CNT3
            return {
                "counter": "CNT2",
                "outcome": "TECH_FAILED",
                "segment_id": seg_id,
                "cnt2": int(self.counters.get(seg_id, CounterKind.CNT2)),
                "cnt3": int(self.counters.get(seg_id, CounterKind.CNT3)),
                "tech_retry": gen.get("tech_retry"),
                "error_class": gen.get("error_class"),
                "error": gen.get("error"),
                "invalidation": invalidation,
                "generation": gen,
                "prompt_augment": augmentation,
            }

        # REGEN 成功产出新版本 -> CNT2 + 1（顺序与流程图一致：REGEN -> CNT2+1 -> R_seg）
        new_segment = gen["segment"]
        cnt2_after = int(self.counters.increment(seg_id, CounterKind.CNT2))
        review = self.reviewer.review(new_segment, gen["version"])
        reviewed = review.get("segment", new_segment)
        self.events.emit(
            "CNT2_INCREMENT",
            stage="SEGMENT_REVIEW",
            object_id=seg_id,
            version=gen["version"],
            detail={"cnt2": cnt2_after, "outcome": "REGENERATED"},
        )
        return {
            "counter": "CNT2",
            "outcome": "REGENERATED_PASSED" if review.get("passed") else "REGENERATED_BLOCKED",
            "segment_id": seg_id,
            "version": gen["version"],
            "cnt2": cnt2_after,
            "cnt3": int(self.counters.get(seg_id, CounterKind.CNT3)),
            "invalidation": invalidation,
            "generation": gen,
            "review": review,
            "segment": reviewed,
            "degrade_flag": DegradeFlag.NONE,
            "deliverable": True,
            "y2": None,
        }

    def _invalidate_old_versions(self, segment: Segment) -> dict:
        """U：旧版本审核结论失效（新版本必须重新审核，旧结论不得授权新版本）。"""
        prior = {a.version for a in segment.attempts}
        prior.add(segment.current_version)
        invalidated = {
            "segment_id": segment.segment_id,
            "current_version": segment.current_version,
            "invalidated_versions": sorted(
                prior, key=lambda v: int(str(v).lstrip("v") or 0)
            ),
            "note": "旧版本审核结论失效：新版本必须重新审核（方案第七部分 二·1）",
            "invalidated_at": now_iso(),
        }
        if self.source_invalidator is not None:
            self.source_invalidator(invalidated)
        return invalidated


def select_highest_score_candidate(segment: Segment) -> dict | None:
    """从段的候选尝试中选最高分（平局取更早版本，保证确定性）。"""
    scored = [
        {
            "attempt_id": a.attempt_id,
            "version": a.version,
            "score": float(a.score),
            "content_hash": a.content_hash,
            "video_path": a.video_path,
            "passed": a.passed,
        }
        for a in segment.attempts
        if a.score is not None
    ]
    if not scored:
        return None
    scored.sort(key=lambda item: (-item["score"], item["version"]))
    return scored[0]
