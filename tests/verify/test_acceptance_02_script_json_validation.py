"""验收 2：有效剧本得到结构化数据；无效 JSON 被拦截，不启动生成。

原文（任务书第 9 节第 2 条，第 184 行）：
    「有效剧本得到结构化数据；无效 JSON 被拦截，不启动生成。」

需要模块：bettercomfyui.prompt.validate（parse_script_output / validate_script /
          ScriptValidationError）
依据：CONTRACTS.md 第 5 节；任务书第 9 节验收 2。

剧本规范结构（以 `validate_script` 实际接受的规范输出为准，已实测）：
    {"title": str?, "assets": [{"id","asset_type","name","prompt"}],
     "chapters": [{"id","title"?, "segments":[{"id","prompt","asset_refs":[...]}]}]}

判定通过：
    P1 合法剧本 -> parse + validate 通过，返回可 JSON 往返的结构化数据；
    P2 非法 JSON 语法 / 缺字段 / 重复 ID / 引用不存在的素材 -> 全部抛 ScriptValidationError；
    P3 失败时携带非空 errors=[{path,code,message}]，**不返回部分结果**；
    P4 「不启动生成」：校验失败后项目目录字节级未变（没有任何版本被创建）。

判定假通过（必须能变红）：
    F1 异常消息里说失败、却仍返回部分 dict —— 断言 with pytest.raises 且调用点无返回值；
    F2 校验函数对任意输入都通过 —— 8 条非法样例（已实测均被拦截）会变红；
    F3 except: pass 吞掉解析错误后返回默认结构 —— 断言异常类型与 errors 内容。
"""

from __future__ import annotations

import copy
import json

import pytest

from _api import create_project
from _harness import assert_tree_unchanged, contract_attr, load, make_config, tree_digest

VALID_SCRIPT = {
    "title": "第一章",
    "assets": [
        {"id": "CHAR_01", "asset_type": "CHARACTER", "name": "林夕", "prompt": "黑长直发少女，蓝色校服"},
        {"id": "CHAR_02", "asset_type": "CHARACTER", "name": "陈默", "prompt": "短发少年，白色衬衫"},
        {"id": "SCENE_01", "asset_type": "SCENE", "name": "教室", "prompt": "黄昏的教室"},
    ],
    "chapters": [
        {
            "id": "CH001",
            "title": "第一章",
            "segments": [
                {"id": "SEG1", "prompt": "林夕走进教室", "asset_refs": ["CHAR_01", "SCENE_01"]},
                {"id": "SEG2", "prompt": "陈默回头看向林夕", "asset_refs": ["CHAR_02"]},
            ],
        }
    ],
}


def _validate_mod():
    return load("bettercomfyui.prompt.validate")


def _mut(**overrides):
    d = copy.deepcopy(VALID_SCRIPT)
    d.update(overrides)
    return d


def test_valid_script_becomes_structured_data():
    """P1：合法剧本 -> 结构化数据（可 JSON 往返）。"""
    v = _validate_mod()
    raw = json.dumps(VALID_SCRIPT, ensure_ascii=False)
    parsed = contract_attr(v, "parse_script_output")(raw)
    assert isinstance(parsed, dict), f"parse_script_output 必须返回 dict，实际 {type(parsed)!r}"

    result = contract_attr(v, "validate_script")(parsed)
    assert isinstance(result, dict), "validate_script 必须返回结构化结果"
    round_trip = json.loads(json.dumps(result, ensure_ascii=False))
    assert round_trip == json.loads(json.dumps(result, ensure_ascii=False)), "结果必须可 JSON 往返"
    assert result.get("assets"), "结构化结果必须保留素材清单"
    assert result.get("chapters"), "结构化结果必须保留章节/分段"


@pytest.mark.parametrize(
    "label,raw",
    [
        ("非法JSON语法", '{"assets": [{"id": "CHAR_01",]'),
        ("非JSON文本", "这只是一段普通描述，没有任何结构"),
        ("JSON但是数组", "[1, 2, 3]"),
        ("空字符串", ""),
        ("数值而非对象", "42"),
    ],
)
def test_invalid_json_is_rejected(label: str, raw: str):
    """P2：解析层必须拦截并抛 ScriptValidationError，绝不返回部分结果。"""
    v = _validate_mod()
    err = contract_attr(v, "ScriptValidationError", kind="class")
    with pytest.raises(err) as ei:
        out = contract_attr(v, "parse_script_output")(raw)
        raise AssertionError(f"{label}：parse_script_output 竟然返回了 {out!r}，无效输入未被拦截")
    errors = getattr(ei.value, "errors", None)
    assert isinstance(errors, list) and errors, (
        f"{label}：ScriptValidationError 必须携带非空 errors=[{{path,code,message}}]，实际 {errors!r}"
    )
    for e in errors:
        assert isinstance(e, dict) and {"path", "code", "message"} <= set(e), (
            f"{label}：errors 元素必须含 path/code/message，实际 {e!r}"
        )


@pytest.mark.parametrize(
    "label,script",
    [
        ("缺assets字段", {k: v for k, v in VALID_SCRIPT.items() if k != "assets"}),
        ("缺chapters字段", {k: v for k, v in VALID_SCRIPT.items() if k != "chapters"}),
        ("素材缺id", _mut(assets=[{"asset_type": "CHARACTER", "name": "林夕"}])),
        ("重复素材ID", _mut(assets=[VALID_SCRIPT["assets"][0], VALID_SCRIPT["assets"][0]])),
        (
            "引用不存在的素材",
            _mut(
                chapters=[
                    {
                        "id": "CH001",
                        "segments": [{"id": "SEG1", "prompt": "x", "asset_refs": ["CHAR_99"]}],
                    }
                ]
            ),
        ),
        ("assets类型错误", _mut(assets="不是列表")),
        ("章节缺segments", _mut(chapters=[{"id": "CH001", "title": "c"}])),
        ("空对象", {}),
    ],
)
def test_structural_violations_are_rejected(label: str, script):
    """P2：结构校验必须拦截缺字段/重复 ID/无效引用/类型错误（8 条均已实测被拦截）。"""
    v = _validate_mod()
    err = contract_attr(v, "ScriptValidationError", kind="class")
    with pytest.raises(err) as ei:
        out = contract_attr(v, "validate_script")(copy.deepcopy(script))
        raise AssertionError(f"{label}：validate_script 竟然通过，返回 {out!r}")
    assert getattr(ei.value, "errors", None), f"{label}：必须给出具体 errors"


def test_invalid_script_does_not_start_generation(tmp_path):
    """P3/P4：校验失败后素材库文件树必须字节级未变（没有任何生成/入库副作用）。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "验证项目", cfg)

    before = tree_digest(ha.root)

    v = _validate_mod()
    err = contract_attr(v, "ScriptValidationError", kind="class")
    bad_raw = '{"assets": [{"id": "CHAR_01"},'
    try:
        parsed = contract_attr(v, "parse_script_output")(bad_raw)
        contract_attr(v, "validate_script")(parsed)
    except err:
        pass  # 期望路径
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"无效输入必须抛 ScriptValidationError，实际抛 {type(exc).__name__}: {exc}"
        ) from exc
    else:
        raise AssertionError("无效 JSON 未被拦截（既没抛 ScriptValidationError 也没报错）")

    after = tree_digest(ha.root)
    assert_tree_unchanged(before, after, what="无效剧本被拦截后，项目目录（不得产生任何素材版本）")


def test_validation_error_is_not_vacuous():
    """F2 反向自检：合法样例必须能通过，否则「非法样例抛错」的断言没有鉴别力。"""
    v = _validate_mod()
    contract_attr(v, "ScriptValidationError", kind="class")
    parsed = contract_attr(v, "parse_script_output")(json.dumps(VALID_SCRIPT, ensure_ascii=False))
    contract_attr(v, "validate_script")(parsed)  # 不得抛错
