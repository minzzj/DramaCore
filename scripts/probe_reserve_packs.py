"""隔离探测：`保留备用的节点` 里的候选包能否**干净导入**。

## 为什么先探再装

任务书 §7 要求：「先比较维护状态、许可证、依赖和当前 ComfyUI/Python 版本，
选择最少且稳定的组合」。而且装坏了会让 ComfyUI **起不来** —— 那是用户最在意的事。

所以先在这里**不安装**、只在隔离进程里导入，确认：

1. 模块能 import；
2. `NODE_CLASS_MAPPINGS` 能拿到；
3. `INPUT_TYPES()` 能调用（ComfyUI 查询节点时会调；它若抛错，节点会显示异常）。

只有三项都过，才谈得上安装。

`__init__.py` 里会 `import server`（ComfyUI 的模块）。判分环境没有，
用一个空壳顶上 —— 包内本来就是 `try/except`，等价于"没有 server"。
"""

from __future__ import annotations

import importlib
import sys
import types
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESERVE = REPO / "保留备用的节点"

CANDIDATES = [
    # 任务书 §7「开源组件与适配策略」点名的各类候选，本地都有。
    # 类别名沿用任务书原文的分法，便于对照。
    ("提示词/剧本", "ComfyUI-Storyboard-LLM"),
    ("提示词/剧本", "ComfyUI-DeepSeek-API-connector"),
    ("素材/资产", "ComfyUI-DNNodes"),
    ("导演/工作流", "ComfyUI_MiniMaxH3_Director"),
    ("导演/工作流", "ComfyUI_MiniMaxH3_Smart_Director"),
    ("导演/工作流", "ComfyUI-MiniMaxH3-ChainDirector"),
    ("导演/工作流", "H3-Chinese-Director"),
    ("审核/通知", "ComfyUI-SystemOne"),
    ("审核/通知", "ComfyUI-JobNotify"),
    ("审核/通知", "ComfyUI-OpenClaw"),
    ("外部 API/云端", "comfyui-api-pack"),
    ("外部 API/云端", "ComfyUI-CometAPI"),
    ("显存/画质优化", "fxai-toolkit"),
    ("显存/画质优化", "ComfyUI-FlashVSR_Ultra_Fast"),
    ("显存/画质优化", "ComfyUI-HR-Endless-Sampler"),
    ("显存/画质优化", "ComfyUI_pxtool"),
]

#: 这些包已经在 ComfyUI 里跑着，探测路径要指向 `.comfyui/custom_nodes`
INSTALLED_DIR = REPO / ".comfyui" / "custom_nodes"


#: ComfyUI **自己**的模块。裸进程里没有它们，但包里 import 这些是正常的 ——
#: 不能因此判"导入失败"（那是假阴性：这些包正在 ComfyUI 里跑着）。
COMFY_INTERNAL = {
    "folder_paths", "comfy", "comfy_extras", "nodes", "server", "execution",
    "model_management", "latent_preview", "comfy_api",
}


def classify_import_error(exc: BaseException) -> tuple[str, bool]:
    """返回 `(结论, 是否假阴性)`。

    * 缺 ComfyUI 内部模块 -> 假阴性（需要运行时才能判定）；
    * 其余 -> 真失败（语法错、缺第三方依赖等）。
    """
    if isinstance(exc, ModuleNotFoundError):
        missing = getattr(exc, "name", "") or ""
        if missing.split(".")[0] in COMFY_INTERNAL:
            return f"需 ComfyUI 运行时（裸进程测不了；缺 {missing}）", True
        return f"缺第三方依赖：{missing}", False
    if isinstance(exc, ImportError):
        msg = str(exc)
        if any(m in msg for m in COMFY_INTERNAL):
            return f"需 ComfyUI 运行时（裸进程测不了；{msg[:60]}）", True
        return f"导入错误：{msg[:80]}", False
    if isinstance(exc, SyntaxError):
        return f"语法错误：{exc}", False
    return f"{type(exc).__name__}: {exc}", False


def probe(name: str) -> dict:
    """导入一个包并检查它暴露的节点。返回探测结果。"""
    out: dict = {"name": name, "imported": False, "nodes": [], "errors": []}
    try:
        mod = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        verdict, false_negative = classify_import_error(exc)
        out["errors"].append(verdict)
        out["false_negative"] = false_negative
        out["traceback"] = traceback.format_exc()
        return out
    out["imported"] = True
    ncm = getattr(mod, "NODE_CLASS_MAPPINGS", {}) or {}
    ndm = getattr(mod, "NODE_DISPLAY_NAME_MAPPINGS", {}) or {}
    out["web"] = getattr(mod, "WEB_DIRECTORY", None)
    for key, cls in ncm.items():
        info = {"key": key, "display": ndm.get(key, getattr(cls, "__name__", "?"))}
        try:
            spec = cls.INPUT_TYPES()
            info["required"] = list((spec.get("required") or {}))
            info["optional"] = list((spec.get("optional") or {}))
            info["return_types"] = list(getattr(cls, "RETURN_TYPES", ()) or ())
            info["return_names"] = list(getattr(cls, "RETURN_NAMES", ()) or ())
            info["function"] = getattr(cls, "FUNCTION", None)
            info["category"] = getattr(cls, "CATEGORY", None)
        except Exception as exc:  # noqa: BLE001
            info["input_types_error"] = f"{type(exc).__name__}: {exc}"
            out["errors"].append(f"{key}.INPUT_TYPES: {info['input_types_error']}")
        out["nodes"].append(info)
    return out


def live_node_count(pack: str, port: int = 8188) -> dict | None:
    """查**运行中的** ComfyUI 里这个包注册了多少节点（最权威的判据）。

    做法：拉 `/object_info`，把每个节点类型反查它属于哪个自定义节点包
    （ComfyUI 的 `object_info` 里带 `python_module` 字段）。
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/object_info", timeout=90) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001
        return None

    hit: list[str] = []
    for node_type, spec in data.items():
        mod = str((spec or {}).get("python_module") or "")
        if pack.replace("-", "_") in mod or pack in mod:
            hit.append(node_type)
    return {"count": len(hit), "sample": sorted(hit)[:8]}


def main() -> int:
    print("=" * 74)
    print("候选巨人清单探测：能否干净导入、节点接口是什么（**不安装任何东西**）")
    print("=" * 74)
    print("对照任务书 §7「开源组件与适配策略」点名的候选方向。")
    print("探测路径：已装的看 .comfyui/custom_nodes，未装的看 保留备用的节点。")

    if not RESERVE.is_dir():
        print(f"[!] 找不到 {RESERVE}")
        return 2
    for p in (RESERVE, INSTALLED_DIR):
        if p.is_dir():
            sys.path.insert(0, str(p))
    sys.modules.setdefault("server", types.ModuleType("server"))

    summary: list[tuple[str, str, str, int]] = []
    for category, name in CANDIDATES:
        where = None
        if (INSTALLED_DIR / name).is_dir():
            where = "已装"
        elif (RESERVE / name).is_dir():
            where = "备用"
        if where is None:
            print(f"\n--- [{category}] {name}  ❌ 本地不存在")
            summary.append((category, name, "不存在", 0))
            continue

        print(f"\n--- [{category}] {name}  ({where})")
        if where == "已装":
            # 已装的包**直接查运行中的 ComfyUI** —— 那才是最权威的答案，
            # 比裸进程探测准得多（裸进程缺 ComfyUI 内部模块，会假阴性）。
            live = live_node_count(name)
            if live is not None:
                print(f"    ✅ 运行中 ComfyUI 已注册它的节点：{live['count']} 个")
                if live["sample"]:
                    print(f"       例：{'、'.join(live['sample'])}")
                summary.append((category, name, "已装/运行中", live["count"]))
                continue
            print("    (查不到运行中的 ComfyUI，退回裸进程探测)")

        r = probe(name)
        if not r["imported"]:
            verdict = r["errors"][0]
            fn = r.get("false_negative", False)
            mark = "⚠️ 待定" if fn else "❌"
            print(f"    {mark} {verdict}")
            summary.append((category, name,
                            "需运行时判定" if fn else "不可用", 0))
            continue
        print(f"    ✅ 导入成功；节点 {len(r['nodes'])} 个")
        n_ok = 0
        for n in r["nodes"]:
            if "input_types_error" in n:
                print(f"       {n['key']}: [INPUT_TYPES 抛错] "
                      f"{n['input_types_error'][:70]}")
                continue
            n_ok += 1
        show = r["nodes"][:6]
        for n in show:
            if "input_types_error" in n:
                continue
            rn = n["return_names"] or n["return_types"]
            print(f"       {n['key']} -> {'、'.join(str(x) for x in rn)}")
        if len(r["nodes"]) > len(show):
            print(f"       … 另有 {len(r['nodes']) - len(show)} 个节点")
        summary.append((category, name, f"{where}/可用", len(r["nodes"])))

    print("\n" + "=" * 74)
    print("汇总：本地可用的巨人（按任务书 §7 分类）")
    print("=" * 74)
    cur = None
    for category, name, state, count in summary:
        if category != cur:
            print(f"\n  【{category}】")
            cur = category
        print(f"    {name:<38} {state:<10} 节点 {count}")

    usable = sum(1 for *_x, state, _c in
                 [(c, n, s, k) for c, n, s, k in summary] if "可用" in state)
    print(f"\n共 {len(summary)} 个候选，其中可用 {usable} 个。")
    print("说明：**只探测导入与接口，没有安装任何东西**；")
    print("      要真正使用还需安装 + 配置（部分需要 API 密钥）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
