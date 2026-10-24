"""构建「单节点」工作流：W-S（本机体验）+ W-F（GPU 出片）。

## 设计原则（按用户要求）

> 将所有功能模块、参数配置全部整合到一个统一节点中，无需拆分为多个零散节点。

* **W-S**：`总控台 -> 预览`，只要 3 个节点。所有参数在总控台一张面板里。
* **W-F**：`总控台 -> 真实导演台 -> 合成 -> 出片`，8 个节点。
  为什么还要导演台节点？因为**真实出片是巨人本体**（MiniMaxH3Director 加载权重、
  逐段生成），不可能塞进编排节点 —— 这不是"零散拆分"，这是**必要的两段职责**：
  编排（我）/ 生成（巨人）。

## 用法

    python scripts/build_simple_workflows.py
"""

from __future__ import annotations

import argparse
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

PROJECT_ID = "我的短剧"     # 演示项目
SIMPLE = "S1_单节点体验（本机可跑）"
FULL = "S2_单节点出片（GPU）"


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


def _node(nid: int, ntype: str, pos, size, values) -> dict:
    return {"id": nid, "type": ntype, "pos": list(pos), "size": list(size),
            "flags": {}, "order": nid, "mode": 0, "inputs": [], "outputs": [],
            "properties": {}, "widgets_values": values}


def _link(wf, src, sslot, dst, dst_name, dtype):
    s = next(n for n in wf["nodes"] if n["id"] == src)
    d = next(n for n in wf["nodes"] if n["id"] == dst)
    wf["last_link_id"] += 1
    lid = wf["last_link_id"]
    while len(s.setdefault("outputs", [])) <= sslot:
        s["outputs"].append({"name": f"o{len(s['outputs'])}", "type": dtype, "links": []})
    s["outputs"][sslot].setdefault("links", []).append(lid)
    d.setdefault("inputs", []).append({"name": dst_name, "type": dtype, "link": lid})
    wf["links"].append([lid, src, sslot, dst, len(d["inputs"]) - 1, dtype])
    return lid


def build_master_values(info: dict, *, placeholder: bool) -> list:
    values = _defaults(info, "BetterComfyUIEverything")
    idx = _widget_index(info, "BetterComfyUIEverything")
    values[idx["剧本"]] = (
        "第一章 雪夜\n"
        "林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
        "林秀在桌边坐下，窗外雪落无声，灯影摇曳。"
    )
    values[idx["系统提示词选择"]] = "电影分镜（默认）"
    values[idx["自定义系统提示词"]] = ""
    values[idx["风格标签"]] = "@冷蓝电影光 @写实电影感"
    values[idx["统一风格"]] = "电影感，真实自然，低饱和，音画同步"
    values[idx["输出语言"]] = "中文"
    values[idx["分镜粒度"]] = "按镜头"
    values[idx["项目"]] = PROJECT_ID
    values[idx["每段时长_秒"]] = 5.0
    values[idx["总时长_秒"]] = 15          # 15 秒需求直接在这里填
    values[idx["目标分辨率"]] = "1080P"
    values[idx["帧率"]] = 24
    values[idx["启用素材库"]] = True
    values[idx["素材优先级"]] = "角色>场景>物品>分镜（推荐）"
    values[idx["段间素材"]] = ""
    values[idx["公共素材模式"]] = "整个素材库（推荐）"
    values[idx["素材审核模式"]] = "模型审核"
    values[idx["素材审核阈值"]] = 70
    values[idx["段间接续"]] = "自动（导演台尾部帧辅助，推荐）"
    values[idx["使用占位结果"]] = placeholder
    return values


def build_simple(info: dict) -> dict:
    """S1：总控台 -> 两个预览（本机可跑，不加载任何模型）。"""
    master = _node(1, "BetterComfyUIEverything", (80, 100), (460, 760),
                   build_master_values(info, placeholder=True))
    view_audit = _node(2, "PreviewAny", (620, 100), (460, 300), [])
    view_script = _node(3, "PreviewAny", (620, 440), (460, 300), [])
    wf = {"last_node_id": 3, "last_link_id": 0,
          "nodes": [master, view_audit, view_script],
          "links": [], "groups": [], "config": {}, "version": 0.4,
          "extra": {"note": (
              "【S1 单节点体验】全链路参数都在「一体化总控台」这一张面板里：\n"
              "剧本 / 系统提示词 / 风格标签 / 分辨率帧率 / 每段时长 / 总时长 / "
              "素材库 / 素材审核 / 接续方式 / 占位开关。\n"
              "本机点「队列」即可看到：审计报告 + 分镜脚本（不加载任何模型）。\n"
              "要看真实 DeepSeek 提示词：把「使用占位结果」改成 False"
              "（需先在 设置→StoryBoard.api_key 配好密钥）。"
          )},
          }
    _link(wf, 1, 4, 2, "source", "STRING")   # 审计报告
    _link(wf, 1, 1, 3, "source", "STRING")   # 分镜脚本
    return wf


def build_full(info: dict) -> dict:
    """S2：总控台 -> 真实导演台 -> 合成 -> 出片（GPU 机器跑）。"""
    base = json.loads(SRC_WF.read_text(encoding="utf-8"))
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "version": 0.4,
          "extra": {"note": (
              "【S2 单节点出片】编排全部在「一体化总控台」——"
              "剧本/提示词/风格/素材/审核/分段/分辨率/帧率/总时长都在这一个面板。\n"
              "右侧 7 个节点是**巨人本体**（加载权重 + 真实出片），不是零散拆分：\n"
              "  导演台 ← 总控台.导演组；加载器/合成/出片是它的固定配套。\n"
              "在 GPU 机器上点队列即出片。"
          )},
          }
    master = _node(1, "BetterComfyUIEverything", (-1500, 100), (460, 760),
                   build_master_values(info, placeholder=False))
    wf["nodes"].append(master)

    # 复用 demo 里的加载器与出片链，只把导演组来源换成总控台
    nid = 2
    for node in base["nodes"]:
        if node["type"] == "BetterComfyUIOneDirector":
            continue
        clone = json.loads(json.dumps(node))
        clone["id"] = nid
        clone["order"] = nid
        clone["inputs"] = []
        clone["outputs"] = []
        clone["pos"] = [node["pos"][0] + 1200, node["pos"][1]]
        wf["nodes"].append(clone)
        nid += 1

    by_type: dict[str, dict] = {}
    for node in wf["nodes"]:
        by_type.setdefault(node["type"], node)
    director = by_type.get("MiniMaxH3Director")
    if director is None:
        raise RuntimeError("基础工作流里没有 MiniMaxH3Director")
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])

    # 导演组 -> 导演台
    _link(wf, 1, 0, director["id"], "r2v_groups", "MMX_DIR_GROUP")
    # 加载器链（照抄 demo 的接线）
    unet = by_type.get("UNETLoader")
    lora = by_type.get("LoraLoaderModelOnly")
    clip = by_type.get("CLIPLoader")
    vaes = [n for n in wf["nodes"] if n["type"] == "VAELoader"]
    create = by_type.get("CreateVideo")
    save = by_type.get("SaveVideo")
    if lora is not None and unet is not None:
        _link(wf, unet["id"], 0, lora["id"], "model", "MODEL")
        _link(wf, lora["id"], 0, director["id"], "model", "MODEL")
    if clip is not None:
        _link(wf, clip["id"], 0, director["id"], "clip", "CLIP")
    if len(vaes) >= 2:
        _link(wf, vaes[0]["id"], 0, director["id"], "video_vae", "VAE")
        _link(wf, vaes[1]["id"], 0, director["id"], "audio_vae", "VAE")
    if create is not None:
        _link(wf, director["id"], 0, create["id"], "images", "IMAGE")
        _link(wf, director["id"], 1, create["id"], "audio", "AUDIO")
        _link(wf, director["id"], 2, create["id"], "fps", "FLOAT")
    if create is not None and save is not None:
        _link(wf, create["id"], 0, save["id"], "video", "VIDEO")

    # 审计预览（可选，便于排查）
    view = _node(nid, "PreviewAny", [-1500, 900], (460, 200), [])
    wf["nodes"].append(view)
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])
    _link(wf, 1, 4, view["id"], "source", "STRING")
    return wf


def validate(info: dict, wf: dict) -> list[str]:
    problems: list[str] = []
    ids = {n["id"] for n in wf["nodes"]}
    for node in wf["nodes"]:
        if node["type"] not in info:
            problems.append(f"节点不存在：{node['type']}")
    for (lid, src, sslot, dst, _d, dtype) in wf["links"]:
        if src not in ids or dst not in ids:
            problems.append(f"link {lid} 指向不存在节点")
            continue
        src_node = next(n for n in wf["nodes"] if n["id"] == src)
        outs = list(info[src_node["type"]].get("output") or [])
        if sslot >= len(outs):
            problems.append(f"link {lid} 源槽位越界 {src_node['type']}[{sslot}]")
        elif outs[sslot] != dtype:
            problems.append(f"link {lid} 类型不匹配 {outs[sslot]} -> {dtype}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    info = _object_info()

    problems: list[str] = []
    for name, builder in ((SIMPLE, build_simple), (FULL, build_full)):
        wf = builder(info)
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
