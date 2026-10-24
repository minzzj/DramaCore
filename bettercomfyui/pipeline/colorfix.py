# -*- coding: utf-8 -*-
"""ROUND72：出片自动调色（亮度/对比/饱和度 + 段间一致性）。

问题：H3 4步蒸馏 LoRA 输出偏暗、低饱和、高光不足，且逐段独立生成导致段间亮度跳变。
做法：对每个已生成段抽样测量 luma/暗部/高光/HSV 饱和度，若低于“酒会基准”
（平均亮度 100-130、暗部<25%、高光>8%、饱和 50-60%），用 ffmpeg eq 逐段校正到
统一目标（同一目标 → 段间亮度一致），重新编码后再拼接。找不到 ffmpeg/numpy 则跳过。
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

TARGET_YAVG = 112.0          # 0-255，酒会/商业片明亮基准
TARGET_DARK_RATIO = 0.24     # luma<60 占比
TARGET_HIGH_RATIO = 0.09     # luma>200 占比
TARGET_SAT = 0.54            # HSV S 均值（0-1）

__all__ = ["normalize_segments", "measure_segment", "TARGET_YAVG", "TARGET_SAT"]


def _ffmpeg_exe() -> str | None:
    try:
        from .core import _ffmpeg_exe as core_ffmpeg

        exe = core_ffmpeg()
        if exe and Path(str(exe)).is_file():
            return str(exe)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("ffmpeg")


def _duration_seconds(ffmpeg: str, path: Path) -> float:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=60)
        text = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _probe_stats(ffmpeg: str, path: Path, samples=(0.2, 0.5, 0.8)) -> dict[str, float] | None:
    try:
        import numpy as np  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    dur = _duration_seconds(ffmpeg, path)
    if dur <= 0.2:
        return None
    rows = []
    for frac in samples:
        t = max(0.0, min(dur - 0.05, dur * frac))
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
                 "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                capture_output=True, timeout=120)
        except Exception:  # noqa: BLE001
            continue
        buf = r.stdout or b""
        if len(buf) < 3000:
            continue
        arr = np.frombuffer(buf, dtype=np.uint8)
        if arr.size % 3:
            arr = arr[: arr.size - (arr.size % 3)]
        arr = arr.reshape(-1, 3).astype(np.float32)
        luma = arr[:, 0] * 0.299 + arr[:, 1] * 0.587 + arr[:, 2] * 0.114
        mx = arr.max(axis=1)
        mn = arr.min(axis=1)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0)
        rows.append((float(luma.mean()), float((luma < 60).mean()),
                     float((luma > 200).mean()), float(sat.mean())))
    if not rows:
        return None
    n = len(rows)
    return {
        "yavg": sum(r[0] for r in rows) / n,
        "dark": sum(r[1] for r in rows) / n,
        "high": sum(r[2] for r in rows) / n,
        "sat": sum(r[3] for r in rows) / n,
    }


def _looks_dull(st: dict[str, float]) -> bool:
    return (st["yavg"] < 100.0 or st["sat"] < 0.50
            or st["high"] < 0.05 or st["dark"] > 0.32)


def _filter_for(st: dict[str, float]) -> str:
    y = max(6.0, float(st["yavg"]))
    # ffmpeg eq 的 gamma: out = in^(1/gamma)，gamma>1 变亮、<1 变暗
    # 目标：target/255 = (y/255)^(1/gamma) → gamma = ln(y/255) / ln(target/255)
    gamma = math.log(max(1e-3, y / 255.0)) / math.log(max(1e-3, TARGET_YAVG / 255.0))
    gamma = max(0.75, min(2.20, gamma))
    # gamma 提亮会抬高 HSV 的 V，使实测饱和度下降（约按 1/gamma 衰减）；
    # 因此 eq 饱和度倍数要把这部分补回来：target * gamma / 当前饱和度
    sat = TARGET_SAT * gamma / max(0.05, float(st["sat"]))
    sat = max(1.0, min(2.40, sat))
    contrast = 1.0
    if st["high"] < 0.06 and st["dark"] < 0.45:
        contrast = 1.06
    return "eq=gamma={:.3f}:contrast={:.3f}:saturation={:.3f}".format(gamma, contrast, sat)


def _encode(ffmpeg: str, src: Path, dst: Path, vf: str) -> bool:
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-c:a", "copy", "-movflags", "+faststart", str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        return r.returncode == 0 and dst.is_file() and dst.stat().st_size > 1024
    except Exception:  # noqa: BLE001
        return False


def measure_segment(path: str | Path) -> dict[str, float] | None:
    """对外暴露：测量单段亮度/暗部/高光/饱和度（供审计/调试）。"""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return None
    return _probe_stats(ffmpeg, Path(path))


def normalize_segments(seg_files: list[str], *, workdir: str | Path,
                       audit: list[str] | None = None,
                       warnings: list[str] | None = None,
                       target_yavg: float = TARGET_YAVG) -> list[str]:
    """逐段测量并校正到统一目标；返回校正后的文件列表（失败/无需校正的原样返回）。"""
    audit = audit if audit is not None else []
    warnings = warnings if warnings is not None else []
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        warnings.append("⚠️ 自动调色跳过：找不到 ffmpeg")
        return list(seg_files)
    try:
        import numpy  # noqa: F401
    except Exception:  # noqa: BLE001
        warnings.append("⚠️ 自动调色跳过：缺少 numpy")
        return list(seg_files)
    out_dir = Path(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: list[str] = []
    changed = 0
    for idx, f in enumerate(seg_files):
        src = Path(str(f))
        if not src.is_file():
            result.append(str(src))
            continue
        st = _probe_stats(ffmpeg, src)
        if not st:
            result.append(str(src))
            continue
        if not _looks_dull(st):
            audit.append("调色：段{} 已达标（亮度{:.0f} 暗部{:.0%} 高光{:.0%} 饱和{:.0%}）→ 不处理".format(
                idx + 1, st["yavg"], st["dark"], st["high"], st["sat"]))
            result.append(str(src))
            continue
        t0 = time.time()
        dst = out_dir / ("seg_{:03d}_color.mp4".format(idx + 1))
        vf = _filter_for(st)
        ok = _encode(ffmpeg, src, dst, vf)
        st2 = _probe_stats(ffmpeg, dst) if ok else None
        if not ok or not st2:
            warnings.append("⚠️ 自动调色失败（段{}，保留原片）".format(idx + 1))
            result.append(str(src))
            continue
        # 一次不够再补一次（gamma 重新计算）
        if (abs(st2["yavg"] - target_yavg) > 14 or st2["sat"] < 0.48) and st2["yavg"] > 0:
            vf2 = _filter_for({"yavg": st2["yavg"], "dark": st2["dark"],
                               "high": st2["high"], "sat": st2["sat"]})
            dst2 = out_dir / ("seg_{:03d}_color2.mp4".format(idx + 1))
            if _encode(ffmpeg, dst, dst2, vf2):
                st3 = _probe_stats(ffmpeg, dst2)
                if st3 and abs(st3["yavg"] - target_yavg) < abs(st2["yavg"] - target_yavg):
                    dst, st2 = dst2, st3
        audit.append(
            "调色：段{} 亮度 {:.0f}→{:.0f} 暗部{:.0%}→{:.0%} 高光{:.0%}→{:.0%} 饱和{:.0%}→{:.0%}"
            "（{}，{:.1f}s）".format(
                idx + 1, st["yavg"], st2["yavg"], st["dark"], st2["dark"],
                st["high"], st2["high"], st["sat"], st2["sat"], vf, time.time() - t0))
        result.append(str(dst))
        changed += 1
    if changed:
        warnings.append("ℹ️ 自动调色：{} 段偏暗/低饱和已按统一基准校正（段间亮度差应明显缩小）".format(changed))
    return result
