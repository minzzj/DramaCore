"""构建「完整逻辑图工作流」W0 + 把 API 配置同步给剧本巨人。

## 用户提的两个问题

1. **「你没有设置 DeepSeek 之类的 API 的配置接口，并不能真正实现」**
   —— 剧本巨人 `ComfyUI-Storyboard-LLM` **没有 api_key 输入端口**，
   它读自己目录下的 `settings.json`（`api_key` / `base_url` / `output_path`）。
   所以只在我的面板里填密钥**不会**让它生效，必须**同时写它的 settings.json**。
   本脚本提供 `sync_giant_settings()` 做这件事。

2. **「制作一套完整的工作流，可以实现我们那套逻辑图」**
   —— 本脚本构建 `W0_完整逻辑图全流程.json`，把图一 SG1–SG7 串起来。

## W0 覆盖的流程图范围

| 图一子图 | 在 W0 里怎么体现 |
|---|---|
| SG7 全局配置 | `BetterComfyUISettings` 读取服务器 API 配置 + 状态显示 |
| SG1 剧本→提示词 | `StoryboardNode`（DeepSeek 拆剧本）→ `BetterComfyUIPromptBridge` |
| SG2 素材库 | `BetterComfyUIAssetSlots`（已 LOCK 素材 → IMAGE） |
| SG3 素材审核 | 由一体化导演台/编排层保证（节点内做 R3 门禁校验） |
| SG4 导演台 | `BetterComfyUIOneDirector` → `MiniMaxH3Director` |
| SG5 视频审核 | 导演台内部产出后由编排层审核（见 W3） |
| SG6 成片输出 | `CreateVideo` → `SaveVideo` |
| SG8 异常/中断 | 由编排层（`run_demo`）承担，工作流层面无独立节点 |

## 用法

    python scripts/build_master_workflow.py
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
GIANT_DIR = REPO_ROOT / ".comfyui" / "custom_nodes" / "ComfyUI-Storyboard-LLM"
NAME = "W0_完整逻辑图全流程"


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


def _add_link(wf: dict, src_id: int, src_slot: int, dst_id: int,
              dst_name: str, dtype: str) -> int:
    src = next(n for n in wf["nodes"] if n["id"] == src_id)
    dst = next(n for n in wf["nodes"] if n["id"] == dst_id)
    wf["last_link_id"] = int(wf.get("last_link_id") or 0) + 1
    lid = wf["last_link_id"]
    while len(src.setdefault("outputs", [])) <= src_slot:
        src["outputs"].append({"name": f"out{len(src['outputs'])}",
                               "type": dtype, "links": []})
    src["outputs"][src_slot].setdefault("links", []).append(lid)
    if dst_name not in [i["name"] for i in dst.get("inputs", [])]:
        dst.setdefault("inputs", []).append(
            {"name": dst_name, "type": dtype, "link": lid})
    wf["links"].append([lid, src_id, src_slot, dst_id,
                        len(dst["inputs"]) - 1, dtype])
    return lid


def sync_giant_settings(*, api_key: str, base_url: str, output_path: str = "") -> dict:
    """把 API 配置写进剧本巨人的 `settings.json`。

    **这一步不做，界面上填了密钥巨人也不认**：它没有 api_key 输入端口，
    只读自己目录下的 settings.json。
    """
    if not GIANT_DIR.is_dir():
        return {"ok": False, "reason": f"巨人包未安装：{GIANT_DIR}"}
    target = GIANT_DIR / "settings.json"
    payload = {
        "api_key": api_key,
        "base_url": base_url or "https://api.deepseek.com",
        "output_path": output_path,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return {"ok": True, "path": str(target)}


def build(info: dict) -> dict:
    base = json.loads(SRC_WF.read_text(encoding="utf-8"))
    wf = copy.deepcopy(base)
    one = next(n for n in wf["nodes"] if n["type"] == "BetterComfyUIOneDirector")
    next_id = max(n["id"] for n in wf["nodes"])

    # ---- SG7 全局配置：API 配置节点 ----
    settings = {
        "id": next_id + 1, "type": "BetterComfyUISettings",
        "pos": [-1500, 500], "size": [340, 160],
        "flags": {}, "order": 0, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": _defaults(info, "BetterComfyUISettings"),
    }
    wf["nodes"].append(settings)

    status_view = {
        "id": next_id + 2, "type": "PreviewAny",
        "pos": [-1500, 720], "size": [420, 220],
        "flags": {}, "order": 1, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": [],
    }
    wf["nodes"].append(status_view)
    _add_link(wf, settings["id"], 3, status_view["id"], "source", "STRING")

    # ---- SG1 剧本 -> 提示词：巨人 + 桥接 ----
    story_vals = _defaults(info, "StoryboardNode")
    s_idx = _widget_index(info, "StoryboardNode")
    story_vals[s_idx["chapter_text"]] = (
        "第一章 雪夜\n主角林秀在雪夜推门走进客栈，老陈抬头看她，两人对视。\n"
        "林秀在桌边坐下，窗外雪落无声。")
    story = {
        "id": next_id + 3, "type": "StoryboardNode",
        "pos": [-1500, 100], "size": [420, 320],
        "flags": {}, "order": 2, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": story_vals,
    }
    wf["nodes"].append(story)

    bridge_vals = _defaults(info, "BetterComfyUIPromptBridge")
    b_idx = _widget_index(info, "BetterComfyUIPromptBridge")
    bridge_vals[b_idx["统一风格"]] = "@冷蓝电影光"
    bridge_vals[b_idx["段时长_秒"]] = 10.0
    bridge = {
        "id": next_id + 4, "type": "BetterComfyUIPromptBridge",
        "pos": [-1000, 100], "size": [400, 320],
        "flags": {}, "order": 3, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": bridge_vals,
    }
    wf["nodes"].append(bridge)
    _add_link(wf, story["id"], 1, bridge["id"], "video_prompts", "STRING")

    # ---- SG2 素材库：已 LOCK 素材 -> IMAGE ----
    slots_vals = _defaults(info, "BetterComfyUIAssetSlots")
    slots = {
        "id": next_id + 5, "type": "BetterComfyUIAssetSlots",
        "pos": [-1000, 500], "size": [400, 240],
        "flags": {}, "order": 4, "mode": 0,
        "inputs": [], "outputs": [], "properties": {},
        "widgets_values": slots_vals,
    }
    wf["nodes"].append(slots)

    # ---- SG4 衔接：桥接 -> 一体化导演台；素材图 -> 参考图 ----
    one.setdefault("inputs", [])
    if "剧本分镜" not in [i["name"] for i in one["inputs"]]:
        one["inputs"].append({"name": "剧本分镜", "type": "STRING", "link": None})
    _add_link(wf, bridge["id"], 0, one["id"], "剧本分镜", "STRING")
    for i in range(3):
        _add_link(wf, slots["id"], i, one["id"],
                  ("参考图_1", "参考图_2", "参考图_3")[i], "IMAGE")

    one["pos"] = [-480, 100]
    wf["nodes"].sort(key=lambda n: n["id"])
    wf["last_node_id"] = max(n["id"] for n in wf["nodes"])
    wf["extra"] = {
        "note": (
            "【完整逻辑图全流程 W0】\n"
            "SG7 全局配置 -> BetterComfyUISettings（读取服务器 API 配置）\n"
            "SG1 剧本->提示词 -> StoryboardNode(DeepSeek) -> 提示词桥接\n"
            "SG2 素材库 -> AssetSlots（把已 LOCK 的素材变成 IMAGE）\n"
            "SG4 导演台 -> 一体化导演台 -> MiniMaxH3Director\n"
            "SG6 成片输出 -> CreateVideo -> SaveVideo\n\n"
            "⚠️ 使用前：在右侧 Better ComfyUI 面板「API 配置」里填 DeepSeek 密钥，\n"
            "   保存时会同时写入剧本巨人的 settings.json（否则巨人拿不到密钥）。\n"
            "⚠️ 本机无 GPU，真实出片需在 24G+ 显存的机器上跑；"
            "否则请用 W1/W3 查看导演组结构。"
        ),
    }
    return wf


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
        out_types = list(info[src_node["type"]].get("output") or [])
        if sslot >= len(out_types):
            problems.append(f"link {lid} 源槽位越界：{src_node['type']}[{sslot}]")
        elif out_types[sslot] != dtype and dtype != "*":
            problems.append(
                f"link {lid} 类型不匹配：{src_node['type']}[{sslot}] 输出 "
                f"{out_types[sslot]}，连线类型 {dtype}")
        node_links = (src_node.get("outputs") or [{}] * (sslot + 1))[sslot].get("links") or []
        if lid not in node_links:
            problems.append(f"link {lid} 未记录在源节点 outputs[{sslot}].links")
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

    wf = build(info)
    problems = validate(info, wf)
    print(f"[{'OK  ' if not problems else 'FAIL'}] {NAME}: "
          f"{len(wf['nodes'])} 节点 / {len(wf['links'])} 连线")
    for item in problems:
        print(f"        - {item}")
    if problems or args.check:
        return 1 if problems else 0

    text = json.dumps(wf, ensure_ascii=False, indent=2) + "\n"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    UI_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{NAME}.json").write_text(text, encoding="utf-8")
    (UI_DIR / f"{NAME}.json").write_text(text, encoding="utf-8")
    print(f"        已安装到界面目录：{NAME}.json")
    print(f"\n节点清单：")
    for node in wf["nodes"]:
        print(f"  id={node['id']:<3} {node['type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
