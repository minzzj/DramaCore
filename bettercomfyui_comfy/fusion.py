"""融合节点：把编排层的段计划/已 LOCK 素材接成导演台能吃的 `MMX_DIR_GROUP`。

**这不是「又一个执行器」，而是本项目与三个导演台包之间唯一的接缝。**

## 为什么这样做（架构依据）

`ComfyUI_MiniMaxH3_Director` 的 `MiniMaxH3Director` 节点有：

```python
RETURN_TYPES = ('IMAGE','AUDIO','FLOAT','INT','IMAGE','STRING','IMAGE','IMAGE')
RETURN_NAMES = ('images','audio','fps','frame_count','source_images','report', ...)
FUNCTION = 'execute'
```

也就是说**真实视频生成本来由它承担**。它同时留了一个可选输入：

```python
"i2v_groups": ("MMX_DIR_GROUP", {"tooltip": "External Image to Video group(s) ... "
                                           "When connected, overrides UI cards for execution "
                                           "(external priority)."})
"r2v_groups": ("MMX_DIR_GROUP", {"tooltip": "External Reference to Video group(s). ..."})
```

**「外部优先」就是官方留好的驱动接口。** 所以我们不碰采样，只产出正确的组。

## 载荷契约（逐字段来自真实源码，不是猜的）

`director/external_groups.py`：

```python
def pack_i2v_group(*, prompt='', duration_sec=..., first_frame=None, last_frame=None):
    return {'version': 1, 'family': 'i2v', 'kind': infer_i2v_kind(first, last),
            'prompt': ..., 'duration_sec': float(...),
            'first_frame': first[:1].clone() if first is not None else None,
            'last_frame':  last[:1].clone() if last  is not None else None,
            'ref_images': {}, 'ref_videos': {}, 'ref_video_audios': {}, 'ref_audios': {}}

def pack_r2v_group(*, prompt='', duration_sec=..., ref_image_size='match',
                   ref_images=None, ref_videos=None, ref_video_audios=None, ref_audios=None):
    return {'version': 1, 'family': 'r2v', 'kind': 'r2v', 'prompt': ...,
            'duration_sec': ..., 'ref_image_size': ...,
            'ref_images': {idx: tensor}, 'ref_videos': {...},
            'ref_video_audios': {...}, 'ref_audios': {...}}
```

`infer_i2v_kind(first, last)`：两者皆无 -> `t2v`；只有首帧 -> `i2v`；只有尾帧 -> `i2v`；
两者都有 -> `fl2v`。（本模块在**不导入** torch 的情况下复刻同一规则，并另有一条测试
在装有 torch 的环境里**真导入** `pack_i2v_group` 做逐字段比对，防止我的复刻漂移。）

## 与 SmartDirector 的关系

`MiniMaxH3SmartDirector` 输出的也是 `MMX_DIR_GROUP`（`_make_group()` 构造），
字段与此处一致。因此两条路可选：

* **要用 AI 分镜** -> 用 SmartDirector 的 `导演组` 输出直连 Director；
* **要用自己的项目/素材/审核** -> 用本模块的节点，把 **已 LOCK 的素材版本** 接成 `ref_images`。

两者可以并存：本模块负责「用哪一版素材、审没审过」，SmartDirector 负责「AI 怎么分镜」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._bridge import BridgeError, load_orchestrator

#: ComfyUI 自定义类型名，必须与 Director 包里的常量逐字一致
MMX_DIR_GROUP = "MMX_DIR_GROUP"

#: 组内参考槽上限（与 Director 的 MAX_REFERENCE_IMAGES 等保持一致量级；
#: 真正校验在 Director 侧，这里只是防止明显越界）
MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_AUDIOS = 3
MAX_REFERENCE_VIDEOS = 3

CATEGORY = "Better ComfyUI/融合"


class FusionError(RuntimeError):
    """面向使用者的融合错误。"""


def _orch():
    try:
        return load_orchestrator()
    except BridgeError as exc:
        raise FusionError(str(exc)) from exc


def infer_i2v_kind(first_frame: Any, last_frame: Any) -> str:
    """复刻 Director 的 `infer_i2v_kind` 判定规则（不依赖 torch）。

    `first_frame`/`last_frame` 为 None 表示未提供。
    """
    has_first = first_frame is not None
    has_last = last_frame is not None
    if has_first and has_last:
        return "fl2v"
    if has_first or has_last:
        return "i2v"
    return "t2v"


def _apply_continuity(group: dict, continuity_from_prev: bool | None) -> dict:
    """按需给组加上 `continuity_from_prev`（**可选键**，默认不产出）。

    ## 为什么这是个「可选键」而不是必填

    `pack_i2v_group` / `pack_r2v_group` 的 return 里**没有**这个键，
    我的组要与它逐字段一致 —— 所以默认**不加**。

    但 Director 确实会读它。证据链（`director/external_groups.py`）：

    ```python
    row = timeline_row_for_index(timeline, int(src_index))
    if not row and isinstance(g, dict):
        row = g                          # ← 回退到**组字典本身**
    ...
    continuity_from_prev=resolve_segment_continuity_from_prev(row, segment_index=plan_idx),
    ```

    而 `resolve_segment_continuity_from_prev` 的语义（`segment_continuity.py:210`）：

    * 第 0 段**永不**接续；
    * **缺字段默认 True**（原文：「Missing field defaults to True so existing
      workflows keep pinning every segment」）；
    * 显式给 `continuityFromPrev` / `continuity_from_prev` 才按它走。

    所以：

    * 不传（默认）-> 行为与只用 `pack_*` **完全一致**（每段都接上一段）；
    * 传 `False` -> 该段**不**接上一段（例如刻意的硬切、跳到新场景）。

    没有这个参数时，用户**无法表达"这一段不要接上一段"** —— 这是个真实的能力缺口。
    """
    if continuity_from_prev is None:
        return group
    if not isinstance(continuity_from_prev, bool):
        raise FusionError(
            f"continuity_from_prev 必须是 True/False 或 None，"
            f"收到 {type(continuity_from_prev).__name__}"
        )
    group["continuity_from_prev"] = continuity_from_prev
    return group


def make_i2v_group(
    *,
    prompt: str,
    duration_sec: float,
    first_frame: Any = None,
    last_frame: Any = None,
    continuity_from_prev: bool | None = None,
) -> dict:
    """构造一个 i2v 组，字段与 `pack_i2v_group` 逐字段一致。

    注意 `first_frame` / `last_frame` 必须是 **IMAGE 批次**（ComfyUI 里就是图像张量）。
    Director 侧会做 `_as_image_batch(...)` 并取 `[:1]`（只保留第一帧），
    所以这里原样透传即可，不做裁剪以免改变语义。

    `continuity_from_prev`（可选）：显式控制「本段是否接上一段」。
    默认 None = **不产出该键**，从而与 `pack_i2v_group` 逐字段一致；
    传 False 才会加上该键（Director 会读，见 `_apply_continuity` 的说明）。
    """
    if not isinstance(duration_sec, (int, float)) or duration_sec <= 0:
        raise FusionError(f"duration_sec 必须是正数，收到 {duration_sec!r}")
    return _apply_continuity({
        "version": 1,
        "family": "i2v",
        "kind": infer_i2v_kind(first_frame, last_frame),
        "prompt": (prompt or "").strip(),
        "duration_sec": float(duration_sec),
        "first_frame": first_frame,
        "last_frame": last_frame,
        "ref_images": {},
        "ref_videos": {},
        "ref_video_audios": {},
        "ref_audios": {},
    }, continuity_from_prev)


def make_r2v_group(
    *,
    prompt: str,
    duration_sec: float,
    ref_image_size: str = "match",
    ref_images: dict[int, Any] | None = None,
    ref_videos: dict[int, Any] | None = None,
    ref_video_audios: dict[int, Any] | None = None,
    ref_audios: dict[int, Any] | None = None,
    continuity_from_prev: bool | None = None,
) -> dict:
    """构造一个 r2v 组，字段与 `pack_r2v_group` 逐字段一致。

    空组保护：与 Director 一致，prompt 与所有参考都为空时抛错
    （否则会生成一个无意义的段）。
    """
    images = {int(k): v for k, v in (ref_images or {}).items() if v is not None}
    videos = {int(k): v for k, v in (ref_videos or {}).items() if v is not None}
    v_audios = {int(k): v for k, v in (ref_video_audios or {}).items() if v is not None}
    audios = {int(k): v for k, v in (ref_audios or {}).items() if v is not None}

    for idx in list(images):
        if not 0 <= idx < MAX_REFERENCE_IMAGES:
            raise FusionError(f"参考图槽位 {idx} 越界（允许 0..{MAX_REFERENCE_IMAGES - 1}）")
    # ⚠️ 视频与视频音轨也要查：真实包对这三类都做上限裁剪
    # （external_groups.py 里 `if 0 <= i < MAX_REFERENCE_VIDEOS: videos[i] = ...`）。
    # 之前漏了这两个循环 —— 越界的视频槽会被**静默保留**，
    # 而真实包会把它丢掉：于是"我这边看着有参考视频、实际导演台看不到"。
    # 这类不一致最难查，所以宁可在这里直接报错。
    for idx in list(videos):
        if not 0 <= idx < MAX_REFERENCE_VIDEOS:
            raise FusionError(
                f"参考视频槽位 {idx} 越界（允许 0..{MAX_REFERENCE_VIDEOS - 1}）"
            )
    for idx in list(v_audios):
        if not 0 <= idx < MAX_REFERENCE_VIDEOS:
            raise FusionError(
                f"参考视频音轨槽位 {idx} 越界（允许 0..{MAX_REFERENCE_VIDEOS - 1}）"
            )
    for idx in list(audios):
        if not 0 <= idx < MAX_REFERENCE_AUDIOS:
            raise FusionError(f"参考音频槽位 {idx} 越界（允许 0..{MAX_REFERENCE_AUDIOS - 1}）")

    if not (prompt or "").strip() and not images and not videos and not audios:
        raise FusionError(
            "参考生视频组既没有提示词也没有任何参考素材 —— "
            "Director 侧会拒绝这种空组，这里提前报错以免产生无意义的段。"
        )

    return _apply_continuity({
        "version": 1,
        "family": "r2v",
        "kind": "r2v",
        "prompt": (prompt or "").strip(),
        "duration_sec": float(duration_sec),
        "ref_image_size": ref_image_size or "match",
        # ⚠️ 这两个键**必须**存在且为 None：真实 pack_r2v_group 的 return 里
        # 同样带 "first_frame": None, "last_frame": None（external_groups.py:403-404）。
        # 由 tests/test_fusion_contract.py 的 AST 字段比对守卫；不要删。
        "first_frame": None,
        "last_frame": None,
        "ref_images": images,
        "ref_videos": videos,
        "ref_video_audios": v_audios,
        "ref_audios": audios,
    }, continuity_from_prev)


# ---------------------------------------------------------------------------
# 从项目素材库构造组
# ---------------------------------------------------------------------------


def _load_image_as_batch(file_path: Path):
    """把素材文件读成 ComfyUI 的 IMAGE 批次（[1,H,W,C] float32 0..1）。

    用 torch + PIL，与 ComfyUI 一致；失败时抛**可操作**错误，绝不返回占位图。
    """
    try:
        import numpy as np
        import torch
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise FusionError(
            f"读取参考图需要 numpy/torch/Pillow，当前环境缺少：{type(exc).__name__}: {exc}"
        ) from exc

    try:
        with Image.open(file_path) as im:
            im = im.convert("RGB")
            arr = np.asarray(im).astype("float32") / 255.0
    except Exception as exc:  # noqa: BLE001
        raise FusionError(f"参考图无法解码：{file_path}（{type(exc).__name__}: {exc}）") from exc

    return torch.from_numpy(arr)[None, ...]  # [1,H,W,C]


def locked_asset_ref_images(project_id: str, segment_asset_refs: list[dict],
                            *, repo_root: Path, config: Any = None) -> tuple[dict[int, Any], list[dict]]:
    """把段的素材引用（已 LOCK 的版本）解析成 r2v 组要的 `ref_images`。

    返回 `(ref_images, audit)`：`audit` 逐个记录「用了哪个素材的哪个版本、指纹是什么」，
    便于在报告与元数据里追溯（这就是本项目相对导演台的增量价值：**版本可追溯**）。

    硬规则（R3）：**只有取得 LOCK 的版本才允许进组。** 未 LOCK 直接抛错，
    不静默跳过 —— 跳过会让用户以为素材用上了，实际没有。
    """
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    assets_mod = import_module("bettercomfyui.store.assets")
    cfg_mod = import_module("bettercomfyui.config")

    repo_root = Path(repo_root)
    if config is None:
        config = cfg_mod.load_config(repo_root=repo_root)

    projects_root = Path(config.global_.project_root)
    if not projects_root.is_absolute():
        projects_root = repo_root / projects_root

    paths = paths_mod.ProjectPaths(root=projects_root, project_id=project_id)
    store = assets_mod.AssetStore(paths)

    ref_images: dict[int, Any] = {}
    audit: list[dict] = []
    slot = 0

    for ref in segment_asset_refs or []:
        asset_id = ref.get("asset_id")
        version = ref.get("version")
        if not asset_id or not version:
            raise FusionError(f"素材引用缺少 asset_id 或 version：{ref!r}（R3 要求必须指定版本）")

        locked = store.resolve_lock(asset_id, version)
        if locked is None:
            raise FusionError(
                f"素材 {asset_id}@{version} 没有有效 LOCK，禁止进入导演台。\n"
                "请先在「Better ComfyUI 素材审核」节点里通过审核（或按项目兜底策略接受）。"
            )

        rel = locked.get("absolute_path") or locked.get("file_path")
        if not rel:
            raise FusionError(f"LOCK 记录里没有可用文件路径：{locked!r}")
        fpath = Path(rel)
        if not fpath.is_absolute():
            fpath = paths.project_dir / fpath
        if not fpath.is_file():
            raise FusionError(
                f"素材文件不存在：{fpath}（LOCK 记录与磁盘不一致，属数据损坏，需人工处理）"
            )
        # 审计 R2：LOCK 授权的是**指定内容指纹**的那份文件；文件被替换时
        # 只查「存在」会把未授权内容送进导演台参考图（静默完整性失效）。
        try:
            assets_mod.verify_locked_file_content(locked, fpath)
        except Exception as exc:  # noqa: BLE001
            raise FusionError(
                f"素材内容与 LOCK 声明的指纹不一致，拒绝送入导演台：{exc}"
            ) from exc

        if slot >= MAX_REFERENCE_IMAGES:
            raise FusionError(
                f"参考图槽位已满（{MAX_REFERENCE_IMAGES}）—— 段 {ref!r} 无法加入。"
                "请减少该段的素材引用数。"
            )
        ref_images[slot] = _load_image_as_batch(fpath)
        audit.append({
            "slot": slot,
            "asset_id": asset_id,
            "version": version,
            "lock_source": locked.get("lock_source"),
            "content_hash": locked.get("content_hash"),
            "file": str(fpath),
        })
        slot += 1

    return ref_images, audit


def group_summary(groups: list[dict]) -> str:
    """人类可读的组摘要（给节点输出与报告用）。"""
    lines = [f"共 {len(groups)} 段"]
    for i, g in enumerate(groups):
        n_ref = len(g.get("ref_images") or {})
        n_aud = len(g.get("ref_audios") or {})
        n_vid = len(g.get("ref_videos") or {})
        extra = ""
        if n_ref:
            extra += f" 参考图{n_ref}"
        if n_aud:
            extra += f" 参考音频{n_aud}"
        if n_vid:
            extra += f" 参考视频{n_vid}"
        first = "首帧" if g.get("first_frame") is not None else ""
        last = "尾帧" if g.get("last_frame") is not None else ""
        fl = f" {first}{last}".rstrip() if (first or last) else ""
        lines.append(
            f"  [{i}] kind={g.get('kind')} {g.get('duration_sec')}s{extra}{fl} "
            f"prompt={(g.get('prompt') or '')[:28]}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 音频 / 视频参考（方案要求「参考主体 / 音画参考」）
# ---------------------------------------------------------------------------


def load_audio_via_comfy(file_path: Path) -> dict:
    """把音频文件读成 ComfyUI 的 AUDIO 字典 `{"waveform": [1,C,N], "sample_rate": int}`。

    **优先用 ComfyUI 自带的 loader**，因为不同版本的音频格式支持、采样率处理
    可能不同；只有拿不到时才用内置 wave 兜底（且仅支持 wav）。

    返回的字典结构必须与 ComfyUI 一致，否则 Director 的 `_as_audio()`
    会判定为无效并**静默跳过**该参考音 —— 那会让你以为加了参考音而实际没有。
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FusionError(f"参考音频文件不存在：{file_path}")

    # 路线 1：ComfyUI 自带 loader（支持 mp3/flac/ogg 等）
    try:
        from comfy_extras.nodes_audio import load_audio  # type: ignore

        return load_audio(str(file_path))
    except Exception:  # noqa: BLE001 - 拿不到就退回 wave
        pass

    # 路线 2：内置 wave（只支持 wav/pcm）
    try:
        import wave

        import numpy as np
        import torch

        with wave.open(str(file_path), "rb") as fh:
            ch = fh.getnchannels()
            sr = fh.getframerate()
            width = fh.getsampwidth()
            frames = fh.readframes(fh.getnframes())
        if width != 2:
            raise FusionError(
                f"内置音频兜底只支持 16-bit PCM wav，实际 {width * 8}-bit：{file_path}"
            )
        data = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch).T          # [C, N]
        else:
            data = data[None, :]                   # [1, N]
        waveform = torch.from_numpy(data)[None, ...]   # [1, C, N]
        return {"waveform": waveform, "sample_rate": int(sr)}
    except FusionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FusionError(
            f"参考音频无法读取：{file_path}（{type(exc).__name__}: {exc}）\n"
            "mp3/flac 等格式需要 ComfyUI 的音频 loader 可用。"
        ) from exc


def load_video_frames_via_comfy(file_path: Path):
    """把视频文件读成 IMAGE 帧批次（ComfyUI 常用形式）。

    Director 的 `_as_video_frames()` 接受视频帧张量；
    拿不到 ComfyUI loader 时用 ffmpeg 抽帧兜底。
    两种都失败就**抛错**（不返回空）。
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FusionError(f"参考视频文件不存在：{file_path}")

    # 路线 1：ComfyUI 的 video loader（版本差异大，探测多个入口）
    for mod_name, fn_name in (
        ("comfy_extras.nodes_video", "load_video"),
        ("comfy_extras.nodes_video", "LoadVideo"),
        ("comfy_extras.nodes_video_model", "load_video"),
    ):
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name)
            out = fn(str(file_path))
            frames = out.get("images") if isinstance(out, dict) else out
            if frames is not None:
                return frames
        except Exception:  # noqa: BLE001
            continue

    # 路线 2：ffmpeg 抽帧
    try:
        import subprocess
        import tempfile

        import numpy as np
        import torch
        from PIL import Image

        # ⚠️ 必须用**绝对导入**。本模块属于 `bettercomfyui_comfy` 这个**顶层包**
        # （ComfyUI 的 custom_nodes 不在 sys.path 上，插件是独立顶层包），
        # 所以 `from ..pipeline.core import ...` 会抛
        # `attempted relative import beyond top-level package`。
        # 这个坑在 config.py 里踩过一次，这里又踩了一次 —— 同一个原因。
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        with tempfile.TemporaryDirectory() as td:
            out_pat = str(Path(td) / "f_%05d.png")
            proc = subprocess.run(
                [_ffmpeg_exe(), "-y", "-i", str(file_path), "-vsync", "0", out_pat],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                raise FusionError(
                    f"ffmpeg 抽帧失败：{(proc.stderr or '')[-300:]}"
                )
            pngs = sorted(Path(td).glob("f_*.png"))
            if not pngs:
                raise FusionError(f"参考视频抽不出任何帧：{file_path}")
            arrs = [np.asarray(Image.open(p).convert("RGB")).astype("float32") / 255.0
                    for p in pngs]
        return torch.from_numpy(np.stack(arrs, axis=0))    # [N,H,W,C]
    except FusionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FusionError(
            f"参考视频无法读取：{file_path}（{type(exc).__name__}: {exc}）"
        ) from exc


def locked_ref_media(
    project_id: str,
    audio_refs: list[dict] | None,
    video_refs: list[dict] | None,
    *,
    repo_root: Path,
    config: Any = None,
) -> tuple[dict[int, Any], dict[int, Any], list[dict]]:
    """把**已 LOCK** 的音频/视频素材解析成 r2v 组要的 `ref_audios` / `ref_videos`。

    与 `locked_asset_ref_images` 同样的硬规则：**未 LOCK 直接报错，不静默跳过**。

    返回 `(ref_audios, ref_videos, audit)`。
    """
    from importlib import import_module

    paths_mod = import_module("bettercomfyui.store.paths")
    assets_mod = import_module("bettercomfyui.store.assets")
    cfg_mod = import_module("bettercomfyui.config")

    repo_root = Path(repo_root)
    if config is None:
        config = cfg_mod.load_config(repo_root=repo_root)
    projects_root = Path(config.global_.project_root)
    if not projects_root.is_absolute():
        projects_root = repo_root / projects_root

    paths = paths_mod.ProjectPaths(root=projects_root, project_id=project_id)
    store = assets_mod.AssetStore(paths)

    def resolve_one(ref: dict, kind: str) -> tuple[dict, Path, dict]:
        asset_id = ref.get("asset_id")
        version = ref.get("version")
        if not asset_id or not version:
            raise FusionError(f"{kind}引用缺少 asset_id 或 version：{ref!r}（R3 要求指定版本）")
        locked = store.resolve_lock(asset_id, version)
        if locked is None:
            raise FusionError(
                f"{kind}素材 {asset_id}@{version} 没有有效 LOCK，禁止进入导演台。\n"
                "请先通过素材审核取得 LOCK。"
            )
        rel = locked.get("absolute_path") or locked.get("file_path")
        if not rel:
            raise FusionError(f"LOCK 记录缺少文件路径：{locked!r}")
        f = Path(rel)
        if not f.is_absolute():
            f = paths.project_dir / f
        if not f.is_file():
            raise FusionError(f"{kind}素材文件不存在：{f}（LOCK 与磁盘不一致，数据损坏）")
        # 审计 R2：LOCK 指纹必须与磁盘字节一致，否则拒绝送入导演台。
        try:
            assets_mod.verify_locked_file_content(locked, f)
        except Exception as exc:  # noqa: BLE001
            raise FusionError(
                f"{kind}素材内容与 LOCK 声明的指纹不一致，拒绝送入导演台：{exc}"
            ) from exc
        return locked, f, {
            "asset_id": asset_id, "version": version,
            "lock_source": locked.get("lock_source"),
            "content_hash": locked.get("content_hash"), "file": str(f),
        }

    ref_audios: dict[int, Any] = {}
    ref_videos: dict[int, Any] = {}
    audit: list[dict] = []

    for i, ref in enumerate(audio_refs or []):
        if i >= MAX_REFERENCE_AUDIOS:
            raise FusionError(
                f"参考音频槽位超过上限 {MAX_REFERENCE_AUDIOS}；请减少引用数。"
            )
        locked, f, info = resolve_one(ref, "音频")
        ref_audios[i] = load_audio_via_comfy(f)
        audit.append({"kind": "audio", "slot": i, **info})

    for i, ref in enumerate(video_refs or []):
        if i >= MAX_REFERENCE_VIDEOS:
            raise FusionError(
                f"参考视频槽位超过上限 {MAX_REFERENCE_VIDEOS}；请减少引用数。"
            )
        locked, f, info = resolve_one(ref, "视频")
        ref_videos[i] = load_video_frames_via_comfy(f)
        audit.append({"kind": "video", "slot": i, **info})

    return ref_audios, ref_videos, audit
