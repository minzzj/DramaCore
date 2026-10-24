# -*- coding: utf-8 -*-
"""ROUND74 契约不确定性的源码级回归测试。

覆盖用户提出的三件事：
1) I2VA 对齐行放在 prompt 里是否会被误判（以及作为段级额外字段时的行为）；
2) Ref2VA 六段结构：放顶层 vs 放 prompt 内，validate.py 的真实行为；
3) segments[].new_assets 与顶层 assets 同 id 是否会触发 duplicate_id。
"""
from __future__ import annotations

import pytest

from bettercomfyui.prompt.validate import (
    ScriptValidationError,
    canonicalize_script,
    validate_script,
)

ALIGN = ("For the target video, at 0.00 seconds into the target video, "
         "<Picture 1> (from [Shot 1]) is fully referenced.")
THREE = ("integrated_multimodal_description: [Shot 1] A man walks into the room.\n"
         "overall_soundscape: Footsteps on marble, low room ambience.\n"
         "non_diegetic_music: Sparse piano at slow tempo.")


def _script(prompt: str, **seg_extra):
    seg = {"segment_index": 1, "prompt": prompt, "asset_refs": [], "duration_s": 5.0}
    seg.update(seg_extra)
    return {"assets": [], "chapters": [{"chapter_id": "CH001", "segments": [seg]}]}


def test_i2va_alignment_line_inside_prompt_is_accepted():
    """对齐行放在 prompt 内：校验只看 prompt 是非空字符串，不会误判 missing_field。"""
    out = validate_script(_script(ALIGN + "\n\n" + THREE))
    assert ALIGN in out["chapters"][0]["segments"][0]["prompt"]


def test_alignment_line_as_extra_segment_field_is_ignored_not_rejected():
    """对齐行作为段级额外字段：未知字段不校验、不报错（但 prompt 仍必须存在）。"""
    out = validate_script(_script(THREE, alignment_instruction=ALIGN))
    seg = out["chapters"][0]["segments"][0]
    assert "alignment_instruction" in seg
    assert seg["prompt"] == THREE


def test_six_section_top_level_without_chapters_is_rejected():
    """Ref2VA 六段放在顶层：没有 chapters/assets → missing_field（不是静默通过）。"""
    six = {
        "subject_definitions": "…",
        "summary": "…",
        "retention_analysis": "…",
        "detailed_description": "[Shot 1] …",
        "overall_soundscape": "…",
        "non_diegetic_music": "…",
    }
    with pytest.raises(ScriptValidationError) as ei:
        validate_script(six)
    codes = {e.get("code") for e in exc_errors(ei.value)}
    assert "missing_field" in codes


def test_six_section_inside_prompt_is_accepted():
    """六段文本整体放进 segments[].prompt：validator 不解析内容，可直接通过。"""
    six_text = ("subject_definitions: …\nsummary: …\nretention_analysis: …\n"
                + THREE)
    out = validate_script(_script(six_text))
    assert six_text in out["chapters"][0]["segments"][0]["prompt"]


def test_segment_new_assets_same_id_as_top_level_is_deduped_not_duplicate_id():
    """段内 new_assets 与顶层 assets 同 id/同名：合并时跳过，不触发 duplicate_id。"""
    payload = {
        "assets": [{"id": "A1", "asset_type": "CHARACTER", "name": "王振东", "prompt": "x"}],
        "chapters": [{
            "chapter_id": "CH001",
            "segments": [{
                "segment_index": 1, "prompt": THREE, "asset_refs": ["A1"], "duration_s": 5.0,
                "new_assets": [{"id": "A1", "asset_type": "CHARACTER", "name": "王振东", "prompt": "x"}],
            }],
        }],
    }
    out = validate_script(payload)
    assert [a["id"] for a in out["assets"]] == ["A1"]


def test_duplicate_id_inside_top_level_assets_still_errors():
    """真正重复的是顶层 assets 数组内部的 id → 仍会 duplicate_id。"""
    payload = {
        "assets": [
            {"id": "A1", "asset_type": "CHARACTER", "name": "甲", "prompt": "x"},
            {"id": "A1", "asset_type": "SCENE", "name": "乙", "prompt": "y"},
        ],
        "chapters": [{"chapter_id": "CH001", "segments": [
            {"segment_index": 1, "prompt": THREE, "asset_refs": [], "duration_s": 5.0}]}],
    }
    with pytest.raises(ScriptValidationError) as ei:
        validate_script(payload)
    codes = {e.get("code") for e in exc_errors(ei.value)}
    assert "duplicate_id" in codes


def test_new_assets_across_two_segments_same_id_merged_once():
    payload = {
        "assets": [],
        "chapters": [{"chapter_id": "CH001", "segments": [
            {"segment_index": 1, "prompt": THREE, "asset_refs": [], "duration_s": 5.0,
             "new_assets": [{"id": "A1", "asset_type": "CHARACTER", "name": "王振东", "prompt": "x"}]},
            {"segment_index": 2, "prompt": THREE, "asset_refs": [], "duration_s": 5.0,
             "new_assets": [{"id": "A1", "asset_type": "CHARACTER", "name": "王振东", "prompt": "x"}]},
        ]}],
    }
    out = validate_script(payload)
    assert len(out["assets"]) == 1


def test_segment_level_h3_fields_merged_by_canonicalize():
    """三字段拆在段级时：canonicalize 会合并进 prompt（对齐行/六段不参与合并）。"""
    payload = {
        "assets": [],
        "chapters": [{"chapter_id": "CH001", "segments": [{
            "segment_index": 1, "asset_refs": [], "duration_s": 5.0,
            "integrated_multimodal_description": "A man walks into the room.",
            "overall_soundscape": "Footsteps.",
            "non_diegetic_music": "Piano.",
        }]}],
    }
    out = validate_script(canonicalize_script(payload))
    assert "integrated_multimodal_description" in out["chapters"][0]["segments"][0]["prompt"]


def exc_errors(exc: ScriptValidationError):
    return getattr(exc, "errors", None) or []
