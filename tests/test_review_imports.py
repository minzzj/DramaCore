"""导入冒烟 + 依赖方向静态检查（Lead 明确要求：每个模块都必须能被直接 import）。"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

REVIEW_MODULES = [
    "arbiter",
    "asset_review",
    "commit",
    "config",
    "counters",
    "errors",
    "events",
    "notify",
    "state",
    "_bridge",
]

FORBIDDEN_TOP_LEVEL = {"prompt", "director", "pipeline", "executors"}


@pytest.mark.parametrize("name", REVIEW_MODULES)
def test_module_importable(name):
    module = importlib.import_module(f"bettercomfyui.review.{name}")
    assert module is not None


def test_package_exports_contract_names():
    from bettercomfyui import review

    for name in ("ReviewCycle", "CounterStore", "AssetReviewer", "arbitrate", "commit_final"):
        assert hasattr(review, name), f"review 包必须导出 CONTRACTS §6 的 {name}"


def _review_dir() -> Path:
    package = importlib.import_module("bettercomfyui.review")
    return Path(package.__file__).resolve().parent


def test_no_forbidden_cross_layer_imports():
    """review 依赖 store/models；不得 import prompt/director/pipeline/executors。"""
    offenders: list[str] = []
    for path in sorted(_review_dir().glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[:1] == ["bettercomfyui"] and len(parts) > 1 and parts[1] in FORBIDDEN_TOP_LEVEL:
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0:
                    parts = module.split(".")
                    if parts[:1] == ["bettercomfyui"] and len(parts) > 1 and parts[1] in FORBIDDEN_TOP_LEVEL:
                        offenders.append(f"{path.name}:{node.lineno} from {module}")
                elif node.level == 2:
                    # `from ..prompt import ...` 之类的越层引用
                    if module.split(".")[0] in FORBIDDEN_TOP_LEVEL:
                        offenders.append(f"{path.name}:{node.lineno} from ..{module}")
    assert not offenders, "review 层出现被禁止的跨层 import：\n" + "\n".join(offenders)


def test_review_never_writes_meta_json_directly():
    """R2/§3：review 层不得自己读写 meta.json / 不得直接 json.dump 覆盖文件。"""
    offenders: list[str] = []
    for path in sorted(_review_dir().glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "meta.json" in text:
            offenders.append(f"{path.name}: 出现 meta.json 字面量（转义都由 store 负责）")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dump":
                offenders.append(f"{path.name}:{node.lineno} 直接使用 json.dump（应经 store/atomic）")
    assert not offenders, "\n".join(offenders)


def test_bridge_reports_source_and_prefers_store_when_available():
    """契约等价回退**绝不静默**：来源必须可自证。"""
    from bettercomfyui.review import _bridge

    _bridge.reset_store_cache()
    assert _bridge.ids_source() in {"store", "fallback"}
    assert _bridge.atomics_source() in {"store", "fallback"}
    try:
        importlib.import_module("bettercomfyui.store.ids")
        importlib.import_module("bettercomfyui.store.atomic")
    except Exception:  # pragma: no cover - store 未就绪时跳过该断言
        pytest.skip("store 尚未就绪")
    assert _bridge.ids_source() == "store"
    assert _bridge.atomics_source() == "store"
    # 委托必须真的生效：ID 生成结果与 store.ids 完全一致
    from bettercomfyui.store import ids as store_ids

    assert _bridge.review_cycle_id("ASSET", "A1", "v1") == store_ids.review_cycle_id("ASSET", "A1", "v1")
    assert _bridge.lock_id("A1", "v1") == store_ids.lock_id("A1", "v1")
    assert _bridge.cnt_id("A1", "CNT1") == store_ids.cnt_id("A1", "CNT1")


def test_store_events_adapter_details():
    """store.events 已就绪时适配层必须是 store 模式；未就绪时必须抛明确异常。"""
    from bettercomfyui.review import events as review_events

    review_events.reset_store_events_cache()
    if review_events.store_events_available():
        review_events.require_store_events()  # 不抛 = 已就绪
        log = review_events.ReviewEventLog(project=None)
        assert log.mode == "memory"  # 没有 project 就没有落盘位置
        assert log.persistence_available is False
    else:  # pragma: no cover - 需要 store 缺失
        with pytest.raises(Exception):
            review_events.require_store_events()


def test_sys_path_bootstrap_is_available():
    assert str(Path(__file__).resolve().parent) in sys.path
