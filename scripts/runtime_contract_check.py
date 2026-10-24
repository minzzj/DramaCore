"""在**真实 ComfyUI 运行时内**执行融合体检的运行时契约比对。

## 为什么必须在这里跑

`director.external_groups` 内部会 `import comfy`，
而 `comfy` 只在 ComfyUI 的运行时（custom_nodes 加载之后）才可导入。
所以"真导入 `pack_*` 比对字段"这一步**只能在 ComfyUI 进程里做** ——
这也是本机此前唯一没能完成的验证。

本脚本要用 **ComfyUI 的 venv**、**在 `.comfyui` 目录下**运行。

用法：
    cd .comfyui
    python ..\scripts\runtime_contract_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    here = Path.cwd()
    if not (here / "custom_nodes").is_dir():
        print(f"[!] 请在 ComfyUI 根目录下运行（当前 {here}）")
        return 2

    print("=" * 68)
    print("融合运行时契约比对（在 ComfyUI 运行时内）")
    print("=" * 68)

    # 1) 让插件可导入（custom_nodes 已在 sys.path 上，但脚本入口不同）
    sys.path.insert(0, str(here))
    sys.path.insert(0, str(here / "custom_nodes"))
    sys.path.insert(0, str(here / "custom_nodes" / "bettercomfyui_comfy"))

    # 2) 先把导演台包按 ComfyUI 的方式加载（这样 director.* 才可用）
    pkg = "ComfyUI_MiniMaxH3_Director"
    try:
        import importlib

        importlib.import_module(pkg)
        print(f"[OK] 已加载导演台包：{pkg}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 加载 {pkg} 失败：{type(exc).__name__}: {exc}")
        print("    后续比对会如实报「未验证」，不会假装通过。")

    # 3) 跑体检节点的自检
    try:
        from bettercomfyui_comfy.fusion_nodes import BetterComfyUIFusionDoctor

        text = BetterComfyUIFusionDoctor._contract_selfcheck()
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"[!] 无法执行自检：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1

    print()
    print(text)
    print()
    print("=" * 68)

    passed = "全部通过" in text
    unverified = "[未验证]" in text
    if passed:
        print("结论：运行时契约比对通过。")
        return 0
    if unverified:
        print("结论：**未验证**（不是通过，也不是失败）。")
        return 3
    print("结论：**存在不一致**，请核对上面的差异。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
