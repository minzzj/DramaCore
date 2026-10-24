"""MockExecutor：确定性、mock 标记、绝不冒充真实生成。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.executors.base import (  # noqa: E402
    ExecutorCapabilityError,
    ExecutorError,
    assert_not_mock,
    assert_real_capabilities,
    is_mock_capabilities,
    validate_capabilities,
)
from bettercomfyui.executors.mock import MockExecutor  # noqa: E402
from bettercomfyui.models.enums import ErrorClass, JobStatus  # noqa: E402


def test_capabilities_declare_mock_and_unverified(tmp_path):
    caps = MockExecutor(tmp_path).capabilities()
    validate_capabilities(caps)
    assert caps["mock"] is True
    assert caps["verified"] is False
    assert caps["video_decodable"] is False
    assert "verified=False" in caps["notes"]
    assert "占位" in caps["notes"]


def test_mock_capabilities_cannot_be_used_as_real(tmp_path):
    caps = MockExecutor(tmp_path).capabilities()
    assert is_mock_capabilities(caps) is True
    with pytest.raises(ExecutorCapabilityError):
        assert_real_capabilities(caps, purpose="最终交付")


def test_submit_is_idempotent_on_same_key(tmp_path):
    ex = MockExecutor(tmp_path)
    ref1 = ex.submit({"a": 1}, {}, idempotency_key="K1")
    ref2 = ex.submit({"a": 1}, {}, idempotency_key="K1")
    assert ref1 == ref2
    assert len(ex._jobs) == 1


def test_empty_idempotency_key_rejected(tmp_path):
    with pytest.raises(ExecutorError) as err:
        MockExecutor(tmp_path).submit({}, {}, idempotency_key="")
    assert err.value.error_class is ErrorClass.INPUT_VALIDATION


def test_outputs_are_deterministic_across_instances(tmp_path):
    a = MockExecutor(tmp_path / "a")
    b = MockExecutor(tmp_path / "b")
    ra = a.submit({"p": "x"}, {"n": 1}, idempotency_key="K")
    rb = b.submit({"p": "x"}, {"n": 1}, idempotency_key="K")
    assert ra.job_id == rb.job_id
    sa = a.poll(ra)
    sb = b.poll(rb)
    for pa, pb in zip(sa.outputs, sb.outputs):
        assert pa.read_bytes() == pb.read_bytes()


def test_different_keys_produce_different_jobs(tmp_path):
    ex = MockExecutor(tmp_path)
    assert ex.submit({}, {"n": 1}, idempotency_key="K1").job_id != ex.submit(
        {}, {"n": 2}, idempotency_key="K2"
    ).job_id


def test_poll_marks_mock_and_writes_real_png(tmp_path):
    ex = MockExecutor(tmp_path)
    ref = ex.submit({"p": "x"}, {}, idempotency_key="K")
    state = ex.poll(ref)
    assert state.status is JobStatus.SUCCEEDED
    assert state.mock is True
    assert state.is_success is True
    with pytest.raises(ExecutorCapabilityError):
        assert_not_mock(state, purpose="拼接")
    names = {p.name for p in state.outputs}
    assert {"frame_first.png", "frame_last.png", "segment.mock.mp4"} <= names
    first = next(p for p in state.outputs if p.name == "frame_first.png")
    assert first.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    manifest = json.loads((first.parent / "mock_manifest.json").read_text(encoding="utf-8"))
    assert manifest["mock"] is True
    assert "禁止当真实成片使用" in manifest["warning"]


def test_mock_video_placeholder_is_not_claimed_decodable(tmp_path):
    ex = MockExecutor(tmp_path)
    ref = ex.submit({}, {}, idempotency_key="K")
    state = ex.poll(ref)
    video = next(p for p in state.outputs if p.name == "segment.mock.mp4")
    payload = json.loads(video.read_text(encoding="utf-8"))
    assert payload["decodable_container"] is False
    assert ex.capabilities()["video_decodable"] is False


def test_injected_failures_are_classified(tmp_path):
    ex = MockExecutor(tmp_path, fail_times=5, fail_error_class=ErrorClass.MODEL_API)
    for i in range(5):
        with pytest.raises(ExecutorError) as err:
            ex.submit({}, {}, idempotency_key=f"K{i}")
        assert err.value.error_class is ErrorClass.MODEL_API
    # 第 6 次成功：失败次数用尽
    assert ex.submit({}, {}, idempotency_key="K6").job_id


def test_cancel_and_unknown_job(tmp_path):
    from bettercomfyui.executors.base import JobRef

    ex = MockExecutor(tmp_path)
    ref = ex.submit({}, {}, idempotency_key="K")
    assert ex.cancel(ref) is True
    cancelled = ex.poll(ref)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.error_class is ErrorClass.USER_CANCELLED
    assert cancelled.is_success is False

    ghost = ex.poll(JobRef(job_id="NOPE", executor="mock"))
    assert ghost.status is JobStatus.FAILED
    assert ghost.status is not JobStatus.UNKNOWN
    assert ghost.error_class is ErrorClass.COMFY_WORKFLOW
