# -*- coding: utf-8 -*-
"""ROUND58 测试：H3 提示词工程精简版（V1.0 / 2026-10）集成。

验收点：
1. docx 全文落地成数据文件（>12,000 字，含「系统提示词 1：H3 视频生成提示词编译器」
   「系统提示词 2」「提示词片段库」），UTF-8 无 BOM、LF；
2. ``resolve_preset("h3_concise_v1")`` 返回全文；旧 ``h3_handbook`` / ``h3_core`` 仍可用；
3. everything_node 的系统提示词选择把精简版放最前，「跟随面板为空」默认精简版；
4. 面板 JS 默认加载精简版全文，另提供「加载历史完整版」，其 mode 别名与服务端解析一致。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.prompt import h3_handbook  # noqa: E402
from bettercomfyui_comfy.everything_node import (  # noqa: E402
    SYSTEM_PROMPT_CHOICES,
    SYSTEM_PROMPT_DEFAULT_KEY,
    BetterComfyUIEverything,
    default_system_prompt_label,
    is_system_prompt_preset,
    resolve_template_choice,
    system_prompt_key,
    system_prompt_label,
)

DATA = ROOT / "bettercomfyui" / "prompt" / "data" / "h3_concise_v1.txt"
UI_JS = ROOT / "bettercomfyui_comfy" / "web" / "js" / "dramacore_director_ui.js"

CONCISE_LABEL = "H3 提示词精简版（2026-10，推荐）"

DOCX_SECTIONS = (
    "系统提示词 1：H3 视频生成提示词编译器（Master）",
    "系统提示词 2：素材图提示词生成器（文生图用）",
    "提示词片段库（按需拼入，增强生成质量）",
)


# ---------------------------------------------------------------------------
# 1) 数据文件 = docx 全文（不是摘要）
# ---------------------------------------------------------------------------


def test_concise_data_file_is_full_docx_text():
    assert DATA.is_file(), f"缺数据文件：{DATA}"
    raw = DATA.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "数据文件不能带 BOM（UTF-8 无 BOM）"
    text = raw.decode("utf-8")
    assert "\r\n" not in text, "数据文件统一 LF，避免 diff/字数漂移"
    assert len(text) > 12000, f"精简版全文过短：{len(text)} 字，疑似只放了摘要"
    for section in DOCX_SECTIONS:
        assert section in text, f"精简版缺章节：{section}"
    for keyword in (
        "使用说明（3 行）",
        "V1.0",
        "2026 年 10 月",
        "输出前自检清单",
        "integrated_multimodal_description",
        "non_diegetic_music",
        "negative_prompt",
        "口播专用补丁",
        "短剧钩子",
    ):
        assert keyword in text, f"精简版缺关键内容：{keyword}"


# ---------------------------------------------------------------------------
# 2) 预设解析：精简版默认、旧版保留
# ---------------------------------------------------------------------------


def test_concise_preset_returns_whole_document():
    body = DATA.read_text(encoding="utf-8").strip()
    text = h3_handbook.resolve_preset("h3_concise_v1") or ""
    assert body in text, "resolve_preset 必须返回全文（正文一字不少）"
    assert len(text) > len(body) > 12000
    assert h3_handbook.get_concise_handbook() == text
    assert h3_handbook.PRESET_CONCISE == "h3_concise_v1"
    assert h3_handbook.DEFAULT_PRESET == "h3_concise_v1"
    assert h3_handbook.PRESETS[0] == "h3_concise_v1"
    assert h3_handbook.preset_chars("h3_concise_v1") == len(text)
    assert h3_handbook.preset_label("h3_concise_v1").startswith("H3 提示词精简版")
    assert h3_handbook.preset_file("h3_concise_v1") == "data/h3_concise_v1.txt"


def test_legacy_presets_kept_and_unchanged():
    full = h3_handbook.resolve_preset("h3_handbook") or ""
    core = h3_handbook.resolve_preset("h3_core") or ""
    assert len(full) > 20000, f"历史完整版被破坏：{len(full)}"
    assert len(core) > 3000, f"旧核心章节被破坏：{len(core)}"
    assert "MiniMax H3" in full
    assert h3_handbook.PRESET_FULL == "h3_handbook"
    assert h3_handbook.PRESET_CORE == "h3_core"
    assert h3_handbook.preset_file("h3_handbook") == "data/h3_handbook_full.txt"


def test_preset_aliases_resolve():
    for alias in ("h3_concise_v1", "H3_CONCISE_V1", "concise", "h3_concise", "concise_v1", "精简版"):
        assert h3_handbook.canonical_preset(alias) == "h3_concise_v1", alias
        assert h3_handbook.is_handbook_preset(alias)
    assert h3_handbook.canonical_preset("full") == "h3_handbook"
    assert h3_handbook.canonical_preset("完整版") == "h3_handbook"
    assert h3_handbook.canonical_preset("core") == "h3_core"
    assert h3_handbook.canonical_preset("精简") == "h3_core"
    assert h3_handbook.resolve_preset("不存在的预设") is None
    assert not h3_handbook.is_handbook_preset("默认")


def test_preset_metadata_complete_for_all_presets():
    for key in h3_handbook.PRESETS:
        assert key in h3_handbook.PRESET_FILES, key
        assert key in h3_handbook.PRESET_LABELS, key
        assert h3_handbook.resolve_preset(key), key
    assert (DATA.parent / "h3_handbook_full.txt").is_file()


# ---------------------------------------------------------------------------
# 3) 节点选项 / 默认逻辑（可单测）
# ---------------------------------------------------------------------------


def test_choices_put_concise_first_and_keep_legacy():
    labels = list(SYSTEM_PROMPT_CHOICES)
    assert labels[0] == CONCISE_LABEL
    assert SYSTEM_PROMPT_CHOICES[labels[0]] == "h3_concise_v1"
    assert SYSTEM_PROMPT_DEFAULT_KEY == "h3_concise_v1"
    assert default_system_prompt_label() == CONCISE_LABEL
    values = list(SYSTEM_PROMPT_CHOICES.values())
    assert values.count("h3_handbook") == 1
    assert values.count("h3_core") == 1
    assert SYSTEM_PROMPT_CHOICES["H3 手册（完整，历史版）"] == "h3_handbook"
    for key in ("default", "strict_json", "chinese_drama", "shot_by_shot", "local_qwen"):
        assert key in values, f"旧模板丢了：{key}"


def test_everything_node_input_default_is_concise():
    spec = BetterComfyUIEverything.INPUT_TYPES()["required"]["系统提示词选择"]
    choices, opts = spec
    assert choices[0] == CONCISE_LABEL
    assert opts["default"] == CONCISE_LABEL
    assert "H3 手册（完整，历史版）" in choices
    assert "H3 手册（精简）" in choices


def test_follow_panel_empty_defaults_to_concise():
    # 跟随面板 + 面板为空 → ROUND58 默认精简版（不是旧完整版）
    assert resolve_template_choice("跟随面板配置（推荐）") == "h3_concise_v1"
    assert resolve_template_choice("跟随面板配置（推荐）", panel_value="   ") == "h3_concise_v1"
    # 面板里存了预设键 / 旧标签 → 收口成预设键（旧工作流兼容）
    assert resolve_template_choice(
        "跟随面板配置（推荐）", panel_value="h3_handbook") == "h3_handbook"
    assert resolve_template_choice(
        "跟随面板配置（推荐）", panel_value="H3 手册（完整，推荐）") == "h3_handbook"
    # 节点上强制指定
    assert resolve_template_choice("H3 手册（完整，历史版）") == "h3_handbook"
    assert resolve_template_choice("H3 手册（精简）") == "h3_core"
    assert resolve_template_choice(CONCISE_LABEL) == "h3_concise_v1"
    assert resolve_template_choice("不认识的标签") == "default"
    # 自定义全文永远优先
    assert resolve_template_choice("跟随面板配置（推荐）", custom="自定义全文") == "自定义全文"
    assert resolve_template_choice(CONCISE_LABEL, custom="自定义") == "自定义"
    # 面板里存的是一整段自定义提示词 → 原样用
    custom_text = "你是一段面板里保存的自定义系统提示词，必须原样使用。"
    assert resolve_template_choice(
        "跟随面板配置（推荐）", panel_value=custom_text) == custom_text


def test_prompt_label_helpers_round_trip():
    for label, key in SYSTEM_PROMPT_CHOICES.items():
        assert system_prompt_key(label) == key
        assert system_prompt_label(key) == label
        assert is_system_prompt_preset(label)
        assert is_system_prompt_preset(key)
    # ROUND56 旧工作流里的旧标签仍可解析
    assert system_prompt_key("H3 手册（完整，推荐）") == "h3_handbook"
    assert system_prompt_label("H3 手册（完整，推荐）") == "H3 手册（完整，历史版）"
    # 空 / 未知 → 默认精简版标签（不静默掉到 default）
    assert system_prompt_label("") == CONCISE_LABEL
    assert system_prompt_label("concise") == CONCISE_LABEL
    assert system_prompt_key("") == "default"
    assert not is_system_prompt_preset("你是一段自定义系统提示词，" * 20)


def test_one_stop_default_follows_panel_with_concise_copy():
    from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop

    spec = BetterComfyUIOneStop.INPUT_TYPES()["required"]["系统提示词选择"]
    choices, opts = spec
    assert opts["default"] == "跟随面板配置（推荐）"
    assert choices[0] == "跟随面板配置（推荐）"
    assert CONCISE_LABEL in choices
    assert "提示词工程精简版" in opts["tooltip"]
    assert "12,890" in opts["tooltip"]
    assert "历史版" in opts["tooltip"]


# ---------------------------------------------------------------------------
# 4) 面板 UI：默认加载精简版，旧完整版仍可加载
# ---------------------------------------------------------------------------


def test_ui_panel_defaults_to_concise_and_offers_legacy_full():
    js = UI_JS.read_text(encoding="utf-8")
    assert 'async loadHandbookPrompt(mode = "concise", opts = {})' in js
    assert 'await this.loadHandbookPrompt("concise")' in js, "面板默认必须加载精简版"
    assert "加载 H3 精简版（推荐）" in js
    assert "加载历史完整版" in js
    assert "加载核心章节" in js
    assert "bettercomfyui/prompt/data/h3_concise_v1.txt" in js
    assert "bettercomfyui/prompt/data/h3_handbook_full.txt" in js
    assert "默认 H3 手册完整版" not in js, "旧文案（默认完整版）必须更新"
    assert "默认：H3 提示词精简版 V1.0（2026-10）" in js
    # 面板配置读取层级修正：GET /panel/config 的默认提示词在 defaults.system_prompt
    assert "defaults.system_prompt" in js
    # 保存预置时存预设键（避开 routes.py 的 8000 字符上限）
    assert "system_prompt: presetKey" in js
    assert "8000" in js


def test_ui_handbook_mode_aliases_resolve_on_server():
    """UI 发出去的 mode 别名，服务端 h3_handbook.resolve_preset 必须都能解析到精简版全文。"""
    js = UI_JS.read_text(encoding="utf-8")
    match = re.search(r'aliases: \[([^\]]*"h3_concise_v1"[^\]]*)\]', js)
    assert match, "UI 缺精简版 mode 别名列表"
    aliases = re.findall(r'"([^"]+)"', match.group(1))
    assert "concise" in aliases
    assert "h3_concise_v1" in aliases
    for alias in aliases:
        text = h3_handbook.resolve_preset(alias) or ""
        assert len(text) > 12000, f"mode 别名 {alias} 没解析到精简版全文"
    # 旧模式仍在 JS 里可用（历史完整版/核心章节）
    assert 'this.loadHandbookPrompt("full")' in js
    assert 'this.loadHandbookPrompt("core")' in js
