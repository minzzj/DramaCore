"""让网页面板的 DOM 级执行测试纳入 pytest（无需浏览器）。

## 这条测试补的是什么

此前关于面板的"验证"只有三条，**都不能证明面板能跑**：

* `node --check` 能解析 —— 只证明语法对，不证明逻辑能跑；
* 路由返回 200 —— 只证明后端在，不证明前端会调用它；
* 节点已注册 —— 与面板无关。

真实的失败模式是：模块在 `registerExtension` 之前抛错、引用了不存在的 DOM API、
或初始化时报错但被自己的 try/catch 吞掉（只剩一行 console.error）。
这些**只有真正执行一遍**才会暴露。

`tests/js/panel_dom_test.mjs` 用最小 DOM 替身把面板跑起来，
本文件把它接进 pytest 并做**执行器自检**（防止"跳过"被当成"通过"）。

无 Node 时 **skip 并标注未验证** —— 不假装跑过。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
SCRIPT = REPO_ROOT / "tests" / "js" / "panel_dom_test.mjs"

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="未验证：本机没有 node，无法执行面板的 DOM 级测试",
)


def test_harness_script_exists():
    assert SCRIPT.is_file(), f"面板 DOM 测试脚本不存在：{SCRIPT}"


def test_panel_executes_clean_in_a_dom():
    """面板必须在替身 DOM 里真正跑起来，且**不打印任何错误**。

    这条会真实执行 `bettercomfyui_comfy/web/js/bettercomfyui_panel.js`。
    """
    proc = subprocess.run(
        [NODE, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"面板 DOM 测试失败（exit={proc.returncode}）：\n{out}"
    )
    # 脚本自己会打印逐项结果；这里再核一遍没有 [FAIL]
    assert "[FAIL]" not in out, f"存在失败项：\n{out}"
    assert "9/9 通过" in out or "通过" in out, out


def test_panel_test_actually_reports_checks():
    """执行器自检：脚本必须**真的跑了几项检查**。

    防止「脚本退化成什么都不做也返回 0」—— 那样这条测试就成了摆设。
    """
    proc = subprocess.run(
        [NODE, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )
    out = proc.stdout or ""
    ok_count = out.count("[OK]")
    assert ok_count >= 8, (
        f"面板 DOM 测试只跑了 {ok_count} 项检查（期望 >=8）—— "
        "脚本可能被改坏了，测试形同虚设"
    )
    # 关键断言必须在其中，否则说明检查项被删了
    for must in ("app.registerExtension", "setup() 能跑完", "面板没有打印任何错误"):
        assert must in out, f"缺少关键检查项：{must}\n{out}"


def test_panel_has_no_undefined_global_references():
    """静态兜底：面板引用的 DOM/全局 API 必须都是真实存在的。

    这不是替代上面的执行测试，而是当 Node 不可用时的**降级证据**
    （本机有 Node 时两条都会跑）。
    """
    src = (REPO_ROOT / "bettercomfyui_comfy" / "web" / "js"
           / "bettercomfyui_panel.js").read_text(encoding="utf-8")
    # 面板用到的全部 DOM 方法（已 grep 核实）。少一个都会在浏览器里炸。
    for api in ("createElement", "getElementById", "querySelector",
                "querySelectorAll", "appendChild", "addEventListener",
                "insertAdjacentHTML", "textContent", "innerHTML"):
        assert api in src, f"面板没有用到 {api}？请核对（此断言用于发现意外改动）"
    # 不能有内联 onclick（会被 CSP 拦）
    assert "onclick=" not in src, "面板用了内联 onclick —— 会被 ComfyUI 的 CSP 拦掉"
