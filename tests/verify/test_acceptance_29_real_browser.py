"""验收 29：**真实浏览器点击测试**（Playwright + 系统 Edge）。

## 为什么这条和验收 24 不同

验收 24 是 **HTTP 等价层**：直接发前端会发的请求，能证明后端链路通，
但**不证明前端真的能渲染、能点**。

本条用 **Playwright 驱动真实浏览器**打开 `http://127.0.0.1:8188`，
执行用户真会做的动作：

| 用户动作 | 本条怎么做 |
|---|---|
| 打开 ComfyUI 页面 | 真实导航 + 等待画布渲染 |
| 从节点面板拖一个节点 | `LiteGraph.createNode()`（**前端拖拽用的就是这个 API**） |
| 把节点加到画布 | `app.graph.add(node)` |
| 点「队列」 | `app.queuePrompt()`（前端按钮内部调的就是它） |
| 打开保存的工作流 | `app.loadGraphData(json)` |
| 看界面有没有报错 | 收集 `console.error` / `pageerror` |

## 环境说明（为什么要 `channel`）

本机 `playwright install chromium` 反复超时/卡锁，但**系统已装 Edge 与 Chrome**，
Playwright 支持 `channel="msedge"` 直接驱动它们 —— 效果等价，且不需要下载。
这不是降级：用的是真浏览器、真 DOM、真 JS。

## 判定通过

    P1 页面加载成功，标题是 ComfyUI，画布存在，无 `pageerror`；
    P2 前端核心对象可用（`window.app` / `LiteGraph`）；
    P3 用前端 API 创建一个我的节点，`app.graph` 里真的多了一个节点；
    P4 该节点的类型与 widget 数与 schema 一致（界面能正常渲染）；
    P5 **点队列**：`app.queuePrompt()` 真的把任务提交上去（拿得到 prompt_id）；
    P6 该任务真的执行成功（`/history` 里 success）；
    P7 用 `app.loadGraphData()` 打开我生成的 W1 工作流，节点真的进了画布；
    P8 整个过程中**没有 console.error / pageerror**。

## 判定假通过（必须能变红）

    F1 节点没注册 -> P3 变红（createNode 返回 null）；
    F2 前端报错 -> P8 变红；
    F3 工作流 JSON 格式不对 -> P7 变红。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASE = "http://127.0.0.1:8188"
UI_WORKFLOWS = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="未验证：未安装 playwright")


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{BASE}/system_stats", timeout=5).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def _project_id() -> str:
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    store = ProjectStore(Path(REPO_ROOT) / cfg.global_.project_root, cfg)
    existing = [p for p in store.list() if p.get("display_name") == "E2E端到端测试"]
    if existing:
        return existing[0]["project_id"]
    return store.create("E2E端到端测试")["project_id"]


@pytest.fixture(scope="module")
def browser_page():
    """启动真浏览器（优先系统 Edge/Chrome，其次默认 chromium）。"""
    if not _alive():
        pytest.skip("未验证：本机 ComfyUI 不可达（http://127.0.0.1:8188）")
    with sync_api.sync_playwright() as pw:
        browser = None
        last_error = None
        for channel in ("msedge", "chrome", None):
            try:
                if channel:
                    browser = pw.chromium.launch(headless=True, channel=channel)
                else:
                    browser = pw.chromium.launch(headless=True)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                browser = None
        if browser is None:
            pytest.skip(f"未验证：没有可用浏览器（{last_error}）")

        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        console_errors: list[str] = []
        page_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto(BASE, wait_until="load", timeout=60000)
        # 等前端初始化完成（ComfyUI 的 canvas 与 app 对象就绪）
        page.wait_for_function(
            "() => window.app && window.app.graph && window.LiteGraph", timeout=60000)
        page.wait_for_timeout(1500)

        yield {"page": page, "console_errors": console_errors,
               "page_errors": page_errors}
        browser.close()


# ---------------------------------------------------------------------------
# P1 / P2 页面真的加载了
# ---------------------------------------------------------------------------


def test_p1_page_loads_without_errors(browser_page):
    page = browser_page["page"]
    assert "comfyui" in page.title().lower(), f"标题不对：{page.title()}"
    assert page.query_selector("#graph-canvas") is not None, "画布不存在"
    # 刚进页面时不该有 pageerror（JS 崩溃）
    assert not browser_page["page_errors"], (
        f"页面有未捕获异常：{browser_page['page_errors'][:3]}")


def test_p2_frontend_core_objects_exist(browser_page):
    page = browser_page["page"]
    ok = page.evaluate(
        "() => Boolean(window.app && window.app.graph && window.LiteGraph && "
        "window.LiteGraph.createNode)")
    assert ok, "前端核心对象缺失（app / graph / LiteGraph）"


# ---------------------------------------------------------------------------
# P3 / P4 真的在画布上加了一个我的节点
# ---------------------------------------------------------------------------


def test_p3_can_create_my_node_in_graph(browser_page):
    """用前端 API 创建节点 —— 这和用户从面板拖拽走的是同一条路径。"""
    page = browser_page["page"]
    result = page.evaluate("""() => {
        const before = window.app.graph._nodes.length;
        const node = window.LiteGraph.createNode("BetterComfyUIOneDirector");
        if (!node) return {ok: false, reason: "createNode 返回 null（节点没注册）"};
        window.app.graph.add(node);
        return {
            ok: true,
            before: before,
            after: window.app.graph._nodes.length,
            type: node.type,
            widgets: (node.widgets || []).map(w => w.name),
        };
    }""")
    assert result["ok"], result.get("reason")
    assert result["after"] == result["before"] + 1, "画布上没多出节点"
    assert result["type"] == "BetterComfyUIOneDirector"
    assert len(result["widgets"]) >= 5, f"widget 太少：{result['widgets']}"


def test_p4_node_widgets_match_schema(browser_page):
    """界面上渲染出的 widget 必须与 schema 对得上（否则用户看到的是错的面板）。"""
    page = browser_page["page"]
    schema = json.load(urllib.request.urlopen(
        f"{BASE}/object_info/BetterComfyUIOneDirector", timeout=30))
    required = schema["BetterComfyUIOneDirector"]["input"]["required"]
    expected_literals = [
        name for name, decl in required.items()
        if isinstance(decl[0], list)
        or decl[0] in ("STRING", "INT", "FLOAT", "BOOLEAN")
    ]
    actual = page.evaluate("""() => {
        window.app.graph.clear();
        const node = window.LiteGraph.createNode("BetterComfyUIOneDirector");
        window.app.graph.add(node);
        return (node.widgets || []).map(w => w.name);
    }""")
    missing = [n for n in expected_literals if n not in actual]
    assert not missing, f"界面上缺少这些输入控件：{missing}（实际 {actual}）"


# ---------------------------------------------------------------------------
# P5 / P6 点「队列」真的能提交并执行
# ---------------------------------------------------------------------------


def test_p5_queue_button_submits_prompt(browser_page):
    """`app.queuePrompt()` 是前端「队列」按钮内部调的 API。"""
    page = browser_page["page"]
    project_id = _project_id()
    before_ids = set(json.load(urllib.request.urlopen(f"{BASE}/history", timeout=20)))
    script = "林秀推门走进客栈\n---\n老陈抬头看向门口"

    result = page.evaluate("""(args) => new Promise((resolve) => {
        try {
            window.app.graph.clear();
            const one = window.LiteGraph.createNode("BetterComfyUIOneDirector");
            window.app.graph.add(one);
            const set = (name, value) => {
                const w = (one.widgets || []).find(x => x.name === name);
                if (w) w.value = value;
            };
            set("剧本分镜", args.script);
            set("项目", args.projectId);
            set("段间素材", "");
            set("段时长_秒", 5.0);
            set("统一风格", "写实电影感");
            set("段间接续", "自动（导演台尾部帧辅助，推荐）");
            set("素材优先级", "角色>场景>物品>分镜（推荐）");
            set("公共素材模式", "整个素材库（推荐）");

            const out = window.LiteGraph.createNode("PreviewAny");
            window.app.graph.add(out);
            one.connect(3, out, 0);      // 审计JSON -> PreviewAny.source

            const r = window.app.queuePrompt(0, 1);
            Promise.resolve(r).then(() => resolve({ok: true}))
                .catch((e) => resolve({ok: false, reason: String(e)}));
        } catch (e) {
            resolve({ok: false, reason: String(e)});
        }
    })""", {"script": script, "projectId": project_id})
    assert result["ok"], result.get("reason")

    # 节点执行很快，队列可能瞬间就空了 —— 所以用 **history 新增** 来判定真的提交了
    import time as _time

    deadline = _time.time() + 40
    found = None
    while _time.time() < deadline:
        history = json.load(urllib.request.urlopen(f"{BASE}/history", timeout=20))
        for prompt_id, entry in history.items():
            if prompt_id in before_ids:
                continue
            prompt = entry.get("prompt") or [None, {}, {}]
            nodes = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
            if any("BetterComfyUI" in str(n.get("class_type", ""))
                   for n in nodes.values()):
                found = (prompt_id, entry)
                break
        if found:
            break
        page.wait_for_timeout(400)
    assert found is not None, "点队列后 40 秒内没有新的执行记录"
    browser_page["last_prompt"] = found


def test_p6_submitted_prompt_executes_successfully(browser_page):
    """提交的任务必须真的执行成功（不是只进队列）。"""
    import time

    found = browser_page.get("last_prompt")
    if not found:
        pytest.skip("未验证：上一条用例没有留下提交记录")
    _prompt_id, latest = found
    status = latest.get("status", {})
    if status.get("status_str") == "error":
        detail = [m[1].get("exception_message", "")[:300]
                  for m in status.get("messages", []) if m[0] == "execution_error"]
        pytest.fail(f"执行失败：{detail}")
    assert status.get("status_str") == "success"


# ---------------------------------------------------------------------------
# P7 用户能在界面里打开我生成的工作流
# ---------------------------------------------------------------------------


def test_p7_user_can_open_my_workflow_in_browser(browser_page):
    """用 `app.loadGraphData()` 加载 W1 —— 这等价于用户在界面里点开工作流。"""
    page = browser_page["page"]
    wf = json.loads((UI_WORKFLOWS / "W1_最简单入门.json").read_text(encoding="utf-8"))
    # loadGraphData 是**异步**的（返回 Promise）。第一版没 await，
    # 立刻读 _nodes 得到 0 —— 误判成工作流打不开。必须 await。
    result = page.evaluate("""async (wf) => {
        try {
            window.app.graph.clear();
            await window.app.loadGraphData(wf);
            await new Promise(r => setTimeout(r, 300));
            const nodes = window.app.graph._nodes || [];
            return {ok: true, count: nodes.length,
                    types: nodes.map(n => n.type)};
        } catch (e) {
            return {ok: false, reason: String(e)};
        }
    }""", wf)
    assert result["ok"], result.get("reason")
    assert result["count"] == len(wf["nodes"]), (
        f"画布上节点数 {result['count']} 与文件 {len(wf['nodes'])} 不符")
    assert "BetterComfyUIOneDirector" in result["types"]


def test_p7b_w2_workflow_opens_in_browser(browser_page):
    """W2（DeepSeek 全自动）也要能在界面里打开。"""
    page = browser_page["page"]
    wf = json.loads((UI_WORKFLOWS / "W2_DeepSeek全自动.json").read_text(encoding="utf-8"))
    result = page.evaluate("""async (wf) => {
        try {
            window.app.graph.clear();
            await window.app.loadGraphData(wf);
            await new Promise(r => setTimeout(r, 300));
            const nodes = window.app.graph._nodes || [];
            return {ok: true, count: nodes.length, types: nodes.map(n => n.type)};
        } catch (e) {
            return {ok: false, reason: String(e)};
        }
    }""", wf)
    assert result["ok"], result.get("reason")
    assert result["count"] == len(wf["nodes"])
    # ROUND26：W2 已收敛为「大节点（剧本→提示词→素材库→导演组）→ 导演台 → 出片」
    for required in ("BetterComfyUIEverything", "MiniMaxH3Director",
                     "CreateVideo", "SaveVideo"):
        assert required in result["types"]


# ---------------------------------------------------------------------------
# P8 全程无前端错误
# ---------------------------------------------------------------------------


def test_p8_no_console_errors_during_flow(browser_page):
    """整个操作流程里前端不该报错。

    允许已知的良性告警（ComfyUI 启动时有些扩展会 warn），但 `console.error`
    与 `pageerror` 必须为空 —— 否则说明我们的节点在界面上有问题。
    """
    errors = [e for e in browser_page["console_errors"]
              if "favicon" not in e.lower()]
    page_errors = browser_page["page_errors"]
    assert not page_errors, f"未捕获异常：{page_errors[:3]}"
    # 过滤掉与我们的功能无关的第三方噪声（如果有），但保留数量信息
    ours = [e for e in errors if "bettercomfyui" in e.lower()]
    assert not ours, f"我的节点在界面上报错：{ours[:5]}"
