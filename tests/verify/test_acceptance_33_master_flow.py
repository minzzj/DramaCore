"""验收 33：第一阶段「总流程」工作流（人工一段剧本 → 全部内容）。

用户要求（ROUND28）：
* 「制作一个完整的工作流，更好的实现逻辑图内容，真正自动化」；
* 「第一阶段主要完成的就是输入一段剧本即可完整输出全部内容」；
* 「工作流不需要用特别特别完整的」；「普通用户也能看懂，不要过于复杂」；
* 「视频审核现在看不到」→ 必须出现在总流程里且节点可见。

判定通过：
    P1 本机版总流程：schema 合法、8 节点、有中文说明区、≥4 个结果预览、分组清晰；
    P2 GPU 版总流程：schema 合法、含 素材生成/入库/素材审核/导演台/视频审核/出片、有说明区与分组；
    P3 本机版**真实执行**：一段剧本 → success，预览含 提示词JSON(E1–E5)+导演组+素材清单+审计；
    P4 视频审核可见：/object_info 有 BetterComfyUIVideoReview、S4 工作流在界面目录、GPU 总流程包含它。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

UI_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"
SRC_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
BASE = "http://127.0.0.1:8188"
LOCAL_NAME = "00_【总流程】剧本→全部内容（本机可跑）.json"
GPU_NAME = "00_【总流程】剧本→成片（GPU）.json"


def _get(url, timeout=60):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _post(url, payload, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def _object_info():
    try:
        return _get(f"{BASE}/object_info", timeout=20)
    except Exception:  # noqa: BLE001
        pytest.skip("未验证：本机 ComfyUI 不可达")


def _load(name: str) -> dict:
    return json.loads((SRC_DIR / name).read_text(encoding="utf-8"))


def _types(wf: dict) -> set[str]:
    return {n["type"] for n in wf["nodes"]}


def _has_note(wf: dict) -> bool:
    return "PrimitiveStringMultiline" in _types(wf)


def _preview_links(wf: dict) -> int:
    by_id = {n["id"]: n for n in wf["nodes"]}
    count = 0
    for link in wf.get("links") or []:
        src = by_id.get(int(link[1]))
        dst = by_id.get(int(link[3]))
        if src and dst and dst["type"] == "PreviewAny" and link[5] == "STRING":
            count += 1
    return count


# ---------------------------------------------------------------------------
# P1 / P2 结构
# ---------------------------------------------------------------------------


def test_p1_local_master_flow_is_simple_and_complete():
    from build_master_workflow import validate

    info = _object_info()
    wf = _load(LOCAL_NAME)
    assert (UI_DIR / LOCAL_NAME).is_file(), "本机版总流程没放进界面目录（用户看不到）"
    assert len(wf["nodes"]) <= 12, f"本机版太复杂：{len(wf['nodes'])} 个节点，普通用户看不懂"
    assert "BetterComfyUIEverything" in _types(wf)
    assert _has_note(wf), "缺少中文说明区（用户要求普通用户能看懂）"
    assert len(wf.get("groups") or []) >= 4, "缺少分组标题"
    assert _preview_links(wf) >= 4, "结果预览不足（用户要求看得到全部内容）"
    problems = validate(info, wf)
    assert not problems, "本机版 schema 校验失败：\n  " + "\n  ".join(problems[:10])


def test_p2_gpu_master_flow_has_all_stages():
    from build_master_workflow import validate

    info = _object_info()
    wf = _load(GPU_NAME)
    assert (UI_DIR / GPU_NAME).is_file(), "GPU 版总流程没放进界面目录（用户看不到）"
    types = _types(wf)
    for required in ("BetterComfyUIEverything", "BetterComfyUIAssetGen",
                     "BetterComfyUIAssetIngest", "BetterComfyUIAssetReview",
                     "MiniMaxH3Director", "CreateVideo", "SaveVideo",
                     "BetterComfyUIFinalize", "BetterComfyUIVideoReview"):
        assert required in types, f"GPU 总流程缺少阶段节点：{required}"
    assert _has_note(wf), "缺少中文说明区"
    assert len(wf.get("groups") or []) >= 5, "阶段分组不足"
    problems = validate(info, wf)
    assert not problems, "GPU 版 schema 校验失败：\n  " + "\n  ".join(problems[:10])


# ---------------------------------------------------------------------------
# P3 本机版真实执行：一段剧本 → 全部内容
# ---------------------------------------------------------------------------


def test_p3_local_master_flow_really_outputs_everything():
    info = _object_info()
    wf = _load(LOCAL_NAME)
    links = {int(raw[0]): raw for raw in wf.get("links") or []}

    def widget_names(node_type: str) -> list[str]:
        spec = info[node_type]["input"]
        names = []
        for section in (spec.get("required") or {}, spec.get("optional") or {}):
            for name, decl in section.items():
                dtype = decl[0]
                if isinstance(dtype, list) or dtype in ("STRING", "INT", "FLOAT", "BOOLEAN"):
                    names.append(name)
        return names

    api = {}
    for node in wf["nodes"]:
        node_type = node["type"]
        inputs = {}
        widgets = list(node.get("widgets_values") or [])
        for i, name in enumerate(widget_names(node_type)):
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

    result = _post(f"{BASE}/prompt", {"prompt": api, "client_id": "acceptance-33"})
    prompt_id = result.get("prompt_id")
    assert prompt_id, f"/prompt 未返回 prompt_id：{result}"
    deadline = time.time() + 180
    entry = None
    while time.time() < deadline:
        data = _get(f"{BASE}/history/{prompt_id}")
        if prompt_id in data:
            entry = data[prompt_id]
            break
        time.sleep(1.0)
    assert entry is not None, "等待执行超时"
    status = entry.get("status") or {}
    assert status.get("status_str") == "success", f"执行失败：{status}"
    texts = []
    for _nid, out in (entry.get("outputs") or {}).items():
        texts.extend(str(x) for x in out.get("text") or [])
    joined = "\n".join(texts)
    assert "bettercomfyui.one-node/v1" in joined, "没有输出标准提示词JSON"
    for type_name in ("CHARACTER", "SCENE", "PROP", "SHOT"):
        assert type_name in joined, f"缺少素材类提示词：{type_name}"
    assert "director_groups" in joined, "没有输出导演组结构"
    assert "---" in joined, "没有输出分镜脚本（--- 分隔）"
    assert "API 调用次数：0 次" in joined or "占位" in joined, "审计没有如实标注占位/调用次数"


# ---------------------------------------------------------------------------
# P4 视频审核可见性（用户：现在看不到）
# ---------------------------------------------------------------------------


def test_p4_video_review_is_visible_in_ui():
    info = _object_info()
    assert "BetterComfyUIVideoReview" in info, "界面节点列表里没有视频审核节点"
    assert (UI_DIR / "S4_视频审核（本机可跑）.json").is_file(), "S4 视频审核工作流不在界面目录"
    gpu = _load(GPU_NAME)
    assert "BetterComfyUIVideoReview" in _types(gpu), "总流程里没有视频审核"
    spec = info["BetterComfyUIVideoReview"]["input"]["required"]
    assert "视频文件路径" in spec and "审核范围" in spec, "视频审核节点参数缺失"
