"""真实浏览器用户行为模拟：打开工作流 → 检查错误 → 点队列 → 看结果。

## 为什么单独写成文件

PowerShell heredoc 会把 JS 里的 `\\n`、`\\` 吃掉，反复出错。
写成独立 Python 文件可以稳定复用，也方便在 GPU 机器上直接跑。

## 模拟的用户动作（全部真实 DOM/JS）

1. 打开 `http://127.0.0.1:8188/`（真浏览器：系统 Edge）
2. 打开指定工作流（等同点击左侧工作流列表）
3. 检查每个节点是否有错误标记、哪些输入没连线
4. 点击「队列」（等同点播放按钮）
5. 读取后端执行结果
6. 汇总 pageerror / console.error

## 用法

    python scripts/browser_sim.py
    python scripts/browser_sim.py --workflow W0_完整逻辑图全流程.json --queue
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8188"
WF_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"


def _get(path: str, timeout: float = 30.0):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def _post_json(path: str, body: dict, timeout: float = 30.0) -> tuple[int, str]:
    """POST 并返回 (状态码, 响应体文本)。"""
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _post_raw(path: str, body: dict, timeout: float = 30.0) -> int:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def open_workflow(page, wf: dict, required_map: dict) -> dict:
    """加载工作流并检查错误（必填清单来自 /object_info，版本无关）。"""
    return page.evaluate("""async ([wf, requiredMap]) => {
        try {
            window.app.graph.clear();
            await window.app.loadGraphData(wf);
            await new Promise(r => setTimeout(r, 600));
            const nodes = window.app.graph._nodes || [];
            const problems = [];
            // 只把**必填**且未连线的输入算问题；可选输入（如 image_prompts、
            // 导演台的 i2v_groups 等）留空是合法的，不该报错。
            const required = (type) => requiredMap[type] || null;
            for (const n of nodes) {
                const req = required(n.type);
                const missing = (n.inputs || [])
                    .filter(i => i.link === null || i.link === undefined)
                    .filter(i => i.type !== '*' && !i.widget)
                    .filter(i => !req || req.includes(i.name))
                    .map(i => i.name);
                if (n.has_errors || n.hasErrors) {
                    problems.push({node: n.type, id: n.id, kind: 'has_errors'});
                }
                if (missing.length) {
                    problems.push({node: n.type, id: n.id, kind: 'unconnected_inputs',
                                   inputs: missing});
                }
                if (n.type === undefined || n.type === null) {
                    problems.push({node: String(n.type), id: n.id, kind: 'unknown_type'});
                }
            }
            return {ok: true, count: nodes.length, problems: problems,
                    types: nodes.map(n => n.type)};
        } catch (e) {
            return {ok: false, error: String(e)};
        }
    }""", [wf, required_map])


def queue_prompt(page) -> dict:
    """点击「队列」（前端按钮内部调用的就是 app.queuePrompt）。"""
    return page.evaluate("""async () => {
        try {
            const r = window.app.queuePrompt(0, 1);
            await Promise.resolve(r);
            return {ok: true};
        } catch (e) {
            return {ok: false, error: String(e)};
        }
    }""")


def graph_api_prompt(page) -> dict:
    """用前端序列化后直接 POST /prompt（更接近真实提交）。"""
    # 注意：新版 ComfyUI 的 graphToPrompt() 是 **async**，必须 await。
    # 不 await 会拿到 Promise，output 为空 -> 提交空 prompt -> 服务端 500。
    prompt = page.evaluate("""async () => {
        try {
            const p = await window.app.graphToPrompt();
            return {ok: true, output: p.output, workflow: p.workflow};
        } catch (e) {
            return {ok: false, error: String(e)};
        }
    }""")
    if not prompt.get("ok"):
        return prompt
    code, body = _post_json("/prompt", {"prompt": prompt["output"], "client_id": "bcui-sim"})
    return {"ok": 200 <= code < 300, "status": code, "body": body[:800],
            "node_count": len(prompt["output"] or {})}


def wait_history(before: set[str], timeout_s: float = 120.0) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        history = _get("/history")
        for prompt_id, entry in history.items():
            if prompt_id in before:
                continue
            prompt = entry.get("prompt") or [None, {}, {}]
            nodes = prompt[2] if len(prompt) > 2 and isinstance(prompt[2], dict) else {}
            if any("BetterComfyUI" in str(n.get("class_type", "")) for n in nodes.values()):
                return {"prompt_id": prompt_id, "entry": entry}
        time.sleep(0.5)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="W0_完整逻辑图全流程.json")
    parser.add_argument("--queue", action="store_true", help="真的点队列提交")
    parser.add_argument("--history-wait", type=float, default=120.0)
    args = parser.parse_args(argv)

    path = WF_DIR / args.workflow
    if not path.is_file():
        print(f"[FAIL] 工作流不存在：{path}")
        return 1
    wf = json.loads(path.read_text(encoding="utf-8"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[FAIL] 未安装 playwright（用系统 python 跑）")
        return 1

    with sync_playwright() as pw:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = (pw.chromium.launch(headless=True, channel=channel)
                           if channel else pw.chromium.launch(headless=True))
                break
            except Exception:  # noqa: BLE001
                browser = None
        if browser is None:
            print("[FAIL] 没有可用浏览器")
            return 1

        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        page_errors: list[str] = []
        console_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None)

        page.goto(BASE, wait_until="load", timeout=60000)
        page.wait_for_function(
            "() => window.app && window.app.graph && window.LiteGraph", timeout=60000)
        page.wait_for_timeout(1800)

        print(f"=== 打开工作流：{args.workflow} ===")
        info = _get("/object_info")
        required_map = {
            name: list((spec.get("input", {}).get("required") or {}).keys())
            for name, spec in info.items()
        }
        result = open_workflow(page, wf, required_map)
        if not result.get("ok"):
            print(f"[FAIL] 加载失败：{result.get('error')}")
            browser.close()
            return 1
        print(f"  画布节点：{result['count']} / 文件 {len(wf['nodes'])}")
        if result["problems"]:
            print("  发现的问题：")
            for item in result["problems"]:
                print(f"    - {item}")
        else:
            print("  节点错误：无；未连线输入：无")

        queue_result = {"skipped": not args.queue}
        if args.queue:
            before = set(_get("/history"))
            print("=== 点「队列」 ===")
            submitted = graph_api_prompt(page)
            print(f"  提交：{submitted}")
            if submitted.get("ok"):
                found = wait_history(before, args.history_wait)
                if found is None:
                    queue_result = {"ok": False, "reason": "等待执行结果超时"}
                    print(f"  {queue_result}")
                else:
                    status = found["entry"].get("status", {})
                    errs = [
                        m[1].get("exception_message", "")[:300]
                        for m in status.get("messages", [])
                        if m[0] == "execution_error"
                    ]
                    queue_result = {
                        "ok": status.get("status_str") == "success",
                        "status": status.get("status_str"),
                        "errors": errs,
                    }
                    print(f"  执行状态：{status.get('status_str')} "
                          f"completed={status.get('completed')}")
                    for err in errs:
                        print(f"    错误：{err}")
            else:
                queue_result = submitted

        print("=== 前端错误 ===")
        print(f"  pageerror：{page_errors[:3] if page_errors else '无'}")
        our_console = [e for e in console_errors if "bettercomfyui" in e.lower()]
        print(f"  我方 console.error：{our_console[:3] if our_console else '无'}")
        print(f"  全部 console.error 数：{len(console_errors)}")

        browser.close()

    ok = (not result["problems"]) and (not page_errors) and queue_result.get("ok", True)
    print(f"\n=== 结论：{'通过' if ok else '有问题需要修'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
