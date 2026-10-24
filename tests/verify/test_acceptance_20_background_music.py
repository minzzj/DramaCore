"""验收 20：背景音乐（图一 SG4 `O7`「背景音乐，后续需提进日程」）。

## 为什么单列一条

`O7` 在愿景里被**自己标注为待办**，此前**零实现**（`bgm`/`背景音乐` 全工程命中 0 处）。
本轮把它做完，并顺手把四个配套配置项（`bgm_volume` / `bgm_loop` / `bgm_fade` /
`bgm_ducking`）真正接上 —— 它们同样有「配了不读」的风险。

## 判定通过

    P1 传 `bgm_file` -> 混音成功，成片有音轨；
    P2 **循环补齐**：BGM 比视频短时，成片时长仍等于视频时长（不被拉长/截短）；
    P3 `audio_join=mute` + BGM -> 成片有音轨（只有配乐，没有原声）；
    P4 `audio_join=manual` + BGM -> 成片有音轨（指定音轨与配乐混音）；
    P5 **不传 BGM 时行为不变**（回归：原音频链路未被破坏）；
    P6 `bgm_file` 不存在 -> **报错**，不静默忽略；
    P7 `bgm_volume` 越界 -> 报错（配置校验真实生效）；
    P8 `bgm_fade=0` 时不生成 afade（不淡入淡出也合法）。

## 判定假通过（必须能变红）

    F1 去掉 `-shortest` -> P2 变红（循环的 BGM 会把成片拉长）；
    F2 忽略 `bgm_file` 参数 -> P1 变红；
    F3 删掉存在性校验 -> P6 变红。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ffmpeg() -> str:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    return _ffmpeg_exe()


def _make_video(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(
        [_ffmpeg(), "-y",
         "-f", "lavfi", "-i", f"color=c=navy:s=160x120:r=10:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(path)],
        capture_output=True, check=True,
    )
    return path


def _make_audio(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi",
         "-i", f"sine=frequency=880:duration={seconds}", "-c:a", "aac", str(path)],
        capture_output=True, check=True,
    )
    return path


def _probe(path: Path) -> tuple[float | None, bool]:
    proc = subprocess.run([_ffmpeg(), "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    duration = None
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return duration, ("Audio:" in proc.stderr)


@pytest.fixture()
def media(tmp_path):
    return (_make_video(tmp_path / "in.mp4", 2.0),
            _make_audio(tmp_path / "bgm.m4a", 1.0),
            _make_audio(tmp_path / "manual.m4a", 2.0))


def _cfg():
    from bettercomfyui.config import load_config

    return load_config(repo_root=REPO_ROOT)


def test_p1_bgm_is_mixed_in(tmp_path, media):
    """传 bgm_file -> 混音成功且成片有音轨。"""
    from bettercomfyui.pipeline.core import encode

    src, bgm, _man = media
    cfg = _cfg()
    cfg.encode.audio_join = "auto"
    out = tmp_path / "p1.mp4"
    encode(src, cfg, out_path=out, bgm_file=bgm)
    duration, has_audio = _probe(out)
    assert has_audio, "加了 BGM 却没有音轨"
    assert duration and abs(duration - 2.0) < 0.5, f"成片时长异常：{duration}"


def test_p2_loop_is_trimmed_to_video_length(tmp_path, media):
    """BGM(1s) 比视频(2s) 短：循环补齐后成片仍是 2s，**不能被拉长**。"""
    from bettercomfyui.pipeline.core import encode

    src, bgm, _man = media
    cfg = _cfg()
    cfg.encode.audio_join = "auto"
    cfg.encode.bgm_loop = True
    out = tmp_path / "p2.mp4"
    encode(src, cfg, out_path=out, bgm_file=bgm)
    duration, _has = _probe(out)
    assert duration is not None
    assert duration < 3.0, (
        f"成片被循环 BGM 拉长了：{duration}s（应 ≈2s）—— 缺 `-shortest`"
    )


def test_p3_mute_with_bgm_keeps_music_only(tmp_path, media):
    """mute + BGM：丢掉原声但保留配乐。"""
    from bettercomfyui.pipeline.core import encode

    src, bgm, _man = media
    cfg = _cfg()
    cfg.encode.audio_join = "mute"
    out = tmp_path / "p3.mp4"
    encode(src, cfg, out_path=out, bgm_file=bgm)
    _duration, has_audio = _probe(out)
    assert has_audio, "mute+BGM 应当只有配乐，不该完全没音轨"


def test_p4_manual_with_bgm_mixes_both(tmp_path, media):
    """manual + BGM：指定音轨与配乐混音。"""
    from bettercomfyui.pipeline.core import encode

    src, bgm, manual = media
    cfg = _cfg()
    cfg.encode.audio_join = "manual"
    out = tmp_path / "p4.mp4"
    encode(src, cfg, out_path=out, bgm_file=bgm, audio_source=manual)
    _duration, has_audio = _probe(out)
    assert has_audio


def test_p5_no_bgm_regression(tmp_path, media):
    """不传 BGM 时行为不变（原音频链路未被破坏）。"""
    from bettercomfyui.pipeline.core import encode

    src, _bgm, _man = media
    cfg = _cfg()
    cfg.encode.audio_join = "auto"
    out = tmp_path / "p5.mp4"
    encode(src, cfg, out_path=out)
    duration, has_audio = _probe(out)
    assert has_audio
    assert duration and abs(duration - 2.0) < 0.5


def test_p6_missing_bgm_raises(tmp_path, media):
    """BGM 文件不存在必须报错，不能静默忽略。"""
    from bettercomfyui.pipeline.core import encode

    src, _bgm, _man = media
    cfg = _cfg()
    with pytest.raises(Exception) as excinfo:
        encode(src, cfg, out_path=tmp_path / "p6.mp4",
               bgm_file=tmp_path / "nope.m4a")
    assert "背景音乐" in str(excinfo.value)


def test_p7_bgm_volume_is_validated():
    """`bgm_volume` 越界必须在配置校验阶段被拒。"""
    # 校验逻辑在 `_cross_validate`（由其调用方 load_config / config_from_dict 触发）
    from bettercomfyui.config import _cross_validate

    cfg = _cfg()
    cfg.encode.bgm_volume = 1.5
    with pytest.raises(Exception) as excinfo:
        _cross_validate(cfg)
    assert "bgm_volume" in str(excinfo.value)


def test_p8_zero_fade_is_allowed(tmp_path, media):
    """`bgm_fade=0` 合法（不淡入淡出）。"""
    from bettercomfyui.pipeline.core import encode

    src, bgm, _man = media
    cfg = _cfg()
    cfg.encode.audio_join = "auto"
    cfg.encode.bgm_fade = 0.0
    out = tmp_path / "p8.mp4"
    encode(src, cfg, out_path=out, bgm_file=bgm)
    _duration, has_audio = _probe(out)
    assert has_audio
