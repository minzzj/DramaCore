"""探针：诊断 graphToPrompt 为什么返回空（用于修工作流提交 500）。

用法：python scripts/probe_graph_prompt.py [工作流文件名]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8188"
WF_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "W0L_本机可跑通体验版.json"
    wf = json.loads((WF_DIR / name).read_text(encoding="utf-8"))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel="msedge")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
        page.goto(BASE, wait_until="load", timeout=60000)
        page.wait_for_function("() => window.app && window.app.graph", timeout=60000)
        page.wait_for_timeout(1500)

        info = page.evaluate("""async (wf) => {
            const out = {};
            window.app.graph.clear();
            await window.app.loadGraphData(wf);
            await new Promise(r => setTimeout(r, 400));
            out.nodeCount = window.app.graph._nodes.length;
            // 每个节点序列化是否成功
            out.serialize = (window.app.graph._nodes || []).map(n => {
                try {
                    const s = n.serialize ? n.serialize() : null;
                    return {type: n.type, ok: !!s, widgets: (n.widgets||[]).length};
                } catch (e) { return {type: n.type, ok: false, err: String(e)}; }
            });
            // graphToPrompt 结果
            try {
                const p = window.app.graphToPrompt();
                out.promptKeys = Object.keys(p.output || {});
                out.promptCount = out.promptKeys.length;
                out.workflowNodeCount = (p.workflow?.nodes || []).length;
                out.sample = out.promptKeys.slice(0, 3).map(k => [k, p.output[k].class_type]);
            } catch (e) {
                out.promptError = String(e);
            }
            // 图校验
            try {
                out.graphErrors = window.app.graph._nodes
                    .map(n => (n.has_errors ? n.type : null)).filter(Boolean);
            } catch (e) { out.graphErrors = 'ERR ' + e; }
            return out;
        }""", wf)
        print(json.dumps(info, ensure_ascii=False, indent=2)[:2500])
        print("--- 控制台最后 8 条 ---")
        for line in logs[-8:]:
            print(" ", line[:180])
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
