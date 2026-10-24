"""``prompt/inject.py`` 测试：既有素材注入（R1 落地）与提示词组装。"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from bettercomfyui.prompt.errors import PromptDependencyError
from bettercomfyui.prompt.inject import (
    SYSTEM_PROMPT_PRESETS,
    build_asset_manifest,
    build_prompt_messages,
    build_system_prompt,
    build_user_prompt,
)

MANIFEST = (
    "【既有素材清单】项目库\n"
    "CHARACTER  #1  林澈        v1  LOCKED (MODEL_PASS)\n"
    "SCENE      #2  雪夜长街    v1  UNLOCKED\n"
)
SCRIPT = "第一章 雪夜\n林澈独行于雪夜长街，忽闻身后剑鸣。"


class Builder:
    """记录调用参数的清单构造器替身。"""

    def __init__(self, text: str = MANIFEST) -> None:
        self.text = text
        self.calls: list[dict] = []

    def __call__(self, store=None, library_scope=None):
        self.calls.append({"store": store, "library_scope": library_scope})
        return self.text


def test_manifest_is_injected_and_forbids_ai_existence_judgement():
    builder = Builder()
    messages = build_prompt_messages(
        script_text=SCRIPT, cfg={"prompt": {}}, store=object(), manifest_builder=builder
    )

    system = messages["system"]
    assert MANIFEST.strip() in system, "既有素材清单必须原样注入"
    assert messages["manifest_injected"] is True
    assert "禁止再次输出" in system
    assert "存在性判断由程序" in system
    assert "只输出新增" in system or "只能" in system
    assert len(builder.calls) == 1


def test_user_prompt_contains_script_and_tail_reminder():
    messages = build_prompt_messages(
        script_text=SCRIPT, cfg={"prompt": {}}, manifest=MANIFEST
    )
    user = messages["user"]
    assert SCRIPT in user
    assert "不要输出「已存在 / 不存在」" in user
    assert "再次强调" in user


def test_empty_library_uses_empty_branch_and_skips_injection():
    builder = Builder(text="")
    messages = build_prompt_messages(
        script_text=SCRIPT, cfg={"prompt": {}}, store=object(), manifest_builder=builder
    )

    assert messages["manifest"] == ""
    assert messages["manifest_injected"] is False
    assert "素材库为空" in messages["system"]
    assert "禁止再次输出" not in messages["system"], "空库不应出现注入块"


def test_inject_can_be_disabled_by_config():
    builder = Builder()
    messages = build_prompt_messages(
        script_text=SCRIPT,
        cfg={"prompt": {"inject_existing_assets": False}},
        store=object(),
        manifest_builder=builder,
    )
    assert builder.calls == [], "关闭注入后不得再去读素材清单"
    assert messages["manifest"] == ""
    assert "素材库为空" in messages["system"]


def test_explicit_manifest_skips_store_access():
    builder = Builder()
    messages = build_prompt_messages(
        script_text=SCRIPT,
        cfg={"prompt": {}},
        store=None,
        manifest=MANIFEST,
        manifest_builder=builder,
    )
    assert builder.calls == []
    assert MANIFEST.strip() in messages["system"]


def test_missing_store_inject_module_fails_loudly(monkeypatch):
    monkeypatch.setitem(sys.modules, "bettercomfyui.store.inject", None)
    with pytest.raises(PromptDependencyError) as excinfo:
        build_asset_manifest(object())
    assert "manifest_builder" in str(excinfo.value)


def test_delimited_format_instructions():
    system = build_system_prompt(cfg={"prompt": {"output_format": "分隔符"}}, manifest="")
    assert "=== ASSET ===" in system
    assert "type: CHARACTER" in system


def test_json_format_includes_schema_skeleton():
    system = build_system_prompt(cfg={"prompt": {"output_format": "json"}}, manifest=MANIFEST)
    assert '"asset_type"' in system
    assert "CHARACTER / SCENE / PROP / SHOT" in system
    assert "chapters" in system


def test_local_model_gets_simplified_constraints():
    system = build_system_prompt(
        cfg={"prompt": {"model_provider": "local_qwen", "output_format": "json"}}, manifest=MANIFEST
    )
    assert "简化要求（本地模型）" in system
    assert "不要解释" in system


def test_preset_selection_and_literal_template():
    assert build_system_prompt(cfg={"prompt": {"system_prompt_template": "strict_json"}}).startswith(
        SYSTEM_PROMPT_PRESETS["strict_json"]
    )
    literal = "这是我自定义的系统提示词，请严格遵守。"
    assert literal in build_system_prompt(cfg={"prompt": {"system_prompt_template": literal}})

    preset_mapping = build_system_prompt(
        cfg={"prompt": {"system_prompt_template": {"a": "第一段", "b": "第二段"}}}
    )
    assert "第一段" in preset_mapping and "第二段" in preset_mapping


def test_language_and_granularity_are_reflected():
    cfg = {"prompt": {"output_language": "英文", "story_granularity": "按场景"}}
    system = build_system_prompt(cfg=cfg, manifest="")
    user = build_user_prompt(script_text=SCRIPT, cfg=cfg)
    assert "英文" in system and "英文" in user
    assert "按场景" in user


def test_empty_script_text_is_rejected():
    with pytest.raises(PromptDependencyError):
        build_user_prompt(script_text="   ")
    with pytest.raises(PromptDependencyError):
        build_user_prompt(script_text=None)  # type: ignore[arg-type]


def test_all_five_presets_exist():
    assert len(SYSTEM_PROMPT_PRESETS) == 5
    assert set(SYSTEM_PROMPT_PRESETS) == {
        "default",
        "strict_json",
        "local_qwen",
        "chinese_drama",
        "shot_by_shot",
    }
