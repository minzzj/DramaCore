#!/usr/bin/env python
"""下载 MiniMax H3 模型权重（约 60GB）—— 为切到 ≥24GB 显存的目标机做准备。

## 为什么不用上游自带的 `download_models.py`

读了 `ComfyUI-MiniMaxH3-ChainDirector/scripts/download_models.py`，有四个实际问题：

1. **根目录定位不可靠**：它用 `dirname(dirname(dirname(here)))` 往上跳三级。
   脚本位于 `custom_nodes/ComfyUI-MiniMaxH3-ChainDirector/scripts/`，
   跳三级会跑到 `custom_nodes` 的**上一级**。实测本机布局下这个推断是错的
   —— 它恰好能命中也只是因为目录深度凑巧。
2. **下载中断要从头再来**：60GB 下一个断线就白等。没有 Range 续传。
3. **没有磁盘空间检查**：60GB 下到一半才发现盘满。
4. **没有大小校验**：只看文件存在且非 0 字节。截断的 safetensors
   在加载时才会报错，而那时你已经等了很久。

本脚本逐条解决：显式传 ComfyUI 根目录（或从配置读）、HTTP Range 断点续传、
下载前检查可用空间、下载后按 `Content-Length` 校验大小、已完成的跳过。

用法：
    # 从配置读 ComfyUI 路径（推荐）
    python scripts/download_models.py --comfy-root-from-config

    # 显式指定
    python scripts/download_models.py --comfy-root "H:\\...\\Better comfyUI\\.comfyui"

    # 只检查不下载（看清单、空间、已有什么）
    python scripts/download_models.py --comfy-root-from-config --check
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (HF 仓库, 仓库内路径, 本地目标子目录)
FILES: tuple[tuple[str, str, str], ...] = (
    ("Comfy-Org/MiniMax-H3",
     "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
     "diffusion_models"),
    ("Comfy-Org/MiniMax-H3",
     "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
     "diffusion_models"),
    ("Comfy-Org/MiniMax-H3",
     "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
     "text_encoders"),
    ("Comfy-Org/MiniMax-H3",
     "vae/minimax_h3_video_vae_fp16.safetensors",
     "vae"),
    ("Comfy-Org/MiniMax-H3",
     "vae/minimax_h3_audio_vae_fp32.safetensors",
     "vae"),
    ("Kijai/MiniMax-H3_comfy",
     "loras/minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy_resized_avg_rank_21_bf16.safetensors",
     "loras"),
)

MIRRORS = {
    "hf": "https://hf-mirror.com",     # 国内镜像，默认
    "hfco": "https://huggingface.co",  # 原生
}

#: 预估总量（GB）。仅用于空间检查与提示；实际以 Content-Length 为准。
ESTIMATED_TOTAL_GB = 60.0

CHUNK = 1024 * 1024
UA = {"User-Agent": "Mozilla/5.0 (bettercomfyui downloader)"}


def resolve_comfy_root(explicit: str | None, from_config: bool) -> Path:
    """确定 ComfyUI 根目录。**不猜**：给不出就报错并告诉用户怎么给。"""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_dir():
            raise SystemExit(f"[!] 指定的 ComfyUI 根目录不存在：{p}")
        return p

    if from_config:
        sys.path.insert(0, str(REPO_ROOT))
        from bettercomfyui.config import load_config

        cfg = load_config(repo_root=REPO_ROOT)
        base = cfg.global_.comfyui_base_path
        if not base:
            raise SystemExit(
                "[!] 配置里 global.comfyui_base_path 为空。\n"
                f"    请在 {REPO_ROOT / 'config' / 'user.yaml'} 里填写 ComfyUI 实例根目录，"
                "或改用 --comfy-root 显式指定。"
            )
        p = Path(base)
        if not p.is_dir():
            raise SystemExit(f"[!] 配置里的 ComfyUI 路径不存在：{p}")
        return p

    # 最后兜底：从本脚本位置向上找带 custom_nodes 的目录（比上游的固定三级跳更稳）
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "custom_nodes").is_dir() and (parent / "models").is_dir():
            return parent
    raise SystemExit(
        "[!] 无法自动确定 ComfyUI 根目录。请用 --comfy-root 指定，"
        "或加 --comfy-root-from-config 从配置读。"
    )


def free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except OSError:
        return -1.0


def remote_size(url: str) -> int | None:
    """用一次 HEAD（失败则退化为 Range 请求）拿远端大小。"""
    for attempt in (("HEAD", None), ("GET", "bytes=0-0")):
        method, rng = attempt
        req = urllib.request.Request(url, headers=dict(UA), method=method)
        if rng:
            req.add_header("Range", rng)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if method == "HEAD":
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        return int(cl)
                else:
                    cr = resp.headers.get("Content-Range")
                    if cr and "/" in cr:
                        return int(cr.rsplit("/", 1)[1])
        except Exception:  # noqa: BLE001 - 换下一种方式
            continue
    return None


def download(url: str, dest: Path, *, retries: int = 5) -> str:
    """带断点续传的下载。

    返回状态字符串：`OK` / `SKIPPED` / `FAILED` / `SIZE_MISMATCH`。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = remote_size(url)

    if dest.is_file() and dest.stat().st_size > 0:
        have = dest.stat().st_size
        if expected and have == expected:
            print(f"  [跳过] {dest.name} 已完成（{have / 1024**3:.2f} GB）")
            return "SKIPPED"
        if expected and have > expected:
            print(f"  [!] {dest.name} 比远端大（{have} > {expected}），删除重下")
            dest.unlink()
        else:
            print(f"  [续传] {dest.name} 已有 {have / 1024**3:.2f} GB"
                  + (f" / {expected / 1024**3:.2f} GB" if expected else ""))

    for attempt in range(1, retries + 1):
        have = dest.stat().st_size if dest.is_file() else 0
        req = urllib.request.Request(url, headers=dict(UA))
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                # 服务器不支持 Range 时会返回 200 且从头开始 —— 必须从头写，不能追加
                if have and resp.status == 200:
                    print("  [!] 服务器不支持断点续传，从头开始")
                    have = 0
                mode = "ab" if have else "wb"
                t0 = time.time()
                last = 0.0
                with open(dest, mode) as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        now = time.time()
                        if now - last > 2.0:
                            last = now
                            mb = have / 1024**2
                            if expected:
                                pct = have * 100 / expected
                                speed = mb / max(now - t0, 0.001)
                                print(f"\r    {pct:5.1f}%  {mb:8.1f} MB  {speed:6.1f} MB/s",
                                      end="", flush=True)
                            else:
                                print(f"\r    {mb:8.1f} MB", end="", flush=True)
                print("", flush=True)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"\n  [!] 第 {attempt}/{retries} 次失败：{type(exc).__name__}: {exc}")
            if attempt == retries:
                return "FAILED"
            time.sleep(min(2 ** attempt, 30))
            continue

        got = dest.stat().st_size
        if expected and got != expected:
            print(f"  [!] 大小不符：本地 {got} != 远端 {expected}，将重试")
            if attempt == retries:
                return "SIZE_MISMATCH"
            continue
        print(f"  [完成] {dest.name}  {got / 1024**3:.2f} GB")
        return "OK"

    return "FAILED"


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 MiniMax H3 权重（带续传与校验）")
    ap.add_argument("--comfy-root", default=None, help="ComfyUI 实例根目录")
    ap.add_argument("--comfy-root-from-config", action="store_true",
                    help="从 config/user.yaml 的 global.comfyui_base_path 读取")
    ap.add_argument("--mirror", choices=list(MIRRORS), default="hf")
    ap.add_argument("--check", action="store_true", help="只检查清单/空间/已有文件，不下载")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只下载文件名包含这些子串的项（便于分批）")
    args = ap.parse_args()

    root = resolve_comfy_root(args.comfy_root, args.comfy_root_from_config)
    base = MIRRORS[args.mirror]
    models = root / "models"

    print("=" * 68)
    print("MiniMax H3 权重下载")
    print("=" * 68)
    print(f"ComfyUI 根目录 : {root}")
    print(f"镜像           : {base}")
    print(f"目标目录       : {models}")
    print()

    items: list[tuple[str, Path, str]] = []
    for repo, path, dest_dir in FILES:
        name = os.path.basename(path)
        if args.only and not any(s in name for s in args.only):
            continue
        items.append((f"{base}/{repo}/resolve/main/{path}", models / dest_dir / name, name))

    if not items:
        print("[!] 没有匹配的下载项（检查 --only）")
        return 2

    # ---- 空间检查：宁可先拒绝，也不要下到一半盘满 ----
    need = 0.0
    have_already = 0
    print("下载清单：")
    for url, dest, name in items:
        exists = dest.is_file() and dest.stat().st_size > 0
        if exists:
            have_already += 1
            print(f"  [已有] {dest.relative_to(root)}  "
                  f"{dest.stat().st_size / 1024**3:.2f} GB")
        else:
            print(f"  [待下] {dest.relative_to(root)}")

    free = free_gb(models if models.is_dir() else root)
    if free >= 0:
        print(f"\n可用空间 : {free:.1f} GB")
    if args.check:
        print(f"预估总量 : 约 {ESTIMATED_TOTAL_GB:.0f} GB（全部 6 个文件）")
        if free >= 0 and free < ESTIMATED_TOTAL_GB:
            print(f"[!] 可用空间 {free:.1f} GB 可能不足（需约 {ESTIMATED_TOTAL_GB:.0f} GB）")
        print("\n（--check 模式：未下载任何文件）")
        return 0

    if have_already == len(items):
        print("\n全部文件已存在，无需下载。")
        return 0

    if free >= 0 and free < ESTIMATED_TOTAL_GB:
        print(
            f"\n[!] 可用空间不足：{free:.1f} GB < 约 {ESTIMATED_TOTAL_GB:.0f} GB。\n"
            "    请先清理磁盘，或改用 --only 分批下载到其他盘后手动放置。"
        )
        return 2

    print()
    results: dict[str, str] = {}
    for url, dest, name in items:
        print(f"[下载] {name}")
        results[name] = download(url, dest)

    print()
    print("=" * 68)
    print("结果")
    print("=" * 68)
    for name, status in results.items():
        mark = {"OK": "[完成]", "SKIPPED": "[已有]", "FAILED": "[失败]",
                "SIZE_MISMATCH": "[大小不符]"}.get(status, f"[{status}]")
        print(f"  {mark} {name}")

    bad = {k: v for k, v in results.items() if v not in ("OK", "SKIPPED")}
    if bad:
        print(
            f"\n[!] {len(bad)} 个文件未完成。**可以直接重跑本脚本**："
            "已下载的部分会断点续传，不会从头再来。"
        )
        return 1

    # ---- 用 doctor 的 probe 复核：现在应该能报出权重 ----
    print("\n复核（用编排层的 doctor 探测口径）：")
    sys.path.insert(0, str(REPO_ROOT))
    from bettercomfyui.cli import _probe_comfyui

    probe = _probe_comfyui(root)
    print(f"  版本       : {probe.get('version') or '未知'}")
    print(f"  权重文件数 : {probe.get('model_files')}")
    for s in probe.get("weight_samples", [])[:6]:
        print(f"    - {s}")
    print("\n下载完成。重启 ComfyUI 即可使用。")
    print("重启后请在 ComfyUI 里跑一次「Better ComfyUI 融合·体检」节点，")
    print("它会用真实 pack_* 完成字段比对（本机因缺权重无法做的最后一步）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
