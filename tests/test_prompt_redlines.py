"""prompt 层契约红线与静态检查（不依赖任何业务行为）。

检查项：
* 依赖方向：``prompt`` 不得 import ``review`` / ``director`` / ``pipeline``；
* ``prompt`` 对 ``store`` 的依赖必须是**函数内延迟导入**（store 未就绪也能 import prompt）；
* 对外签名与契约 §5 逐项一致（含 ``MatchResult`` 字段顺序、``GatewayError.error_class``）；
* 不出现被禁止的跨层内容：重复枚举定义、硬编码绝对路径、自己写文件/日志；
* import 冒烟：所有子模块可导入。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

PROMPT_DIR = _ROOT / "bettercomfyui" / "prompt"
PROMPT_FILES = sorted(PROMPT_DIR.glob("*.py"))
FORBIDDEN_LAYERS = ("review", "director", "pipeline", "executors")

MODULES = [
    "bettercomfyui.prompt",
    "bettercomfyui.prompt.errors",
    "bettercomfyui.prompt._conf",
    "bettercomfyui.prompt.normalize",
    "bettercomfyui.prompt.validate",
    "bettercomfyui.prompt.gateway",
    "bettercomfyui.prompt.match",
    "bettercomfyui.prompt.inject",
]


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _top_level_imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("module", MODULES)
def test_import_smoke(module):
    assert importlib.import_module(module) is not None


def test_no_prompt_file_is_empty_and_all_have_docstrings():
    for path in PROMPT_FILES:
        tree = _tree(path)
        assert ast.get_docstring(tree), f"{path.name} 缺少模块 docstring"
        assert path.stat().st_size > 200, f"{path.name} 内容过少"


def test_dependency_direction_is_one_way():
    for path in PROMPT_FILES:
        source = path.read_text(encoding="utf-8")
        for layer in FORBIDDEN_LAYERS:
            assert f"..{layer}" not in source, f"{path.name} 违反依赖方向：{layer}"
            assert f"bettercomfyui.{layer}" not in source, f"{path.name} 违反依赖方向：{layer}"


def test_store_dependency_is_lazy():
    """store 必须延迟导入：match/inject 在函数内部 import，而不是模块顶层。"""
    for name in ("match.py", "inject.py"):
        tree = _tree(PROMPT_DIR / name)
        imports = _top_level_imports(tree)
        assert not any("store" in item for item in imports), (
            f"{name} 顶层不得 import store（应为函数内延迟导入）：{imports}"
        )


def test_no_duplicate_enum_definitions():
    for path in PROMPT_FILES:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {ast.unparse(base) for base in node.bases}
                assert not (bases & {"Enum", "_StrEnum", "IntEnum", "StrEnum"}), (
                    f"{path.name} 重复定义了枚举 {node.name}；枚举真源只能是 models/enums.py"
                )


def test_no_absolute_paths_hardcoded():
    for path in PROMPT_FILES:
        source = path.read_text(encoding="utf-8")
        for bad in ("C:\\", "H:\\", "/Users/", "/home/", "D:\\"):
            assert bad not in source, f"{path.name} 出现硬编码绝对路径：{bad}"


def test_prompt_layer_never_writes_files_or_logs_directly():
    """写文件/写日志都属于 store 的职责；prompt 层不得自建第二份真源。"""
    import re

    for path in PROMPT_FILES:
        source = path.read_text(encoding="utf-8")
        assert re.search(r"(?<!url)\bopen\(", source) is None, f"{path.name} 出现 open() 写/读文件"
        for banned in ("write_text(", "json.dump(", "events.jsonl", "logging.FileHandler"):
            assert banned not in source, f"{path.name} 出现禁止的文件/日志写入：{banned}"


def test_contract_signatures():
    from bettercomfyui.prompt import gateway, inject, match, normalize, validate

    assert list(inspect.signature(normalize.normalize_name).parameters) == ["name"]
    suspect_params = list(inspect.signature(normalize.is_suspect).parameters)
    assert suspect_params[:2] == ["candidate", "existing"]

    parse_params = inspect.signature(validate.parse_script_output).parameters
    assert list(parse_params)[:2] == ["raw", "output_format"]
    assert parse_params["output_format"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parse_params["output_format"].default == "json"

    assert list(inspect.signature(validate.validate_script).parameters)[0] == "parsed"

    match_params = inspect.signature(match.match_candidate).parameters
    assert list(match_params)[:2] == ["candidate", "store"]
    assert list(inspect.signature(match.build_suspect_queue).parameters)[0] == "store"

    gateway_params = inspect.signature(gateway.build_gateway).parameters
    assert list(gateway_params)[0] == "cfg"

    assert list(inspect.signature(inject.build_asset_manifest).parameters)[0] == "store"
    assert list(inspect.signature(inject.build_prompt_messages).parameters)[0] == "script_text"


def test_public_exports_match_contract():
    import bettercomfyui.prompt as prompt

    expected = {
        "normalize_name",
        "is_suspect",
        "parse_script_output",
        "validate_script",
        "ScriptValidationError",
        "match_candidate",
        "build_suspect_queue",
        "MatchResult",
        "build_gateway",
        "GatewayError",
        "PromptModelGateway",
        "build_asset_manifest",
        "build_prompt_messages",
    }
    assert expected <= set(prompt.__all__)
    for name in expected:
        assert hasattr(prompt, name), name


def test_match_outcome_comes_from_models_enums():
    from bettercomfyui.models.enums import MatchOutcome
    from bettercomfyui.prompt.match import MatchResult

    assert MatchResult._fields == ("outcome", "asset_id", "version", "suspect_id", "reason")
    assert set(MatchOutcome) == {
        MatchOutcome.EXISTS_REUSE,
        MatchOutcome.NEW_GENERATE,
        MatchOutcome.SUSPECT_DUPLICATE,
    }


def test_gateway_error_requires_error_class():
    from bettercomfyui.models.enums import ErrorClass
    from bettercomfyui.prompt.gateway import GatewayError

    error = GatewayError("boom")
    assert isinstance(error.error_class, ErrorClass)
    assert error.missing_keys == []
    customized = GatewayError("boom", error_class=ErrorClass.MODEL_API, missing_keys=["x"])
    assert customized.error_class is ErrorClass.MODEL_API
    assert "x" in str(customized)


def test_script_validation_error_carries_structured_errors():
    from bettercomfyui.prompt.validate import ScriptValidationError

    error = ScriptValidationError([{"path": "$.a", "code": "c", "message": "m"}])
    assert error.errors == [{"path": "$.a", "code": "c", "message": "m"}]
    assert "c" in error.codes
    with pytest.raises(ValueError):
        ScriptValidationError([])


def test_prompt_package_import_does_not_require_store(monkeypatch):
    """store 不可用时 prompt 仍可导入（store 是延迟导入）。"""
    monkeypatch.setitem(sys.modules, "bettercomfyui.store", None)
    monkeypatch.setitem(sys.modules, "bettercomfyui.store.inject", None)
    for module in ("bettercomfyui.prompt.match", "bettercomfyui.prompt.inject"):
        sys.modules.pop(module, None)
        assert importlib.import_module(module) is not None
