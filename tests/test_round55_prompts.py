# -*- coding: utf-8 -*-
"""ROUND55 回归测试：H3 提示词手册落地 + 禁止生成字幕注入 + 配置开关。

不联网、不用 GPU（纯函数 + 配置加载）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.config import ConfigError, config_from_dict  # noqa: E402
from bettercomfyui.prompt import h3_manual as h3  # noqa: E402

import yaml  # noqa: E402


# ---------------------------------------------------------------------------
# 1) 手册要点附录
# ---------------------------------------------------------------------------
def test_system_appendix_non_empty_and_keywords():
    text = h3.build_system_prompt_appendix()
    assert isinstance(text, str)
    assert text.strip(), "H3 手册附录不能为空"
    # 任务书要求的关键字：三字段 / 可见文字 / 负面
    assert "三字段" in text
    assert "可见文字" in text
    assert "负面" in text
    # 手册关键规则抽样
    for keyword in ("integrated_multimodal_description", "overall_soundscape",
                    "non_diegetic_music", "T2VA", "Ref2VA", "字幕", "水印"):
        assert keyword in text, f"手册附录缺少关键规则：{keyword}"


def test_system_appendix_length_budget():
    text = h3.build_system_prompt_appendix()
    assert len(text) <= h3.MAX_APPENDIX_CHARS, (
        f"手册附录超长：{len(text)} > {h3.MAX_APPENDIX_CHARS}")


# ---------------------------------------------------------------------------
# 2) 负面提示词库（手册 6.10）
# ---------------------------------------------------------------------------
def test_negative_prompt_contains_core_terms():
    neg = h3.build_negative_prompt()
    assert neg.strip()
    for term in ("blurry", "watermark", "text", "logo", "bad anatomy", "flickering"):
        assert term in neg, f"负向词库缺少：{term}"
    assert ", " in neg and "blurry, low quality" in neg


def test_negative_prompt_extra_dedupe():
    neg = h3.build_negative_prompt(extra="blurry, 新增词")
    assert "新增词" in neg
    # 大小写不敏感去重：blurry 只出现一次
    assert neg.lower().count("blurry") == 1


# ---------------------------------------------------------------------------
# 3) 风格归一化 / 任务模式归一化（纯函数幂等）
# ---------------------------------------------------------------------------
def test_normalize_style_basic_and_idempotent():
    assert h3.normalize_style(None) == ""
    assert h3.normalize_style("") == ""
    out = h3.normalize_style("  冷蓝电影光，， 高对比。 ")
    assert out == "冷蓝电影光, 高对比"
    assert h3.normalize_style(out) == out
    # 超长截断
    long_text = "冷蓝电影光，" * 500
    assert len(h3.normalize_style(long_text)) <= 800


def test_normalize_task_type():
    assert h3.normalize_task_type("i2v — 图生视频(Image to Video)") == "I2VA"
    assert h3.normalize_task_type("Ref2VA") == "Ref2VA"
    assert h3.normalize_task_type("T2V") == "T2VA"
    assert h3.normalize_task_type("") == ""
    assert h3.normalize_task_type("完全不认识") == ""


# ---------------------------------------------------------------------------
# 4) 禁止生成字幕注入
# ---------------------------------------------------------------------------
def test_no_subtitle_rule_constant_covers_required_terms():
    rule = h3.NO_SUBTITLE_SYSTEM_RULE
    for term in ("字幕", "UI 文字", "水印", "logo", "文字标识", "可见文字"):
        assert term in rule, f"禁字幕系统规则缺少：{term}"


def test_inject_empty_prompt_safe():
    out = h3.inject_no_subtitle_instruction("")
    assert out and "字幕" in out
    assert h3.inject_no_subtitle_instruction("   ") == h3.NO_SUBTITLE_PROMPT_RULE


def test_inject_appends_rule_and_keeps_original():
    prompt = "integrated_multimodal_description: [Shot 1] A woman signs a document."
    out = h3.inject_no_subtitle_instruction(prompt)
    assert out.startswith(prompt)
    assert "字幕" in out and "水印" in out
    assert len(out) > len(prompt)


def test_inject_dedupe_when_instruction_present():
    prompt = "A scene. 不要生成字幕，不要水印。"
    first = h3.inject_no_subtitle_instruction(prompt)
    assert first == prompt, "已含禁字幕指令时不得重复追加"
    assert h3.inject_no_subtitle_instruction(first) == first


def test_inject_keep_scripted_visible_text():
    prompt = 'A red neon sign reading "营业中" glows above the doorway.'
    keep = h3.inject_no_subtitle_instruction(prompt, keep_scripted_visible_text=True)
    assert '"营业中"' in keep
    assert "剧本明确要求的可见文字" in keep
    assert h3.extract_scripted_visible_text(prompt) == ["营业中"]

    strict = h3.inject_no_subtitle_instruction(prompt, keep_scripted_visible_text=False)
    assert "即使剧本或提示词中出现引号文字，也不要把它画到画面上" in strict
    assert "剧本明确要求的可见文字（英文双引号内原样出现，不翻译、不改成字幕）" not in strict


# ---------------------------------------------------------------------------
# 5) 配置字段（config.py 只加字段）
# ---------------------------------------------------------------------------
def test_config_prompt_switches_default_true():
    cfg = config_from_dict({})
    assert getattr(cfg.prompt, "h3_manual_enabled") is True
    assert getattr(cfg.prompt, "no_subtitle") is True


def test_config_prompt_switches_overridable():
    cfg = config_from_dict(
        {"prompt": {"h3_manual_enabled": False, "no_subtitle": False}})
    assert cfg.prompt.h3_manual_enabled is False
    assert cfg.prompt.no_subtitle is False


def test_config_unknown_prompt_key_still_rejected():
    try:
        config_from_dict({"prompt": {"h3_manual_enabeld": True}})  # 故意拼错
    except ConfigError as exc:
        assert "未知键" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知配置键必须报错")


def test_default_yaml_optional_and_config_defaults_survive():
    """ROUND55 开关默认值由 config.py 提供（default.yaml 的显式键可在同步后追加）。

    注意：本轮**不**把这两个键写进 config/default.yaml —— 运行中的 ComfyUI 可能仍加载
    旧安装副本的 config.py（不认新键），YAML 加键会让旧副本 load_config 报未知键。
    Lead 同步（scripts/sync_to_comfyui.py）+ 重启后再加 YAML 即可。
    """
    cfg = config_from_dict({})
    assert cfg.prompt.h3_manual_enabled is True
    assert cfg.prompt.no_subtitle is True
    raw = yaml.safe_load((ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert "prompt" in raw  # YAML 里没有新键也不影响默认值（可选显式记录位）


# ---------------------------------------------------------------------------
# 6) build_prompt_bundle（供 Lead 一站式注入）
# ---------------------------------------------------------------------------
def test_build_prompt_bundle_defaults():
    bundle = h3.build_prompt_bundle(prompt="[Shot 1] A woman walks in.")
    assert bundle["h3_manual_enabled"] is True
    assert bundle["no_subtitle"] is True
    assert bundle["system_appendix"]
    assert bundle["appendix_chars"] == len(bundle["system_appendix"])
    assert bundle["system_rule"] == h3.NO_SUBTITLE_SYSTEM_RULE
    assert "字幕" in bundle["injected_prompt"]
    assert "watermark" in bundle["negative_prompt"]
    assert bundle["negative_for_video"]


def test_build_prompt_bundle_switches_off():
    cfg = SimpleNamespace(prompt=SimpleNamespace(h3_manual_enabled=False, no_subtitle=False))
    bundle = h3.build_prompt_bundle(prompt="[Shot 1] A woman walks in.", cfg=cfg)
    assert bundle["h3_manual_enabled"] is False
    assert bundle["no_subtitle"] is False
    assert bundle["system_appendix"] == ""
    assert bundle["system_rule"] == ""
    assert bundle["negative_prompt"] == ""
    assert bundle["injected_prompt"] == "[Shot 1] A woman walks in."


def test_build_prompt_bundle_empty_prompt_safe():
    bundle = h3.build_prompt_bundle(prompt="")
    assert bundle["injected_prompt"] == h3.NO_SUBTITLE_PROMPT_RULE
