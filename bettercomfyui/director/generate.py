"""分段视频生成（P，契约 §7 `SegmentGenerator`）。

红线：
1. **R3 门禁**：只允许 `resolve_lock()` 通过的素材版本进入生成；未 LOCK -> 抛
   `LockGateError`，且在提交任何作业**之前**失败（不产生副作用）。
2. **技术失败不消耗质量计数**：API 失败 / 节点异常 / 超时 / 文件损坏 只增加
   *独立技术重试计数*（`TechRetryCounter`），`CNT2`/`CNT3` 一动不动。
3. 技术失败必须有 `ErrorClass`；执行器返回 `UNKNOWN` 一律按技术失败处理。
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from bettercomfyui.director.deps import (
    AssetLockResolver,
    CounterLike,
    IdFactory,
    default_ids,
    resolve_lock_resolver,
)
from bettercomfyui.director.errors import DirectorDependencyError, LockGateError
from bettercomfyui.director.events import EventRecorder
from bettercomfyui.director.plan import ensure_segment
from bettercomfyui.director.settings import cfg_get
from bettercomfyui.executors.base import (
    Executor,
    ExecutorCapabilityError,
    ExecutorError,
    JobRef,
    JobState,
    validate_capabilities,
)
from bettercomfyui.models.entities import Config, Segment, SegmentAttempt, now_iso
from bettercomfyui.models.enums import (
    CounterKind,
    ErrorClass,
    JobStatus,
    SegmentSource,
    SegmentStatus,
)

__all__ = [
    "SegmentGenerator",
    "TechRetryCounter",
    "classify_error",
    "classify_failure",
    "content_hash_of_file",
]


def content_hash_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def classify_error(source: Any) -> ErrorClass:
    """把异常 / JobState 分类为契约 §6 的 ErrorClass。绝不返回 None。"""
    if isinstance(source, JobState):
        return source.error_class or ErrorClass.COMFY_WORKFLOW
    if isinstance(source, ExecutorError):
        return source.error_class
    # 注意：TimeoutError 在 Python 3.11 是 OSError 的子类，必须先判超时，
    # 否则超时会被误分类成 FILE_ENCODING（文件/编码）。
    if isinstance(source, TimeoutError):
        return ErrorClass.COMFY_WORKFLOW
    if isinstance(source, (UnicodeDecodeError, UnicodeError)):
        return ErrorClass.FILE_ENCODING
    if isinstance(source, (FileNotFoundError, IsADirectoryError, PermissionError, OSError)):
        return ErrorClass.FILE_ENCODING
    if isinstance(source, (ValueError, KeyError, TypeError)):
        return ErrorClass.INPUT_VALIDATION
    return ErrorClass.COMFY_WORKFLOW


def classify_failure(exc: Any, job_state: JobState | None = None) -> ErrorClass:
    """技术失败分类：优先信异常自带的分类，其次信作业状态，最后按异常类型推断。"""
    if isinstance(exc, ExecutorError):
        return exc.error_class
    if job_state is not None and job_state.error_class is not None:
        return job_state.error_class
    return classify_error(exc)


class TechRetryCounter:
    """独立技术重试计数（第四类计数，**不属于** CNT1/CNT2/CNT3）。

    可选落盘（仅用于断点恢复），默认纯内存；任何情况下都不写进 CNT2/CNT3。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._counts: dict[str, int] = {}
        self._log: list[dict] = []
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._counts = {str(k): int(v) for k, v in (raw.get("counts") or {}).items()}
            except (OSError, ValueError, json.JSONDecodeError):
                self._counts = {}

    def increment(self, obj_id: str, *, reason: str | None = None, error_class: Any = None) -> int:
        key = str(obj_id)
        self._counts[key] = self._counts.get(key, 0) + 1
        self._log.append(
            {
                "ts": now_iso(),
                "obj_id": key,
                "reason": reason,
                "error_class": str(error_class) if error_class is not None else None,
                "count": self._counts[key],
            }
        )
        self._flush()
        return self._counts[key]

    def get(self, obj_id: str) -> int:
        return self._counts.get(str(obj_id), 0)

    def total(self) -> int:
        return sum(self._counts.values())

    def snapshot(self) -> dict:
        return {"counts": dict(self._counts), "log": list(self._log)}

    def reset(self, obj_id: str) -> None:
        self._counts.pop(str(obj_id), None)
        self._flush()

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class SegmentGenerator:
    """一次调用 = 一次候选生成尝试。"""

    def __init__(
        self,
        store: AssetLockResolver | None = None,
        counters: CounterLike | None = None,
        *,
        cfg: Config | dict | Any = None,
        ids: IdFactory | None = None,
        project_id: str | None = None,
        lock_resolver: Callable[[str, str], dict | None] | None = None,
        tech_retries: TechRetryCounter | None = None,
        events: EventRecorder | None = None,
        workflow_builder: Callable[[Segment, dict], dict] | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        poll_limit: int = 600,
        check_output_files: bool = True,
    ) -> None:
        self.store = store
        self.counters = counters
        self.cfg = cfg
        self.cfg_source = cfg
        self._ids = ids
        self.project_id = str(project_id or "")
        self.lock_resolver = resolve_lock_resolver(store, lock_resolver)
        self.tech_retries = tech_retries if tech_retries is not None else TechRetryCounter()
        self.events = events if events is not None else EventRecorder(namespace="BETTERCOMFYUI")
        self.workflow_builder = workflow_builder or _default_workflow_builder
        self._sleep = sleeper or time.sleep
        #: 单调时钟。可注入，便于测试"任务超时"而不必真的等 900 秒。
        self._clock = clock or time.monotonic
        self.poll_limit = int(poll_limit)
        self.check_output_files = bool(check_output_files)

    # -- 配置 -------------------------------------------------------------
    @property
    def ids(self) -> IdFactory:
        if self._ids is None:
            self._ids = default_ids()
        return self._ids

    @property
    def tech_retry_limit(self) -> int:
        return int(cfg_get(self.cfg, "global.tech_retry_limit", 3))

    def _count(self, segment_id: str, kind: CounterKind) -> int | None:
        if self.counters is None:
            return None
        return int(self.counters.get(segment_id, kind))

    # -- R3 门禁 ----------------------------------------------------------
    def assert_assets_locked(self, segment: Segment) -> list[dict]:
        """校验段内全部素材引用都通过 resolve_lock（R3）。不通过直接抛错。"""
        refs = list(segment.asset_refs or [])
        verified: list[dict] = []
        for ref in refs:
            asset_id = ref.get("asset_id")
            version = ref.get("version")
            if not asset_id or not version:
                raise LockGateError(
                    f"段 {segment.segment_id} 的素材引用缺少 asset_id/version：{ref!r}",
                    detail={"segment_id": segment.segment_id, "ref": ref},
                )
            lock = self.lock_resolver(asset_id, version)
            if not lock:
                raise LockGateError(
                    f"段 {segment.segment_id} 引用了未 LOCK 的素材版本 {asset_id}@{version}："
                    "R3 禁止未审核素材进入生成",
                    detail={"segment_id": segment.segment_id, "asset_id": asset_id, "version": version},
                )
            declared = ref.get("content_hash")
            actual = lock.get("content_hash") if isinstance(lock, dict) else None
            if declared and actual and declared != actual:
                raise LockGateError(
                    f"段 {segment.segment_id} 的素材 {asset_id}@{version} 内容指纹不一致："
                    f"引用声明 {declared}，LOCK 实际 {actual}（R3 内容指纹校验）",
                    detail={"segment_id": segment.segment_id, "asset_id": asset_id, "version": version},
                )
            verified.append({"asset_id": asset_id, "version": version, "lock": dict(lock) if isinstance(lock, dict) else lock})
        return verified

    # -- 生成 -------------------------------------------------------------
    def generate(
        self,
        segment: Segment,
        *,
        executor: Executor,
        source: SegmentSource | str | None = None,
        params: dict | None = None,
    ) -> dict:
        if not isinstance(segment, Segment):
            # 兼容契约 §7 的 dict 调用方；dict 会经 Segment.from_dict 还原（保留守卫）
            segment = ensure_segment(segment, where="SegmentGenerator.generate")
        seg_id = segment.segment_id
        caps = validate_capabilities(executor.capabilities())
        if not caps.get("video"):
            raise ExecutorCapabilityError(
                f"执行器 {executor.name!r} 声明 video=False，无法生成视频段：{caps.get('notes')}",
                detail={"executor": executor.name, "capabilities": caps},
            )
        verified_refs = self.assert_assets_locked(segment)  # 先门禁，后副作用

        run_params = {"segment_seconds": segment.segment_duration, **(segment.generation_params or {})}
        run_params.update(params or {})
        workflow = self.workflow_builder(segment, run_params)
        attempt_index = len(segment.attempts) + 1
        idem = self.ids.idempotency_key(
            "SEGMENT_GENERATE",
            {
                "segment_id": seg_id,
                "version": segment.current_version,
                "prompt": segment.prompt,
                "asset_refs": segment.asset_refs,
                "params": run_params,
                "attempt_index": attempt_index,
                "executor": executor.name,
            },
        )
        self.events.emit(
            "SEGMENT_GENERATE_SUBMIT",
            stage="SEGMENT_GENERATE",
            object_id=seg_id,
            version=segment.current_version,
            detail={"executor": executor.name, "idempotency_key": idem, "attempt_index": attempt_index},
        )

        # 1) 提交；连接失败等一律按技术失败处理（不消耗 CNT2/CNT3）
        try:
            ref = executor.submit(workflow, run_params, idempotency_key=idem)
        except Exception as exc:  # noqa: BLE001 - 执行器层异常全部归类为技术失败
            return self._technical_failure(segment, exc=exc, where="submit", executor=executor)

        # 2) 轮询到终态
        try:
            state = self._poll_until_terminal(ref, executor, segment=segment)
        except Exception as exc:  # noqa: BLE001
            return self._technical_failure(
                segment, exc=exc, where="poll", executor=executor, ref=ref
            )

        if not state.is_success:
            return self._technical_failure(
                segment,
                exc=ExecutorError(
                    f"作业未成功：{state.status}（{state.error or '无错误说明'}）",
                    error_class=state.error_class or classify_error(state),
                ),
                where=f"job:{state.status}",
                executor=executor,
                ref=ref,
                job_state=state,
            )

        # 3) 产出完整性（文件损坏属于技术失败，不消耗质量计数）
        try:
            outputs, content_hash, first_frame, last_frame = self._inspect_outputs(state)
        except Exception as exc:  # noqa: BLE001
            return self._technical_failure(
                segment, exc=exc, where="integrity", executor=executor, ref=ref, job_state=state
            )

        version = self._version_for(segment, attempt_index)
        attempt_id = self.ids.attempt_id(seg_id, version, attempt_index)
        new_segment = copy.deepcopy(segment)
        attempt = SegmentAttempt(
            attempt_id=attempt_id,
            segment_id=seg_id,
            version=version,
            index=attempt_index,
            job_id=ref.job_id,
            executor=executor.name,
            video_path=str(outputs[0]) if outputs else None,
            first_frame_path=str(first_frame) if first_frame else None,
            last_frame_path=str(last_frame) if last_frame else None,
            content_hash=content_hash,
            passed=None,
        )
        new_segment.attempts = list(segment.attempts) + [attempt]
        new_segment.current_version = version
        new_segment.candidate_version = version
        new_segment.status = SegmentStatus.GENERATED
        new_segment.source = SegmentSource.parse(source) if source is not None else segment.source
        new_segment.updated_at = now_iso()

        self.events.emit(
            "SEGMENT_GENERATED",
            stage="SEGMENT_GENERATE",
            object_id=seg_id,
            version=version,
            detail={
                "attempt_id": attempt_id,
                "executor": executor.name,
                "mock": bool(state.mock),
                "content_hash": content_hash,
                "outputs": [str(p) for p in outputs],
            },
        )
        return {
            "segment_id": seg_id,
            "version": version,
            "attempt_id": attempt_id,
            "status": SegmentStatus.GENERATED,
            "source": new_segment.source,
            "job_ref": ref,
            "job_state": state,
            "outputs": [str(p) for p in outputs],
            "video_path": str(outputs[0]) if outputs else None,
            "first_frame": str(first_frame) if first_frame else None,
            "last_frame": str(last_frame) if last_frame else None,
            "content_hash": content_hash,
            "mock": bool(state.mock),
            "technical_failure": False,
            "error": None,
            "error_class": None,
            "tech_retry": self.tech_retries.get(seg_id),
            "verified_assets": verified_refs,
            "cnt2": self._count(seg_id, CounterKind.CNT2),
            "cnt3": self._count(seg_id, CounterKind.CNT3),
            "segment": new_segment,
            "idempotency_key": idem,
        }

    def generate_with_retries(
        self,
        segment: Segment,
        *,
        executor: Executor,
        source: SegmentSource | str | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        """带**上限**的技术重试；达上限返回最后一次失败结果。绝不无限重试。"""
        limit = int(max_attempts if max_attempts is not None else self.tech_retry_limit)
        if limit < 1:
            raise DirectorDependencyError(f"技术重试上限必须 >=1，收到 {limit}")
        result: dict | None = None
        made = 0
        for _ in range(limit):
            made += 1
            result = self.generate(segment, executor=executor, source=source)
            if not result["technical_failure"]:
                break
            newest = result.get("segment")
            if newest is not None:
                segment = newest
        assert result is not None
        result["attempts_made"] = made
        result["tech_retry_limit"] = limit
        result["retry_exhausted"] = bool(result["technical_failure"] and made >= limit)
        return result

    # -- 内部 -------------------------------------------------------------
    def _version_for(self, segment: Segment, attempt_index: int) -> str:
        """版本口径：初始候选 = 计划里的 current_version（v1），重生成才 +1。

        依据方案参数表六：「CNT2/CNT3 重生成上限各 3 次，**不含初始段版本**」。
        判定依据是数据而非调用次数：只有当前版本已有对应候选尝试，才算「已有初始版本」。
        """
        if attempt_index == 1 and not any(
            a.version == segment.current_version for a in segment.attempts
        ):
            return segment.current_version
        return self.ids.next_version(segment.current_version)

    def rework_timeout_s(self, segment: Segment | None = None) -> float:
        """取本次任务应使用的超时秒数。

        优先级：Segment.overrides.timeout_s > global.task_timeout_s（默认 900）。

        global.task_timeout_s 此前**配了没人读**（ROUND18 棘轮发现）——
        没有看门狗意味着一个卡死的任务会永久挂住整条流水线。
        """
        if segment is not None:
            try:
                # SegmentOverrides.resolve(cfg) 返回 (生效值, 被覆盖字段)
                resolved, _overridden = segment.overrides.resolve(self.cfg)
                value = (resolved or {}).get("timeout_s")
                if value is not None:
                    timeout = float(value)
                    if timeout > 0:
                        return timeout
            except Exception:  # noqa: BLE001 - 取不到就退回全局
                pass
        try:
            return float(cfg_get(self.cfg, "global.task_timeout_s", 900.0))
        except (TypeError, ValueError):
            return 900.0

    def _poll_until_terminal(
        self, ref: JobRef, executor: Executor, *, segment: Segment | None = None
    ) -> JobState:
        # 任务超时看门狗（图一 SG8 + `global.task_timeout_s`）。
        # 没有它，一个卡死的任务会永久挂住整条流水线 —— 用户只看到"没反应"。
        timeout_s = self.rework_timeout_s(segment)
        started = self._clock()
        seg_id = getattr(segment, "segment_id", None)
        state: JobState | None = None
        for _ in range(self.poll_limit):
            state = executor.poll(ref)
            if state.is_terminal:
                return state
            elapsed = self._clock() - started
            if timeout_s > 0 and elapsed >= timeout_s:
                raise ExecutorError(
                    f"任务超时：段 {seg_id} 轮询 {elapsed:.1f}s 超过上限 "
                    f"{timeout_s:.1f}s 仍未终态（最后状态 {state.status}）。\n"
                    "超时来自 global.task_timeout_s 或 Segment.overrides.timeout_s；"
                    "已按失败处理，不无限等待。",
                    error_class=ErrorClass.COMFY_WORKFLOW,
                    detail={"segment_id": seg_id, "timeout_s": timeout_s,
                            "elapsed_s": round(elapsed, 3)},
                )
            self._sleep(0.0)
        assert state is not None
        raise ExecutorError(
            f"轮询超过上限 {self.poll_limit} 次仍未终态，最后状态 {state.status}",
            error_class=ErrorClass.COMFY_WORKFLOW,
        )

    def _inspect_outputs(self, state: JobState) -> tuple[list[Path], str | None, Path | None, Path | None]:
        outputs = [Path(p) for p in state.outputs]
        if not outputs:
            raise ExecutorError(
                "执行器报告 SUCCEEDED 但没有任何输出文件（禁止当作成功）",
                error_class=ErrorClass.FILE_ENCODING,
            )
        if self.check_output_files:
            for path in outputs:
                if not path.exists():
                    raise ExecutorError(
                        f"输出文件不存在：{path}（文件损坏/丢失属于技术失败）",
                        error_class=ErrorClass.FILE_ENCODING,
                        detail={"path": str(path)},
                    )
                if path.is_file() and path.stat().st_size == 0:
                    raise ExecutorError(
                        f"输出文件为空：{path}（文件损坏属于技术失败）",
                        error_class=ErrorClass.FILE_ENCODING,
                        detail={"path": str(path)},
                    )
        video = outputs[0]
        content_hash = content_hash_of_file(video) if self.check_output_files and video.is_file() else None
        first_frame = next((p for p in outputs if p.name.startswith("frame_first")), None)
        last_frame = next((p for p in outputs if p.name.startswith("frame_last")), None)
        return outputs, content_hash, first_frame, last_frame

    def _technical_failure(
        self,
        segment: Segment,
        *,
        exc: Any,
        where: str,
        executor: Executor,
        ref: JobRef | None = None,
        job_state: JobState | None = None,
    ) -> dict:
        error_class = classify_failure(exc, job_state)
        count = self.tech_retries.increment(
            segment.segment_id, reason=f"{where}:{type(exc).__name__}", error_class=error_class
        )
        # 显式确认：技术失败不得触碰 CNT2/CNT3（只读一次用于回报，不做任何写操作）
        cnt2 = self._count(segment.segment_id, CounterKind.CNT2)
        cnt3 = self._count(segment.segment_id, CounterKind.CNT3)
        self.events.emit(
            "SEGMENT_TECH_FAILURE",
            stage="SEGMENT_GENERATE",
            object_id=segment.segment_id,
            version=segment.current_version,
            severity="WARN",
            detail={
                "where": where,
                "error_class": str(error_class),
                "error": str(exc),
                "executor": executor.name,
                "tech_retry": count,
                "cnt2": cnt2,
                "cnt3": cnt3,
                "note": "技术失败不消耗 CNT2/CNT3",
            },
        )
        new_segment = copy.deepcopy(segment)
        failed_attempt = SegmentAttempt(
            attempt_id=f"TECHFAIL_{segment.segment_id}_{len(segment.attempts) + 1}",
            segment_id=segment.segment_id,
            version=segment.current_version,
            index=len(segment.attempts) + 1,
            job_id=ref.job_id if ref else None,
            executor=executor.name,
            error_class=error_class,
            error=f"{where}: {exc}",
        )
        new_segment.attempts = list(segment.attempts)
        new_segment.status = SegmentStatus.FAILED
        new_segment.updated_at = now_iso()
        return {
            "segment_id": segment.segment_id,
            "version": segment.current_version,  # 技术失败不产生新版本
            "attempt_id": None,
            "status": SegmentStatus.FAILED,
            "source": segment.source,
            "job_ref": ref,
            "job_state": job_state,
            "outputs": [],
            "video_path": None,
            "first_frame": None,
            "last_frame": None,
            "content_hash": None,
            "mock": bool(job_state.mock) if job_state is not None else False,
            "technical_failure": True,
            "technical_failure_where": where,
            "error": str(exc),
            "error_class": error_class,
            "tech_retry": count,
            "tech_retry_limit": self.tech_retry_limit,
            "failed_attempt": failed_attempt,
            "cnt2": cnt2,
            "cnt3": cnt3,
            "segment": new_segment,
        }


def _default_workflow_builder(segment: Segment, params: dict) -> dict:
    """默认工作流请求（结构化的段生成请求；具体 ComfyUI 图由适配层/流水线填充）。"""
    return {
        "kind": "segment_generate",
        "segment_id": segment.segment_id,
        "segment_index": segment.segment_index,
        "prompt": segment.prompt,
        "asset_refs": [dict(r) for r in segment.asset_refs or []],
        "first_frame": segment.first_frame,
        "last_frame": segment.last_frame,
        "params": dict(params),
    }
