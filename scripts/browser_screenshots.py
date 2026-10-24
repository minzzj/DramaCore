# -*- coding: utf-8 -*-
"""给三套「总流程」工作流拍清晰截图（自动缩放居中，只截画布区域），供一页纸说明展示。

用法：python -B scripts/browser_screenshots.py
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8188"
WF_DIR = REPO / "bettercomfyui_comfy" / "workflows"
OUT_DIR = REPO / "docs" / "images"

TARGETS = [
    ("00_【总流程】剧本→全部内容（本机可跑）.json", "01_剧本到全部内容"),
    ("00_【总流程】段视频→剪好成片（本机可跑）.json", "02_段视频到成片"),
    ("00_【总流程】剧本→成片（GPU）.json", "03_剧本到成片GPU"),
]


def _get(path: str):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=60))


def _bbox(wf: dict) -> tuple[float, float, float, float]:
    xs, ys, xe, ye = [], [], [], []
    for node in wf.get("nodes") or []:
        pos = node.get("pos") or [0, 0]
        if isinstance(pos, dict):
            x, y = pos.get("0", 0), pos.get("1", 0)
        else:
            x, y = pos[0], pos[1]
        size = node.get("size") or [300, 150]
        xs.append(x); ys.append(y)
        xe.append(x + size[0]); ye.append(y + size[1])
    return min(xs), min(ys), max(xe), max(ye)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[FAIL] 未安装 playwright（用系统 python 跑）")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    info = _get("/object_info")
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
        page = browser.new_page(viewport={"width": 1920, "height": 1200})
        page.goto(BASE, wait_until="load", timeout=60000)
        page.wait_for_function("() => window.app && window.app.graph && window.LiteGraph",
                               timeout=60000)
        page.wait_for_timeout(1500)
        failed = 0
        for filename, short in TARGETS:
            path = WF_DIR / filename
            if not path.is_file():
                print(f"[SKIP] 缺少 {filename}")
                failed += 1
                continue
            wf = json.loads(path.read_text(encoding="utf-8"))
            x0, y0, x1, y1 = _bbox(wf)
            width, height = max(1.0, x1 - x0), max(1.0, y1 - y0)
            # 留 8% 边距，按画布区（约 1500x950）计算缩放
            scale = min(1500.0 / (width * 1.08), 950.0 / (height * 1.08), 0.9)
            result = page.evaluate("""async ([wf, box, scale]) => {
                try {
                    window.app.graph.clear();
                    await window.app.loadGraphData(wf);
                    await new Promise(r => setTimeout(r, 900));
                    const ds = window.app.canvas.ds;
                    ds.scale = scale;
                    ds.offset = [-box.x0 * scale + 40, -box.y0 * scale + 40];
                    window.app.canvas.setDirty(true, true);
                    await new Promise(r => setTimeout(r, 900));
                    return {ok: true, nodes: (window.app.graph._nodes || []).length,
                            scale: ds.scale};
                } catch (e) { return {ok: false, error: String(e)}; }
            }""", [wf, {"x0": x0, "y0": y0, "x1": x1, "y1": y1}, scale])
            if not result.get("ok"):
                print(f"[FAIL] {filename}: {result.get('error')}")
                failed += 1
                continue
            out = OUT_DIR / f"{short}.png"
            canvas = page.locator("#graph-canvas")
            try:
                canvas.screenshot(path=str(out))
            except Exception:  # noqa: BLE001 - 退化为整页截图
                page.screenshot(path=str(out), full_page=False)
            print(f"[OK] {filename} -> {out}  (节点 {result['nodes']}, scale {result['scale']:.2f})")
        browser.close()
    print(f"[done] 截图目录：{OUT_DIR}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
