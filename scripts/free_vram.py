# -*- coding: utf-8 -*-
"""向 ComfyUI 请求释放模型显存（POST /free）。

用途：32GB 生产机分两段跑时，第一段（素材/图像模型）结束后调用一次，
把模型从显存卸载，避免第二段（H3）与图像模型同时驻留导致 OOM。

用法：
    python -B scripts/free_vram.py                # 默认 http://127.0.0.1:8188
    python -B scripts/free_vram.py --comfy http://127.0.0.1:8188
    python -B scripts/free_vram.py --check        # 只读 /system_stats 看显存

本脚本未在 GPU 机器验证显存确实下降（CPU 机器只能验证 HTTP 调通）；请配合
scripts/vram_monitor.py 在目标机确认「free 前后显存下降」。
"""
from __future__ import annotations

import argparse
import json
import urllib.request


def _post(url: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(getattr(resp, "status", 200)), resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _stats(comfy: str) -> dict:
    try:
        with urllib.request.urlopen(f"{comfy.rstrip('/')}/system_stats", timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comfy", default="http://127.0.0.1:8188")
    parser.add_argument("--check", action="store_true", help="只读显存状态，不释放")
    args = parser.parse_args()
    before = _stats(args.comfy)
    print("[before]", json.dumps(before.get("system", before), ensure_ascii=False)[:300])
    if args.check:
        return 0 if "error" not in before else 1
    code, body = _post(f"{args.comfy.rstrip('/')}/free",
                       {"unload_models": True, "free_memory": True})
    print(f"[free] status={code} body={body[:200]}")
    after = _stats(args.comfy)
    print("[after]", json.dumps(after.get("system", after), ensure_ascii=False)[:300])
    if code != 200:
        print("[FAIL] /free 调用失败（ComfyUI 版本/路由可能不同）")
        return 1
    print("[OK] 已请求释放显存（是否真的下降需在 GPU 机器上用 nvidia-smi 确认）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
