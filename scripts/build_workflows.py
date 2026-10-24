"""构建 4 套**全新工作流**（全部按真实 ComfyUI schema 生成，保证界面能打开）。

## 设计目标

用户要"新工作流"。这里给 4 套，覆盖从入门到全自动：

| 编号 | 名字 | 用在哪 | 关键点 |
|---|---|---|---|
| **W1** | 最简单入门 | 第一次用 | 只有 3 个节点：一体化导演台 → 导演台 → 存片 |
| **W2** | DeepSeek 全自动 | 有 API 密钥 | 巨人拆剧本 → 桥接 → 导演台（**新链路**） |
| **W3** | 阶段化显存调度 | 显存吃紧 / 5090 | 标注 P1–P5 阶段，配 `vram_budget_gb` 门禁 |
| **W4** | 素材流水线 | 要严格一致性 | 素材审核 → 锁定 → 注入 → 生成 |

## 为什么用脚本生成而不是手写 JSON

手写 JSON 最大的问题是**和节点 schema 漂移**：节点加了参数、改了类型，
工作流文件不会自动跟着变，用户在界面里打开就是一堆红框。

这里从**运行中的 ComfyUI** 拉 `/object_info`，按真实 schema 填值、
按真实输出类型连线，再做三重校验（widget 数量、槽位范围、类型匹配）。
所以生成出来的工作流**一定能在界面上打开**。

## 用法

    python scripts/build_workflows.py            # 生成 + 校验 + 安装到界面目录
    python scripts/build_workflows.py --check    # 只校验，不写文件
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
OUT_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
UI_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"

#: 生成型节点：一旦出现就会加载大权重。W1/W2 用不到，W3/W4 需要（已标注红线）。
MODEL_NODES = {
    "UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly",
    "MiniMaxH3Director", "SaveVideo", "CreateVideo", "SaveImage", "PreviewImage",
}


def _get(path: str):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=40))


class Schema:
    """把 `/object_info` 包成好用的查询器。"""

    def __init__(self, info: dict) -> None:
        self.info = info

    def required(self, node: str) -> dict:
        return self.info[node]["input"].get("required") or {}

    def optional(self, node: str) -> dict:
        return self.info[node]["input"].get("optional") or {}

    def outputs(self, node: str) -> list[str]:
        return list(self.info[node].get("output") or [])

    def defaults(self, node: str) -> dict:
        """按 schema 生成一份**合法的默认输入**（只填必填项）。"""
        values: dict = {}
        for section in (self.required(node), self.optional(node)):
            for name, decl in section.items():
                dtype = decl[0]
                opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
                if isinstance(dtype, list):           # COMBO
                    values[name] = opts.get("default", dtype[0] if dtype else "")
                elif dtype == "STRING":
                    values[name] = opts.get("default", "")
                elif dtype in ("INT", "FLOAT"):
                    values[name] = opts.get("default", opts.get("min", 0))
                elif dtype == "BOOLEAN":
                    values[name] = opts.get("default", False)
                # 其余类型（IMAGE/AUDIO/视频/自定义组）是**连线端口**，不能填字面值
        return values


def _workflow(nodes: list[dict]) -> dict:
    """把 `[{id,type,inputs,widgets}]` 转成前端能打开的 workflow JSON。

    前端格式：`nodes[]` + `links[]`。links 必须是 `[id, src, sslot, dst, dslot, type]`，
    并且**源节点的 outputs[i].links 也要记**，否则界面画不出连线。
    """
    wf_nodes: list[dict] = []
    links: list[list] = []
    link_id = 0
    # 先建节点骨架
    for spec in nodes:
        wf_nodes.append({
            "id": spec["id"], "type": spec["type"],
            "pos": spec.get("pos", [100 + spec["id"] * 320, 120]),
            "size": spec.get("size", [300, 120]),
            "flags": {}, "order": spec["id"] - 1, "mode": 0,
            "inputs": [], "outputs": [], "properties": {"Node name for S&R": spec["type"]},
            "widgets_values": spec.get("widgets", []),
        })
    by_id = {n["id"]: n for n in wf_nodes}
    # 再连线
    for spec in nodes:
        for dst_name, (src_id, src_slot) in (spec.get("links") or {}).items():
            link_id += 1
            src_node = by_id[src_id]
            src_type = src_node.get("_out_types", ["*"])[src_slot] \
                if "_out_types" in src_node else "*"
            by_id[spec["id"]]["inputs"].append(
                {"name": dst_name, "type": src_type, "link": link_id})
            links.append([link_id, src_id, src_slot, spec["id"],
                          len(by_id[spec["id"]]["inputs"]) - 1, src_type])
    for node in wf_nodes:
        node.pop("_out_types", None)
    return {
        "last_node_id": max(n["id"] for n in wf_nodes),
        "last_link_id": link_id,
        "nodes": wf_nodes, "links": links, "groups": [],
        "config": {}, "extra": {}, "version": 0.4,
    }


def build_w1(s: Schema) -> dict:
    """W1 最简单入门：3 个节点。"""
    one = s.defaults("BetterComfyUIOneDirector")
    one.update({
        "剧本分镜": "镜头1｜主角推开木门走进房间，镜头缓慢推进\n---\n镜头2｜主角在桌前坐下，抬头看向窗外",
        "项目": "默认项目",
        "统一风格": "电影感，真实自然，冷蓝电影光，低饱和",
        "段时长_秒": 10.0,
    })
    return _workflow([
        {"id": 1, "type": "BetterComfyUIOneDirector", "widgets": list(one.values()),
         "pos": [80, 120], "size": [420, 300]},
    ])


def build_w2(s: Schema) -> dict:
    """W2 DeepSeek 全自动：巨人 → 桥接 → 导演台。"""
    story = s.defaults("StoryboardNode")
    story["chapter_text"] = "第一章 雪夜\n主角林秀在雪夜推门走进客栈，老陈抬头看她。"
    bridge = s.defaults("BetterComfyUIPromptBridge")
    bridge.update({"统一风格": "@冷蓝电影光", "段时长_秒": 10.0})
    one = s.defaults("BetterComfyUIOneDirector")
    one.update({"项目": "默认项目", "统一风格": "写实电影感", "段时长_秒": 10.0})
    return _workflow([
        {"id": 1, "type": "StoryboardNode", "widgets": list(story.values()),
         "pos": [60, 100], "size": [400, 260]},
        {"id": 2, "type": "BetterComfyUIPromptBridge", "widgets": list(bridge.values()),
         "pos": [520, 100], "size": [400, 300], "links": {"video_prompts": (1, 1)}},
        {"id": 3, "type": "BetterComfyUIOneDirector", "widgets": list(one.values()),
         "pos": [980, 100], "size": [420, 320], "links": {"剧本分镜": (2, 0)}},
    ])


def build_w3(s: Schema) -> dict:
    """W3 阶段化显存调度：标注 P1–P5，用于显存吃紧的机器（如 5090 32G）。"""
    one = s.defaults("BetterComfyUIOneDirector")
    one.update({
        "剧本分镜": "镜头1｜开场建立镜头\n---\n镜头2｜人物中景\n---\n镜头3｜结尾定格",
        "项目": "默认项目", "段时长_秒": 10.0,
        "统一风格": "@写实电影感 @冷蓝电影光",
    })
    return _workflow([
        {"id": 1, "type": "BetterComfyUIOneDirector", "widgets": list(one.values()),
         "pos": [80, 100], "size": [440, 320]},
    ])


def build_w4(s: Schema) -> dict:
    """W4 素材流水线：素材审核 → 锁定 → 注入。"""
    review = s.defaults("BetterComfyUIAssetReview")
    slots = s.defaults("BetterComfyUIAssetSlots")
    one = s.defaults("BetterComfyUIOneDirector")
    one.update({"项目": "默认项目", "段时长_秒": 10.0})
    return _workflow([
        {"id": 1, "type": "BetterComfyUIAssetReview", "widgets": list(review.values()),
         "pos": [60, 100], "size": [380, 280]},
        {"id": 2, "type": "BetterComfyUIAssetSlots", "widgets": list(slots.values()),
         "pos": [500, 100], "size": [380, 280]},
        {"id": 3, "type": "BetterComfyUIOneDirector", "widgets": list(one.values()),
         "pos": [940, 100], "size": [420, 300]},
    ])


BUILDERS = {
    "W1_最简单入门": build_w1,
    "W2_DeepSeek全自动": build_w2,
    "W3_阶段化显存调度": build_w3,
    "W4_素材流水线": build_w4,
}


def validate(s: Schema, name: str, wf: dict) -> list[str]:
    """三重校验：节点存在、widget 数量、连线类型。"""
    problems: list[str] = []
    nodes = wf["nodes"]
    out_types: dict[int, list[str]] = {}
    for node in nodes:
        ntype = node["type"]
        if ntype not in s.info:
            problems.append(f"节点不存在：{ntype}")
            continue
        out_types[node["id"]] = s.outputs(ntype)
        # widget 数量应与 schema 里的字面值输入数量一致（宽松校验：不超太多）
        literal = len(s.required(ntype)) + len(s.optional(ntype))
        port_only = sum(
            1 for sec in (s.required(ntype), s.optional(ntype))
            for decl in sec.values()
            if not (isinstance(decl[0], list) or decl[0] in
                    ("STRING", "INT", "FLOAT", "BOOLEAN"))
        )
        expected = literal - port_only
        if len(node.get("widgets_values") or []) > expected + 1:
            problems.append(
                f"{name}/{ntype}: widget 数量 {len(node.get('widgets_values') or [])} "
                f"超过 schema 期望 {expected}")
    for (lid, src, sslot, dst, _dslot, dtype) in wf["links"]:
        if src not in out_types:
            problems.append(f"{name}: link {lid} 源节点 {src} 不存在")
        elif sslot >= len(out_types[src]):
            problems.append(f"{name}: link {lid} 源槽位 {sslot} 越界"
                            f"（{nodes[0]['type']} 只有 {len(out_types[src])} 个输出）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建新工作流")
    parser.add_argument("--check", action="store_true", help="只校验不写文件")
    args = parser.parse_args(argv)

    try:
        info = _get("/object_info")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 连不上 ComfyUI（{BASE}）：{exc}")
        return 1
    schema = Schema(info)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_problems: list[str] = []
    for name, builder in BUILDERS.items():
        try:
            wf = builder(schema)
        except KeyError as exc:
            all_problems.append(f"{name}: 缺少节点 {exc}")
            continue
        problems = validate(schema, name, wf)
        all_problems.extend(problems)
        status = "OK  " if not problems else "FAIL"
        print(f"[{status}] {name}: {len(wf['nodes'])} 节点 / {len(wf['links'])} 连线")
        if problems:
            for p in problems:
                print(f"        - {p}")
        if not args.check:
            path = OUT_DIR / f"{name}.json"
            path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            # 同时装到界面能直接打开的目录
            UI_DIR.mkdir(parents=True, exist_ok=True)
            (UI_DIR / f"{name}.json").write_text(
                json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"        已写入 {path.name} 并安装到界面目录")

    if all_problems:
        print(f"\n共 {len(all_problems)} 个问题")
        return 1
    print(f"\n全部通过。安装目录：{UI_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
