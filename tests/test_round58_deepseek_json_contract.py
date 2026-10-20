"""ROUND58：DeepSeek 系统提示词 ↔ JSON 解析器的输出契约测试。

覆盖用户报障的三类真实输出：

1. 纯 JSON；
2. ```json fenced``` 代码围栏；
3. JSON 前后带说明文字。

同时锁定 ROUND58 的契约修复：

* 系统提示词里的【输出契约】优先于 H3 手册的 JSON 结构；
* 配置里的 ``zh`` / ``shot`` 会翻译成「中文 / 按镜头」再进提示词；
* ``config/default.yaml`` 的 custom provider 配置能被 ``build_gateway`` 直接使用；
* 解析器兼容 H3 手册结构、外层封装、中文键、双重编码、旧 ``type`` 形状；
* 系统提示词允许引用既有素材清单名时，解析器不再把这类引用丢掉；
* 兼容但**不吞错**：缺 ``video_prompt``、未知顶层结构都会给出可操作错误码。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from bettercomfyui.config import load_config
from bettercomfyui.prompt.gateway import CustomGateway, build_gateway
from bettercomfyui.prompt.inject import (
    OUTPUT_CONTRACT_VERSION,
    build_system_prompt,
    build_user_prompt,
    parse_manifest_asset_names,
)
from bettercomfyui.prompt.script_build import generate_script_prompts
from bettercomfyui.prompt.validate import (
    ScriptValidationError,
    adapt_script_output,
    parse_with_report,
    validate_script,
)

REPO_ROOT = _ROOT
FIXTURES = _HERE / "fixtures"
SCRIPT = "第一章 雪夜\n林澈独行于雪夜长街，忽闻身后剑鸣。"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeGateway:
    """测试替身：不发真实网络请求，返回预置文本。"""

    provider = "fake"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, **params):
        self.calls.append({"system": system, "user": user, "params": params})
        return self.text

    def describe(self) -> dict:
        return {"provider": "fake", "model": "deepseek-chat", "url": ""}


class _FakeTransport:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post_json(self, *, url, headers, payload, timeout_s):
        self.calls.append({"url": url, "headers": dict(headers), "payload": payload})
        return self.response


@pytest.fixture(scope="module")
def cfg():
    return load_config(repo_root=REPO_ROOT)


# ---------------------------------------------------------------------------
# 系统提示词：契约必须写得足够明确
# ---------------------------------------------------------------------------


def test_system_prompt_contains_machine_actionable_contract(cfg):
    system = build_system_prompt(cfg=cfg, manifest="")
    assert OUTPUT_CONTRACT_VERSION in system
    assert "本段优先于上文任何 JSON 格式要求" in system
    assert '"asset_type"' in system
    assert "video_prompt" in system and "material_image_prompts" in system
    assert "视频提示词必须写进 chapters[].segments[].prompt" in system
    assert "禁止使用 H3 手册的顶层结构" in system
    assert "json.loads" in system
    # 配置里是 zh/shot，提示词里必须是中文标签（否则模型只看到半截指令）
    assert "【输出语言】中文" in system
    assert "zh" not in system.split("【输出语言】")[1][:10]


def test_user_prompt_translates_language_and_granularity_codes(cfg):
    user = build_user_prompt(script_text=SCRIPT, cfg=cfg)
    assert "输出语言：中文" in user
    assert "分镜粒度：按镜头" in user
    assert "输出格式：JSON" in user


def test_contract_wins_over_handbook_style_template():
    handbook_like = (
        '【输出格式】严格输出 JSON：\n'
        '{"task_type": "T2VA", "video_prompt": "...", '
        '"material_image_prompts": [], "character_sheet": []}'
    )
    system = build_system_prompt(
        cfg={"prompt": {"output_format": "json", "system_prompt_template": handbook_like}},
        manifest="",
    )
    assert "task_type" in system
    assert system.index(OUTPUT_CONTRACT_VERSION) > system.index("task_type"), (
        "输出契约必须追加在手册式模板之后（后出现的约束才会覆盖前面的格式要求）"
    )


#: 推荐的 custom provider 配置（与 Config()/config/default.yaml 当前默认值一致）。
#: 显式复制一份，便于测试直接构造配置，不依赖 YAML 合并结果；
#: `test_default_config_carries_the_recommended_custom_provider_values` 负责防漂移。
RECOMMENDED_CUSTOM_PROMPT = {
    "model_provider": "custom",
    "endpoint": "http://127.0.0.1:9999/v1",
    "model_name": "deepseek-chat",
    "api_key": "test-key",
    "custom_request_template": {
        "model": "{model}",
        "messages": [
            {"role": "system", "content": "{system}"},
            {"role": "user", "content": "{user}"},
        ],
        "stream": False,
    },
    "custom_response_path": "choices.0.message.content",
}


# ---------------------------------------------------------------------------
# 配置：custom provider 的 JSON 结构必须真的能被 gateway 使用
# ---------------------------------------------------------------------------


def test_recommended_custom_provider_config_is_gateway_compatible():
    template = RECOMMENDED_CUSTOM_PROMPT["custom_request_template"]
    assert isinstance(template, dict)
    assert template["messages"][0]["content"] == "{system}"
    assert template["messages"][1]["content"] == "{user}"

    transport = _FakeTransport(
        {"choices": [{"message": {"content": '{"chapters": [], "assets": []}'}}]}
    )
    gateway = build_gateway(
        {"prompt": dict(RECOMMENDED_CUSTOM_PROMPT)},
        transport=transport,
        sleep=lambda _: None,
    )

    assert isinstance(gateway, CustomGateway)
    text = gateway.complete(system="SYS", user="USR")
    assert text == '{"chapters": [], "assets": []}'
    assert transport.calls[0]["url"] == "http://127.0.0.1:9999/v1/chat/completions"
    payload = transport.calls[0]["payload"]
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]


def test_default_config_carries_the_recommended_custom_provider_values(cfg):
    """默认配置与推荐 custom provider 配置保持一致（防止 YAML/实体默认值再漂移）。"""
    prompt = dataclasses.asdict(cfg.prompt)
    assert prompt["custom_response_path"] == RECOMMENDED_CUSTOM_PROMPT["custom_response_path"]
    assert prompt["custom_request_template"] == RECOMMENDED_CUSTOM_PROMPT["custom_request_template"]
    assert prompt["max_tokens"] == 8192


def test_custom_request_template_can_be_a_json_string():
    """``custom_request_template`` 写成一整段 JSON 字符串时也要能用。"""
    prompt = {
        "model_provider": "custom",
        "endpoint": "http://127.0.0.1:9999",
        "model_name": "m",
        "custom_request_template": (
            '{"model": "{model}", "prompt": "{system}\\n{user}", '
            '"max_tokens": "{max_tokens}"}'
        ),
        "custom_response_path": "data",
        "max_tokens": 1234,
    }
    transport = _FakeTransport({"data": {"content": "hello"}})
    gateway = build_gateway({"prompt": prompt}, transport=transport, sleep=lambda _: None)
    assert gateway.complete(system="S", user="U") == "hello"
    payload = transport.calls[0]["payload"]
    assert payload["prompt"] == "S\nU"
    assert payload["max_tokens"] == "1234"


# ---------------------------------------------------------------------------
# 三类真实输出：纯 JSON / 代码围栏 / 前后说明文字
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["r58_pure.json", "r58_fenced.txt", "r58_prose.txt"],
)
def test_three_real_output_shapes_parse_and_validate(name):
    parsed, report = parse_with_report(_fixture(name), output_format="json")
    assert report.method == "json"

    script, warnings, shape = adapt_script_output(parsed)
    validated = validate_script(script)
    assert shape == "script"
    assert [asset["asset_type"] for asset in validated["assets"]] == ["CHARACTER", "SCENE"]
    segment = validated["chapters"][0]["segments"][0]
    assert segment["asset_refs"] == ["A1", "A2"]
    assert "overall_soundscape" in segment["prompt"]
    assert warnings == ()


@pytest.mark.parametrize(
    "name",
    ["r58_pure.json", "r58_fenced.txt", "r58_prose.txt"],
)
def test_three_real_output_shapes_work_end_to_end(cfg, name):
    gateway = _FakeGateway(_fixture(name))
    result = generate_script_prompts(
        cfg=cfg, script_text=SCRIPT, gateway=gateway, output_format="json"
    )
    assert result.api_calls == 1
    assert result.meta["output_shape"] == "script"
    assert len(result.assets_by_type["CHARACTER"]) == 1
    assert len(result.assets_by_type["SCENE"]) == 1
    assert len(result.video_prompts) == 1
    assert "A1" in result.script["chapters"][0]["segments"][0]["asset_refs"]


# ---------------------------------------------------------------------------
# H3 手册形状：能识别（转成标准结构），但转换必须留痕
# ---------------------------------------------------------------------------


def test_handbook_shape_is_converted_with_visible_warnings(cfg):
    parsed, _report = parse_with_report(_fixture("r58_handbook.json"), output_format="json")
    script, warnings, shape = adapt_script_output(parsed)
    validated = validate_script(script)

    assert shape == "handbook"
    assert [asset["asset_type"] for asset in validated["assets"]] == ["CHARACTER", "SCENE"]
    assert validated["chapters"][0]["segments"][0]["prompt"].startswith(
        "integrated_multimodal_description:"
    )
    assert any("H3 手册结构" in warning for warning in warnings)
    assert any("character_sheet" in warning for warning in warnings)

    result = generate_script_prompts(
        cfg=cfg,
        script_text=SCRIPT,
        gateway=_FakeGateway(_fixture("r58_handbook.json")),
        output_format="json",
    )
    assert result.meta["output_shape"] == "handbook"
    assert result.meta["notes"], "转换说明必须写进 notes，不能静默"
    assert len(result.assets_by_type["CHARACTER"]) == 1
    assert len(result.assets_by_type["SCENE"]) == 1
    assert len(result.video_prompts) == 1


def test_handbook_without_video_prompt_fails_with_actionable_code():
    with pytest.raises(ScriptValidationError) as excinfo:
        adapt_script_output(
            {"task_type": "T2VA", "material_image_prompts": [{"type": "人物图", "prompt": "x"}]}
        )
    assert "handbook_missing_video_prompt" in excinfo.value.codes
    assert "video_prompt" in str(excinfo.value)


def test_unknown_top_level_shape_lists_keys():
    with pytest.raises(ScriptValidationError) as excinfo:
        adapt_script_output({"foo": 1, "bar": 2})
    assert "unrecognized_output_shape" in excinfo.value.codes
    assert "foo" in str(excinfo.value) and "assets" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 其他真实变体：外层封装 / 中文键 / 双重编码 / 旧形状 / H3 三字段
# ---------------------------------------------------------------------------


def test_envelope_and_chinese_keys_are_adapted():
    raw = json.dumps(
        {
            "code": 0,
            "msg": "ok",
            "data": {
                "标题": "雪夜",
                "素材": [
                    {"编号": "A1", "类型": "角色", "名称": "林澈", "提示词": "p1"},
                    {"编号": "A2", "类型": "场景", "名称": "雪夜长街", "提示词": "p2"},
                ],
                "章节": [
                    {
                        "章节id": "CH001",
                        "分镜": [
                            {
                                "段号": 1,
                                "提示词": "独自走在雪夜长街",
                                "素材引用": "A1, A2",
                            }
                        ],
                    }
                ],
            },
        },
        ensure_ascii=False,
    )
    parsed, _report = parse_with_report(raw, output_format="json")
    script, warnings, shape = adapt_script_output(parsed)
    validated = validate_script(script)

    assert shape == "script"
    assert validated["title"] == "雪夜"
    assert [asset["asset_type"] for asset in validated["assets"]] == ["CHARACTER", "SCENE"]
    assert validated["chapters"][0]["segments"][0]["asset_refs"] == ["A1", "A2"]
    assert any("外层封装" in warning for warning in warnings)
    assert any("别名" in warning for warning in warnings)


def test_double_encoded_json_string_is_unwrapped():
    raw = json.dumps(_fixture("r58_pure.json"), ensure_ascii=False)
    parsed, report = parse_with_report(raw, output_format="json")
    assert report.extracted_from == "double_encoded"
    script, _warnings, shape = adapt_script_output(parsed)
    assert shape == "script"
    assert len(validate_script(script)["assets"]) == 2


def test_legacy_type_and_chapter_level_assets_still_work():
    legacy = {
        "title": "雪夜",
        "chapters": [
            {
                "chapter_id": "CH001",
                "assets": [
                    {"id": "A1", "type": "角色", "name": "林澈", "prompt": "p1"},
                    {"id": "A2", "type": "场景", "name": "雪夜长街", "prompt": "p2"},
                ],
                "segments": [
                    {"segment_index": 1, "prompt": "v", "asset_refs": ["A1", "A2"]}
                ],
            }
        ],
    }
    script, warnings, shape = adapt_script_output(legacy)
    validated = validate_script(script)

    assert shape == "legacy"
    assert [asset["asset_type"] for asset in validated["assets"]] == ["CHARACTER", "SCENE"]
    assert "assets" not in validated["chapters"][0], "章节级副本必须被移除，避免重复处理"
    assert any("合并到顶层" in warning for warning in warnings)


def test_h3_three_fields_are_merged_into_segment_prompt():
    script = {
        "title": "雪夜",
        "assets": [],
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {
                        "segment_index": 1,
                        "integrated_multimodal_description": "[Shot 1] A.",
                        "overall_soundscape": "Wind.",
                        "non_diegetic_music": "Strings.",
                        "asset_refs": [],
                    }
                ],
            }
        ],
    }
    out, warnings, _shape = adapt_script_output(script)
    prompt = out["chapters"][0]["segments"][0]["prompt"]
    assert "integrated_multimodal_description: [Shot 1] A." in prompt
    assert "overall_soundscape: Wind." in prompt
    assert "non_diegetic_music: Strings." in prompt
    assert any("H3 三字段" in warning for warning in warnings)
    validate_script(out)


def test_missing_asset_refs_defaults_to_empty_with_warning():
    script = {
        "title": "雪夜",
        "assets": [],
        "chapters": [{"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "v"}]}],
    }
    out, warnings, _shape = adapt_script_output(script)
    assert out["chapters"][0]["segments"][0]["asset_refs"] == []
    assert any("asset_refs" in warning for warning in warnings)
    validate_script(out)


# ---------------------------------------------------------------------------
# 提示词说「可引用既有素材名」→ 解析器必须允许，而不是静默丢弃
# ---------------------------------------------------------------------------


def test_manifest_asset_names_are_allowed_in_asset_refs(cfg):
    manifest = (
        "[已有素材清单 | 由程序从素材库读取，是权威来源，未列出即为不存在 | "
        "列：类型 / 序号 / 名称 / 锁定状态]\n"
        "CHARACTER\t01\t苏晚\tLOCKED v1 MODEL_PASS\n"
        "SCENE\t01\t客栈大堂\tUNLOCKED"
    )
    raw = json.dumps(
        {
            "title": "雪夜",
            "assets": [
                {"id": "A1", "asset_type": "CHARACTER", "name": "林澈", "prompt": "p1"}
            ],
            "chapters": [
                {
                    "chapter_id": "CH001",
                    "segments": [
                        {
                            "segment_index": 1,
                            "prompt": "v",
                            "asset_refs": ["A1", "苏晚"],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    result = generate_script_prompts(
        cfg=cfg,
        script_text=SCRIPT,
        manifest=manifest,
        gateway=_FakeGateway(raw),
        output_format="json",
    )
    segment = result.script["chapters"][0]["segments"][0]
    assert segment["asset_refs"] == ["A1", "苏晚"], (
        "系统提示词允许引用清单里的既有素材名，解析器不得把它当无效引用丢掉"
    )
    assert result.meta["existing_asset_names"] == 2
    assert "dropped_invalid_refs" not in result.meta


def test_parse_manifest_asset_names_handles_store_and_fallback_formats():
    tabbed = (
        "[已有素材清单]\n"
        "CHARACTER\t01\t苏晚\tLOCKED v1 MODEL_PASS\n"
        "SCENE\t01\t客栈大堂\tUNLOCKED"
    )
    assert parse_manifest_asset_names(tabbed) == ["苏晚", "客栈大堂"]

    fallback = (
        "CHARACTER  #1  林澈        v1  LOCKED (MODEL_PASS)\n"
        "SCENE  #2  雪夜长街    v1  UNLOCKED"
    )
    assert parse_manifest_asset_names(fallback) == ["林澈", "雪夜长街"]

    assert parse_manifest_asset_names("") == []
    assert parse_manifest_asset_names(tabbed + "\nCHARACTER\t02\t苏 晚\tUNLOCKED") == [
        "苏晚",
        "客栈大堂",
    ]


# ---------------------------------------------------------------------------
# 兼容层不改变严格校验：非法字段仍然报出精确 path
# ---------------------------------------------------------------------------


def test_adapter_still_reports_precise_field_errors():
    script = {
        "title": "雪夜",
        "assets": [{"id": "A1", "asset_type": "CHARACTER", "name": "林澈", "prompt": "p1"}],
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [{"segment_index": 1, "prompt": "", "asset_refs": ["A1"]}],
            }
        ],
    }
    out, _warnings, _shape = adapt_script_output(script)
    with pytest.raises(ScriptValidationError) as excinfo:
        validate_script(out)
    assert any("segments[0].prompt" in error["path"] for error in excinfo.value.errors)
