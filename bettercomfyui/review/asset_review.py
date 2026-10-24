"""素材版本审核（方案第七部分·一 第 1-10 条 + 参数表三 + CONTRACTS §6）。

口径要点（每条都有针对性测试）：
1. **状态机不可绕过**：所有状态变化都经 `ReviewCycle.fire()`（`REVIEW_TRANSITIONS`）。
   人工周期的合法起点是「初始状态 = AWAITING_REVIEW」（`REVIEW_TRANSITIONS` 无入边）；
   人工模式下确定性校验失败走 `RECEIVED → start_review → VALIDATING → validation_failed → FAILED`。
2. **CNT1 只在模型模式生效**：人工审核次数**不计入** CNT1；人工超时切换到模型模式时
   `CNT1` 从 0 开始（唯一合法 reset）；`每次实际执行模型审核前计数一次`；**版本号递增绝不清零**。
3. **超时暂停**：只有「持续进行有效审核操作」才顺延超时（`VALID_HUMAN_OP_KINDS`）；
   界面打开/心跳/闲置（`NON_ACTIVITY_KINDS`）与未知操作类型**都不算有效操作**——
   界面开着但持续闲置必须仍能超时。
4. **统一后端时间戳**：人工与模型结果都写 `ReviewAttempt.backend_ts = 服务端 now_iso()`；
   `client_ts` 只写事件详情并显式标记 `client_ts_used=False`，绝不参与判定与仲裁。
5. **H_arb**：见 `arbiter.arbitrate`（人工 ts < 模型 ts → 人工胜出 + ABORT_SIGNAL；
   ≥ → 模型胜出，人工仅写日志；**平局模型胜出**）。
6. **唯一提交点**：`commit.commit_final`（`finalization_token` 条件更新）；
   迟到结果只归档为 ABORTED，绝不改变当前版本状态。

FALLBACK 口径（Lead 裁决）：`ReviewRecord.version = 被选中的最高分候选版本`，
`record.content_hash` 必须等于该版本的真实指纹，`lock_id == LOCK_{asset_id}_{fallback_version}`；
跨版本兜底时「接受结论」提交到**被选中版本的那个 review_cycle_id** 上，
兜底决策则在达到上限的那个周期上按真实边 `CLASSIFYING → FALLBACK` 落地。
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..models.entities import (
    LOCAL_TZ,
    AssetReviewParams,
    Config,
    ReviewAttempt,
    ReviewRecord,
    now_iso,
)
from ..models.enums import (
    CounterKind,
    ErrorClass,
    LockSource,
    ReviewMode,
    ReviewState,
    Stage,
)
from . import _bridge as bridge
from .arbiter import arbitrate
from .commit import archive_late_result, commit_final, get_ledger
from .config import resolve_review_params, timeout_seconds
from .counters import CounterStore
from .errors import (
    CandidateLimitReached,
    IllegalTransition,
    ModelRoundAlreadyOpen,
    ReviewError,
    ReviewTechnicalError,
)
from .events import ReviewEventLog
from .notify import NotificationService
from .state import ReviewCycle

_LOG = logging.getLogger(__name__)

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: 有效审核操作：只有这些操作才算「持续进行有效审核」并顺延超时
VALID_HUMAN_OP_KINDS = frozenset(
    {
        "view_frame",
        "inspect",
        "zoom",
        "annotate",
        "draw_annotation",
        "score_input",
        "score_dimension_input",
        "comment",
        "compare_candidates",
        "playback",
        "select_direction",
        "prompt_edit",
        "draft_save",
        "reject_reason_input",
        "pass_confirm",
        "candidate_switch",
    }
)

#: 已知的「不算有效审核操作」事件：界面打开/心跳/闲置
NON_ACTIVITY_KINDS = frozenset(
    {
        "ui_open",
        "window_open",
        "page_open",
        "panel_open",
        "heartbeat",
        "ui_heartbeat",
        "idle",
        "focus",
        "blur",
        "mouse_move_idle",
        "keepalive",
    }
)


def _to_dt(value: Any, label: str = "时间戳") -> datetime:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ReviewError(f"{label} 不是合法 ISO8601：{value!r}") from exc
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=LOCAL_TZ)


class AssetReviewer:
    """素材版本审核器：人工/模型双模式、超时切换、CNT1、仲裁与唯一提交。"""

    def __init__(
        self,
        store: Any = None,
        *,
        config: Config | AssetReviewParams | dict | None = None,
        model_review: Callable[[dict], dict] | None = None,
        notifier: NotificationService | None = None,
        event_log: Any = None,
        counters: CounterStore | None = None,
        clock: Callable[[], str] | None = None,
        project: Any = None,
        project_id: str | None = None,
        ledger: Any = None,
        ledger_path: Any = None,
        downstream: Callable[[dict], Any] | None = None,
        max_notify_retries: int | None = None,
        auto_commit: bool = True,
    ) -> None:
        self._cfg = resolve_review_params(config)
        self.store = store
        self._clock = clock or now_iso
        self._model_review = model_review
        self._downstream = downstream
        #: True（默认）：结论一到就调用 `commit.commit_final` 完成唯一提交（Lead 的 demo 与
        #: pipeline 依赖该行为：审核通过即写 LOCK）。
        #: False：只把周期推进到结论态并返回**可直接喂给 commit_final 的 cycle dict**
        #: （见 `_result`），由调用方负责唯一提交 —— 验证员/上层可显式选择这条路径。
        self.auto_commit = bool(auto_commit)
        # 图一 SG7 Z5：「日志记录所有手动操作和自动切换」。
        # 开关取自 global.log_all_manual_and_auto_switch（默认 True）。
        # 这里存一份布尔，让下面每个切换点都能便宜地判断要不要记。
        _global_params = getattr(config, "global_", None)
        self.log_switches = bool(
            getattr(_global_params, "log_all_manual_and_auto_switch", True)
        )
        self.events = event_log if event_log is not None else ReviewEventLog(
            project=project, project_id=project_id, clock=self._clock
        )
        self._ledger = ledger
        self._ledger_path = ledger_path if ledger_path is not None else self._derive_ledger_path(store)
        self.counters = counters if counters is not None else CounterStore(
            event_log=self.events, clock=self._clock
        )
        if notifier is not None:
            self.notifier = notifier
        else:
            retries = max_notify_retries
            if retries is None:
                global_params = getattr(config, "global_", None)
                retries = int(getattr(global_params, "max_notify_retries", 0) or 0)
            self.notifier = NotificationService(
                params=self._cfg, event_log=self.events, max_retries=retries
            )
        self._records: dict[str, ReviewRecord] = {}
        self._cycles: dict[str, ReviewCycle] = {}
        self._superseded: dict[str, list[ReviewRecord]] = {}
        self._human_windows: dict[str, dict] = {}
        self._candidates: dict[str, list[dict]] = {}
        self._concluded: dict[str, str] = {}
        self._round_open: set[str] = set()
        self._attempt_seq: dict[str, int] = {}
        self._commit_results: dict[str, dict] = {}
        self._pending_commit: dict[str, dict] = {}
        self._fallback_commit: dict[str, dict] = {}
        self._fallback_version: dict[str, str] = {}
        self._fallback_score: dict[str, float] = {}
        self._late: dict[str, list[dict]] = {}
        self._reg_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 只读访问
    # ------------------------------------------------------------------
    @property
    def config(self) -> AssetReviewParams:
        return self._cfg

    def records(self) -> list[ReviewRecord]:
        with self._reg_lock:
            return list(self._records.values())

    def get_record(self, cycle_id: str) -> ReviewRecord:
        return self._require(cycle_id)[0]

    def get_cycle(self, cycle_id: str) -> ReviewCycle:
        return self._require(cycle_id)[1]

    def record_dict(self, cycle_id: str) -> dict:
        record, _ = self._require(cycle_id)
        return record.to_dict()

    def commit_result(self, cycle_id: str) -> dict | None:
        return self._commit_results.get(cycle_id)

    def late_results(self, cycle_id: str) -> list[dict]:
        return list(self._late.get(cycle_id, []))

    def candidates(self, obj_id: str) -> list[dict]:
        return [dict(c) for c in self._candidates.get(obj_id, [])]

    # ------------------------------------------------------------------
    # 主流程：素材版本审核
    # ------------------------------------------------------------------
    def review_version(
        self,
        asset_id: str,
        version: str,
        *,
        mode: ReviewMode,
        material: dict,
    ) -> dict:
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("asset_id 必须是非空字符串")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("version 必须是非空字符串（v{int}）")
        if not isinstance(material, dict):
            raise TypeError(f"material 必须是 dict，实际：{type(material).__name__}")
        try:
            mode = ReviewMode.parse(mode)
        except ValueError as exc:
            raise ReviewError(f"mode 非法：{exc}") from exc
        if mode is None:
            raise ReviewError("mode 不能为 None")

        cycle_id = bridge.review_cycle_id("ASSET", asset_id, version)
        existing = self._lookup(cycle_id)
        record: ReviewRecord | None = None
        cycle: ReviewCycle | None = None
        recovered = False
        if existing is not None:
            record, cycle = existing
            if self._concluded.get(cycle_id):
                return self._result(record, cycle, extra={"reused": True, "already_finalized": True})
            if cycle.state is ReviewState.FAILED:
                # FAILED 是终态，但方案允许 technical_recovery_resubmit 恢复：
                # 必须**先**判 FAILED 再判终态，否则恢复路径永远不可达。
                if mode is ReviewMode.HUMAN:
                    self._retire(cycle_id, record, cycle,
                                 reason="FAILED 恢复：人工周期以初始状态 AWAITING_REVIEW 重开")
                    record = cycle = None
                else:
                    self._fire(cycle, record, "technical_recovery_resubmit",
                               reason="技术恢复：使用原任务恢复信息重新提交")
                    record.reason = ""
                    record.score = None
                    record.lock_source = None
                    recovered = True
            elif cycle.state.is_terminal:
                return self._result(record, cycle, extra={"reused": True, "already_finalized": True})
            elif cycle.state is ReviewState.SCANNING:
                # 人工超时切换模型后周期停在 SCANNING（切换只改模式，不替调用方决定评分）。
                # 审计 R1 修复：此前这里一律 reused，导致「切换后再次调用 review_version」
                # 永远返回 SCANNING 而不执行模型轮 —— H4→H2 链路在产品里断了。
                # 现在：模型模式下对 SCANNING 周期真正执行一轮模型审核（跨进程恢复后同样适用）。
                if mode in (ReviewMode.MODEL, ReviewMode.AUTO_SWITCHED):
                    self._guard_candidate_limit(record.target_id)
                    self._execute_model_round(record, cycle, material)
                    return self._result(record, cycle, extra={"reused": True, "resumed": True})
                return self._result(record, cycle, extra={"reused": True})
            else:
                # 同一版本只允许一个正式审核周期：幂等返回，绝不重复建周期/重复提交
                return self._result(record, cycle, extra={"reused": True})

        errors = self._validate_material(material)

        # 图一 SG7 Z5：记录「人工操作」与「自动切换」。
        # 这一步在开周期之前 —— 即使后面因材料无效而失败，这次操作也已经被记下来了。
        if self.log_switches:
            _mode = getattr(mode, "value", str(mode))
            if mode is ReviewMode.HUMAN:
                _LOG.info("[MANUAL_REVIEW] %s@%s mode=%s", asset_id, version, _mode)
            elif mode is ReviewMode.AUTO_SWITCHED:
                _LOG.info(
                    "[AUTO_SWITCH] %s@%s 人工超时 -> 自动切换为模型审核（CNT1 从 0 起算）",
                    asset_id, version,
                )
            else:
                _LOG.info("[AUTO_REVIEW] %s@%s mode=%s", asset_id, version, _mode)

        if record is None or cycle is None:
            if mode in (ReviewMode.MODEL, ReviewMode.AUTO_SWITCHED):
                self._guard_candidate_limit(asset_id)
            record, cycle = self._open_cycle(asset_id, version, mode, material, errors)
        elif recovered:
            # FAILED -> RECEIVED 之后必须走真实边重新进入：start_review -> VALIDATING -> ...
            self._guard_candidate_limit(asset_id)
            self._fire(cycle, record, "start_review", reason="技术恢复后重新进入校验")
            if errors:
                self._fire(cycle, record, "validation_failed", errors=errors)
                self._fail_validation(record, cycle, errors)
            else:
                self._fire(cycle, record, "validation_passed")
                self._execute_model_round(record, cycle, material)
        elif errors:  # pragma: no cover - 防御（正常路径已在上面分支处理）
            self._fire(cycle, record, "validation_failed", errors=errors)
            self._fail_validation(record, cycle, errors)
        else:  # pragma: no cover - 防御
            self._fire(cycle, record, "validation_passed")
            self._execute_model_round(record, cycle, material)
        return self._result(record, cycle)

    def _open_cycle(
        self,
        asset_id: str,
        version: str,
        mode: ReviewMode,
        material: dict,
        errors: list[dict],
    ) -> tuple[ReviewRecord, ReviewCycle]:
        cycle_id = bridge.review_cycle_id("ASSET", asset_id, version)
        human_ok = mode in (ReviewMode.HUMAN,) and not errors
        initial = ReviewState.AWAITING_REVIEW if human_ok else ReviewState.RECEIVED
        record = ReviewRecord(
            review_cycle_id=cycle_id,
            mod_id=bridge.mod_id("ASSET", asset_id),
            review_state=initial,
            mode=mode,
            target_kind="asset",
            target_id=asset_id,
            version=version,
            content_hash=self._sanitize_hash(material),
            cnt_id=bridge.cnt_id(asset_id, CounterKind.CNT1),
            finalization_token=bridge.new_finalization_token(),
        )
        cycle = ReviewCycle(
            cycle_id,
            state=initial,
            object_type="ASSET",
            object_id=asset_id,
            version=version,
            mode=mode,
            review_record_id=record.mod_id,
            clock=self._clock,
            event_log=self.events,
        )
        with self._reg_lock:
            self._records[cycle_id] = record
            self._cycles[cycle_id] = cycle
        self._window(cycle_id)
        self._emit(
            "review_cycle_opened",
            object_id=asset_id,
            version=version,
            mode=mode,
            detail={
                "review_cycle_id": cycle_id,
                "initial_state": initial.value,
                "mod_id": record.mod_id,
                "finalization_token": record.finalization_token,
                "note": (
                    "人工周期以初始状态 AWAITING_REVIEW 建立（REVIEW_TRANSITIONS 无 target=AWAITING_REVIEW 的边）"
                    if human_ok
                    else "模型周期以 RECEIVED 建立，随后走 REVIEW_TRANSITIONS 的真实边"
                ),
            },
        )
        if not human_ok and not errors:
            self._fire(cycle, record, "start_review")
            self._fire(cycle, record, "validation_passed")
            self._execute_model_round(record, cycle, material)
        elif human_ok:
            self._emit(
                "deterministic_validation_passed",
                object_id=asset_id,
                version=version,
                mode=mode,
                detail={"errors": [], "note": "人工模式：确定性校验通过后等待人工审核（不经过 SCANNING/CLASSIFYING）"},
            )
        else:
            self._fire(cycle, record, "start_review")
            self._fire(cycle, record, "validation_failed", errors=errors)
            self._fail_validation(record, cycle, errors)
        return record, cycle

    def _fail_validation(self, record: ReviewRecord, cycle: ReviewCycle, errors: list[dict]) -> None:
        record.reason = f"确定性校验失败：{errors}"
        self.counters.increment_tech_retry(record.target_id)
        self._emit(
            "deterministic_validation_failed",
            object_id=record.target_id,
            version=record.version,
            mode=record.mode,
            severity="ERROR",
            detail={
                "errors": errors,
                "error_class": ErrorClass.INPUT_VALIDATION.value,
                "cnt1": self.counters.get(record.target_id, CounterKind.CNT1),
                "cnt1_consumed": False,
                "tech_retry": self.counters.get_tech_retry(record.target_id),
                "note": "技术失败不消耗任何 CNT（R4）",
            },
        )

    # ------------------------------------------------------------------
    # 模型审核轮次
    # ------------------------------------------------------------------
    def begin_model_round(self, cycle_id: str) -> int:
        """在执行模型审核**之前**计数一次（方案第七部分·一·4）。返回 CNT1 当前值。"""
        record, cycle = self._require(cycle_id)
        if cycle_id in self._round_open:
            raise ModelRoundAlreadyOpen(
                f"{cycle_id} 已有一轮模型审核在进行：禁止重复计数（会污染 CNT1 口径）"
            )
        if cycle.state is not ReviewState.SCANNING:
            raise IllegalTransition(
                cycle.state, "scan_completed", allowed=cycle.allowed_events(),
                reason="只有 SCANNING 状态才允许执行模型审核轮次",
            )
        self._guard_candidate_limit(record.target_id)
        self._round_open.add(cycle_id)
        try:
            return self.counters.increment(record.target_id, CounterKind.CNT1)
        except Exception:
            self._round_open.discard(cycle_id)
            raise

    def _resolve_dimension_scores(
        self,
        scores: dict[str, float] | None,
        score: float | None,
        passed: bool | None,
    ) -> tuple[float, bool]:
        """把「分维度分数」折算成 (总分, 是否通过)。

        * 没传 scores -> 用原来的 score/passed（**旧调用方行为不变**）；
        * 传了 scores -> 必须覆盖 sset_review.score_dimensions 里的**全部维度**
          （缺维度直接报错，不能悄悄按缺的算平均 —— 那会让分数虚高）；
        * 每维必须在 [0,100]；
        * 是否通过按 score_threshold 判定（给了 passed 也不采信，
          避免分数没到但调用方说通过这种自相矛盾）。
        """
        if scores is None:
            if score is None:
                raise ValueError("submit_model_result 需要 score 或 scores 其中之一")
            return float(score), bool(passed)

        if not isinstance(scores, dict) or not scores:
            raise ValueError("scores 必须是非空 dict（维度名 -> 分数）")
        dims = list(getattr(self._cfg, "score_dimensions", None) or [])
        if not dims:
            raise ValueError("asset_review.score_dimensions 未配置，无法按维度评分")

        missing = [d for d in dims if d not in scores]
        if missing:
            raise ValueError(
                f"分维度评分缺少维度：{missing}；"
                f"配置要求全部 {len(dims)} 维：{dims}（缺维度会让平均分虚高）"
            )
        values: list[float] = []
        for dim in dims:
            raw = scores[dim]
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"维度 {dim} 的分数不是数字：{raw!r}") from exc
            if not (0.0 <= value <= 100.0):
                raise ValueError(f"维度 {dim} 的分数必须在 [0,100]，收到 {value}")
            values.append(value)

        total = sum(values) / len(values)
        threshold = float(getattr(self._cfg, "score_threshold", 70.0))
        return total, total >= threshold

    def submit_model_result(
        self,
        cycle_id: str,
        *,
        score: float | None = None,
        passed: bool | None = None,
        scores: dict[str, float] | None = None,
    ) -> dict:
        # 图一 SG3 H5「评分维度：画质/一致性/合规/构图」+ sset_review.score_dimensions。
        # 此前 score_dimensions **只被校验、没人用来组织评分**（ROUND18 棘轮发现）：
        # AssetReviewer 只接收单一 score，四维没有参与判定。
        # 现在允许传分维度分数：按配置的维度取平均得出总分，并校验维度齐全。
        score, passed = self._resolve_dimension_scores(scores, score, passed)

        found = self._lookup(cycle_id)
        if found is None:
            return self._orphan_or_raise(
                cycle_id, source="model", passed=bool(passed), score=float(score),
                reason="模型迟到结果（周期已由其他实例/其他提交路径终结）",
            )
        record, cycle = found
        terminal = cycle.state.is_terminal or bool(self._concluded.get(cycle_id))
        if terminal:
            attempt = self._new_attempt(record, ReviewMode.MODEL, score=float(score), passed=bool(passed),
                                        reason="迟到模型结果")
            return self._archive_late(record, cycle, attempt, source="model",
                                      reason=f"周期已产生结论（{self._concluded.get(cycle_id) or cycle.state.value}），模型迟到结果只归档")
        if cycle.state not in (ReviewState.SCANNING, ReviewState.ARBITRATING):
            raise IllegalTransition(
                cycle.state, "scan_completed", allowed=cycle.allowed_events(),
                reason=f"模型审核结果与当前状态不符（{cycle.state.value}）：人工模式不得提交模型结论",
            )
        if cycle_id in self._round_open:
            self._round_open.discard(cycle_id)
            round_no = self.counters.get(record.target_id, CounterKind.CNT1)
        else:
            self._guard_candidate_limit(record.target_id)
            round_no = self.counters.increment(record.target_id, CounterKind.CNT1)
        self._register_model_result(record, cycle, float(score), bool(passed), round_no)
        return self._result(record, cycle, extra={"round": round_no})

    def submit_model_failure(
        self,
        cycle_id: str,
        *,
        error_class: ErrorClass = ErrorClass.MODEL_API,
        reason: str = "",
    ) -> dict:
        """技术失败：**不消耗任何 CNT**（若已执行前计数则回滚），只加独立技术重试计数。"""
        found = self._lookup(cycle_id)
        if found is None:
            try:
                parsed_error = ErrorClass.parse(error_class)
            except ValueError:
                parsed_error = ErrorClass.MODEL_API
            return self._orphan_or_raise(
                cycle_id, source="model", passed=False, score=None,
                error_class=parsed_error,
                reason=f"技术失败信息（周期已终结）：{reason or parsed_error.value}",
            )
        record, cycle = found
        if cycle.state.is_terminal or self._concluded.get(cycle_id):
            attempt = self._new_attempt(record, ReviewMode.MODEL, score=None, passed=False,
                                        reason=f"技术失败（迟到）：{reason}",
                                        error_class=error_class)
            return self._archive_late(record, cycle, attempt, source="model",
                                      reason="周期已产生结论，技术失败信息只归档")
        if cycle.state is not ReviewState.SCANNING:
            raise IllegalTransition(
                cycle.state, "scan_technical_failure", allowed=cycle.allowed_events(),
                reason="技术失败只在 SCANNING 状态下登记",
            )
        try:
            error_class = ErrorClass.parse(error_class)
        except ValueError as exc:
            raise ReviewError(f"error_class 非法：{exc}") from exc
        if cycle_id in self._round_open:
            self._round_open.discard(cycle_id)
            self.counters.rollback_increment(record.target_id, CounterKind.CNT1)
        tech = self.counters.increment_tech_retry(record.target_id)
        record.reason = f"技术失败：{reason or error_class.value}"
        attempt = self._new_attempt(record, ReviewMode.MODEL, score=None, passed=False,
                                    reason=record.reason, error_class=error_class)
        record.attempts.append(attempt)
        self._fire(cycle, record, "scan_technical_failure",
                   error_class=str(error_class), error=reason)
        self._emit(
            "model_review_technical_failure",
            object_id=record.target_id,
            version=record.version,
            mode=record.mode,
            severity="ERROR",
            detail={
                "error_class": str(error_class),
                "attempt": attempt.to_dict(),
                "cnt1": self.counters.get(record.target_id, CounterKind.CNT1),
                "cnt1_consumed": False,
                "tech_retry": tech,
                "note": "技术失败不消耗任何 CNT；只累计独立技术重试计数（R4）",
            },
        )
        return self._result(record, cycle, extra={"tech_retry": tech, "error_class": str(error_class)})

    def _execute_model_round(self, record: ReviewRecord, cycle: ReviewCycle, material: dict) -> int | None:
        cycle_id = record.review_cycle_id
        round_no = self.begin_model_round(cycle_id)
        try:
            payload = self._invoke_model_review(material)
        except ReviewTechnicalError as exc:
            self._round_open.discard(cycle_id)
            self.counters.rollback_increment(record.target_id, CounterKind.CNT1)
            tech = self.counters.increment_tech_retry(record.target_id)
            record.reason = f"技术失败：{exc}"
            self._fire(cycle, record, "scan_technical_failure",
                       error_class=str(exc.error_class), error=str(exc))
            self._emit(
                "model_review_technical_failure",
                object_id=record.target_id,
                version=record.version,
                mode=record.mode,
                severity="ERROR",
                detail={
                    "error_class": str(exc.error_class),
                    "error": str(exc),
                    "cnt1": self.counters.get(record.target_id, CounterKind.CNT1),
                    "cnt1_consumed": False,
                    "cnt1_rolled_back": True,
                    "tech_retry": tech,
                },
            )
            return None
        self._round_open.discard(cycle_id)
        self._register_model_result(record, cycle, payload["score"], payload["passed"], round_no)
        return round_no

    def _register_model_result(
        self, record: ReviewRecord, cycle: ReviewCycle, score: float, passed: bool, round_no: int
    ) -> ReviewAttempt:
        attempt = self._new_attempt(
            record, record.mode, score=float(score), passed=bool(passed),
            reason=f"模型审核第 {round_no} 轮",
        )
        record.model_attempt = attempt
        record.attempts.append(attempt)
        self._candidates.setdefault(record.target_id, []).append(
            {
                "version": record.version,
                "score": float(score),
                "round": int(round_no),
                "review_cycle_id": record.review_cycle_id,
                "attempt_id": attempt.attempt_id,
                "ts": attempt.backend_ts,
            }
        )
        self._emit(
            "model_review_result",
            object_id=record.target_id,
            version=record.version,
            mode=record.mode,
            detail={**attempt.to_dict(), "cnt1": self.counters.get(record.target_id, CounterKind.CNT1),
                    "candidate_limit": self._cfg.model_candidate_limit},
        )
        if cycle.state is ReviewState.ARBITRATING:
            self._resolve_arbitration(record, cycle)
            return attempt
        if cycle.state is not ReviewState.SCANNING:
            raise IllegalTransition(
                cycle.state, "scan_completed", allowed=cycle.allowed_events(),
                reason="模型结果到达时周期不在扫描态",
            )
        self._fire(cycle, record, "scan_completed", attempt=attempt.attempt_id)
        self._apply_model_score(record, cycle, float(score), bool(passed), int(round_no))
        return attempt

    def _apply_model_score(
        self, record: ReviewRecord, cycle: ReviewCycle, score: float, passed: bool, round_no: int
    ) -> None:
        limit = int(self._cfg.model_candidate_limit)
        threshold = float(self._cfg.score_threshold)
        record.score = float(score)
        if passed:
            self._conclude(
                record, cycle, "score_above_threshold", lock_source=LockSource.MODEL_PASS,
                score=score, reason=f"模型审核通过：评分 {score} ≥ 阈值 {threshold}",
            )
        elif round_no < limit:
            self._conclude(
                record, cycle, "score_below_threshold_retryable", lock_source=None,
                score=score,
                reason=f"模型审核未通过：评分 {score} < 阈值 {threshold}（第 {round_no}/{limit} 轮，可重试）",
            )
        else:
            self._conclude_fallback(
                record, cycle, event="score_below_threshold_at_candidate_limit",
                reason=f"模型审核达到候选上限 {limit} 轮仍未通过",
            )

    # ------------------------------------------------------------------
    # 人工审核
    # ------------------------------------------------------------------
    def submit_human_result(
        self,
        cycle_id: str,
        *,
        passed: bool,
        operator: str,
        client_ts: float | None = None,
    ) -> dict:
        if not isinstance(passed, bool):
            raise TypeError("passed 必须是 bool")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator 必须是非空字符串（人工操作必须留痕）")
        found = self._lookup(cycle_id)
        if found is None:
            return self._orphan_or_raise(
                cycle_id, source="human", passed=passed, score=None, operator=operator,
                reason="人工迟到结果（周期已由其他实例/其他提交路径终结）",
            )
        record, cycle = found
        # 统一后端时间戳：客户端时钟一律不参与判定（只写日志）
        backend_ts = self._now()
        attempt = self._new_attempt(record, ReviewMode.HUMAN, score=None, passed=passed,
                                    reason=f"人工审核（operator={operator}）", operator=operator)
        self._emit(
            "human_review_result",
            object_id=record.target_id,
            version=record.version,
            mode=ReviewMode.HUMAN,
            detail={
                **attempt.to_dict(),
                "client_ts": client_ts,
                "client_ts_used": False,
                "note": "客户端时间戳仅记录，判定与仲裁一律使用 backend_ts",
            },
        )
        if cycle.state.is_terminal or self._concluded.get(cycle_id):
            return self._archive_late(
                record, cycle, attempt, source="human",
                reason=f"周期已产生结论（{self._concluded.get(cycle_id) or cycle.state.value}），人工迟到结果只归档",
            )

        replaced = record.human_attempt is not None
        record.human_attempt = attempt
        record.attempts.append(attempt)
        if replaced:
            self._emit("human_result_replaced", severity="WARN",
                       object_id=record.target_id, version=record.version, mode=ReviewMode.HUMAN,
                       detail={"note": "结论提交前人工结果被更新；时间戳取最新一次后端时间"})

        state = cycle.state
        if state is ReviewState.AWAITING_REVIEW:
            if passed:
                self._conclude(record, cycle, "human_pass", lock_source=LockSource.HUMAN_PASS,
                               reason=f"人工审核通过（operator={operator}）")
            else:
                self._conclude(record, cycle, "human_reject", lock_source=None,
                               reason=f"人工审核未通过（operator={operator}）")
        elif state in (ReviewState.TIMEOUT, ReviewState.SCANNING, ReviewState.CLASSIFYING):
            event = {
                ReviewState.TIMEOUT: "late_human_result_needs_arbitration",
                ReviewState.SCANNING: "late_human_result_arrived",
                ReviewState.CLASSIFYING: "model_result_ready_with_pending_human",
            }[state]
            self._fire(cycle, record, event, attempt=attempt.attempt_id)
            self._resolve_arbitration_if_ready(record, cycle)
        elif state is ReviewState.ARBITRATING:
            self._resolve_arbitration_if_ready(record, cycle)
        else:
            # RECEIVED / VALIDATING / BLOCKED：交给状态机报错，绝不静默接受
            self._fire(cycle, record, "human_pass" if passed else "human_reject",
                       attempt=attempt.attempt_id)
            return self._result(record, cycle)  # pragma: no cover - fire 必抛
        return self._result(record, cycle, extra={"attempt": attempt.to_dict(),
                                                  "client_ts_ignored": client_ts is not None})

    # ------------------------------------------------------------------
    # 超时与切换
    # ------------------------------------------------------------------
    def is_timed_out(self, cycle_id: str) -> bool:
        _, cycle = self._require(cycle_id)
        if cycle.state is not ReviewState.AWAITING_REVIEW:
            return False
        anchor = _to_dt(self._anchor_ts(cycle_id), "超时计时基准")
        return self._now_dt() >= anchor + timedelta(seconds=timeout_seconds(self._cfg))

    def deadline(self, cycle_id: str) -> str | None:
        _, cycle = self._require(cycle_id)
        if cycle.state is not ReviewState.AWAITING_REVIEW:
            return None
        anchor = _to_dt(self._anchor_ts(cycle_id), "超时计时基准")
        return (anchor + timedelta(seconds=timeout_seconds(self._cfg))).isoformat(timespec="seconds")

    def record_human_activity(self, cycle_id: str, *, kind: str = "inspect") -> dict:
        """登记一次人工界面操作。只有有效审核操作才顺延超时（H1B/H1C）。

        `kind` 缺省为 `"inspect"`（一次有效审核操作）：调用方显式上报「人工操作发生了」
        即视为有效操作。**未知操作类型不会顺延超时**（见下方 WARN 分支）——
        宁可多超时一次，也不能因为一个没见过的 UI 事件而永久不超时。
        """
        self._require(cycle_id)
        kind_text = str(kind or "").strip().lower()
        if not kind_text:
            raise ValueError("kind 必须是非空字符串")
        _, cycle = self._require(cycle_id)
        window = self._window(cycle_id)
        if self._concluded.get(cycle_id) or cycle.state is not ReviewState.AWAITING_REVIEW:
            return {
                "cycle_id": cycle_id, "paused": False, "deadline": None,
                "reason": f"周期不在人工等待态（{cycle.state.value}）", "kind": kind_text,
            }
        if kind_text in VALID_HUMAN_OP_KINDS:
            if not self._cfg.timeout_pause_on_active_op:
                window["ignored_ops"] += 1
                self._emit("human_review_activity",
                           object_id=cycle.object_id, version=cycle.version, mode=ReviewMode.HUMAN,
                           detail={"kind": kind_text, "paused": False,
                                   "reason": "timeout_pause_on_active_op=false"})
                return {"cycle_id": cycle_id, "paused": False, "deadline": self.deadline(cycle_id),
                        "reason": "配置关闭了超时暂停", "kind": kind_text}
            window["last_valid_op_ts"] = self._now()
            window["valid_ops"] += 1
            window["last_kind"] = kind_text
            self._emit("human_review_activity",
                       object_id=cycle.object_id, version=cycle.version, mode=ReviewMode.HUMAN,
                       detail={"kind": kind_text, "paused": True, "valid_ops": window["valid_ops"]})
            return {"cycle_id": cycle_id, "paused": True, "deadline": self.deadline(cycle_id),
                    "valid_ops": window["valid_ops"], "kind": kind_text}
        window["ignored_ops"] += 1
        if kind_text in NON_ACTIVITY_KINDS:
            self._emit("human_review_activity",
                       object_id=cycle.object_id, version=cycle.version, mode=ReviewMode.HUMAN,
                       detail={"kind": kind_text, "paused": False,
                               "reason": "界面打开/心跳/闲置不算有效审核操作，超时计时继续"})
            return {"cycle_id": cycle_id, "paused": False, "deadline": self.deadline(cycle_id),
                    "reason": "非有效审核操作（界面打开/心跳/闲置）", "kind": kind_text}
        # 未知操作类型：绝不当作有效操作（宁可超时，也不能因未知事件永久不超时）
        self._emit("human_review_activity", severity="WARN",
                   object_id=cycle.object_id, version=cycle.version, mode=ReviewMode.HUMAN,
                   detail={"kind": kind_text, "paused": False,
                           "reason": "未知操作类型，未计入有效审核操作"})
        return {"cycle_id": cycle_id, "paused": False, "deadline": self.deadline(cycle_id),
                "reason": f"未知操作类型：{kind_text}", "kind": kind_text}

    def on_human_timeout(self, cycle_id: str) -> dict:
        """人工超时：写 AUTO_SWITCHED、CNT1 归零、发非阻断通知（方案第七部分·一·3）。"""
        record, cycle = self._require(cycle_id)
        if self._concluded.get(cycle_id):
            return {"timeout": False, "reason": "already_concluded", "review_cycle_id": cycle_id,
                    "state": record.review_state}
        if cycle.state is not ReviewState.AWAITING_REVIEW:
            return {"timeout": False, "reason": f"state_not_awaiting_review:{cycle.state.value}",
                    "review_cycle_id": cycle_id, "state": cycle.state}
        if not self.is_timed_out(cycle_id):
            deadline = self.deadline(cycle_id)
            remaining = (self._parse_dt(deadline) - self._now_dt()).total_seconds() if deadline else None
            return {"timeout": False, "reason": "timeout_not_reached", "review_cycle_id": cycle_id,
                    "state": cycle.state, "deadline": deadline,
                    "remaining_s": None if remaining is None else round(remaining, 3)}
        return self._switch_to_model(record, cycle, reason="人工审核超时（无有效响应）")

    def force_auto_switch(self, cycle_id: str) -> dict:
        """管理/测试入口：跳过超时判定直接切换（生产路径应使用 `on_human_timeout`）。"""
        record, cycle = self._require(cycle_id)
        if cycle.state is not ReviewState.AWAITING_REVIEW:
            return {"timeout": False, "reason": f"state_not_awaiting_review:{cycle.state.value}",
                    "review_cycle_id": cycle_id, "state": cycle.state}
        return self._switch_to_model(record, cycle, reason="人工审核超时（强制切换）", forced=True)

    def poll_timeouts(self) -> list[dict]:
        """扫描所有人工等待周期，对已超时者执行切换。返回切换结果列表。"""
        results: list[dict] = []
        with self._reg_lock:
            cycle_ids = list(self._cycles)
        for cycle_id in cycle_ids:
            record, cycle = self._require(cycle_id)
            if cycle.state is not ReviewState.AWAITING_REVIEW or self._concluded.get(cycle_id):
                continue
            if self.is_timed_out(cycle_id):
                results.append(self.on_human_timeout(cycle_id))
        return results

    def _switch_to_model(
        self, record: ReviewRecord, cycle: ReviewCycle, *, reason: str, forced: bool = False
    ) -> dict:
        cycle_id = record.review_cycle_id
        window = self._window(cycle_id)
        elapsed = (self._now_dt() - self._parse_dt(window["opened_ts"])).total_seconds()
        self._fire(cycle, record, "timeout_reached", reason=reason)
        record.reason = f"人工审核超时：{reason}"
        self._emit(
            "human_review_timeout",
            object_id=record.target_id,
            version=record.version,
            mode=ReviewMode.HUMAN,
            severity="WARN",
            detail={
                "review_cycle_id": cycle_id,
                "timeout_minutes": float(self._cfg.human_timeout_minutes),
                "elapsed_s": round(elapsed, 3),
                "valid_ops": window["valid_ops"],
                "ignored_ops": window["ignored_ops"],
                "last_valid_op_ts": window["last_valid_op_ts"],
                "forced": forced,
                "note": "只有持续进行有效审核操作才顺延超时；界面打开但持续闲置仍会超时",
            },
        )
        self._fire(cycle, record, "auto_switch_model_started", reason="超时自动切换为模型审核")
        record.mode = ReviewMode.AUTO_SWITCHED
        cycle.mode = ReviewMode.AUTO_SWITCHED
        record.updated_at = self._now()
        # 切换时 CNT1 从 0 开始（唯一合法重置）；候选兜底范围随计数作用域重启
        before = self.counters.get(record.target_id, CounterKind.CNT1)
        self.counters.reset(record.target_id, CounterKind.CNT1)
        cleared = len(self._candidates.pop(record.target_id, []) or [])
        window["switched_at"] = self._now()
        self._emit(
            "review_mode_switched",
            object_id=record.target_id,
            version=record.version,
            mode=ReviewMode.AUTO_SWITCHED,
            detail={
                "review_cycle_id": cycle_id,
                "from": ReviewMode.HUMAN.value,
                "to": ReviewMode.AUTO_SWITCHED.value,
                "cnt1_before": before,
                "cnt1_after": 0,
                "human_attempts_not_counted_into_cnt1": True,
                "candidate_scope_cleared": cleared,
                "reason": reason,
            },
        )
        notification = self._notify_switch(record, reason)
        return {
            "timeout": True,
            "review_cycle_id": cycle_id,
            "state": record.review_state,
            "mode": ReviewMode.AUTO_SWITCHED,
            "cnt1": self.counters.get(record.target_id, CounterKind.CNT1),
            "cnt1_before": before,
            "notification": notification,
            "deadline": None,
            "record": record.to_dict(),
        }

    def _notify_switch(self, record: ReviewRecord, reason: str) -> list[dict]:
        title = "素材审核超时：已自动切换为模型审核"
        message = (
            f"{record.target_id} {record.version} 在 {self._cfg.human_timeout_minutes} 分钟内"
            f"无有效审核响应（{reason}），已切换为模型审核，CNT1 从 0 开始。"
        )
        try:
            return self.notifier.notify(
                title=title, message=message, severity="WARN",
                detail={"review_cycle_id": record.review_cycle_id, "asset_id": record.target_id,
                        "version": record.version, "mode": ReviewMode.AUTO_SWITCHED.value},
            )
        except Exception as exc:  # pragma: no cover - notify 自身已保证不抛
            _LOG.warning("超时通知投递异常（不阻断主流程）：%s", exc)
            return [{"channel": None, "ok": False, "error": str(exc)}]

    # ------------------------------------------------------------------
    # 仲裁
    # ------------------------------------------------------------------
    def arbitrate_cycle(self, cycle_id: str) -> dict:
        record, cycle = self._require(cycle_id)
        if cycle.state is not ReviewState.ARBITRATING:
            raise ReviewError(
                f"仲裁需要 ARBITRATING 状态，当前 {cycle.state.value}（先入仲裁才能裁决）"
            )
        return self._resolve_arbitration(record, cycle)

    def _resolve_arbitration_if_ready(self, record: ReviewRecord, cycle: ReviewCycle) -> dict | None:
        if record.human_attempt is None or record.model_attempt is None:
            self._emit(
                "arbitration_pending",
                object_id=record.target_id,
                version=record.version,
                mode=record.mode,
                detail={
                    "review_cycle_id": record.review_cycle_id,
                    "human_ready": record.human_attempt is not None,
                    "model_ready": record.model_attempt is not None,
                    "note": "等待另一侧结果，暂不提交最终结论",
                },
            )
            return None
        return self._resolve_arbitration(record, cycle)

    def _resolve_arbitration(self, record: ReviewRecord, cycle: ReviewCycle) -> dict:
        outcome = arbitrate(record.to_dict())
        record.arb_id = outcome["arb_id"]
        record.arb_winner = outcome["winner"]
        self._emit(
            "arbitration_resolved",
            object_id=record.target_id,
            version=record.version,
            mode=record.mode,
            detail={**outcome, "review_cycle_id": record.review_cycle_id},
        )
        limit = int(self._cfg.model_candidate_limit)
        if outcome["winner"] == "HUMAN":
            abort = bridge.abort_id(record.mod_id)
            record.abort_id = abort
            self._emit(
                "abort_signal_issued",
                object_id=record.target_id,
                version=record.version,
                mode=record.mode,
                severity="WARN",
                detail={
                    "abort_id": abort,
                    "arb_id": outcome["arb_id"],
                    "rule": outcome["rule"],
                    "human_ts": outcome["human_ts"],
                    "model_ts": outcome["model_ts"],
                    "human_passed": outcome["human_passed"],
                    "note": "模型迟到结果不得再触发 LOCK、更新素材库或启动下游",
                },
            )
            record.score = record.human_attempt.score
            if outcome["human_passed"]:
                self._conclude(
                    record, cycle, "human_earlier_and_passed", lock_source=LockSource.HUMAN_PASS,
                    reason=(
                        f"仲裁：人工时间戳 {outcome['human_ts']} < 模型完成时间戳 {outcome['model_ts']}，"
                        f"人工通过胜出并发出 ABORT_SIGNAL（{abort}）"
                    ),
                )
            else:
                self._conclude(
                    record, cycle, "human_earlier_and_rejected", lock_source=None,
                    reason=(
                        f"仲裁：人工时间戳 {outcome['human_ts']} < 模型完成时间戳 {outcome['model_ts']}，"
                        f"人工拒绝胜出并发出 ABORT_SIGNAL（{abort}）"
                    ),
                )
        else:
            self._emit(
                "human_result_logged_only",
                object_id=record.target_id,
                version=record.version,
                mode=record.mode,
                detail={
                    "reason": outcome["rule"],
                    "human_attempt_id": outcome["human_attempt_id"],
                    "human_ts": outcome["human_ts"],
                    "model_ts": outcome["model_ts"],
                    "tie": outcome["tie"],
                    "note": "模型结论胜出，人工结果仅写日志（方案第七部分·一·9）",
                },
            )
            record.score = record.model_attempt.score
            if outcome["model_passed"]:
                self._conclude(
                    record, cycle, "model_earlier_or_equal_and_passed",
                    lock_source=LockSource.MODEL_PASS,
                    reason=(
                        f"仲裁：模型完成时间戳 {outcome['model_ts']} ≥ 人工时间戳 {outcome['human_ts']}"
                        "，模型通过胜出，人工结果仅记日志"
                    ),
                )
            else:
                cnt1 = self.counters.get(record.target_id, CounterKind.CNT1)
                if cnt1 < limit:
                    self._conclude(
                        record, cycle, "model_earlier_or_equal_not_passed_retryable",
                        lock_source=None,
                        reason=(
                            f"仲裁：模型胜出但不通过（CNT1={cnt1}/{limit}，可继续优化新候选）"
                        ),
                    )
                else:
                    self._conclude_fallback(
                        record, cycle, event="model_earlier_or_equal_not_passed_at_limit",
                        reason=f"仲裁：模型胜出但未通过且 CNT1 已达上限 {limit} → 兜底",
                    )
        return {
            "arbitration": outcome,
            "state": record.review_state,
            "review_cycle_id": record.review_cycle_id,
            "lock_source": record.lock_source,
            "abort_id": record.abort_id,
            "committed": (self._commit_results.get(record.review_cycle_id) or {}).get("committed"),
            "lock_id": (self._commit_results.get(record.review_cycle_id) or {}).get("lock_id"),
        }

    # ------------------------------------------------------------------
    # 结论提交
    # ------------------------------------------------------------------
    def _conclude(
        self,
        record: ReviewRecord,
        cycle: ReviewCycle,
        event: str,
        *,
        lock_source: LockSource | None,
        reason: str,
        score: float | None = None,
    ) -> dict | None:
        record.reason = reason
        if score is not None:
            record.score = float(score)
        record.lock_source = lock_source
        self._fire(cycle, record, event, reason=reason)
        self._concluded[record.review_cycle_id] = record.review_state.value
        return self._commit(record, cycle)

    def _conclude_fallback(
        self,
        record: ReviewRecord,
        cycle: ReviewCycle,
        *,
        event: str,
        reason: str,
    ) -> None:
        """兜底：从 CNT1 作用域内的候选里选最高分，并把 LOCK 提交到被选中版本的周期。"""
        obj_id = record.target_id
        candidates = list(self._candidates.get(obj_id, []))
        if not candidates:
            raise ReviewError(
                "兜底需要已审核候选记录，但候选为空——拒绝隐式兜底（方案第七部分·一·6）"
            )
        best = max(candidates, key=lambda c: (float(c["score"]), -int(c["round"])))
        best_version = str(best["version"])
        detail = {
            "candidates": candidates,
            "selected_version": best_version,
            "selected_score": best["score"],
            "selected_round": best["round"],
            "policy": self._cfg.fallback,
            "cnt1": self.counters.get(obj_id, CounterKind.CNT1),
            "candidate_limit": int(self._cfg.model_candidate_limit),
            "tie_break": "分数最高；同分取轮次更早者（确定性）",
        }
        record.reason = (
            f"{reason}；候选={[(c['version'], c['score']) for c in candidates]}，选择 {best_version}"
        )
        record.lock_source = None
        self._fire(cycle, record, event, fallback=detail)
        self._emit(
            "fallback_selected",
            object_id=obj_id,
            version=record.version,
            mode=record.mode,
            severity="WARN",
            detail={**detail, "review_cycle_id": record.review_cycle_id,
                    "note": "兜底结论不伪装成普通阈值通过（方案第七部分·一·6）"},
        )
        self._concluded[record.review_cycle_id] = record.review_state.value
        self._fallback_version[record.review_cycle_id] = best_version
        self._fallback_score[record.review_cycle_id] = float(best["score"])

        if best_version == record.version:
            record.lock_source = LockSource.FALLBACK_PASS
            record.score = float(best["score"])
            record.__post_init__()  # 复查语义守卫（FALLBACK + lock_source 合法）
            self._commit(record, cycle)
            return

        # 跨版本兜底：接受结论提交到「被选中版本的那个 cycle」上（Lead 裁决）
        target_cycle_id = bridge.review_cycle_id("ASSET", obj_id, best_version)
        previous = self._records.get(target_cycle_id)
        token = None
        if previous is not None and previous.finalization_token:
            token = previous.finalization_token
        ledger = self._ledger_for(record)
        if ledger.get(target_cycle_id) is not None:
            ledger.release(target_cycle_id, token, reason=f"兜底接受补记：{reason}")
        fb_record = ReviewRecord(
            review_cycle_id=target_cycle_id,
            mod_id=bridge.mod_id("ASSET", obj_id),
            review_state=ReviewState.FALLBACK,
            mode=record.mode,
            target_kind="asset",
            target_id=obj_id,
            version=best_version,
            content_hash=None,  # 由 commit_final 从 store 读取该版本真实指纹（R3）
            score=float(best["score"]),
            reason=(
                f"兜底接受：{reason}；候选={[(c['version'], c['score']) for c in candidates]}，"
                f"选择 {best_version}；决策周期={record.review_cycle_id}"
            ),
            lock_source=LockSource.FALLBACK_PASS,
            attempts=list(previous.attempts) if previous is not None else [],
            cnt_id=bridge.cnt_id(obj_id, CounterKind.CNT1),
            finalization_token=token or bridge.new_finalization_token(),
        )
        fb_cycle = ReviewCycle(
            target_cycle_id,
            state=ReviewState.FALLBACK,
            object_type="ASSET",
            object_id=obj_id,
            version=best_version,
            mode=record.mode,
            review_record_id=fb_record.mod_id,
            clock=self._clock,
            event_log=self.events,
        )
        if previous is not None:
            self._superseded.setdefault(target_cycle_id, []).append(previous)
        with self._reg_lock:
            self._records[target_cycle_id] = fb_record
            self._cycles[target_cycle_id] = fb_cycle
        self._emit(
            "fallback_acceptance_recorded",
            object_id=obj_id,
            version=best_version,
            mode=record.mode,
            severity="WARN",
            detail={
                "review_cycle_id": target_cycle_id,
                "decision_cycle_id": record.review_cycle_id,
                "previous_state": previous.review_state.value if previous is not None else None,
                "amended": previous is not None,
                "lock_source": LockSource.FALLBACK_PASS.value,
                "note": (
                    "REVIEW_TRANSITIONS 未定义 BLOCKED→FALLBACK 边：本记录是「兜底接受补记」，"
                    "以初始状态 FALLBACK 建立被选中版本的接受记录，不伪造状态转移"
                ),
            },
        )
        self._concluded[target_cycle_id] = ReviewState.FALLBACK.value
        result = self._commit(fb_record, fb_cycle)
        self._fallback_commit[record.review_cycle_id] = result
        self._fallback_version[record.review_cycle_id] = best_version
        self._emit(
            "fallback_accepted",
            object_id=obj_id,
            version=best_version,
            mode=record.mode,
            detail={
                "decision_cycle_id": record.review_cycle_id,
                "accepted_cycle_id": target_cycle_id,
                "commit": result,
                "lock_id": (result or {}).get("lock_id"),
            },
        )

    def _commit(self, record: ReviewRecord, cycle: ReviewCycle) -> dict:
        data = self._cycle_dict(record, cycle)
        if not self.auto_commit:
            # 只推进状态、不提交：把 decisions 交给调用方的 commit_final（唯一提交点不变）
            pending = {
                "committed": False,
                "reason": "auto_commit_disabled",
                "claim": "not_attempted",
                "review_cycle_id": record.review_cycle_id,
                "state": record.review_state,
                "version": record.version,
                "lock_id": None,
                "finalized_at": None,
                "side_effects": [],
                "detail": "auto_commit=False：请用 review_version/submit_* 返回的 cycle dict 调用 commit_final",
            }
            self._pending_commit[record.review_cycle_id] = pending
            return pending
        result = commit_final(data, store=self.store)
        token = data.get("finalization_token")
        if token:
            record.finalization_token = token
        if result.get("committed"):
            record.finalized_at = result.get("finalized_at") or self._now()
        self._commit_results[record.review_cycle_id] = result
        if not result.get("committed") and result.get("reason") != "already_finalized":
            self._emit(
                "finalization_rejected",
                object_id=record.target_id,
                version=record.version,
                mode=record.mode,
                severity="WARN",
                detail={"review_cycle_id": record.review_cycle_id, "commit": result},
            )
        return result

    def _archive_late(
        self,
        record: ReviewRecord,
        cycle: ReviewCycle,
        attempt: ReviewAttempt,
        *,
        source: str,
        reason: str,
    ) -> dict:
        """迟到结果：归档为 ABORTED，**绝不改变当前版本状态**。"""
        attempt.superseded = True
        record.attempts.append(attempt)
        outcome = archive_late_result(
            self._cycle_dict(record, cycle),
            store=self.store,
            ledger=self._ledger_for(record),
            reason=reason,
            attempt=attempt.to_dict(),
        )
        entry = {
            "source": source,
            "reason": reason,
            "review_cycle_id": record.review_cycle_id,
            "asset_id": record.target_id,
            "version": record.version,
            "state_after": record.review_state.value,
            "archive": outcome,
            "attempt": attempt.to_dict(),
        }
        self._late.setdefault(record.review_cycle_id, []).append(entry)
        return {
            "accepted": False,
            "committed": False,
            "reason": reason,
            "archived": True,
            "archive_state": ReviewState.ABORTED.value,
            "wrote_version_state": False,
            "state": record.review_state,
            "review_cycle_id": record.review_cycle_id,
            "asset_id": record.target_id,
            "version": record.version,
            "attempt": attempt.to_dict(),
            "archive": outcome,
        }

    # ------------------------------------------------------------------
    # 状态/记录管理
    # ------------------------------------------------------------------
    def _lookup(self, cycle_id: str) -> tuple[ReviewRecord, ReviewCycle] | None:
        with self._reg_lock:
            record = self._records.get(cycle_id)
            cycle = self._cycles.get(cycle_id)
        if record is None or cycle is None:
            return None
        return record, cycle

    def _require(self, cycle_id: str) -> tuple[ReviewRecord, ReviewCycle]:
        found = self._lookup(cycle_id)
        if found is None:
            known = ", ".join(sorted(self._records)[:5]) or "（无）"
            raise ReviewError(f"未知 review_cycle_id：{cycle_id!r}；本实例已知周期示例：{known}")
        return found

    def ledger_entry(self, cycle_id: str) -> dict | None:
        """读取该周期的最终提交记录（落盘 ledger 优先）。用于跨实例/重启后的迟到结果判定。"""
        ledger = self._ledger if self._ledger is not None else get_ledger(self._ledger_path)
        try:
            return ledger.get(cycle_id)
        except Exception as exc:  # pragma: no cover - 读取失败按「无记录」处理并告警
            _LOG.warning("读取 finalization ledger 失败：%s", exc)
            return None

    def _orphan_or_raise(
        self,
        cycle_id: str,
        *,
        source: str,
        passed: bool | None,
        score: float | None,
        operator: str | None = None,
        error_class: ErrorClass | None = None,
        reason: str = "",
    ) -> dict:
        """未知周期：若 ledger 证明它已被提交，则按迟到结果归档；否则**显式报错**（绝不静默）。"""
        entry = self.ledger_entry(cycle_id)
        if entry is None:
            known = ", ".join(sorted(self._records)[:5]) or "（无）"
            raise ReviewError(
                f"未知 review_cycle_id：{cycle_id!r}；本实例已知周期示例：{known}；"
                "finalization ledger 中也没有该周期的提交记录，无法判定为迟到结果"
            )
        return self._archive_late_orphan(
            cycle_id, entry, source=source, passed=passed, score=score,
            operator=operator, error_class=error_class,
            reason=reason or "周期已终结，结果只归档",
        )

    def _archive_late_orphan(
        self,
        cycle_id: str,
        entry: dict,
        *,
        source: str,
        passed: bool | None,
        score: float | None,
        operator: str | None = None,
        error_class: ErrorClass | None = None,
        reason: str,
    ) -> dict:
        """迟到结果到达**另一个实例**（或进程重启后）：凭 ledger 里的提交记录归档为 ABORTED。

        关键：本路径**不触碰版本状态**（不调 set_lock / set_version_status），
        只追加归档记录与 WARN 事件——这正是「迟到结果绝不改变当前版本状态」的落地方式。
        """
        target_id = entry.get("target_id") or entry.get("target_id".upper()) or ""
        version = entry.get("version") or "v0"
        mode = entry.get("mode") or str(ReviewMode.MODEL)
        try:
            mode_value = ReviewMode.parse(mode) or ReviewMode.MODEL
        except ValueError:  # pragma: no cover - 防御
            mode_value = ReviewMode.MODEL
        attempt = ReviewAttempt(
            attempt_id=bridge.attempt_id(target_id or "LATE", version, 1),
            review_cycle_id=cycle_id,
            mode=mode_value,
            score=score,
            passed=passed if passed is not None else False,
            reason=f"迟到结果（跨实例归档，source={source}）：{reason}",
            backend_ts=self._now(),
            operator=operator,
            candidate_version=version,
            error_class=error_class,
            superseded=True,
        )
        stub = {
            "review_cycle_id": cycle_id,
            "review_state": entry.get("state") or ReviewState.ABORTED.value,
            "finalization_token": entry.get("token"),
            "target_kind": entry.get("target_kind") or "asset",
            "target_id": target_id,
            "version": version,
            "mode": mode,
            "event_log": self.events,
        }
        ledger = self._ledger if self._ledger is not None else get_ledger(self._ledger_path)
        outcome = archive_late_result(
            stub, store=None, ledger=ledger, reason=reason, attempt=attempt.to_dict()
        )
        entry_record = {
            "source": source,
            "reason": reason,
            "review_cycle_id": cycle_id,
            "asset_id": target_id,
            "version": version,
            "state_after": entry.get("state"),
            "orphan_instance": True,
            "archive": outcome,
            "attempt": attempt.to_dict(),
        }
        self._late.setdefault(cycle_id, []).append(entry_record)
        return {
            "accepted": False,
            "committed": False,
            "reason": reason,
            "archived": True,
            "archive_state": ReviewState.ABORTED.value,
            "wrote_version_state": False,
            "orphan_instance": True,
            "state": entry.get("state"),
            "review_cycle_id": cycle_id,
            "asset_id": target_id,
            "version": version,
            "attempt": attempt.to_dict(),
            "archive": outcome,
        }

    def _retire(self, cycle_id: str, record: ReviewRecord, cycle: ReviewCycle, *, reason: str) -> None:
        with self._reg_lock:
            self._superseded.setdefault(cycle_id, []).append(record)
            self._records.pop(cycle_id, None)
            self._cycles.pop(cycle_id, None)
            self._commit_results.pop(cycle_id, None)
            self._concluded.pop(cycle_id, None)
            self._round_open.discard(cycle_id)
        self._emit(
            "review_cycle_retired",
            object_id=record.target_id,
            version=record.version,
            mode=record.mode,
            severity="WARN",
            detail={"review_cycle_id": cycle_id, "state": record.review_state.value,
                    "reason": reason},
        )

    def _fire(self, cycle: ReviewCycle, record: ReviewRecord, event: str, **payload: Any) -> ReviewState:
        state = cycle.fire(event, **payload)
        record.review_state = state
        record.updated_at = self._now()
        record.__post_init__()  # 重新跑语义守卫（ALLOWED 必须携带 lock_source 等）
        return state

    def _cycle_dict(self, record: ReviewRecord, cycle: ReviewCycle) -> dict:
        data = record.to_dict()
        data.update(
            {
                "lock_version": record.version,
                "event_log": self.events,
                "stage": Stage.ASSET_REVIEW,
                "downstream": self._downstream,
            }
        )
        if self._ledger is not None:
            data["ledger"] = self._ledger
        elif self._ledger_path is not None:
            data["ledger_path"] = str(self._ledger_path)
        return data

    def _ledger_for(self, record: ReviewRecord):
        if self._ledger is not None:
            return self._ledger
        return get_ledger(self._ledger_path)

    def _window(self, cycle_id: str) -> dict:
        window = self._human_windows.get(cycle_id)
        if window is None:
            window = {
                "opened_ts": self._now(),
                "last_valid_op_ts": None,
                "valid_ops": 0,
                "ignored_ops": 0,
                "last_kind": None,
            }
            self._human_windows[cycle_id] = window
        return window

    def _anchor_ts(self, cycle_id: str) -> str:
        window = self._window(cycle_id)
        if self._cfg.timeout_pause_on_active_op and window.get("last_valid_op_ts"):
            return window["last_valid_op_ts"]
        return window["opened_ts"]

    def _new_attempt(
        self,
        record: ReviewRecord,
        mode: ReviewMode,
        *,
        score: float | None,
        passed: bool,
        reason: str,
        operator: str | None = None,
        error_class: ErrorClass | None = None,
    ) -> ReviewAttempt:
        seq = self._attempt_seq.get(record.review_cycle_id, 0) + 1
        self._attempt_seq[record.review_cycle_id] = seq
        return ReviewAttempt(
            attempt_id=bridge.attempt_id(record.target_id, record.version, seq),
            review_cycle_id=record.review_cycle_id,
            mode=mode,
            score=score,
            passed=passed,
            reason=reason,
            backend_ts=self._now(),  # 统一后端时间戳（服务端 now）
            operator=operator,
            candidate_version=record.version,
            error_class=error_class,
        )

    def _validate_material(self, material: dict) -> list[dict]:
        errors: list[dict] = []
        if not material:
            errors.append({"path": "material", "code": "EMPTY", "message": "material 为空，无法做确定性校验"})
            return errors
        file_path = material.get("file_path") or material.get("path")
        content_hash = material.get("content_hash")
        if not file_path and not content_hash:
            errors.append({
                "path": "file_path/content_hash",
                "code": "MISSING",
                "message": "既没有 file_path 也没有 content_hash：无法定位并校验被审核版本",
            })
        if content_hash is not None and not _HASH_RE.match(str(content_hash)):
            errors.append({
                "path": "content_hash",
                "code": "BAD_FORMAT",
                "message": f"content_hash 格式非法：{content_hash!r}；要求 sha256:<64位小写hex>",
            })
        return errors

    @staticmethod
    def _sanitize_hash(material: dict) -> str | None:
        value = (material or {}).get("content_hash")
        if value is None:
            return None
        return str(value) if _HASH_RE.match(str(value)) else None

    def _guard_candidate_limit(self, obj_id: str) -> None:
        limit = int(self._cfg.model_candidate_limit)
        current = self.counters.get(obj_id, CounterKind.CNT1)
        if current >= limit:
            best = self._best_candidate(obj_id)
            raise CandidateLimitReached(
                f"CNT1={current} 已达模型候选上限 {limit}：不得再创建新的模型候选版本；"
                f"应使用已选兜底版本（{best['version'] if best else '无候选记录'}）。"
                "方案第七部分·一·6：达到上限后从候选里选最高分，不再新增候选。",
                best_candidate=best,
            )

    def _best_candidate(self, obj_id: str) -> dict | None:
        candidates = self._candidates.get(obj_id) or []
        if not candidates:
            return None
        return max(candidates, key=lambda c: (float(c["score"]), -int(c["round"])))

    def _invoke_model_review(self, material: dict) -> dict:
        payload: Any
        if self._model_review is not None:
            try:
                payload = self._model_review(material)
            except ReviewTechnicalError:
                raise
            except Exception as exc:
                raise ReviewTechnicalError(
                    f"模型审核执行失败：{type(exc).__name__}: {exc}",
                    error_class=ErrorClass.MODEL_API,
                ) from exc
        else:
            payload = material.get("model_result")
            if payload is None:
                raise ReviewTechnicalError(
                    "未注入 model_review 且 material 未提供 model_result："
                    "拒绝把「没有结果」当成审核通过",
                    error_class=ErrorClass.MODEL_API,
                )
        if not isinstance(payload, dict):
            raise ReviewTechnicalError(
                f"模型审核结果必须是 dict，实际 {type(payload).__name__}", error_class=ErrorClass.MODEL_API
            )
        error_class = payload.get("error_class")
        if error_class is not None:
            try:
                parsed = ErrorClass.parse(error_class)
            except ValueError as exc:
                raise ReviewTechnicalError(
                    f"模型审核结果 error_class 非法：{error_class!r}", error_class=ErrorClass.MODEL_API
                ) from exc
            if parsed is not ErrorClass.QUALITY_REVIEW:
                raise ReviewTechnicalError(
                    f"模型审核技术失败（{parsed.value}）：{payload.get('error') or payload}",
                    error_class=parsed,
                )
        if "score" not in payload:
            raise ReviewTechnicalError(
                "模型审核结果缺少 score 字段：无法判定阈值，按技术失败处理（绝不默认通过）",
                error_class=ErrorClass.MODEL_API,
            )
        try:
            score = float(payload["score"])
        except (TypeError, ValueError) as exc:
            raise ReviewTechnicalError(
                f"模型审核 score 不是数字：{payload['score']!r}", error_class=ErrorClass.INPUT_VALIDATION
            ) from exc
        passed = payload.get("passed")
        if passed is None:
            passed = score >= float(self._cfg.score_threshold)
        return {"score": score, "passed": bool(passed)}

    def _now(self) -> str:
        value = self._clock()
        return value if isinstance(value, str) else str(value)

    def _now_dt(self) -> datetime:
        return self._parse_dt(self._now())

    @staticmethod
    def _parse_dt(value: Any) -> datetime:
        return _to_dt(value)

    def _emit(
        self,
        event: str,
        *,
        severity: str = "INFO",
        stage: Stage | None = Stage.ASSET_REVIEW,
        object_id: Any = None,
        version: Any = None,
        mode: Any = None,
        detail: dict | None = None,
    ) -> None:
        try:
            self.events.emit(event, stage=stage, object_id=object_id, version=version,
                             mode=mode, severity=severity, detail=detail)
        except Exception as exc:  # pragma: no cover - 事件日志绝不阻断主流程
            _LOG.warning("审核事件写入失败（不阻断主流程）：%s", exc)

    def _result(
        self,
        record: ReviewRecord,
        cycle: ReviewCycle,
        *,
        extra: dict | None = None,
    ) -> dict:
        commit = self._commit_results.get(record.review_cycle_id)
        if commit is None:
            commit = self._pending_commit.get(record.review_cycle_id)
        result = {
            "state": record.review_state,
            "review_cycle_id": record.review_cycle_id,
            "score": record.score,
            "reason": record.reason,
            "lock_source": record.lock_source,
            "asset_id": record.target_id,
            "version": record.version,
            "mode": record.mode,
            "cnt1": self.counters.get(record.target_id, CounterKind.CNT1),
            "committed": (commit or {}).get("committed"),
            "lock_id": (commit or {}).get("lock_id"),
            "finalization": commit,
            "record": record.to_dict(),
            "arb_winner": record.arb_winner,
            "abort_id": record.abort_id,
            # --- 以下字段让返回值**本身就是一个合法的 commit_final 输入**（契约缺口 G-4：
            # cycle dict 字段未定义）。`review_state` 与 `state` 同值，`target_id/target_kind/
            # content_hash/finalization_token/lock_version` 供唯一提交点直接使用。
            "review_state": record.review_state,
            "target_kind": record.target_kind,
            "target_id": record.target_id,
            "content_hash": record.content_hash,
            "finalization_token": record.finalization_token,
            "lock_version": record.version,
            "event_log": self.events,
        }
        if self._ledger is not None:
            result["ledger"] = self._ledger
        elif self._ledger_path is not None:
            result["ledger_path"] = str(self._ledger_path)
        fallback_version = self._fallback_version.get(record.review_cycle_id)
        if fallback_version is not None:
            result["fallback_version"] = fallback_version
            # FALLBACK 的结论分数 = 被选中候选的分数（方案第七部分·一·6）
            result["score"] = self._fallback_score.get(record.review_cycle_id, result["score"])
            result["lock_source"] = LockSource.FALLBACK_PASS
            result["fallback_candidates"] = self.candidates(record.target_id)
        fallback_commit = self._fallback_commit.get(record.review_cycle_id)
        if fallback_commit is not None:
            result["committed"] = fallback_commit.get("committed")
            result["lock_id"] = fallback_commit.get("lock_id")
            result["fallback_commit"] = fallback_commit
        if extra:
            result.update(extra)
        return result

    # ------------------------------------------------------------------
    # 断点恢复
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._reg_lock:
            return {
                "records": [r.to_dict() for r in self._records.values()],
                "cycles": [c.snapshot() for c in self._cycles.values()],
                "counters": self.counters.snapshot(),
                "activity": copy.deepcopy(self._human_windows),
                "candidates": copy.deepcopy(self._candidates),
                "concluded": dict(self._concluded),
                "attempt_seq": dict(self._attempt_seq),
            }

    def restore(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ReviewError("restore 需要 dict 快照")
        records: dict[str, ReviewRecord] = {}
        for item in data.get("records") or []:
            record = ReviewRecord.from_dict(item)
            records[record.review_cycle_id] = record
        cycles: dict[str, ReviewCycle] = {}
        for item in data.get("cycles") or []:
            cycle = ReviewCycle.restore(item, clock=self._clock, event_log=self.events)
            cycles[cycle.review_cycle_id] = cycle
        with self._reg_lock:
            self._records = records
            self._cycles = cycles
            self._human_windows = copy.deepcopy(data.get("activity") or {})
            self._candidates = copy.deepcopy(data.get("candidates") or {})
            self._concluded = dict(data.get("concluded") or {})
            self._attempt_seq = {str(k): int(v) for k, v in (data.get("attempt_seq") or {}).items()}
        self.counters.restore(data.get("counters") or {})

    @staticmethod
    def _derive_ledger_path(store: Any):
        """从 store 推导落盘 ledger 路径（跨进程唯一提交所需）。

        路径 = `<项目目录>/review/finalizations.json`，其中项目目录来自
        `ProjectPaths.project_dir`。

        ⚠️ 关键隔离点：真实 `ProjectPaths.root` 是 **`projects/` 基目录**（多个项目共用），
        绝不能把 ledger 放在那里 —— 那会让不同项目共享提交权，直接违反 R5 项目隔离。
        因此优先 `project_dir`；只有在拿不到 project_id 时才退回 root。
        若 store 完全不暴露目录，则不落盘，`commit_final` 返回 `durable=False` 如实上报
        （绝不假装跨进程安全）。
        """
        if store is None:
            return None
        containers: list[Any] = []
        for attr in ("paths", "project", "project_paths"):
            value = getattr(store, attr, None)
            if value is not None:
                containers.append(value)
        containers.append(store)

        for container in containers:
            project_id = getattr(store, "project_id", None)
            if isinstance(container, (str, Path)):
                candidate: Path | None = Path(str(container))
            else:
                project_id = getattr(container, "project_id", None) or project_id
                candidate = None
                project_dir = getattr(container, "project_dir", None)
                if isinstance(project_dir, (str, Path)) and str(project_dir):
                    candidate = Path(str(project_dir))
                else:
                    for sub in ("root", "base", "path", "dir"):
                        value = getattr(container, sub, None)
                        if isinstance(value, (str, Path)) and str(value):
                            candidate = Path(str(value))
                            break
            if candidate is None:
                continue
            try:
                if project_id and candidate.name != str(project_id):
                    nested = candidate / str(project_id)
                    if nested.exists():
                        candidate = nested
                if candidate.exists() or candidate.parent.exists():
                    return candidate / "review" / "finalizations.json"
            except OSError:  # pragma: no cover - 防御
                continue
        return None


__all__ = ["AssetReviewer", "VALID_HUMAN_OP_KINDS", "NON_ACTIVITY_KINDS"]
