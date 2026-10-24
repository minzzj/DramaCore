"""导演台**高级接缝**的能力检测与类型验证测试。

## 背景：任务书第 7 节的硬要求

> Agent 应检测能力并说明不支持项；不要硬编码为不可修改的规则。

导演台有 4 个高级接缝（`refine` / `selflift` / `semantic_bridge` / `face_refine`），
每个都由一个专门的节点产出对应类型。它们的 COMBO 选项在缺权重时**会退化**：

* `latent_upscale_model: ['(将 3D 权重放入 models/latent_upscale_models)']`
* `detector: [['face_yolov8m.pt'], ...]` ← **会显示一个名字，但磁盘上并没有**

第二类特别危险：光看 schema 会以为"有这个选项就能用"。
所以能力检测**必须查磁盘**。

## 本文件钉住两个我踩过的判断错误

1. **一刀切整个节点**：`MiniMaxH3DirectorRefine` 的 `refine` / `upscale`
   两个模式**不需要任何额外权重**，只有 `latent_upscale` 才需要。
   把整个节点标成"不可用"会让用户放弃本来能用的功能。
2. **查错目录**：人脸检测权重在 `models/ultralytics/bbox/`
   （节点 tooltip 原文），不是 `models/detection/`。
   查错目录会得出**相反**的结论。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui_comfy.fusion_nodes import (  # noqa: E402
    ADVANCED_MODES,
    check_advanced_modes,
)


def _fake_comfy(tmp_path: Path, files: dict[str, list[str]]) -> Path:
    """造一个假的 ComfyUI 目录：`{models 子目录: [文件名]}`。"""
    models = tmp_path / "models"
    for sub, names in files.items():
        d = models / sub
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x")
    return tmp_path


# ---------------------------------------------------------------------------
# 1) 不能一刀切：Refine 的免费选项
# ---------------------------------------------------------------------------


def test_refine_free_options_available_without_weights(tmp_path: Path):
    """`refine` / `upscale` 不需要额外权重，必须报可用。

    这是「一刀切整个节点」那个错误的守卫：如果实现只看
    `latent_upscale_models/` 是否为空就判定整个 Refine 不可用，
    这条会红。
    """
    root = _fake_comfy(tmp_path, {})          # 什么权重都没有
    checks = {c["name"]: c for c in check_advanced_modes(root)}
    refine = next(c for c in checks.values() if c["seam"] == "refine")
    assert refine["available"] is True, refine
    assert "refine" in refine["usable_options"]
    assert "upscale" in refine["usable_options"]
    # latent_upscale 仍应被标为不可用（它确实缺权重）
    blocked = {b["option"] for b in refine["blocked_options"]}
    assert "latent_upscale" in blocked, refine


def test_refine_latent_upscale_becomes_available_when_weight_present(tmp_path: Path):
    root = _fake_comfy(tmp_path, {"latent_upscale_models": ["3d_upscale.safetensors"]})
    refine = next(c for c in check_advanced_modes(root) if c["seam"] == "refine")
    assert "latent_upscale" in refine["usable_options"], refine
    assert all(b["option"] != "latent_upscale" for b in refine["blocked_options"])


# ---------------------------------------------------------------------------
# 2) 目录必须查对（face_refine 在 ultralytics/bbox）
# ---------------------------------------------------------------------------


def test_face_refine_checks_ultralytics_bbox(tmp_path: Path):
    """人脸检测权重在 `models/ultralytics/bbox/`，不是 `models/detection/`。

    查错目录会得出**相反**的结论 —— 这是真实踩到的错误。
    """
    spec = ADVANCED_MODES["FaceRefine（人脸细化）"]
    dirs = {v[0] for v in spec["gated_options"].values()}
    assert "ultralytics/bbox" in dirs, f"face_refine 查的目录不对：{dirs}"
    assert "detection" not in dirs, "face_refine 不该查 models/detection/"

    # 放到正确目录 -> 可用
    root = _fake_comfy(tmp_path, {"ultralytics/bbox": ["face_yolov8m.pt"]})
    face = next(c for c in check_advanced_modes(root) if c["seam"] == "face_refine")
    assert face["available"] is True, face


def test_face_refine_unavailable_without_weight(tmp_path: Path):
    root = _fake_comfy(tmp_path, {})
    face = next(c for c in check_advanced_modes(root) if c["seam"] == "face_refine")
    assert face["available"] is False
    assert face["blocked_options"]


# ---------------------------------------------------------------------------
# 3) 占位文件与目录不存在都要正确处理
# ---------------------------------------------------------------------------


def test_placeholder_files_do_not_count_as_weights(tmp_path: Path):
    """`put_*` 占位文件不算权重（与 doctor 的既有纪律一致）。"""
    root = _fake_comfy(tmp_path, {"latent_upscale_models": ["put_here"]})
    refine = next(c for c in check_advanced_modes(root) if c["seam"] == "refine")
    assert "latent_upscale" not in refine["usable_options"], refine


def test_non_weight_extensions_do_not_count(tmp_path: Path):
    """`configs/*.yaml` 一类非权重文件不算。"""
    root = _fake_comfy(tmp_path, {"latent_upscale_models": ["readme.yaml", "notes.txt"]})
    refine = next(c for c in check_advanced_modes(root) if c["seam"] == "refine")
    assert "latent_upscale" not in refine["usable_options"], refine


def test_missing_comfy_base_reports_all_gated_unavailable():
    """未配置 ComfyUI 路径时必须报"缺权重"，**不许猜成可用**。"""
    checks = check_advanced_modes(None)
    for c in checks:
        if c["seam"] == "refine":
            # 免费选项仍可用（它们本来就不依赖权重）
            assert c["available"] is True
        else:
            assert c["available"] is False, f"{c['name']} 在无配置时不该报可用"
            assert c["blocked_options"]


# ---------------------------------------------------------------------------
# 4) 结构完整性：四个接缝都要有
# ---------------------------------------------------------------------------


def test_all_four_seams_are_covered():
    """四个接缝必须都在检测范围里 —— 漏一个就等于默默不检测。"""
    seams = {spec["seam"] for spec in ADVANCED_MODES.values()}
    assert seams == {"refine", "selflift", "semantic_bridge", "face_refine"}, seams


def test_every_seam_names_a_real_node():
    """每个接缝都要写明由哪个节点产出（便于用户查文档）。"""
    for name, spec in ADVANCED_MODES.items():
        assert spec["node"].startswith("MiniMaxH3"), f"{name} 的节点名可疑：{spec['node']}"
        assert spec["free_options"] or spec["gated_options"], (
            f"{name} 既没有免费选项也没有受限选项 —— 检测逻辑会漏掉它"
        )


def test_report_mentions_correct_dirs(tmp_path: Path):
    """报告里写的目录必须是**真正该放权重**的目录。

    写错目录比不写更糟：用户照着放还是不管用。
    """
    from bettercomfyui_comfy.fusion_nodes import BetterComfyUIFusionDoctor as D

    root = _fake_comfy(tmp_path, {})
    # 直接调内部报告（读配置那步会失败并走"未验证"分支，所以这里喂养数据）
    checks = check_advanced_modes(root)
    text = "\n".join(
        f"{c['name']} {b['option']} {b['weight_dir']}"
        for c in checks for b in c["blocked_options"]
    )
    assert "ultralytics/bbox" in text
    assert "latent_upscale_models" in text
    assert "semantic_bridge" in text
