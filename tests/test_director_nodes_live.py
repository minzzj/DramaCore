"""对着**运行中的 ComfyUI** 验证导演台节点可用性（验收 14 的正向路径）。

## 为什么要单独写这个文件

`tests/verify/test_acceptance_14_third_party_nodes.py` 只覆盖**反向路径**：
它用 `http://127.0.0.1:1`（必然拒绝连接）去证明「连不上时明确报错」。
这很重要，但它**证明不了**「节点装了之后真的能被认出来」。

实测确认：在那条用例里 `nodes_available` 为 False，
**并不能说明节点没装** —— 只能说明那个 dead URL 上没有节点。
而本机实际上已经装好三个导演台包并注册了 20 个 `MiniMaxH3*` 节点。

所以这里补正向路径（有服务时跑，没服务时 skip 并标注未验证）：

* `nodes_available is True` 且能点名找到每个导演台的节点类型；
* `require_nodes()` **不抛错**（门禁放行）；
* 版本是从实例现场读到的，不是硬编码；
* `verified` 仍然必须是 False（因为无模型权重 —— 能力未验证，R8）。

最后一条容易被忽略：**节点可用 ≠ 能力已验证**。
把前者当成后者就是造假的开始。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

LIVE_URL = "http://127.0.0.1:8188"


def _live_available() -> bool:
    try:
        with urllib.request.urlopen(f"{LIVE_URL}/system_stats", timeout=6):
            return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _live_available(),
    reason=f"未验证：{LIVE_URL} 上没有运行中的 ComfyUI —— 跳过正向路径验证",
)


def _live_node_types() -> set[str]:
    with urllib.request.urlopen(f"{LIVE_URL}/object_info", timeout=40) as resp:
        return set(json.load(resp).keys())


def _executor():
    from bettercomfyui.executors.director_nodes import DirectorNodeExecutor

    return DirectorNodeExecutor(base_url=LIVE_URL, timeout_s=40)


def test_live_director_nodes_are_detected():
    """正向路径：**用户指定的三个导演台**必须被真的识别出来。

    注意 `DIRECTOR_NODE_MAP` 里有 **4** 组，第 4 组 `h3_chinese_director`
    指向另一个包（`H3-Chinese-Director`，映射到 `H3PromptSidecar` 等）。
    那是一个 181 节点 + 8 个外部依赖的完整生产线，**不在用户指定的三个包之内**，
    本机未安装。因此这里把它当**可选**，只在报告里说明，不算失败。
    """
    from bettercomfyui.executors.director_nodes import DIRECTOR_NODE_MAP

    #: 用户明确要求融合的三个包（对应 DIRECTOR_NODE_MAP 的前三组）
    REQUIRED_GROUPS = ("chain_director", "aimixer_director", "smart_director")
    #: 本机未安装的可选线
    OPTIONAL_GROUPS = ("h3_chinese_director",)

    present = _live_node_types()
    ex = _executor()
    caps = ex.capabilities()

    assert caps.get("nodes_available") is True, (
        f"实例上明明有导演台节点，nodes_available 却是 {caps.get('nodes_available')!r}；"
        f"notes={caps.get('notes')!r}"
    )

    missing_required: list[str] = []
    for group in REQUIRED_GROUPS:
        candidates = DIRECTOR_NODE_MAP[group]
        if not (set(candidates) & present):
            missing_required.append(f"{group}({candidates})")
    assert not missing_required, (
        f"**必须**装的导演台在实例上找不到任何候选节点：{missing_required}；"
        f"实例里 MiniMaxH3* 节点有 "
        f"{sorted(n for n in present if n.startswith('MiniMaxH3'))[:12]}"
    )

    # 可选组：不要求存在，但若存在则记录下来（避免"以为装了其实没装"）
    optional_status = {
        g: bool(set(DIRECTOR_NODE_MAP[g]) & present) for g in OPTIONAL_GROUPS
    }
    assert set(optional_status) == set(OPTIONAL_GROUPS)
    print(f"[INFO] 可选导演台安装情况：{optional_status}")


def test_live_require_nodes_passes_when_nodes_present():
    """节点齐全时门禁必须放行（否则用户会被自己的门禁挡住）。"""
    from bettercomfyui.executors.director_nodes import DirectorNodeUnavailable

    ex = _executor()
    try:
        ex.require_nodes()
    except DirectorNodeUnavailable as exc:
        pytest.fail(
            f"实例上导演台节点齐全，require_nodes() 却拒绝了：{exc}\n"
            f"（提示：这条失败通常意味着节点探测逻辑与实际注册名不匹配）"
        )


def test_live_version_is_probed_not_hardcoded():
    """版本必须现场读到，且与实例自报一致。"""
    import re

    ex = _executor()
    caps = ex.capabilities()
    notes = str(caps.get("notes", ""))
    m = re.search(r"版本 '([^']+)'", notes)
    assert m, f"capabilities().notes 里没有带出实例版本，实际：{notes!r}"

    with urllib.request.urlopen(f"{LIVE_URL}/system_stats", timeout=20) as resp:
        stats = json.load(resp)
    real = (stats.get("system") or {}).get("comfyui_version")
    if real:
        assert m.group(1) == str(real), (
            f"探测到的版本 {m.group(1)!r} 与实例自报 {real!r} 不一致"
        )


def test_live_node_availability_does_not_mean_capability_verified():
    """**关键区分**：节点可用 ≠ 能力已验证。

    本机装了全部导演台节点，但 `models/` 无权重，所以真实生成能力仍必须 `verified=False`。
    把「节点在」当成「能出片」就是假成功。
    """
    ex = _executor()
    caps = ex.capabilities()
    assert caps.get("nodes_available") is True
    assert caps.get("verified") is False, (
        f"无模型权重却声称 verified=True —— 这是假成功。caps={caps!r}"
    )
    assert str(caps.get("notes", "")).strip(), "verified=False 必须写明原因"
    assert caps.get("mock") is not True, "这不是 mock 执行器，不得标成 mock"
