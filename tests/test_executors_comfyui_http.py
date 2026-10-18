"""ComfyUIHttpExecutor：提交/轮询/取消/能力探测 + 失败分类（全部离线，用注入传输层）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.executors.base import (  # noqa: E402
    ExecutorError,
    ExecutorUnavailable,
    JobRef,
    validate_capabilities,
)
from bettercomfyui.executors.comfyui_http import (  # noqa: E402
    DEFAULT_BASE_URL,
    ComfyUIHttpExecutor,
    meets_min_comfyui,
    UrllibTransport,
)
from bettercomfyui.models.enums import ErrorClass, JobStatus  # noqa: E402

BASE = DEFAULT_BASE_URL

#: 测试用的本机版本。**不再从源码读常量** —— 源码已改为现场探测；
#: 硬编码版本常量正是被修掉的 bug（doctor 报 0.41.0、demo 报 0.22.3）。
TEST_COMFYUI_VERSION = '0.41.0'


class FakeTransport:
    """离线传输层替身：按路由返回，支持序列响应，并记录全部调用。"""

    def __init__(self, routes=None, handler=None):
        self.routes = dict(routes or {})
        self.handler = handler
        self.calls: list[dict] = []
        self._seq: dict[str, int] = {}

    def request(self, method, url, *, json_body=None, timeout=None):
        path = url[len(BASE) :] if url.startswith(BASE) else url
        path = "/" + path.lstrip("/")
        self.calls.append({"method": method, "path": path, "body": json_body})
        if self.handler is not None:
            return self.handler(method, path, json_body)
        if path not in self.routes:
            return (404, {"error": "not found", "path": path})
        value = self.routes[path]
        if isinstance(value, list):
            idx = min(self._seq.get(path, 0), len(value) - 1)
            self._seq[path] = idx + 1
            return value[idx]
        return value

    def paths(self):
        return [c["path"] for c in self.calls]


def make_exec(**kwargs):
    transport = kwargs.pop("transport")
    return ComfyUIHttpExecutor(BASE, transport=transport, **kwargs)


def _comfyui_alive() -> bool:
    """真实探测本机 ComfyUI 是否在运行（仅用于决定是否 skip，不参与断言）。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{BASE}/system_stats", timeout=1.5) as resp:
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


def test_submit_returns_job_ref_and_dedupes():
    transport = FakeTransport({"/prompt": (200, {"prompt_id": "PID1", "number": 1})})
    ex = make_exec(transport=transport)
    ref = ex.submit({"1": {"class_type": "X"}}, {"a": 1}, idempotency_key="K1")
    assert ref.job_id == "PID1"
    assert ref.executor == "comfyui_http"
    again = ex.submit({"1": {"class_type": "X"}}, {"a": 1}, idempotency_key="K1")
    assert again == ref
    assert transport.paths().count("/prompt") == 1  # 幂等键去重，未重复提交
    assert transport.calls[0]["body"]["extra_data"]["idempotency_key"] == "K1"


def test_submit_requires_idempotency_key():
    ex = make_exec(transport=FakeTransport())
    with pytest.raises(ExecutorError) as err:
        ex.submit({}, {}, idempotency_key="")
    assert err.value.error_class is ErrorClass.INPUT_VALIDATION


def test_submit_connection_failure_is_failed_with_error_class_not_unknown():
    def handler(method, path, body):
        raise ExecutorUnavailable(
            "无法连接 ComfyUI：connection refused", error_class=ErrorClass.COMFY_WORKFLOW
        )

    ex = make_exec(transport=FakeTransport(handler=handler))
    with pytest.raises(ExecutorError) as err:
        ex.submit({}, {}, idempotency_key="K")
    assert err.value.error_class is ErrorClass.COMFY_WORKFLOW
    state = err.value.to_job_state(job_id="unknown")
    assert state.status is JobStatus.FAILED
    assert state.status is not JobStatus.UNKNOWN
    assert state.is_success is False


def test_submit_wraps_unexpected_transport_exception_as_comfy_workflow():
    def handler(method, path, body):
        raise RuntimeError("socket exploded")

    ex = make_exec(transport=FakeTransport(handler=handler))
    with pytest.raises(ExecutorUnavailable) as err:
        ex.submit({}, {}, idempotency_key="K")
    assert err.value.error_class is ErrorClass.COMFY_WORKFLOW


def test_submit_http_400_is_input_validation():
    transport = FakeTransport({"/prompt": (400, {"error": "bad workflow"})})
    ex = make_exec(transport=transport)
    with pytest.raises(ExecutorError) as err:
        ex.submit({}, {}, idempotency_key="K")
    assert err.value.error_class is ErrorClass.INPUT_VALIDATION


def test_submit_node_errors_is_rejected():
    transport = FakeTransport(
        {"/prompt": (200, {"prompt_id": "PID", "node_errors": {"3": {"error": "missing model"}}})}
    )
    ex = make_exec(transport=transport)
    with pytest.raises(ExecutorError) as err:
        ex.submit({}, {}, idempotency_key="K")
    assert err.value.error_class is ErrorClass.COMFY_WORKFLOW
    assert "node_errors" in str(err.value)


def test_submit_missing_prompt_id_is_rejected_not_treated_as_success():
    transport = FakeTransport({"/prompt": (200, {"number": 3})})
    ex = make_exec(transport=transport)
    with pytest.raises(ExecutorError) as err:
        ex.submit({}, {}, idempotency_key="K")
    assert "prompt_id" in str(err.value)


# ---------------------------------------------------------------------------
# 轮询
# ---------------------------------------------------------------------------


def test_poll_success_collects_outputs(tmp_path):
    history = {
        "/history/PID1": (
            200,
            {
                "PID1": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "9": {
                            "images": [{"filename": "a.png", "subfolder": "sub", "type": "output"}],
                            "gifs": [{"filename": "v.mp4", "subfolder": "", "type": "output"}],
                        }
                    },
                }
            },
        )
    }
    ex = make_exec(transport=FakeTransport(history), output_dir=tmp_path)
    state = ex.poll(JobRef(job_id="PID1", executor="comfyui_http"))
    assert state.status is JobStatus.SUCCEEDED
    assert state.is_success is True
    assert state.mock is False
    assert tmp_path / "sub" / "a.png" in state.outputs
    assert tmp_path / "v.mp4" in state.outputs


def test_poll_execution_error_is_failed_comfy_workflow():
    history = {
        "/history/PID": (
            200,
            {
                "PID": {
                    "status": {
                        "completed": True,
                        "status_str": "error",
                        "messages": [["execution_error", {"exception_message": "CUDA OOM"}]],
                    },
                    "outputs": {},
                }
            },
        )
    }
    ex = make_exec(transport=FakeTransport(history))
    state = ex.poll(JobRef(job_id="PID", executor="comfyui_http"))
    assert state.status is JobStatus.FAILED
    assert state.error_class is ErrorClass.COMFY_WORKFLOW
    assert state.is_success is False


def test_poll_completed_without_outputs_is_file_encoding_failure():
    history = {
        "/history/PID": (200, {"PID": {"status": {"completed": True, "status_str": "success"}, "outputs": {}}}),
    }
    ex = make_exec(transport=FakeTransport(history))
    state = ex.poll(JobRef(job_id="PID", executor="comfyui_http"))
    assert state.status is JobStatus.FAILED
    assert state.error_class is ErrorClass.FILE_ENCODING


def test_poll_lost_job_is_failed_not_unknown():
    transport = FakeTransport({"/history/GONE": (200, {}), "/queue": (200, {"queue_running": [], "queue_pending": []})})
    ex = make_exec(transport=transport)
    state = ex.poll(JobRef(job_id="GONE", executor="comfyui_http"))
    assert state.status is JobStatus.FAILED
    assert state.status is not JobStatus.UNKNOWN
    assert state.error_class is ErrorClass.COMFY_WORKFLOW


def test_poll_running_and_queued():
    running = FakeTransport(
        {
            "/history/PID": (200, {}),
            "/queue": (200, {"queue_running": [[1, "PID"]], "queue_pending": []}),
        }
    )
    assert make_exec(transport=running).poll(JobRef("PID", "comfyui_http")).status is JobStatus.RUNNING
    pending = FakeTransport(
        {
            "/history/PID": (200, {}),
            "/queue": (200, {"queue_running": [], "queue_pending": [[2, "PID"]]}),
        }
    )
    assert make_exec(transport=pending).poll(JobRef("PID", "comfyui_http")).status is JobStatus.QUEUED


def test_poll_transport_always_failing_returns_unknown_with_explanation():
    def handler(method, path, body):
        raise ExecutorUnavailable("network down", error_class=ErrorClass.COMFY_WORKFLOW)

    ex = make_exec(transport=FakeTransport(handler=handler), poll_max_attempts=3)
    state = ex.poll(JobRef(job_id="PID", executor="comfyui_http"))
    assert state.status is JobStatus.UNKNOWN  # 无法判定
    assert state.is_success is False  # 绝不是成功
    assert state.error and "轮询" in state.error
    assert state.error_class is ErrorClass.COMFY_WORKFLOW


def test_poll_partial_failure_then_success():
    history = {
        "/history/PID": [
            (500, {"error": "temp"}),
            (200, {"PID": {"status": {"completed": True, "status_str": "success"}, "outputs": {"1": {"images": [{"filename": "a.png"}]}}}}),
        ]
    }
    ex = make_exec(transport=FakeTransport(history), poll_max_attempts=3)
    state = ex.poll(JobRef(job_id="PID", executor="comfyui_http"))
    assert state.status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_uses_interrupt():
    transport = FakeTransport({"/interrupt": (200, {})})
    assert make_exec(transport=transport).cancel(JobRef("PID", "comfyui_http")) is True
    assert "/interrupt" in transport.paths()


def test_cancel_falls_back_to_queue_delete():
    transport = FakeTransport({"/interrupt": (500, {}), "/queue": (200, {})})
    assert make_exec(transport=transport).cancel(JobRef("PID", "comfyui_http")) is True
    assert transport.paths() == ["/interrupt", "/queue"]


def test_cancel_returns_false_when_unreachable():
    def handler(method, path, body):
        raise ExecutorUnavailable("down", error_class=ErrorClass.COMFY_WORKFLOW)

    assert make_exec(transport=FakeTransport(handler=handler)).cancel(JobRef("PID", "comfyui_http")) is False


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------


def test_capabilities_unreachable_declares_everything_false():
    def handler(method, path, body):
        raise ExecutorUnavailable("connection refused", error_class=ErrorClass.COMFY_WORKFLOW)

    caps = make_exec(transport=FakeTransport(handler=handler)).capabilities()
    validate_capabilities(caps)
    assert caps["verified"] is False
    assert caps["reachable"] is False
    assert caps["image"] is False and caps["video"] is False
    assert caps["tail_frames"] is None
    assert "24GB" in caps["notes"]  # 前置条件必须写明
    assert "verified=False" in caps["notes"]


def test_capabilities_reachable_but_no_models_stays_unverified():
    routes = {
        "/system_stats": (200, {"system": {"comfyui_version": TEST_COMFYUI_VERSION}, "devices": [{"name": "GTX 1050 Ti"}]}),
        "/object_info": (
            200,
            {
                "KSampler": {"input": {"required": {}}},
                "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[]]}}},
                "MiniMaxH3Director": {"input": {"required": {}}},
            },
        ),
    }
    caps = make_exec(transport=FakeTransport(routes)).capabilities()
    validate_capabilities(caps)
    assert caps["reachable"] is True
    assert caps["comfyui_version"] == TEST_COMFYUI_VERSION
    assert caps["node_type_count"] == 3
    assert caps["video"] is True  # 有导演节点但无权重
    assert caps["verified"] is False  # 仍然未验证真实生成
    assert caps["checkpoints"] == []
    assert caps["verified_reason"]


def test_default_base_url_is_local_comfyui():
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000"


def test_urllib_transport_failure_is_classified():
    """真实传输层（本机 9 号端口必然拒绝）：失败必须归类，绝不静默。"""
    ex = ComfyUIHttpExecutor("http://127.0.0.1:9", transport=UrllibTransport(), timeout_s=1.0)
    with pytest.raises(ExecutorError) as err:
        ex.submit({}, {}, idempotency_key="K")
    assert err.value.error_class is ErrorClass.COMFY_WORKFLOW


@pytest.mark.skipif(
    not _comfyui_alive(),
    reason="本机 ComfyUI 未运行（实测 127.0.0.1:8000 连接被拒绝）；"
    "该用例只在真实服务存在时验证探测字段，不做任何真实生成",
)
def test_live_probe_reports_real_facts():
    caps = ComfyUIHttpExecutor(BASE).capabilities()
    assert caps["reachable"] is True
    assert caps["verified"] is False  # 无论节点/权重是否齐备，本机都未验证真实生成
