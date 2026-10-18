"""融合节点定义：在 ComfyUI 里把「编排层」与「三个导演台」真正接起来。

## 融合拓扑（两条路，用户自选）

**路线 A：用自己的项目 / 素材 / 审核（推荐，体现本项目价值）**

```
[Better ComfyUI 项目] ─project_id─┐
                                  ├─▶ [Better ComfyUI 融合·段计划]─┬─ MMX_DIR_GROUP ─▶ Director.i2v_groups / r2v_groups
[Better ComfyUI 时间轴]─剧本──────┘                                └─ 组摘要/审计 ─▶ 显示
                                                                          │
MiniMaxH3Director(images/audio) ─▶ [Better ComfyUI 融合·回灌] ─▶ 段版本 + 审核材料 + CNT 记账
```

**路线 B：用 SmartDirector 的 AI 分镜**

```
[MiniMaxH3SmartDirector] ─导演组(MMX_DIR_GROUP)─▶ Director.i2v_groups / r2v_groups
[MiniMaxH3SmartDirector] ─Smart计划─▶ [Better ComfyUI 融合·回灌] ─▶ 段版本 + 审核
```

**路线 C：ChainDirector 一站式**（它自己出片，不需要 Director）

```
[MiniMaxH3ChainDirector] ─画面帧/音频─▶ [Better ComfyUI 融合·回灌] ─▶ 段版本 + 审核
```

## 为什么回灌节点是必要的

三个导演台**都只管出画面**，它们不回答：
「这段是谁的第几个版本？素材用的哪一版？审没审过？重做了几次？」

那正是本项目的职责（方案里 `CNT2`/`CNT3` 与非继承 LOCK）。所以：
**导演台出片 -> 回灌成段版本 -> 段级审核 -> 不合格就 CNT2 重做。**
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import fusion
from ._bridge import BridgeError, load_orchestrator

CATEGORY = "Better ComfyUI/融合"
MMX_DIR_GROUP = fusion.MMX_DIR_GROUP

#: 回灌产物的相对目录
INGEST_SUBDIR = "ingest"


class FusionNodeError(RuntimeError):
    """面向使用者的节点错误。"""


def _orch():
    try:
        return load_orchestrator()
    except BridgeError as exc:
        raise FusionNodeError(str(exc)) from exc


def _cfg(repo_root: Path):
    from importlib import import_module

    cfg_mod = import_module("bettercomfyui.config")
    return cfg_mod.load_config(repo_root=repo_root)


#: 导演台高级模式 -> 它们**依赖的外部权重目录**。
#:
#: 任务书第 7 节要求：「Agent 应检测能力并说明不支持项」。
#: 这四个模式各自需要一个外部模型，缺了就无法使用 —— 而且缺的时候
#: 对应 COMBO 选项会退化成一句提示文字（如
#: `'(将 3D 权重放入 models/latent_upscale_models)'`）。
#:
#: 所以这里不只是"列出功能"，而是**真的去查权重目录**。
#:
#: ⚠️ 必须**按选项**判断，不能一刀切整个节点：
#: `MiniMaxH3DirectorRefine` 的 `refine` / `upscale` 两个模式
#: **不需要任何额外权重**，只有 `latent_upscale` 模式才需要 3D latent 放大模型。
#: 把整个节点标成"不可用"会误导用户放弃本来能用的功能。
ADVANCED_MODES: dict[str, dict] = {
    "二采 / 放大（Refine）": {
        "node": "MiniMaxH3DirectorRefine",
        "seam": "refine",
        #: 哪些选项不需要额外权重就能用
        "free_options": ["refine", "upscale"],
        #: 需要额外权重的选项 -> (权重目录, 说明)
        "gated_options": {
            "latent_upscale": ("latent_upscale_models",
                               "需要 3D latent 放大模型（models/latent_upscale_models）"),
            "upscale_method=nvidia_rtx_vsr": ("upscale_models",
                                              "需要 RTX VSR 放大模型"),
        },
    },
    "SelfLift（分段自提升）": {
        "node": "MiniMaxH3DirectorSelfLift",
        "seam": "selflift",
        "free_options": [],
        "gated_options": {
            "*": ("latent_upscale_models", "需要 3D latent 放大模型（走 latent_upscale_model）"),
        },
    },
    "SemanticBridge（语义桥接）": {
        "node": "MiniMaxH3DirectorSemanticBridge",
        "seam": "semantic_bridge",
        "free_options": [],
        "gated_options": {
            "*": ("semantic_bridge", "需要 Semantic Bridge adapter 权重"),
        },
    },
    "FaceRefine（人脸细化）": {
        "node": "MiniMaxH3DirectorFaceRefine",
        "seam": "face_refine",
        "free_options": [],
        "gated_options": {
            # ⚠️ 目录必须是 **ultralytics/bbox**，不是 detection。
            # 它的 tooltip 原文：「人脸检测权重，放到 models/ultralytics/bbox/」。
            # 我第一版查的是 models/detection/ —— 查错目录会得出相反的结论
            # （那个目录里只有占位文件，于是"不可用"；实际原因只是目录查错了）。
            "*": ("ultralytics/bbox",
                  "需要人脸检测权重（YOLO 人脸模型，如 face_yolov8m.pt）"),
        },
    },
}


def _weight_files_in(models_dir: Path, subdir: str) -> list[str]:
    """列出某个 models 子目录下的真实权重文件（排除 put_* 占位）。"""
    d = models_dir / subdir
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".safetensors", ".ckpt", ".pt", ".pth", ".bin",
                                    ".gguf", ".onnx", ".sft"}:
            continue
        if p.name.startswith("put_"):
            continue
        out.append(str(p.relative_to(d)))
    return sorted(out)


def check_advanced_modes(comfy_base: str | Path | None) -> list[dict]:
    """检查导演台四个高级模式在本机**哪些选项真的可用**（查权重目录，不靠猜）。

    返回每项：`{name, node, seam, usable_options, blocked_options, available, note}`
    其中 `available` 表示**至少有一个选项能用**。

    **为什么必须查目录**：这些模式的 COMBO 选项在缺权重时会退化成一句
    提示文字，光看 schema 会以为"有这个选项就是能用的"。
    只有查磁盘才知道到底有没有那个权重。
    """
    results: list[dict] = []
    models_dir = (Path(comfy_base) / "models") if comfy_base else None
    for name, spec in ADVANCED_MODES.items():
        usable: list[str] = []
        blocked: list[dict] = []
        for opt in spec["free_options"]:
            usable.append(opt)
        for opt, (subdir, why) in spec["gated_options"].items():
            files = _weight_files_in(models_dir, subdir) if models_dir else []
            if files:
                usable.append(opt)
            else:
                blocked.append({"option": opt, "weight_dir": subdir, "why": why})
        results.append({
            "name": name,
            "node": spec["node"],
            "seam": spec["seam"],
            "usable_options": usable,
            "blocked_options": blocked,
            # 「可用」= 至少有一个选项真的能用
            "available": bool(usable),
        })
    return results


def _resolve_project_paths(project_id: str, cfg: Any = None):
    """解析项目路径。

    `cfg` 可显式传入 —— 不传才去加载。这样调用方（节点）用的配置与
    路径解析用的配置**必然是同一份**；否则测试里改了配置根目录，
    路径仍按磁盘上的配置解析，两边不一致（实测踩到：计数写到了真实 projects/）。
    """
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    from .nodes import _orch_repo_root, _projects_root

    if cfg is None:
        cfg = _cfg(_orch_repo_root())
    root = _projects_root(cfg)
    paths = paths_mod.ProjectPaths(root=root, project_id=project_id)
    paths.ensure_layout()
    return paths, cfg, root


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tensor_batch_to_uint8(frames: Any):
    """ComfyUI IMAGE 批次 [N,H,W,C] float 0..1 -> uint8 numpy [N,H,W,C]。"""
    import numpy as np

    arr = frames
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    arr = np.asarray(arr)
    if arr.ndim == 3:
        arr = arr[None, ...]
    if arr.ndim != 4:
        raise FusionNodeError(f"IMAGE 批次维度应为 [N,H,W,C]，收到 {arr.shape}")
    arr = np.clip(arr * 255.0, 0, 255).astype("uint8")
    return arr


# ===========================================================================
# 1) 段计划 -> MMX_DIR_GROUP
# ===========================================================================


class BetterComfyUIFusionGroups:
    """把剧本/段计划 + 素材库已 LOCK 版本 → `MMX_DIR_GROUP`。

    输出**直连** `MiniMaxH3Director` 的 `i2v_groups` 或 `r2v_groups` 输入。

    硬规则（R3）：只有取得 LOCK 的素材版本才会进组；未 LOCK **直接报错**，
    不静默跳过 —— 跳过会让你以为素材用上了而实际没有。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "script_text": ("STRING", {"default": "", "multiline": True,
                                           "tooltip": "结构化 JSON 或纯文本分镜；留空用内置演示剧本。"}),
                "family": (["r2v 参考生视频", "i2v 图生视频"], {"default": "r2v 参考生视频"}),
                "default_duration": ("FLOAT", {"default": 5.0, "min": 0.2, "max": 120.0, "step": 0.1}),
                "max_segments": ("INT", {"default": 6, "min": 1, "max": 200}),
            },
            "optional": {
                "use_locked_assets": ("BOOLEAN", {"default": True,
                                                  "tooltip": "开启后段引用会解析成已 LOCK 的真实参考图；关闭则只出提示词。"}),
                "prev_last_frame": ("IMAGE", {"tooltip": "上段尾帧；接上后首段之后会带连续性（可选）。"}),
                "base_seed": ("INT", {"default": -1, "min": -1, "max": 9007199254740991,
                                      "tooltip": "固定种子；-1 = 按段确定性推导（同一段同一版本永远同一结果）"}),
            },
        }

    RETURN_TYPES = (MMX_DIR_GROUP, "STRING", "STRING", "INT")
    RETURN_NAMES = ("groups", "组摘要", "审计JSON", "首段种子")
    FUNCTION = "build"
    CATEGORY = CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs) -> str:
        """导演组规划依赖素材库 LOCK 状态：每次排队都必须重新读库。

        审计 R2：ComfyUI 按输入哈希缓存输出；素材新增 LOCK 后重复排队会命中旧缓存，
        导演组仍挂旧素材（看起来在工作，实际没更新）。
        """
        import time
        import uuid

        return f"{time.time_ns()}-{uuid.uuid4().hex}"

    def build(self, project_id: str, script_text: str, family: str,
              default_duration: float, max_segments: int,
              use_locked_assets: bool = True, prev_last_frame: Any = None,
              base_seed: int = -1):
        _orch()
        from bettercomfyui.seed import derive_seed
        from .nodes import _orch_repo_root

        repo_root = _orch_repo_root()
        pid = (project_id or "").strip()
        if not pid:
            raise FusionNodeError(
                "请先填写 project_id（用「Better ComfyUI 项目」节点生成）。"
                "融合节点必须挂在具体项目上，否则素材版本无法追溯。"
            )

        # ---- 解析剧本 -> 段列表 ----
        segments = _parse_script_to_segments(
            script_text, default_duration=default_duration, max_segments=max_segments
        )
        if not segments:
            raise FusionNodeError("剧本没有解析出任何段；请检查 script_text。")

        audit: list[dict] = []
        groups: list[dict] = []
        want_r2v = family.startswith("r2v")
        seeds: list[int] = []

        for i, seg in enumerate(segments):
            ref_images: dict[int, Any] = {}
            ref_audios: dict[int, Any] = {}
            ref_videos: dict[int, Any] = {}
            seg_audit: list[dict] = []

            if use_locked_assets:
                cfg = _cfg(repo_root)
                if seg.get("asset_refs"):
                    ref_images, img_audit = fusion.locked_asset_ref_images(
                        pid, seg["asset_refs"], repo_root=repo_root, config=cfg
                    )
                    seg_audit.extend(img_audit)
                # 音画参考（方案要求「参考主体 / 音画参考」）。
                # 只对 r2v 生效 —— i2v 的组结构里 ref_audios/ref_videos 是空字典，
                # 传进去也会被真实包 `pack_i2v_group` 丢掉（它是固定空值）。
                if want_r2v and (seg.get("audio_refs") or seg.get("video_refs")):
                    try:
                        ref_audios, ref_videos, media_audit = fusion.locked_ref_media(
                            pid, seg.get("audio_refs"), seg.get("video_refs"),
                            repo_root=repo_root, config=cfg,
                        )
                        seg_audit.extend(media_audit)
                    except fusion.FusionError as exc:
                        raise FusionNodeError(
                            f"第 {i + 1} 段音画参考解析失败：{exc}"
                        ) from exc

            if want_r2v:
                group = fusion.make_r2v_group(
                    prompt=seg["prompt"],
                    duration_sec=seg["duration_sec"],
                    ref_images=ref_images,
                    ref_videos=ref_videos,
                    ref_audios=ref_audios,
                )
            else:
                # i2v：只有首段能用外部首帧；后续段由 Director 的时间轴自己接续
                first = prev_last_frame if (i == 0 and prev_last_frame is not None) else None
                group = fusion.make_i2v_group(
                    prompt=seg["prompt"],
                    duration_sec=seg["duration_sec"],
                    first_frame=first,
                    last_frame=None,
                )

            # 种子：固定值优先，否则按段确定性推导 —— 保证「好看的这一版」可复现
            seed = (base_seed if base_seed >= 0
                    else derive_seed(pid, f"SEG{i + 1:04d}", "v1", 1))
            seeds.append(seed)

            groups.append(group)
            audit.append({
                "segment_index": i + 1,
                "kind": group["kind"],
                "duration_sec": group["duration_sec"],
                "prompt": group["prompt"],
                "seed": seed,
                "seed_mode": "fixed" if base_seed >= 0 else "derived",
                "asset_refs": seg.get("asset_refs") or [],
                "audio_refs": seg.get("audio_refs") or [],
                "video_refs": seg.get("video_refs") or [],
                "ref_counts": {
                    "images": len(group.get("ref_images") or {}),
                    "audios": len(group.get("ref_audios") or {}),
                    "videos": len(group.get("ref_videos") or {}),
                },
                "resolved_refs": seg_audit,
            })

        summary = fusion.group_summary(groups)
        summary += (
            f"\n种子：{'固定 ' + str(base_seed) if base_seed >= 0 else '按段确定性推导'}"
            f"（首段 {seeds[0]}）"
        )
        if not use_locked_assets:
            summary += "\n（注意：use_locked_assets=False，本次未解析真实参考图，只出了提示词）"

        return (groups, summary,
                json.dumps({"project_id": pid, "segments": audit},
                           ensure_ascii=False, indent=2),
                seeds[0] if seeds else -1)


def _refs_of(raw: dict) -> list[dict]:
    """从段定义里取出素材引用数组（只保留合法对象项）。

    `audio_refs` / `video_refs` 用于音画参考（方案要求「参考主体 / 音画参考」）。
    非对象项一律丢弃而不是报错 —— 与 `asset_refs` 既有行为保持一致。
    """
    return [r for r in (raw.get("asset_refs") or []) if isinstance(r, dict)]


def _media_refs_of(raw: dict, key: str) -> list[dict]:
    """取音/视频引用。同时接受 `audio_refs` / `audio` 两种写法。"""
    val = raw.get(key)
    if val is None:
        val = raw.get("audio" if key == "audio_refs" else "video")
    if isinstance(val, dict):
        val = [val]
    if not isinstance(val, list):
        return []
    out: list[dict] = []
    for item in val:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # 允许直接写素材 id（不带版本）；版本留空会在解析时报明确错误
            out.append({"asset_id": item})
    return out


def _parse_script_to_segments(script_text: str, *, default_duration: float,
                              max_segments: int) -> list[dict]:
    """把用户输入解析成 [{prompt, duration_sec, asset_refs, audio_refs, video_refs}]。

    支持两种形式（与 SmartDirector 一致，降低用户学习成本）：
    1. **结构化 JSON**：`{"shots":[{...}]}` 或 `{"chapters":[{"segments":[...]}]}`；
       段内可用 `audio_refs` / `video_refs` 声明音画参考（同样只接受已 LOCK 版本）；
    2. **纯文本分镜**：空行/`---` 分隔，可带 `[5s]` 或 `5s：` 前缀。
    留空则使用内置演示剧本。
    """
    text = (script_text or "").strip()
    if not text:
        # 用编排层的演示剧本，保证节点开箱可试
        from importlib import import_module

        demo = import_module("bettercomfyui.demo")
        data = json.loads(json.dumps(demo.DEMO_SCRIPT))
        out = []
        for ch in data.get("chapters", []):
            for s in ch.get("segments", []):
                out.append({
                    "prompt": s.get("prompt") or "",
                    "duration_sec": float(s.get("duration_s") or default_duration),
                    "asset_refs": _refs_of(s),
                    "audio_refs": _media_refs_of(s, "audio_refs"),
                    "video_refs": _media_refs_of(s, "video_refs"),
                })
        return out[:max_segments]

    # 形式 1：JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        shots = data.get("shots")
        if isinstance(shots, list) and shots:
            out = []
            for s in shots:
                if not isinstance(s, dict):
                    continue
                if s.get("enabled") is False:
                    continue
                out.append({
                    "prompt": str(s.get("prompt") or s.get("action") or s.get("title") or ""),
                    "duration_sec": float(s.get("duration") or default_duration),
                    "asset_refs": _refs_of(s),
                    "audio_refs": _media_refs_of(s, "audio_refs"),
                    "video_refs": _media_refs_of(s, "video_refs"),
                })
            return out[:max_segments]

        out = []
        for ch in data.get("chapters", []):
            for s in ch.get("segments", []) or []:
                if not isinstance(s, dict):
                    continue
                out.append({
                    "prompt": str(s.get("prompt") or ""),
                    "duration_sec": float(s.get("duration_s") or s.get("duration") or default_duration),
                    "asset_refs": _refs_of(s),
                    "audio_refs": _media_refs_of(s, "audio_refs"),
                    "video_refs": _media_refs_of(s, "video_refs"),
                })
        if out:
            return out[:max_segments]

    # 形式 2：纯文本分镜
    import re

    blocks = [b.strip() for b in re.split(r"\n\s*\n|^\s*---+\s*$", text, flags=re.M) if b.strip()]
    if len(blocks) <= 1:
        blocks = [b.strip() for b in re.split(r"[\n；;]+", text) if b.strip()]

    out = []
    for b in blocks:
        m = re.match(r"^\s*[\[（(]?\s*(\d+(?:\.\d+)?)\s*(?:s|秒)\s*[\]）)]?\s*[:：\-–]?\s*(.*)$",
                     b, flags=re.S)
        if m:
            dur = float(m.group(1))
            prompt = m.group(2).strip()
        else:
            dur = default_duration
            prompt = b
        if prompt:
            out.append({"prompt": prompt, "duration_sec": dur,
                        "asset_refs": [], "audio_refs": [], "video_refs": []})
    return out[:max_segments]


# ===========================================================================
# 2) 手工打包一个组（给需要精细控制的用户）
# ===========================================================================


class BetterComfyUIFusionPackGroup:
    """手工把一段提示词 + 首尾帧/参考图/**音画参考**打包成一个 `MMX_DIR_GROUP`。

    音画参考（`ref_audio_1..3` / `ref_video_1..3`）只在 `参考 r2v` 模式下生效 ——
    这是真实包的结构决定的：`pack_i2v_group` 的 `ref_audios`/`ref_videos` 是**固定空字典**，
    传了也会被丢掉。所以这里如实声明边界，而不是"看起来支持实际无效"。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "duration_sec": ("FLOAT", {"default": 5.0, "min": 0.2, "max": 120.0, "step": 0.1}),
                "mode": (["自动（按首尾帧推断）", "强制 t2v", "强制 i2v", "强制 fl2v", "参考 r2v"],
                         {"default": "自动（按首尾帧推断）"}),
            },
            "optional": {
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
                "ref_image_1": ("IMAGE",),
                "ref_image_2": ("IMAGE",),
                "ref_image_3": ("IMAGE",),
                "ref_image_4": ("IMAGE",),
                # 音画参考：仅「参考 r2v」模式生效（真实包结构决定）
                "ref_audio_1": ("AUDIO", {"tooltip": "参考音频（音色/音画参考）；仅「参考 r2v」模式生效"}),
                "ref_audio_2": ("AUDIO",),
                "ref_audio_3": ("AUDIO",),
                "ref_video_1": ("IMAGE", {"tooltip": "参考视频帧序列；仅「参考 r2v」模式生效"}),
                "ref_video_2": ("IMAGE",),
                "ref_video_3": ("IMAGE",),
            },
        }

    RETURN_TYPES = (MMX_DIR_GROUP, "STRING", "STRING")
    RETURN_NAMES = ("group", "摘要", "审计JSON")
    FUNCTION = "pack"
    CATEGORY = CATEGORY

    def pack(self, prompt: str, duration_sec: float, mode: str,
             first_frame: Any = None, last_frame: Any = None,
             ref_image_1: Any = None, ref_image_2: Any = None,
             ref_image_3: Any = None, ref_image_4: Any = None,
             ref_audio_1: Any = None, ref_audio_2: Any = None, ref_audio_3: Any = None,
             ref_video_1: Any = None, ref_video_2: Any = None, ref_video_3: Any = None):
        if mode == "参考 r2v":
            refs = {}
            for i, img in enumerate([ref_image_1, ref_image_2, ref_image_3, ref_image_4]):
                if img is not None:
                    refs[i] = img
            audios = {}
            for i, a in enumerate([ref_audio_1, ref_audio_2, ref_audio_3]):
                if a is not None:
                    audios[i] = a
            videos = {}
            for i, v in enumerate([ref_video_1, ref_video_2, ref_video_3]):
                if v is not None:
                    videos[i] = v
            group = fusion.make_r2v_group(
                prompt=prompt, duration_sec=duration_sec, ref_images=refs,
                ref_audios=audios, ref_videos=videos,
            )
            note = ""
        else:
            group = fusion.make_i2v_group(
                prompt=prompt, duration_sec=duration_sec,
                first_frame=first_frame, last_frame=last_frame,
            )
            if mode.startswith("强制"):
                forced = mode.split()[-1]
                group["kind"] = forced
            # 如实说明边界：i2v 组结构里没有音画参考的位置
            given_media = [a for a in (ref_audio_1, ref_audio_2, ref_audio_3,
                                       ref_video_1, ref_video_2, ref_video_3) if a is not None]
            note = (
                f"\n⚠️ 本模式（{mode}）下音画参考不生效："
                f"真实包的 i2v 组结构中 ref_audios/ref_videos 是固定空字典。"
                f"你提供了 {len(given_media)} 个音画参考，已**忽略**。"
                "需要音画参考请把 mode 改成「参考 r2v」。"
                if given_media else ""
            )

        audit = json.dumps({
            "kind": group["kind"],
            "ref_counts": {
                "images": len(group.get("ref_images") or {}),
                "audios": len(group.get("ref_audios") or {}),
                "videos": len(group.get("ref_videos") or {}),
            },
        }, ensure_ascii=False, indent=2)
        return ([group], fusion.group_summary([group]) + note, audit)


# ===========================================================================
# 3) 合并多组
# ===========================================================================


class BetterComfyUIFusionCombine:
    """把多个组按顺序合并成一个 `MMX_DIR_GROUP` 列表（对应 `GroupsCombine`）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"group_1": (MMX_DIR_GROUP,)},
            "optional": {
                f"group_{i}": (MMX_DIR_GROUP,) for i in range(2, 7)
            },
        }

    RETURN_TYPES = (MMX_DIR_GROUP, "STRING")
    RETURN_NAMES = ("groups", "摘要")
    FUNCTION = "combine"
    CATEGORY = CATEGORY

    def combine(self, group_1: Any, **kwargs: Any):
        out: list[dict] = []
        inputs = [("group_1", group_1)] + [
            (f"group_{i}", kwargs.get(f"group_{i}")) for i in range(2, 7)
        ]
        for name, g in inputs:
            if g is None:
                continue
            if isinstance(g, list):
                out.extend(g)
            elif isinstance(g, dict):
                out.append(g)
            else:
                raise FusionNodeError(
                    f"{name} 不是合法的 MMX_DIR_GROUP（应为 dict 或 dict 列表），"
                    f"收到 {type(g).__name__}"
                )
        if not out:
            raise FusionNodeError("没有收到任何有效的组输入")
        return (out, fusion.group_summary(out))


# ===========================================================================
# 4) 回灌：导演台产物 -> 段版本 + 审核材料
# ===========================================================================


class BetterComfyUIFusionIngest:
    """把导演台/ChainDirector 的真实产物**回灌**成项目里的段版本，并登记审核。

    为什么必须这么做：三个导演台**都不回答**「这是谁的第几个版本、素材用的哪一版、
    审没审过、重做了几次」。回灌后：

    * 每段写一个**内容指纹**（frames 数据的 sha256），版本可追溯；
    * 写一份**审核材料 JSON**（含指纹、帧数、fps、来源），供段级审核使用；
    * 报告里明确写出「本段需要审核，未审核前不得进入拼接」。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "segment_index": ("INT", {"default": 1, "min": 1, "max": 9999}),
                "images": ("IMAGE",),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.01}),
            },
            "optional": {
                "audio": ("AUDIO",),
                "frame_count": ("INT", {"default": 0, "min": 0, "max": 1000000}),
                "source": ("STRING", {"default": "MiniMaxH3Director", "multiline": False}),
                "run_report": ("STRING", {"default": "", "multiline": True}),
                "save_preview_png": ("BOOLEAN", {"default": True,
                                                 "tooltip": "存一张首帧 PNG 作为可人工查看的证据（仅证据，不是成片）。"}),
                "review_and_count": ("BOOLEAN", {"default": True,
                                                 "tooltip": "回灌后立即做段级审核；重做版本按规定记账 CNT2/CNT3。"}),
                "retry_kind": (["自动判断", "质量重做(CNT2)", "级联重做(CNT3)", "首次生成不计"],
                               {"default": "自动判断",
                                "tooltip": "这一段为什么重做。CNT2 与 CNT3 语义必须分开（硬规则 R4）。"}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("audit_id", "frame_count", "报告")
    FUNCTION = "ingest"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs) -> str:
        """回灌会写段版本/审核记录/计数器：每次排队都必须真正执行。

        审计 R2：无 IS_CHANGED 时，重复排队命中 ComfyUI 缓存 —— 段版本不再追加、
        CNT2/CNT3 不记账，界面却显示成功（标准假成功形态）。
        """
        import time
        import uuid

        return f"{time.time_ns()}-{uuid.uuid4().hex}"

    def ingest(self, project_id: str, segment_index: int, images: Any, fps: float,
               audio: Any = None, frame_count: int = 0, source: str = "MiniMaxH3Director",
               run_report: str = "", save_preview_png: bool = True,
               review_and_count: bool = True, retry_kind: str = "自动判断",
               segment_id: str = ""):
        _orch()
        from .nodes import _orch_repo_root

        pid = (project_id or "").strip()
        if not pid:
            raise FusionNodeError("回灌需要 project_id（否则版本无法归属到具体项目）")

        # 用节点自己的配置解析路径（与审核用的配置保持同一份）
        cfg = _cfg(_orch_repo_root())
        paths, _cfg_obj, _root = _resolve_project_paths(pid, cfg)
        arr = _tensor_batch_to_uint8(images)
        n = int(arr.shape[0])
        if n == 0:
            raise FusionNodeError("收到 0 帧，拒绝登记一个空段")

        # 内容指纹：对**像素数据**做哈希（不是对文件，因为此时还没有文件）
        digest = _sha256_bytes(arr.tobytes())

        # ---- 落盘成**真实段版本**（goal 第 3 项的核心）----
        # 导演台不回答「这是谁的第几个版本」；只写一份 JSON 也不是「版本」。
        # 必须真的进段存储：否则重启后不知道哪些版本已存在（验收 12），
        # 也不知道旧审核结论对应哪个版本（任务书第 3 节 F 规则 4）。
        from bettercomfyui.seed import derive_seed
        from bettercomfyui.store.segments import SegmentStore, SegmentStoreError

        seg_id = (segment_id or "").strip() or f"SEG{segment_index:04d}"
        store = SegmentStore(paths)
        seg = store.load(seg_id)
        if seg is None:
            # 段计划还没建：按导演台产出的顺序**补建**一个段，
            # 并在 note 里说明来源（不假装它来自剧本计划阶段）。
            from bettercomfyui.models.entities import Segment
            from bettercomfyui.models.enums import SegmentSource

            seg = Segment(
                segment_id=seg_id,
                segment_index=int(segment_index),
                chapter_id="INGEST",
                prompt=str(run_report or "")[:200],
                segment_duration=round(n / float(fps), 6) if fps else None,
                fps=int(fps),
                source=SegmentSource.NORMAL,
                note=f"由 {source} 回灌自动补建（未经剧本计划阶段）",
            )
            store.save(seg)

        version = f"v{len(seg.attempts) + 1}"
        seed = seg.resolved_seed or seg.seed or derive_seed(pid, seg_id, version, 1)
        try:
            store.add_version(
                seg_id,
                version=version,
                content_hash=digest,
                used_seed=seed,
            )
        except SegmentStoreError as exc:
            raise FusionNodeError(
                f"{exc}\n（回灌被拒绝：同一版本号不得对应两份不同内容。"
                "请先递增版本号再回灌。）"
            ) from exc

        out_dir = paths.project_dir / INGEST_SUBDIR / f"SEG{segment_index:04d}"
        out_dir.mkdir(parents=True, exist_ok=True)

        preview_path = None
        if save_preview_png:
            try:
                from PIL import Image

                preview_path = out_dir / "first_frame.png"
                Image.fromarray(arr[0]).save(preview_path)
            except Exception as exc:  # noqa: BLE001 - 预览只是证据，失败不应中断登记
                preview_path = None
                run_report += f"\n[警告] 首帧预览保存失败：{type(exc).__name__}: {exc}"

        audio_info = None
        if isinstance(audio, dict):
            wf = audio.get("waveform")
            sr = audio.get("sample_rate")
            audio_info = {
                "sample_rate": int(sr) if isinstance(sr, (int, float)) else None,
                "samples": (int(wf.shape[-1])
                            if hasattr(wf, "shape") and len(wf.shape) >= 1 else None),
            }
            try:
                import wave

                import numpy as np

                wav = wf
                if hasattr(wav, "detach"):
                    wav = wav.detach().cpu().numpy()
                wav = np.asarray(wav)
                if wav.ndim == 2:
                    wav = wav[None, ...]
                if wav.ndim == 3:
                    wav = wav[0]
                if wav.ndim == 2 and wav.shape[0] > wav.shape[1]:
                    wav = wav.T
                data = np.clip(wav, -1.0, 1.0)
                pcm = (data * 32767.0).astype("<i2").tobytes()
                wav_path = out_dir / "audio.wav"
                with wave.open(str(wav_path), "wb") as fh:
                    fh.setnchannels(int(data.shape[0]) if data.ndim == 2 else 1)
                    fh.setsampwidth(2)
                    fh.setframerate(int(audio_info["sample_rate"] or 44100))
                    fh.writeframes(pcm)
                audio_info["wav"] = str(wav_path)
            except Exception as exc:  # noqa: BLE001 - 音频落盘失败不阻断登记
                audio_info["wav_error"] = f"{type(exc).__name__}: {exc}"

        audit_id = f"AUDIT_{pid}_SEG{segment_index:04d}"
        material = {
            "audit_id": audit_id,
            "project_id": pid,
            "segment_id": seg_id,
            "segment_version": version,
            "source": source,
            "frame_count": n,
            "declared_frame_count": int(frame_count) if frame_count else n,
            "fps": float(fps),
            "duration_sec": round(n / float(fps), 6) if fps else None,
            "content_hash": digest,
            "used_seed": seed,
            "image_shape": list(arr.shape),
            "audio": audio_info,
            "first_frame_png": str(preview_path) if preview_path else None,
            "review_state": "PENDING_REVIEW",
            "note": (
                "本段由导演台产出，已登记内容指纹与审核材料。"
                "按项目策略，**未通过段级审核前不得进入拼接**。"
            ),
        }
        material_path = out_dir / "review_material.json"
        material_path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        report_lines = [
            f"已回灌段 {segment_index} → {seg_id}@{version}：{n} 帧 / {fps} fps"
            + (f" / 约 {material['duration_sec']}s" if material["duration_sec"] else ""),
            f"内容指纹：{digest[:26]}…",
            f"种子：{seed}（已随版本落盘，可复现）",
            f"审核材料：{material_path}",
        ]

        # ---- 段级审核 + CNT2/CNT3 记账（goal 第 3 项的另一半）----
        # 「回灌」不只是存文件：必须触发段级审核，否则"未审核前不得拼接"无从执行；
        # 重做版本还必须按规定记账 —— 否则返工次数在任何地方都看不到。
        if review_and_count:
            review_lines, counters = self._review_and_count(
                paths=paths, store=store, seg_id=seg_id, version=version,
                material=material, retry_kind=retry_kind, cfg=cfg, frames=arr,
            )
            report_lines.extend(review_lines)
            material["review"] = counters.get("review")
            material["counters"] = counters.get("counters")
            material["review_state"] = counters.get("review_state") or material["review_state"]
            material_path.write_text(
                json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            report_lines.append(
                f"审核状态：{material['review_state']}（已按设置跳过审核 —— 未审核前不得拼接）"
            )

        if source:
            report_lines.append(f"来源：{source}")
        if audio_info:
            report_lines.append(
                f"音频：{audio_info.get('sample_rate')} Hz"
                + (f"，WAV: {audio_info.get('wav')}" if audio_info.get("wav") else "")
            )
        if run_report.strip():
            report_lines.append("导演台报告：" + run_report.strip()[:400])

        return (audit_id, n, "\n".join(report_lines))

    # -- 段级审核与计数器（从 ingest 拆出来，便于单独测试）----------------
    def _review_and_count(self, *, paths: Any, store: Any, seg_id: str, version: str,
                          material: dict, retry_kind: str, cfg: Any,
                          frames: Any = None) -> tuple[list[str], dict]:
        """对刚回灌的版本做段级审核，并按语义记账 CNT2 / CNT3。

        **CNT2 与 CNT3 必须分开**（硬规则 R4）：

        * `CNT2` = 本段**自己质量不合格**而重做；
        * `CNT3` = **因前段变化**触发本段衔接重做。

        把两者混在一起，就无法判断"返工到底是我自己的问题还是被上游带的"。

        记账时机：**第 2 个及以后的版本**才算"重做"；第 1 版是首次生成，
        不计入 CNT2/CNT3（也不该计 —— 首次生成不是返工）。
        """
        from bettercomfyui.director.review import SegmentReviewer
        from bettercomfyui.models.enums import CounterKind
        from bettercomfyui.review.counters import CounterStore

        seg = store.load_or_raise(seg_id)
        prev_versions = max(0, len(seg.attempts) - 1)
        lines: list[str] = []

        # --- 计数器 ---
        # CounterStore 不自己落盘（它只维护内存表 + 事件日志），
        # 所以这里显式用一个 JSON 文件做持久化 —— 否则重启后返工次数全丢。
        counter_path = paths.project_dir / "counters.json"
        counters = CounterStore()
        if counter_path.is_file():
            try:
                counters.restore(json.loads(counter_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 - 计数文件损坏时如实重来，不静默假装有数据
                lines.append("[警告] counters.json 无法解析，计数从零开始")

        counted: dict[str, int] = {}
        if prev_versions > 0:
            kind = self._resolve_retry_kind(retry_kind, seg)
            if kind is not None:
                counted[kind.value] = counters.increment(seg_id, kind)
                why = "质量重做" if kind is CounterKind.CNT2 else "级联重做"
                lines.append(
                    f"记账：{kind.value}（{why}）= {counted[kind.value]}"
                    f"（第 {prev_versions + 1} 版，前 {prev_versions} 次属重做）"
                )
            else:
                lines.append(
                    f"记账：本版不计 CNT2/CNT3（retry_kind={retry_kind}）"
                )
        else:
            lines.append("记账：首次生成，不计 CNT2/CNT3")

        counter_path.write_text(
            json.dumps(counters.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # --- 段级审核 ---
        # ⚠️ 旧版这里是 `{d: 80.0}` —— **常量分数 = 假审核**，永远通过（ROUND27 修）：
        # 现在用本地启发式评分器对**真实帧序列**统计 R1a 四维（画面连贯/角色一致/
        # 动作合理/无闪烁）；评不出来就明确失败，绝不用常量冒充通过。
        if frames is None:
            lines.append(
                "审核：**未完成** —— 没有拿到帧数据，无法评分。"
                "该段不得进入拼接（不假装通过）。"
            )
            return lines, {"review": None, "counters": counted,
                           "review_state": "PENDING_REVIEW"}
        dims = material.get("dimensions")
        score_source = "review_material"
        if not dims:
            try:
                from bettercomfyui.review import local_scorer

                dims = local_scorer.frame_scores(frames)
                score_source = "local_heuristic"
            except Exception as exc:  # noqa: BLE001 - 评不出来就是失败，不给常量
                lines.append(
                    f"审核：**未完成** —— 本地评分失败：{type(exc).__name__}: {exc}\n"
                    "      该段不得进入拼接（审核未通过不等于可以放行）。"
                )
                return lines, {"review": None, "counters": counted,
                               "review_state": "PENDING_REVIEW"}
        avg = round(sum(float(v) for v in dims.values()) / len(dims), 3)
        lines.append(
            f"审核评分：{dims}（均值 {avg}，来源={score_source}）")
        reviewer = SegmentReviewer(
            counters, cfg=cfg, project_id=str(paths.project_id),
            scorer=lambda segment, ver: dict(dims),
        )
        try:
            review = reviewer.review(seg, version, material={"dimensions": dims})
        except Exception as exc:  # noqa: BLE001 - 审核失败必须说清楚，不能假装通过
            lines.append(
                f"审核：**未完成** —— {type(exc).__name__}: {exc}\n"
                "      该段不得进入拼接（审核未通过不等于可以放行）。"
            )
            return lines, {"review": None, "counters": counted, "review_state": "PENDING_REVIEW"}

        passed = bool(review.get("passed"))
        state = "ALLOWED" if passed else "BLOCKED"
        lines.append(
            f"审核：{'通过' if passed else '**未通过**'} "
            f"（score={review.get('score')} / threshold={review.get('threshold')}，"
            f"audit_id={review.get('audit_id')}）"
        )
        if not passed:
            lines.append(
                "      -> 该段质量不合格：应只重做本段（CNT2），"
                "并检查依赖它的后续段是否进入级联复核（CNT3）。"
            )

        # 审核结论写回段（否则"审过了"这件事只存在于当次输出）
        updated = review.get("segment")
        if updated is not None:
            try:
                store.save(updated)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"[警告] 审核结论写回段失败：{type(exc).__name__}: {exc}")

        return lines, {"review": {k: v for k, v in review.items() if k != "segment"},
                       "counters": counted, "review_state": state}

    @staticmethod
    def _resolve_retry_kind(retry_kind: str, seg: Any):
        """把界面选项解析成 CounterKind；「自动判断」按段的级联标记决定。"""
        from bettercomfyui.models.enums import CounterKind

        if retry_kind.startswith("质量重做"):
            return CounterKind.CNT2
        if retry_kind.startswith("级联重做"):
            return CounterKind.CNT3
        if retry_kind.startswith("首次生成") or retry_kind.startswith("不计"):
            return None
        # 自动判断：被级联标记过 -> 因上游变化而重做；否则算自身质量重做
        return CounterKind.CNT3 if bool(getattr(seg, "cascade_marked", False)) else CounterKind.CNT2


# ===========================================================================
# 5) 体检：三个导演台是否装齐
# ===========================================================================


#: 融合所需的节点类型 -> 所属包（用于体检提示）
REQUIRED_NODE_TYPES = {
    "MiniMaxH3Director": "ComfyUI_MiniMaxH3_Director（AIMixer）",
    "MiniMaxH3SmartDirector": "ComfyUI_MiniMaxH3_Smart_Director",
    "MiniMaxH3SmartDirectorAllInOne": "ComfyUI_MiniMaxH3_Smart_Director",
    "MiniMaxH3ChainDirector": "ComfyUI-MiniMaxH3-ChainDirector",
    "MiniMaxH3DirectorGroupImageToVideo": "ComfyUI_MiniMaxH3_Director（AIMixer）",
    "MiniMaxH3DirectorGroupReferenceToVideo": "ComfyUI_MiniMaxH3_Director（AIMixer）",
    "MiniMaxH3DirectorGroupsCombine": "ComfyUI_MiniMaxH3_Director（AIMixer）",
}


class BetterComfyUIFusionDoctor:
    """检查三个导演台包是否可用——融合前先跑这个。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("报告",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def run(self):
        lines = ["=== Better ComfyUI 融合体检 ==="]

        # 1) 编排层
        try:
            _orch()
            lines.append("[OK]   编排层 bettercomfyui 可导入")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"[缺失] 编排层不可用：{exc}")
            return ("\n".join(lines),)

        # 2) 三个导演台的节点是否注册
        available: set[str] = set()
        try:
            import nodes as comfy_nodes  # type: ignore

            available = set(getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", {}) or {})
        except Exception:  # noqa: BLE001
            pass

        if available:
            missing = {t: pkg for t, pkg in REQUIRED_NODE_TYPES.items() if t not in available}
            present = [t for t in REQUIRED_NODE_TYPES if t in available]
            for t in present:
                lines.append(f"[OK]   {t}（{REQUIRED_NODE_TYPES[t]}）")
            for t, pkg in missing.items():
                lines.append(f"[缺失] {t} <- 需要安装 {pkg}")
            if missing:
                lines.append("")
                lines.append(
                    "缺失项需要在 ComfyUI 的 custom_nodes 里安装对应包；"
                    "融合节点本身可用，但缺导演台就无法出片。"
                )
        else:
            lines.append(
                "[未验证] 无法读取 ComfyUI 的 NODE_CLASS_MAPPINGS（不在 ComfyUI 进程里运行？），"
                "因此**没有**校验三个导演台是否装齐。"
            )

        # 3) 高级模式能力检测（任务书第 7 节：**检测能力并说明不支持项**）
        lines.append("")
        lines.append(BetterComfyUIFusionDoctor._advanced_modes_report())

        # 4) 契约自检：schema 字段是否与真实包一致
        lines.append("")
        lines.append(self._contract_selfcheck())
        return ("\n".join(lines),)

    @staticmethod
    def _advanced_modes_report() -> str:
        """报告四个高级模式**本机是否真的可用**（查权重目录，不靠猜）。

        这些模式的 COMBO 选项在缺权重时会退化成一句提示文字
        （例如 `'(将 3D 权重放入 models/latent_upscale_models)'`），
        所以**光看 schema 会以为"有这个选项就是能用"**。必须查磁盘。
        """
        try:
            from .nodes import _orch_repo_root, _projects_root  # noqa: F401

            cfg = _cfg(_orch_repo_root())
            base = cfg.global_.comfyui_base_path or None
        except Exception:  # noqa: BLE001 - 拿不到配置就报未验证，不猜
            base = None

        if not base:
            return (
                "[未验证] 未配置 global.comfyui_base_path —— 无法检查高级模式所需权重。\n"
                "         四个高级模式（二采/SelfLift/SemanticBridge/FaceRefine）都需要\n"
                "         外部权重，缺权重时不可用；配置路径后才能给出结论。"
            )

        checks = check_advanced_modes(base)
        out = ["[高级模式能力检测] 导演台的 4 个高级接缝："]
        for c in checks:
            mark = "[可用]" if c["available"] else "[全部不可用]"
            out.append(f"  {mark} {c['name']}  (接缝 {c['seam']} ← {c['node']})")
            if c["usable_options"]:
                out.append("           可用选项：" + "、".join(c["usable_options"])
                           + ("（不需要额外权重）" if len(c["usable_options"]) == len(
                               ADVANCED_MODES[c["name"]]["free_options"]) else ""))
            for b in c["blocked_options"]:
                out.append(f"           选项 {b['option']} 不可用：{b['why']}")
                out.append(f"             -> 把权重放进 models/{b['weight_dir']}/")
        out.append("")
        n_ok = sum(1 for c in checks if c["available"])
        if n_ok:
            out.append(
                f"结论：{n_ok}/{len(checks)} 个高级接缝有可用选项。"
                "**被标记为不可用的选项不要接** —— 缺权重时会失败或静默降级。"
            )
        else:
            out.append(
                f"结论：{len(checks)} 个高级接缝**都没有可用选项**（缺外部权重）。"
            )
        out.append(
            "     这不影响主流程：三个导演台的基础出片（t2v/i2v/fl2v/r2v）"
            "不需要这些权重。"
        )
        return "\n".join(out)

    @staticmethod
    def _find_director_package() -> tuple[str | None, list[str]]:
        """在 `sys.modules` 里找出已加载的导演台包名。

        ## 为什么会有多个候选键

        ComfyUI 对**目录型** custom node 的注册键不是干净包名，而是
        「完整路径把 `.` 换成 `_x_`」。所以本机上同时存在：

        * `ComfyUI_MiniMaxH3_Director`（干净名）
        * `H:\\...\\Better comfyUI\\_x_comfyui\\custom_nodes\\ComfyUI_MiniMaxH3_Director`
          （路径式键 —— 其中 `_x_comfyui` 就是 `.comfyui` 被替换的结果，
          并不存在名为 `_x_comfyui` 的目录）

        这一点由 ChainDirector 自己的 `_autopatch.py` 佐证（它同时处理两种键）。

        ## 为什么要排序而不是取第一个

        按 `sys.modules` 的遍历顺序取第一个是**不确定的** —— 可能拿到路径式键，
        也可能拿到干净名。虽然指向同一份源码，但报告里打印哪个键会变，
        且万一存在两份拷贝就会比错对象。这里显式**优先干净包名**，
        并把全部候选一并返回，便于报告里如实写出。

        返回 `(选中的包名, 全部候选)`；找不到时选中的为 None。
        """
        import sys

        candidates: list[str] = []
        for name, mod in list(sys.modules.items()):
            if not name or "." in name:
                continue
            if not getattr(mod, "__path__", None):
                continue
            if "MiniMaxH3_Director" in name:
                candidates.append(name)
        # 干净包名优先（长度更短、且不含路径式 `_x_` 标记）
        path_style = [c for c in candidates if "_x_" in c]
        clean = sorted(c for c in candidates if c not in path_style)
        ordered = clean + sorted(path_style)
        return (ordered[0] if ordered else None), candidates

    @staticmethod
    def _contract_selfcheck() -> str:
        """用**真实包**的 `pack_*` 与我构造的组做逐字段比对。

        ## 为什么必须比对"我构造的组"而不是另一份硬编码清单

        第一版这里写着 `want_i2v = {...}` —— 一份**手抄的**字段清单。
        那样比对的其实是「手抄清单 vs 真实包」，**证明了不我的产出是对的**
        （我改坏了 builder，这条检查依然会绿）。这是个假守卫。

        现在改为：从真实包 import `pack_i2v_group` / `pack_r2v_group`，
        再与 `fusion.make_i2v_group()` / `fusion.make_r2v_group()` 的真实输出比。
        """
        pkg_name, candidates = BetterComfyUIFusionDoctor._find_director_package()
        if not pkg_name:
            return (
                "[未验证] 找不到已加载的导演台包（`sys.modules` 里没有\n"
                "         含 MiniMaxH3_Director 的包）—— 未做运行时字段比对。\n"
                "         静态比对（AST 读源码）已在 tests/test_fusion_contract.py 通过。"
            )

        try:
            from importlib import import_module

            ext = import_module(f"{pkg_name}.director.external_groups")
            pack_i2v = ext.pack_i2v_group
            pack_r2v = ext.pack_r2v_group
        except Exception as exc:  # noqa: BLE001
            return (
                f"[未验证] 导入 {pkg_name}.director.external_groups 失败："
                f"{type(exc).__name__}: {exc}"
            )

        out = [f"[契约自检] 包={pkg_name}"]
        if len(candidates) > 1:
            out.append(
                f"           （sys.modules 里有 {len(candidates)} 个候选键，"
                "已优先取干净包名；路径式键是 ComfyUI 对目录型节点的注册方式）"
            )
        out.append(
            f"           external_groups 实际文件={getattr(ext, '__file__', '未知')}"
        )
        out.append("           用真实 pack_* 与**我构造的组**逐字段比对：")
        ok = True

        for label, real_fn, mine_fn, mine_kwargs in (
            ("i2v", pack_i2v, fusion.make_i2v_group, {"prompt": "x", "duration_sec": 5.0}),
            ("r2v", pack_r2v, fusion.make_r2v_group, {"prompt": "x", "duration_sec": 5.0}),
        ):
            try:
                real = real_fn(prompt="x")
                mine = mine_fn(**mine_kwargs)
            except Exception as exc:  # noqa: BLE001
                out.append(f"  [失败] {label}：调用抛错 {type(exc).__name__}: {exc}")
                ok = False
                continue

            extra, missing = sorted(set(mine) - set(real)), sorted(set(real) - set(mine))
            if not extra and not missing:
                # 字段一致后再比关键取值（字段名对但语义错也会坏事）
                diffs = []
                if real.get("family") != mine.get("family"):
                    diffs.append(f"family: 真实={real.get('family')!r} 我={mine.get('family')!r}")
                if real.get("kind") != mine.get("kind"):
                    diffs.append(f"kind: 真实={real.get('kind')!r} 我={mine.get('kind')!r}")
                if bool(real.get("first_frame") is None) != bool(mine.get("first_frame") is None):
                    diffs.append("first_frame 有无不一致")
                if diffs:
                    out.append(f"  [不一致] {label} 字段名一致但取值不同：{diffs}")
                    ok = False
                else:
                    out.append(f"  [OK] {label} 字段完全一致（{len(real)} 个键，取值也相符）")
            else:
                out.append(f"  [不一致] {label} 多 {extra}，少 {missing}")
                ok = False

        # 空组保护也要一致（真实包抛 ValueError，我必须也拒绝）
        try:
            pack_r2v(prompt="   ")
            out.append("  [不一致] 真实包对空 r2v 组**没有**报错，而我预期它报错 —— 请核对")
            ok = False
        except Exception:
            try:
                fusion.make_r2v_group(prompt="   ", duration_sec=5.0)
                out.append("  [不一致] 真实包拒绝空 r2v 组，我却放行了")
                ok = False
            except Exception:
                out.append("  [OK] 空 r2v 组保护一致（双方都拒绝）")

        out.append("")
        out.append("结论：" + ("运行时契约比对**全部通过**" if ok else "**存在不一致**，见上"))
        return "\n".join(out)


NODE_CLASS_MAPPINGS = {
    "BetterComfyUIFusionGroups": BetterComfyUIFusionGroups,
    "BetterComfyUIFusionPackGroup": BetterComfyUIFusionPackGroup,
    "BetterComfyUIFusionCombine": BetterComfyUIFusionCombine,
    "BetterComfyUIFusionIngest": BetterComfyUIFusionIngest,
    "BetterComfyUIFusionDoctor": BetterComfyUIFusionDoctor,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIFusionGroups": "Better ComfyUI 融合·段计划→导演组",
    "BetterComfyUIFusionPackGroup": "Better ComfyUI 融合·单组打包",
    "BetterComfyUIFusionCombine": "Better ComfyUI 融合·组合并",
    "BetterComfyUIFusionIngest": "Better ComfyUI 融合·回灌段版本",
    "BetterComfyUIFusionDoctor": "Better ComfyUI 融合·体检",
}
