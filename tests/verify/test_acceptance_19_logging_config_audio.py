"""验收 19：日志/配置开关（图一 SG7 `Z3`/`Z4`/`Z5`）与音频拼接（`ENC1`）。

## 为什么单列一条

这四项的共同形态是「**配置有值、校验过、产品代码零读取**」（ROUND18 棘轮扫描发现）：

| 配置项 | 修前 |
|---|---|
| `global.log_level` | 只校验了 DEBUG/INFO/WARN/ERROR，从没用来设置日志级别 |
| `global.log_all_manual_and_auto_switch` | 从没被读过 |
| `global.config_import_export` | 没有导入导出实现 |
| `encode.audio_join` | 从没被读过（`encode()` 里没有音频分支） |

愿景原文要求：「开源用户都是开发者，**所有手动操作和自动切换仅在日志中记录**」。

## 判定通过

    P1 `apply_logging` 把 `global.log_level` 真正应用到根 logger；
    P2 级别变化会被应用（INFO -> DEBUG）；
    P3 `log_all_manual_and_auto_switch=True` 时记录人工审核 / 自动切换；
    P4 该开关为 False 时**不记录**（开关必须真的改变行为）；
    P5 配置导出**必然脱敏**：真实 `api_key` 不出现在导出文本里；
    P6 导入时脱敏占位 `***` 被**丢弃**（不会写成字面量密钥）；
    P7 `encode.audio_join=auto` 保留音轨；
    P8 `encode.audio_join=mute` 成片无音轨；
    P9 `encode.audio_join=manual` 用指定音轨替换；
    P10 `manual` 不给音轨必须报错（不静默回退）；
    P11 `auto` 却传了音轨也必须报错（矛盾输入不静默忽略）。

## 判定假通过（必须能变红）

    F1 删掉 `apply_logging` 的 `setLevel` -> P1/P2 变红；
    F2 让 `log_manual_or_switch` 忽略开关 -> P4 变红；
    F3 去掉 `_redact` -> P5 变红；
    F4 去掉 `audio_join` 的三个分支 -> P8/P9 变红。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# P1 / P2 日志级别
# ---------------------------------------------------------------------------


def test_p1_log_level_is_applied_to_root_logger():
    """`global.log_level` 必须真的改到根 logger 上。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.logging_setup import apply_logging

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.log_level = "DEBUG"
    name = apply_logging(cfg, level="DEBUG")
    assert name == "DEBUG"
    assert logging.getLogger().level == logging.DEBUG, (
        f"根 logger 级别没变：{logging.getLevelName(logging.getLogger().level)}"
    )


def test_p2_level_change_takes_effect():
    """级别变化要被应用（不是只应用一次就锁死）。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.logging_setup import apply_logging

    cfg = load_config(repo_root=REPO_ROOT)
    apply_logging(cfg, level="INFO")
    assert logging.getLogger().level == logging.INFO
    apply_logging(cfg, level="ERROR")
    assert logging.getLogger().level == logging.ERROR


# ---------------------------------------------------------------------------
# P3 / P4 人工操作与自动切换日志
# ---------------------------------------------------------------------------


def test_p3_manual_and_switch_are_logged_when_enabled(caplog):
    """开关打开时必须记录人工操作/自动切换。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.logging_setup import log_manual_or_switch

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.log_all_manual_and_auto_switch = True
    with caplog.at_level(logging.INFO, logger="bettercomfyui"):
        ok = log_manual_or_switch(cfg, "人工通过素材 A1", action="MANUAL_REVIEW",
                                  operator="alice")
    assert ok is True
    assert any("MANUAL_REVIEW" in r.message for r in caplog.records), (
        f"没有记录人工操作日志：{[r.message for r in caplog.records]}"
    )


def test_p4_switch_flag_actually_disables_logging(caplog):
    """开关为 False 时**不记录** —— 开关必须真的改变行为，不能是摆设。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.logging_setup import log_manual_or_switch

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.log_all_manual_and_auto_switch = False
    with caplog.at_level(logging.INFO, logger="bettercomfyui"):
        ok = log_manual_or_switch(cfg, "不该出现的日志", action="AUTO_SWITCH")
    assert ok is False
    assert not any("不该出现的日志" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# P5 / P6 配置导入导出
# ---------------------------------------------------------------------------


def test_p5_export_never_contains_real_secret(tmp_path):
    """导出必然脱敏：真实 api_key 不得出现在导出文本里。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.logging_setup import export_config_json

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.prompt.api_key = "sk-REAL-SECRET-DO-NOT-LEAK"
    text = export_config_json(cfg, tmp_path / "cfg.json")
    assert "sk-REAL-SECRET-DO-NOT-LEAK" not in text, "导出文件泄露了真实密钥！"
    assert "***" in text, "没有脱敏占位符"


def test_p6_import_drops_masked_placeholder(tmp_path):
    """导入时 `***` 必须被丢弃，不能写成字面量密钥。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.logging_setup import export_config_json, import_config_json

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.prompt.api_key = "sk-REAL"
    out = tmp_path / "cfg.json"
    export_config_json(cfg, out)

    merged = import_config_json(out)
    assert merged.get("prompt", {}).get("api_key", None) is None, (
        "脱敏占位 *** 被当成真值导入了"
    )
    # 其余字段要正常带回来
    assert merged.get("assets", {}).get("resolution")


# ---------------------------------------------------------------------------
# P7–P11 音频拼接三态（真跑 ffmpeg）
# ---------------------------------------------------------------------------


def _ffmpeg():
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    return _ffmpeg_exe()


def _make_video(path: Path) -> Path:
    subprocess.run(
        [_ffmpeg(), "-y",
         "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(path)],
        capture_output=True, check=True,
    )
    return path


def _make_audio(path: Path) -> Path:
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
         "-c:a", "aac", str(path)],
        capture_output=True, check=True,
    )
    return path


def _has_audio(path: Path) -> bool:
    proc = subprocess.run([_ffmpeg(), "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    return "Audio:" in proc.stderr


@pytest.fixture()
def media(tmp_path):
    return _make_video(tmp_path / "in.mp4"), _make_audio(tmp_path / "bgm.m4a")


def test_p7_audio_join_auto_keeps_audio(tmp_path, media):
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import encode

    src, _aud = media
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.encode.audio_join = "auto"
    out = tmp_path / "auto.mp4"
    encode(src, cfg, out_path=out)
    assert _has_audio(out), "auto 模式丢了音轨"


def test_p8_audio_join_mute_removes_audio(tmp_path, media):
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import encode

    src, _aud = media
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.encode.audio_join = "mute"
    out = tmp_path / "mute.mp4"
    encode(src, cfg, out_path=out)
    assert not _has_audio(out), "mute 模式仍有音轨"


def test_p9_audio_join_manual_replaces_audio(tmp_path, media):
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import encode

    src, aud = media
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.encode.audio_join = "manual"
    out = tmp_path / "manual.mp4"
    encode(src, cfg, out_path=out, audio_source=aud)
    assert _has_audio(out), "manual 模式没有音轨"


def test_p10_manual_without_audio_source_raises(tmp_path, media):
    """`manual` 不给音轨必须报错 —— 不静默回退到自动拼接。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import encode

    src, _aud = media
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.encode.audio_join = "manual"
    with pytest.raises(Exception) as excinfo:
        encode(src, cfg, out_path=tmp_path / "bad.mp4")
    assert "manual" in str(excinfo.value)


def test_p11_auto_with_audio_source_raises(tmp_path, media):
    """`auto` 却传了音轨属于矛盾输入，必须报错而不是悄悄忽略。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import encode

    src, aud = media
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.encode.audio_join = "auto"
    with pytest.raises(Exception) as excinfo:
        encode(src, cfg, out_path=tmp_path / "bad2.mp4", audio_source=aud)
    assert "冲突" in str(excinfo.value) or "audio_source" in str(excinfo.value)
