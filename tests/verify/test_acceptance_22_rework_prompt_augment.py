"""验收 22：返工提示词加料（图一 SG5 `X`「提示词自动加相关提示词和 @素材」）。

## 为什么单列

`X` 此前**零实现**。后果很具体：返工时把**同一段提示词原样再喂一遍**，
模型没有任何新信息 —— 不合格的维度大概率还是不合格，
CNT2 三次很快烧完，最后只能取最高分兜底（降级成片）。

**加料**才是"重做"与"重复"的区别。

## 判定通过

    P1 `mode=disabled` -> 提示词**原样不变**（`changed=False`）；
    P2 `mode=dimensions` -> 只加**低于阈值**的维度，且翻译成**正向约束**；
    P3 高于阈值的维度**不得**出现在加料里（不瞎加）；
    P4 `mode=assets` -> 只加 `@素材` 标记，格式为图一 `XF` 的 `@类型_序号_名称`；
    P5 `mode=both` -> 两种都加；
    P6 加料**追加在原文之后**，原文完整保留（不改写）；
    P7 非法 mode -> 报错（不静默退回默认）；
    P8 审核结果结构不认识时**不崩**，返回空加料（防御性）；
    P9 加料后写回 `segment.generation_params` 且**保留原始提示词**（可追溯）；
    P10 `ReworkController` 返工时会真的调用加料（不是只有函数没人调）。

## 判定假通过（必须能变红）

    F1 让 `disabled` 也加料 -> P1 变红；
    F2 忽略阈值、把所有维度都加上 -> P3 变红；
    F3 覆盖原文而不是追加 -> P6 变红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.director.augment import (  # noqa: E402
    AUGMENT_MODES,
    asset_markers,
    build_rework_prompt,
    failed_dimensions,
    format_asset_marker,
)


class _Seg:
    def __init__(self, prompt="林秀推门走进客栈", refs=None):
        self.segment_id = "P_CH1_SEG1"
        self.prompt = prompt
        self.asset_refs = refs if refs is not None else [
            {"asset_id": "p_character_1_abc", "ref": "林秀"},
            {"asset_id": "p_scene_2_def", "ref": "客栈大堂"},
        ]
        self.current_version = "v1"
        self.generation_params = {}


def _review() -> dict:
    return {
        "threshold": 70,
        "score": 62.0,
        "r1a_scores": {
            "角色一致性": 62.0,   # 低于阈值 -> 应加
            "画面连贯": 88.0,     # 高于阈值 -> 不应加
            "动作合理": 91.0,
            "无闪烁": 95.0,
        },
    }


def _resolver(asset_id: str) -> dict:
    if "character" in asset_id:
        return {"name": "林秀", "asset_type": "CHARACTER"}
    return {"name": "客栈大堂", "asset_type": "SCENE"}


# ---------------------------------------------------------------------------
# P1 – P6 加料行为
# ---------------------------------------------------------------------------


def test_p1_disabled_leaves_prompt_untouched():
    seg = _Seg()
    r = build_rework_prompt(seg, _review(), mode="disabled")
    assert r["changed"] is False
    assert r["augmented"] == seg.prompt


def test_p2_dimensions_added_with_positive_hints():
    r = build_rework_prompt(_Seg(), _review(), mode="dimensions")
    names = [d[0] for d in r["added_dimensions"]]
    assert names == ["角色一致性"], names
    assert r["added_hints"], "没生成正向约束"
    assert all("角色" in h for h in r["added_hints"])
    assert "角色一致性" not in "".join(str(d) for d in r["added_dimensions"]
                                     if d[0] != "角色一致性")


def test_p3_passing_dimensions_are_not_added():
    """高于阈值的维度**不得**出现在加料里。"""
    r = build_rework_prompt(_Seg(), _review(), mode="dimensions")
    joined = r["augmented"]
    for passing in ("画面连贯", "动作合理", "无闪烁"):
        assert passing not in joined, f"通过的维度 {passing} 被误加进提示词"


def test_p4_assets_markers_use_xf_format():
    """`@素材` 标记必须是图一 XF 的 `@类型_序号_名称`。"""
    r = build_rework_prompt(_Seg(), _review(), mode="assets", resolver=_resolver)
    assert "@角色_1_林秀" in r["added_markers"], r["added_markers"]
    assert "@场景_2_客栈大堂" in r["added_markers"], r["added_markers"]
    assert "@角色_1_林秀" in r["augmented"]


def test_p5_both_adds_both_kinds():
    r = build_rework_prompt(_Seg(), _review(), mode="both", resolver=_resolver)
    assert r["added_dimensions"], "没加维度"
    assert r["added_markers"], "没加素材标记"


def test_p6_original_prompt_is_preserved():
    """加料**追加**在原文之后，原文完整保留。"""
    seg = _Seg(prompt="原始提示词内容")
    r = build_rework_prompt(seg, _review(), mode="both", resolver=_resolver)
    assert r["augmented"].startswith("原始提示词内容"), r["augmented"]
    assert r["original"] == "原始提示词内容"


# ---------------------------------------------------------------------------
# P7 – P8 边界
# ---------------------------------------------------------------------------


def test_p7_invalid_mode_raises():
    with pytest.raises(ValueError) as excinfo:
        build_rework_prompt(_Seg(), _review(), mode="乱写")
    assert "rework_prompt_augment" in str(excinfo.value)


def test_p8_unknown_review_shape_does_not_crash():
    """审核结果结构不认识时返回空加料，而不是崩掉整个返工。"""
    r = build_rework_prompt(_Seg(), {"奇怪结构": [1, 2, 3]}, mode="both")
    assert r["added_dimensions"] == []
    # 素材标记仍然能加（不依赖 review 结构）
    assert r["added_markers"]


def test_p8b_missing_threshold_uses_all_scores():
    """没有 threshold 时按"全部维度都算"处理（不静默什么都不加）。"""
    dims = failed_dimensions({"r1a_scores": {"画质": 50.0}})
    assert dims and dims[0][0] == "画质"


# ---------------------------------------------------------------------------
# P9 – P10 接线
# ---------------------------------------------------------------------------


def test_p9_controller_writes_back_and_keeps_original():
    """`ReworkController` 返工时要写回加料后的提示词，并保留原文。"""
    from bettercomfyui.director.rework import ReworkController
    from bettercomfyui.review.counters import CounterStore

    seg = _Seg()
    ctrl = ReworkController(
        CounterStore(),
        cfg=None,
        generator=None,
        reviewer=None,
    )
    result = ctrl._augment_rework_prompt(seg, _review())
    assert result["changed"] is True, "加料没生效"
    assert seg.generation_params["prompt"] == result["augmented"]
    assert seg.generation_params["prompt_original_before_augment"] == "林秀推门走进客栈"


def test_p10_controller_uses_config_mode():
    """`video_review.rework_prompt_augment=disabled` 时控制器不得加料。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.director.rework import ReworkController
    from bettercomfyui.review.counters import CounterStore

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.video_review.rework_prompt_augment = "disabled"
    seg = _Seg()
    ctrl = ReworkController(CounterStore(), cfg=cfg)
    result = ctrl._augment_rework_prompt(seg, _review())
    assert result["changed"] is False
    assert seg.prompt == "林秀推门走进客栈", "disabled 却改了提示词"


def test_p11_modes_are_declared():
    assert AUGMENT_MODES == ("disabled", "dimensions", "assets", "both")


# ---------------------------------------------------------------------------
# 纯函数细节
# ---------------------------------------------------------------------------


def test_format_asset_marker_fallbacks():
    """拿不到完整信息时要退化成 `@名称` / `@ref`，**绝不返回空标记**。"""
    assert format_asset_marker(name="林秀") == "@林秀"
    assert format_asset_marker(ref="c1") == "@c1"
    assert format_asset_marker(asset_id="x_1") == "@x_1"
    assert format_asset_marker() == ""


def test_asset_markers_dedup_and_order():
    refs = [{"asset_id": "a_character_1_x", "ref": "甲"},
            {"asset_id": "a_character_1_x", "ref": "甲"},
            {"asset_id": "a_scene_2_y", "ref": "乙"}]
    out = asset_markers(refs)
    assert len(out) == 2, f"没有去重：{out}"
