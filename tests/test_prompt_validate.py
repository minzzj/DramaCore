"""``prompt/validate.py`` 测试：解析 + 严格校验（任务书验收 2：无效 JSON 被拦截）。

硬要求：缺字段 / 无效引用 / 重复 ID / 格式错误**一律抛错**，
且**绝不返回部分结果**。
"""

from __future__ import annotations

import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import copy

import pytest

from _prompt_testutil import valid_script
from bettercomfyui.prompt.validate import (
    ParseReport,
    ScriptValidationError,
    parse_script_output,
    parse_with_report,
    validate_script,
)


def _broken(mutator) -> dict:
    script = valid_script()
    mutator(script)
    return script


# ---------------------------------------------------------------------------
# 合法路径
# ---------------------------------------------------------------------------


def test_valid_script_parses_and_validates():
    script = valid_script()
    parsed = parse_script_output(json.dumps(script, ensure_ascii=False))
    result = validate_script(parsed)
    assert result["title"] == "第一章 雪夜"
    assert len(result["chapters"]) == 1
    chapter = result["chapters"][0]
    assert chapter["chapter_id"] == "CH001"
    assert [s["segment_index"] for s in chapter["segments"]] == [1, 2]
    assert chapter["segments"][0]["asset_refs"] == ["A1", "A2"]
    assert chapter["segments"][0]["first_frame_ref"] == "A2"
    assert [a["id"] for a in result["assets"]] == ["A1", "A2"]
    assert result["assets"][0]["asset_type"] == "CHARACTER"
    assert result["assets"][0]["source"] == "AI"
    assert result["assets"][0]["library_scope"] == "PROJECT"
    assert result["assets"][0]["normalized_name"] == "林澈"


def test_validate_fills_missing_chapter_id_and_segment_index():
    script = _broken(
        lambda s: (
            s["chapters"][0].pop("chapter_id"),
            s["chapters"][0]["segments"][0].pop("segment_index"),
            s["chapters"][0]["segments"][1].pop("segment_index"),
        )
    )
    result = validate_script(script)
    assert result["chapters"][0]["chapter_id"] == "CH001"
    assert [s["segment_index"] for s in result["chapters"][0]["segments"]] == [1, 2]


def test_validate_does_not_mutate_input_and_returns_new_object():
    script = valid_script()
    snapshot = copy.deepcopy(script)
    result = validate_script(script)
    assert script == snapshot, "校验器不得就地修改调用方传入的对象"
    assert result is not script
    result["assets"][0]["name"] = "改过了"
    assert script["assets"][0]["name"] == "林澈"


def test_existing_asset_reference_resolves(tmp_path):
    script = valid_script()
    script["chapters"][0]["segments"][0]["asset_refs"].append("苏晚")
    with pytest.raises(ScriptValidationError) as excinfo:
        validate_script(script)
    assert "invalid_reference" in excinfo.value.codes

    result = validate_script(script, existing_assets=["苏晚", "雪夜长街"])
    assert "苏晚" in result["chapters"][0]["segments"][0]["asset_refs"]


def test_existing_assets_pairwise_normalization():
    script = valid_script()
    script["chapters"][0]["segments"][0]["asset_refs"] = ["苏 晚"]
    result = validate_script(script, existing_assets=["苏晚"])
    assert result["chapters"][0]["segments"][0]["asset_refs"] == ["苏 晚"]


def test_asset_ref_accepts_type_prefix_and_dict():
    script = valid_script()
    script["chapters"][0]["segments"][1]["asset_refs"] = ["角色:林澈", {"id": "A2"}]
    result = validate_script(script)
    assert result["chapters"][0]["segments"][1]["asset_refs"] == ["林澈", "A2"]


# ---------------------------------------------------------------------------
# 解析失败：绝不返回部分结果
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "这不是 JSON",
        "{不是合法 JSON}",
        '{"chapters": [1,2,3}',          # 括号不闭合
        '{"chapters": [] "assets": []}',  # 缺逗号
    ],
)
def test_invalid_json_is_rejected(raw):
    with pytest.raises(ScriptValidationError) as excinfo:
        parse_script_output(raw, output_format="json")
    assert excinfo.value.errors, "必须携带结构化错误列表"
    assert all({"path", "code", "message"} <= set(e) for e in excinfo.value.errors)


def test_top_level_array_is_rejected():
    with pytest.raises(ScriptValidationError) as excinfo:
        parse_script_output('[{"name": "林澈"}]')
    assert "invalid_top_level_type" in excinfo.value.codes


def test_output_format_json_does_not_fall_back_to_delimited():
    """配置成 json 时，分隔符文本必须被拦截，而不是被「宽容」解析。"""
    delimited = "=== ASSET ===\nname: 林澈\nprompt: 少年\n"
    with pytest.raises(ScriptValidationError) as excinfo:
        parse_script_output(delimited, output_format="json")
    assert "invalid_json" in excinfo.value.codes


def test_unknown_output_format_is_rejected():
    with pytest.raises(ScriptValidationError) as excinfo:
        parse_script_output("{}", output_format="yaml")
    assert "invalid_output_format" in excinfo.value.codes


def test_non_str_raw_is_rejected():
    with pytest.raises(ScriptValidationError):
        parse_script_output({"chapters": []})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 校验失败：缺字段 / 重复 ID / 无效引用 / 格式错误
# ---------------------------------------------------------------------------


def _expect_error(script: dict, code: str, path_fragment: str = "") -> ScriptValidationError:
    with pytest.raises(ScriptValidationError) as excinfo:
        validate_script(script)
    error = excinfo.value
    assert code in error.codes, f"期望错误码 {code}，实际 {error.codes}｜{error}"
    if path_fragment:
        assert any(path_fragment in e["path"] for e in error.errors), error.errors
    return error


def test_missing_top_level_fields():
    _expect_error(_broken(lambda s: s.pop("chapters")), "missing_field", "$.chapters")
    _expect_error(_broken(lambda s: s.pop("assets")), "missing_field", "$.assets")


def test_missing_asset_fields():
    _expect_error(_broken(lambda s: s["assets"][0].pop("id")), "missing_field", ".assets[0].id")
    _expect_error(_broken(lambda s: s["assets"][0].pop("name")), "missing_field", ".assets[0].name")
    _expect_error(
        _broken(lambda s: s["assets"][0].pop("asset_type")), "missing_field", ".assets[0].asset_type"
    )
    _expect_error(
        _broken(lambda s: s["assets"][0].pop("prompt")), "missing_field", ".assets[0].prompt"
    )


def test_missing_segment_fields():
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].pop("prompt")),
        "missing_field",
        "segments[0].prompt",
    )
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].pop("asset_refs")),
        "missing_field",
        "segments[0].asset_refs",
    )


def test_empty_fields_are_rejected():
    _expect_error(_broken(lambda s: s["assets"][0].update(prompt="   ")), "empty_field")
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].update(prompt="")), "empty_field"
    )
    _expect_error(_broken(lambda s: s["chapters"][0].update(segments=[])), "empty_field")
    _expect_error(_broken(lambda s: s.update(chapters=[])), "empty_field")


def test_duplicate_ids_are_rejected():
    _expect_error(
        _broken(lambda s: s["assets"][1].update(id="A1")), "duplicate_id", ".assets[1].id"
    )


def test_duplicate_normalized_names_are_rejected():
    """匹配按归一化名进行，重名会让程序无法确定性匹配 -> 必须拦截。"""
    error = _expect_error(
        _broken(lambda s: s["assets"][1].update(name="林 澈")), "duplicate_normalized_name"
    )
    assert "林澈" in str(error)


def test_duplicate_chapter_and_segment_ids_are_rejected():
    _expect_error(
        _broken(lambda s: s["chapters"].append(copy.deepcopy(s["chapters"][0]))),
        "duplicate_id",
    )
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][1].update(segment_index=1)),
        "duplicate_id",
        "segment_index",
    )


def test_invalid_references_are_rejected():
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0]["asset_refs"].append("不存在的人物")),
        "invalid_reference",
        "asset_refs[2]",
    )
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].update(first_frame_ref="幽灵素材")),
        "invalid_reference",
        "first_frame_ref",
    )


def test_format_errors_are_rejected():
    _expect_error(_broken(lambda s: s["chapters"][0].update(chapter_id="CH1")), "invalid_format")
    _expect_error(_broken(lambda s: s["chapters"][0].update(chapter_id="ch001")), "invalid_format")
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].update(segment_index="1")),
        "invalid_format",
    )
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].update(segment_index=0)),
        "invalid_format",
    )
    _expect_error(
        _broken(lambda s: s["chapters"][0]["segments"][0].update(duration_s=-3)),
        "invalid_format",
    )
    _expect_error(_broken(lambda s: s["assets"][0].update(asset_type="FOO")), "invalid_enum")
    _expect_error(_broken(lambda s: s["assets"][0].update(tags="不是数组")), "invalid_type")
    _expect_error(_broken(lambda s: s["assets"][0].update(prompt=123)), "invalid_type")
    _expect_error(_broken(lambda s: s["chapters"][0].update(segments="不是数组")), "invalid_type")
    _expect_error(_broken(lambda s: s.update(assets="不是数组")), "invalid_type")


def test_control_characters_rejected():
    _expect_error(
        _broken(lambda s: s["assets"][0].update(prompt="提示词\x00被截断")), "invalid_format"
    )


def test_validate_rejects_empty_and_non_dict():
    with pytest.raises(ScriptValidationError) as excinfo:
        validate_script({})
    assert "empty_script" in excinfo.value.codes
    with pytest.raises(ScriptValidationError) as excinfo:
        validate_script(["not", "a", "dict"])  # type: ignore[arg-type]
    assert "invalid_type" in excinfo.value.codes


def test_multiple_errors_are_all_reported_and_nothing_is_returned():
    """一次报告全部错误，但绝不返回部分结果。"""

    def break_many(s: dict) -> None:
        s["assets"][0].pop("prompt")
        s["assets"][1].update(asset_type="WRONG")
        s["chapters"][0]["segments"][0]["asset_refs"].append("幽灵")
        s["chapters"][0].update(chapter_id="BAD")

    script = _broken(break_many)
    error = _expect_error(script, "missing_field")
    codes = set(error.codes)
    assert {"missing_field", "invalid_enum", "invalid_reference", "invalid_format"} <= codes
    assert len(error.errors) >= 4
    assert str(error).startswith("剧本校验失败")


# ---------------------------------------------------------------------------
# 分隔符文本
# ---------------------------------------------------------------------------


DELIMITED_SAMPLE = """好的，以下是拆解结果：

=== 角色 ===
id: A1
name: 林澈
prompt: 少年剑客，白衣，长发束起
=== 场景 ===
id: A2
name: 雪夜长街
prompt: 大雪，青石板路，灯笼

=== CHAPTER ===
chapter_id: CH001
title: 雪夜
=== SEGMENT ===
segment_index: 1
prompt: 雪夜长街，林澈独行
asset_refs: A1, A2
duration_s: 10
"""


def test_delimited_text_parses_and_validates():
    parsed = parse_script_output(DELIMITED_SAMPLE, output_format="delimited")
    result = validate_script(parsed)
    assert [a["asset_type"] for a in result["assets"]] == ["CHARACTER", "SCENE"]
    segment = result["chapters"][0]["segments"][0]
    assert segment["asset_refs"] == ["A1", "A2"]
    assert segment["duration_s"] == 10


def test_delimited_missing_required_field_is_rejected_not_patched():
    text = "=== 角色 ===\nname: 林澈\nprompt: 少年\n=== CHAPTER ===\nprompt: 走\n"
    parsed = parse_script_output(text, output_format="delimited")
    with pytest.raises(ScriptValidationError) as excinfo:
        validate_script(parsed)
    assert {"missing_field", "empty_field"} & set(excinfo.value.codes)


def test_auto_format_prefers_json_then_delimited():
    parsed, report = parse_with_report(json.dumps(valid_script(), ensure_ascii=False), output_format="auto")
    assert report.method == "json"
    parsed2, report2 = parse_with_report(DELIMITED_SAMPLE, output_format="auto")
    assert report2.method == "delimited"
    assert report2.warnings == () or isinstance(report2.warnings, tuple)
    assert isinstance(report, ParseReport)
    assert report.as_dict()["method"] == "json"


def test_fenced_json_is_extracted():
    raw = "前言\n```json\n" + json.dumps(valid_script(), ensure_ascii=False) + "\n```\n后记"
    parsed, report = parse_with_report(raw, output_format="json")
    assert report.method == "json"
    assert validate_script(parsed)["assets"][0]["name"] == "林澈"


def test_delimited_without_structure_is_rejected():
    with pytest.raises(ScriptValidationError) as excinfo:
        parse_script_output("随便一段没有任何字段的文字", output_format="delimited")
    assert "no_recognizable_structure" in excinfo.value.codes


def test_script_validation_error_requires_errors():
    with pytest.raises(ValueError):
        ScriptValidationError([])
