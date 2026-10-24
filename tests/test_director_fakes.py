"""共享测试替身（**不是测试模块**：本文件不定义任何 `test_` 函数）。

放在 `tests/test_director_fakes.py` 是为了遵守 task-5 的独占写入范围
（`tests/test_director_*.py`），同时避免与 prompt-assets 维护的
`tests/_fake_store.py` 冲突（Lead 明确要求不要覆写那个文件）。

替身只实现契约 §4/§6 的最小接口；真实 store/review 就绪后由
`tests/test_director_integration.py` 切换真实实现重跑。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bettercomfyui.executors.base import ExecutorError, JobRef, JobState
from bettercomfyui.models.entities import Config, Segment, SegmentAttempt
from bettercomfyui.models.enums import (
    CounterKind,
    ErrorClass,
    JobStatus,
    SegmentSource,
    SegmentStatus,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def make_config(**overrides: Any) -> Config:
    """真实 `models.entities.Config` + 允许覆盖点分路径（如 video_review.cnt3_limit=10）。"""
    cfg = Config()
    for path, value in overrides.items():
        head, _, tail = path.partition(".")
        if not tail:
            setattr(cfg, path, value)
            continue
        target = getattr(cfg, head)
        setattr(target, tail, value)
    return cfg


# ---------------------------------------------------------------------------
# review.CounterStore 替身
# ---------------------------------------------------------------------------


class FakeCounterStore:
    """契约 §6 CounterStore 的最小替身；记录每次写操作，便于断言「没被碰过」。"""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], int] = {}
        self.increments: list[tuple[str, str]] = []
        self.resets: list[tuple[str, str]] = []

    def increment(self, obj_id: str, kind: Any) -> int:
        key = (str(obj_id), str(CounterKind.parse(kind)))
        self.counts[key] = self.counts.get(key, 0) + 1
        self.increments.append(key)
        return self.counts[key]

    def get(self, obj_id: str, kind: Any) -> int:
        return self.counts.get((str(obj_id), str(CounterKind.parse(kind))), 0)

    def reset(self, obj_id: str, kind: Any) -> None:
        key = (str(obj_id), str(CounterKind.parse(kind)))
        self.counts.pop(key, None)
        self.resets.append(key)

    # 断言辅助
    def increments_of(self, kind: CounterKind) -> int:
        return sum(1 for _, k in self.increments if k == str(kind))

    def keys_touched(self) -> set[str]:
        return {obj for obj, _ in self.increments}


# ---------------------------------------------------------------------------
# store.ids 替身（契约 §2 格式）
# ---------------------------------------------------------------------------


class FakeIds:
    """严格按契约 §2 生成 ID；uuid8 用递增计数器保证可复现。"""

    def __init__(self, prefix: str = "abcd1234") -> None:
        self._n = 0
        self._prefix = prefix

    def _uuid8(self) -> str:
        self._n += 1
        return f"{self._prefix}{self._n:04d}"[:64]

    def chapter_id(self, index: int) -> str:
        return f"CH{int(index):03d}"

    def segment_id(self, project_id: str, chapter_no: int, seg_no: int) -> str:
        return f"{project_id}_CH{int(chapter_no)}_SEG{int(seg_no)}"

    def next_version(self, version: str | None) -> str:
        if not version:
            return "v1"
        return f"v{int(str(version).lstrip('v')) + 1}"

    def attempt_id(self, segment_id: str, version: str, index: int) -> str:
        return f"ATTEMPT_{segment_id}_{version}_{int(index)}"

    def audit_id(self, segment_id: str, version: str) -> str:
        return f"AUDIT_{segment_id}_{version}"

    def review_cycle_id(self, target_kind: str, target_id: str, version: str) -> str:
        return f"{target_kind}:{target_id}:{version}"

    def new_cascade_id(self, segment_id: str) -> str:
        return f"CASCADE_{segment_id}_{self._uuid8()}"

    def cnt_id(self, obj_id: str, kind: Any) -> str:
        return f"CNT_{obj_id}_{CounterKind.parse(kind)}"

    def idempotency_key(self, namespace: str, payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return f"{namespace}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# store.AssetStore 替身
# ---------------------------------------------------------------------------


class FakeAssetStore:
    """只实现 R3 需要的 resolve_lock；LOCK 绑定 asset_id+version+content_hash。"""

    def __init__(self, locked: dict[tuple[str, str], dict] | None = None) -> None:
        self.locked: dict[tuple[str, str], dict] = dict(locked or {})
        self.resolve_calls: list[tuple[str, str]] = []

    def add_lock(
        self, asset_id: str, version: str, *, content_hash: str | None = None, lock_source: str = "HUMAN_PASS"
    ) -> dict:
        entry = {
            "asset_id": asset_id,
            "version": version,
            "content_hash": content_hash or ("sha256:" + "a" * 64),
            "lock_source": lock_source,
        }
        self.locked[(asset_id, version)] = entry
        return entry

    def clear_lock(self, asset_id: str, version: str) -> None:
        self.locked.pop((asset_id, version), None)

    def resolve_lock(self, asset_id: str, version: str) -> dict | None:
        self.resolve_calls.append((str(asset_id), str(version)))
        return self.locked.get((str(asset_id), str(version)))


def locked_assets_for(*pairs: tuple[str, str]) -> dict[str, dict]:
    """构造 plan_segments 用的 `locked_assets`（ref -> {asset_id, version}）。"""
    return {asset_id: {"asset_id": asset_id, "version": version} for asset_id, version in pairs}


# ---------------------------------------------------------------------------
# Segment 工厂
# ---------------------------------------------------------------------------


def make_segment(
    segment_id: str = "SEG1",
    index: int = 1,
    *,
    chapter_id: str = "CH001",
    prompt: str = "测试分镜提示词",
    asset_refs: list[dict] | None = None,
    current_version: str = "v1",
    dependency_refs: list[str] | None = None,
    attempts: list[SegmentAttempt] | None = None,
    status: SegmentStatus = SegmentStatus.PLANNED,
    cascade_marked: bool = False,
    source: SegmentSource = SegmentSource.NORMAL,
    overrides: Any = None,
) -> Segment:
    from bettercomfyui.models.entities import SegmentOverrides

    return Segment(
        segment_id=segment_id,
        segment_index=index,
        chapter_id=chapter_id,
        prompt=prompt,
        asset_refs=list(asset_refs if asset_refs is not None else [{"asset_id": "A1", "version": "v1"}]),
        current_version=current_version,
        dependency_refs=list(dependency_refs or []),
        attempts=list(attempts or []),
        status=status,
        cascade_marked=cascade_marked,
        source=source,
        overrides=(overrides if overrides is not None else SegmentOverrides()),
    )


def make_attempt(
    segment_id: str,
    version: str,
    index: int,
    *,
    score: float | None = None,
    passed: bool | None = None,
    content_hash: str | None = None,
    first_frame_path: str | None = None,
    last_frame_path: str | None = None,
) -> SegmentAttempt:
    return SegmentAttempt(
        attempt_id=f"ATTEMPT_{segment_id}_{version}_{index}",
        segment_id=segment_id,
        version=version,
        index=index,
        score=score,
        passed=passed,
        content_hash=content_hash,
        first_frame_path=first_frame_path,
        last_frame_path=last_frame_path,
        video_path=f"{segment_id}_{version}.mp4",
    )


# ---------------------------------------------------------------------------
# Executor 替身
# ---------------------------------------------------------------------------


class ScriptedExecutor:
    """确定性执行器替身：写真实文件、可控制成功/失败/UNKNOWN 与内容变化。

    注意：这是**测试替身**，`verified=False` 且带 `testdouble` 标记，
    不代表任何真实生成能力。
    """

    name = "scripted_testdouble"

    def __init__(
        self,
        workdir: Path,
        *,
        video_ok: bool = True,
        delta: bool = True,
        state_override: JobState | None = None,
        omit_outputs: bool = False,
        missing_outputs: bool = False,
        fail_error_class: ErrorClass = ErrorClass.COMFY_WORKFLOW,
        fail_message: str = "替身注入的执行失败",
    ) -> None:
        self.workdir = Path(workdir)
        self.video_ok = video_ok
        self.delta = delta
        self.state_override = state_override
        self.omit_outputs = omit_outputs
        self.missing_outputs = missing_outputs
        self.fail_error_class = fail_error_class
        self.fail_message = fail_message
        self.submits: list[dict] = []
        self.polls = 0
        self._jobs: dict[str, JobState] = {}

    def capabilities(self) -> dict:
        return {
            "image": True,
            "video": True,
            "first_last_frame": True,
            "tail_frames": 22,
            "verified": False,
            "notes": "测试替身（testdouble），不是真实生成能力；仅用于验证编排逻辑",
            "testdouble": True,
        }

    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef:
        self.submits.append({"workflow": workflow, "params": params, "idempotency_key": idempotency_key})
        n = len(self.submits)
        job_id = f"JOB_{n}"
        job_dir = self.workdir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        if self.state_override is not None:
            self._jobs[job_id] = self.state_override
            return JobRef(job_id=job_id, executor=self.name)
        if not self.video_ok:
            self._jobs[job_id] = JobState(
                status=JobStatus.FAILED, outputs=[], error=self.fail_message, error_class=self.fail_error_class
            )
            return JobRef(job_id=job_id, executor=self.name)
        token = str(n) if self.delta else "fixed"
        video = job_dir / "segment.mp4"
        video.write_bytes(f"scripted-video-{token}".encode("utf-8"))
        first = job_dir / "frame_first.png"
        last = job_dir / "frame_last.png"
        first.write_bytes(f"frame-first-{token}".encode("utf-8"))
        last.write_bytes(f"frame-last-{token}".encode("utf-8"))
        outputs = [] if self.omit_outputs else [video, first, last]
        if self.missing_outputs:
            outputs = [job_dir / "missing.mp4"]  # 报告成功但文件并不存在（文件损坏场景）
        self._jobs[job_id] = JobState(status=JobStatus.SUCCEEDED, progress=1.0, outputs=outputs)
        return JobRef(job_id=job_id, executor=self.name)

    def poll(self, ref: JobRef) -> JobState:
        self.polls += 1
        return self._jobs[ref.job_id]

    def cancel(self, ref: JobRef) -> bool:
        return True


class FailingExecutor:
    """每次 submit 都抛 ExecutorError 的替身（技术失败来源）。"""

    name = "failing_testdouble"

    def __init__(self, *, error_class: ErrorClass = ErrorClass.MODEL_API, message: str = "注入的技术失败") -> None:
        self.error_class = error_class
        self.message = message
        self.attempts = 0

    def capabilities(self) -> dict:
        return {
            "image": True,
            "video": True,
            "first_last_frame": True,
            "tail_frames": 22,
            "verified": False,
            "notes": "测试替身：总是失败，用于验证技术失败不消耗 CNT2/CNT3",
            "testdouble": True,
        }

    def submit(self, workflow: dict, params: dict, *, idempotency_key: str) -> JobRef:
        self.attempts += 1
        raise ExecutorError(self.message, error_class=self.error_class)

    def poll(self, ref: JobRef) -> JobState:  # pragma: no cover - 不会走到
        raise AssertionError("FailingExecutor.submit 已抛错，不应轮询")

    def cancel(self, ref: JobRef) -> bool:  # pragma: no cover
        return False


class NoVideoExecutor(ScriptedExecutor):
    """声明 video=False 的执行器替身（能力门禁用）。"""

    name = "no_video_testdouble"

    def capabilities(self) -> dict:
        caps = super().capabilities()
        caps["video"] = False
        caps["first_last_frame"] = False
        caps["notes"] = "测试替身：声明不具备视频能力，用于验证能力门禁"
        return caps


# ---------------------------------------------------------------------------
# 评分器替身
# ---------------------------------------------------------------------------


def fixed_scorer(score: float = 80.0, dims: tuple[str, ...] = ("画面连贯", "角色一致", "动作合理", "无闪烁")):
    """R1a 评分器替身：四个维度都给固定分。"""

    def scorer(segment: Segment, version: str) -> dict:
        return {d: score for d in dims}

    return scorer


def version_scorer(passing_versions: set[str], *, pass_score: float = 80.0, fail_score: float = 40.0):
    """按版本号给出通过/不通过（用于级联 N1 循环测试）。"""

    def scorer(segment: Segment, version: str) -> dict:
        score = pass_score if version in passing_versions else fail_score
        return {d: score for d in ("画面连贯", "角色一致", "动作合理", "无闪烁")}

    return scorer


def countdown_edge_scorer(*results: bool, score_pass: float = 90.0, score_fail: float = 10.0):
    """边界评分器替身：按顺序返回给定结果（边界验证测试用），耗尽后沿用最后一个。"""
    calls = {"n": 0}

    def scorer(seg_n: Segment, seg_n1: Segment) -> dict:
        idx = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return {"score": score_pass if results[idx] else score_fail}

    scorer.calls = calls  # type: ignore[attr-defined]
    return scorer


@pytest.fixture
def fakes_tmp(tmp_path: Path) -> Path:
    return tmp_path
