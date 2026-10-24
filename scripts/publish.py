#!/usr/bin/env python
"""把暂存区的文件发布到工作区，**并立即验证**。

## 为什么需要这个脚本

本项目反复出现同一类事故（一晚上三次）：

1. 我用编辑器修好了工作区里的文件（例如给 `make_r2v_group` 补上
   `first_frame`/`last_frame`）；
2. 之后为了发布另一个改动，从暂存目录整份 `Copy-Item` 覆盖，
   **把之前的修复覆盖掉了**；
3. 直到测试变红才发现 —— 如果那条测试不存在，就会带着旧 bug 交付。

根因是「发布」和「验证」是两步手动操作，中间没有任何强制检查。

本脚本把两者合成一步：**复制 → 语法检查 → 契约测试 → 明确报告**。
任何一步失败就非零退出，不会带着坏文件继续。

用法：
    python scripts/publish.py                    # 发布全部已跟踪文件
    python scripts/publish.py --files a.py b.py  # 只发布指定文件（相对仓库根）
    python scripts/publish.py --no-verify        # 只复制（不推荐）
"""

from __future__ import annotations

import argparse
import ast
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE = Path(r"C:\Users\Administrator\AppData\Local\Temp\bcui_stage")

#: 需要从暂存区发布的路径（相对仓库根）。加新文件时补到这里，避免漏发。
TRACKED = (
    "bettercomfyui/gap_analysis.py",
    "bettercomfyui/seed.py",
    "bettercomfyui/pipeline/core.py",
    "bettercomfyui/cli.py",
    "bettercomfyui/models/entities.py",
    "bettercomfyui_comfy/__init__.py",
    "bettercomfyui_comfy/nodes.py",
    "bettercomfyui_comfy/routes.py",
    "bettercomfyui_comfy/_bridge.py",
    "bettercomfyui_comfy/fusion.py",
    "bettercomfyui_comfy/fusion_nodes.py",
    "bettercomfyui_comfy/asset_nodes.py",
    "bettercomfyui_comfy/phase2.py",
    "bettercomfyui_comfy/web/js/bettercomfyui_panel.js",
    "bettercomfyui_comfy/workflows/build_fusion_workflow.py",
    "bettercomfyui_comfy/workflows/validate_fusion_workflow.py",
    "bettercomfyui_comfy/workflows/minimal_workflow.json",
)

#: 发布后必须通过的测试（快速、能抓到"覆盖掉修复"这类事故）
VERIFY_TESTS = (
    "tests/test_fusion_contract.py",
    "tests/test_comfy_plugin.py",
    "tests/test_phase1_gaps.py",
)


def publish_one(rel: str, *, dry: bool, overwrite: bool) -> str:
    """发布单个文件。

    ⚠️ 安全默认：**工作区已有的文件不会被覆盖**。
    这是为了防止「用陈旧暂存副本抹掉工作区里的修复」这类事故
    （本项目一晚上发生过三次，其中最严重的一次让所有面板路由 404）。
    """
    src = STAGE / rel
    dst = REPO_ROOT / rel
    if not src.is_file():
        return f"  [跳过] 暂存区没有：{rel}"
    if dst.is_file():
        if filecmp.cmp(src, dst, shallow=False):
            return f"  [相同] {rel}"
        if not overwrite:
            return (
                f"  [保留工作区] {rel} —— 工作区版本与暂存区不同，"
                "按安全规则**不覆盖**。若确需用暂存版替换，请加 --overwrite"
            )
    if dry:
        return f"  [待发布] {rel}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return f"  [已发布] {rel}"


def syntax_check(paths: list[Path]) -> list[str]:
    problems: list[str] = []
    for p in paths:
        if p.suffix != ".py" or not p.is_file():
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{p.relative_to(REPO_ROOT)}: 语法错误 {exc}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="发布暂存文件到工作区并验证")
    ap.add_argument("--files", nargs="*", help="只发布这些（相对仓库根）")
    ap.add_argument("--dry", action="store_true", help="只显示会做什么")
    ap.add_argument("--overwrite", action="store_true",
                    help="显式允许用暂存版覆盖工作区已有文件（危险：会抹掉工作区里的修复）")
    ap.add_argument("--no-verify", action="store_true", help="跳过验证（不推荐）")
    args = ap.parse_args()

    targets = args.files or list(TRACKED)
    print(f"暂存区：{STAGE}")
    print(f"工作区：{REPO_ROOT}\n")

    for rel in targets:
        print(publish_one(rel.replace("\\", "/"), dry=args.dry, overwrite=args.overwrite))

    if args.dry:
        return 0

    touched = [REPO_ROOT / r.replace("\\", "/") for r in targets]
    problems = syntax_check(touched)
    if problems:
        print("\n[!] 语法检查失败：")
        for p in problems:
            print(f"    {p}")
        return 1
    print("\n语法检查：全部通过")

    if args.no_verify:
        print("[!] 已按 --no-verify 跳过测试验证")
        return 0

    print("\n运行验证测试（防止覆盖掉既有修复）...")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *VERIFY_TESTS, "-q", "-p", "no:cacheprovider",
         "--tb=line"],
        cwd=str(REPO_ROOT), check=False,
    )
    if proc.returncode != 0:
        print(
            "\n[!] 验证失败 —— 发布可能覆盖了先前的修复。\n"
            "    请检查上面失败的测试，修正后重新发布。"
        )
        return proc.returncode
    print("\n验证通过。发布完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
