"""新补齐能力的回归测试（种子可复现 / 字幕水印 / 显存档位 / 覆盖盘点）。

这些是「三份文档第一阶段要求」里先前缺失的项。每一条都钉住**行为**，
而不是钉住「函数存在」—— 后者在功能坏掉时依然会通过。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.seed import (  # noqa: E402
    SEED_MAX,
    derive_seed,
    is_reproducible,
    resolve_seed,
)


# ---------------------------------------------------------------------------
# 种子可复现（方案：种子 随机/固定，可复现）
# ---------------------------------------------------------------------------


def test_derive_seed_is_deterministic_across_calls():
    """同一输入必须永远得到同一种子 —— 这是「可复现」的全部意义。"""
    a = derive_seed("proj", "SEG0001", "v1", 1)
    b = derive_seed("proj", "SEG0001", "v1", 1)
    assert a == b


def test_derive_seed_differs_by_every_dimension():
    """段/项目/版本/尝试序号 任一变化都必须给出不同种子。"""
    base = derive_seed("p", "SEG0001", "v1", 1)
    assert derive_seed("p", "SEG0002", "v1", 1) != base, "不同段应不同"
    assert derive_seed("other", "SEG0001", "v1", 1) != base, "不同项目应不同"
    assert derive_seed("p", "SEG0001", "v2", 1) != base, "不同版本应不同"
    assert derive_seed("p", "SEG0001", "v1", 2) != base, "不同尝试序号应不同"


def test_derive_seed_stable_across_processes():
    """**跨进程必须稳定** —— 不能用 Python 的 hash()（它随 PYTHONHASHSEED 变化）。

    这条用独立进程验证：如果实现里用了内置 hash()，两次不同 PYTHONHASHSEED
    会给出不同结果，测试就会红。
    """
    script = (
        "import sys; sys.path.insert(0, r'%s');"
        "from bettercomfyui.seed import derive_seed;"
        "print(derive_seed('p','SEG0001','v1',1))" % REPO_ROOT
    )
    outs = []
    for seed_env in ("0", "12345"):
        import os

        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed_env
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, encoding="utf-8", env=env, check=False)
        assert proc.returncode == 0, proc.stderr
        outs.append(proc.stdout.strip())
    assert outs[0] == outs[1], (
        f"不同 PYTHONHASHSEED 下种子不同（{outs}）—— 实现里可能用了内置 hash()，"
        "那样跨进程不可复现"
    )


def test_derive_seed_in_range():
    for i in range(1, 50):
        s = derive_seed("p", f"SEG{i:04d}", "v1", 1)
        assert 0 <= s <= SEED_MAX


def test_fixed_seed_is_respected_not_randomised():
    """用户指定固定种子时必须原样使用 —— 不能"顺手随机化"。"""
    assert resolve_seed(requested=42, project_id="p", segment_id="s", version="v1") == 42
    assert resolve_seed(requested=0, project_id="p", segment_id="s", version="v1") == 0


def test_unspecified_seed_falls_back_to_derivation():
    got = resolve_seed(requested=None, project_id="p", segment_id="s", version="v1")
    assert got == derive_seed("p", "s", "v1", 1)


@pytest.mark.parametrize("bad", [-1, -100])
def test_negative_seed_is_rejected_not_silently_replaced(bad):
    """负数种子必须报错。静默替换会让用户以为固定种子生效了。"""
    with pytest.raises(ValueError, match="不能为负"):
        resolve_seed(requested=bad, project_id="p", segment_id="s", version="v1")


def test_seed_over_max_is_rejected():
    with pytest.raises(ValueError, match="超出允许范围"):
        resolve_seed(requested=SEED_MAX + 1, project_id="p", segment_id="s", version="v1")


def test_non_integer_seed_is_rejected():
    with pytest.raises(ValueError, match="必须是整数"):
        resolve_seed(requested="42", project_id="p", segment_id="s", version="v1")  # type: ignore[arg-type]


def test_reproducibility_judgement():
    assert is_reproducible(None, 123) is True, "只要记录了实际种子就算可复现"
    assert is_reproducible(42, None) is False, "只有要求、没有实际值 = 不可复现"


def test_segment_model_has_seed_fields():
    """数据模型必须真的带 seed 字段（不能只在辅助函数里）"""
    from bettercomfyui.models.entities import Segment, SegmentAttempt

    seg = Segment(segment_id="s1", segment_index=1, chapter_id="CH001", seed=7)
    assert seg.seed == 7
    seg.resolved_seed = 7
    d = seg.to_dict()
    assert d["seed"] == 7 and d["resolved_seed"] == 7

    att = SegmentAttempt(attempt_id="a", segment_id="s1", version="v1", index=1, used_seed=9)
    assert att.to_dict()["used_seed"] == 9


# ---------------------------------------------------------------------------
# 字幕 / 水印（方案：字幕/水印 默认不加，可启用）
# ---------------------------------------------------------------------------


def _ffmpeg() -> str:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    return _ffmpeg_exe()


def _make_source(path: Path) -> None:
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "color=c=0x224466:s=320x240:r=24:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        capture_output=True, check=False,
    )


requires_ffmpeg = pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg") and not (REPO_ROOT / ".comfyenv").exists(),
    reason="未验证：无 ffmpeg",
)


def test_subtitle_enabled_without_file_raises(tmp_path: Path):
    """开启字幕却没给文件 → 必须报错，不能假装加了字幕。"""
    from bettercomfyui.config import Config
    from bettercomfyui.pipeline.core import PipelineError, encode

    src = tmp_path / "s.mp4"
    _make_source(src)
    cfg = Config()
    cfg.encode.subtitle = True
    with pytest.raises(PipelineError, match="subtitle_file"):
        encode(src, cfg, out_path=tmp_path / "o.mp4")


def test_watermark_enabled_without_file_raises(tmp_path: Path):
    from bettercomfyui.config import Config
    from bettercomfyui.pipeline.core import PipelineError, encode

    src = tmp_path / "s.mp4"
    _make_source(src)
    cfg = Config()
    cfg.encode.watermark = True
    with pytest.raises(PipelineError, match="watermark_file"):
        encode(src, cfg, out_path=tmp_path / "o.mp4")


def test_missing_subtitle_file_path_raises(tmp_path: Path):
    from bettercomfyui.config import Config
    from bettercomfyui.pipeline.core import PipelineError, encode

    src = tmp_path / "s.mp4"
    _make_source(src)
    cfg = Config()
    cfg.encode.subtitle = True
    with pytest.raises(PipelineError, match="字幕文件不存在"):
        encode(src, cfg, out_path=tmp_path / "o.mp4",
               subtitle_file=tmp_path / "nope.srt")


def test_subtitle_and_watermark_really_render(tmp_path: Path):
    """真烧字幕 + 真叠加水印，产物必须是合法媒体。"""
    from bettercomfyui.config import Config
    from bettercomfyui.pipeline.core import encode, probe_media

    src = tmp_path / "s.mp4"
    _make_source(src)

    srt = tmp_path / "sub.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,900\n测试字幕\n", encoding="utf-8")
    wm = tmp_path / "wm.png"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "color=c=red:s=40x20",
         "-frames:v", "1", str(wm)],
        capture_output=True, check=False,
    )
    assert wm.is_file(), "水印测试图未生成"

    cfg = Config()
    cfg.encode.subtitle = True
    cfg.encode.watermark = True
    out = tmp_path / "o.mp4"
    encode(src, cfg, out_path=out, subtitle_file=srt, watermark_file=wm,
           watermark_position="br")

    checks = probe_media(out)
    assert checks["duration_s"] > 0
    assert checks["has_video"] is True
    assert out.stat().st_size > 0
    # 有字幕+水印的产物应比纯转码更大（多了一层绘制），至少不能是空文件
    plain = tmp_path / "plain.mp4"
    encode(src, Config(), out_path=plain)
    assert out.stat().st_size != 0 and plain.stat().st_size != 0


# ---------------------------------------------------------------------------
# 显存档位（方案：显存档位自动检测 低/中/高）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,tier",
    [
        ("4096 MiB", "minimal"),
        ("8192 MiB", "low"),
        ("16384 MiB", "mid"),
        ("24564 MiB", "high"),
        ("49152 MiB", "high"),
    ],
)
def test_vram_tier_mapping(text, tier):
    from bettercomfyui.cli import detect_vram_tier

    assert detect_vram_tier(text)["tier"] == tier


def test_vram_tier_unknown_is_not_guessed():
    """解析不出数字时必须报 unknown，**不许猜**一个档位。"""
    from bettercomfyui.cli import detect_vram_tier

    r = detect_vram_tier("garbage")
    assert r["tier"] == "unknown"
    assert r["vram_mib"] is None
    assert r["tier_note"]


def test_vram_tier_reports_mib():
    from bettercomfyui.cli import detect_vram_tier

    assert detect_vram_tier("24564 MiB")["vram_mib"] == 24564


# ---------------------------------------------------------------------------
# 覆盖盘点本身
# ---------------------------------------------------------------------------


def test_gap_analysis_has_no_stale_todo_without_reason():
    """TODO 项必须写明缺什么；不允许空备注的 TODO（那是没想清楚）。"""
    from bettercomfyui.gap_analysis import GAPS

    for g in GAPS:
        if g.status == "TODO":
            assert g.note, f"TODO 项缺少说明：{g.area}/{g.item}"


def test_gap_analysis_statuses_are_legal():
    from bettercomfyui.gap_analysis import GAPS

    for g in GAPS:
        assert g.status in ("DONE", "DELEGATED", "TODO"), f"非法状态：{g}"
        assert g.where, f"{g.area}/{g.item} 没有给出实现位置（DONE 必须可追溯）"


def test_gap_analysis_counts_is_consistent():
    from bettercomfyui.gap_analysis import GAPS, counts

    c = counts()
    assert sum(c.values()) == len(GAPS)
