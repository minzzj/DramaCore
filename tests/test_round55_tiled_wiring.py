"""ROUND55：tiled（空间分块）采样接线 —— 离线测试（无需 GPU / 真实模型 / ComfyUI）。

覆盖：
1. **0 / 关闭路径**：``maybe_wrap_spatial_tiles(enable_tiling=False)`` 返回 None 且
   完全不触碰采样器 → 与改造前 ``restore_tiles = None`` 逐行等价；
2. **启用路径**：``enable_tiling=True`` 时确实包装 ``sampler.sampler_function``，
   并可 restore 还原（证明 wrapper 被调用）；
3. 参数解析/钳制/告警（``resolve_tiled_request``）与 SelfLift/Refine 组合说明；
4. ``director_runtime`` 给 one_stop 的集成 helper（kwargs/审计行）；
5. 源码接线断言（AST）：``MiniMaxH3Director.execute`` → ``execute_director_plan_core``
   → 主 ``sample_single_stage(... enable_tiling=...)`` 三段链路参数没有断。

注意：本机（无 torch）也能跑：``spatial_tiled_sampling`` 已把 torch 导入改为可选，
纯开关/钳制/包装函数不依赖 torch；真正逐块推理才需要 torch。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DIRECTOR_DIR = REPO_ROOT / "bettercomfyui_comfy" / "vendor" / "h3_director" / "director"
DIRECTOR_NODE = REPO_ROOT / "bettercomfyui_comfy" / "vendor" / "h3_director" / "nodes" / "director.py"

from bettercomfyui_comfy import director_runtime as dr  # noqa: E402
from bettercomfyui_comfy.vendor.h3_director.director import core_sampling as cs  # noqa: E402
from bettercomfyui_comfy.vendor.h3_director.director import spatial_tiled_sampling as sts  # noqa: E402


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class _FakeSampler:
    """最小采样器替身：只需要 sampler_function 属性（wrap 就是换它）。"""

    def __init__(self) -> None:
        def sampler_function(model, noise, sigmas, extra_args=None,
                             callback=None, disable=None, **kwargs):
            return "sampled"

        self.sampler_function = sampler_function


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _find_func(tree: ast.Module, name: str, *, class_name: str | None = None):
    body = tree.body
    if class_name is not None:
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
        body = cls.body
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}（class={class_name}）")


def _kwargs(call: ast.Call) -> list[str]:
    return [kw.arg for kw in call.keywords if kw.arg]


def _calls(node: ast.AST, func_name: str) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == func_name:
                out.append(sub)
    return out


# ---------------------------------------------------------------------------
# 1) 0 路径：完全不碰采样器（旧行为不变）
# ---------------------------------------------------------------------------
def test_disabled_returns_none_and_leaves_sampler_untouched():
    sampler = _FakeSampler()
    original = sampler.sampler_function

    restore = cs.maybe_wrap_spatial_tiles(
        sampler, enable_tiling=False, tile_count=4, tile_overlap=64)

    assert restore is None, "关闭时必须返回 None（等价旧版 restore_tiles = None）"
    assert sampler.sampler_function is original, "关闭时绝不能修改 sampler.sampler_function"


def test_default_arguments_are_disabled():
    sampler = _FakeSampler()
    original = sampler.sampler_function
    assert cs.maybe_wrap_spatial_tiles(sampler, enable_tiling=False) is None
    assert sampler.sampler_function is original


# ---------------------------------------------------------------------------
# 2) 启用路径：真的包装 + 可还原
# ---------------------------------------------------------------------------
def test_enabled_wraps_sampler_and_restore_works():
    sampler = _FakeSampler()
    original = sampler.sampler_function

    restore = cs.maybe_wrap_spatial_tiles(
        sampler, enable_tiling=True, tile_count=2, tile_overlap=64)

    assert callable(restore), "启用时必须返回 restore 回调"
    assert sampler.sampler_function is not original, "启用时必须替换 sampler_function"
    wrapped = sampler.sampler_function
    # 包装后的调用仍能走通（fake sampler_function 不依赖 torch）
    assert wrapped("model", "noise", "sigmas") == "sampled"
    restore()
    assert sampler.sampler_function is original, "restore 必须还原原函数"


def test_wrap_n_tiles_le_1_is_noop_even_when_enabled():
    sampler = _FakeSampler()
    original = sampler.sampler_function
    restore = sts.wrap_sampler_spatial_tiles(sampler, n_tiles=1, overlap_pixels=64)
    assert callable(restore)
    assert sampler.sampler_function is original
    restore()


def test_require_torch_raises_clear_error_when_missing(monkeypatch):
    """真推理前无 torch → 清晰异常（不静默退回全画布）。"""
    monkeypatch.setattr(sts, "torch", None)
    with pytest.raises(RuntimeError, match="PyTorch"):
        sts._require_torch()


# ---------------------------------------------------------------------------
# 3) 参数解析 / 钳制 / 组合说明
# ---------------------------------------------------------------------------
def test_clamp_bounds():
    assert sts.clamp_n_tiles(0) == 1
    assert sts.clamp_n_tiles(2) == 2
    assert sts.clamp_n_tiles(99) == sts.MAX_TILE_COUNT == 8
    assert sts.clamp_n_tiles("bad") == 2
    assert sts.clamp_tile_overlap(-5) == 0
    assert sts.clamp_tile_overlap(64) == 64
    assert sts.clamp_tile_overlap(99999) == 2048


def test_resolve_disabled_zero_keeps_old_behavior():
    req = sts.resolve_tiled_request(0, 64)
    assert req["enabled"] is False
    assert req["n_tiles"] == 0
    assert req["overlap_pixels"] == 64
    assert req["warnings"] == []
    assert "off" in req["report"] and "默认 0 行为不变" in req["report"]


@pytest.mark.parametrize("raw", [1, -3, 0])
def test_resolve_disabled_for_le_one(raw):
    req = sts.resolve_tiled_request(raw, 64)
    assert req["enabled"] is False
    assert req["n_tiles"] == 0


def test_resolve_invalid_value_disables_with_warning():
    req = sts.resolve_tiled_request("not-a-number", 64)
    assert req["enabled"] is False
    assert req["warnings"], "非法 tiled_n_tiles 必须有告警，不能静默"


def test_resolve_enabled():
    req = sts.resolve_tiled_request(4, 96)
    assert req["enabled"] is True
    assert req["n_tiles"] == 4
    assert req["overlap_pixels"] == 96
    assert req["warnings"] == []
    assert "ON" in req["report"]
    assert "时间维/音频维不分块" in req["report"]


def test_resolve_clamps_and_warns():
    req = sts.resolve_tiled_request(12, 5000)
    assert req["enabled"] is True
    assert req["n_tiles"] == 8
    assert req["overlap_pixels"] == 2048
    joined = " ".join(req["warnings"])
    assert "钳制" in joined


def test_describe_combinations_selflift_and_refine():
    on = sts.resolve_tiled_request(4, 64)
    notes_selflift = sts.describe_tiled_combinations(on, selflift=True)
    assert notes_selflift and any("SelfLift" in n for n in notes_selflift)
    notes_refine = sts.describe_tiled_combinations(on, refine=True)
    assert notes_refine and any("Refine" in n for n in notes_refine)
    off = sts.resolve_tiled_request(0, 64)
    assert sts.describe_tiled_combinations(off, selflift=True, refine=True) == []


# ---------------------------------------------------------------------------
# 4) director_runtime 给 one_stop 的集成 helper
# ---------------------------------------------------------------------------
def test_runtime_zero_kwargs_unchanged():
    assert dr.tiled_sampling_kwargs(0) == {
        "tiled_n_tiles": 0, "tiled_overlap_pixels": 64,
    }
    info = dr.normalize_tiled_sampling(0)
    assert info["enabled"] is False
    assert info["kwargs"] == {"tiled_n_tiles": 0, "tiled_overlap_pixels": 64}
    assert "off" in info["report"]
    assert "off" in dr.tiled_audit_line(0)


def test_runtime_enabled_maps_to_vendor_values():
    info = dr.normalize_tiled_sampling(4, 96)
    assert info["enabled"] is True
    assert info["kwargs"] == {"tiled_n_tiles": 4, "tiled_overlap_pixels": 96}
    assert "ON" in info["report"]
    assert "ON" in dr.tiled_audit_line(4, 96)


def test_runtime_clamps_like_vendor():
    assert dr.tiled_sampling_kwargs(12, 5000) == {
        "tiled_n_tiles": 8, "tiled_overlap_pixels": 2048,
    }
    assert dr.tiled_sampling_kwargs("bad", "bad") == {
        "tiled_n_tiles": 0, "tiled_overlap_pixels": 64,
    }


# ---------------------------------------------------------------------------
# 5) 源码接线断言（AST）：三段链路参数没有断
# ---------------------------------------------------------------------------
def test_director_execute_accepts_and_forwards_tiled():
    tree = _parse(DIRECTOR_NODE)
    fn = _find_func(tree, "execute", class_name="MiniMaxH3Director")
    args = [a.arg for a in fn.args.args]
    assert "tiled_n_tiles" in args
    assert "tiled_overlap_pixels" in args
    # 默认值：0 / 64（0 = 关闭，旧行为不变）
    defaults = dict(zip(args[-len(fn.args.defaults):], fn.args.defaults))
    assert isinstance(defaults["tiled_n_tiles"], ast.Constant)
    assert defaults["tiled_n_tiles"].value == 0
    assert isinstance(defaults["tiled_overlap_pixels"], ast.Constant)
    assert defaults["tiled_overlap_pixels"].value == 64

    core_calls = _calls(fn, "execute_director_plan_core")
    assert core_calls, "execute 必须调用 execute_director_plan_core"
    kw = _kwargs(core_calls[0])
    assert "tiled_n_tiles" in kw and "tiled_overlap_pixels" in kw, \
        "execute → execute_director_plan_core 的 tiled 参数断链"


def test_executor_core_signature_and_main_sample_wiring():
    tree = _parse(DIRECTOR_DIR / "executor_core.py")
    fn = _find_func(tree, "execute_director_plan_core")
    args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "tiled_n_tiles" in args and "tiled_overlap_pixels" in args

    sample_calls = _calls(fn, "sample_single_stage")
    assert sample_calls, "executor_core 必须调用 sample_single_stage"
    kw = set(_kwargs(sample_calls[0]))
    assert {"enable_tiling", "tile_count", "tile_overlap"} <= kw, \
        "主采样调用点没有接 tiled 参数"

    # 解析结果必须落到 plan，供采样点读取（不允许静默丢参）
    src = (DIRECTOR_DIR / "executor_core.py").read_text(encoding="utf-8")
    assert "plan.tiled_enabled" in src
    assert "plan.tiled_n_tiles" in src
    assert "plan.tiled_overlap_pixels" in src
    assert "resolve_tiled_request(" in src
    assert "describe_tiled_combinations(" in src


def test_core_sampling_backward_compatible_defaults():
    tree = _parse(DIRECTOR_DIR / "core_sampling.py")
    fn = _find_func(tree, "sample_single_stage")
    args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    defaults = {}
    all_args = fn.args.args + fn.args.kwonlyargs
    offset = len(fn.args.args) - len(fn.args.defaults)
    for idx, node in enumerate(fn.args.defaults):
        defaults[fn.args.args[offset + idx].arg] = node
    for idx, node in enumerate(fn.args.kw_defaults):
        if node is not None:
            defaults[fn.args.kwonlyargs[idx].arg] = node
    assert {"enable_tiling", "tile_count", "tile_overlap"} <= set(args)
    assert isinstance(defaults["enable_tiling"], ast.Constant)
    assert defaults["enable_tiling"].value is False, "默认必须仍为关闭（旧行为不变）"
    assert defaults["tile_count"].value == 2
    assert defaults["tile_overlap"].value == 128

    # 关闭时返回 None（等价旧版 restore_tiles = None）
    helper = _find_func(tree, "maybe_wrap_spatial_tiles")
    src_fn = ast.get_source_segment(
        (DIRECTOR_DIR / "core_sampling.py").read_text(encoding="utf-8"), helper) or ""
    assert "if not enable_tiling:" in src_fn and "return None" in src_fn


def test_restore_is_only_called_when_not_none():
    """sample_single_stage 的 finally 保持 ``if restore_tiles is not None``（0 路径不调用）。"""
    src = (DIRECTOR_DIR / "core_sampling.py").read_text(encoding="utf-8")
    assert "if restore_tiles is not None:" in src
    assert "restore_tiles = maybe_wrap_spatial_tiles(" in src
