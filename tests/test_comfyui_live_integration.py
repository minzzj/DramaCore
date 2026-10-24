"""对着**运行中的 ComfyUI** 做真实的提交/轮询/产出验证（验收 4 与 14 的正向路径）。

## 为什么这条测试价值高

本机没有模型权重，因此「真实视频生成」无法验证 —— 这点必须如实承认。
但**「与 ComfyUI 的 HTTP 集成是否正确」是可以验证的**，而且不需要任何权重：

用一个**纯 core 节点**的小工作流（`EmptyImage` -> `ImageScale` -> `PreviewImage`），
它不需要 checkpoint/CLIP/VAE，就能真的跑完一次执行。

这条路径覆盖了此前**只被 FakeTransport 覆盖**的东西：

* 请求体形状（`prompt` / `client_id` / `extra_data.idempotency_key`）真的被 ComfyUI 接受；
* `prompt_id` 真的能拿到；
* `/history/{id}` 的返回**真实结构**能被解析出 `SUCCEEDED` 与产出文件；
* 幂等键真的能阻止重复提交；
* 版本 / 节点数能从 `/object_info` 真读出来。

FakeTransport 证明的是「我按我理解的协议写」；这条测试证明「ComfyUI 也这么理解」。

无服务时 skip 并标注「未验证」——绝不假装跑过。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

LIVE = "http://127.0.0.1:8188"

#: 不需要任何模型权重的纯 core 工作流
CORE_GRAPH = {
    "1": {"class_type": "EmptyImage",
          "inputs": {"width": 256, "height": 256, "batch_size": 1, "color": 8421504}},
    "2": {"class_type": "ImageScale",
          "inputs": {"image": ["1", 0], "upscale_method": "nearest-exact",
                     "width": 128, "height": 128, "crop": "disabled"}},
    "3": {"class_type": "PreviewImage", "inputs": {"images": ["2", 0]}},
}


def _live() -> bool:
    try:
        with urllib.request.urlopen(f"{LIVE}/system_stats", timeout=6):
            return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _live(), reason=f"未验证：{LIVE} 上没有运行中的 ComfyUI"
)


def _executor(**kw):
    from bettercomfyui.executors.comfyui_http import ComfyUIHttpExecutor

    return ComfyUIHttpExecutor(base_url=LIVE, timeout_s=60,
                               poll_interval_s=0.5, poll_max_attempts=5, **kw)


def test_submit_accepts_a_real_core_workflow():
    """请求体形状必须被真实 ComfyUI 接受，并返回 prompt_id。"""
    from bettercomfyui.executors.base import JobRef

    ex = _executor()
    ref = ex.submit(CORE_GRAPH, {"purpose": "integration-test"},
                    idempotency_key="itest-submit-1")
    assert isinstance(ref, JobRef), f"submit 必须返回 JobRef，实际 {type(ref)!r}"
    assert ref.job_id, "prompt_id 不能为空（空 id 会被当成提交失败）"


def test_idempotency_key_prevents_duplicate_submission():
    """同一个幂等键重复提交必须返回同一 JobRef（不得真的提交两次）。"""
    ex = _executor()
    a = ex.submit(CORE_GRAPH, {}, idempotency_key="itest-idem-1")
    b = ex.submit(CORE_GRAPH, {}, idempotency_key="itest-idem-1")
    assert a.job_id == b.job_id, "同一幂等键提交两次得到了不同 job_id —— 会重复执行"


def test_poll_reaches_success_and_reports_outputs():
    """真实执行到底，并从 /history 解析出 SUCCEEDED 与产出。"""
    from bettercomfyui.models.enums import JobStatus

    ex = _executor()
    ref = ex.submit(CORE_GRAPH, {}, idempotency_key="itest-poll-1")

    state = None
    deadline = time.time() + 120
    while time.time() < deadline:
        state = ex.poll(ref)
        if state.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
            break
        time.sleep(0.5)

    assert state is not None, "poll 必须返回 JobState"
    assert state.status is JobStatus.SUCCEEDED, (
        f"纯 core 工作流应当执行成功，实际 {state.status}，error={state.error!r}"
    )
    assert state.outputs, (
        "SUCCEEDED 却没有任何产出 —— 这是假成功（PreviewImage 会产出一个 png）"
    )


def test_capabilities_reads_live_instance_facts():
    """capabilities 里的版本/节点数是**真读**的，不是常量。"""
    ex = _executor()
    caps = ex.capabilities()
    assert caps["verified"] is False, "无权重时 verified 必须为 False（R8）"
    notes = str(caps.get("notes", ""))
    assert notes.strip(), "verified=False 必须写明原因"

    with urllib.request.urlopen(f"{LIVE}/system_stats", timeout=20) as resp:
        real_version = (json.load(resp).get("system") or {}).get("comfyui_version")
    if real_version:
        assert str(real_version) in notes, (
            f"capabilities().notes 里没有出现实例的真实版本 {real_version!r}；notes={notes!r}"
        )


def test_no_weights_means_capabilities_stay_unverified():
    """**关键区分**：ComfyUI 能跑通 ≠ 视频生成能力已验证。

    本机连纯 core 工作流都跑通了，但 `models/` 无权重，
    所以 `video` 能力仍必须 `verified=False`。
    把「HTTP 通了」当成「能出片」就是假成功。
    """
    ex = _executor()
    caps = ex.capabilities()
    assert caps["verified"] is False, (
        f"无模型权重却声称能力已验证 —— 假成功。caps={caps!r}"
    )
    assert caps.get("mock") is not True, "这不是 mock 执行器，不得标成 mock"
