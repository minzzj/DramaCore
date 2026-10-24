"""把 UI 格式的融合工作流转成 API 格式并**提交给 ComfyUI 校验**。

## 为什么值得做

`validate_fusion_workflow.py` 已经查了：控件数、源/目标槽范围、类型匹配、
目标输入名存在于 schema。但它终究是**我在检查我自己的理解**。

把工作流转成 API 格式（`/prompt` 实际吃的形状）提交给 ComfyUI，
则由 **ComfyUI 自己**回答：

* 图结构是否合法（有没有环路、有没有缺必需输入）；
* 输入名与类型是否被真实节点接受；
* 报错是否精确指出问题节点。

即使因为**缺模型权重**导致执行失败，只要 `/prompt` 返回的不是
`node_errors`（校验错误），就说明**图本身是好的** —— 这已经把
"输入名写错 / 类型不对 / 必需输入漏了" 这一类问题全部排除。

用法：
    python bettercomfyui_comfy/workflows/submit_check.py
    python bettercomfyui_comfy/workflows/submit_check.py --port 8188
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_fusion_workflow import fetch_object_info, widget_slots  # noqa: E402

#: 与 build/validate 保持一致的连线类型集合
LINK_TYPES = {
    "IMAGE", "AUDIO", "VIDEO", "MODEL", "VAE", "CLIP", "LATENT", "CONDITIONING",
    "MASK", "CONTROL_NET", "SAMPLER", "SIGMAS", "GUIDER", "NOISE", "SAMPLER_SELECT",
    "MMX_DIR_GROUP", "MMX_DIR_REFINE", "MMX_DIR_SELFLIFT", "MMX_DIR_SEMANTIC_BRIDGE",
    "MMX_DIR_FACE_REFINE", "MINIMAX_H3_SMART_PLAN", "BDGROUP",
}


def ui_to_api(wf: dict, schemas: dict) -> tuple[dict, list[str]]:
    """把 UI 格式转成 API 格式。返回 `(api_graph, problems)`。"""
    nodes = {n["id"]: n for n in wf.get("nodes") or []}
    # link_id -> (src_node, src_slot)
    link_src = {lk[0]: (lk[1], lk[2]) for lk in (wf.get("links") or [])}
    problems: list[str] = []
    api: dict[str, dict] = {}

    for nid, n in nodes.items():
        ntype = n["type"]
        schema = schemas.get(ntype)
        if schema is None:
            problems.append(f"节点 {nid} 类型 {ntype} 无 schema（无法转换）")
            continue

        inputs: dict = {}
        # 1) 连线输入：按节点声明的 inputs 数组顺序，与 schema 里的连线口一一对应
        #    这里直接信任节点自己声明的 name（validate 已核对过 name 真实性）
        for slot in n.get("inputs") or []:
            lid = slot.get("link")
            if lid is None:
                continue
            src = link_src.get(lid)
            if src is None:
                problems.append(f"节点 {nid} 的输入 {slot.get('name')} 指向不存在的 link {lid}")
                continue
            inputs[slot["name"]] = [str(src[0]), src[1]]

        # 2) 控件值：按 schema 的控件顺序与 widgets_values 位置对应
        slots = widget_slots(schema)
        values = n.get("widgets_values") or []
        for i, (name, _meta) in enumerate(slots):
            if i < len(values):
                inputs[name] = values[i]

        api[str(nid)] = {"class_type": ntype, "inputs": inputs, "_meta": {"title": ntype}}

    return api, problems


def classify(node_errors: dict, api: dict) -> tuple[list[str], list[str]]:
    """把 ComfyUI 的 node_errors 分成两类。

    **为什么要分**（一个真实的误判风险）：
    「模型文件不在列表里」这类错误是**环境问题**（权重还没下载完），
    不是工作流的缺陷。如果把它和真正的图结构错误混在一起报，
    就会得出"工作流有问题"的错误结论 —— 而实际上图是对的。

    返回 `(真问题, 环境问题)`。
    """
    real: list[str] = []
    env: list[str] = []
    for nid, detail in (node_errors or {}).items():
        ctype = (api.get(nid) or {}).get("class_type", "?")
        for e in (detail or {}).get("errors") or []:
            etype = e.get("type")
            name = (e.get("extra_info") or {}).get("input_name")
            received = (e.get("extra_info") or {}).get("received_value")
            msg = (f"节点 {nid} ({ctype}) {name}: {e.get('message')} "
                   f"— {e.get('details')}")
            # value_not_in_list 且收到的是一个**文件名**（含扩展名）-> 权重未就位
            if etype == "value_not_in_list" and isinstance(received, str) \
                    and "." in received:
                env.append(msg + "（权重文件尚未出现在 ComfyUI 的清单里）")
            else:
                real.append(msg)
    return real, env


def kill_queued(base: str) -> str:
    """**提交后立刻取消执行**，只留校验结果。

    ## 为什么必须这么做（血的教训）

    `/prompt` 的行为是「**校验通过就入队执行**」。校验答案在响应里就拿到了，
    但任务已经进了队列，会**真的开始加载模型**。

    这台机器只有 15.75 GB 物理内存，而 MiniMax H3 最小也让
    ref2va（19.53 GB）+ 文本编码器（14.61 GB）= **34.14 GB** 同时驻留。
    队列一跑就是极端内存压力。

    实测后果：2026-10-17 三次蓝屏，
    `BugCheckCode = 0x0000001A MEMORY_MANAGEMENT`（参数 0x42001，三次完全一致），
    时间 4:02 / 8:24 / 8:31 —— 与队列执行时刻吻合。
    而 10 月**没有任何 WHEA 硬件错误**，所以不是硬件/过热问题。

    结论：这个脚本**必须**在拿到校验结果后立即清空队列，
    哪怕因此偶尔误杀一次真实执行 —— 真实执行本来就不该在这台机器上跑。
    """
    notes: list[str] = []
    # 先中断正在跑的（如果已经起来了）
    for url, payload in ((f"{base}/interrupt", b"{}"),
                         (f"{base}/queue", json.dumps({"clear": True}).encode())):
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                notes.append(f"{url.rsplit('/', 1)[-1]}={resp.status}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{url.rsplit('/', 1)[-1]}失败({type(exc).__name__})")
    # 再确认一次队列真的空了
    try:
        with urllib.request.urlopen(f"{base}/queue", timeout=30) as resp:
            q = json.load(resp)
        left = len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])
        notes.append(f"剩余={left}")
    except Exception:  # noqa: BLE001
        notes.append("队列状态未知")
    return "，".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser(description="提交融合工作流给 ComfyUI 做图结构校验")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--strict-env", action="store_true",
                    help="把环境问题（权重未就位）也算作失败")
    ap.add_argument("--allow-execute", action="store_true",
                    help=("**危险**：不要清空队列，允许提交的图真的执行。\n"
                          "在内存不足 64GB 的机器上会触发 0x1A MEMORY_MANAGEMENT 蓝屏，"
                          "除非你明确知道自己在做什么，否则不要加这个参数。"))
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    if not args.allow_execute:
        print("[安全模式] 提交后会自动清空队列 —— 只取校验结果，不执行。")
        print("           本机物理内存 15.75 GB，H3 需 34.14 GB 同时驻留；")
        print("           让队列真的跑会触发 0x1A MEMORY_MANAGEMENT 蓝屏（已发生 3 次）。")
        print()

    # 与 validate_fusion_workflow 同一口径：fusion_* 与 *_demo 都要提交校验，
    # 否则新工作流会成为校验盲区（报告显示 PASS 却没检查过它）。
    files = sorted(set(HERE.glob("fusion_*.json")) | set(HERE.glob("*_demo.json")))
    if not files:
        print("没有找到工作流 JSON（fusion_*.json / *_demo.json）", file=sys.stderr)
        return 2

    # 收集所有需要的节点类型
    types: set[str] = set()
    wfs: dict[Path, dict] = {}
    for f in files:
        wf = json.loads(f.read_text(encoding="utf-8"))
        wfs[f] = wf
        types |= {n["type"] for n in wf.get("nodes") or []}
    print(f"从 {base} 读取 {len(types)} 个节点类型的 schema...")
    schemas = fetch_object_info(args.port, sorted(types))

    real_bad = 0
    env_bad = 0
    ok_count = 0
    for f, wf in wfs.items():
        print(f"\n=== {f.name} ===")
        api, problems = ui_to_api(wf, schemas)
        if problems:
            real_bad += 1
            for p in problems:
                print(f"  [FAIL] 转换失败：{p}")
            continue
        print(f"  转换完成：{len(api)} 个节点")

        payload = {"prompt": api, "client_id": "bettercomfyui-submit-check"}
        req = urllib.request.Request(
            f"{base}/prompt", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                err = json.loads(raw)
            except Exception:  # noqa: BLE001
                real_bad += 1
                print(f"  [FAIL] HTTP {exc.code}：{raw[:400]}")
                continue
            node_errors = err.get("node_errors") or {}
            if not node_errors and not err.get("error"):
                print(f"  [OK]   ComfyUI 接受了这个图（HTTP {exc.code}）")
                ok_count += 1
                continue
            real, env = classify(node_errors, api)
            for m in real:
                print(f"  [FAIL] {m}")
            for m in env:
                print(f"  [ENV]  {m}")
            if real:
                real_bad += 1
            elif env:
                env_bad += 1
                print("         （图结构本身被接受；仅权重未就位，属环境问题）")
            else:
                real_bad += 1
                print(f"  [FAIL] ComfyUI 报错但未归类：{json.dumps(err, ensure_ascii=False)[:300]}")
            continue

        pid = body.get("prompt_id")
        node_errors = body.get("node_errors") or {}
        if node_errors:
            real, env = classify(node_errors, api)
            for m in real:
                print(f"  [FAIL] {m}")
            for m in env:
                print(f"  [ENV]  {m}")
            if real:
                real_bad += 1
            else:
                env_bad += 1
            continue

        # 校验结果已经在响应里拿到了；**立刻**清队列，
        # 免得它真的开始加载 34 GB 权重（见 kill_queued 的 docstring）。
        if not args.allow_execute:
            print(f"  [OK]   图结构被 ComfyUI 接受（prompt_id={pid}）")
            print(f"         已清除队列：{kill_queued(base)}")
        else:
            print(f"  [OK]   图结构被 ComfyUI 接受，已入队 prompt_id={pid}")
            print("         [!] --allow-execute：任务会真的执行，注意内存")
        ok_count += 1

    print()
    print(f"汇总：图结构通过 {ok_count} 个；真问题 {real_bad} 个；"
          f"仅环境问题（权重未就位）{env_bad} 个")
    if real_bad:
        print("总结论：**存在问题**（图结构或输入不合法）")
        return 1
    if env_bad and args.strict_env:
        print("总结论：仅环境问题（--strict-env 下算失败）")
        return 1
    print("总结论：所有工作流的图结构都被 ComfyUI 接受"
          + ("（部分因权重未就位而无法执行，属环境问题）" if env_bad else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
