"""executors/base.py：能力声明如实性、JobState 不变量、mock 边界。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.executors.base import (  # noqa: E402
    EXECUTOR_CAPABILITY_MIN_KEYS,
    Executor,
    ExecutorCapabilityError,
    ExecutorError,
    JobRef,
    JobState,
    assert_not_mock,
    assert_real_capabilities,
    is_mock_capabilities,
    validate_capabilities,
)
from bettercomfyui.models.enums import ErrorClass, JobStatus  # noqa: E402


def _caps(**overrides):
    caps = {
        "image": True,
        "video": True,
        "first_last_frame": True,
        "tail_frames": 22,
        "verified": False,
        "notes": "未验证：本机无模型权重",
    }
    caps.update(overrides)
    return caps


def test_capability_min_keys_match_contract():
    assert set(EXECUTOR_CAPABILITY_MIN_KEYS) == {
        "image",
        "video",
        "first_last_frame",
        "tail_frames",
        "verified",
        "notes",
    }


def test_validate_capabilities_accepts_complete_declaration():
    assert validate_capabilities(_caps())["verified"] is False


@pytest.mark.parametrize("missing", ["image", "video", "first_last_frame", "tail_frames", "verified", "notes"])
def test_missing_capability_key_is_rejected(missing):
    caps = _caps()
    caps.pop(missing)
    with pytest.raises(ExecutorCapabilityError) as err:
        validate_capabilities(caps)
    assert missing in str(err.value)


def test_notes_must_be_non_empty():
    with pytest.raises(ExecutorCapabilityError):
        validate_capabilities(_caps(notes="   "))


def test_verified_must_be_bool():
    with pytest.raises(ExecutorCapabilityError):
        validate_capabilities(_caps(verified="yes"))


def test_tail_frames_must_be_int_or_none():
    with pytest.raises(ExecutorCapabilityError):
        validate_capabilities(_caps(tail_frames="22"))


def test_mock_cannot_claim_verified():
    with pytest.raises(ExecutorCapabilityError) as err:
        validate_capabilities(_caps(mock=True, verified=True))
    assert "mock" in str(err.value)


def test_is_mock_capabilities():
    assert is_mock_capabilities(_caps(mock=True)) is True
    assert is_mock_capabilities(_caps()) is False


def test_assert_real_capabilities_rejects_mock():
    with pytest.raises(ExecutorCapabilityError):
        assert_real_capabilities(_caps(mock=True), purpose="最终交付")


def test_assert_real_capabilities_rejects_unverified():
    with pytest.raises(ExecutorCapabilityError) as err:
        assert_real_capabilities(_caps(), purpose="最终交付")
    assert "verified=False" in str(err.value)


def test_assert_real_capabilities_accepts_verified_real():
    caps = _caps(verified=True, notes="已在目标机跑通 MiniMax-H3 工作流")
    assert assert_real_capabilities(caps, purpose="最终交付") is caps


def test_succeeded_state_rejects_error_class():
    with pytest.raises(ValueError):
        JobState(status=JobStatus.SUCCEEDED, error_class=ErrorClass.MODEL_API)


def test_failed_state_requires_error_class():
    with pytest.raises(ValueError):
        JobState(status=JobStatus.FAILED, error="炸了")


def test_unknown_state_requires_explanation():
    with pytest.raises(ValueError):
        JobState(status=JobStatus.UNKNOWN)


def test_progress_out_of_range_rejected():
    with pytest.raises(ValueError):
        JobState(status=JobStatus.RUNNING, progress=1.5)
    with pytest.raises(ValueError):
        JobState(status=JobStatus.RUNNING, progress=float("nan"))


def test_only_succeeded_is_success():
    for status in JobStatus:
        state = JobState(
            status=status,
            error="x" if status in (JobStatus.FAILED, JobStatus.UNKNOWN) else None,
            error_class=ErrorClass.COMFY_WORKFLOW
            if status in (JobStatus.FAILED, JobStatus.UNKNOWN)
            else None,
        )
        assert state.is_success is (status is JobStatus.SUCCEEDED)
    unknown = JobState(status=JobStatus.UNKNOWN, error="轮询失败")
    assert unknown.is_success is False
    assert unknown.is_technical_failure is True


def test_outputs_are_coerced_to_path():
    state = JobState(status=JobStatus.SUCCEEDED, outputs=["a/b.png"])
    assert state.outputs == [Path("a/b.png")]


def test_mock_flag_default_false_and_settable():
    assert JobState(status=JobStatus.SUCCEEDED).mock is False
    assert JobState(status=JobStatus.SUCCEEDED, mock=True).mock is True


def test_assert_not_mock_blocks_mock_outputs():
    with pytest.raises(ExecutorCapabilityError):
        assert_not_mock(JobState(status=JobStatus.SUCCEEDED, mock=True), purpose="拼接")
    real = JobState(status=JobStatus.SUCCEEDED)
    assert assert_not_mock(real, purpose="拼接") is real


def test_executor_error_maps_to_failed_not_unknown():
    err = ExecutorError("连接失败", error_class=ErrorClass.COMFY_WORKFLOW)
    state = err.to_job_state(job_id="J1")
    assert state.status is JobStatus.FAILED
    assert state.status is not JobStatus.UNKNOWN
    assert state.error_class is ErrorClass.COMFY_WORKFLOW
    assert state.is_success is False


def test_executor_error_default_class_is_comfy_workflow():
    assert ExecutorError("x").error_class is ErrorClass.COMFY_WORKFLOW


def test_job_ref_is_frozen():
    ref = JobRef(job_id="J1", executor="mock")
    with pytest.raises(Exception):
        ref.job_id = "J2"  # type: ignore[misc]


def test_protocol_is_runtime_checkable():
    class Dummy:
        name = "dummy"

        def submit(self, workflow, params, *, idempotency_key):
            return JobRef(job_id="1", executor=self.name)

        def poll(self, ref):
            return JobState(status=JobStatus.SUCCEEDED)

        def cancel(self, ref):
            return True

        def capabilities(self):
            return _caps()

    assert isinstance(Dummy(), Executor)
