"""验收 18：**素材库级约束**真的执行（图一 SG2 `G3` / `G4`）。

## 为什么单列一条

之前这两项**配了不执行**（ROUND18 实测）：

* `per_class_limit = 9`，连续造出 **12 个**同类素材，**无一被拦**；
* `assets.resolution` 在**产品代码里的读取点为 0**，全工程无 resize/scale。

配置里有值、`config.py` 也校验了边界，但没有任何业务代码用它 ——
「可配置」与「会执行」是两件事。

## 判定通过

    P1 `per_class_limit`：达到上限后再建同类素材必抛错，且错误信息含具体上限；
    P2 上限是**每类**不是全局：不同类型不受影响；
    P3 不传 `per_class_limit` 时不限制（旧调用方行为不变）；
    P4 `unify_resolution`：图片按**最长边**等比缩放到目标值；
    P5 **等比**而非强拉：宽高比保持（不允许为了统一而把角色拉变形）；
    P6 尺寸**已符合**时不重编码（避免无谓画质损失）；
    P7 **非图片**（音频）原样入库，不报错；
    P8 缩放记录写进版本 `extra`，可追溯（original_size / unified_size）。

## 判定假通过（必须能变红）

    F1 删掉 `create_asset` 里的上限判断 -> P1 变红；
    F2 把 `_unified_image` 改成强拉成正方形 -> P5 变红；
    F3 对已符合尺寸的图也重编码 -> P6 变红。
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

numpy = pytest.importorskip("numpy", reason="未验证：无 numpy，无法构造图像")
PIL_Image = pytest.importorskip("PIL.Image", reason="未验证：无 Pillow，无法构造图像")


def _store(tmp: Path):
    from bettercomfyui.config import load_config
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("素材库约束")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    return cfg, AssetStore(paths), paths


def _new_asset(store, tmp: Path, *, name="角色", asset_type=None, size=(64, 64)):
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    img = tmp / f"{name}.png"
    PIL_Image.fromarray(
        numpy.zeros((size[1], size[0], 3), dtype=numpy.uint8)).save(img)
    e = store.create_asset(asset_type=asset_type or AssetType.CHARACTER, name=name,
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    return e["asset_id"], img


# ---------------------------------------------------------------------------
# P1–P3 每类上限
# ---------------------------------------------------------------------------


def test_p1_per_class_limit_is_enforced(tmp_path):
    """达到上限后再建同类素材必须被拒，且错误信息含具体上限值。"""
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    cfg, store, _paths = _store(tmp_path)
    limit = cfg.assets.per_class_limit
    assert isinstance(limit, int) and limit >= 1

    for i in range(limit):
        store.create_asset(asset_type=AssetType.CHARACTER, name=f"角色{i}",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                           per_class_limit=limit)

    with pytest.raises(Exception) as excinfo:
        store.create_asset(asset_type=AssetType.CHARACTER, name="超限角色",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                           per_class_limit=limit)
    msg = str(excinfo.value)
    assert "上限" in msg, f"拒绝理由没说清是上限：{msg[:160]}"
    assert str(limit) in msg, f"拒绝理由缺少具体上限值 {limit}：{msg[:160]}"


def test_p2_limit_is_per_class_not_global(tmp_path):
    """上限是**每类**：CHARACTER 满了，SCENE 仍可建。"""
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    cfg, store, _paths = _store(tmp_path)
    limit = cfg.assets.per_class_limit
    for i in range(limit):
        store.create_asset(asset_type=AssetType.CHARACTER, name=f"角色{i}",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                           per_class_limit=limit)
    # 另一类不受影响
    store.create_asset(asset_type=AssetType.SCENE, name="场景1",
                       source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                       per_class_limit=limit)


def test_p3_limit_is_optional(tmp_path):
    """不传 `per_class_limit` 时不限制（旧调用方行为不变）。"""
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope

    cfg, store, _paths = _store(tmp_path)
    for i in range(cfg.assets.per_class_limit + 3):
        store.create_asset(asset_type=AssetType.CHARACTER, name=f"角色{i}",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT)


# ---------------------------------------------------------------------------
# P4–P8 统一分辨率
# ---------------------------------------------------------------------------


def test_p4_image_is_scaled_to_longest_edge(tmp_path):
    """3000x1200 的图 -> 最长边等于 `assets.resolution`。"""
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope

    cfg, store, paths = _store(tmp_path)
    aid, img = _new_asset(store, tmp_path, name="大图",
                          asset_type=AssetType.SCENE, size=(3000, 1200))
    v = store.add_version(aid, source=AssetSource.AI, file_path=img,
                          status=AssetStatus.PENDING_REVIEW,
                          unify_resolution=cfg.assets.resolution)
    stored = Path(paths.project_dir) / v["file_path"]
    size = PIL_Image.open(stored).size
    assert max(size) == cfg.assets.resolution, f"最长边应为 {cfg.assets.resolution}，实际 {size}"


def test_p5_scaling_is_proportional(tmp_path):
    """**等比**缩放：宽高比必须保持（不允许把角色拉变形）。"""
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope

    cfg, store, paths = _store(tmp_path)
    aid, img = _new_asset(store, tmp_path, name="宽图",
                          asset_type=AssetType.SCENE, size=(2000, 500))
    v = store.add_version(aid, source=AssetSource.AI, file_path=img,
                          status=AssetStatus.PENDING_REVIEW,
                          unify_resolution=cfg.assets.resolution)
    w, h = PIL_Image.open(Path(paths.project_dir) / v["file_path"]).size
    src_ratio = 2000 / 500
    assert abs((w / h) - src_ratio) < 0.02, (
        f"宽高比变了：原 {src_ratio:.2f} 实际 {w/h:.2f}（{w}x{h}）—— 不是等比缩放"
    )


def test_p6_already_matching_size_is_not_reencoded(tmp_path):
    """尺寸已符合时不重编码 —— 避免无谓画质损失。"""
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope

    cfg, store, paths = _store(tmp_path)
    target = cfg.assets.resolution
    aid, img = _new_asset(store, tmp_path, name="刚好",
                          asset_type=AssetType.SCENE, size=(target, target // 2))
    v = store.add_version(aid, source=AssetSource.AI, file_path=img,
                          status=AssetStatus.PENDING_REVIEW,
                          unify_resolution=target)
    assert v["extra"].get("resolution_unified") is None, (
        "尺寸已符合却做了缩放（会白白损失画质）"
    )


def test_p7_non_image_is_passed_through(tmp_path):
    """非图片（音频）原样入库，不报错、不改尺寸。"""
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope

    cfg, store, _paths = _store(tmp_path)
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<" + "h" * 800, *([0] * 800)))

    aid = store.create_asset(asset_type=AssetType.AUDIO, name="音",
                             source=AssetSource.AI, library_scope=LibraryScope.PROJECT)["asset_id"]
    v = store.add_version(aid, source=AssetSource.AI, file_path=wav,
                          status=AssetStatus.PENDING_REVIEW,
                          unify_resolution=cfg.assets.resolution)
    assert v["extra"].get("resolution_unified") is None, "音频不该被当成图片处理"


def test_p8_scaling_is_recorded_for_traceability(tmp_path):
    """缩放前后尺寸写进 `extra`，可追溯。"""
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope

    cfg, store, _paths = _store(tmp_path)
    aid, img = _new_asset(store, tmp_path, name="可追溯",
                          asset_type=AssetType.SCENE, size=(1600, 800))
    v = store.add_version(aid, source=AssetSource.AI, file_path=img,
                          status=AssetStatus.PENDING_REVIEW,
                          unify_resolution=cfg.assets.resolution)
    extra = v["extra"]
    assert extra.get("original_size") == [1600, 800], f"没记原始尺寸：{extra}"
    assert extra.get("unified_size"), f"没记缩放后尺寸：{extra}"
