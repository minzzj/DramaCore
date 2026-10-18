# -*- coding: utf-8 -*-
"""生成效率归一化计算器：按「生成耗时 / 输出视频秒数」计算，支持 480p/720p/1080p/480p+超分。

实测锚点（RTX 4090-48G，H3 turbo，单段 5.167s）：
  480p(864x480):   82.45 s/段  → 15.96 s / 输出秒
  720p(1280x736): 232.03 s/段  → 44.91 s / 输出秒（另加超分 0；这是原生）
  1080p(1920x1088):802.28 s/段 → 155.27 s / 输出秒
  480p+超分1080p: 82.45 + 42.0(超分均值) = 124.45 s/段 → 24.09 s / 输出秒

用法：
  python -B scripts/estimate_efficiency.py --segments 12 --res 480p
  python -B scripts/estimate_efficiency.py --output-seconds 60 --res 720p --speedup 1.55
  python -B scripts/estimate_efficiency.py --all
"""
from __future__ import annotations

import argparse
import json
import math

#: 实测单段生成耗时（秒）与单段输出秒数
MEASURED = {
    "480p": {"gen_s": 82.45, "seg_s": 5.167, "vram_gb": 45.58,
             "note": "864x480 原生（最低成本验证口径）"},
    "720p": {"gen_s": 232.03, "seg_s": 5.167, "vram_gb": 41.09,
             "note": "1280x736 原生"},
    "1080p": {"gen_s": 802.28, "seg_s": 5.167, "vram_gb": 42.43,
              "note": "1920x1088 原生（生产不可接受）"},
    "upscale1080p": {"gen_s": 82.45 + 42.0, "seg_s": 5.167, "vram_gb": 41.09,
                     "note": "480p 生成 + RealESRGAN x2 + lanczos 到 1080p（推荐）"},
}


def fmt_seconds(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}min"
    return f"{seconds/3600:.2f}h"


def report(res: str, *, speedup: float = 1.0, segments: int | None = None,
           output_seconds: float | None = None) -> dict:
    m = MEASURED[res]
    gen_s = m["gen_s"] / speedup
    s_per_out = gen_s / m["seg_s"]
    out = {
        "resolution": res,
        "note": m["note"],
        "speedup_applied": speedup,
        "gen_s_per_segment": round(gen_s, 2),
        "segment_output_s": m["seg_s"],
        "gen_s_per_output_s": round(s_per_out, 2),
        "vram_peak_gb": m["vram_gb"],
        "per_minute_output_norm": fmt_seconds(s_per_out * 60),
        "per_30min_output_norm": fmt_seconds(s_per_out * 1800),
        "per_hour_output_norm": fmt_seconds(s_per_out * 3600),
    }
    if segments:
        out["segments"] = segments
        out["output_seconds"] = round(segments * m["seg_s"], 2)
        out["total_gpu_s"] = round(segments * gen_s, 1)
        out["total_gpu_time"] = fmt_seconds(segments * gen_s)
        out["total_video_time"] = fmt_seconds(segments * m["seg_s"])
    if output_seconds:
        segs_needed = math.ceil(output_seconds / m["seg_s"])
        out["output_seconds_requested"] = output_seconds
        out["segments_needed"] = segs_needed
        out["total_gpu_s"] = round(segs_needed * gen_s, 1)
        out["total_gpu_time"] = fmt_seconds(segs_needed * gen_s)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--res", choices=list(MEASURED), default="upscale1080p")
    p.add_argument("--speedup", type=float, default=1.0,
                   help="相对 4090 的加速比（5090 预计 1.4~1.7；默认 1.0=4090）")
    p.add_argument("--segments", type=int, default=None)
    p.add_argument("--output-seconds", type=float, default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if args.all:
        rows = [report(r, speedup=args.speedup) for r in MEASURED]
    else:
        rows = [report(args.res, speedup=args.speedup, segments=args.segments,
                       output_seconds=args.output_seconds)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"{'分辨率/路线':<14}{'单段生成':>10}{'每输出秒':>10}{'1分钟成片':>12}{'30分钟':>10}{'1小时':>10}{'显存峰值':>10}")
    for r in rows:
        print(f"{r['resolution']:<14}{r['gen_s_per_segment']:>9.1f}s"
              f"{r['gen_s_per_output_s']:>9.1f}s{r['per_minute_output_norm']:>12}"
              f"{r['per_30min_output_norm']:>10}{r['per_hour_output_norm']:>10}"
              f"{r['vram_peak_gb']:>9.1f}G")
    print("\n说明：每输出秒 = 单段生成耗时 ÷ 单段输出秒数（5.167s）。")
    print("1080p 原生在 4090-48G 实测 802.28s/段，生产不推荐；推荐 480p+超分 1080p。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
