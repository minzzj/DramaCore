# -*- coding: utf-8 -*-
"""构建审核相关工作流（ROUND27）：S3 素材生成→入库→审核、S4 视频审核。

## 为什么是两套

逻辑图把「素材生成/素材审核」和「视频审核」分在 SG2/SG3 与 SG5/SG6 两段，
用户也明确说「可以做一个大节点，也可以做两个单独节点用于审核」。所以：

* `S3_素材生成入库审核（GPU）`：素材生成（取任务）→ 任意图像模型 → 素材入库 → 素材审核；
  需要用户在图里选自己的 Checkpoint（本机无图像权重，不在本机跑）；
* `S4_视频审核（本机可跑）`：给一个成片文件路径，做段级 R_seg + 边界验证 + 整片 R_full；
  本机就能跑（本地启发式评分，真实帧统计）。

生成后用真实 ComfyUI `/object_info` 做 schema 校验（复用 build_master_workflow.validate）。
用法（ComfyUI 在 8188）：python -B scripts/build_review_workflows.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_one_node_workflows as B  # noqa: E402

PROJECT_ID = B.PROJECT_ID
PREVIEW_INPUT = "source"


def _widget_index(info: dict, node: str) -> dict[str, int]:
    return B.node_widget_index(info, node)


def _set(node: dict, info: dict, key: str, value) -> None:
    B.set_widget(node, info, key, value)


def _add(wf: dict, info: dict, node_type: str, widgets: list, *, pos, size) -> dict:
    """add_node + 按 /object_info 预置 outputs（多输出节点不会因连线顺序错位）。"""
    node = B.add_node(wf, node_type, widgets, pos=pos, size=size)
    spec = info.get(node_type) or {}
    out_types = spec.get("output") or []
    out_names = spec.get("output_name") or []
    node["outputs"] = [
        {"name": (out_names[i] if i < len(out_names) else f"out{i}"),
         "type": out_types[i], "links": []}
        for i in range(len(out_types))
    ]
    return node


def _preview(wf: dict, slot: int, src: dict, label: str, pos) -> None:
    view = B.add_node(wf, "PreviewAny", [], pos=pos, size=(520, 260))
    view["title"] = label
    B.add_link(wf, src, slot, view, PREVIEW_INPUT, "STRING")


def build_s3(info: dict) -> dict:
    """S3：素材生成（取任务）→ 图像工作流 → 素材入库 → 素材审核 → 预览。"""
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}

    # ---- SG2 GEN：取待生成素材 ----
    gen_vals = B.node_defaults(info, "BetterComfyUIAssetGen")
    gen = _add(wf, info, "BetterComfyUIAssetGen", gen_vals, pos=(-2200, 200), size=(460, 320))
    _set(gen, info, "项目", PROJECT_ID)
    _set(gen, info, "素材类型", "全部类型")
    _set(gen, info, "选取顺序", "按优先级（角色>场景>物品>分镜）")
    _set(gen, info, "指定名称或ID", "")

    # ---- 任意图像工作流（用户在这里换自己的模型/采样器）----
    ckpt = _add(wf, info, "CheckpointLoaderSimple",
                   B.node_defaults(info, "CheckpointLoaderSimple"),
                   pos=(-2200, 700), size=(420, 120))
    positive = _add(wf, info, "CLIPTextEncode",
                       B.node_defaults(info, "CLIPTextEncode"),
                       pos=(-1650, 150), size=(460, 260))
    B.add_link(wf, gen, 0, positive, "text", "STRING")
    B.add_link(wf, ckpt, 1, positive, "clip", "CLIP")

    negative = _add(wf, info, "CLIPTextEncode",
                       B.node_defaults(info, "CLIPTextEncode"),
                       pos=(-1650, 480), size=(460, 160))
    _set(negative, info, "text", "低质量，模糊，变形，多手多脚，水印")
    B.add_link(wf, ckpt, 1, negative, "clip", "CLIP")

    latent = _add(wf, info, "EmptyLatentImage",
                     B.node_defaults(info, "EmptyLatentImage"),
                     pos=(-1650, 700), size=(420, 120))
    sampler = _add(wf, info, "KSampler", B.node_defaults(info, "KSampler"),
                      pos=(-1050, 150), size=(420, 420))
    decode = _add(wf, info, "VAEDecode", B.node_defaults(info, "VAEDecode"),
                     pos=(-560, 150), size=(380, 120))
    B.add_link(wf, ckpt, 0, sampler, "model", "MODEL")
    B.add_link(wf, positive, 0, sampler, "positive", "CONDITIONING")
    B.add_link(wf, negative, 0, sampler, "negative", "CONDITIONING")
    B.add_link(wf, latent, 0, sampler, "latent_image", "LATENT")
    B.add_link(wf, sampler, 0, decode, "samples", "LATENT")
    B.add_link(wf, ckpt, 2, decode, "vae", "VAE")

    # ---- SG2 G：入库（新版本 + 指纹 + 待审）----
    ingest_vals = B.node_defaults(info, "BetterComfyUIAssetIngest")
    ingest = _add(wf, info, "BetterComfyUIAssetIngest", ingest_vals,
                      pos=(-80, 150), size=(460, 320))
    _set(ingest, info, "项目", PROJECT_ID)
    B.add_link(wf, gen, 2, ingest, "素材ID", "STRING")
    B.add_link(wf, decode, 0, ingest, "images", "IMAGE")

    # ---- SG3 H：素材审核（模型审核/本地启发式；无人值守）----
    review_vals = B.node_defaults(info, "BetterComfyUIAssetReview")
    review = _add(wf, info, "BetterComfyUIAssetReview", review_vals,
                      pos=(520, 150), size=(460, 300))
    _set(review, info, "project_id", PROJECT_ID)
    _set(review, info, "action", "模型审核（本地启发式）")
    _set(review, info, "asset_id", "")
    _set(review, info, "version", "")
    _set(review, info, "operator", "workflow")
    # 依赖边：素材入库的「素材ID」→ 审核节点 asset_id，保证「先入库、后审核」
    # （否则两者无依赖，ComfyUI 可能先跑审核 → 报“没有待审版本”）。
    B.add_link(wf, ingest, 0, review, "asset_id", "STRING")

    # ---- 结果可见 ----
    _preview(wf, 6, gen, "素材生成·摘要（进度）", (520, 700))
    _preview(wf, 3, ingest, "素材入库·摘要（版本/指纹）", (1060, 150))
    _preview(wf, 3, review, "素材审核·摘要（LOCK/评分/CNT1）", (1060, 450))
    return wf


def build_s4(info: dict) -> dict:
    """S4：视频审核（段级 R_seg + 边界 + 整片 R_full），本机可跑。"""
    wf = {"last_node_id": 0, "last_link_id": 0, "nodes": [], "links": [],
          "groups": [], "config": {}, "extra": {}, "version": 0.4}
    vals = B.node_defaults(info, "BetterComfyUIVideoReview")
    node = _add(wf, info, "BetterComfyUIVideoReview", vals, pos=(-900, 150), size=(560, 560))
    _set(node, info, "项目", PROJECT_ID)
    _set(node, info, "每段时长_秒", 10.0)
    _set(node, info, "审核范围", "段级 + 整片（推荐）")
    _set(node, info, "评分来源", "本地启发式（真实计算）")
    _set(node, info, "整片失败策略", "按项目配置")
    for slot, label, y in ((0, "结论", 150), (1, "段审核JSON（R_seg/CNT2/边界）", 450),
                           (2, "整片审核JSON（R_full/策略）", 750),
                           (4, "审计报告", 1050)):
        _preview(wf, slot, node, label, (40, y))
    return wf


def main() -> int:
    info = B.object_info()
    missing = [n for n in ("BetterComfyUIAssetGen", "BetterComfyUIAssetIngest",
                           "BetterComfyUIAssetReview", "BetterComfyUIVideoReview",
                           "CheckpointLoaderSimple", "KSampler") if n not in info]
    if missing:
        print(f"[FAIL] ComfyUI 里缺少节点：{missing}（先同步插件并重启）")
        return 1
    B.install(info, "S3_素材生成入库审核（GPU）", build_s3(info))
    B.install(info, "S4_视频审核（本机可跑）", build_s4(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
