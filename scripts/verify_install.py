"""成品安装验证：确认插件真的装进 ComfyUI 且可用。

## 为什么需要这个脚本

"装好了"不能靠嘴说。用户打开 ComfyUI 看不到节点、找不到工作流，
那就是没装好。本脚本逐项核实：

1. **插件目录**已复制到 `.comfyui/custom_nodes/bettercomfyui_comfy`
2. **逐文件哈希一致**（源码 vs 已安装），排除"装了一半/装的是旧版"
3. **运行中的 ComfyUI 里节点全部注册**（`/object_info` 能查到 17 个）
4. **工作流已放进界面目录**（用户能在界面里直接打开）
5. **两个巨人包**（DeepSeek 剧本 + 提示词连接器）也在

任一项失败都以非零退出码结束，并打印具体原因 —— 不静默。

## 用法

    python scripts/verify_install.py
    python scripts/verify_install.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMFYUI = REPO_ROOT / ".comfyui"
INSTALLED = COMFYUI / "custom_nodes" / "bettercomfyui_comfy"
PLUGIN_SRC = REPO_ROOT / "bettercomfyui_comfy"
UI_WORKFLOWS = COMFYUI / "user" / "default" / "workflows"
BASE = "http://127.0.0.1:8188"

#: 我期望在界面里能看到的节点（24 个）。
EXPECTED_NODES = (
    "BetterComfyUIOneDirector",
    "BetterComfyUIEverything",
    "BetterComfyUIPromptBridge",
    "BetterComfyUISettings",
    "BetterComfyUIStoryboard",
    "BetterComfyUIAssetGen",
    "BetterComfyUIAssetIngest",
    "BetterComfyUIAssetReview",
    "BetterComfyUIVideoReview",
    "BetterComfyUIFinalize",
    "BetterComfyUIRun",
    "BetterComfyUIProject",
    "BetterComfyUIResult",
    "BetterComfyUIAssetMedia",
    "BetterComfyUIAssetSlots",
    "BetterComfyUIAssetImage",
    "BetterComfyUIFusionIngest",
    "BetterComfyUIFusionGroups",
    "BetterComfyUIFusionCombine",
    "BetterComfyUIFusionPackGroup",
    "BetterComfyUIFusionDoctor",
    "BetterComfyUIDoctor",
    "BetterComfyUIManualAsset",
    "BetterComfyUITimelineText",
)

#: 期望随包安装的第三方巨人（剧本 + 提示词）。
EXPECTED_GIANTS = (
    "ComfyUI-Storyboard-LLM",
    "ComfyUI-DeepSeek-API-connector",
)

#: 期望能在界面里直接打开的工作流。
EXPECTED_WORKFLOWS = (
    "00_【总流程】剧本→全部内容（本机可跑）.json",
    "00_【总流程】剧本→成片（GPU）.json",
    "00_【总流程】段视频→剪好成片（本机可跑）.json",
    "P1_剧本→全量提示词（本机可跑）.json",
    "S3_素材生成入库审核（GPU）.json",
    "S4_视频审核（本机可跑）.json",
    "S1_单节点体验（本机可跑）.json",
    "S2_单节点出片（GPU）.json",
    "W0_完整逻辑图全流程.json",
    "W0L_本机可跑通体验版.json",
    "W1_最简单入门.json",
    "W2_DeepSeek全自动.json",
    "W3_全流程出片.json",
    "W4_素材参考图注入.json",
    "one_director_demo.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compare_trees(src: Path, dst: Path) -> list[str]:
    """逐文件比较源码与已安装副本，返回不一致清单。"""
    problems: list[str] = []
    if not src.is_dir():
        return [f"源码目录不存在：{src}"]
    if not dst.is_dir():
        return [f"安装目录不存在：{dst}（插件未安装）"]
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        target = dst / rel
        if not target.is_file():
            problems.append(f"安装目录缺少文件：{rel}")
            continue
        if _sha256(path) != _sha256(target):
            problems.append(f"文件内容不一致（装的是旧版）：{rel}")
    return problems


def _object_info() -> dict | None:
    try:
        return json.load(urllib.request.urlopen(f"{BASE}/object_info", timeout=40))
    except Exception:  # noqa: BLE001
        return None


def verify(need_running: bool = True) -> dict:
    report: dict = {"ok": True, "checks": [], "problems": []}

    def check(name: str, problems: list[str], detail: str = "") -> None:
        passed = not problems
        report["checks"].append(
            {"name": name, "passed": passed, "detail": detail}
        )
        if not passed:
            report["ok"] = False
            report["problems"].extend(f"{name}: {p}" for p in problems)

    # 1) 插件目录 + 逐文件哈希
    check("插件已安装且逐文件哈希一致", _compare_trees(PLUGIN_SRC, INSTALLED),
          str(INSTALLED.relative_to(REPO_ROOT)))

    # 2) 巨人包
    giants = []
    for name in EXPECTED_GIANTS:
        path = COMFYUI / "custom_nodes" / name
        if not path.is_dir():
            giants.append(f"缺少巨人包：{name}")
    check("两个巨人包已安装", giants,
          ", ".join(EXPECTED_GIANTS))

    # 3) 工作流
    wf_problems = []
    for name in EXPECTED_WORKFLOWS:
        if not (UI_WORKFLOWS / name).is_file():
            wf_problems.append(f"界面目录缺少工作流：{name}")
    check("新工作流已放进界面目录", wf_problems, str(UI_WORKFLOWS.relative_to(REPO_ROOT)))

    # 4) 运行中的 ComfyUI 里节点注册
    info = _object_info()
    if info is None:
        if need_running:
            check("ComfyUI 可访问", [f"连不上 {BASE}（服务未启动？）"])
        else:
            check("ComfyUI 可访问", [], "跳过（--no-running）")
    else:
        missing = [n for n in EXPECTED_NODES if n not in info]
        check(f"{len(EXPECTED_NODES)} 个节点在界面里可见", missing,
              f"总节点 {len(info)}")
        missing_giants = [g for g in ("StoryboardNode", "DeepSeekPromptConnector")
                          if g not in info]
        check("巨人节点在界面里可见", missing_giants)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证成品安装")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-running", action="store_true",
                        help="不检查运行中的 ComfyUI（只查文件）")
    args = parser.parse_args(argv)

    report = verify(need_running=not args.no_running)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print("=== 成品安装验证 ===")
    for item in report["checks"]:
        tag = "OK  " if item["passed"] else "FAIL"
        detail = f"（{item['detail']}）" if item["detail"] else ""
        print(f"  [{tag}] {item['name']}{detail}")
    if report["ok"]:
        print("\n结论：安装完整且可用。")
        return 0
    print("\n结论：安装有问题：")
    for problem in report["problems"]:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
