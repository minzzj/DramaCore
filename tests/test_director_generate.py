"""director/generate.py：R3 门禁 + 技术失败分类与独立技术重试计数（不碰 CNT2/CNT3）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.director.errors import DirectorDependencyError, LockGateError  # noqa: E402
from bettercomfyui.director.events import EventRecorder  # noqa: E402
from bettercomfyui.director.generate import (  # noqa: E402
    SegmentGenerator,
    TechRetryCounter,
    classify_error,
)
from bettercomfyui.executors.base import ExecutorCapabilityError, JobState  # noqa: E402
from bettercomfyui.models.enums import (  # noqa: E402
    CounterKind,
    ErrorClass,
    JobStatus,
    SegmentSource,
    SegmentStatus,
)

from test_director_fakes import (  # noqa: E402
    FailingExecutor,
    FakeAssetStore,
    FakeCounterStore,
    FakeIds,
    NoVideoExecutor,
    ScriptedExecutor,
    make_config,
    make_segment,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def build(tmp_path, *, cfg=None, check_output_files=True):
    counters = FakeCounterStore()
    store = FakeAssetStore()
    store.add_lock("A1", "v1", content_hash=HASH_A)
    events = EventRecorder(namespace="BETTERCOMFYUI")
    gen = SegmentGenerator(
        store,
        counters,
        cfg=cfg or make_config(),
        ids=FakeIds(),
        project_id="P",
        events=events,
        check_output_files=check_output_files,
    )
    segment = make_segment("P_CH1_SEG1", 1, asset_refs=[{"asset_id": "A1", "version": "v1"}])
    return gen, counters, store, segment, events


# ---------------------------------------------------------------------------
# R3 门禁
# ---------------------------------------------------------------------------


def test_r3_gate_blocks_unlocked_asset_before_any_submit(tmp_path):
    gen, counters, store, _, _ = build(tmp_path)
    store.clear_lock("A1", "v1")
    executor = ScriptedExecutor(tmp_path / "jobs")
    segment = make_segment("P_CH1_SEG1", 1, asset_refs=[{"asset_id": "A1", "version": "v1"}])
    with pytest.raises(LockGateError) as err:
        gen.generate(segment, executor=executor)
    assert "未 LOCK" in str(err.value)
    assert executor.submits == []  # 门禁失败不得产生任何副作用
    assert counters.increments == []


def test_r3_gate_rejects_content_hash_mismatch(tmp_path):
    gen, _, _, _, _ = build(tmp_path)
    segment = make_segment(
        "P_CH1_SEG1",
        1,
        asset_refs=[{"asset_id": "A1", "version": "v1", "content_hash": HASH_B}],
    )
    with pytest.raises(LockGateError) as err:
        gen.generate(segment, executor=ScriptedExecutor(tmp_path / "jobs"))
    assert "内容指纹" in str(err.value)


def test_r3_gate_rejects_reference_without_version(tmp_path):
    gen, _, _, _, _ = build(tmp_path)
    segment = make_segment("P_CH1_SEG1", 1)
    segment.asset_refs = [{"asset_id": "A1"}]
    with pytest.raises(LockGateError):
        gen.generate(segment, executor=ScriptedExecutor(tmp_path / "jobs"))


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_initial_generation_is_v1_then_regeneration_bumps(tmp_path):
    gen, counters, _, segment, events = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs")
    first = gen.generate(segment, executor=executor)
    assert first["status"] is SegmentStatus.GENERATED
    assert first["version"] == "v1"  # 初始段版本不含在重生成上限里
    assert first["attempt_id"] == "ATTEMPT_P_CH1_SEG1_v1_1"
    assert first["content_hash"].startswith("sha256:")
    assert first["mock"] is False
    assert first["cnt2"] == 0 and first["cnt3"] == 0
    assert first["source"] is SegmentSource.NORMAL
    assert Path(first["video_path"]).exists()

    second = gen.generate(first["segment"], executor=executor, source=SegmentSource.REGENERATED)
    assert second["version"] == "v2"
    assert second["source"] is SegmentSource.REGENERATED
    assert [a.version for a in second["segment"].attempts] == ["v1", "v2"]
    # 生成阶段绝不写任何 CNT
    assert counters.increments == []
    assert events.events("SEGMENT_GENERATED")


def test_capability_gate_blocks_video_less_executor(tmp_path):
    gen, _, _, segment, _ = build(tmp_path)
    executor = NoVideoExecutor(tmp_path / "jobs")
    with pytest.raises(ExecutorCapabilityError):
        gen.generate(segment, executor=executor)
    assert executor.submits == []


# ---------------------------------------------------------------------------
# 技术失败：独立计数，不消耗 CNT2/CNT3
# ---------------------------------------------------------------------------


def test_five_technical_failures_do_not_touch_cnt2_cnt3(tmp_path):
    gen, counters, _, segment, events = build(tmp_path)
    executor = FailingExecutor(error_class=ErrorClass.MODEL_API)
    results = [gen.generate(segment, executor=executor) for _ in range(5)]
    assert all(r["technical_failure"] for r in results)
    assert all(r["status"] is SegmentStatus.FAILED for r in results)
    assert all(r["error_class"] is ErrorClass.MODEL_API for r in results)
    assert [r["tech_retry"] for r in results] == [1, 2, 3, 4, 5]
    assert gen.tech_retries.get("P_CH1_SEG1") == 5
    assert gen.tech_retries.total() == 5
    # CNT2/CNT3 必须一动不动
    assert counters.get("P_CH1_SEG1", CounterKind.CNT2) == 0
    assert counters.get("P_CH1_SEG1", CounterKind.CNT3) == 0
    assert counters.increments == []  # 连一次写操作都没有发生
    assert counters.resets == []
    assert all(r["cnt2"] == 0 and r["cnt3"] == 0 for r in results)
    # 技术失败不产生新版本、不追加候选尝试
    assert all(r["version"] == "v1" for r in results)
    assert all(r["segment"].attempts == [] for r in results)
    assert events.events("SEGMENT_TECH_FAILURE")


def test_technical_failure_from_job_state_is_classified(tmp_path):
    gen, counters, _, segment, _ = build(tmp_path)
    executor = ScriptedExecutor(
        tmp_path / "jobs",
        state_override=JobState(
            status=JobStatus.FAILED, error="节点异常", error_class=ErrorClass.COMFY_WORKFLOW
        ),
    )
    result = gen.generate(segment, executor=executor)
    assert result["technical_failure"] is True
    assert result["error_class"] is ErrorClass.COMFY_WORKFLOW
    assert counters.increments == []


def test_unknown_job_state_is_never_treated_as_success(tmp_path):
    gen, counters, _, segment, _ = build(tmp_path)
    gen.poll_limit = 3
    executor = ScriptedExecutor(
        tmp_path / "jobs", state_override=JobState(status=JobStatus.UNKNOWN, error="无法判定")
    )
    result = gen.generate(segment, executor=executor)
    assert result["technical_failure"] is True
    assert result["error_class"] is ErrorClass.COMFY_WORKFLOW
    assert counters.increments == []


def test_missing_outputs_is_file_encoding_technical_failure(tmp_path):
    gen, counters, _, segment, _ = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs", omit_outputs=True)
    result = gen.generate(segment, executor=executor)
    assert result["technical_failure"] is True
    assert result["error_class"] is ErrorClass.FILE_ENCODING
    assert counters.increments == []


def test_corrupted_output_file_is_file_encoding_technical_failure(tmp_path):
    gen, counters, _, segment, _ = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs", missing_outputs=True)
    result = gen.generate(segment, executor=executor)
    assert result["technical_failure"] is True
    assert result["error_class"] is ErrorClass.FILE_ENCODING
    assert "不存在" in result["error"]
    assert counters.increments == []


def test_retry_has_hard_limit(tmp_path):
    gen, counters, _, segment, _ = build(
        tmp_path, cfg=make_config(**{"global_.tech_retry_limit": 3})
    )
    executor = FailingExecutor(error_class=ErrorClass.COMFY_WORKFLOW)
    result = gen.generate_with_retries(segment, executor=executor)
    assert result["attempts_made"] == 3
    assert result["retry_exhausted"] is True
    assert executor.attempts == 3
    assert gen.tech_retries.get("P_CH1_SEG1") == 3
    assert counters.increments == []


def test_retry_stops_on_first_success(tmp_path):
    gen, _, _, segment, _ = build(tmp_path)
    executor = ScriptedExecutor(tmp_path / "jobs")
    result = gen.generate_with_retries(segment, executor=executor)
    assert result["attempts_made"] == 1
    assert result["technical_failure"] is False


def test_retry_limit_must_be_positive(tmp_path):
    gen, _, _, segment, _ = build(tmp_path)
    with pytest.raises(DirectorDependencyError):
        gen.generate_with_retries(segment, executor=ScriptedExecutor(tmp_path / "jobs"), max_attempts=0)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        (FileNotFoundError("x"), ErrorClass.FILE_ENCODING),
        (UnicodeDecodeError("utf-8", b"", 0, 1, "bad"), ErrorClass.FILE_ENCODING),
        (TimeoutError("t"), ErrorClass.COMFY_WORKFLOW),
        (ValueError("v"), ErrorClass.INPUT_VALIDATION),
        (KeyError("k"), ErrorClass.INPUT_VALIDATION),
        (RuntimeError("r"), ErrorClass.COMFY_WORKFLOW),
    ],
)
def test_classify_error(source, expected):
    assert classify_error(source) is expected


def test_tech_retry_counter_is_separate_and_persistable(tmp_path):
    path = tmp_path / "tech_retry.json"
    counter = TechRetryCounter(path)
    counter.increment("S1", reason="api", error_class=ErrorClass.MODEL_API)
    counter.increment("S1")
    counter.increment("S2")
    assert counter.get("S1") == 2 and counter.total() == 3
    reloaded = TechRetryCounter(path)
    assert reloaded.get("S1") == 2 and reloaded.get("S2") == 1
    # 与 CNT 完全是两套存储
    assert "CNT2" not in str(reloaded.snapshot())
