"""音画参考链路（`ref_audios` / `ref_videos`）与插件导入纪律的回归测试。

## 覆盖一个"写完了忘记接线"的缺口

方案要求「参考主体 / 音画参考」，三个导演台的 r2v 组也支持
`ref_audios` / `ref_videos`。`fusion.locked_ref_media` 写完之后
**没有被任何节点调用** —— 于是这条链路长期只有代码、没有证据。

本文件钉住三件事：

1. `locked_ref_media` 的**硬规则**（未 LOCK / 缺版本必须报错，不许静默跳过）；
2. 音画参考真的能进 r2v 组，且**不引入真实包没有的键**；
3. **插件里不得出现跨顶级包的相对导入** —— 这个坑踩过两次，
   第二次是 `fusion.py` 里的 `from ..pipeline.core import _ffmpeg_exe`
   （插件是独立顶层包，`..` 直接抛 ImportError）。

需要 torch 的用例在无 torch 时 skip 并标注未验证。
"""

from __future__ import annotations

import ast
import subprocess
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 3) 导入纪律（不需要 torch，永远要跑）
# ---------------------------------------------------------------------------


def test_plugin_has_no_cross_top_level_relative_imports():
    """`bettercomfyui_comfy` 是**独立顶层包**，不得出现 `from ..x import`。

    ComfyUI 的 custom_nodes 不在 `sys.path` 上，插件是被当成顶层包加载的，
    所以 `from ..pipeline.core import ...` 会抛
    `attempted relative import beyond top-level package`。

    这个坑踩过两次：
    * `bettercomfyui/config.py`（编排包内，单点 `.` 即可）；
    * `bettercomfyui_comfy/fusion.py`（插件内，**必须用绝对导入**）。

    用 AST 扫，不扫文本 —— 否则注释/文档里提到这种写法会误报。
    """
    offenders: list[str] = []
    for py in sorted((REPO_ROOT / "bettercomfyui_comfy").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # level >= 2 表示 `from ..` 或更深
                if (node.level or 0) >= 2:
                    offenders.append(
                        f"{py.relative_to(REPO_ROOT)}:{node.lineno}: "
                        f"from {'.' * node.level}{node.module or ''} import ..."
                    )
    assert not offenders, (
        "插件里出现跨顶级包的相对导入（会在 ComfyUI 里抛 ImportError）：\n  "
        + "\n  ".join(offenders)
    )


def test_plugins_absolute_imports_resolve():
    """插件里 `from bettercomfyui...` 的绝对导入必须真的能解析。

    这条是对上一条的补充：允许绝对导入，但**必须存在**。
    """
    missing: list[str] = []
    for py in sorted((REPO_ROOT / "bettercomfyui_comfy").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.level or 0) == 0:
                mod = node.module or ""
                if not mod.startswith("bettercomfyui"):
                    continue
                # 只检查模块是否存在，不执行导入（避免 torch 依赖）
                parts = mod.split(".")
                p = REPO_ROOT.joinpath(*parts)
                if not (p.with_suffix(".py").is_file() or p.is_dir()):
                    missing.append(f"{py.relative_to(REPO_ROOT)}:{node.lineno}: {mod}")
    assert not missing, f"插件引用了不存在的模块：{missing}"


# ---------------------------------------------------------------------------
# 1) + 2) 音画参考链路（需要 torch）
# ---------------------------------------------------------------------------


torch = pytest.importorskip("torch", reason="未验证：无 torch，无法构造张量")


@pytest.fixture()
def project(tmp_path: Path):
    from bettercomfyui.config import load_config
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("音画参考测试")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    return cfg, pid, paths


def _make_wav(path: Path, seconds: float = 0.5, sr: int = 44100) -> Path:
    import numpy as np

    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    stereo = np.stack([0.3 * np.sin(2 * np.pi * 440 * t)] * 2, axis=1)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes((np.clip(stereo, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def _make_mp4(path: Path) -> bool:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    proc = subprocess.run(
        [_ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
         "testsrc=size=64x64:rate=8:duration=1", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=False,
    )
    return proc.returncode == 0 and path.is_file()


def _add_and_lock(store, cfg, pid, asset_type, name, src):
    """走**真实审核链路**加 LOCK。

    直接 `store.set_lock` 会被守卫拒绝（要求 `review_cycle_id` ——
    必须能追溯到授权它的审核周期）。顺从 R3 而不是绕过它。
    """
    from bettercomfyui.models.enums import (
        AssetSource, AssetStatus, LibraryScope, ReviewMode,
    )
    from bettercomfyui.review.asset_review import AssetReviewer

    entry = store.create_asset(asset_type=asset_type, name=name,
                               source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    ver = store.add_version(entry["asset_id"], source=AssetSource.AI,
                            file_path=src, status=AssetStatus.PENDING_REVIEW)
    reviewer = AssetReviewer(
        store, config=cfg, project_id=pid,
        model_review=lambda mat: {"score": 92.0, "passed": True},
        auto_commit=True,
    )
    reviewer.review_version(entry["asset_id"], ver["version"], mode=ReviewMode.MODEL,
                            material={"file_path": str(src),
                                      "content_hash": ver["content_hash"]})
    assert store.resolve_lock(entry["asset_id"], ver["version"]) is not None
    return entry["asset_id"], ver["version"]


def test_audio_asset_resolves_to_ref_audio(project, tmp_path: Path):
    """已 LOCK 的音频素材必须能解析成 ComfyUI 的 AUDIO 结构。"""
    from bettercomfyui.models.enums import AssetType
    from bettercomfyui.store.assets import AssetStore

    from bettercomfyui_comfy import fusion

    cfg, pid, paths = project
    store = AssetStore(paths)
    wav = _make_wav(tmp_path / "v.wav")
    aid, ver = _add_and_lock(store, cfg, pid, AssetType.AUDIO, "音色", wav)

    audios, videos, audit = fusion.locked_ref_media(
        pid, [{"asset_id": aid, "version": ver}], None,
        repo_root=REPO_ROOT, config=cfg,
    )
    assert set(audios) == {0}
    a = audios[0]
    # 结构必须与 ComfyUI 一致，否则真实包会判定无效并**静默跳过**
    assert sorted(a) == ["sample_rate", "waveform"], sorted(a)
    assert a["sample_rate"] == 44100
    assert tuple(a["waveform"].shape) == (1, 2, 22050)
    assert videos == {}
    assert [x["kind"] for x in audit] == ["audio"]


def test_video_asset_resolves_to_ref_video(project, tmp_path: Path):
    """已 LOCK 的视频素材必须能解析成帧批次。"""
    from bettercomfyui.models.enums import AssetType
    from bettercomfyui.store.assets import AssetStore

    from bettercomfyui_comfy import fusion

    cfg, pid, paths = project
    mp4 = tmp_path / "r.mp4"
    if not _make_mp4(mp4):
        pytest.skip("未验证：无可用 ffmpeg，无法造参考视频")

    store = AssetStore(paths)
    aid, ver = _add_and_lock(store, cfg, pid, AssetType.VIDEO, "参考视频", mp4)

    audios, videos, audit = fusion.locked_ref_media(
        pid, None, [{"asset_id": aid, "version": ver}],
        repo_root=REPO_ROOT, config=cfg,
    )
    assert audios == {}
    assert set(videos) == {0}
    assert videos[0].shape[0] == 8, f"应抽到 8 帧，实际 {videos[0].shape}"
    assert videos[0].shape[-1] == 3
    assert [x["kind"] for x in audit] == ["video"]


def test_unlocked_media_is_rejected(project, tmp_path: Path):
    """**R3**：未 LOCK 的音画素材不得进入导演台，且必须报错。"""
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope
    from bettercomfyui.store.assets import AssetStore

    from bettercomfyui_comfy import fusion

    cfg, pid, paths = project
    store = AssetStore(paths)
    wav = _make_wav(tmp_path / "u.wav")
    entry = store.create_asset(asset_type=AssetType.AUDIO, name="未审核",
                               source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    ver = store.add_version(entry["asset_id"], source=AssetSource.AI,
                            file_path=wav, status=AssetStatus.PENDING_REVIEW)

    with pytest.raises(fusion.FusionError, match="LOCK"):
        fusion.locked_ref_media(pid, [{"asset_id": entry["asset_id"],
                                       "version": ver["version"]}], None,
                                repo_root=REPO_ROOT, config=cfg)


def test_missing_version_is_rejected(project, tmp_path: Path):
    """缺版本号必须报错（R3 要求引用到具体版本）。"""
    from bettercomfyui.models.enums import AssetType
    from bettercomfyui.store.assets import AssetStore

    from bettercomfyui_comfy import fusion

    cfg, pid, paths = project
    store = AssetStore(paths)
    wav = _make_wav(tmp_path / "v2.wav")
    aid, _ver = _add_and_lock(store, cfg, pid, AssetType.AUDIO, "音色", wav)

    with pytest.raises(fusion.FusionError, match="asset_id 或 version"):
        fusion.locked_ref_media(pid, [{"asset_id": aid}], None,
                                repo_root=REPO_ROOT, config=cfg)


def test_media_refs_land_in_r2v_group_without_extra_keys():
    """音画参考必须真的进组，且**不引入真实包没有的键**。

    真实 r2v 组只有 12 个键；多一个都会被 AST 契约测试判为不一致。
    """
    from bettercomfyui_comfy import fusion

    audios = {0: {"waveform": torch.zeros(1, 1, 100), "sample_rate": 32000}}
    videos = {0: torch.zeros(4, 8, 8, 3)}
    g = fusion.make_r2v_group(prompt="x", duration_sec=5.0,
                              ref_images={}, ref_videos=videos, ref_audios=audios)
    assert len(g["ref_audios"]) == 1
    assert len(g["ref_videos"]) == 1
    assert g["kind"] == "r2v"
    # 与真实包一致的 12 个键，没有额外键
    assert len(g) == 12, sorted(g)


def test_media_slot_overflow_is_rejected():
    """超过真实包槽位上限必须报错（不静默丢弃）。

    与参考图走同一套校验：越界 -> 报错并说明允许范围。
    """
    from bettercomfyui_comfy import fusion

    bad = {fusion.MAX_REFERENCE_AUDIOS: {"waveform": None, "sample_rate": 1}}
    with pytest.raises(fusion.FusionError, match="越界"):
        fusion.make_r2v_group(prompt="x", duration_sec=5.0, ref_audios=bad)

    bad_v = {fusion.MAX_REFERENCE_VIDEOS: torch.zeros(1, 4, 4, 3)}
    with pytest.raises(fusion.FusionError, match="越界"):
        fusion.make_r2v_group(prompt="x", duration_sec=5.0, ref_videos=bad_v)


def test_asset_type_has_audio_and_video():
    """素材类别必须包含音/视频 —— 否则参考音画无处归类。"""
    from bettercomfyui.models.enums import AssetType

    names = {t.name for t in AssetType}
    assert {"AUDIO", "VIDEO"} <= names, f"缺少音/视频类别：{sorted(names)}"
    # 原有四类必须保留（方案要求"至少四类"）
    assert {"CHARACTER", "SCENE", "PROP", "SHOT"} <= names
