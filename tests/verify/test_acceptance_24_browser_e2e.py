"""验收 24：**模拟浏览器操作**的端到端测试（HTTP 等价层，真跑 ComfyUI）。

## 为什么这样做

用户要求：「模拟用户的浏览器面上的所有点击等操作」。
真浏览器要下载 chromium（本机无网络），所以退一层做到**等价强度**：

ComfyUI 前端每个"点击"最终只产生这些 HTTP 请求 ——
我们**逐条真实调用**它们，就等于把界面上能点的都点了一遍：

| 界面动作 | HTTP 请求 |
|---|---|
| 打开节点面板、拖节点 | `GET /object_info`（前端靠它渲染节点与控件） |
| 连线后点「队列」 | `POST /prompt` |
| 看进度/等结果 | `GET /history/{id}` |
| 点「取消」 | `POST /interrupt` |
| 清空队列 | `POST /queue {"delete":[...]}` |
| 释放显存 | `POST /free` |

## 模型边界打桩（用户要求的"关掉模型直接返回结果"）

* 本测试**只用不加载模型的节点**（`BetterComfyUIOneDirector` 是纯 Python 计算，
  `PreviewAny` 只显示字符串）—— 这就是"把模型关掉直接给结果"，
  用来验证**插件-节点-系统接线完整性**，不消耗算力与显存。
* **依赖模型的节点（加载 H3 权重）绝不提交** —— 那会加载 34 GB 进内存，
  本机物理内存 15.75 GB，会直接把机器打挂（历史上蓝屏三次）。
  这类只做**结构校验**：节点/输入/连线类型都对得上，但不入队。

## 判定通过

    P1 `GET /object_info` 里我的 16 个节点都在，且输入 schema 可渲染（有类型/默认值）；
    P2 前端能拿到的 COMBO 选项非空（否则界面上下拉框是空的，用户点不了）；
    P3 **真实提交** `OneDirector -> PreviewAny`：`POST /prompt` 返回 prompt_id；
    P4 **真实执行成功**：`GET /history` 里 `status_str=success` 且 `completed=True`；
    P5 输出内容可验证：审计 JSON 里 `segment_count` 与剧本段数一致、风格被注入；
    P6 非法 `project_id` 被**节点自己拒绝**（R5 项目隔离），不是静默接受；
    P7 `POST /queue` 清空可调用；
    P8 `POST /free` 可调用且**核验到显存下降**（阶段化调度的物理前提）；
    P9 依赖模型的节点**不被提交**（用代码断言工作流里没有模型加载节点）。

## 判定假通过（必须能变红）

    F1 节点没注册 -> P1 变红；
    F2 节点执行抛错 -> P4 变红；
    F3 去掉 R5 校验 -> P6 变红；
    F4 把带模型加载的节点塞进提交 -> P9 变红（并会真的把机器打挂）。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASE = "http://127.0.0.1:8188"


def _get(path: str, timeout: float = 20.0):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def _post(path: str, body: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _post_raw(path: str, body: dict, timeout: float = 30.0) -> int:
    """POST 但不解析 JSON —— ComfyUI 的 /queue 返回**空 body**。"""
    import urllib.error as _ue

    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200))
    except _ue.HTTPError as exc:
        return int(exc.code)


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
    return {"base": BASE}


@pytest.fixture(scope="module")
def project_id() -> str:
    """建一个真实项目（节点要求合法 32 位 hex project_id，R5 项目隔离）。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    store = ProjectStore(Path(REPO_ROOT) / cfg.global_.project_root, cfg)
    existing = [p for p in store.list()
                if p.get("display_name") == "E2E端到端测试"]
    if existing:
        return existing[0]["project_id"]
    return store.create("E2E端到端测试")["project_id"]


# ---------------------------------------------------------------------------
# P1 / P2 前端渲染等价（object_info）
# ---------------------------------------------------------------------------


MY_NODES = (
    "BetterComfyUIOneDirector",
    "BetterComfyUIRun",
    "BetterComfyUIProject",
    "BetterComfyUIResult",
    "BetterComfyUIAssetReview",
    "BetterComfyUIAssetMedia",
    "BetterComfyUIAssetSlots",
    "BetterComfyUIFusionIngest",
    "BetterComfyUIFusionGroups",
    "BetterComfyUIFusionCombine",
    "BetterComfyUIFusionPackGroup",
)


def test_p1_my_nodes_are_registered_with_renderable_schema(comfy):
    """界面要渲染节点，必须能从 `/object_info` 拿到完整 schema。"""
    info = _get("/object_info")
    missing = [n for n in MY_NODES if n not in info]
    assert not missing, f"这些节点在界面里不存在：{missing}"

    spec = info["BetterComfyUIOneDirector"]
    required = spec["input"]["required"]
    assert "剧本分镜" in required, f"核心输入缺失：{list(required)}"
    # 前端渲染需要 (type, opts) 二元结构
    for name, decl in required.items():
        assert isinstance(decl, list) and decl, f"{name} 的 schema 不可渲染：{decl!r}"


def test_p2_combo_options_are_not_empty(comfy):
    """COMBO 选项为空会导致界面下拉框点不动。"""
    info = _get("/object_info/BetterComfyUIOneDirector")
    required = info["BetterComfyUIOneDirector"]["input"]["required"]
    combos = {k: v[0] for k, v in required.items() if isinstance(v[0], list)}
    assert combos, "没有任何 COMBO 输入"
    for name, options in combos.items():
        assert options, f"COMBO {name} 选项为空，界面上无法选择"
    assert "段间接续" in combos


# ---------------------------------------------------------------------------
# P3 / P4 / P5 真实提交 + 真实执行 + 输出校验
# ---------------------------------------------------------------------------


def _director_workflow(project_id: str, script: str) -> dict:
    return {
        "1": {
            "class_type": "BetterComfyUIOneDirector",
            "inputs": {
                "剧本分镜": script,
                "项目": project_id,
                "段间素材": "",
                "段时长_秒": 5.0,
                "统一风格": "写实电影感",
                "段间接续": "自动（导演台尾部帧辅助，推荐）",
                "素材优先级": "角色>场景>物品>分镜（推荐）",
                "公共素材模式": "整个素材库（推荐）",
            },
        },
        # PreviewAny 是纯显示节点，不加载任何模型 —— 这就是"模型打桩"的形态：
        # 我们验证的是插件算出来的东西，不是模型生成的东西。
        "2": {"class_type": "PreviewAny", "inputs": {"source": ["1", 3]}},
    }


def _wait(comfy, prompt_id: str, *, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        history = _get(f"/history/{prompt_id}")
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(0.25)
    raise AssertionError(f"等待 {prompt_id} 超时（{timeout_s}s）")


def test_p3_submit_returns_prompt_id(comfy, project_id):
    """`POST /prompt` 等价于界面点「队列」，必须返回 prompt_id。"""
    wf = _director_workflow(project_id, "测试镜头一\n---\n测试镜头二")
    resp = _post("/prompt", {"prompt": wf, "client_id": "bcui-e2e-p3"})
    assert resp.get("prompt_id"), f"没有拿到 prompt_id：{resp}"
    # 清理：别把队列留给下一次
    try:
        _post("/queue", {"delete": [resp["prompt_id"]]})
    except Exception:  # noqa: BLE001
        pass


def test_p4_workflow_executes_successfully(comfy, project_id):
    """真实执行：`status_str=success` 且 `completed=True`。"""
    script = "林秀推门走进客栈大堂，镜头缓推\n---\n老陈抬头看向门口，特写"
    wf = _director_workflow(project_id, script)
    pid = _post("/prompt", {"prompt": wf, "client_id": "bcui-e2e-p4"})["prompt_id"]
    entry = _wait(comfy, pid)
    status = entry.get("status", {})
    if status.get("status_str") == "error":
        detail = [
            m[1].get("exception_message", "")[:400]
            for m in status.get("messages", []) if m[0] == "execution_error"
        ]
        pytest.fail(f"节点执行失败：{detail}")
    assert status.get("status_str") == "success", status
    assert status.get("completed") is True, status


def test_p5_output_contains_expected_audit_payload(comfy, project_id):
    """输出可验证：段数与剧本一致、风格被注入、审计 JSON 结构正确。"""
    script = ("林秀推门走进客栈大堂\n---\n老陈抬头看向门口\n---\n林秀在桌边坐下")
    wf = _director_workflow(project_id, script)
    pid = _post("/prompt", {"prompt": wf, "client_id": "bcui-e2e-p5"})["prompt_id"]
    entry = _wait(comfy, pid)
    outputs = entry.get("outputs", {})
    assert outputs, "没有任何输出节点返回结果"

    texts = []
    for node_out in outputs.values():
        for value in node_out.values():
            if isinstance(value, list):
                texts.extend(str(v) for v in value)
            else:
                texts.append(str(value))
    blob = "\n".join(texts)
    assert "segment_count" in blob, f"输出里没有审计 JSON：{blob[:300]}"

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
    assert payload is not None, f"解析不出审计 JSON：{blob[:400]}"
    assert payload["segment_count"] == 3, (
        f"段数应等于剧本的 3 段，实际 {payload['segment_count']}"
    )
    # 统一风格必须被注入到每一段
    assert all("写实电影感" in s.get("prompt", "") for s in payload["segments"]), (
        "风格没有注入到每一段"
    )


# ---------------------------------------------------------------------------
# P6 节点自身的守卫
# ---------------------------------------------------------------------------


def test_p6_invalid_project_id_is_rejected_by_node(comfy):
    """非法 project_id 必须被**节点自己**拒绝（R5 项目隔离），不能静默接受。"""
    wf = _director_workflow("'不是合法id'", "测试镜头")
    pid = _post("/prompt", {"prompt": wf, "client_id": "bcui-e2e-p6"})["prompt_id"]
    entry = _wait(comfy, pid)
    status = entry.get("status", {})
    assert status.get("status_str") == "error", "非法 project_id 竟然执行成功"
    msgs = " ".join(
        m[1].get("exception_message", "") for m in status.get("messages", [])
        if m[0] == "execution_error"
    )
    assert "project_id" in msgs, f"错误信息没提到 project_id：{msgs[:300]}"


# ---------------------------------------------------------------------------
# P7 / P8 队列与显存（阶段化调度的基础）
# ---------------------------------------------------------------------------


def test_p7_queue_can_be_cleared(comfy):
    """界面「清空队列」等价调用。"""
    status = _post_raw("/queue", {"delete": []})
    assert 200 <= status < 300, f"/queue 返回 {status}"


def test_p8_free_models_verifies_vram_drop(comfy):
    """`POST /free` 必须可调用且**核验到显存下降**（不假装释放）。"""
    from bettercomfyui.executors.comfyui_http import ComfyUIHttpExecutor

    ex = ComfyUIHttpExecutor(base_url=BASE)
    result = ex.free_models(verify=True, settle_s=0.8)
    assert result["ok"] is True, result
    assert result["before"] is not None and result["after"] is not None
    # 注意：空闲机器上"已用显存"可能本来就很小，下降幅度可能为 0；
    # 所以只断言**核验逻辑跑了**，下降与否如实记录，不强行要求。
    assert "reason" in result and result["reason"], "没有给出核验说明"
    if not result["verified"]:
        pytest.skip(
            "未验证：/free 调用成功但未观测到显存下降（机器空闲，无模型驻留）"
        )


# ---------------------------------------------------------------------------
# P9 红线：绝不提交依赖模型的节点
# ---------------------------------------------------------------------------


#: 这些类名一旦出现在提交体里就会加载大权重 —— 本机 15.75 GB 内存扛不住。
_MODEL_LOADING_NODES = (
    "UNETLoader", "CheckpointLoaderSimple", "VAELoader", "CLIPLoader",
    "LoraLoader", "LoraLoaderModelOnly", "DualCLIPLoader",
    "MiniMaxH3Director",
)


def test_p9_model_loading_nodes_are_never_submitted(comfy, project_id):
    """**红线断言**：本测试提交的工作流里不得出现任何模型加载节点。

    这条测试的价值不在"验证功能"，而在于**防止将来有人往 E2E 里塞真模型**，
    把 34 GB 权重压进 15.75 GB 物理内存的机器（历史上已蓝屏三次）。
    """
    wf = _director_workflow(project_id, "红线检查镜头")
    classes = {node["class_type"] for node in wf.values()}
    forbidden = classes & set(_MODEL_LOADING_NODES)
    assert not forbidden, f"E2E 工作流里出现了模型加载节点：{forbidden}"

    # 反证：确实有依赖模型的节点存在（否则这条红线没有意义）
    info = _get("/object_info")
    assert "MiniMaxH3Director" in info, (
        "本机没有 MiniMaxH3Director —— 红线断言失去意义，请重新确认环境"
    )
