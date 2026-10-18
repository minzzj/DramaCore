"""素材与时间轴适配节点：把编排层的东西变成导演台**能直接接的线**。

## 为什么需要这一层

三个导演台的参考图输入都是 **IMAGE 张量**，而本项目的素材是**带版本与 LOCK 的文件**。
中间的落差必须有人补上，否则用户只能"手动把图拖进来"——那就等于放弃版本管理。

```
[Better ComfyUI 素材] (asset_id+version → IMAGE)  ─┐
                                                    ├─▶ MiniMaxH3DirectorGroupReferenceToVideo.ref_image_N
[Better ComfyUI 素材·多槽] (按顺序出 1..9 张)      ─┘            │
                                                                ▼
                                                  MiniMaxH3Director.i2v_groups / r2v_groups
```

## 两条硬规则

1. **只允许已 LOCK 的版本**（R3）。未 LOCK 直接报错，不静默跳过。
2. **版本必须显式**。可以在节点上指定 `version`，留空则取"当前可用版本"
   （即 `resolve_lock` 能通过的最新一个）；报告里会写出实际用了哪个版本与指纹，
   这样成片能追溯到具体素材版本。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._bridge import BridgeError, load_orchestrator

CATEGORY = "Better ComfyUI/融合"

#: 与导演台一致的参考图槽上限
MAX_SLOTS = 9


class AssetNodeError(RuntimeError):
    """面向使用者的素材节点错误。"""


def _orch():
    try:
        return load_orchestrator()
    except BridgeError as exc:
        raise AssetNodeError(str(exc)) from exc


def _repo_root() -> Path:
    from .nodes import _orch_repo_root

    return _orch_repo_root()


def _open_store(project_id: str):
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    assets_mod = import_module("bettercomfyui.store.assets")
    cfg_mod = import_module("bettercomfyui.config")
    from .nodes import _projects_root

    repo_root = _repo_root()
    cfg = cfg_mod.load_config(repo_root=repo_root)
    root = _projects_root(cfg)
    paths = paths_mod.ProjectPaths(root=root, project_id=project_id)
    paths.ensure_layout()
    return assets_mod.AssetStore(paths), paths, cfg


def _image_tensor(file_path: Path):
    """素材文件 -> ComfyUI IMAGE 批次 [1,H,W,C] float32 0..1。"""
    try:
        import numpy as np
        import torch
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise AssetNodeError(
            f"读取素材图片需要 numpy/torch/Pillow：{type(exc).__name__}: {exc}"
        ) from exc

    try:
        with Image.open(file_path) as im:
            arr = np.asarray(im.convert("RGB")).astype("float32") / 255.0
    except Exception as exc:  # noqa: BLE001
        raise AssetNodeError(
            f"素材文件无法解码为图片：{file_path}（{type(exc).__name__}: {exc}）"
        ) from exc
    return torch.from_numpy(arr)[None, ...]


def _resolve_locked(store, asset_id: str, version: str | None) -> dict:
    """取一个**已 LOCK** 的版本；可用版本不存在时报可操作错误。"""
    aid = (asset_id or "").strip()
    if not aid:
        raise AssetNodeError("asset_id 不能为空（可用「Better ComfyUI 素材审核」节点列出候选）")

    ver = (version or "").strip()
    if ver:
        locked = store.resolve_lock(aid, ver)
        if locked is None:
            raise AssetNodeError(
                f"素材 {aid}@{ver} 没有有效 LOCK。\n"
                "请先在「Better ComfyUI 素材审核」节点里通过审核（或按项目兜底策略接受）。"
                "未审核的素材不允许进入导演台（R3）。"
            )
        return locked

    # 未指定版本 -> 取最新可用（已 LOCK）版本
    latest = store.latest_locked_version(aid)
    if not latest:
        raise AssetNodeError(
            f"素材 {aid} 没有任何已 LOCK 的版本。\n"
            "请先审核该素材取得 LOCK，或在节点上显式填写 version。"
        )
    locked = store.resolve_lock(aid, latest)
    if locked is None:  # pragma: no cover - latest_locked_version 与 resolve_lock 应一致
        raise AssetNodeError(f"素材 {aid}@{latest} 被报告为已锁定但 resolve_lock 取不到，数据不一致")
    return locked


def _locked_file(locked: dict, paths) -> Path:
    rel = locked.get("absolute_path") or locked.get("file_path")
    if not rel:
        raise AssetNodeError(f"LOCK 记录缺少文件路径（记录损坏，需人工处理）：{locked!r}")
    f = Path(rel)
    if not f.is_absolute():
        f = paths.project_dir / f
    if not f.is_file():
        raise AssetNodeError(
            f"素材文件不存在：{f}\nLOCK 记录与磁盘不一致 —— 属数据损坏，拒绝静默继续。"
        )
    # 审计 R2：只检查「文件存在」不够 —— 文件可能被替换成别的内容，
    # 而 LOCK 授权的是**指定指纹**的那份内容。这里重算磁盘指纹并比对。
    try:
        from bettercomfyui.store.assets import verify_locked_file_content

        verify_locked_file_content(locked, f)
    except AssetNodeError:
        raise
    except Exception as exc:  # noqa: BLE001 - 指纹不一致/读取失败都是明确拒绝
        raise AssetNodeError(
            f"素材内容校验失败：{type(exc).__name__}: {exc}\n"
            "LOCK 只授权其声明指纹的内容；内容被替换时必须新建版本并重新审核。"
        ) from exc
    return f


# ===========================================================================
# 单个素材 -> IMAGE
# ===========================================================================


class BetterComfyUIAssetImage:
    """把一个**已 LOCK** 的素材版本输出为 IMAGE，供导演台参考图槽使用。

    输出 `IMAGE` 直接连 `MiniMaxH3DirectorGroupReferenceToVideo.ref_image_N`，
    或连 ChainDirector 的 `image_0`…`image_8`。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "asset_id": ("STRING", {"default": "", "multiline": False,
                                        "tooltip": "素材全局 ID（如 项目名_character_1_ab12cd34）"}),
            },
            "optional": {
                "version": ("STRING", {"default": "",
                                       "tooltip": "留空 = 取最新已 LOCK 版本；填了就必须是已 LOCK 的版本"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("image", "素材信息", "审计JSON")
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, project_id: str, asset_id: str, version: str = ""):
        _orch()
        pid = (project_id or "").strip()
        if not pid:
            raise AssetNodeError("需要 project_id（否则素材归属不明确）")

        store, paths, _cfg = _open_store(pid)
        locked = _resolve_locked(store, asset_id, version)
        f = _locked_file(locked, paths)
        img = _image_tensor(f)

        audit = {
            "project_id": pid,
            "asset_id": locked.get("asset_id"),
            "version": locked.get("version"),
            "lock_id": locked.get("lock_id"),
            "lock_source": locked.get("lock_source"),
            "content_hash": locked.get("content_hash"),
            "file": str(f),
            "shape": list(img.shape),
        }
        info = (
            f"素材 {audit['asset_id']}@{audit['version']}  "
            f"LOCK={audit['lock_source']}  {img.shape[1]}x{img.shape[2]}  "
            f"指纹={str(audit['content_hash'])[:22]}…"
        )
        return (img, info, json.dumps(audit, ensure_ascii=False, indent=2))


# ===========================================================================
# 一个段的全部素材 -> 多槽 IMAGE
# ===========================================================================


class BetterComfyUIAssetSlots:
    """按段的 `asset_refs` 顺序，把多个已 LOCK 素材输出成固定 9 个 IMAGE 槽。

    固定槽位是为了**能直接连线**（ComfyUI 需要有确定的输出口）。
    未使用的槽输出空白图（不是 None），因为下游 `ref_image_N` 拿到 None 会跳过，
    而"跳过"会让用户误以为连上了却没生效。空槽用一个显式的灰色占位，
    并在报告里写明"该槽为空"。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "asset_refs_json": ("STRING", {"default": "[]", "multiline": True,
                                               "tooltip": '形如 [{"asset_id":"...","version":"v1"}, ...]，最多 9 项'}),
            },
        }

    RETURN_TYPES = ("IMAGE",) * MAX_SLOTS + ("STRING", "STRING")
    RETURN_NAMES = tuple(f"图{i + 1}" for i in range(MAX_SLOTS)) + ("槽位报告", "审计JSON")
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, project_id: str, asset_refs_json: str):
        _orch()
        pid = (project_id or "").strip()
        if not pid:
            raise AssetNodeError("需要 project_id")

        try:
            refs = json.loads(asset_refs_json or "[]")
        except json.JSONDecodeError as exc:
            raise AssetNodeError(
                f"asset_refs_json 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from exc
        if not isinstance(refs, list):
            raise AssetNodeError("asset_refs_json 必须是数组")
        if len(refs) > MAX_SLOTS:
            raise AssetNodeError(
                f"素材引用 {len(refs)} 项超过槽位上限 {MAX_SLOTS}；请拆成多段或用「素材」单节点手动连。"
            )

        store, paths, _cfg = _open_store(pid)

        blank = None
        outs: list[Any] = []
        audit: list[dict] = []
        lines: list[str] = []

        for i in range(MAX_SLOTS):
            if i < len(refs):
                ref = refs[i]
                if not isinstance(ref, dict):
                    raise AssetNodeError(f"asset_refs[{i}] 不是对象：{ref!r}")
                locked = _resolve_locked(store, ref.get("asset_id"), ref.get("version"))
                f = _locked_file(locked, paths)
                outs.append(_image_tensor(f))
                audit.append({
                    "slot": i + 1,
                    "asset_id": locked.get("asset_id"),
                    "version": locked.get("version"),
                    "lock_source": locked.get("lock_source"),
                    "content_hash": locked.get("content_hash"),
                    "file": str(f),
                })
                lines.append(f"图{i + 1} <- {locked.get('asset_id')}@{locked.get('version')}")
            else:
                if blank is None:
                    import torch

                    blank = torch.zeros((1, 64, 64, 3), dtype=torch.float32) + 0.5
                outs.append(blank)
                lines.append(f"图{i + 1} = 空槽（未使用）")

        report = "\n".join(lines)
        return tuple(outs) + (report, json.dumps({"project_id": pid, "slots": audit},
                                                 ensure_ascii=False, indent=2))


# ===========================================================================
# 素材 -> 音频 / 视频参考（音画参考链路）
# ===========================================================================


class BetterComfyUIAssetMedia:
    """把**已 LOCK** 的音频或视频素材输出为导演台能接的参考输入。

    为什么需要它：`MiniMaxH3Director` 的 r2v 组支持
    `ref_audios` / `ref_videos`（音色参考、参考视频），
    但素材库里的音频/视频是**带版本与 LOCK 的文件**，中间必须有人转换。

    输出类型与 ComfyUI 原生一致（`AUDIO` / `IMAGE`），
    可直接接「融合·单组打包」的 `ref_audio_N` / `ref_video_N`。

    硬规则（R3）：只允许已 LOCK 的版本；未 LOCK **直接报错**，不静默跳过。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_id": ("STRING", {"default": "", "multiline": False}),
                "asset_id": ("STRING", {"default": "", "multiline": False}),
                "kind": (["音频", "视频"], {"default": "音频"}),
            },
            "optional": {
                "version": ("STRING", {"default": "",
                                       "tooltip": "留空 = 取最新已 LOCK 版本"}),
            },
        }

    RETURN_TYPES = ("AUDIO", "IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("audio", "video_frames", "素材信息", "审计JSON")
    FUNCTION = "load"
    CATEGORY = CATEGORY

    def load(self, project_id: str, asset_id: str, kind: str, version: str = ""):
        _orch()
        pid = (project_id or "").strip()
        if not pid:
            raise AssetNodeError("需要 project_id（否则素材归属不明确）")

        store, paths, _cfg = _open_store(pid)
        locked = _resolve_locked(store, asset_id, version)
        f = _locked_file(locked, paths)

        audit = {
            "project_id": pid,
            "asset_id": locked.get("asset_id"),
            "version": locked.get("version"),
            "lock_source": locked.get("lock_source"),
            "content_hash": locked.get("content_hash"),
            "file": str(f),
            "kind": kind,
        }
        info = (
            f"素材 {audit['asset_id']}@{audit['version']}  "
            f"LOCK={audit['lock_source']}  类型={kind}  {f.name}"
        )

        if kind == "音频":
            from .fusion import load_audio_via_comfy

            audio = load_audio_via_comfy(f)
            wf = audio.get("waveform")
            audit["sample_rate"] = audio.get("sample_rate")
            audit["samples"] = int(wf.shape[-1]) if hasattr(wf, "shape") else None
            # 视频口没有内容：返回一张 1x1 占位而不是 None ——
            # ComfyUI 的 IMAGE 口不接受 None，且下游拿到 None 会静默跳过（更难查）。
            import torch

            blank = torch.zeros((1, 8, 8, 3), dtype=torch.float32)
            return (audio, blank, info + f"  {audit['sample_rate']}Hz", 
                    json.dumps(audit, ensure_ascii=False, indent=2))

        from .fusion import load_video_frames_via_comfy

        frames = load_video_frames_via_comfy(f)
        audit["frame_count"] = int(frames.shape[0]) if hasattr(frames, "shape") else None
        empty_audio = {"waveform": __import__("torch").zeros(1, 1, 0), "sample_rate": 32000}
        return (empty_audio, frames, info + f"  {audit['frame_count']} 帧",
                json.dumps(audit, ensure_ascii=False, indent=2))


# ===========================================================================
# 时间轴文本（喂 ChainDirector）
# ===========================================================================


class BetterComfyUITimelineText:
    """生成 ChainDirector 需要的 `时间轴提示词` 文本，并**规避它的一个真实陷阱**。

    ## 为什么不能只写 `全局提示词`

    实测（读源码确认）ChainDirector 构造的 timeline 里没有 `commonEnabled`，
    于是依赖包里 `prompt = group_prompt or fallback_prompt` 走 else 分支：
    **只要段文本非空，`全局提示词` 就被完全丢弃**，不进入正向提示词。

    所以本节点把"全片设定"**逐段前置**到每一行时间轴文本里，
    这样设定句一定能生效 —— 而不是依赖一个会被丢掉的输入。

    ## 时间轴行格式（与 ChainDirector 的解析正则一致）

    `起始-结束[单位]: 动作文本`，分隔符支持 `-` 与 `至`/`~`，单位 `s`/`秒` 可选。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "global_setting": ("STRING", {"default": "", "multiline": True,
                                              "tooltip": "全片设定（风格/角色/画质）。会被逐段前置，避免被 ChainDirector 丢弃。"}),
                "segment_prompts": ("STRING", {"default": "", "multiline": True,
                                               "tooltip": "每行一段的动作描述；可用 [5s] 前缀指定时长。"}),
                "segment_seconds": ("FLOAT", {"default": 5.0, "min": 0.5, "max": 60.0, "step": 0.5}),
            },
            "optional": {
                "also_emit_global": ("BOOLEAN", {"default": True,
                                                 "tooltip": "同时把设定输出到「全局提示词」口（它可能被丢弃，但无害）"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("时间轴提示词", "全局提示词", "段数报告")
    FUNCTION = "build"
    CATEGORY = CATEGORY

    def build(self, global_setting: str, segment_prompts: str,
              segment_seconds: float, also_emit_global: bool = True):
        import re

        lines = [ln.strip() for ln in (segment_prompts or "").splitlines() if ln.strip()]
        if not lines:
            raise AssetNodeError(
                "segment_prompts 为空。每行写一段动作描述，例如：\n"
                "  角色从屏幕里钻出来，轻轻跳落到桌面上站定。\n"
                "  角色在桌面上走动，抬头打量零食盒。"
            )

        setting = (global_setting or "").strip()
        timeline_rows: list[str] = []
        notes: list[str] = []
        t = 0.0

        for idx, raw in enumerate(lines):
            m = re.match(r"^\s*[\[（(]?\s*(\d+(?:\.\d+)?)\s*(?:s|秒)?\s*[\]）)]?\s*[:：\-–—]?\s*(.*)$",
                         raw)
            if m and m.group(2).strip():
                dur = float(m.group(1))
                text = m.group(2).strip()
            else:
                dur = float(segment_seconds)
                text = raw

            if dur <= 0:
                raise AssetNodeError(f"第 {idx + 1} 段时长非法：{dur}")

            # 全片设定逐段前置 —— 这是本节点存在的核心原因
            full = f"{setting} {text}".strip() if setting else text
            start, end = t, t + dur
            timeline_rows.append(f"{start:g}-{end:g}s: {full}")
            t = end

        timeline = "\n".join(timeline_rows)
        total = t
        notes.append(f"共 {len(timeline_rows)} 段，总时长 {total:g}s")
        if setting:
            notes.append(
                "已把「全片设定」逐段前置到每行时间轴文本。"
                "原因：ChainDirector 在段文本非空时会丢弃「全局提示词」"
                "（其 timeline 未设置 commonEnabled，导致走 fallback 分支）。"
            )
        else:
            notes.append("未填全片设定；仅有动作描述。")
        if also_emit_global and setting:
            notes.append("同时输出了「全局提示词」口（可能被 ChainDirector 丢弃，仅作兼容）。")

        return (timeline, setting if also_emit_global else "", "\n".join(notes))


NODE_CLASS_MAPPINGS = {
    "BetterComfyUIAssetImage": BetterComfyUIAssetImage,
    "BetterComfyUIAssetSlots": BetterComfyUIAssetSlots,
    "BetterComfyUIAssetMedia": BetterComfyUIAssetMedia,
    "BetterComfyUITimelineText": BetterComfyUITimelineText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIAssetImage": "Better ComfyUI 素材·取已锁定图",
    "BetterComfyUIAssetSlots": "Better ComfyUI 素材·多槽输出(1-9)",
    "BetterComfyUIAssetMedia": "Better ComfyUI 素材·取已锁定音/视频",
    "BetterComfyUITimelineText": "Better ComfyUI 融合·时间轴文本",
}
