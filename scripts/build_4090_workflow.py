"""构建「剧核总流程 · 4090 远程适配版」工作流 + 对照远端 /object_info 做 schema 校验。

背景（实测，2026-10-18）
----
| | 本地开发机 | 远端 4090 |
|---|---|---|
| ComfyUI | 0.41.0 | **0.33.0** |
| 前端 | 1.55.17 | **1.48.7** |
| 巨人节点 | 已装（同源）| **未装** |
| 模型文件名 | 本地名 | 见 MODEL_MAP |

**关键差异**：远端前端 1.48.7 会在「带 control_after_generate 的 INT 控件」后面
多存一个 `control_after_generate` widget 值（"fixed"/"randomize"），本地 0.41 前端
已不再存它。于是把本地生成的工作流直接拿到远端加载，widget 值会**整体错位 1 位**，
点队列时报（远端日志实证 /tmp/bcu_comfyui2.log）：
    - Failed to convert an input value to a FLOAT value: cfg, euler
    - Value not in list: sampler_name: 'simple' not in (list of length 44)
    - Value not in list: scheduler: 1 not in [...]
本脚本按远端 schema **重排 widget 数组**（补 control 值、按名称搬运旧值、
类型/下拉值非法时回落默认值），并把模型名/项目名/成片剪辑自动发现一起改好。

另一个差异：MiniMaxH3Director 的 `BDGROUP` 三个分组控件（采样设置/高级采样/性能）
是巨人 web JS 动态挂到节点上的，/object_info 里看不到，但 widget 数组里占位置，
本脚本用 DIRECTOR_LAYOUT 显式声明（取自巨人包自带 r2v 示例工作流）。

用法（可离线用快照）
----
    python -B scripts/build_4090_workflow.py --remote-oi <远端快照> --local-oi <本地快照>

快照怎么来（只读 curl）：
    curl -s os.environ.get("BCU_REMOTE_BASE", "http://YOUR-GPU-HOST:8188")/object_info -o remote_oi.json
    curl -s http://127.0.0.1:8188/object_info      -o local_oi.json
"""

from __future__ import annotations

import os

import argparse
import copy
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "bettercomfyui_comfy" / "workflows" / "00_【总流程】剧本→成片（GPU）.json"
OUT = REPO_ROOT / "docs" / "远程适配工作流" / "00_剧核总流程_4090适配版.json"

# ---- 远端实测映射（2026-10-18 09:50 通过 /object_info 下拉清单核对） ----
PROJECT_NAME = "剧核试用"

# 节点 id -> {widget 名称: 新值}
MODEL_MAP = {
    5: {"ckpt_name": "RealVisXL_V5.0_fp16.safetensors"},
    13: {"unet_name": "minimax_h3_ref2va_pruned_int8_convrot.safetensors"},
    14: {"lora_name": "minimax_h3_fl2v_turbo_4step_v1.1_768p_comfyui_bf16.safetensors"},
    15: {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"},  # 4090 无 NVFP4
    16: {"vae_name": "minimax_h3_video_vae_fp16.safetensors"},
    17: {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"},
}

# 成片剪辑：开启「自动发现最新视频」，解决「演示输入错误：段视频路径列表为空」
FINALIZE_WIDGETS = {
    "项目": PROJECT_NAME,
    "段视频路径列表": "",
    "输出文件名": "",
    "编码CRF": 18,
    "完整性检查": "快速抽样（≤10分钟）",
    "自动发现最新视频": True,
    "发现时间窗_分钟": 720,
    "计划段数": 0,
    "每段计划时长_秒": 0.0,
}

PROJECT_WIDGETS = {"项目", "project_id"}

VALIDATE_TYPES = ("STRING", "INT", "FLOAT", "BOOLEAN", "COMBO",
                  "COMFY_DYNAMICCOMBO_V3")
CONTROL_VALUES = ["fixed", "randomize", "increment", "decrement"]

# 巨人 web JS 动态挂载的分组控件：/object_info 里没有，但 widget 数组占位。
# 位置 = 紧跟在锚点控件之后（取自巨人包 example_workflows/minimax_h3_director_r2v.json）
DIRECTOR_JS_WIDGETS = {
    "MiniMaxH3Director": [("bd_grp_sample", "global_prompt"),
                          ("bd_grp_advanced", "timeline_data"),
                          ("bd_grp_perf", "shift_audio")],
}


# ---------------------------------------------------------------- schema → layout
def widget_layout(info: dict, node_type: str) -> list[dict]:
    """推导远端前端保存的 widget 顺序（含 control_after_generate / JS 分组控件）。"""
    spec = info.get(node_type)
    if not spec:
        return []
    layout: list[dict] = []

    def emit(name: str, kind: str, opts: dict, meta: dict | None = None) -> None:
        layout.append({"name": name, "kind": kind, "opts": opts, **(meta or {})})
        if opts.get("control_after_generate"):
            layout.append({"name": "control_after_generate", "kind": "control",
                           "opts": {}, "control": True})

    js_anchors = {anchor: name for name, anchor in DIRECTOR_JS_WIDGETS.get(node_type, [])}
    for sec in ("required", "optional"):
        for name, decl in ((spec.get("input") or {}).get(sec) or {}).items():
            dtype = decl[0]
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
            if isinstance(dtype, list):
                emit(name, "combo", {**opts, "options": list(dtype)})
            elif dtype == "COMBO":
                emit(name, "combo", {**opts, "options": list(opts.get("options") or [])})
            elif dtype == "COMFY_DYNAMICCOMBO_V3":
                opts2 = dict(opts)
                opts2["options"] = [o.get("key") for o in (opts.get("options") or [])
                                   if isinstance(o, dict)]
                emit(name, "dynamic", opts2)
            elif dtype in VALIDATE_TYPES and not opts.get("forceInput"):
                emit(name, dtype, opts)
            if name in js_anchors:
                layout.append({"name": js_anchors[name], "kind": "jsgroup", "opts": {},
                               "js": True})
    return layout


def socket_schema(info: dict, node_type: str) -> set[str]:
    """真正的输入端口名集合：非 widget 类型 + forceInput 的 widget 转端口。"""
    spec = info.get(node_type) or {}
    out: set[str] = set()
    for sec in ("required", "optional"):
        for name, decl in ((spec.get("input") or {}).get(sec) or {}).items():
            dtype = decl[0]
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
            if isinstance(dtype, list):
                if opts.get("forceInput"):
                    out.add(name)
            elif dtype not in VALIDATE_TYPES or opts.get("forceInput"):
                out.add(name)
    return out


def default_for(meta: dict):
    kind, opts = meta["kind"], meta["opts"]
    if kind == "control":
        return "fixed"
    if kind == "jsgroup":
        return opts.get("default", "")
    if kind in ("combo", "dynamic"):
        return opts.get("default", (opts.get("options") or [""])[0])
    return opts.get("default", {"INT": 0, "FLOAT": 0.0, "BOOLEAN": False}.get(kind, ""))


def coerce(meta: dict, value):
    """把旧前端留下的值校准成远端 schema 接受的值；不合法返回默认值。"""
    kind = meta["kind"]
    if kind == "control":
        return value if value in CONTROL_VALUES else "fixed"
    if kind in ("combo", "dynamic"):
        return value if value in meta["opts"].get("options", []) else default_for(meta)
    if kind == "INT":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return default_for(meta)
        return value
    if kind == "FLOAT":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default_for(meta)
        return float(value)
    if kind == "BOOLEAN":
        return value if isinstance(value, bool) else default_for(meta)
    return value


# ---------------------------------------------------------------- adapt
def adapt(wf: dict, remote: dict, local: dict) -> tuple[dict, list[str]]:
    """layout 参考：远端 schema 为准；巨人节点的 JS 控件按本地（同源）实现补。"""
    wf = copy.deepcopy(wf)
    notes: list[str] = []
    by_id = {n["id"]: n for n in wf["nodes"]}
    orig_vals = {n["id"]: list(n.get("widgets_values") or []) for n in wf["nodes"]}

    for node in wf["nodes"]:
        ntype = node["type"]
        info = local if (ntype in DIRECTOR_JS_WIDGETS and ntype in local) else remote
        layout = widget_layout(info, ntype)
        if not layout:
            continue
        vals = list(node.get("widgets_values") or [])
        old_layout = [e for e in layout if not e.get("control")]
        has_control = len(layout) != len(old_layout)

        if has_control and len(vals) == len(old_layout):
            # 本地 0.41 不存 control 值 → 按名称一对一搬运，control 位置补默认
            old_map = {e["name"]: v for e, v in zip(old_layout, vals)}
            new_vals = [old_map.get(e["name"], default_for(e)) for e in layout]
            notes.append(f"补 control_after_generate id={node['id']} {ntype}: "
                         f"{len(vals)} -> {len(layout)} 个 widget（前端版本差异）")
            vals = new_vals
        else:
            if len(vals) > len(layout):
                notes.append(f"截断多余 widget id={node['id']} {ntype}: "
                             f"{len(vals)} -> {len(layout)}（丢弃 {vals[len(layout):]!r}）")
            while len(vals) < len(layout):
                extra = layout[len(vals)]
                notes.append(f"补齐缺失 widget id={node['id']} {ntype}.{extra['name']} = "
                             f"{default_for(extra)!r}")
                vals.append(default_for(extra))
            vals = vals[:len(layout)]

        # 类型/下拉值校准
        for i, meta in enumerate(layout):
            new = coerce(meta, vals[i])
            if new != vals[i]:
                notes.append(f"值校准 id={node['id']} {ntype}.{meta['name']}: "
                             f"{vals[i]!r} -> {new!r}")
            vals[i] = new
        node["widgets_values"] = vals

        # 按名称覆盖：模型 / 项目 / 成片剪辑
        idx = {e["name"]: i for i, e in enumerate(layout)}
        overrides: dict[str, object] = {}
        overrides.update(MODEL_MAP.get(node["id"], {}))
        overrides.update({k: v for k, v in FINALIZE_WIDGETS.items()
                          if ntype == "BetterComfyUIFinalize"})
        for name in PROJECT_WIDGETS & set(idx):
            overrides[name] = PROJECT_NAME
        for name, value in overrides.items():
            if name not in idx:
                notes.append(f"警告：{ntype} 无 widget「{name}」，跳过覆盖")
                continue
            i = idx[name]
            if vals[i] != value:
                notes.append(f"覆盖 id={node['id']} {ntype}.{name}: {vals[i]!r} -> {value!r}")
            vals[i] = value

    # 巨人 task_type 用远端合法完整标签（"r2v — 参考主体生视频(Reference to Video)"）
    for node in wf["nodes"]:
        if node["type"] != "MiniMaxH3Director":
            continue
        layout = widget_layout(local, node["type"])
        idx = {e["name"]: i for i, e in enumerate(layout)}
        vals = node["widgets_values"]
        task_meta = layout[idx["task_type"]]
        options = task_meta["opts"].get("options") or []
        options = [o for o in options if isinstance(o, str)]
        # 源数组第 0 位就是 task_type（本地 0.41 与远端字段顺序一致），用它还原意图
        desired = str((orig_vals.get(node["id"]) or [""])[0] or "r2v")
        match = next((o for o in options if o.strip().lower().startswith(
            desired.strip().lower())), None)
        if match is None:
            match = next((o for o in options if "r2v" in o.lower()), None)
        if match is not None:
            if vals[idx["task_type"]] != match:
                notes.append(f"巨人 task_type: {desired!r}/{vals[idx['task_type']]!r} -> {match!r}")
            vals[idx["task_type"]] = match

    # 使用说明文本里的项目名同步
    for node in wf["nodes"]:
        if node["type"] != "PrimitiveStringMultiline":
            continue
        vals = node.get("widgets_values") or []
        if vals and isinstance(vals[0], str) and "我的短剧" in vals[0]:
            count = vals[0].count("我的短剧")
            vals[0] = vals[0].replace("我的短剧", PROJECT_NAME)
            notes.append(f"说明文本 id={node['id']}: 「我的短剧」->「{PROJECT_NAME}」（{count} 处）")
            node["widgets_values"] = vals

    wf["extra"] = {
        "note": (
            "【剧核总流程 · 4090 远程适配版】\n"
            "目标机：ComfyUI 0.33.0 / 前端 1.48.7；模型名已换成远端实际文件名；\n"
            f"项目默认「{PROJECT_NAME}」；成片剪辑已开「自动发现最新视频」（时间窗 720 分钟）；\n"
            "widget 数组已按远端前端（含 control_after_generate）重排，避免值错位。\n"
            "导入前确认已装 6 个巨人节点（scripts/远程安装巨人.sh），否则 MiniMaxH3Director 仍是红节点。\n"
            "首次运行前：右侧 Better ComfyUI 面板填 DeepSeek 密钥。"
        ),
        "dsh_adaptation": {
            "source_workflow": SRC.name,
            "target_host": "RTX 4090 / ComfyUI 0.33.0 / 前端 1.48.7",
            "project_default": PROJECT_NAME,
            "adapted_at": "2026-10-18",
            "known_diffs": {
                "control_after_generate": "远端前端 1.48.7 多存该 widget 值，本版已补齐",
                "CreateVideo": "远端只有 [fps, bit_depth]（本地还有 color_space/codec）",
                "SaveVideo": "远端有 [filename_prefix, format, codec]（本地 1 个）",
                "MiniMaxH3Director_js_widgets": "bd_grp_sample/bd_grp_advanced/bd_grp_perf 由巨人 JS 挂载",
            },
        },
    }
    return wf, notes


# ---------------------------------------------------------------- validate
def validate(wf: dict, info: dict, label: str) -> list[str]:
    problems: list[str] = []
    ids = {n["id"]: n for n in wf["nodes"]}

    for node in wf["nodes"]:
        if node["type"] not in info:
            problems.append(f"[{label}] 节点不存在：{node['type']}（id={node['id']}）")

    for node in wf["nodes"]:
        layout = widget_layout(info, node["type"])
        if not layout:
            continue
        vals = list(node.get("widgets_values") or [])
        if len(vals) != len(layout):
            problems.append(f"[{label}] {node['type']} id={node['id']} widget 数量 "
                            f"{len(vals)} != schema {len(layout)}")
        for i, meta in enumerate(layout):
            if i >= len(vals):
                continue
            val = vals[i]
            if coerce(meta, val) != val:
                problems.append(f"[{label}] 值不合法 {node['type']}.{meta['name']}={val!r}"
                                f"（kind={meta['kind']}）")

    for link in wf["links"]:
        lid, src, sslot, dst, _dslot, dtype = link
        sn, dn = ids.get(src), ids.get(dst)
        if not sn or not dn:
            problems.append(f"[{label}] link {lid} 指向不存在的节点")
            continue
        if sn["type"] not in info or dn["type"] not in info:
            continue
        outs = info[sn["type"]].get("output") or []
        if sslot >= len(outs):
            problems.append(f"[{label}] link {lid} 源槽位越界 {sn['type']}[{sslot}]")
        elif outs[sslot] != dtype and dtype != "*":
            problems.append(f"[{label}] link {lid} 类型不匹配 "
                            f"{sn['type']}[{sslot}]={outs[sslot]} vs {dtype}")
        din = next((x for x in (dn.get("inputs") or []) if x.get("link") == lid), None)
        if din is None:
            problems.append(f"[{label}] link {lid} 目标节点 inputs 里没有该连线记录")
            continue
        allowed = socket_schema(info, dn["type"]) | {
            e["name"] for e in widget_layout(info, dn["type"])}
        if din["name"] not in allowed:
            problems.append(f"[{label}] link {lid} 目标端口 {dn['type']}.{din['name']} 不在 schema")
        dout = next((x for x in (sn.get("outputs") or []) if lid in (x.get("links") or [])), None)
        if dout is None:
            problems.append(f"[{label}] link {lid} 未记录在源节点 outputs[..].links")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remote-oi", default="", help="远端 /object_info 快照 json")
    ap.add_argument("--local-oi", default="", help="本地 /object_info 快照 json（补巨人定义）")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--check", action="store_true", help="只校验不写文件")
    args = ap.parse_args(argv)

    wf = json.loads(SRC.read_text(encoding="utf-8"))
    if args.remote_oi:
        remote = json.loads(Path(args.remote_oi).read_text(encoding="utf-8"))
    else:
        import urllib.request
        remote = json.load(urllib.request.urlopen(
            "os.environ.get("BCU_REMOTE_BASE", "http://YOUR-GPU-HOST:8188")/object_info", timeout=60))
    local = {}
    if args.local_oi:
        local = json.loads(Path(args.local_oi).read_text(encoding="utf-8"))

    adapted, notes = adapt(wf, remote, local)

    print("=" * 78)
    print("适配改动清单")
    print("=" * 78)
    for line in notes:
        print("  -", line)

    print("\n" + "=" * 78)
    print("校验 1/2：直连远端 0.33（未装巨人）")
    print("=" * 78)
    p_remote = validate(adapted, remote, "远端")
    expected_missing = [p for p in p_remote if "节点不存在" in p]
    unexpected = [p for p in p_remote if "节点不存在" not in p]
    for line in expected_missing:
        print("  [预期缺失]", line)
    for line in unexpected:
        print("  [问题]", line)
    print(f"  → 预期缺失 {len(expected_missing)} 条（巨人节点），其它问题 {len(unexpected)} 条")

    p_comp: list[str] = []
    if local:
        composite = dict(remote)
        for key, spec in local.items():
            composite.setdefault(key, spec)
        print("\n" + "=" * 78)
        print("校验 2/2：复合 schema（远端 0.33 + 本地巨人节点定义，模拟装好巨人）")
        print("=" * 78)
        p_comp = validate(adapted, composite, "复合")
        if not p_comp:
            print("  [OK] 0 个问题：节点/下拉值/widget/连线全部与 schema 匹配")
        for line in p_comp:
            print("  [问题]", line)

    if not args.check:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(adapted, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"\n已写出：{out}")
        print(f"节点数={len(adapted['nodes'])} 连线数={len(adapted['links'])} "
              f"（源工作流 {len(wf['nodes'])}/{len(wf['links'])}）")

    bad = len(unexpected) + len(p_comp)
    print("\n结论：", "PASS（除预期的巨人节点缺失外全部通过）" if bad == 0
          else f"FAIL（{bad} 个问题需修）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
