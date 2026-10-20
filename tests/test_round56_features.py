# -*- coding: utf-8 -*-
"""ROUND56 测试：默认自动转场/硬字幕、多段列表JSON、H3 手册全文预设、台词提取、UI 契约。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.prompt import h3_handbook  # noqa: E402
from bettercomfyui_comfy.one_stop_node import (  # noqa: E402
    BetterComfyUIOneStop,
    _extract_dialogue_lines,
)


def test_defaults_are_configured_for_beginner():
    req = BetterComfyUIOneStop.INPUT_TYPES()["required"]
    assert req["转场"][1]["default"] == "AI自动（按剧情）"
    assert req["字幕"][1]["default"] == "硬字幕（程序对齐）"
    assert req["系统提示词选择"][1]["default"] == "跟随面板配置（推荐）"
    assert "多段剧本列表JSON" in req
    assert req["多段剧本列表JSON"][1]["default"] == ""


def test_handbook_full_is_real_manual_not_summary():
    full = h3_handbook.resolve_preset("h3_handbook") or ""
    core = h3_handbook.resolve_preset("h3_core") or ""
    assert len(full) > 20000, f"完整手册过短：{len(full)}，疑似只放了要点"
    assert len(core) > 3000
    for kw in ("三字段", "integrated_multimodal_description", "负面", "常见失败"):
        assert kw in full, f"手册全文缺关键字 {kw}"
    assert "MiniMax H3" in full


def test_multi_script_json_list_routes_multichapter():
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    captured: dict = {}

    def fake_multi(kwargs):
        captured.update(kwargs)
        return {"ok": True}

    node._run_multi_script = fake_multi  # type: ignore[assignment]
    out = BetterComfyUIOneStop.run(
        node,
        多段剧本列表JSON=json.dumps(["第一段", "第二段", "第三段"], ensure_ascii=False),
        多段剧本="",
    )
    assert out == {"ok": True}
    assert captured["_chapters_override"] == ["第一段", "第二段", "第三段"]


def test_multi_script_json_invalid_falls_back_single():
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    called = {}

    def fake_single(**kwargs):
        called.update(kwargs)
        return {"ok": "single"}

    node._run_single = fake_single  # type: ignore[assignment]
    out = BetterComfyUIOneStop.run(
        node, 多段剧本列表JSON="[bad json", 多段剧本="", 剧本="单段剧本")
    assert out == {"ok": "single"}
    assert called["剧本"] == "单段剧本"


def test_extract_dialogue_lines_programmatic():
    script = (
        "林秀：\"你终于来了。\"\n"
        "老陈：\"路上雪大。\"\n"
        "（旁白：不需要字幕）\n"
        "重复：\"你终于来了。\"\n"
    )
    lines = _extract_dialogue_lines(script)
    assert lines == ["你终于来了。", "路上雪大。"]


def test_ui_contract_has_add_button_and_prompt_panel():
    js = (ROOT / "bettercomfyui_comfy/web/js/dramacore_director_ui.js").read_text(encoding="utf-8")
    for needle in ("＋ 添加一段", "⑭ 提示词", "转场决策", "transition_decision",
                   "多段剧本列表JSON", "loadHandbookPrompt"):
        assert needle in js, f"UI 缺内容：{needle}"
    # 多段不再只靠分隔符：动态列表方法存在
    for needle in ("addMultiChapter", "removeMultiChapter", "moveMultiChapter"):
        assert needle in js
