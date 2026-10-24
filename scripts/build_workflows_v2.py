"""基于**已验证的** `one_director_demo.json` 派生 4 套完整工作流。

## 为什么派生而不是从头拼

`one_director_demo.json` 的加载器链路（UNET -> LoRA -> Director，CLIP/VAE -> Director，
Director -> CreateVideo -> SaveVideo）是**在本机实测能跑通**的（19 项校验全过）。
从头拼装很容易在权重名/槽位上出错；派生只改"前半段"，后半段保持已验证的接线。

## 四套工作流

| 编号 | 名字 | 节点 | 用途 |
|---|---|---|---|
| W1 | 最简单入门 | 1 | 第一次用：只产导演组，自己接导演台 |
| W2 | DeepSeek 全自动 | 10 | 巨人拆剧本 → 桥接 → 导演台 → 出片 |
| W3 | 全流程出片 | 9 | OneDirector → 导演台 → 出片（不含 DeepSeek） |
| W4 | 素材参考图注入 | 10 | AssetSlots 的参考图接到 OneDirector |

## 用法

    python scripts/build_workflows_v2.py
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


def _object_info() -> dict:
    return json.load(urllib.request.urlopen(f"{BASE}/object_info", timeout=40))


def _defaults(info: dict, node: str) -> list:
    """按 schema 生成一份合法的 widgets 值列表（顺序与 schema 一致）。"""
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
            # 其余是连线端口，不进 widgets
    return values


def _widget_index(info: dict, node: str) -> dict[str, int]:
    """widget 名 -> 在 widgets_values 里的下标。"""
    spec = info[node]["input"]
    index: dict[str, int] = {}
    i = 0
    for section in (spec.get("required") or {}, spec.get("optional") or {}):
        for name, decl in section.items():
            dtype = decl[0]
            if (isinstance(dtype, list)
                    or dtype in ("STRING", "INT", "FLOAT", "BOOLEAN")):
                index[name] = i
                i += 1
    return index


def _clone(wf: dict) -> dict:
    return copy.deepcopy(wf)


def _find(wf: dict, node_type: str) -> dict:
    for node in wf["nodes"]:
        if node["type"] == node_type:
            return node
    raise KeyError(f"基础工作流里没有 {node_type}")


def _add_node(wf: dict, node: dict) -> dict:
    wf["nodes"].append(node)
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])
    return node


def _add_link(wf: dict, src_id: int, src_slot: int, dst_id: int,
             dst_name: str, dtype: str) -> int:
    """加一条连线，**同时**记录源节点 outputs.links（否则界面画不出线）。"""
    src = next(n for n in wf["nodes"] if n["id"] == src_id)
    dst = next(n for n in wf["nodes"] if n["id"] == dst_id)
    wf["last_link_id"] = int(wf.get("last_link_id") or 0) + 1
    lid = wf["last_link_id"]
    while len(src.setdefault("outputs", [])) <= src_slot:
        src["outputs"].append({"name": f"out{len(src['outputs'])}",
                               "type": dtype, "links": []})
    src["outputs"][src_slot].setdefault("links", []).append(lid)
    dst.setdefault("inputs", []).append(
        {"name": dst_name, "type": dtype, "link": lid})
    wf["links"].append([lid, src_id, src_slot, dst_id,
                        len(dst["inputs"]) - 1, dtype])
    return lid


def build_w1(info: dict, base: dict) -> dict:
    """W1：只留一体化导演台（最简单入门）。"""
    one = copy.deepcopy(_find(base, "BetterComfyUIOneDirector"))
    one.update({"id": 1, "pos": [120, 140], "inputs": [], "outputs": []})
    return {
        "last_node_id": 1, "last_link_id": 0,
        "nodes": [one], "links": [], "groups": [], "config": {},
        "extra": {"note": "最简单入门：只有一体化导演台。把「导演组」输出接到 "
                          "MiniMaxH3Director 的 r2v_groups 即可出片。"},
        "version": 0.4,
    }


def build_w3(info: dict, base: dict) -> dict:
    """W3：全流程出片（OneDirector -> 导演台 -> CreateVideo -> SaveVideo）。"""
    wf = _clone(base)
    wf["extra"] = {"note": "全流程出片：一体化导演台产导演组，真实导演台出片。"}
    return wf


def build_w2(info: dict, base: dict) -> dict:
    """W2：DeepSeek 全自动（巨人 -> 桥接 -> OneDirector -> 出片）。"""
    wf = _clone(base)
    one = _find(wf, "BetterComfyUIOneDirector")
    max_id = max(n["id"] for n in wf["nodes"])

    story_vals = _defaults(info, "StoryboardNode")
    s_idx = _widget_index(info, "StoryboardNode")
    story_vals[s_idx["chapter_text"]] = (
        "第一章 雪夜\n主角林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。")
    story = {
        "id": max_id + 1, "type": "StoryboardNode",
        "pos": [-1100, 100], "size": [400, 300], "flags": {}, "order": 0,
        "mode": 0, "inputs": [], "outputs": [], "properties": {},
        "widgets_values": story_vals,
    }
    _add_node(wf, story)

    bridge_vals = _defaults(info, "BetterComfyUIPromptBridge")
    b_idx = _widget_index(info, "BetterComfyUIPromptBridge")
    bridge_vals[b_idx["统一风格"]] = "@冷蓝电影光"
    bridge_vals[b_idx["段时长_秒"]] = 10.0
    bridge = {
        "id": max_id + 2, "type": "BetterComfyUIPromptBridge",
        "pos": [-620, 100], "size": [400, 320], "flags": {}, "order": 1,
        "mode": 0, "inputs": [], "outputs": [], "properties": {},
        "widgets_values": bridge_vals,
    }
    _add_node(wf, bridge)

    # StoryboardNode.video_prompts 是第 2 个输出（index 1）
    _add_link(wf, story["id"], 1, bridge["id"], "video_prompts", "STRING")
    # Bridge.剧本分镜 是第 1 个输出（index 0）-> OneDirector.剧本分镜 是 STRING widget
    # 前端把 widget 当输入时，input 名为 widget 名，但输入本身仍是端口 ——
    # 这里改成把 one 的「剧本分镜」widget 转为输入连接。
    one.setdefault("inputs", []).insert(0, {"name": "剧本分镜", "type": "STRING", "link": None})
    # 重新计算 link_id 并写入
    _add_link(wf, bridge["id"], 0, one["id"], "剧本分镜", "STRING")
    one["inputs"] = [i for i in one["inputs"] if i["name"] != "剧本分镜"][:0] + one["inputs"]
    # 去掉重复的占位 input
    seen = set()
    dedup = []
    for item in one["inputs"]:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        dedup.append(item)
    one["inputs"] = dedup

    one["pos"] = [-140, 100]
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])
    wf["extra"] = {"note": "DeepSeek 全自动：StoryboardGenerator 拆剧本 -> "
                           "提示词桥接 -> 一体化导演台 -> 真实导演台出片。\n"
                           "首次使用需在 StoryboardNode 里填 api_key。"}
    return wf


def build_w4(info: dict, base: dict) -> dict:
    """W4：素材参考图注入（AssetSlots 的 IMAGE 接到 OneDirector 参考图）。"""
    wf = _clone(base)
    one = _find(wf, "BetterComfyUIOneDirector")
    max_id = max(n["id"] for n in wf["nodes"])
    slots_vals = _defaults(info, "BetterComfyUIAssetSlots")
    slots = {
        "id": max_id + 1, "type": "BetterComfyUIAssetSlots",
        "pos": [-700, 100], "size": [400, 300], "flags": {}, "order": 0,
        "mode": 0, "inputs": [], "outputs": [], "properties": {},
        "widgets_values": slots_vals,
    }
    _add_node(wf, slots)
    # AssetSlots 的 9 个 IMAGE 输出 -> OneDirector 的参考图_1/2/3
    for i, target in enumerate(("参考图_1", "参考图_2", "参考图_3")):
        _add_link(wf, slots["id"], i, one["id"], target, "IMAGE")
    one["pos"] = [-140, 100]
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])
    wf["extra"] = {"note": "素材参考图注入：BetterComfyUIAssetSlots 把已 LOCK 的素材"
                           "变成 IMAGE，接到一体化导演台的参考图_1/2/3。"}
    return wf


BUILDERS = {
    "W1_最简单入门": (build_w1, "1 节点"),
    "W2_DeepSeek全自动": (build_w2, "10 节点"),
    "W3_全流程出片": (build_w3, "9 节点"),
    "W4_素材参考图注入": (build_w4, "10 节点"),
}


def validate(info: dict, wf: dict) -> list[str]:
    problems: list[str] = []
    ids = {n["id"] for n in wf["nodes"]}
    for node in wf["nodes"]:
        if node["type"] not in info:
            problems.append(f"节点不存在：{node['type']}")
    for (lid, src, sslot, dst, dslot, dtype) in wf["links"]:
        if src not in ids or dst not in ids:
            problems.append(f"link {lid} 指向不存在的节点")
            continue
        src_node = next(n for n in wf["nodes"] if n["id"] == src)
        dst_node = next(n for n in wf["nodes"] if n["id"] == dst)
        out_types = list(info[src_node["type"]].get("output") or [])
        if sslot >= len(out_types):
            problems.append(f"link {lid} 源槽位越界：{src_node['type']}[{sslot}]")
        elif out_types[sslot] != dtype:
            problems.append(
                f"link {lid} 类型不匹配：{src_node['type']}[{sslot}] 是 "
                f"{out_types[sslot]}，连到 {dst_node['type']}.{dst_node['inputs'][dslot]['name']} "
                f"要 {dtype}")
        # 源节点 outputs[sslot].links 必须记录了这条线，否则界面画不出
        links_on_src = (src_node.get("outputs") or [{}] * (sslot + 1))[sslot].get("links") or []
        if lid not in links_on_src:
            problems.append(f"link {lid} 没有记录在源节点 outputs[{sslot}].links 里")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    try:
        info = _object_info()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 连不上 ComfyUI：{exc}")
        return 1
    base = json.loads(SRC_WF.read_text(encoding="utf-8"))

    problems: list[str] = []
    for name, (builder, label) in BUILDERS.items():
        try:
            wf = builder(info, base)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: 构建失败 {type(exc).__name__}: {exc}")
            print(f"[FAIL] {name}: {exc}")
            continue
        found = validate(info, wf)
        problems.extend(found)
        print(f"[{'OK  ' if not found else 'FAIL'}] {name}（{label}）: "
              f"{len(wf['nodes'])} 节点 / {len(wf['links'])} 连线")
        for item in found:
            print(f"        - {item}")
        if not args.check and not found:
            text = json.dumps(wf, ensure_ascii=False, indent=2) + "\n"
            (OUT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
            UI_DIR.mkdir(parents=True, exist_ok=True)
            (UI_DIR / f"{name}.json").write_text(text, encoding="utf-8")
            print(f"        已安装到界面目录：{name}.json")

    if problems:
        print(f"\n共 {len(problems)} 个问题")
        return 1
    print(f"\n全部通过，安装目录：{UI_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
