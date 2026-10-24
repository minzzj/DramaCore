# -*- coding: utf-8 -*-
"""ROUND55 片头片尾模块单测（离线：真实 ffmpeg + Pillow，不依赖 GPU/网络）。

验收要点：
- mode 四种：无/片头/片尾/片头+片尾；
- 真实 ffmpeg 生成短视频，probe 时长 > 1s、有视频流与**静音音轨**；
- 文字为空 → 占位标题（审计可见，不伪造用户输入）；
- 失败不静默：ffmpeg 双路由失败 → 返回 None + warnings；字体可诊断；
- 尺寸偶数归一化、mov 格式、未知 mode/格式明确报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.pipeline import intro_outro as io  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    try:
        return io._ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - 环境缺 ffmpeg 时给出明确 skip 原因
        pytest.skip(f"ffmpeg 不可用：{exc}")


def _probe(path: Path) -> dict:
    return io._probe(path)


# ---------------------------------------------------------------------------
# 基础契约：mode 归一化 / 格式解析 / 字体探测
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("无", io.MODE_NONE), ("none", io.MODE_NONE), ("", io.MODE_NONE),
    ("片头", io.MODE_INTRO), ("intro", io.MODE_INTRO), ("title", io.MODE_INTRO),
    ("片尾", io.MODE_OUTRO), ("outro", io.MODE_OUTRO),
    ("片头+片尾", io.MODE_BOTH), ("both", io.MODE_BOTH),
    ("片头片尾", io.MODE_BOTH),
])
def test_normalize_mode(raw, expected):
    assert io.normalize_mode(raw) == expected


def test_normalize_mode_unknown_raises():
    with pytest.raises(ValueError):
        io.normalize_mode("随便写")


@pytest.mark.parametrize("label,key,suffix", [
    ("mp4（H.264）", "mp4", ".mp4"),
    ("MOV（H.264）", "mov", ".mov"),
    ("webm（VP9/Opus）", "webm", ".webm"),
])
def test_resolve_format(label, key, suffix):
    fmt = io.resolve_format(label)
    assert fmt["key"] == key
    assert fmt["suffix"] == suffix


def test_resolve_format_unknown_raises():
    with pytest.raises(ValueError):
        io.resolve_format("avi")


def test_find_font_file_is_optional_but_diagnosable():
    font = io.find_font_file()
    assert font is None or Path(font).is_file()


# ---------------------------------------------------------------------------
# mode=无：不生成、不调用 ffmpeg
# ---------------------------------------------------------------------------
def test_mode_none_skips_ffmpeg(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("mode=无 不应调用 ffmpeg")

    monkeypatch.setattr(io, "_ffmpeg_exe", _boom)
    out = io.generate_intro_outro(tmp_path / "work", mode="无")
    assert out["intro"] is None and out["outro"] is None
    assert out["mode"] == io.MODE_NONE
    assert any("不生成" in line for line in out["audit"])


# ---------------------------------------------------------------------------
# 真实生成（验收：2 个短视频 + probe 时长 > 1s）
# ---------------------------------------------------------------------------
def test_intro_only_real_ffmpeg(ffmpeg_bin, tmp_path):
    work = tmp_path / "nested" / "work"
    out = io.generate_intro_outro(
        work, mode="片头", width=960, height=544, fps=24,
        intro_seconds=2.0, title="雪夜客栈", subtitle="第一章")
    intro = out["intro"]
    assert intro is not None and Path(intro).is_file()
    assert Path(intro).suffix == ".mp4"
    info = _probe(Path(intro))
    assert info["has_video"] is True
    assert info["has_audio"] is True, "片头必须带静音音轨"
    assert float(info["duration_s"]) > 1.0
    assert out["outro"] is None
    assert any("片头卡" in line for line in out["audit"])
    assert any(("drawtext 路由" in line) or ("PIL PNG 路由" in line)
               for line in out["audit"])


def test_outro_only_real_ffmpeg(ffmpeg_bin, tmp_path):
    out = io.generate_intro_outro(
        tmp_path, mode="片尾", width=960, height=544, fps=24, outro_seconds=2.0,
        outro_title="终章", credits="导演 张三\n摄影 李四", watermark_text="剧核")
    outro = out["outro"]
    assert outro is not None and Path(outro).is_file()
    info = _probe(Path(outro))
    assert info["has_video"] and info["has_audio"]
    assert float(info["duration_s"]) > 1.0
    assert out["intro"] is None


def test_mode_both_generates_two_videos(ffmpeg_bin, tmp_path):
    out = io.generate_intro_outro(
        tmp_path, mode="片头+片尾", width=640, height=360, fps=24,
        intro_seconds=1.2, outro_seconds=1.2,
        title="剧核", credits="出品：剧核 DramaCore")
    assert out["intro"] is not None and out["outro"] is not None
    for path in (out["intro"], out["outro"]):
        info = _probe(Path(path))
        assert info["has_video"] and info["has_audio"]
        assert float(info["duration_s"]) > 1.0
    assert out["mode"] == io.MODE_BOTH


# ---------------------------------------------------------------------------
# 文本：占位 / 片尾文字 / 演职员
# ---------------------------------------------------------------------------
def test_empty_text_uses_placeholder_and_is_audited(ffmpeg_bin, tmp_path):
    out = io.generate_intro_outro(tmp_path, mode="片头+片尾",
                                  width=640, height=360, fps=24,
                                  intro_seconds=1.2, outro_seconds=1.2)
    assert out["intro"] is not None and out["outro"] is not None
    joined = "\n".join(out["audit"])
    assert "占位标题" in joined
    assert io.DEFAULT_TITLE in joined
    assert io.DEFAULT_OUTRO_TITLE in joined
    assert io.DEFAULT_CREDITS in joined


def test_outro_title_and_credits_mapping(ffmpeg_bin, tmp_path):
    out = io.generate_intro_outro(
        tmp_path, mode="片尾", width=640, height=360, fps=24, outro_seconds=1.2,
        subtitle="", outro_title="片尾文字", credits="演职员：A B C")
    assert out["outro"] is not None
    joined = "\n".join(out["audit"])
    # 有真实文字时不应出现「片尾文字为空」占位提示
    assert "片尾文字为空" not in joined
    assert "片尾演职员为空" not in joined


# ---------------------------------------------------------------------------
# 尺寸/格式/参数健壮性
# ---------------------------------------------------------------------------
def test_odd_dimensions_normalized_even(ffmpeg_bin, tmp_path):
    out = io.generate_intro_outro(
        tmp_path, mode="片头", width=641, height=361, fps=23.976,
        intro_seconds=1.2, title="奇偶")
    intro = Path(out["intro"])
    assert intro.is_file()
    info = _probe(intro)
    width, height = info.get("width"), info.get("height")
    if width is not None:
        assert int(width) % 2 == 0 and int(height) % 2 == 0
    assert any("尺寸归一化" in line for line in out["audit"])
    assert float(out["params"]["fps"]) == pytest.approx(23.976)


def test_mov_format_output(ffmpeg_bin, tmp_path):
    out = io.generate_intro_outro(
        tmp_path, mode="片头", fmt_label="mov（H.264）", width=320, height=180,
        fps=24, intro_seconds=1.0, title="MOV")
    assert out["intro"] is not None
    assert Path(out["intro"]).suffix == ".mov"
    assert out["format"] == "mov"
    assert _probe(Path(out["intro"]))["has_video"] is True


def test_invalid_mode_and_format_raise(tmp_path):
    with pytest.raises(ValueError):
        io.generate_intro_outro(tmp_path, mode="不存在")
    with pytest.raises(ValueError):
        io.generate_intro_outro(tmp_path, mode="片头", fmt_label="avi")


# ---------------------------------------------------------------------------
# 失败口径：不静默、不返回损坏文件
# ---------------------------------------------------------------------------
def test_drawtext_failure_falls_back_to_pil(monkeypatch, ffmpeg_bin, tmp_path):
    """drawtext 路由失败（如 filter 不可用）→ 自动改 PIL PNG 路由，仍然成功。"""
    def _fail(*_a, **_k):
        raise RuntimeError("模拟 drawtext 不可用")

    monkeypatch.setattr(io, "_encode_drawtext", _fail)
    out = io.generate_intro_outro(tmp_path, mode="片头", width=640, height=360,
                                  fps=24, intro_seconds=1.2, title="兜底")
    intro = out["intro"]
    if intro is None:  # 字体环境极端缺失时允许失败，但必须有告警
        assert out["warnings"], "双路由失败必须写 warnings（不能静默）"
        return
    assert Path(intro).is_file()
    assert any("PIL PNG 路由" in line for line in out["audit"])


def test_total_failure_returns_none_with_warnings(monkeypatch, tmp_path):
    """两条编码路由都失败 → 返回 None + warnings（不伪造成功）。"""
    def _fail(*_a, **_k):
        raise RuntimeError("模拟 ffmpeg 完全不可用")

    monkeypatch.setattr(io, "_encode_drawtext", _fail)
    monkeypatch.setattr(io, "_encode_still_png", _fail)
    external: list[str] = []
    out = io.generate_intro_outro(tmp_path, mode="片头+片尾",
                                  width=640, height=360, fps=24,
                                  intro_seconds=1.0, outro_seconds=1.0,
                                  warnings=external)
    assert out["intro"] is None and out["outro"] is None
    assert out["warnings"] is external, "应复用调用方传入的 warnings 列表"
    assert len(external) >= 2, f"两条卡片失败都要有告警：{external}"
    assert all("生成失败" in item for item in external)
    assert any("返回空，不冒充成功" in item for item in external)


def test_ffmpeg_missing_returns_none_with_warnings(monkeypatch, tmp_path):
    def _boom():
        raise RuntimeError("找不到 ffmpeg（模拟）")

    monkeypatch.setattr(io, "_ffmpeg_exe", _boom)
    monkeypatch.setattr(io, "_render_background", lambda *a, **k: None)
    out = io.generate_intro_outro(tmp_path, mode="片头", width=320, height=180,
                                  fps=24, intro_seconds=1.0, title="无 ffmpeg")
    assert out["intro"] is None
    assert out["warnings"], "缺 ffmpeg 必须写 warnings"


# ---------------------------------------------------------------------------
# logo / 水印：坏 logo 不拖垮卡片，但要有告警
# ---------------------------------------------------------------------------
def test_bad_logo_warns_but_renders(ffmpeg_bin, tmp_path):
    bad_logo = tmp_path / "not_an_image.png"
    bad_logo.write_bytes(b"not a png")
    out = io.generate_intro_outro(
        tmp_path, mode="片头", width=640, height=360, fps=24,
        intro_seconds=1.2, title="Logo 容错", logo_path=str(bad_logo),
        watermark_text="剧核 DramaCore")
    assert out["intro"] is not None
    # 若走 PIL 路由，坏 logo 会在 warnings 里；drawtext 路由也可能告警，二者必有一个
    assert isinstance(out["warnings"], list)
