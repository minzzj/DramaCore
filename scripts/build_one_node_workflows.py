# -*- coding: utf-8 -*-
"""按「一个大节点」重建工作流（ROUND26，用户第二次强调）。

## 用户要求（原话）

> 「做成一个大节点就可以了，插上模型，插上输出，然后就可以用了……把它们整合起来，
> 整合的要完整，而不是重新造很多东西，或者把它生硬的接起来……我要它更加的自主化自动化。」

## 结果

`BetterComfyUIEverything`（一体化总控台）= **唯一大节点**：剧本进 → 内部一次 API 调用
产出 E1–E5 → 确定性匹配登记素材库 → 已 LOCK 素材全量挂每段 → 直接输出导演组。
用户只需要：
    [API 配置] ──(可选)──> [一体化总控台] ──导演组──> MiniMaxH3Director（插模型）
                                                    └─ IMAGE/AUDIO → CreateVideo → SaveVideo（插输出）

四套工作流：
* `W0_完整逻辑图全流程`：API 配置 → 大节点 → 导演台 → 合成 → 出片（GPU）；3 个预览看结果；
* `W2_DeepSeek全自动`：**最简形态**——大节点（密钥自动读服务器配置）→ 导演台 → 出片；
* `P1_剧本→全量提示词（本机可跑）`：API 配置 → 大节点 → 3 个预览；零权重；
* `W0L_本机可跑通体验版`：大节点（占位）→ 3 个预览；无密钥也能看结构。

生成后统一用真实 ComfyUI `/object_info` 做 schema 校验（复用 build_master_workflow.validate），
不通过不落盘。

用法（ComfyUI 在 8188 运行）：python -B scripts/build_one_node_workflows.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

BASE = "http://127.0.0.1:8188"
SRC_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
UI_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"

PROJECT_ID = "我的短剧"
#: 旧 W0 里的权重链节点（UNET/LoRA/CLIP/VAE×2/Director/CreateVideo/SaveVideo）
WEIGHT_CHAIN_IDS = {41, 42, 43, 44, 45, 46, 47, 48}

SCRIPT_TEXT = (
    "第一章 雪夜\n"
    "林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
    "林秀在桌边坐下，窗外雪落无声，灯影摇曳。"
)
GLOBAL_STYLE = "电影感，真实自然，冷蓝电影光，低饱和，音画同步"

PREVIEW_INPUT = "source"  # 实测 /object_info：PreviewAny.required.source


# ---------------------------------------------------------------------------
# /object_info schema 工具
# ---------------------------------------------------------------------------


def object_info() -> dict:
    return json.load(urllib.request.urlopen(f"{BASE}/object_info", timeout=60))


def node_defaults(info: dict, node: str) -> list:
    spec = info[node]["input"]
    values: list = []
    for section in (spec.get("required") or {}, spec.get("optional") or {}):
        for _name, decl in section.items():
            dtype = decl[0]
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
            if isinstance(dtype, list):
                values.append(opts.get("default", dtype[0] if dtype else ""))
            elif dtype == "STRING":
                values.append(opts.get("default", ""))
            elif dtype in ("INT", "FLOAT"):
                values.append(opts.get("default", opts.get("min", 0)))
            elif dtype == "BOOLEAN":
                values.append(opts.get("default", False))
    return values


def node_widget_index(info: dict, node: str) -> dict[str, int]:
    spec = info[node]["input"]
    index: dict[str, int] = {}
    i = 0
    for section in (spec.get("required") or {}, spec.get("optional") or {}):
        for name, decl in section.items():
            dtype = decl[0]
            if isinstance(dtype, list) or dtype in ("STRING", "INT", "FLOAT", "BOOLEAN"):
                index[name] = i
                i += 1
    return index


# ---------------------------------------------------------------------------
# 低层图操作（按输入名连线，槽位永不错位）
# ---------------------------------------------------------------------------


def _find(wf: dict, node_id: int) -> dict | None:
    return next((n for n in wf["nodes"] if n["id"] == node_id), None)


def _remove_link(wf: dict, link_id: int) -> None:
    for i, raw in enumerate(list(wf.get("links") or [])):
        if int(raw[0]) != int(link_id):
            continue
        lid, oid, oslot, tid, tslot = (int(raw[0]), int(raw[1]), int(raw[2]),
                                       int(raw[3]), int(raw[4]))
        src = _find(wf, oid)
        if src is not None:
            outs = src.get("outputs") or []
            if oslot < len(outs):
                outs[oslot]["links"] = [x for x in (outs[oslot].get("links") or [])
                                        if int(x) != lid]
        dst = _find(wf, tid)
        if dst is not None:
            ins = dst.get("inputs") or []
            if tslot < len(ins) and ins[tslot].get("link") is not None:
                if int(ins[tslot]["link"]) == lid:
                    ins[tslot]["link"] = None
        wf["links"].pop(i)
        return


def drop_nodes(wf: dict, node_ids: set[int]) -> None:
    for raw in list(wf.get("links") or []):
        if int(raw[1]) in node_ids or int(raw[3]) in node_ids:
            _remove_link(wf, int(raw[0]))
    wf["nodes"] = [n for n in wf["nodes"] if n["id"] not in node_ids]


def add_node(wf: dict, node_type: str, widget_values: list, *,
             pos=(0, 0), size=(420, 300)) -> dict:
    node_id = int(wf.get("last_node_id") or 0) + 1
    wf["last_node_id"] = node_id
    node = {
        "id": node_id, "type": node_type, "pos": list(pos), "size": list(size),
        "flags": {}, "order": node_id, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": widget_values,
    }
    wf["nodes"].append(node)
    return node


def add_link(wf: dict, src: dict, src_slot: int, dst: dict, dst_name: str, dtype: str) -> int:
    inputs = dst.setdefault("inputs", [])
    index = None
    for i, item in enumerate(inputs):
        if item.get("name") == dst_name:
            index = i
            break
    if index is None:
        inputs.append({"name": dst_name, "type": dtype, "link": None})
        index = len(inputs) - 1
    old = inputs[index].get("link")
    if old is not None:
        _remove_link(wf, int(old))

    outputs = src.setdefault("outputs", [])
    while len(outputs) <= src_slot:
        outputs.append({"name": f"out{len(outputs)}", "type": dtype, "links": []})
    actual = outputs[src_slot].get("type") or dtype
    if actual != dtype:
        raise ValueError(
            f"连线类型不匹配：{src['type']}[{src_slot}] 是 {actual}，"
            f"而 {dst['type']}.{dst_name} 期望 {dtype}")
    link_id = int(wf.get("last_link_id") or 0) + 1
    wf["last_link_id"] = link_id
    wf["links"].append([link_id, src["id"], src_slot, dst["id"], index, dtype])
    inputs[index]["link"] = link_id
    outputs[src_slot].setdefault("links", []).append(link_id)
    return link_id


def set_widget(node: dict, info: dict, key: str, value) -> None:
    idx = node_widget_index(info, node["type"])
    if key not in idx:
        raise ValueError(f"{node['type']} 没有 widget {key!r}；可用：{sorted(idx)}")
    node["widgets_values"][idx[key]] = value


# ---------------------------------------------------------------------------
# 节点工厂
# ---------------------------------------------------------------------------


def make_settings(wf: dict, info: dict, pos) -> dict:
    return add_node(wf, "BetterComfyUISettings",
                    node_defaults(info, "BetterComfyUISettings"),
                    pos=pos, size=(380, 180))


def make_big_node(wf: dict, info: dict, *, placeholder: bool, write_library: bool,
                  pos, project: str = PROJECT_ID) -> dict:
    values = node_defaults(info, "BetterComfyUIEverything")
    node = add_node(wf, "BetterComfyUIEverything", values, pos=pos, size=(520, 640))
    set_widget(node, info, "剧本", SCRIPT_TEXT)
    set_widget(node, info, "系统提示词选择", "默认（影视拆解，推荐）")
    set_widget(node, info, "自定义系统提示词", "")
    set_widget(node, info, "风格标签", "@冷蓝电影光")
    set_widget(node, info, "统一风格", GLOBAL_STYLE)
    set_widget(node, info, "输出语言", "中文")
    set_widget(node, info, "分镜粒度", "按镜头")
    set_widget(node, info, "项目", project)
    set_widget(node, info, "写入素材库", bool(write_library))
    set_widget(node, info, "每段时长_秒", 10.0)
    set_widget(node, info, "总时长_秒", 0)
    set_widget(node, info, "目标分辨率", "1080P")
    set_widget(node, info, "帧率", 24)
    set_widget(node, info, "启用素材库", True)
    set_widget(node, info, "素材优先级", "角色>场景>物品>分镜（推荐）")
    set_widget(node, info, "段间素材", "")
    set_widget(node, info, "公共素材模式", "整个素材库（推荐）")
    set_widget(node, info, "素材审核模式", "模型审核")
    set_widget(node, info, "素材审核阈值", 70)
    set_widget(node, info, "段间接续", "自动（导演台尾部帧辅助，推荐）")
    set_widget(node, info, "使用占位结果", bool(placeholder))
    return node


def make_preview(wf: dict, pos, title: str, *, size=(520, 260)) -> dict:
    node = add_node(wf, "PreviewAny", [], pos=pos, size=size)
    node["title"] = title
    return node


def wire_previews(wf: dict, big: dict, *, y0: int) -> None:
    """大节点的三条文本输出各接一个预览（结果可见：提示词JSON / 素材清单 / 审计报告）。"""
    for slot, label, dy in ((2, "提示词JSON（E1–E5 + 素材库统计）", 0),
                            (3, "素材清单（登记/复用/LOCK）", 300),
                            (4, "审计报告（API 调用次数/模式）", 600)):
        view = make_preview(wf, (-900, y0 + dy), label)
        add_link(wf, big, slot, view, PREVIEW_INPUT, "STRING")


# ---------------------------------------------------------------------------
# 四套工作流
# ---------------------------------------------------------------------------


def build_p1(info: dict) -> dict:
    """P1：本机可跑（零权重）——API 配置 + 大节点 + 预览。"""
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}
    settings = make_settings(wf, info, (-2200, 100))
    big = make_big_node(wf, info, placeholder=False, write_library=True, pos=(-1700, 100))
    add_link(wf, settings, 0, big, "api_key", "STRING")
    add_link(wf, settings, 1, big, "model", "STRING")
    add_link(wf, settings, 2, big, "api_base", "STRING")
    wire_previews(wf, big, y0=100)
    return wf


def build_w0(info: dict, base_wf: dict) -> dict:
    """W0：API 配置 → 大节点 → 导演台（插模型）→ 合成 → 出片（插输出）。"""
    wf = base_wf
    drop_nodes(wf, {n["id"] for n in wf["nodes"] if n["id"] not in WEIGHT_CHAIN_IDS})
    director = _find(wf, 46)
    settings = make_settings(wf, info, (-2200, 120))
    big = make_big_node(wf, info, placeholder=False, write_library=True, pos=(-1700, 120))
    add_link(wf, settings, 0, big, "api_key", "STRING")
    add_link(wf, settings, 1, big, "model", "STRING")
    add_link(wf, settings, 2, big, "api_base", "STRING")
    add_link(wf, big, 0, director, "r2v_groups", "MMX_DIR_GROUP")
    wire_previews(wf, big, y0=120)
    return wf


def build_w2(info: dict, base_wf: dict) -> dict:
    """W2：最简形态 —— 大节点（自动读服务器配置）→ 导演台 → 出片。"""
    wf = base_wf
    drop_nodes(wf, {n["id"] for n in wf["nodes"] if n["id"] not in WEIGHT_CHAIN_IDS})
    director = _find(wf, 46)
    big = make_big_node(wf, info, placeholder=False, write_library=True, pos=(-1700, 120))
    add_link(wf, big, 0, director, "r2v_groups", "MMX_DIR_GROUP")
    wire_previews(wf, big, y0=120)
    return wf


def build_w0l(info: dict) -> dict:
    """W0L：本机体验 —— 大节点占位（0 次 API）→ 预览。"""
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}
    big = make_big_node(wf, info, placeholder=True, write_library=False, pos=(-1700, 120))
    wire_previews(wf, big, y0=120)
    return wf


# ---------------------------------------------------------------------------
# 校验与安装
# ---------------------------------------------------------------------------


def install(info: dict, name: str, wf: dict) -> None:
    from build_master_workflow import validate

    problems = validate(info, wf)
    if problems:
        print(f"[FAIL] {name}: {len(problems)} 个问题")
        for problem in problems[:20]:
            print("   -", problem)
        raise SystemExit(1)
    text = json.dumps(wf, ensure_ascii=False, indent=2)
    (SRC_DIR / f"{name}.json").write_text(text, encoding="utf-8")
    UI_DIR.mkdir(parents=True, exist_ok=True)
    (UI_DIR / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"[OK] {name}: {len(wf['nodes'])} 节点 / {len(wf['links'])} 连线")


def main() -> int:
    info = object_info()
    missing = [n for n in ("BetterComfyUIEverything", "BetterComfyUISettings",
                           "MiniMaxH3Director", "PreviewAny") if n not in info]
    if missing:
        print(f"[FAIL] ComfyUI 里缺少节点：{missing}（先同步插件并重启）")
        return 1

    base_path = SRC_DIR / "W0_完整逻辑图全流程.json"
    base_wf = json.loads(base_path.read_text(encoding="utf-8"))
    for node in base_wf["nodes"]:
        node.setdefault("inputs", [])
        node.setdefault("outputs", [])

    install(info, "P1_剧本→全量提示词（本机可跑）", build_p1(info))
    install(info, "W0_完整逻辑图全流程", build_w0(info, json.loads(json.dumps(base_wf))))
    install(info, "W2_DeepSeek全自动", build_w2(info, json.loads(json.dumps(base_wf))))
    install(info, "W0L_本机可跑通体验版", build_w0l(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
