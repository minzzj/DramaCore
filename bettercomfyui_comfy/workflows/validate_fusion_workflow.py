"""校验**融合工作流**：节点类型、控件槽位数、连线完整性。

与 `validate_workflow.py`（校验最简工作流）分开，因为融合工作流的节点来自
**两个不同的包**（我的 + 导演台），需要对照**运行中的 ComfyUI** 拿全部 schema，
而不是只对照本包的 `NODE_CLASS_MAPPINGS`。

三重检查：

1. **节点类型存在**：每个 `type` 都能在本机 `/object_info` 里查到；
2. **控件槽位数匹配**：`widgets_values` 的长度必须等于该节点的控件槽位数
   —— 这条最重要：长度不匹配会让参数**静默错位**（cfg 填进 seed 这种），
   载入后不报错但结果全错；
3. **连线自洽**：每条 link 的源/目标节点存在，且源节点确实有那个输出槽。

用法：
    python bettercomfyui_comfy/workflows/validate_fusion_workflow.py
    python bettercomfyui_comfy/workflows/validate_fusion_workflow.py --port 8188
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 与本包 build_fusion_workflow 保持一致的「连线类型」集合
LINK_TYPES = {
    "IMAGE", "AUDIO", "VIDEO", "MODEL", "VAE", "CLIP", "LATENT", "CONDITIONING",
    "MASK", "CONTROL_NET", "SAMPLER", "SIGMAS", "GUIDER", "NOISE", "SAMPLER_SELECT",
    "MMX_DIR_GROUP", "MMX_DIR_REFINE", "MMX_DIR_SELFLIFT", "MMX_DIR_SEMANTIC_BRIDGE",
    "MMX_DIR_FACE_REFINE", "MINIMAX_H3_SMART_PLAN", "BDGROUP",
}


def widget_count(schema: dict) -> int:
    """该节点在 UI 上有几个控件槽位。

    **必须与 build_fusion_workflow.widget_slots 完全同规则** ——
    否则生成与校验各算一套，校验只会给出假安心。

    规则：类型是取值列表 -> COMBO 控件；类型是 INT/FLOAT/STRING/BOOLEAN -> 控件；
    裸类型名且**没有任何 UI 专属选项键**（default/min/max/step/multiline）-> 连线口。

    （不用长度/命名约定判断 —— "VAE" 只有 3 个字符，
     而早期的 len(t) > 3 判断正是把它误判成控件的元凶。）
    """
    scalar_widgets = {"INT", "FLOAT", "STRING", "BOOLEAN"}
    ui_only_keys = {"default", "min", "max", "step", "multiline", "multiline_text"}
    n = 0
    spec = schema.get("input", {})
    for section in ("required", "optional"):
        for _name, decl in (spec.get(section) or {}).items():
            if not (isinstance(decl, list) and decl):
                continue
            t = decl[0]
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
            if isinstance(t, list):
                n += 1
            elif isinstance(t, str) and t in scalar_widgets:
                n += 1
            elif isinstance(t, str) and not (ui_only_keys & set(opts)):
                continue
            else:
                n += 1
    return n


def fetch(port: int, node_type: str) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/object_info/{node_type}", timeout=30
        ) as resp:
            data = json.load(resp)
        return data.get(node_type)
    except Exception:  # noqa: BLE001
        return None


def validate(path: Path, *, port: int) -> tuple[bool, list[str], list[str]]:
    ok: list[str] = []
    err: list[str] = []
    wf = json.loads(path.read_text(encoding="utf-8"))
    nodes = wf.get("nodes") or []
    if not nodes:
        err.append("没有 nodes")
        return False, ok, err

    by_id = {n["id"]: n for n in nodes}

    for n in nodes:
        t = n.get("type")
        schema = fetch(port, t)
        if schema is None:
            err.append(f"节点 {n['id']} 类型 {t!r} 在本机 /object_info 里不存在（会显示为缺失节点）")
            continue
        expect = widget_count(schema)
        got = len(n.get("widgets_values") or [])
        if got != expect:
            err.append(
                f"节点 {n['id']} ({t}) 控件数不匹配：widgets_values={got}，schema={expect}"
                " —— 参数会静默错位"
            )
        else:
            ok.append(f"节点 {n['id']} ({t}) 控件数 {got} 匹配")

        # 输出槽声明是否与 schema 一致
        declared = schema.get("output_name") or schema.get("output") or []
        outs = n.get("outputs") or []
        if declared and len(outs) != len(declared):
            err.append(f"节点 {n['id']} ({t}) 输出槽数 {len(outs)} != schema {len(declared)}")

    for link in wf.get("links") or []:
        # ComfyUI 的 link 形式：[id, src_node, src_slot, dst_node, dst_slot, type]
        if not isinstance(link, list) or len(link) < 5:
            err.append(f"link 结构异常（期望至少 5 项）：{link!r}")
            continue
        lid, src, sslot, dst, dslot = link[0], link[1], link[2], link[3], link[4]
        if src not in by_id:
            err.append(f"link {lid} 的源节点 {src} 不存在")
            continue
        if dst not in by_id:
            err.append(f"link {lid} 的目标节点 {dst} 不存在")
            continue
        src_outs = by_id[src].get("outputs") or []
        if sslot >= len(src_outs):
            err.append(f"link {lid} 源槽 {sslot} 越界（节点 {src} 只有 {len(src_outs)} 个输出）")
            continue

        # ⚠️ 只查槽位越界是不够的：**类型必须对得上**。
        # 把 IMAGE 接到 MODEL 口上、或把 MMX_DIR_GROUP 接到 IMAGE 口上，
        # 槽位序号可能都在范围内，但 ComfyUI 会拒绝执行（或行为异常）。
        # 融合工作流的核心就是 `MMX_DIR_GROUP` 这条接缝，所以这里必须查。
        src_type = src_outs[sslot].get("type")
        dst_inputs = by_id[dst].get("inputs") or []
        if dslot >= len(dst_inputs):
            err.append(
                f"link {lid} 目标槽 {dslot} 越界（节点 {dst} 只声明了 {len(dst_inputs)} 个输入）"
            )
            continue
        dst_name = dst_inputs[dslot].get("name")
        dst_type = dst_inputs[dslot].get("type")

        # 目标输入名必须是 schema 里真实存在的（控件口用名字连时尤其重要）
        dst_schema = fetch(port, by_id[dst].get("type"))
        if dst_schema:
            spec = dst_schema.get("input", {})
            known = set((spec.get("required") or {}).keys()) | \
                    set((spec.get("optional") or {}).keys())
            if dst_name not in known:
                err.append(
                    f"link {lid} 目标节点 {dst}({by_id[dst].get('type')}) 没有名为 "
                    f"{dst_name!r} 的输入（真实输入有：{sorted(known)[:10]}）"
                )
                continue

        if src_type and dst_type and src_type != dst_type:
            # 允许兼容转换的常见组合由 ComfyUI 自行处理；
            # 但这两类不兼容必须拦住：把组接到张量口、或把张量接到组口。
            if {src_type, dst_type} & {"MMX_DIR_GROUP", "IMAGE", "MODEL", "VAE", "CLIP",
                                       "AUDIO", "VIDEO"}:
                err.append(
                    f"link {lid} 类型不匹配：{src}[{sslot}]={src_type} -> "
                    f"{dst}.{dst_name}={dst_type}"
                )
                continue

        ok.append(
            f"link {lid}: {src}[{sslot}]({src_type}) -> {dst}.{dst_name}({dst_type})"
        )

    return (not err), ok, err


def main() -> int:
    ap = argparse.ArgumentParser(description="校验融合工作流")
    ap.add_argument("--port", type=int, default=8188)
    args = ap.parse_args()

    # ⚠️ 只 glob `fusion_*.json` 会留下盲区：新加的工作流（如 one_director_demo.json）
    # **根本没被校验**，而报告照样显示"全部 PASS" —— 是假安心。
    # 两类都收进来；新增工作流只要命名成 `*_demo.json` 就会自动进入校验。
    files = sorted(set(HERE.glob("fusion_*.json")) | set(HERE.glob("*_demo.json")))
    if not files:
        print("没有找到工作流 JSON（fusion_*.json / *_demo.json）", file=sys.stderr)
        return 2

    # 先确认 ComfyUI 是否在跑
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/system_stats", timeout=10):
            pass
    except Exception as exc:  # noqa: BLE001
        print(
            f"[!] 连不上 ComfyUI (127.0.0.1:{args.port})：{type(exc).__name__}: {exc}\n"
            "    本校验需要运行中的 ComfyUI 才能读取真实 schema。\n"
            "    先启动：.comfyenv\\Scripts\\python.exe main.py --cpu --listen 127.0.0.1 --port 8188",
            file=sys.stderr,
        )
        return 2

    all_ok = True
    for f in files:
        passed, oks, errs = validate(f, port=args.port)
        print(f"\n=== {f.name} ===")
        for line in oks:
            print(f"  [OK]  {line}")
        for line in errs:
            print(f"  [FAIL] {line}")
        print(f"  结论：{'PASS' if passed else 'FAIL'}（{len(oks)} OK / {len(errs)} FAIL）")
        all_ok = all_ok and passed

    print()
    print("总结论：" + ("全部 PASS" if all_ok else "存在 FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
