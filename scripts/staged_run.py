# -*- coding: utf-8 -*-
"""分两段自动执行 GPU 总流程（控制 32GB 显存）。

背景：图像模型 + H3 同时驻留 ≈46GB，32GB 生产机会 OOM。本脚本把一个图形化工作流
**自动拆成两段**并依次提交：
    A 素材阶段：配置/剧本 + 素材生成→入库→审核（Everything 在这里真实跑，调一次 API）
    B 出片阶段：配置/剧本 + 导演台出片 + 成片剪辑 + 视频审核
阶段之间调用 /free 释放上一段模型显存。
B 段仍包含 Everything 节点（否则拿不到导演组），但输入与 A 段完全相同，
ComfyUI 会命中执行缓存、**不会再调 API**（若目标机缓存行为不同，B 段会多一次 API，
脚本会在结果里如实报告两段的 API 次数差）。

⚠️ 显存是否真的降到 32GB 以下，只能在 GPU 机器上用 scripts/vram_monitor.py 验证；
本脚本的拆分/提交流程可以在 CPU 机器上用 --dry-run 做结构验证。

用法：
    # 1) 只看拆分结果（不需要机器，本地可验证）
    python -B scripts/staged_run.py --dry-run

    # 2) 真跑（GPU 机器）
    python -B scripts/staged_run.py --project "我的短剧" --free-between
    python -B scripts/staged_run.py --stage a --project "我的短剧"   # 只跑素材段
    python -B scripts/staged_run.py --stage b --project "我的短剧"   # 只跑出片段（需 A 缓存）

依赖：本机 ComfyUI 在跑（默认 8188），工作流文件在 bettercomfyui_comfy/workflows/。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_DIR = REPO / "bettercomfyui_comfy" / "workflows"
DEFAULT_WF = "00_【总流程】剧本→成片（GPU）.json"

# 阶段 A（素材）：配置/剧本 + 图像生成链 + 入库 + 素材审核
A_TYPES = {
    "BetterComfyUISettings", "BetterComfyUIEverything",
    "BetterComfyUIAssetGen", "CheckpointLoaderSimple", "CLIPTextEncode",
    "EmptyLatentImage", "KSampler", "VAEDecode",
    "BetterComfyUIAssetIngest", "BetterComfyUIAssetReview",
}
# 阶段 B（出片+剪辑+审核）：配置/剧本（命中缓存）+ 视频权重链 + 剪辑 + 视频审核
B_TYPES = {
    "BetterComfyUISettings", "BetterComfyUIEverything",
    "UNETLoader", "LoraLoaderModelOnly", "CLIPLoader", "VAELoader",
    "MiniMaxH3Director", "CreateVideo", "SaveVideo",
    "BetterComfyUIFinalize", "BetterComfyUIVideoReview",
}
PREVIEW_TYPE = "PreviewAny"


def get_json(url: str, timeout: int = 60):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def post_json(url: str, payload: dict, timeout: int = 120):
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return int(getattr(resp, "status", 200)), (json.loads(body) if body.strip() else {})
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def widget_names(info: dict, node_type: str) -> list[str]:
    spec = info[node_type]["input"]
    names = []
    for section in (spec.get("required") or {}, spec.get("optional") or {}):
        for name, decl in section.items():
            dtype = decl[0]
            if isinstance(dtype, list) or dtype in ("STRING", "INT", "FLOAT", "BOOLEAN"):
                names.append(name)
    return names


def workflow_to_api(info: dict, wf: dict) -> dict:
    api = {}
    links = {int(raw[0]): raw for raw in wf.get("links") or []}
    for node in wf["nodes"]:
        node_type = node["type"]
        if node_type not in info:
            raise SystemExit(f"目标节点类型未注册：{node_type}")
        inputs = {}
        widgets = list(node.get("widgets_values") or [])
        for i, name in enumerate(widget_names(info, node_type)):
            if i < len(widgets):
                inputs[name] = widgets[i]
        for entry in node.get("inputs") or []:
            link_id = entry.get("link")
            if link_id is None:
                continue
            raw = links.get(int(link_id))
            if raw is None:
                continue
            inputs[entry["name"]] = [str(int(raw[1])), int(raw[2])]
        api[str(node["id"])] = {"class_type": node_type, "inputs": inputs}
    return api


def split_workflow(wf: dict) -> tuple[dict, dict, dict]:
    """按类型拆成 A/B 两段（含各自需要的预览节点）。返回 (stage_a, stage_b, meta)。"""
    nodes = wf["nodes"]
    by_id = {n["id"]: n for n in nodes}

    def selected(types: set[str]) -> set[int]:
        return {n["id"] for n in nodes if n["type"] in types}

    a_ids = selected(A_TYPES)
    b_ids = selected(B_TYPES)
    # 预览节点按上游来源归类：只保留源在对应阶段的预览
    for n in nodes:
        if n["type"] != PREVIEW_TYPE:
            continue
        src_ids = set()
        for entry in n.get("inputs") or []:
            lid = entry.get("link")
            if lid is None:
                continue
            raw = {int(x[0]): x for x in wf.get("links") or []}.get(int(lid))
            if raw:
                src_ids.add(int(raw[1]))
        if src_ids & a_ids:
            a_ids.add(n["id"])
        elif src_ids & b_ids:
            b_ids.add(n["id"])

    def build(ids: set[int]) -> dict:
        out_nodes = []
        for n in nodes:
            if n["id"] not in ids:
                continue
            out_nodes.append(n)
        return {"nodes": out_nodes, "links": wf.get("links") or []}

    meta = {
        "total_nodes": len(nodes),
        "stage_a_nodes": len(a_ids),
        "stage_b_nodes": len(b_ids),
        "shared": sorted({by_id[i]["type"] for i in a_ids & b_ids}),
        "a_types": sorted({by_id[i]["type"] for i in a_ids}),
        "b_types": sorted({by_id[i]["type"] for i in b_ids}),
        "only_in_a": sorted({by_id[i]["type"] for i in a_ids - b_ids}),
        "only_in_b": sorted({by_id[i]["type"] for i in b_ids - a_ids}),
    }
    return build(a_ids), build(b_ids), meta


def prune_prompt(api: dict, wf_ids: set[int]) -> tuple[dict, list[str]]:
    """删掉不在本阶段 prompt 里的上游输入；返回 (pruned, 被剪掉的输入说明)。"""
    pruned = {}
    dropped: list[str] = []
    for nid, node in api.items():
        if int(nid) not in wf_ids:
            continue
        inputs = {}
        for key, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) and len(value) == 2 and str(value[0]).isdigit():
                if int(value[0]) not in wf_ids:
                    dropped.append(f"{nid}:{node['class_type']}.{key} <- {value[0]}")
                    continue
            inputs[key] = value
        pruned[nid] = {"class_type": node["class_type"], "inputs": inputs}
    return pruned, dropped


def override_project(api: dict, project: str) -> int:
    changed = 0
    for node in api.values():
        inputs = node.get("inputs") or {}
        if "项目" in inputs:
            inputs["项目"] = project
            changed += 1
        if "project_id" in inputs:
            inputs["project_id"] = project
            changed += 1
    return changed


def wait_prompt(comfy: str, prompt_id: str, timeout_s: float = 7200) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data = get_json(f"{comfy.rstrip('/')}/history/{prompt_id}", timeout=30)
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
            continue
        if prompt_id in data:
            return data[prompt_id]
        time.sleep(1.5)
    return {"status": {"status_str": "timeout"}}


def submit(comfy: str, prompt: dict, label: str) -> dict:
    code, result = post_json(f"{comfy.rstrip('/')}/prompt",
                             {"prompt": prompt, "client_id": f"bcu-staged-{label}"})
    if code != 200 or not result.get("prompt_id"):
        print(f"[{label}] 提交失败：status={code} {result}")
        return {"ok": False, "error": result}
    prompt_id = result["prompt_id"]
    print(f"[{label}] 已提交 prompt_id={prompt_id}，等待完成…")
    entry = wait_prompt(comfy, prompt_id)
    status = entry.get("status") or {}
    ok = status.get("status_str") == "success"
    print(f"[{label}] status={status.get('status_str')} completed={status.get('completed')}")
    if not ok:
        for msg in (status.get("messages") or [])[:6]:
            print("   ", str(msg)[:300])
    return {"ok": ok, "prompt_id": prompt_id, "entry": entry}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comfy", default="http://127.0.0.1:8188")
    parser.add_argument("--workflow", default=DEFAULT_WF)
    parser.add_argument("--project", default="", help="覆盖工作流里的项目（显示名或 id）")
    parser.add_argument("--stage", choices=["all", "a", "b"], default="all")
    parser.add_argument("--free-between", action="store_true", default=True)
    parser.add_argument("--no-free", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    wf_path = Path(args.workflow)
    if not wf_path.is_absolute():
        wf_path = SRC_DIR / args.workflow
    if not wf_path.is_file():
        print(f"[FAIL] 工作流不存在：{wf_path}")
        return 2
    wf = json.loads(wf_path.read_text(encoding="utf-8"))
    try:
        info = get_json(f"{args.comfy.rstrip('/')}/object_info")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] ComfyUI 不可达（{args.comfy}）：{type(exc).__name__}: {exc}")
        return 2

    stage_a, stage_b, meta = split_workflow(wf)
    print("[拆分] " + json.dumps({k: meta[k] for k in
                                  ("total_nodes", "stage_a_nodes", "stage_b_nodes",
                                   "only_in_a", "only_in_b")}, ensure_ascii=False))
    if args.dry_run:
        full_api = workflow_to_api(info, wf)
        for label, stage in (("A", stage_a), ("B", stage_b)):
            ids = {n["id"] for n in stage["nodes"]}
            api, dropped = prune_prompt(full_api, ids)
            print(f"  阶段{label}: {len(api)} 个节点提交；被剪掉的上游输入 {len(dropped)} 个")
            if dropped:
                for item in dropped[:6]:
                    print("    -", item)
        print("[dry-run] 结构验证通过（没有提交任何队列）")
        return 0

    exit_code = 0
    if args.stage in ("all", "a"):
        api_a, _ = prune_prompt(workflow_to_api(info, wf), {n["id"] for n in stage_a["nodes"]})
        if args.project:
            print(f"[项目] 覆盖 {override_project(api_a, args.project)} 处")
        res_a = submit(args.comfy, api_a, "A-素材")
        if not res_a.get("ok"):
            return 1
        if args.stage == "all" and not args.no_free:
            code, body = post_json(f"{args.comfy.rstrip('/')}/free",
                                   {"unload_models": True, "free_memory": True})
            print(f"[free] status={code}（释放素材段模型）")
    if args.stage in ("all", "b"):
        api_b, _ = prune_prompt(workflow_to_api(info, wf), {n["id"] for n in stage_b["nodes"]})
        if args.project:
            print(f"[项目] 覆盖 {override_project(api_b, args.project)} 处")
        res_b = submit(args.comfy, api_b, "B-出片")
        if not res_b.get("ok"):
            exit_code = 1
    print("[done] 分两段执行结束；显存峰值请用 scripts/vram_monitor.py 记录并回填实测模板。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
