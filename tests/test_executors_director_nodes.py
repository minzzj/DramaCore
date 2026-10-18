"""DirectorNodeExecutor：节点缺失必须明确报错，绝不静默/退化。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.executors.base import ExecutorError, validate_capabilities  # noqa: E402
from bettercomfyui.executors.comfyui_http import DEFAULT_BASE_URL  # noqa: E402
from bettercomfyui.executors.director_nodes import (  # noqa: E402
    DIRECTOR_NODE_MAP,
    DirectorNodeExecutor,
    DirectorNodeUnavailable,
)
from bettercomfyui.models.enums import ErrorClass  # noqa: E402

from test_executors_comfyui_http import FakeTransport  # noqa: E402


def _routes(node_names: list[str], *, checkpoints: list[str] | None = None):
    object_info = {name: {"input": {"required": {}}} for name in node_names}
    object_info["CheckpointLoaderSimple"] = {
        "input": {"required": {"ckpt_name": [checkpoints or []]}}
    }
    return {
        "/system_stats": (200, {"system": {"comfyui_version": "0.22.3"}}),
        "/object_info": (200, object_info),
    }


def test_node_class_names_come_from_local_repos():
    """类名必须来自本地仓库实测，不是臆测。"""
    assert "MiniMaxH3ChainDirector" in DIRECTOR_NODE_MAP["chain_director"]
    assert "MiniMaxH3Director" in DIRECTOR_NODE_MAP["aimixer_director"]
    assert "MiniMaxH3SmartDirectorAllInOne" in DIRECTOR_NODE_MAP["smart_director"]
    assert "H3PromptSidecar" in DIRECTOR_NODE_MAP["h3_chinese_director"]


def test_missing_nodes_raise_with_explicit_list():
    ex = DirectorNodeExecutor(
        DEFAULT_BASE_URL,
        required=("aimixer_director", "chain_director"),
        transport=FakeTransport(_routes(["KSampler"])),
    )
    with pytest.raises(DirectorNodeUnavailable) as err:
        ex.require_nodes()
    assert set(err.value.missing) == {"aimixer_director", "chain_director"}
    assert "MiniMaxH3Director" in str(err.value)
    assert err.value.error_class is ErrorClass.COMFY_WORKFLOW


def test_missing_nodes_block_submit_no_silent_fallback():
    transport = FakeTransport(_routes(["KSampler"]))
    ex = DirectorNodeExecutor(DEFAULT_BASE_URL, transport=transport)
    with pytest.raises(DirectorNodeUnavailable):
        ex.submit({"1": {}}, {}, idempotency_key="K")
    assert "/prompt" not in transport.paths()  # 未提交任何作业


def test_unreachable_comfyui_reports_missing_nodes():
    def handler(method, path, body):
        raise ExecutorError("connection refused", error_class=ErrorClass.COMFY_WORKFLOW)

    ex = DirectorNodeExecutor(DEFAULT_BASE_URL, transport=FakeTransport(handler=handler))
    with pytest.raises(DirectorNodeUnavailable) as err:
        ex.require_nodes()
    assert err.value.detail["reachable"] is False
    assert "不可达" in str(err.value)


def test_nodes_present_still_unverified():
    ex = DirectorNodeExecutor(
        DEFAULT_BASE_URL,
        required=("aimixer_director", "chain_director", "smart_director", "h3_chinese_director"),
        transport=FakeTransport(
            _routes(
                [
                    "MiniMaxH3Director",
                    "MiniMaxH3ChainDirector",
                    "MiniMaxH3SmartDirector",
                    "H3PromptSidecar",
                ]
            )
        ),
    )
    report = ex.require_nodes()
    assert report["missing"] == []
    assert report["available"]["aimixer_director"] == "MiniMaxH3Director"
    caps = validate_capabilities(ex.capabilities())
    assert caps["nodes_available"] is True
    assert caps["mock"] is False
    # 节点存在 ≠ 真实生成已验证：仍然 verified=False
    assert caps["verified"] is False
    assert "节点存在也不等于真实生成已验证" in caps["notes"]


def test_unknown_required_capability_is_rejected():
    with pytest.raises(ExecutorError) as err:
        DirectorNodeExecutor(DEFAULT_BASE_URL, required=("not_a_director",), transport=FakeTransport({}))
    assert err.value.error_class is ErrorClass.INPUT_VALIDATION
