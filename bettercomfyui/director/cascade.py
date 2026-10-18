"""级联控制（流程图 cluster_SG5：CASCADE_CHECK / CASCADE_NEXT / Y2 / N1 / CNT3_CHECK /
N1_EDGE / N1_OK / N1_CLEAN / N1_FALLBACK / DEGRADED_EDGE / ALL_SEG_DONE）。

三条硬底线：
1. **CNT3 只用于级联段**：`run()` 每次真正产出新版本后才 `CNT3 + 1`，与 CNT2 完全独立，
   不因版本号递增清零；技术失败不加 CNT3。
2. **级联必须终止**：`max_cascade_depth`（默认取 `cfg.video_review.max_cascade_depth`，缺省 8）
   + 每段 `cnt3_limit` + 硬迭代上界（`_iteration_guard`）三重终止，
   超限按显式策略（BLOCK_OUTPUT / REQUEST_HUMAN / DEGRADE_OUTPUT）处理并产出
   `DegradeFlag.CASCADE_DEGRADED`，**绝不允许无限循环**。
3. **只传播真正受影响的段**：仅当「参考帧/动作/时间轴依赖确实失效」才标记后续段；
   无证据（指纹缺失且版本未变）一律不标记。

依赖失效判定（证据优先，可审计）：
- 若依赖边记录了 `ref_facet` 且当前可取到该维度指纹 -> 指纹不等才判失效（最直接证据）；
- 否则若记录了 `ref_version` 且依赖维度属于内容维度 -> 版本变化才判失效（较弱证据）；
- 两者都没有 -> **不判失效**（规则 9：不得无条件传播）。
每条判定都会在返回值的 `reason` 字段里说明依据。
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

from bettercomfyui.director.deps import CounterLike, IdFactory, default_ids
from bettercomfyui.director.errors import (
    CascadeDependencyMissing,
    CascadePolicyError,
    DirectorDependencyError,
    DirectorError,
)
from bettercomfyui.director.events import EventRecorder
from bettercomfyui.director.plan import ensure_segment, facet_value, parse_dependency_ref
from bettercomfyui.director.rework import select_highest_score_candidate
from bettercomfyui.director.settings import cfg_get
from bettercomfyui.executors.base import ExecutorError
from bettercomfyui.models.entities import Segment, now_iso
from bettercomfyui.models.enums import (
    CounterKind, DegradeFlag, ReviewState, SegmentSource, SegmentStatus,
)

__all__ = [
    "CASCADE_POLICIES",
    "CONTENT_DEPENDENCY_KINDS",
    "CascadeController",
    "segments_depending_on_asset",
]

#: 级联超限策略（显式记录，不隐式决定）。
CASCADE_POLICIES = frozenset({"BLOCK_OUTPUT", "REQUEST_HUMAN", "DEGRADE_OUTPUT"})

#: 内容维度：版本变化可作为（较弱但可审计的）失效证据。
CONTENT_DEPENDENCY_KINDS = frozenset(
    {"first_frame", "last_frame", "reference_frame", "motion", "action", "timeline"}
)

#: 段「已通过」的判定：状态或审核结论任一说通过即算通过。
_PASSED_SEGMENT_STATUS = frozenset(
    {SegmentStatus.PASSED, SegmentStatus.FALLBACK_SELECTED}
)
_PASSED_REVIEW_STATE = frozenset({ReviewState.ALLOWED, ReviewState.FALLBACK})


def _segment_passed(segment: Segment) -> bool:
    """段是否**已经通过、可以不再重跑**。

    ⚠️ 判据以 `status`（生命周期真相）为主，`review_status` 只在没有 status 时兜底。

    第一版写成「status 或 review_status **任一**说通过就算通过」，实测踩坑：
    一个段被标成 `FAILED`（需要重做）之后，它的 `review_status` 仍是上次审核留下的
    `ALLOWED` —— 于是"要重做的段"被误判成"已通过"，中程失败查询返回空集。

    `review_status` 表达的是**上一个版本**的审核结论；
    `status` 表达的才是这一段**当前是否还需要重做**。两者不能混。
    """
    status = getattr(segment, "status", None)
    if status is not None:
        return status in _PASSED_SEGMENT_STATUS
    return getattr(segment, "review_status", None) in _PASSED_REVIEW_STATE


def segments_depending_on_asset(
    store: Any,
    segments: Iterable[Segment],
    asset_id: str,
    *,
    version: Any = None,
    include_passed: bool = False,
) -> list[dict]:
    """找出**依赖某个素材**的段；默认只返回**尚未通过**的。返回 `[{segment, reference, passed}]`。

    ## 这对应流程图哪几条

    图一 SG8：

        ERR1 -.->|"中程失败仅重跑依赖该素材且未通过的段，已通过段用缓存"| GEN1

    图四：

        F2A["某角色参考图不合格"] --> F2B["仅重跑依赖该角色且未通过的段"]

    这是愿景「爆炸半径」设计的核心：**只重跑受影响的部分，已通过的用缓存保留**
    （愿景原话：「算力成本和质量之间取中间值……只重新生成不合格的那一段」）。

    ## 为什么需要它（ROUND17 缺口）

    「素材 → 段」的**反向索引**此前根本没人写（`record_reference` 零调用），
    所以这条能力**算不出来**，只能整条链重跑。
    ROUND19 已在 `plan_segments` 里把索引写起来，这里补上查询。

    ## 参数

    * `store`：`AssetStore`（用 `referencing_segments` 反查）；
    * `segments`：段列表（用来判断"是否已通过"）；
    * `version`：只查某个素材版本；`None` 查全部版本；
    * `include_passed`：True 时连已通过的段也返回（默认 False）。

    不在 `segments` 里的引用记录会被忽略（段可能已被删除/归档）——
    这比返回一个查不到状态的段更安全。
    """
    by_id = {str(getattr(s, "segment_id", "")): s for s in segments or []}
    out: list[dict] = []
    for ref in store.referencing_segments(asset_id, version=version):
        seg_id = str(ref.get("segment_id") or "")
        segment = by_id.get(seg_id)
        if segment is None:
            continue
        passed = _segment_passed(segment)
        if passed and not include_passed:
            continue
        out.append({"segment": segment, "reference": ref, "passed": passed})
    return out


def _edge_record(dependent_id: str, upstream_id: str, edge: dict) -> dict:
    """统一的依赖边记录：`segment_id` = 受影响的下游段（dependent）。"""
    return {
        "segment_id": dependent_id,
        "upstream_id": upstream_id,
        "kind": edge["kind"],
        "ref_facet": edge.get("ref_facet"),
        "ref_version": edge.get("ref_version"),
    }


def _dedupe_edges(edges: list[dict]) -> list[dict]:
    """同一 (下游段, 依赖维度) 只保留**证据最强**的一条边（指纹 > 版本 > 无证据）。"""
    best: dict[tuple[str, str], dict] = {}
    for edge in edges:
        key = (edge["segment_id"], edge["kind"])
        score = 2 if edge.get("ref_facet") else (1 if edge.get("ref_version") else 0)
        current = best.get(key)
        if current is None or score > _edge_evidence(current):
            best[key] = edge
    return list(best.values())


def _edge_evidence(edge: dict) -> int:
    return 2 if edge.get("ref_facet") else (1 if edge.get("ref_version") else 0)


class CascadeController:
    """Y2 标记 / N1 级联重生成 / 级联终止。"""

    def __init__(
        self,
        counters: CounterLike,
        *,
        cfg: Any = None,
        ids: IdFactory | None = None,
        generator: Any | None = None,
        reviewer: Any | None = None,
        store: Any | None = None,
        scorer: Any | None = None,
        edge_scorer: Any | None = None,
        events: EventRecorder | None = None,
        project_id: str | None = None,
        max_cascade_depth: int | None = None,
        depth_policy: str | None = None,
        segments: Iterable[Segment] | None = None,
    ) -> None:
        if counters is None:
            raise DirectorDependencyError(
                "CascadeController 必须注入 review.CounterStore（CNT 唯一真源）"
            )
        self.counters = counters
        self.cfg = cfg
        self._ids = ids
        self._generator = generator
        self._reviewer = reviewer
        self.store = store
        self._scorer = scorer
        self._edge_scorer = edge_scorer
        self.events = events if events is not None else EventRecorder(namespace="CASCADE")
        self.project_id = str(project_id or "")
        self._max_depth_override = max_cascade_depth
        self._policy_override = depth_policy
        self._registry: dict[str, Segment] = {}
        self._pending: set[str] = set()
        self._cascade_events: list[dict] = []
        if segments is not None:
            self.set_segments(segments)

    # -- 生成/审核依赖（可按需自建；缺失时给出可操作错误，绝不假装完成）------
    @property
    def generator(self) -> Any:
        if self._generator is None:
            from bettercomfyui.director.generate import SegmentGenerator  # noqa: PLC0415

            self._generator = SegmentGenerator(
                self.store,
                self.counters,
                cfg=self.cfg,
                ids=self._ids,
                project_id=self.project_id,
                events=self.events,
            )
        return self._generator

    @property
    def reviewer(self) -> Any:
        if self._reviewer is None:
            from bettercomfyui.director.review import SegmentReviewer  # noqa: PLC0415

            self._reviewer = SegmentReviewer(
                self.counters,
                cfg=self.cfg,
                ids=self._ids,
                scorer=self._scorer,
                edge_scorer=self._edge_scorer,
                events=self.events,
                project_id=self.project_id,
            )
        return self._reviewer

    # -- 配置 -------------------------------------------------------------
    @property
    def ids(self) -> IdFactory:
        if self._ids is None:
            self._ids = default_ids()
        return self._ids

    @property
    def max_cascade_depth(self) -> int:
        if self._max_depth_override is not None:
            return int(self._max_depth_override)
        return int(cfg_get(self.cfg, "video_review.max_cascade_depth", 8))

    @property
    def depth_policy(self) -> str:
        if self._policy_override is not None:
            policy = str(self._policy_override)
        else:
            policy = str(
                cfg_get(self.cfg, "video_review.cascade_depth_policy", "DEGRADE_OUTPUT")
                or "DEGRADE_OUTPUT"
            )
        if policy not in CASCADE_POLICIES:
            raise CascadePolicyError(
                f"级联超限策略非法：{policy!r}；必须是 {sorted(CASCADE_POLICIES)} 之一"
            )
        return policy

    @property
    def cnt3_limit(self) -> int:
        return int(cfg_get(self.cfg, "video_review.cnt3_limit", 3))

    @property
    def cascade_mark_enabled(self) -> bool:
        return bool(cfg_get(self.cfg, "video_review.cascade_mark_enabled", True))

    # -- 注册表 -----------------------------------------------------------
    def set_segments(self, segments: Iterable[Segment]) -> None:
        for seg in segments:
            self.register(seg)

    def register(self, segment: Segment) -> Segment:
        segment = ensure_segment(segment, where="CascadeController.register")
        self._registry[segment.segment_id] = segment
        if segment.cascade_marked or segment.status is SegmentStatus.NEEDS_CASCADE_CHECK:
            self._pending.add(segment.segment_id)
        return segment

    def get(self, segment_id: str) -> Segment | None:
        return self._registry.get(segment_id)

    def downstream_edges(self, segment: Segment) -> list[dict]:
        """扫描注册表，找出所有依赖 `segment` 的后续段（CASCADE_CHECK 的输入）。

        返回的每条边的 `segment_id` = **受影响的下游段**（dependent），
        `upstream_id` = 发生变化的上游段；`kind/ref_facet/ref_version` 描述依赖内容。
        """
        edges: list[dict] = []
        for other in self._registry.values():
            if other.segment_id == segment.segment_id:
                continue
            for raw in other.dependency_refs or []:
                edge = parse_dependency_ref(raw)
                if edge["segment_id"] == segment.segment_id:
                    edges.append(_edge_record(other.segment_id, segment.segment_id, edge))
        return edges

    def pending_verifications(self) -> list[str]:
        """仍待衔接验证的段（Y2 标记未清除）。"""
        return sorted(
            sid
            for sid, seg in self._registry.items()
            if sid in self._pending or seg.status is SegmentStatus.NEEDS_CASCADE_CHECK
        )

    def all_segments_done(self) -> bool:
        """ALL_SEG_DONE：没有待验证标记。"""
        return not self.pending_verifications()

    # -- CASCADE_CHECK ----------------------------------------------------
    def on_segment_changed(
        self,
        segment: Segment,
        *,
        depth: int = 0,
        downstream: Iterable[Any] | None = None,
    ) -> dict:
        """该段变化是否影响后续段依赖？只有确实失效才返回 affected。"""
        segment = ensure_segment(segment, where="CascadeController.on_segment_changed")
        edges = _dedupe_edges(self._collect_edges(segment, downstream))
        affected: list[dict] = []
        skipped: list[dict] = []
        for edge in edges:
            invalidated, reason = self._is_invalidated(edge, segment)
            record = {**edge, "reason": reason}
            (affected if invalidated else skipped).append(record)

        max_depth = self.max_cascade_depth
        depth_exceeded = (depth + 1) > max_depth
        if not self.cascade_mark_enabled:
            result = {
                "segment_id": segment.segment_id,
                "version": segment.current_version,
                "depth": depth,
                "max_cascade_depth": max_depth,
                "depth_exceeded": False,
                "affected": [],
                "skipped": skipped,
                "blocked": [],
                "cascade_id": None,
                "policy": None,
                "degrade_flag": None,
                "deliverable": True,
                "human_required": False,
                "disabled": "cascade_mark_disabled",
            }
            self.events.emit(
                "CASCADE_CHECK",
                stage="CASCADE",
                object_id=segment.segment_id,
                version=segment.current_version,
                detail={"affected": 0, "skipped": len(skipped), "disabled": True},
            )
            return result

        if depth_exceeded:
            policy = self.depth_policy
            blocked = [a["segment_id"] for a in affected]
            result = {
                "segment_id": segment.segment_id,
                "version": segment.current_version,
                "depth": depth,
                "max_cascade_depth": max_depth,
                "depth_exceeded": True,
                "affected": [],
                "skipped": skipped,
                "blocked": blocked,
                "blocked_edges": affected,
                "cascade_id": None,
                "policy": policy,
                "degrade_flag": DegradeFlag.CASCADE_DEGRADED,
                "deliverable": policy == "DEGRADE_OUTPUT",
                "human_required": policy == "REQUEST_HUMAN",
            }
            self.events.emit(
                "CASCADE_DEPTH_EXCEEDED",
                stage="CASCADE",
                object_id=segment.segment_id,
                version=segment.current_version,
                severity="WARN",
                detail={
                    "depth": depth,
                    "max_cascade_depth": max_depth,
                    "policy": policy,
                    "blocked": blocked,
                    "degrade_flag": str(DegradeFlag.CASCADE_DEGRADED),
                },
            )
            return result

        cascade_id = self.ids.new_cascade_id(segment.segment_id) if affected else None
        self.events.emit(
            "CASCADE_CHECK",
            stage="CASCADE",
            object_id=segment.segment_id,
            version=segment.current_version,
            detail={
                "depth": depth,
                "affected": [a["segment_id"] for a in affected],
                "skipped": [{"segment_id": s["segment_id"], "reason": s["reason"]} for s in skipped],
                "cascade_id": cascade_id,
            },
        )
        return {
            "segment_id": segment.segment_id,
            "version": segment.current_version,
            "depth": depth,
            "max_cascade_depth": max_depth,
            "depth_exceeded": False,
            "affected": affected,
            "skipped": skipped,
            "blocked": [],
            "cascade_id": cascade_id,
            "policy": None,
            "degrade_flag": None,
            "deliverable": True,
            "human_required": False,
        }

    # -- Y2 ---------------------------------------------------------------
    def mark_next(self, segment: Segment, *, cascade_id: str | None = None) -> dict:
        """Y2：标记下一受影响段待验证；为该段建立**独立 CNT3**。

        注意：建立计数器作用域**不等于**计数；CNT3 只在真正重生成成功后 +1。
        """
        if not isinstance(segment, Segment):
            segment = ensure_segment(segment, where="CascadeController.mark_next")
        seg_id = segment.segment_id
        cnt_fn = getattr(self.ids, "cnt_id", None)
        scope = (
            cnt_fn(seg_id, "CNT3")
            if callable(cnt_fn)
            else f"CNT_{seg_id}_CNT3"  # 契约 §2 的格式兜底（store.ids 缺失时）
        )
        already = seg_id in self._pending or segment.status is SegmentStatus.NEEDS_CASCADE_CHECK
        segment.cascade_marked = True
        segment.status = SegmentStatus.NEEDS_CASCADE_CHECK
        segment.cascade_id = cascade_id or segment.cascade_id or scope
        segment.updated_at = now_iso()
        self._registry[seg_id] = segment
        self._pending.add(seg_id)
        result = {
            "segment_id": seg_id,
            "created": not already,
            "reason": "already_marked" if already else "marked",
            "status": SegmentStatus.NEEDS_CASCADE_CHECK,
            "cnt3_scope": scope,
            "cascade_id": segment.cascade_id,
            "cnt2": int(self.counters.get(seg_id, CounterKind.CNT2)),
            "cnt3": int(self.counters.get(seg_id, CounterKind.CNT3)),
            "cnt3_incremented": False,
            "segment": segment,
        }
        if not already:
            self.events.emit(
                "Y2_MARK_NEXT",
                stage="CASCADE",
                object_id=seg_id,
                version=segment.current_version,
                detail={"cnt3_scope": scope, "cascade_id": segment.cascade_id},
            )
        return result

    # -- N1 ---------------------------------------------------------------
    def run(self, segment: Segment, *, executor: Any) -> dict:
        """N1：级联段重生成。CNT3 + 1（与 CNT2 完全独立），循环次数受 CNT3 上限硬约束。"""
        segment = ensure_segment(segment, where="CascadeController.run")
        seg_id = segment.segment_id
        upstreams = self._upstreams_of(segment)
        if not upstreams:
            raise CascadeDependencyMissing(
                f"级联段 {seg_id} 找不到上游依赖，无法执行 N1_EDGE 边界验证"
            )
        limit = self.cnt3_limit
        current = segment
        iterations = 0
        while int(self.counters.get(seg_id, CounterKind.CNT3)) < limit:
            iterations += 1
            gen = self.generator.generate(
                current, executor=executor, source=SegmentSource.REGENERATED
            )
            if gen.get("technical_failure"):
                self.events.emit(
                    "N1_TECH_FAILURE",
                    stage="CASCADE",
                    object_id=seg_id,
                    severity="WARN",
                    detail={
                        "tech_retry": gen.get("tech_retry"),
                        "error_class": str(gen.get("error_class")),
                        "note": "技术失败不加 CNT3",
                    },
                )
                return {
                    "outcome": "TECH_FAILED",
                    "status": SegmentStatus.FAILED,
                    "segment_id": seg_id,
                    "iterations": iterations,
                    "cnt2": int(self.counters.get(seg_id, CounterKind.CNT2)),
                    "cnt3": int(self.counters.get(seg_id, CounterKind.CNT3)),
                    "tech_retry": gen.get("tech_retry"),
                    "error_class": gen.get("error_class"),
                    "error": gen.get("error"),
                    "generation": gen,
                    "degrade_flag": DegradeFlag.NONE,
                    "deliverable": False,  # 级联段未验证通过，不得声称可交付
                }
            # 重生成成功产出新版本 -> CNT3 + 1
            cnt3_after = int(self.counters.increment(seg_id, CounterKind.CNT3))
            new_segment = gen["segment"]
            current = new_segment
            review = self.reviewer.review(new_segment, gen["version"])
            reviewed = review.get("segment", new_segment)
            if not review.get("passed"):
                # 级联段自身 R_seg 未通过 -> 再次 N1（受 CNT3 上限约束）
                current = reviewed
                self._registry[seg_id] = reviewed
                continue
            edge_results = [
                self.reviewer.verify_edge(up, reviewed) for up in self._upstreams_of(reviewed)
            ]
            if all(e.get("passed") for e in edge_results):
                # N1_CLEAN：更新段级审核记录；清除本次待验证标记
                reviewed.status = SegmentStatus.PASSED
                reviewed.cascade_marked = False
                reviewed.updated_at = now_iso()
                self._pending.discard(seg_id)
                self._registry[seg_id] = reviewed
                self._record_edge_facets(reviewed)
                self.events.emit(
                    "N1_CLEAN",
                    stage="CASCADE",
                    object_id=seg_id,
                    version=gen["version"],
                    detail={"cnt3": cnt3_after, "edges": [e.get("edge_id") for e in edge_results]},
                )
                return {
                    "outcome": "N1_CLEAN",
                    "status": SegmentStatus.PASSED,
                    "segment_id": seg_id,
                    "version": gen["version"],
                    "iterations": iterations,
                    "cnt2": int(self.counters.get(seg_id, CounterKind.CNT2)),
                    "cnt3": cnt3_after,
                    "review": review,
                    "edges": edge_results,
                    "generation": gen,
                    "segment": reviewed,
                    "degrade_flag": DegradeFlag.NONE,
                    "deliverable": True,
                }
            current = reviewed
            self._registry[seg_id] = reviewed
            self.events.emit(
                "N1_EDGE_FAILED",
                stage="CASCADE",
                object_id=seg_id,
                version=gen["version"],
                severity="WARN",
                detail={"cnt3": cnt3_after, "edges": edge_results},
            )
        # N1_FALLBACK：达到 CNT3 上限仍未通过 -> 取最高分 + CASCADE_FALLBACK + CASCADE_DEGRADED
        return self._fallback(current, reason="cnt3_limit", iterations=iterations)

    # -- 传播（CASCADE_NEXT -> ALL_SEG_DONE） --------------------------------
    def propagate(
        self,
        root: Segment,
        *,
        executor: Any = None,
        max_depth: int | None = None,
        policy: str | None = None,
    ) -> dict:
        """从 root 出发做有界级联传播。返回终止原因、触及段与降级标记。"""
        depth_limit = int(max_depth) if max_depth is not None else self.max_cascade_depth
        if depth_limit < 0:
            raise CascadePolicyError(f"max_cascade_depth 不得为负：{depth_limit}")
        active_policy = str(policy) if policy is not None else self.depth_policy
        if active_policy not in CASCADE_POLICIES:
            raise CascadePolicyError(
                f"级联超限策略非法：{active_policy!r}；必须是 {sorted(CASCADE_POLICIES)} 之一"
            )
        self.register(root)
        guard = self._iteration_guard(depth_limit)
        frontier: deque[tuple[str, int]] = deque([(root.segment_id, 0)])
        marked: list[str] = []
        regenerated: list[str] = []
        degraded: list[str] = []
        iterations = 0
        stopped_reason = "all_done"
        deliverable = True
        human_required = False
        while frontier:
            node_id, depth = frontier.popleft()
            if iterations >= guard:
                stopped_reason = "iteration_guard"
                deliverable = False
                self.events.emit(
                    "CASCADE_ITERATION_GUARD",
                    stage="CASCADE",
                    object_id=node_id,
                    severity="ERROR",
                    detail={"guard": guard, "note": "硬上界触发，停止级联（防无限循环）"},
                )
                break
            iterations += 1
            changed = self._registry.get(node_id)
            if changed is None:
                continue
            check = self.on_segment_changed(changed, depth=depth)
            if check.get("depth_exceeded"):
                stopped_reason = "max_cascade_depth"
                if active_policy != "DEGRADE_OUTPUT":
                    deliverable = False
                if active_policy == "REQUEST_HUMAN":
                    human_required = True
                for sid in check.get("blocked", []):
                    self._mark_degraded(sid, reason="max_cascade_depth")
                    degraded.append(sid)
                break
            if depth + 1 > depth_limit:
                # 双保险：即使 CASCADE_CHECK 未触发，也绝不再向下一层传播
                stopped_reason = "max_cascade_depth"
                break
            for edge in check.get("affected", []):
                target = self._registry.get(edge["segment_id"])
                if target is None:
                    continue
                mark = self.mark_next(target, cascade_id=check.get("cascade_id"))
                if mark["created"]:
                    marked.append(target.segment_id)
                if executor is None:
                    # 无执行器 = 无法完成 N1：按策略阻止交付并留降级证据（不静默、不假装完成）
                    degraded.append(target.segment_id)
                    self._mark_degraded(target.segment_id, reason="缺少 executor，无法执行 N1")
                    stopped_reason = "cascade_unavailable"
                    deliverable = False
                    frontier.clear()
                    break
                try:
                    run = self.run(target, executor=executor)
                except (DirectorError, ExecutorError) as exc:
                    blocked_reason = f"{type(exc).__name__}: {exc}"
                    if target.segment_id not in degraded:
                        degraded.append(target.segment_id)
                    self._mark_degraded(target.segment_id, reason=blocked_reason)
                    self.events.emit(
                        "CASCADE_UNAVAILABLE",
                        stage="CASCADE",
                        object_id=target.segment_id,
                        severity="ERROR",
                        detail={"error": blocked_reason, "note": "级联无法完成，阻止交付并留降级标记"},
                    )
                    stopped_reason = "cascade_unavailable"
                    deliverable = False
                    frontier.clear()
                    break
                if run.get("segment") is not None:
                    self._registry[target.segment_id] = run["segment"]
                regenerated.append(target.segment_id)
                if run.get("outcome") in ("N1_FALLBACK", "N1_FALLBACK_NO_CANDIDATE"):
                    if target.segment_id not in degraded:
                        degraded.append(target.segment_id)
                    if run.get("deliverable") is False:
                        deliverable = False
                if run.get("outcome") == "TECH_FAILED":
                    stopped_reason = "technical_failure"
                    deliverable = False
                    frontier.clear()
                    break
                frontier.append((target.segment_id, depth + 1))
        pending = self.pending_verifications()
        if stopped_reason == "all_done" and pending:
            stopped_reason = "pending_verification"
        degrade_flag = DegradeFlag.CASCADE_DEGRADED if degraded else DegradeFlag.NONE
        result = {
            "root": root.segment_id,
            "iterations": iterations,
            "iteration_guard": guard,
            "stopped_reason": stopped_reason,
            "max_cascade_depth": depth_limit,
            "policy": active_policy,
            "segments_marked": marked,
            "segments_regenerated": regenerated,
            "segments_degraded": degraded,
            "degrade_flag": degrade_flag,
            "deliverable": bool(deliverable),
            "human_required": bool(human_required),
            "pending": pending,
            "all_segments_done": self.all_segments_done(),
            "full_review_calls": self._full_review_calls(),
        }
        self.events.emit(
            "CASCADE_DONE" if result["all_segments_done"] else "CASCADE_PENDING",
            stage="CASCADE",
            object_id=root.segment_id,
            severity="INFO" if result["deliverable"] else "WARN",
            detail={
                "stopped_reason": stopped_reason,
                "iterations": iterations,
                "degraded": degraded,
                "deliverable": deliverable,
                "policy": active_policy,
            },
        )
        return result

    # -- 内部 -------------------------------------------------------------
    def _iteration_guard(self, depth_limit: int) -> int:
        """硬迭代上界：depth 上界 × 段数 × 2 + 4，永远有限。"""
        n = max(1, len(self._registry))
        return (depth_limit + 1) * n * 2 + 4

    def _full_review_calls(self) -> int:
        return int(getattr(self.reviewer, "full_review_calls", 0) or 0)

    def _collect_edges(self, segment: Segment, downstream: Iterable[Any] | None) -> list[dict]:
        if downstream is None:
            return self.downstream_edges(segment)
        edges: list[dict] = []
        for item in downstream:
            if isinstance(item, Mapping) and (
                "segment_index" in item or "current_version" in item or "chapter_id" in item
            ):
                item = ensure_segment(item, where="CascadeController._collect_edges")
            if isinstance(item, Segment):
                for raw in item.dependency_refs or []:
                    edge = parse_dependency_ref(raw)
                    if edge["segment_id"] == segment.segment_id:
                        edges.append(_edge_record(item.segment_id, segment.segment_id, edge))
            else:
                # 显式传入的边：`segment_id` 指受影响的**下游段**
                parsed = parse_dependency_ref(item)
                edges.append(_edge_record(parsed["segment_id"], segment.segment_id, parsed))
        return edges

    def _is_invalidated(self, edge: dict, changed: Segment) -> tuple[bool, str]:
        kind = edge["kind"]
        current = facet_value(changed, kind)
        ref_facet = edge.get("ref_facet")
        if ref_facet is not None and current is not None:
            if current != ref_facet:
                return True, "facet_changed"
            return False, "facet_unchanged"
        ref_version = edge.get("ref_version")
        if ref_version is not None and kind in CONTENT_DEPENDENCY_KINDS:
            if changed.current_version != ref_version:
                return True, "version_changed_no_facet"
            return False, "version_unchanged"
        return False, "no_evidence"

    def _upstreams_of(self, segment: Segment) -> list[Segment]:
        out: list[Segment] = []
        for raw in segment.dependency_refs or []:
            edge = parse_dependency_ref(raw)
            up = self._registry.get(edge["segment_id"])
            if up is not None and up not in out:
                out.append(up)
        return out

    def _record_edge_facets(self, segment: Segment) -> None:
        """边界验证通过后，把上游当前指纹写回依赖边（作为下次失效判定的证据）。"""
        new_refs: list[str] = []
        for raw in segment.dependency_refs or []:
            edge = parse_dependency_ref(raw)
            upstream = self._registry.get(edge["segment_id"])
            if upstream is None:
                new_refs.append(raw)
                continue
            facet = facet_value(upstream, edge["kind"])
            new_refs.append(
                "|".join(
                    [
                        edge["segment_id"],
                        edge["kind"],
                        facet if facet is not None else "-",
                        upstream.current_version,
                    ]
                )
            )
        segment.dependency_refs = new_refs

    def _mark_degraded(self, segment_id: str, *, reason: str) -> None:
        seg = self._registry.get(segment_id)
        if seg is None:
            return
        if DegradeFlag.CASCADE_DEGRADED not in seg.degrade_flags:
            seg.degrade_flags = list(seg.degrade_flags) + [DegradeFlag.CASCADE_DEGRADED]
        seg.status = SegmentStatus.CASCADE_FAILED
        seg.updated_at = now_iso()
        self.events.emit(
            "DEGRADED_EDGE",
            stage="CASCADE",
            object_id=segment_id,
            version=seg.current_version,
            severity="WARN",
            detail={"reason": reason, "degrade_flag": str(DegradeFlag.CASCADE_DEGRADED)},
        )

    def _fallback(self, segment: Segment, *, reason: str, iterations: int) -> dict:
        """N1_FALLBACK：从候选里取最高分；无候选则**不伪造**，直接判不可交付。"""
        best = select_highest_score_candidate(segment)
        degraded = copy.deepcopy(segment)
        if DegradeFlag.CASCADE_DEGRADED not in degraded.degrade_flags:
            degraded.degrade_flags = list(degraded.degrade_flags) + [DegradeFlag.CASCADE_DEGRADED]
        self._pending.discard(segment.segment_id)
        degraded.cascade_marked = False
        degraded.updated_at = now_iso()
        if best is None:
            degraded.status = SegmentStatus.CASCADE_FAILED
            degraded.source = SegmentSource.CASCADE_FALLBACK
            self._registry[segment.segment_id] = degraded
            self.events.emit(
                "N1_FALLBACK_NO_CANDIDATE",
                stage="CASCADE",
                object_id=segment.segment_id,
                severity="ERROR",
                detail={"reason": reason, "note": "没有带评分的候选，禁止伪造兜底选择"},
            )
            return {
                "outcome": "N1_FALLBACK_NO_CANDIDATE",
                "status": SegmentStatus.CASCADE_FAILED,
                "segment_id": segment.segment_id,
                "version": segment.current_version,
                "iterations": iterations,
                "selected": None,
                "source": SegmentSource.CASCADE_FALLBACK,
                "degrade_flag": DegradeFlag.CASCADE_DEGRADED,
                "deliverable": False,
                "cnt2": int(self.counters.get(segment.segment_id, CounterKind.CNT2)),
                "cnt3": int(self.counters.get(segment.segment_id, CounterKind.CNT3)),
                "segment": degraded,
            }
        degraded.status = SegmentStatus.FALLBACK_SELECTED
        degraded.source = SegmentSource.CASCADE_FALLBACK
        degraded.candidate_version = best["version"]
        self._registry[segment.segment_id] = degraded
        self.events.emit(
            "N1_FALLBACK",
            stage="CASCADE",
            object_id=segment.segment_id,
            version=best["version"],
            severity="WARN",
            detail={
                "reason": reason,
                "selected": best,
                "degrade_flag": str(DegradeFlag.CASCADE_DEGRADED),
                "source": str(SegmentSource.CASCADE_FALLBACK),
            },
        )
        return {
            "outcome": "N1_FALLBACK",
            "status": SegmentStatus.FALLBACK_SELECTED,
            "segment_id": segment.segment_id,
            "version": best["version"],
            "iterations": iterations,
            "selected": best,
            "source": SegmentSource.CASCADE_FALLBACK,
            "degrade_flag": DegradeFlag.CASCADE_DEGRADED,
            "deliverable": True,  # 方案二·8：允许按项目输出策略进入最终汇总
            "cnt2": int(self.counters.get(segment.segment_id, CounterKind.CNT2)),
            "cnt3": int(self.counters.get(segment.segment_id, CounterKind.CNT3)),
            "segment": degraded,
        }
