"""融合工作流的结构测试（不需要运行中的 ComfyUI）。

`validate_fusion_workflow.py` 需要连 ComfyUI 才能读真实 schema；
本文件做**离线**检查，保证工作流在结构上自洽，这样 CI 里也能跑。

两条最重要的检查：

1. **控件数必须与声明的 schema 一致** —— 不匹配会让参数**静默错位**
   （cfg 填进 seed 这种），载入后不报错但结果全错。
2. **连线两端必须存在**，且源节点确实有那个输出槽。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WF_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
FUSION_FILES = sorted(WF_DIR.glob("fusion_*.json"))


def test_fusion_workflows_exist():
    assert FUSION_FILES, (
        f"{WF_DIR} 下没有 fusion_*.json —— 融合工作流未交付。"
        "用 `python bettercomfyui_comfy/workflows/build_fusion_workflow.py` 生成。"
    )


@pytest.mark.parametrize("path", FUSION_FILES, ids=[p.name for p in FUSION_FILES])
def test_fusion_workflow_is_valid_json_with_nodes(path: Path):
    wf = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(wf.get("nodes"), list) and wf["nodes"], f"{path.name} 没有 nodes"
    assert isinstance(wf.get("links"), list), f"{path.name} 没有 links"


@pytest.mark.parametrize("path", FUSION_FILES, ids=[p.name for p in FUSION_FILES])
def test_links_reference_existing_nodes(path: Path):
    wf = json.loads(path.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in wf["nodes"]}
    for link in wf["links"]:
        # [id, src_node, src_slot, dst_node, dst_slot, type]
        assert len(link) >= 5, f"{path.name} 的 link 结构异常：{link!r}"
        lid, src, sslot, dst, dslot = link[0], link[1], link[2], link[3], link[4]
        assert src in by_id, f"{path.name} link {lid} 源节点 {src} 不存在"
        assert dst in by_id, f"{path.name} link {lid} 目标节点 {dst} 不存在"
        outs = by_id[src].get("outputs") or []
        assert sslot < len(outs), (
            f"{path.name} link {lid} 源槽 {sslot} 越界（节点 {src} 只有 {len(outs)} 个输出）"
        )


@pytest.mark.parametrize("path", FUSION_FILES, ids=[p.name for p in FUSION_FILES])
def test_node_output_slots_have_names(path: Path):
    wf = json.loads(path.read_text(encoding="utf-8"))
    for n in wf["nodes"]:
        for slot, out in enumerate(n.get("outputs") or []):
            assert out.get("name"), f"{path.name} 节点 {n['id']} 输出槽 {slot} 没有名字"
            assert out.get("type"), f"{path.name} 节点 {n['id']} 输出槽 {slot} 没有类型"


@pytest.mark.parametrize("path", FUSION_FILES, ids=[p.name for p in FUSION_FILES])
def test_widget_values_present_for_every_node(path: Path):
    """每个节点都要有 widgets_values（ComfyUI 靠位置对应控件）。"""
    wf = json.loads(path.read_text(encoding="utf-8"))
    for n in wf["nodes"]:
        assert "widgets_values" in n, f"{path.name} 节点 {n['id']} 缺少 widgets_values"


def test_fusion_workflow_uses_our_fusion_nodes():
    """融合工作流必须真的用到融合节点，否则它就不叫融合工作流。"""
    used: set[str] = set()
    for path in FUSION_FILES:
        wf = json.loads(path.read_text(encoding="utf-8"))
        used |= {n["type"] for n in wf["nodes"]}
    assert any(t.startswith("BetterComfyUIFusion") for t in used), (
        f"融合工作流里没有用到任何融合节点，实际用到：{sorted(used)}"
    )


def test_fusion_workflow_targets_a_real_director():
    """至少要有一个工作流真的接上三个导演台之一。"""
    directors = {"MiniMaxH3Director", "MiniMaxH3SmartDirector",
                 "MiniMaxH3SmartDirectorAllInOne", "MiniMaxH3ChainDirector"}
    used: set[str] = set()
    for path in FUSION_FILES:
        wf = json.loads(path.read_text(encoding="utf-8"))
        used |= {n["type"] for n in wf["nodes"]}
    assert used & directors, (
        f"没有任何融合工作流接上导演台，实际用到：{sorted(used)}"
    )


def test_embedded_script_is_parseable_json():
    """融合作流里内嵌的剧本必须是合法 JSON（否则用户一载入就报错）。"""
    for path in FUSION_FILES:
        wf = json.loads(path.read_text(encoding="utf-8"))
        for n in wf["nodes"]:
            if n["type"] != "BetterComfyUIFusionGroups":
                continue
            vals = n.get("widgets_values") or []
            assert vals, "FusionGroups 没有控件值"
            script = vals[1]  # project_id, script_text, ...
            json.loads(script)  # 不合法会抛 JSONDecodeError


# ---------------------------------------------------------------------------
# 目标槽位必须落在节点自己的 inputs 数组里（离线可查，不需要 ComfyUI）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", FUSION_FILES, ids=[p.name for p in FUSION_FILES])
def test_link_target_slot_is_within_declared_inputs(path: Path):
    """每条 link 的目标槽位必须 < 该节点 `inputs` 数组长度。

    **实测踩到**：工作流 B 里我照抄了别人的目标槽编号（7 / 2 / 3），
    而两个节点实际只声明了 1~2 个输入 —— 槽位越界。
    加"目标槽越界"检查后当场抓到。

    这条离线可查，所以放进 pytest；连类型是否匹配的检查需要真实 schema，
    留在 `validate_fusion_workflow.py` 里。
    """
    wf = json.loads(path.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in wf["nodes"]}
    for link in wf["links"]:
        lid, _src, _sslot, dst, dslot = link[0], link[1], link[2], link[3], link[4]
        n_inputs = len(by_id[dst].get("inputs") or [])
        assert dslot < n_inputs, (
            f"{path.name} link {lid} 目标槽 {dslot} 越界："
            f"节点 {dst}({by_id[dst]['type']}) 只声明了 {n_inputs} 个输入"
        )


@pytest.mark.parametrize("path", FUSION_FILES, ids=[p.name for p in FUSION_FILES])
def test_link_source_slot_is_within_declared_outputs(path: Path):
    """每条 link 的源槽位必须 < 该节点 `outputs` 数组长度。"""
    wf = json.loads(path.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in wf["nodes"]}
    for link in wf["links"]:
        lid, src, sslot = link[0], link[1], link[2]
        n_outs = len(by_id[src].get("outputs") or [])
        assert sslot < n_outs, (
            f"{path.name} link {lid} 源槽 {sslot} 越界："
            f"节点 {src}({by_id[src]['type']}) 只有 {n_outs} 个输出"
        )


def test_each_link_has_five_elements_and_a_type():
    """link 必须是 `[id, src, sslot, dst, dslot, type]` 且带类型。

    少了类型就无法做类型匹配检查（融合的关键接缝正是 `MMX_DIR_GROUP`）。
    """
    for path in FUSION_FILES:
        wf = json.loads(path.read_text(encoding="utf-8"))
        for link in wf["links"]:
            assert len(link) >= 6, f"{path.name} 的 link 缺少类型字段：{link!r}"
            assert isinstance(link[5], str) and link[5], (
                f"{path.name} 的 link {link[0]} 类型为空"
            )


def test_output_slot_links_are_recorded():
    """源节点的 `outputs[i].links` 必须记下它发出的 link（连线的双向一致性）。"""
    for path in FUSION_FILES:
        wf = json.loads(path.read_text(encoding="utf-8"))
        by_id = {n["id"]: n for n in wf["nodes"]}
        for link in wf["links"]:
            lid, src, sslot = link[0], link[1], link[2]
            outs = by_id[src].get("outputs") or []
            recorded = outs[sslot].get("links") or []
            assert lid in recorded, (
                f"{path.name} link {lid} 未记录在节点 {src} 的 outputs[{sslot}].links 里"
                f"（实际 {recorded}）—— 连线会显示不出来"
            )
