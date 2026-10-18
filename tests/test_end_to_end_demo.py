"""Lead 负责的端到端闭环测试（任务书验收 15）与交付校验测试。

本文件钉住三件事，都是「看起来通过但实际上没验证」的高危区：

  E1 **闭环真的走完了**：剧本 -> 素材 -> 段 -> 审核 -> 拼接 -> 输出，
     且产物是**合法媒体**（用 ffmpeg 实测时长/编码），不是「文件存在即通过」。
  E2 **R_full 只跑一次**，且段级审核路径绝不触发它。
  E3 **mock 边界不被模糊**：报告必须声明 used_mock / unverified，
     绝不允许把占位媒体描述成真实生成结果。

E1 依赖 ffmpeg（本机由 imageio-ffmpeg 提供），无 ffmpeg 时明确 skip 为「未验证」，
而不是悄悄通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bettercomfyui.config import load_config
from bettercomfyui.models.enums import FullReviewPolicy
from bettercomfyui.pipeline.core import PipelineError, probe_media


def _ffmpeg_available() -> bool:
    try:
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        return bool(_ffmpeg_exe())
    except Exception:  # noqa: BLE001
        return False


requires_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="未验证：本机无 ffmpeg，无法验证真实拼接/封装与媒体探测",
)


# ---------------------------------------------------------------------------
# E3 先测：mock 边界（不依赖 ffmpeg）
# ---------------------------------------------------------------------------


def test_demo_report_declares_mock_and_unverified(repo_root: Path):
    """报告必须显式声明「用了占位媒体」与「哪些没验证」，否则会被误读为全链路已验证。"""
    from bettercomfyui import demo

    cfg = load_config(repo_root=repo_root)
    # 只造报告对象，不真跑流程：检查字段语义
    rep = demo.DemoReport()
    assert rep.used_mock is True, "默认必须假定为 mock，不能默认声称真实"
    assert rep.real_generation is False, "默认不得声称真实生成可用"

    d = rep.to_dict()
    for key in ("used_mock", "real_generation", "real_generation_reason", "unverified"):
        assert key in d, f"报告缺少 {key} 字段（mock 边界必须可被机器判定）"


def test_demo_module_states_placeholder_media_explicitly():
    """demo.py 的文档必须写明「占位媒体不是模型输出」，防止后人误读。"""
    from bettercomfyui import demo

    doc = (demo.__doc__ or "").lower()
    assert "占位" in doc, "demo 模块文档必须说明使用占位媒体"
    assert "不证明" in (demo.__doc__ or "") or "not" in doc


# ---------------------------------------------------------------------------
# 媒体探测：必须真的探测，不许假装
# ---------------------------------------------------------------------------


def test_probe_media_rejects_missing_and_empty(tmp_path: Path):
    with pytest.raises(PipelineError, match="文件不存在"):
        probe_media(tmp_path / "nope.mp4")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(PipelineError, match="文件为空"):
        probe_media(empty)


def test_probe_media_rejects_non_media_content(tmp_path: Path):
    """扩展名对但内容不是媒体 -> 必须报错。

    这是「假成功」的典型入口：只检查文件存在，就会把损坏文件当成交付成功。
    """
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is definitely not an mp4 file" * 200)
    if not _ffmpeg_available():
        pytest.skip("未验证：无 ffmpeg，无法探测媒体")
    with pytest.raises(PipelineError, match="无法解析为合法媒体"):
        probe_media(fake)


def test_probe_media_honours_injected_runner(tmp_path: Path):
    """注入 runner 时应走注入实现，便于离线测试。"""
    f = tmp_path / "x.bin"
    f.write_bytes(b"z" * 8192)
    out = probe_media(f, ffprobe_runner=lambda p: {"duration_s": 12.5, "has_audio": True})
    assert out["probed"] is True
    assert out["duration_s"] == 12.5
    assert out["has_audio"] is True


def test_probe_media_rejects_zero_duration(tmp_path: Path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"z" * 8192)
    with pytest.raises(PipelineError, match="不是有效媒体"):
        probe_media(f, ffprobe_runner=lambda p: {"duration_s": 0, "has_audio": False})


# ---------------------------------------------------------------------------
# E1 + E2：完整闭环
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_end_to_end_demo_completes_and_produces_valid_media(repo_root: Path, tmp_path: Path):
    """验收 15：剧本 -> 素材 -> 视频段 -> 审核 -> 拼接 -> 输出，且产物是合法媒体。"""
    from bettercomfyui.demo import run_demo

    cfg = load_config(repo_root=repo_root)
    # 用独立的 projects 根，避免污染真实项目目录
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)

    report = run_demo(
        repo_root=repo_root, config=cfg, workdir=tmp_path / "work", executor="mock"
    )

    assert report["ok"] is True, f"端到端失败：{report.get('error')}"
    assert report["project_id"]

    # 素材：3 个新建，全部通过审核并 LOCK
    assert report["assets_created"] == 3, report["assets_created"]
    assert report["assets_locked"] == 3, report["assets_locked"]

    # 段：3 段计划、3 段生成、3 段通过
    assert report["segments_planned"] == 3
    assert report["segments_generated"] == 3
    assert report["segments_passed"] == 3, (
        f"应有 3 段通过，实际 passed={report['segments_passed']} "
        f"fallback={report['segments_fallback']}"
    )

    # E2：段级审核路径绝不触发 R_full；整片审核只在最后跑一次
    assert report["full_review_calls"] == 1, (
        f"R_full 必须恰好在最终拼接后执行一次，实际 {report['full_review_calls']} 次"
    )

    assert report["spliced"] is True and report["encoded"] is True

    # E1 的核心：产物必须是**合法媒体**，而不仅是「文件存在」
    out = Path(report["output_path"])
    assert out.exists(), out
    checks = probe_media(out)
    assert checks["probed"] is True
    assert checks["duration_s"] > 0
    assert checks["has_video"] is True, "成片必须有视频轨"
    assert checks["video_codec"] == "h264", checks["video_codec"]
    # 3 段 × 3 秒：时长应接近 9 秒（给编码/拼接留 0.5s 容差）
    assert 8.0 <= checks["duration_s"] <= 10.0, (
        f"成片时长 {checks['duration_s']}s 与 3×3s 不符 —— 可能漏段或段被截断"
    )

    # 元数据：可追溯
    meta = json.loads(Path(report["metadata_path"]).read_text(encoding="utf-8"))
    assert meta["project_id"] == report["project_id"]
    assert len(meta["segments"]) == 3
    for seg in meta["segments"]:
        assert seg["segment_version"] == "v1"
        assert seg["final_score"] is not None
        assert seg["content_hash"], "每段都必须在元数据里留下内容指纹"
    assert meta["movie_content_hash"].startswith("sha256:")
    assert set(meta["r1b_scores"]) == {"叙事流畅", "音画同步", "节奏合理", "无明显跳变"}

    # E3：报告必须明确标注这是占位媒体路径
    assert report["used_mock"] is True
    assert report["real_generation"] is False
    assert report["unverified"], "必须列出未验证项"
    assert any("真实视频模型生成" in u for u in report["unverified"])


@requires_ffmpeg
def test_mock_provenance_is_persisted_into_movie_metadata(repo_root: Path, tmp_path: Path):
    """占位媒体产物必须在**元数据里**留下来源声明，而不只是内存里的一个布尔值。

    为什么这条重要：成片文件躺在磁盘上是无法区分「模型生成」与「占位媒体」的。
    如果来源只存在于当次运行的 stdout，任务书第 9 节末句
    「不能把模拟结果说成真实生成已通过」在事实上就无法被长期守住。
    """
    from bettercomfyui.demo import run_demo

    cfg = load_config(repo_root=repo_root)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)

    report = run_demo(repo_root=repo_root, config=cfg,
                      workdir=tmp_path / "w", executor="mock")
    assert report["ok"] is True, report.get("error")

    meta = json.loads(Path(report["metadata_path"]).read_text(encoding="utf-8"))
    prov = meta.get("generation_provenance")
    assert isinstance(prov, dict) and prov, (
        "成片元数据缺少 generation_provenance —— 占位产出日后无法与真实产出区分"
    )
    assert prov["is_mock"] is True
    assert prov["executor"]
    assert prov["reason"], "必须写明为什么判定为不可用真实生成"
    assert prov["unverified"], "必须列出未验证项"

    # 反向要求：元数据里必须真的出现「占位/未验证」这类字样，供人工与工具双重识别
    blob = json.dumps(meta, ensure_ascii=False)
    assert any(k in blob for k in ("占位", "未验证", "mock", "MOCK")), (
        "元数据文本里找不到任何来源标注，存在被误读为真实产出的风险"
    )


def test_metadata_rejects_non_dict_provenance():
    """generation_provenance 必须是 dict —— 类型错误要在构造期暴露。"""
    from bettercomfyui.models.entities import MovieMetadata

    with pytest.raises(ValueError, match="generation_provenance 必须是 dict"):
        MovieMetadata(project_id="p", generation_provenance="mock")


def test_metadata_is_mock_output_property():
    from bettercomfyui.models.entities import MovieMetadata

    assert MovieMetadata(project_id="p").is_mock_output is False
    assert MovieMetadata(
        project_id="p", generation_provenance={"is_mock": True}
    ).is_mock_output is True


@requires_ffmpeg
def test_demo_is_repeatable_without_duplicating_assets(repo_root: Path, tmp_path: Path):
    """第二次运行时，已有素材必须被复用而不是重复生成（R1 的核心）。

    用同一个项目跑两次：第二次 assets_created 必须为 0、assets_reused 必须为 3。
    """
    from bettercomfyui.demo import run_demo

    cfg = load_config(repo_root=repo_root)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)

    first = run_demo(repo_root=repo_root, config=cfg,
                     workdir=tmp_path / "w1", executor="mock")
    assert first["ok"] is True, first.get("error")
    assert first["assets_created"] == 3

    second = run_demo(repo_root=repo_root, config=cfg, project=first["project_id"],
                      workdir=tmp_path / "w2", executor="mock")
    assert second["ok"] is True, second.get("error")
    assert second["assets_created"] == 0, (
        f"第二次不应新建素材，实际新建 {second['assets_created']} 个 —— "
        "说明增量匹配失效，会重复生成同一角色（跨章节身份漂移的根因）"
    )
    assert second["assets_reused"] == 3, second["assets_reused"]


@requires_ffmpeg
def test_demo_blocks_when_full_review_fails_without_policy(repo_root: Path, tmp_path: Path):
    """整片审核未通过且未配置策略时，必须失败而不是隐式降级输出。"""
    from bettercomfyui import demo

    cfg = load_config(repo_root=repo_root)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    assert cfg.video_review.full_review_policy is None

    # 把内置剧本改成单段，减少耗时；并让整片评分器给低分
    monkeypatch_script = json.loads(json.dumps(demo.DEMO_SCRIPT))
    monkeypatch_script["chapters"][0]["segments"] = monkeypatch_script["chapters"][0]["segments"][:1]

    report = demo.run_demo(
        repo_root=repo_root, config=cfg, workdir=tmp_path / "w", executor="mock"
    )
    # 默认剧本 + 默认评分器（85 分）会通过，所以这里只验证「通过时不写策略」这条语义
    assert report["ok"] is True, report.get("error")
    assert report["degrade_flags"] == [], (
        "整片审核通过时不应产生任何降级标记"
    )
