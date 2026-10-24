"""验收 26：A4 三项（C6 风格标签库 / G2 素材命名 / 分维度评分）。

## 对应流程图

| 项 | 图一原文 | 修前状态 |
|---|---|---|
| `C6` | 风格标签库预置供勾选 | **只有 5 套系统提示词模板**，没有画风词表 |
| `G2` | 素材命名规则 类型_序号_名称 | ID 有类型+序号但**不含可读名称** |
| `H5` | 评分维度 画质/一致性/合规/构图 | `score_dimensions` **只被校验**，审核器只吃单一 `score` |

## 判定通过

    P1 风格标签库有足够多的预置标签，且每个都有非空描述；
    P2 `@标签名` 能展开成完整描述；**多个标签**按顺序展开且不重复；
    P3 普通自由文本**原样保留**（不写 `@` 的老用法完全不受影响）；
    P4 **未知标签原样保留**（`@乱写的` 不被吞掉，用户能看出写错了）；
    P5 `human_label` 产出 `类型_序号_名称`（图一 G2 格式）；
    P6 分维度评分：传 `scores` 按配置维度取平均得出总分；
    P7 **缺维度直接报错**（不能悄悄按缺的算平均，那会让分数虚高）；
    P8 维度分数越界报错；
    P9 不传 `scores` 时**完全沿用旧行为**（旧调用方不受影响）；
    P10 是否通过按 `score_threshold` 判定，不采信调用方自相矛盾的 `passed`。

## 判定假通过（必须能变红）

    F1 把未知标签也当已知展开 -> P4 变红；
    F2 缺维度时用现有维度算平均 -> P7 变红；
    F3 改 `asset_id` 而不是新增 `human_label` -> P5 变红（且会破坏已有项目）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# C6 风格标签库
# ---------------------------------------------------------------------------


def test_p1_style_library_is_populated():
    from bettercomfyui.prompt.styles import STYLE_LIBRARY

    assert len(STYLE_LIBRARY) >= 10, f"标签太少：{len(STYLE_LIBRARY)}"
    for name, description in STYLE_LIBRARY.items():
        assert name and description, f"标签 {name!r} 描述为空"
        assert len(description) >= 8, f"标签 {name!r} 描述太短：{description}"


def test_p2_multiple_tokens_expand_in_order():
    from bettercomfyui.prompt.styles import STYLE_LIBRARY, expand_style_tokens

    text = "@写实电影感 @冷蓝电影光"
    out = expand_style_tokens(text)
    assert STYLE_LIBRARY["写实电影感"] in out
    assert STYLE_LIBRARY["冷蓝电影光"] in out
    # 顺序：写实在前、冷蓝在后
    assert out.index(STYLE_LIBRARY["写实电影感"]) < out.index(STYLE_LIBRARY["冷蓝电影光"])


def test_p2b_duplicate_tokens_expand_once():
    from bettercomfyui.prompt.styles import STYLE_LIBRARY, expand_style_tokens

    out = expand_style_tokens("@水墨 @水墨 @水墨")
    assert out.count(STYLE_LIBRARY["水墨"]) == 1, f"重复展开了：{out}"


def test_p3_free_text_is_untouched():
    """不写 `@` 的老用法必须**完全不受影响**。"""
    from bettercomfyui.prompt.styles import expand_style_tokens

    for text in ("古风，暖色调", "电影感，真实自然", "", "   "):
        assert expand_style_tokens(text) == text, f"{text!r} 被改动了"


def test_p4_unknown_token_is_preserved():
    """未知标签**不能被吞掉** —— 用户要能看出自己写错了。"""
    from bettercomfyui.prompt.styles import expand_style_tokens

    out = expand_style_tokens("@根本不存在的标签")
    assert "@根本不存在的标签" in out, f"未知标签被吞掉了：{out!r}"


def test_p4b_mixed_known_and_unknown():
    from bettercomfyui.prompt.styles import STYLE_LIBRARY, expand_style_tokens

    out = expand_style_tokens("@水墨 @乱写的")
    assert STYLE_LIBRARY["水墨"] in out, "已知标签没展开"
    assert "@乱写的" in out, "未知标签被吞掉"


# ---------------------------------------------------------------------------
# G2 素材命名
# ---------------------------------------------------------------------------


@pytest.fixture()
def asset_store(tmp_path):
    from bettercomfyui.config import load_config
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("G2命名测试")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    return AssetStore(paths)


def test_p5_human_label_matches_flowchart_format(asset_store):
    """`human_label` 必须是 `类型_序号_名称`（图一 G2）。"""
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    entry = asset_store.create_asset(
        asset_type=AssetType.CHARACTER, name="林秀",
        source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
    )
    label = asset_store.human_label(entry["asset_id"])
    assert label == "角色_1_林秀", f"格式不对：{label}"


def test_p5b_human_label_covers_all_types(asset_store):
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    expected = {
        AssetType.CHARACTER: "角色", AssetType.SCENE: "场景",
        AssetType.PROP: "物品", AssetType.SHOT: "分镜",
    }
    for asset_type, prefix in expected.items():
        entry = asset_store.create_asset(
            asset_type=asset_type, name=f"{prefix}甲",
            source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
        )
        label = asset_store.human_label(entry["asset_id"])
        assert label.startswith(prefix + "_"), f"{asset_type} 的前缀不对：{label}"
        assert label.endswith(f"{prefix}甲")


def test_p5c_asset_id_format_is_unchanged(asset_store):
    """**关键回归**：加可读命名**不得**改变磁盘 asset_id 格式。

    改了会让已有项目目录失效（破坏性迁移）。
    """
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    entry = asset_store.create_asset(
        asset_type=AssetType.CHARACTER, name="林秀",
        source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
    )
    asset_id = entry["asset_id"]
    parts = asset_id.split("_")
    assert len(parts) >= 3, f"asset_id 格式变了：{asset_id}"
    assert parts[-2].isdigit(), f"序号位置不对：{asset_id}"
    assert not asset_id.startswith("角色"), "不该把中文塞进磁盘 ID"


# ---------------------------------------------------------------------------
# 分维度评分
# ---------------------------------------------------------------------------


@pytest.fixture()
def reviewer(tmp_path):
    from bettercomfyui.config import load_config
    from bettercomfyui.review.asset_review import AssetReviewer
    from bettercomfyui.review.counters import CounterStore
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("评分维度测试")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    store = AssetStore(paths)
    return AssetReviewer(
        store, config=cfg, counters=CounterStore(), project_id=pid,
        project=paths,
        model_review=lambda material: {"score": 88.0, "passed": True, "reason": "ok"},
    ), cfg


def test_p6_dimension_scores_are_averaged(reviewer):
    rv, cfg = reviewer
    dims = list(cfg.asset_review.score_dimensions)
    scores = {d: 80.0 for d in dims}
    total, passed = rv._resolve_dimension_scores(scores, None, None)
    assert total == pytest.approx(80.0)
    assert passed is (80.0 >= float(cfg.asset_review.score_threshold))


def test_p7_missing_dimension_raises(reviewer):
    """缺维度必须报错 —— 悄悄按缺的算平均会让分数虚高。"""
    rv, cfg = reviewer
    dims = list(cfg.asset_review.score_dimensions)
    with pytest.raises(ValueError) as excinfo:
        rv._resolve_dimension_scores({dims[0]: 90.0}, None, None)
    msg = str(excinfo.value)
    assert "缺少维度" in msg, msg[:200]


def test_p8_out_of_range_score_raises(reviewer):
    rv, cfg = reviewer
    dims = list(cfg.asset_review.score_dimensions)
    with pytest.raises(ValueError) as excinfo:
        rv._resolve_dimension_scores({d: 150.0 for d in dims}, None, None)
    assert "0,100" in str(excinfo.value) or "[0,100]" in str(excinfo.value)


def test_p9_legacy_single_score_still_works(reviewer):
    """不传 `scores` 时**完全沿用旧行为**。"""
    rv, _cfg = reviewer
    total, passed = rv._resolve_dimension_scores(None, 88.0, True)
    assert total == 88.0 and passed is True
    total2, passed2 = rv._resolve_dimension_scores(None, 55.0, False)
    assert total2 == 55.0 and passed2 is False


def test_p10_passed_is_derived_from_threshold(reviewer):
    """传了 `scores` 时，是否通过按阈值判定 —— 不采信矛盾的自报 passed。"""
    rv, cfg = reviewer
    dims = list(cfg.asset_review.score_dimensions)
    low = {d: 10.0 for d in dims}
    total, passed = rv._resolve_dimension_scores(low, None, True)  # 自报通过
    assert total == pytest.approx(10.0)
    assert passed is False, "低分却采信了调用方自报的 passed"


def test_p10b_no_score_at_all_raises(reviewer):
    rv, _cfg = reviewer
    with pytest.raises(ValueError):
        rv._resolve_dimension_scores(None, None, None)
