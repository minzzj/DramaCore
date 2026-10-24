"""导入冒烟 + 依赖方向静态检查（Lead 要求：import 可直接成功，禁止跨层 import）。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

MODULES = [
    "bettercomfyui.executors",
    "bettercomfyui.executors.base",
    "bettercomfyui.executors.mock",
    "bettercomfyui.executors.comfyui_http",
    "bettercomfyui.executors.director_nodes",
    "bettercomfyui.director",
    "bettercomfyui.director.errors",
    "bettercomfyui.director.settings",
    "bettercomfyui.director.deps",
    "bettercomfyui.director.events",
    "bettercomfyui.director.plan",
    "bettercomfyui.director.generate",
    "bettercomfyui.director.review",
    "bettercomfyui.director.rework",
    "bettercomfyui.director.cascade",
]

FORBIDDEN_IMPORTS = {
    "bettercomfyui/director": ("bettercomfyui.pipeline", "bettercomfyui.prompt"),
    "bettercomfyui/executors": ("bettercomfyui.pipeline", "bettercomfyui.director", "bettercomfyui.store"),
}


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    assert importlib.import_module(module) is not None


def test_public_api_is_exported():
    import bettercomfyui.director as director
    import bettercomfyui.executors as executors

    for name in (
        "plan_segments",
        "SegmentGenerator",
        "SegmentReviewer",
        "CascadeController",
        "ReworkController",
        "TechRetryCounter",
        "LockGateError",
        "CascadePolicyError",
    ):
        assert hasattr(director, name), name
    for name in (
        "JobRef",
        "JobState",
        "Executor",
        "MockExecutor",
        "ComfyUIHttpExecutor",
        "DirectorNodeExecutor",
        "validate_capabilities",
    ):
        assert hasattr(executors, name), name


def test_no_forbidden_cross_layer_imports():
    root = Path(__file__).parents[1]
    for package, forbidden in FORBIDDEN_IMPORTS.items():
        for path in (root / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for name in forbidden:
                assert f"import {name}" not in source, f"{path} 不得 import {name}"
                assert f"from {name}" not in source, f"{path} 不得 from {name} import"


def test_no_duplicate_enums_defined_here():
    """枚举唯一真源是 models/enums.py：本层不得自行定义同义枚举。"""
    root = Path(__file__).parents[1]
    for package in ("bettercomfyui/director", "bettercomfyui/executors"):
        for path in (root / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "class CounterKind" not in source
            assert "class SegmentStatus" not in source
            assert "class JobStatus" not in source
            assert "class ErrorClass" not in source
            assert "class ReviewState" not in source


def test_no_hardcoded_absolute_paths():
    root = Path(__file__).parents[1]
    for package in ("bettercomfyui/director", "bettercomfyui/executors"):
        for path in (root / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "H:\\" not in source and "C:\\Users" not in source
            assert "projects/" not in source or "output" not in source
