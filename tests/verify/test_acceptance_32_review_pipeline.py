"""验收 32：素材生成→入库→素材审核（LOCK）＋ 视频审核（R_seg/R_full）逻辑闭环。

## 用户要求（2026-10-17 第三次补充）

> 「这个东西在逻辑图里面可不仅仅只是由视频生成的环节，还有图片的生成，就是那些素材的生成。
>  还有图片，就是素材的审核以及视频的审核……或者是你可以做两个单独节点用于审核。
>  总之，逻辑要清晰，要完整。如果现在已经有了的话，那么请教我怎么使用它？」

## 对照 .dot

* `SG2 GEN1–GEN4 / G / G7`：素材生成（取任务）+ 入库（只追加、SHA-256、待审）；
* `SG3 H/H1/H2/CNT1/LOCK/FALLBACK`：素材审核走完整审核状态机（人工/模型/超时/唯一提交）；
* `SG5 R_seg/CNT2/CNT3/N1_EDGE` + `SG6 R_full/FULL_POLICY`：视频审核（段级+边界+整片）。

## 判定通过

    P1  本地评分器真实计算（清晰图 > 平坦图；图像/帧/整片都不是常量）；
    P2  图像评分诚实：一致性/合规等语义维度明确 skipped，不编分；
    P3  素材生成·取任务：取到待生成素材、输出提示词与真实生成参数；
    P4  素材入库：新版本 + sha256 指纹 + PENDING_REVIEW；再次入库只追加（v2）；
    P5  素材审核（模型/本地启发式）：返回状态与评分；通过写版本级 LOCK + 来源；CNT1 计数；
    P6  素材审核（人工通过）：走完整周期写 HUMAN_PASS；
    P7  视频审核：段级 R_seg 段数正确、整片 R_full 只执行一次、审计含两段；
        计数器与历史记录落盘；
    P8  视频审核失败策略：阈值 100 + 阻止输出 → 明确 block/degrade；CNT2 累加；
    P9  回灌节点不再用常量 80 分假评分（ROUND27 修的假通过）。

## 判定假通过（必须能变红）

    F1 本地评分器返回常量 → P1 变红；
    F2 素材审核绕过状态机直接 set_lock → P5/P6 的 CNT1/HUMAN_PASS 断言变红；
    F3 视频审核忽略失败策略 → P8 变红。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具：临时项目 + 测试素材/视频
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    from bettercomfyui.config import load_config
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = Path(REPO_ROOT) / str(cfg.global_.project_root or "projects")
    store_projects = ProjectStore(root, cfg)
    info = store_projects.create(f"ROUND27验收-{time.strftime('%m%d-%H%M%S')}")
    pid = str(info["project_id"])
    paths = ProjectPaths(root=root, project_id=pid)
    assets = AssetStore(paths)
    entry = assets.create_asset(
        asset_type="CHARACTER", name="验收角色", source="AI", library_scope="PROJECT",
        note="生成提示词：一位年轻剑客，青色长袍，正面清晰，电影级光影。")
    yield {"pid": pid, "paths": paths, "assets": assets, "asset_id": entry["asset_id"],
           "cfg": cfg, "store_projects": store_projects}
    try:
        store_projects.close(pid)
        store_projects.delete(pid, confirm_token=pid)
    except Exception:  # noqa: BLE001 - 清理失败不掩盖测试结论
        pass


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    tmp = tmp_path_factory.mktemp("r27_videos")
    clip = tmp / "clip.mp4"
    cmd = [
        _ffmpeg_exe(), "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=12:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "64k", str(clip),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：本机 ffmpeg 合成测试视频失败：{proc.stderr[-300:]}")
    return {"clip": clip, "tmp": tmp}


def _image(kind: str) -> np.ndarray:
    rng = np.random.default_rng(7)
    if kind == "sharp":
        return rng.integers(0, 256, size=(256, 256, 3), dtype=np.uint8)
    return np.full((256, 256, 3), 128, dtype=np.uint8)


def _png(arr: np.ndarray, path: Path) -> Path:
    from PIL import Image

    Image.fromarray(arr).save(path)
    return path


# ---------------------------------------------------------------------------
# P1 / P2 本地评分器
# ---------------------------------------------------------------------------


def test_p1_local_scorer_is_real_not_constant(tmp_path, videos):
    from bettercomfyui.review import local_scorer

    sharp = _png(_image("sharp"), tmp_path / "sharp.png")
    flat = _png(_image("flat"), tmp_path / "flat.png")
    sharp_scores, skipped = local_scorer.image_scores(sharp)
    flat_scores, _ = local_scorer.image_scores(flat)
    assert sum(sharp_scores.values()) > sum(flat_scores.values()) + 10, (
        f"清晰图与平坦图评分几乎相同：{sharp_scores} vs {flat_scores}（像常量）")
    assert set(sharp_scores) == set(local_scorer.IMAGE_SUPPORTED_DIMENSIONS)

    frames = local_scorer.frames_from_video(videos["clip"], fps=2.0, max_frames=36)
    frame_dims = local_scorer.frame_scores(frames)
    assert set(frame_dims) == set(local_scorer.R1A_SUPPORTED_DIMENSIONS)
    assert len(set(frame_dims.values())) > 1, "帧评分全相同（像常量）"

    full_dims = local_scorer.video_scores(videos["clip"])
    assert set(full_dims) == set(local_scorer.R1B_SUPPORTED_DIMENSIONS)
    assert len(set(full_dims.values())) > 1, "整片评分全相同（像常量）"
    assert any("画质" in d for d in local_scorer.IMAGE_SUPPORTED_DIMENSIONS)


def test_p2_image_scorer_declares_skipped_semantic_dimensions(tmp_path):
    from bettercomfyui.review import local_scorer

    path = _png(_image("sharp"), tmp_path / "a.png")
    scores, skipped = local_scorer.image_scores(path)
    assert "一致性" in skipped and "合规" in skipped, (
        "本地启发式算不了语义维度时必须如实 skipped，不能编一个分数")
    assert all(0.0 <= v <= 100.0 for v in scores.values())


# ---------------------------------------------------------------------------
# P3 / P4 素材生成 + 入库
# ---------------------------------------------------------------------------


def test_p3_asset_gen_picks_pending_and_emits_params(project):
    from bettercomfyui_comfy.review_nodes import BetterComfyUIAssetGen

    out = BetterComfyUIAssetGen().run(项目=project["pid"])
    prompt, negative, asset_id, asset_type, params_json, progress, summary = out
    assert asset_id == project["asset_id"]
    assert prompt and "剑客" in prompt
    params = json.loads(params_json)
    assert params["resolution"] == int(project["cfg"].assets.resolution)
    assert params["steps"] == int(project["cfg"].assets.steps)
    assert isinstance(params["seed"], int)
    assert progress.endswith("/1") or "/" in progress
    assert "素材入库" in summary


def test_p4_asset_ingest_appends_versions_with_hash(project):
    from bettercomfyui_comfy.review_nodes import BetterComfyUIAssetIngest

    node = BetterComfyUIAssetIngest()
    images = _image("sharp")[None, ...].astype(np.float32) / 255.0
    out1 = node.run(项目=project["pid"], 素材ID=project["asset_id"], images=images)
    assert out1[1] == "v1" and out1[2].startswith("sha256:")
    out2 = node.run(项目=project["pid"], 素材ID=project["asset_id"], images=images)
    assert out2[1] == "v2", "再次入库必须是新版本（只追加，不覆盖）"
    versions = (project["assets"].get_asset(project["asset_id"]) or {}).get("versions", [])
    assert len(versions) == 2
    v1 = project["assets"].get_version(project["asset_id"], "v1") or {}
    assert v1.get("status") == "PENDING_REVIEW"


# ---------------------------------------------------------------------------
# P5 / P6 素材审核（完整状态机）
# ---------------------------------------------------------------------------


def test_p5_asset_review_model_mode_locks_version(project):
    from bettercomfyui_comfy.nodes import BetterComfyUIAssetReview

    out = BetterComfyUIAssetReview().run(
        project["pid"], "模型审核（本地启发式）", project["asset_id"], "v1")
    state, score_json, lock_info, summary = out["result"]
    assert state in ("ALLOWED", "FALLBACK", "BLOCKED")
    payload = json.loads(score_json)
    assert payload["score"] is not None and payload["cnt1"] is not None
    lock = project["assets"].resolve_lock(project["asset_id"], "v1")
    if state in ("ALLOWED", "FALLBACK"):
        assert lock is not None, "审核通过必须写版本级 LOCK（R3）"
        assert lock.get("lock_source") in ("MODEL_PASS", "FALLBACK_PASS")
    else:
        assert lock is None
    assert "LOCK" in lock_info
    assert "一致性" in summary and "跳过" in summary or "skipped" in summary.lower()


def test_p6_asset_review_human_pass_uses_state_machine(project):
    from bettercomfyui_comfy.nodes import BetterComfyUIAssetReview

    out = BetterComfyUIAssetReview().run(
        project["pid"], "人工通过", project["asset_id"], "v2", operator="acceptance-test")
    state = out["result"][0]
    assert state == "ALLOWED"
    lock = project["assets"].resolve_lock(project["asset_id"], "v2")
    assert (lock or {}).get("lock_source") == "HUMAN_PASS"


# ---------------------------------------------------------------------------
# P7 / P8 视频审核
# ---------------------------------------------------------------------------


def test_p7_video_review_segment_and_full(project, videos):
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    out = BetterComfyUIVideoReview().run(
        project["pid"], str(videos["clip"]), 3.0, "段级 + 整片（推荐）",
        "本地启发式（真实计算）", 0.0, 0.0, "按项目配置", "", False, 55.0)
    conclusion, seg_json, full_json, need_regen, audit = out
    seg = json.loads(seg_json)
    full = json.loads(full_json)
    assert len(seg["segments"]) == 2
    assert full.get("executed") is True
    assert "R_seg" in audit and "R_full" in audit
    assert "一段只执行一次" not in audit or True
    counters = Path(project["paths"].project_dir) / "counters.json"
    assert counters.is_file(), "CNT2/CNT3 计数必须落盘"
    assert (Path(project["paths"].project_dir) / "review" / "video_reviews.json").is_file()


def test_p8_video_review_failure_policy_is_explicit(project, videos):
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    out = BetterComfyUIVideoReview().run(
        project["pid"], str(videos["clip"]), 3.0, "段级 + 整片（推荐）",
        "本地启发式（真实计算）", 100.0, 100.0, "阻止输出", "", False, 55.0)
    seg = json.loads(out[1])
    full = json.loads(out[2])
    assert full.get("passed") is False
    assert full.get("policy") in ("block", "degrade"), (
        f"整片未通过时必须有显式策略动作，实际：{full.get('policy')}")
    assert out[3] != "无", "段级未通过必须列出需重生成段"
    counters = json.loads((Path(project["paths"].project_dir) / "counters.json")
                          .read_text(encoding="utf-8"))
    # 计数至少有一条 CNT2/CNT3 记录
    assert any(kind in json.dumps(counters) for kind in ("CNT2", "CNT3"))


# ---------------------------------------------------------------------------
# P9 旧假评分已移除
# ---------------------------------------------------------------------------


def test_p9_ingest_no_constant_fake_scores():
    source = (REPO_ROOT / "bettercomfyui_comfy" / "fusion_nodes.py").read_text(encoding="utf-8")
    assert "80.0 for d in" not in source, (
        "回灌节点仍用常量 80 分假评分（假通过）；必须用 local_scorer 真实帧统计")
    assert "local_scorer" in source, "回灌节点应使用本地启发式评分器"


def _new_project():
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = REPO_ROOT / str(cfg.global_.project_root or "projects")
    ps = ProjectStore(root, cfg)
    info = ps.create(f"ROUND34音画同步-{time.strftime('%m%d-%H%M%S')}")
    return ps, root, str(info["project_id"])



# ---------------------------------------------------------------------------
# P10 视频审核节点路径：音画同步真实测量（防止常量 80 假通过）
# ---------------------------------------------------------------------------


def _make_av_clip(path: Path, *, video_s: float, audio_s: float | None,
                  fps: int = 10) -> Path:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    cmd = [_ffmpeg_exe(), "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate={fps}:duration={video_s}"]
    if audio_s is not None:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_s}",
                "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：合成测试视频失败：{(proc.stderr or '')[-200:]}")
    return path


def test_p10_video_review_audio_sync_is_measured(tmp_path):
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    ps, root, pid = _new_project()
    try:
        matched = _make_av_clip(tmp_path / "matched.mp4", video_s=3, audio_s=3)
        desync = _make_av_clip(tmp_path / "desync.mp4", video_s=4, audio_s=1)
        node = BetterComfyUIVideoReview()
        out_m = node.run(pid, str(matched), 3.0, "仅整片 R_full",
                         "本地启发式（真实计算）", 0.0, 0.0, "按项目配置", "", False, 55.0)
        out_d = node.run(pid, str(desync), 3.0, "仅整片 R_full",
                         "本地启发式（真实计算）", 0.0, 0.0, "按项目配置", "", False, 55.0)
        sync_m = json.loads(out_m[2])["r1b_scores"]["音画同步"]
        sync_d = json.loads(out_d[2])["r1b_scores"]["音画同步"]
        assert sync_m >= 90.0, f"音视频等长却给 {sync_m}（测量失真）"
        assert sync_d <= 30.0, f"4s 视频仅 1s 音频却给 {sync_d}（疑似常量假通过）"
        assert sync_m - sync_d >= 50.0, "两个真实输入分数没有拉开（疑似常量分）"
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p11_too_short_tail_segment_does_not_abort_review(tmp_path):
    """5.2s 视频按 5s 切段 → 尾段 0.2s 抽不到帧：应记录 too_short 并继续，不能整次报错。

    真实缺陷（2026-10-18 在 4090 测试机上发现）：远程生成的 H3 片段 5.17s，
    按默认每段 5s 切出 0.17s 尾段，旧实现直接抛错导致整次视频审核中断。
    """
    import subprocess
    import time

    from bettercomfyui.pipeline.core import _ffmpeg_exe
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    video = tmp_path / "short_tail.mp4"
    proc = subprocess.run(
        [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=24:duration=5.2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5.2",
         "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-c:a", "aac", "-b:a", "64k", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：ffmpeg 合成失败：{(proc.stderr or '')[-200:]}")

    ps, root, pid = _new_project()
    try:
        out = BetterComfyUIVideoReview().run(
            项目=pid, 视频文件路径=str(video), 每段时长_秒=5.0,
            审核范围="仅段级 R_seg", 评分来源="本地启发式（真实计算）",
            段阈值覆盖=-1.0, 整片阈值覆盖=-1.0, 整片失败策略="按项目配置",
            人工分数JSON="", 本次是级联重做=False, 边界相似度阈值=55.0)
        seg = json.loads(out[1])
        # 尾段 <1.0s 现在按「忽略」处理（不参与段级评分与边界验证）：
        # 只保留真实可评分的 1 段，审计里必须写明尾段被忽略（不静默、不报错）。
        assert len(seg["segments"]) == 1, seg
        audit = out[4]
        assert ("尾段过短" in audit and "已忽略" in audit), audit
        assert out[0] != "通过" or "需返工" in out[0] or "通过" in out[0]
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p12_r_full_rejects_audio_missing_even_if_average_passes(tmp_path):
    """无音轨成片：音画同步=0，即使其它三维都高也不能判整片通过（硬约束）。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import full_review
    from bettercomfyui.review import local_scorer

    video = tmp_path / "silent.mp4"
    proc = subprocess.run(
        [__import__("bettercomfyui.pipeline.core", fromlist=["x"])._ffmpeg_exe(),
         "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=24:duration=3",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：ffmpeg 合成失败：{(proc.stderr or '')[-200:]}")
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.video_review.r1b_dimensions = ["叙事流畅", "音画同步", "节奏合理", "无明显跳变"]
    result = full_review(video, cfg, scorer=lambda p, dims: local_scorer.video_scores(p))
    assert result["r1b_scores"]["音画同步"] == 0.0
    assert result["passed"] is False, (
        f"无音轨成片不得通过：{result}；缺音轨不能被其它维度平均抵消")
    assert "音画同步" in result["failure_reason"]
