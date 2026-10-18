"""验收 27：SG1 界面接通 —— 剧本巨人 → 桥接 → 一体化导演台。

## 为什么要有这一条（这是图一最后一个功能缺口）

`SG1`「剧本 → 提示词」的逻辑**库里早就实现了**（`prompt/gateway.py` 三个 gateway
+ `validate_script`），但**界面上没有任何节点调用它** —— 用户在工作流里点不出
"给剧本出提示词"。按"站在巨人肩膀上"的路线，正确做法是接现成巨人，不是重写。

本轮接法（全部实测）：

    安装 ComfyUI-Storyboard-LLM        -> StoryboardNode（DeepSeek 拆剧本）
    安装 ComfyUI-DeepSeek-API-connector-> DeepSeekPromptConnector
    新增 BetterComfyUIPromptBridge     -> 把巨人输出转成导演台要的 `剧本分镜`

## 巨人输出格式（实测其源码第 339-353 行）

    [镜头1] 主角推开木门走进房间，镜头缓慢推进
    <空行>
    [镜头2] 主角在桌前坐下，抬头看向窗外

## 判定通过

    P1 桥接能正确解析巨人的 `[镜头N]` + 空行格式；
    P2 无标签时按空行切分（降级但不丢内容）；
    P3 单段/空输入不崩（返回 0 或 1 段，绝不抛异常）；
    P4 输出的 `剧本分镜` 是 `---` 独占一行分隔（导演台要的格式）；
    P5 输出 JSON 结构正确（schema/segment_count/segments）；
    P6 **不把风格拼进每段文本**（风格是导演台职责，避免重复注入）；
    P7 在**真实 ComfyUI** 里跑通 `Bridge -> OneDirector -> PreviewAny` 整条链；
    P8 真实链路输出里**风格只出现一次**（修前会重复注入两次）；
    P9 两个巨人节点在真实 ComfyUI 里注册成功（`/object_info` 可见）。

## 未验证（必须如实标注）

    巨人自己调用 DeepSeek API 那一步**未验证** —— 需要真实 API 密钥，
    而密钥不得写进代码仓库。本条只验证"接线与格式转换"，
    不把"API 调通了"写成已验证。
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

BASE = "http://127.0.0.1:8188"

#: 巨人 `StoryboardNode` 的**真实输出格式**（照其源码 339-353 行构造）。
GIANT_OUTPUT = """[镜头1] 林秀推门走进客栈大堂，镜头缓推，冷蓝夜色

[镜头2] 老陈抬头看向门口，特写，油灯昏黄

[镜头3] 林秀在桌边坐下，中景，镜头轻微横移"""


def _get(path: str, timeout: float = 20.0):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def _post(path: str, body: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _alive() -> bool:
    try:
        _get("/system_stats", timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def comfy():
    if not _alive():
        pytest.skip("未验证：本机 ComfyUI 不可达（http://127.0.0.1:8188）")
    return True


# ---------------------------------------------------------------------------
# P1 – P6 桥接行为（纯函数，不依赖 ComfyUI）
# ---------------------------------------------------------------------------


def test_p1_parses_giant_label_format():
    from bettercomfyui_comfy.prompt_bridge import parse_shot_prompts

    shots = parse_shot_prompts(GIANT_OUTPUT)
    assert len(shots) == 3, f"应解析出 3 个镜头，实际 {len(shots)}：{shots}"
    assert "林秀推门走进客栈大堂" in shots[0]
    assert "[镜头" not in shots[0], f"标签没被去掉：{shots[0]!r}"


def test_p2_falls_back_to_blank_lines():
    from bettercomfyui_comfy.prompt_bridge import parse_shot_prompts

    shots = parse_shot_prompts("第一段\n\n第二段\n\n第三段")
    assert len(shots) == 3, f"空行降级失败：{shots}"


def test_p3_graceful_on_degenerate_input():
    """单段/空输入不能崩，也不能丢内容。"""
    from bettercomfyui_comfy.prompt_bridge import parse_shot_prompts

    assert parse_shot_prompts("只有一段没有任何分隔") == ["只有一段没有任何分隔"]
    assert parse_shot_prompts("") == []
    assert parse_shot_prompts(None) == []
    assert parse_shot_prompts("   \n  \n ") == []


def test_p4_script_uses_triple_dash_separator():
    from bettercomfyui_comfy.prompt_bridge import BetterComfyUIPromptBridge

    script, _payload, _summary = BetterComfyUIPromptBridge().bridge(GIANT_OUTPUT)
    # 导演台要求 `---` **独占一行**
    assert "\n---\n" in script, f"没有 --- 分隔：{script[:200]}"
    assert script.count("\n---\n") == 2, f"3 段应有 2 个分隔符：{script!r}"


def test_p5_json_payload_is_wellformed():
    from bettercomfyui_comfy.prompt_bridge import BetterComfyUIPromptBridge

    _script, payload_json, summary = BetterComfyUIPromptBridge().bridge(
        GIANT_OUTPUT, 统一风格="@冷蓝电影光", 段时长_秒=7.5)
    payload = json.loads(payload_json)
    assert payload["segment_count"] == 3
    assert payload["segment_seconds"] == 7.5
    assert len(payload["segments"]) == 3
    assert all(seg["prompt"] for seg in payload["segments"])
    # 风格写进 JSON（供下游用），并出现在摘要里
    assert payload["global_style"], "风格没写进 JSON"
    assert "镜头数" in summary


def test_p6_style_is_not_injected_into_segment_text():
    """**关键回归**：风格不能拼进每段文本（否则下游再拼一次 = 重复注入）。"""
    from bettercomfyui_comfy.prompt_bridge import BetterComfyUIPromptBridge

    script, payload_json, _summary = BetterComfyUIPromptBridge().bridge(
        GIANT_OUTPUT, 统一风格="@冷蓝电影光")
    assert "全片视觉风格" not in script, f"桥接擅自注入了风格：{script[:200]}"
    payload = json.loads(payload_json)
    for seg in payload["segments"]:
        assert "全片视觉风格" not in seg["prompt"], f"段文本里被注入了风格：{seg}"


def test_p6b_empty_video_falls_back_to_image_prompts():
    from bettercomfyui_comfy.prompt_bridge import BetterComfyUIPromptBridge

    _script, payload_json, summary = BetterComfyUIPromptBridge().bridge(
        "", image_prompts=GIANT_OUTPUT)
    payload = json.loads(payload_json)
    assert payload["segment_count"] == 3, "空 video_prompts 没有兜底"
    assert "兜底" in payload["source"] or "兜底" in summary


# ---------------------------------------------------------------------------
# P7 – P9 真实 ComfyUI 链路
# ---------------------------------------------------------------------------


def test_p9_giants_are_registered_in_real_comfyui(comfy):
    """两个巨人与桥接节点必须都在真实 ComfyUI 里注册。"""
    info = _get("/object_info", timeout=40)
    for name in ("StoryboardNode", "DeepSeekPromptConnector",
                 "BetterComfyUIPromptBridge", "BetterComfyUIOneDirector"):
        assert name in info, f"{name} 没在 ComfyUI 里注册"
    # 桥接节点的输入必须能接巨人的输出
    bridge = info["BetterComfyUIPromptBridge"]["input"]
    assert "video_prompts" in bridge["required"]
    assert bridge["required"]["video_prompts"][1].get("forceInput") is True


def _project_id() -> str:
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    store = ProjectStore(Path(REPO_ROOT) / cfg.global_.project_root, cfg)
    existing = [p for p in store.list() if p.get("display_name") == "E2E端到端测试"]
    if existing:
        return existing[0]["project_id"]
    return store.create("E2E端到端测试")["project_id"]


def test_p7_bridge_to_director_chain_runs_in_real_comfyui(comfy):
    """真实链路：Bridge -> OneDirector -> PreviewAny，必须执行成功。"""
    project_id = _project_id()
    wf = {
        "1": {"class_type": "BetterComfyUIPromptBridge",
              "inputs": {"video_prompts": GIANT_OUTPUT,
                         "统一风格": "@冷蓝电影光", "段时长_秒": 5.0}},
        "2": {"class_type": "BetterComfyUIOneDirector",
              "inputs": {
                  "剧本分镜": ["1", 0], "项目": project_id, "段间素材": "",
                  "段时长_秒": 5.0, "统一风格": "写实电影感",
                  "段间接续": "自动（导演台尾部帧辅助，推荐）",
                  "素材优先级": "角色>场景>物品>分镜（推荐）",
                  "公共素材模式": "整个素材库（推荐）"}},
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 3]}},
    }
    prompt_id = _post("/prompt", {"prompt": wf, "client_id": "bcui-a3-p7"})["prompt_id"]
    deadline = time.time() + 90
    entry = None
    while time.time() < deadline:
        history = _get(f"/history/{prompt_id}")
        if prompt_id in history:
            entry = history[prompt_id]
            break
        time.sleep(0.25)
    assert entry is not None, "等待执行结果超时"

    status = entry.get("status", {})
    if status.get("status_str") == "error":
        detail = [m[1].get("exception_message", "")[:400]
                  for m in status.get("messages", []) if m[0] == "execution_error"]
        pytest.fail(f"链路执行失败：{detail}")
    assert status.get("status_str") == "success"
    assert entry.get("outputs"), "没有任何输出"


def test_p8_style_appears_only_once_in_real_chain(comfy):
    """**真实链路回归**：风格在最终提示词里只能出现一次。

    修前桥接和导演台各注入一次，提示词里出现
    「全片视觉风格：冷蓝…。全片视觉风格：写实电影感」。
    """
    project_id = _project_id()
    wf = {
        "1": {"class_type": "BetterComfyUIPromptBridge",
              "inputs": {"video_prompts": GIANT_OUTPUT,
                         "统一风格": "@冷蓝电影光", "段时长_秒": 5.0}},
        "2": {"class_type": "BetterComfyUIOneDirector",
              "inputs": {
                  "剧本分镜": ["1", 0], "项目": project_id, "段间素材": "",
                  "段时长_秒": 5.0, "统一风格": "写实电影感",
                  "段间接续": "自动（导演台尾部帧辅助，推荐）",
                  "素材优先级": "角色>场景>物品>分镜（推荐）",
                  "公共素材模式": "整个素材库（推荐）"}},
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 3]}},
    }
    prompt_id = _post("/prompt", {"prompt": wf, "client_id": "bcui-a3-p8"})["prompt_id"]
    deadline = time.time() + 90
    entry = None
    while time.time() < deadline:
        history = _get(f"/history/{prompt_id}")
        if prompt_id in history:
            entry = history[prompt_id]
            break
        time.sleep(0.25)
    assert entry is not None

    blob = ""
    for node_out in entry.get("outputs", {}).values():
        for value in node_out.values():
            blob += str(value[0] if isinstance(value, list) and value else value)
    assert "segment_count" in blob, f"没拿到导演台审计 JSON：{blob[:300]}"

    payload = None
    for start in [i for i, ch in enumerate(blob) if ch == "{"]:
        for end in range(len(blob), start, -1):
            if blob[end - 1] == "}":
                try:
                    candidate = json.loads(blob[start:end])
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(candidate, dict) and "segment_count" in candidate:
                    payload = candidate
                    break
        if payload:
            break
    assert payload is not None

    for seg in payload["segments"]:
        text = seg["prompt"]
        assert text.count("全片视觉风格") <= 1, (
            f"风格被重复注入：{text!r}"
        )
        assert "写实电影感" in text, "导演台的统一风格没注入"
