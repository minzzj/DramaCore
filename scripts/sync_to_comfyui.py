#!/usr/bin/env python
"""把工作区里的插件与编排包同步到 ComfyUI 的 custom_nodes。

**为什么需要它**

本仓库有两份「同一份代码」：

* `bettercomfyui/`、`bettercomfyui_comfy/`        —— 开发态（改动发生在这里）
* `.comfyui/custom_nodes/bettercomfyui/`、`.../bettercomfyui_comfy/` —— 运行态（ComfyUI 实际加载的）

两份手工维护必然漂移。实测已经吃过一次亏：修好的 `routes.py` 只更新了开发态，
运行态仍是旧的，于是装进 ComfyUI 后 `/bettercomfyui/doctor` 依旧 500。
**漂移会让「我明明修好了」变成一句空话。**

因此本脚本是唯一的同步入口，并在结束时**逐文件比对哈希**，不一致就报错退出。

用法：
    python scripts/sync_to_comfyui.py            # 同步 + 校验
    python scripts/sync_to_comfyui.py --check    # 只比对，不改动（CI / 提交前用）
    python scripts/sync_to_comfyui.py --comfyui <路径>
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]   # scripts/ 的上一级才是仓库根

#: 需要同步的 (工作区目录, custom_nodes 下的目录名)
PACKAGES = [
    ("bettercomfyui", "bettercomfyui"),
    ("bettercomfyui_comfy", "bettercomfyui_comfy"),
]

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_map(root: Path) -> dict[str, str]:
    """目录下所有文件的 {相对路径: sha256}。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts:
            continue
        out[str(p.relative_to(root)).replace("\\", "/")] = sha256(p)
    return out


def compare(src: Path, dst: Path) -> tuple[list[str], list[str], list[str]]:
    """返回 (仅源有, 仅目标有, 内容不同)。"""
    a, b = file_map(src), file_map(dst)
    only_src = sorted(set(a) - set(b))
    only_dst = sorted(set(b) - set(a))
    differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    return only_src, only_dst, differ


def sync_one(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE)


def main() -> int:
    ap = argparse.ArgumentParser(description="同步插件到 ComfyUI custom_nodes")
    ap.add_argument("--comfyui", default=str(REPO_ROOT / ".comfyui"),
                    help="ComfyUI 根目录（含 custom_nodes/）")
    ap.add_argument("--check", action="store_true", help="只比对，不写入")
    args = ap.parse_args()

    comfyui = Path(args.comfyui).resolve()
    if not comfyui.is_dir():
        print(f"[!] 找不到 ComfyUI 目录：{comfyui}", file=sys.stderr)
        print("    用 --comfyui <路径> 指定，或先跑 scripts/setup_comfyui.py", file=sys.stderr)
        return 2

    custom_nodes = comfyui / "custom_nodes"
    if not custom_nodes.is_dir():
        print(f"[!] {comfyui} 下没有 custom_nodes/", file=sys.stderr)
        return 2

    print(f"仓库根    : {REPO_ROOT}")
    print(f"custom_nodes: {custom_nodes}")
    print(f"模式      : {'只比对 (--check)' if args.check else '同步'}\n")

    failed = False
    for src_name, dst_name in PACKAGES:
        src = REPO_ROOT / src_name
        dst = custom_nodes / dst_name
        if not src.is_dir():
            print(f"[!] 源目录不存在：{src}", file=sys.stderr)
            failed = True
            continue

        only_src, only_dst, differ = compare(src, dst)

        if args.check:
            status = "一致" if not (only_src or only_dst or differ) else "不一致"
            print(f"[{status}] {src_name} -> {dst_name}")
            if only_src:
                print(f"    仅源有 {len(only_src)}：{only_src[:6]}{' ...' if len(only_src) > 6 else ''}")
            if only_dst:
                print(f"    仅目标有 {len(only_dst)}：{only_dst[:6]}{' ...' if len(only_dst) > 6 else ''}")
            if differ:
                print(f"    内容不同 {len(differ)}：{differ[:6]}{' ...' if len(differ) > 6 else ''}")
            if only_src or only_dst or differ:
                failed = True
            continue

        sync_one(src, dst)
        only_src, only_dst, differ = compare(src, dst)
        ok = not (only_src or only_dst or differ)
        print(f"[{'OK' if ok else '失败'}] {src_name} -> {dst_name}")
        if not ok:
            failed = True
            if only_src:
                print(f"    同步后仍仅源有：{only_src[:6]}")
            if differ:
                print(f"    同步后仍不一致：{differ[:6]}")

    print()
    if failed:
        print("[!] 同步校验未通过。运行态与开发态不一致会导致「修好了但没生效」。", file=sys.stderr)
        return 1
    print("同步完成，且逐文件哈希一致。" if not args.check else "全部一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
