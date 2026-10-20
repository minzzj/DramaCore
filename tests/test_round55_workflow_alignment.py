# -*- coding: utf-8 -*-
"""ROUND55 发布前守卫：单节点工作流 widgets_values 必须与节点定义（按名）对齐。

背景（ROUND55 终检 P0-1）：工作流若按旧节点顺序保存，会把参数整体错位（如"转场"位置变成 18）。
本测试按 ComfyUI 加载时的真实顺序（定义顺序 + 4 个分组 button + control_after_generate）重建
期望位置，并逐项检查关键参数值，防止再生成一次错位工作流。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop  # noqa: E402

WF = ROOT / "bettercomfyui_comfy/workflows/00_【单节点】剧本→成片（无外部依赖）.json"
BUTTONS = {
    "音频VAE": "素材 Assets ▾",
    "图像步数": "视频 Video ▾",
    "语义桥alpha": "输出 Output ▾",
    "使用占位结果": "审核 Review ▾",
}


def _expected_order() -> list[str]:
    it = BetterComfyUIOneStop.INPUT_TYPES()
    defs = list(it["required"].keys()) + list(it.get("optional", {}).keys())
    order: list[str] = []
    for name in defs:
        order.append(name)
        if name == "seed":
            order.append("control_after_generate")
        if name in BUTTONS:
            order.append(BUTTONS[name])
    return order


def test_workflow_widgets_aligned_by_name():
    data = json.loads(WF.read_text(encoding="utf-8"))
    node = next(n for n in data["nodes"] if n.get("type") == "BetterComfyUIOneStop")
    values = node["widgets_values"]
    order = _expected_order()
    assert len(order) == len(values), (
        f"工作流 values 数量 {len(values)} != 期望加载顺序 {len(order)}：请重新生成工作流"
    )
    pos = {name: i for i, name in enumerate(order)}

    def val(name: str):
        return values[pos[name]]

    assert str(val("剧本")).strip(), "剧本默认内容不应为空（否则首次体验为空）"
    assert val("项目") == "剧核试用"
    assert val("输出文件名") == "大节点成片.mp4"
    assert float(val("每段时长_秒")) == 10.0
    assert int(val("宽度")) == 1920 and int(val("高度")) == 1088
    assert int(val("素材最多生成个数")) == 12
    assert val("多段剧本列表JSON") == ""
    assert int(val("CNT2上限")) == 3 and int(val("CNT3上限")) == 3
    assert val("走Finalize剪辑") is True
    assert val("转场") == "AI自动（按剧情）"
    assert float(val("转场时长_秒")) == 0.5
    assert val("片头片尾") == "无"
    assert val("片头文字") == "剧核 DramaCore"
    assert val("片尾文字") == "剧终"
    assert val("片尾演职员") == "剧核 DramaCore 出品"
    assert val("字幕") == "硬字幕（程序对齐）" and val("字幕台词") == ""
    assert int(val("tiled采样")) == 0 and int(val("tiled重叠像素")) == 64
    assert int(val("审核阈值")) == 70
    assert val("整片失败策略") == "按项目配置"
    assert val("使用占位结果") is False


def test_new_params_are_real_inputs():
    it = BetterComfyUIOneStop.INPUT_TYPES()
    names = list(it["required"].keys()) + list(it.get("optional", {}).keys())
    for name in ("片头片尾", "片头文字", "片尾文字", "片尾演职员",
                 "转场时长_秒", "tiled重叠像素", "字幕台词"):
        assert name in names, f"缺参数 {name}"
