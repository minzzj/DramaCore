# -*- coding: utf-8 -*-
"""显存监控（生产 5090-32G 红线：峰值必须 <30.5GB）。

用法（GPU 机器上另开终端）：
    python -B scripts/vram_monitor.py --interval 2 --threshold 30.5 --out vram.csv
    python -B scripts/vram_monitor.py --interrupt-on-over --comfy http://127.0.0.1:8188

结束时打印：采样数/峰值/均值/超阈值次数，并写 CSV。无 nvidia-smi 时明确退出 2。

⚠️ 本脚本未在真 GPU 上验证（本机无 nvidia-smi）；逻辑简单，首次使用请先看 --help 输出。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def _query() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        raise FileNotFoundError("找不到 nvidia-smi（没有 NVIDIA 驱动/GPU）")
    proc = subprocess.run(
        [exe, "--query-gpu=timestamp,index,name,memory.used,memory.total,utilization.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"nvidia-smi 失败：{proc.stderr[-300:]}")
    rows = []
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        rows.append({
            "timestamp": parts[0], "index": parts[1], "name": parts[2],
            "used_mib": float(parts[3]), "total_mib": float(parts[4]),
            "util_pct": float(parts[5]),
        })
    return rows


def _interrupt(comfy: str) -> None:
    try:
        req = urllib.request.Request(f"{comfy.rstrip('/')}/interrupt", data=b"{}",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        print("[guard] 已向 ComfyUI 发送 /interrupt（超阈值保护）")
    except Exception as exc:  # noqa: BLE001
        print(f"[guard] /interrupt 失败：{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--threshold", type=float, default=30.5, help="GB，超过即告警")
    parser.add_argument("--duration", type=float, default=0, help="0=一直跑到 Ctrl+C")
    parser.add_argument("--out", default="vram.csv")
    parser.add_argument("--interrupt-on-over", action="store_true")
    parser.add_argument("--comfy", default="http://127.0.0.1:8188")
    args = parser.parse_args()

    try:
        first = _query()
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 2
    print(f"[start] GPU: {[r['name'] for r in first]} | 阈值 {args.threshold}GB | "
          f"间隔 {args.interval}s")
    out = Path(args.out)
    start = time.time()
    peak = 0.0
    total = 0.0
    count = 0
    over = 0
    interrupted = False
    try:
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["timestamp", "index", "name",
                                                    "used_mib", "total_mib", "util_pct",
                                                    "used_gb", "over_threshold"])
            writer.writeheader()
            while True:
                for row in _query():
                    used_gb = round(row["used_mib"] / 1024.0, 2)
                    is_over = used_gb >= args.threshold
                    writer.writerow({**row, "used_gb": used_gb, "over_threshold": int(is_over)})
                    fh.flush()
                    peak = max(peak, used_gb)
                    total += used_gb
                    count += 1
                    if is_over:
                        over += 1
                        print(f"[WARN] 显存 {used_gb:.2f}GB ≥ {args.threshold}GB（第 {over} 次）",
                              flush=True)
                        if args.interrupt_on_over and not interrupted:
                            _interrupt(args.comfy)
                            interrupted = True
                if args.duration and (time.time() - start) >= args.duration:
                    break
                time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print("\n[stop] Ctrl+C")
    mean = total / count if count else 0.0
    summary = {
        "samples": count,
        "peak_gb": round(peak, 2),
        "mean_gb": round(mean, 2),
        "threshold_gb": args.threshold,
        "over_threshold_count": over,
        "passed": bool(count and peak < args.threshold),
        "csv": str(out),
    }
    print("[SUMMARY] " + json.dumps(summary, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
