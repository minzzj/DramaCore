"""构建完整工作流 W0（全链路连起来）+ W0L（本机可安全跑通版）。

## 修什么

用户实测指出：
1. **「API 配置节点的输出没接到任何节点」** —— 确实：`api_key/model/api_base`
   三个输出悬空，工作流是断的。
2. **「不是一个完整的工作流」** —— 剧本环节原来用巨人的 `StoryboardNode`，
   它没有 api_key 输入端口，接不上配置节点。

修法：新增 `BetterComfyUIStoryboard`（有 api_key/model/api_base 输入），
让 **配置 → 剧本 → 桥接 → 导演台 → 出片** 全链路真的连起来。

## 两套

| 名字 | 内容 | 在哪跑 |
|---|---|---|
| `W0_完整逻辑图全流程` | 配置→剧本→桥接→素材→导演台→真实导演台→合成→出片 | **GPU 机器** |
| `W0L_本机可跑通体验版` | 配置→剧本→桥接→素材→导演台→预览（**不加载任何模型**） | 本机也能跑 |

W0L 的意义：用户在本机就能按「队列」看到真实产出（导演组 JSON/审计信息），
不用等 GPU 机器；到 GPU 机器再跑 W0 出片。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

BASE = "http://127.0.0.1:8188"
SRC_WF = REPO_ROOT / "bettercomfyui_comfy" / "workflows" / "one_director_demo.json"
OUT_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
UI_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"

FULL_NAME = "W0_完整逻辑图全流程"
LITE_NAME = "W0L_本机可跑通体验版"

CHAPTER_TEXT = (
    "第一章 雪夜\n"
    "林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
    "林秀在桌边坐下，窗外雪落无声，灯影摇曳。"
)
GLOBAL_STYLE = "电影感，冷蓝夜色，低饱和，音画同步"

#: 真实项目 id（工作流里两个节点都要求它是合法 32 位 hex，否则执行报「需要 project_id」）
PROJECT_ID = "我的短剧"
PROJECT_NAME = "演示项目"


def _object_info() -> dict:
    return json.load(urllib.request.urlopen(f"{BASE}/object_info", timeout=40))


def _defaults(info: dict, node: str) -> list:
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


def _widget_index(info: dict, node: str) -> dict[str, int]:
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


def _node(node_id: int, node_type: str, pos, size, widget_values) -> dict:
    return {
        "id": node_id, "type": node_type,
        "pos": list(pos), "size": list(size),
        "flags": {}, "order": node_id, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": widget_values,
    }


def _link(wf: dict, src_id: int, src_slot: int, dst_id: int,
          dst_name: str, dtype: str) -> int:
    src = next(n for n in wf["nodes"] if n["id"] == src_id)
    dst = next(n for n in wf["nodes"] if n["id"] == dst_id)
    wf["last_link_id"] = int(wf.get("last_link_id") or 0) + 1
    lid = wf["last_link_id"]
    while len(src.setdefault("outputs", [])) <= src_slot:
        src["outputs"].append({
            "name": f"out{len(src['outputs'])}", "type": dtype, "links": []})
    src["outputs"][src_slot].setdefault("links", []).append(lid)
    dst.setdefault("inputs", []).append(
        {"name": dst_name, "type": dtype, "link": lid})
    wf["links"].append([lid, src_id, src_slot, dst_id,
                        len(dst["inputs"]) - 1, dtype])
    return lid


def build(info: dict, *, full: bool) -> dict:
    """构建 W0（full=True）或 W0L（full=False）。"""
    base = json.loads(SRC_WF.read_text(encoding="utf-8")) if full else {
        "last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
        "groups": [], "config": {}, "extra": {}, "version": 0.4,
    }
    wf = copy.deepcopy(base)
    for node in wf["nodes"]:
        node.setdefault("inputs", [])
        node.setdefault("outputs", [])
    next_id = max((n["id"] for n in wf["nodes"]), default=0)

    # ---- SG7 API 配置节点 ----
    settings_vals = _defaults(info, "BetterComfyUISettings")
    settings = _node(next_id + 1, "BetterComfyUISettings",
                     (-1700, 520), (360, 170), settings_vals)
    wf["nodes"].append(settings)

    # ---- SG1 剧本 -> 镜头提示词（可接密钥）----
    sb_vals = _defaults(info, "BetterComfyUIStoryboard")
    sb_idx = _widget_index(info, "BetterComfyUIStoryboard")
    sb_vals[sb_idx["剧本"]] = CHAPTER_TEXT
    sb_vals[sb_idx["段时长_秒"]] = 10.0
    # 本机无密钥：默认占位模式，工作流开箱即可跑通（输出首行大字标 ⚠️ 占位）
    sb_vals[sb_idx["使用占位结果"]] = True
    storyboard = _node(next_id + 2, "BetterComfyUIStoryboard",
                       (-1300, 100), (420, 360), sb_vals)
    wf["nodes"].append(storyboard)

    # 状态显示（配置节点的「状态」输出真的接到地方）
    status_view = _node(next_id + 3, "PreviewAny",
                        (-1700, 760), (420, 200), [])
    wf["nodes"].append(status_view)

    # ---- SG1 提示词桥接 ----
    br_vals = _defaults(info, "BetterComfyUIPromptBridge")
    br_idx = _widget_index(info, "BetterComfyUIPromptBridge")
    br_vals[br_idx["统一风格"]] = "@冷蓝电影光"
    br_vals[br_idx["段时长_秒"]] = 10.0
    bridge = _node(next_id + 4, "BetterComfyUIPromptBridge",
                   (-800, 100), (420, 340), br_vals)
    wf["nodes"].append(bridge)

    # ---- SG2 素材库 ----
    slots_vals = _defaults(info, "BetterComfyUIAssetSlots")
    sl_idx = _widget_index(info, "BetterComfyUIAssetSlots")
    slots_vals[sl_idx["project_id"]] = PROJECT_ID
    slots = _node(next_id + 5, "BetterComfyUIAssetSlots",
                  (-800, 520), (420, 240), slots_vals)
    wf["nodes"].append(slots)

    # ---- SG4 一体化导演台 ----
    one = next((n for n in wf["nodes"]
                if n["type"] == "BetterComfyUIOneDirector"), None)
    if one is None:
        one_vals = _defaults(info, "BetterComfyUIOneDirector")
        oi = _widget_index(info, "BetterComfyUIOneDirector")
        one_vals[oi["项目"]] = PROJECT_ID
        one_vals[oi["段时长_秒"]] = 10.0
        one_vals[oi["统一风格"]] = GLOBAL_STYLE
        one = _node(next_id + 6, "BetterComfyUIOneDirector",
                    (-300, 100), (440, 340), one_vals)
        wf["nodes"].append(one)
    one["pos"] = [-300, 100]

    # ---- 连线：配置 -> 剧本（**用户指出缺的就是这三根**）----
    _link(wf, settings["id"], 0, storyboard["id"], "api_key", "STRING")
    _link(wf, settings["id"], 1, storyboard["id"], "model", "STRING")
    _link(wf, settings["id"], 2, storyboard["id"], "api_base", "STRING")
    _link(wf, settings["id"], 3, status_view["id"], "source", "STRING")

    # ---- 连线：剧本 -> 桥接 -> 导演台 ----
    _link(wf, storyboard["id"], 0, bridge["id"], "video_prompts", "STRING")
    _link(wf, bridge["id"], 0, one["id"], "剧本分镜", "STRING")
    # ---- 素材 -> 参考图 ----
    for slot in range(3):
        _link(wf, slots["id"], slot, one["id"],
              ("参考图_1", "参考图_2", "参考图_3")[slot], "IMAGE")

    # ---- 审计预览 ----
    audit_view = _node(next_id + 7, "PreviewAny", (-300, 520), (440, 240), [])
    wf["nodes"].append(audit_view)
    _link(wf, one["id"], 3, audit_view["id"], "source", "STRING")

    if full:
        # 真实导演台的必需输入（i2v_groups 等是可选的，不接也合法）
        director = next((n for n in wf["nodes"]
                         if n["type"] == "MiniMaxH3Director"), None)
        if director is not None:
            _link(wf, one["id"], 0, director["id"], "r2v_groups", "MMX_DIR_GROUP")
            director["pos"] = [200, 100]

    wf["nodes"].sort(key=lambda n: n["id"])
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])
    wf["extra"] = {
        "note": (
            ("【完整逻辑图全流程 W0】\n" if full else
             "【本机可跑通体验版 W0L】不加载任何模型，本机也能点「队列」看真实产出。\n\n")
            + "SG7 全局配置 -> APIConfig（输出接到剧本节点的 api_key/model/api_base）\n"
            + "SG1 剧本→镜头提示词 -> 剧本节点 -> 提示词桥接\n"
            + "SG2 素材库 -> AssetSlots（已 LOCK 素材 -> IMAGE）\n"
            + "SG4 一体化导演台 -> 导演组\n"
            + ("SG4 真实导演台 -> 合成 -> 出片\n" if full else
               "（本机版止步于导演组 + 审计预览，不加载权重）\n")
            + "\n使用：① 在 ComfyUI 原生「设置 → StoryBoard」或 APIConfig 面板填 DeepSeek 密钥；"
              "② 把剧本节点的「使用占位结果」改成 False 走真实模型；③ 点队列。"
        ),
    }
    return wf


def validate(info: dict, wf: dict) -> list[str]:
    problems: list[str] = []
    ids = {n["id"] for n in wf["nodes"]}
    for node in wf["nodes"]:
        if node["type"] not in info:
            problems.append(f"节点不存在：{node['type']}")
    for (lid, src, sslot, dst, _dslot, dtype) in wf["links"]:
        if src not in ids or dst not in ids:
            problems.append(f"link {lid} 指向不存在的节点")
            continue
        src_node = next(n for n in wf["nodes"] if n["id"] == src)
        out_types = list(info[src_node["type"]].get("output") or [])
        if sslot >= len(out_types):
            problems.append(f"link {lid} 源槽位越界：{src_node['type']}[{sslot}]")
        elif out_types[sslot] != dtype:
            problems.append(
                f"link {lid} 类型不匹配：{src_node['type']}[{sslot}]={out_types[sslot]}"
                f" -> 需要 {dtype}")
        node_links = (src_node.get("outputs") or [{}] * (sslot + 1))[sslot].get("links") or []
        if lid not in node_links:
            problems.append(f"link {lid} 未记录在源节点 outputs[{sslot}].links")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    info = _object_info()
    problems: list[str] = []
    for name, full in ((FULL_NAME, True), (LITE_NAME, False)):
        wf = build(info, full=full)
        found = validate(info, wf)
        problems.extend(f"{name}: {p}" for p in found)
        print(f"[{'OK  ' if not found else 'FAIL'}] {name}: "
              f"{len(wf['nodes'])} 节点 / {len(wf['links'])} 连线")
        for item in found:
            print(f"        - {item}")
        if not args.check and not found:
            text = json.dumps(wf, ensure_ascii=False, indent=2) + "\n"
            (OUT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
            UI_DIR.mkdir(parents=True, exist_ok=True)
            (UI_DIR / f"{name}.json").write_text(text, encoding="utf-8")
            print(f"        已安装：{name}.json")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
