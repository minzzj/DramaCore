#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 ComfyUI 工作流 JSON（UI 图格式），确保它真能被 ComfyUI 加载并执行。

用法::

    python bettercomfyui_comfy/workflows/validate_workflow.py
    python bettercomfyui_comfy/workflows/validate_workflow.py --workflow 别的.json
    python bettercomfyui_comfy/workflows/validate_workflow.py --package bettercomfyui_comfy

退出码：``0`` = PASS，``1`` = FAIL（工作流有问题），``2`` = 无法校验（环境/文件问题）。

为什么需要这个脚本（而不是肉眼看 JSON）：

1. ComfyUI 的节点 ``type`` 必须**逐字**等于 ``NODE_CLASS_MAPPINGS`` 的 key。
   差一个字母，加载工作流时就是一个红框 "Node type not found"。
   本脚本**真 import** ``bettercomfyui_comfy`` 取注册表，绝不硬编码节点名。
2. ``widgets_values`` 是**按位置**对齐控件输入的数组（required 先、optional 后），
   **包括已经被连线占用的那个控件槽位**（实证：真实工作流里 BasicScheduler 的
   ``steps`` 已连线，``widgets_values`` 仍然为它占一格）。少一项就会让后面所有
   控件整体错位 —— 界面上看起来"值不对"，但 JSON 本身完全合法，肉眼极难发现。
3. 必填输入既没连线、又没取到值 -> ComfyUI 会在执行时报错。
4. ``outputs`` 的名称/类型与节点类的 ``RETURN_TYPES``/``RETURN_NAMES`` 不一致，
   说明工作流是旧版本节点写的（schema 漂移），必须显式报出来。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

#: 本文件在 <repo>/bettercomfyui_comfy/workflows/ 下 —— 仓库根是上两级。
#: 直接把仓库根塞进 sys.path，这样「以脚本方式运行」也能 import 到本包
#: （否则 sys.path[0] 是 workflows/ 目录，import bettercomfyui_comfy 会失败）。
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKFLOW = Path(__file__).resolve().with_name("minimal_workflow.json")
DEFAULT_PACKAGE = "bettercomfyui_comfy"

#: UI 里会渲染成「控件」的输入类型。其余类型（IMAGE/MODEL/LATENT/...）只能靠连线喂值。
WIDGET_TYPE_NAMES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"})

#: ComfyUI 前端要求的最小顶层字段。
REQUIRED_TOP_KEYS = ("nodes", "links", "last_node_id", "last_link_id", "version")
KNOWN_TOP_KEYS = frozenset(
    REQUIRED_TOP_KEYS
    + ("id", "revision", "groups", "config", "extra", "definitions", "models", "labels")
)
GRAPH_VERSION = 0.4

#: 本工作流的硬性验收要求（用户明确指定）：只放 3 个节点、且两条连线必须存在。
EXPECTED_NODE_TYPES = ("BetterComfyUIProject", "BetterComfyUIRun", "BetterComfyUIResult")
EXPECTED_LINKS = (
    ("BetterComfyUIProject", "project_id", "BetterComfyUIRun", "project_id"),
    ("BetterComfyUIRun", "report_json", "BetterComfyUIResult", "report_json"),
)

_MISSING = object()  #: 「没取到值」的哨兵，与合法的 None / "" 区分开


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


class Report:
    """人类可读的 PASS/FAIL 收集器，逐条打印，末尾汇总。"""

    def __init__(self) -> None:
        self.n_ok = 0
        self.n_warn = 0
        self.fail_msgs: list[str] = []

    @property
    def n_fail(self) -> int:
        return len(self.fail_msgs)

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")

    def _emit(self, tag: str, msg: str) -> None:
        lines = str(msg).splitlines() or [""]
        print(f"  [{tag}] {lines[0]}")
        for extra in lines[1:]:
            print(f"         {extra}")

    def ok(self, msg: str) -> None:
        self.n_ok += 1
        self._emit(" OK ", msg)

    def warn(self, msg: str) -> None:
        self.n_warn += 1
        self._emit("WARN", msg)

    def fail(self, msg: str) -> None:
        self.fail_msgs.append(str(msg))
        self._emit("FAIL", msg)


def _short(value: object, limit: int = 48) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------------------
# INPUT_TYPES 规格解析（兼容 V1 的几种写法）
# ---------------------------------------------------------------------------


def spec_type(spec: object) -> object:
    """取输入声明的「类型」部分。``("STRING", {...})`` -> ``"STRING"``。"""
    if isinstance(spec, dict):
        return spec.get("type", spec)
    if isinstance(spec, (list, tuple)):
        return spec[0] if spec else None
    return spec


def is_widget(spec: object) -> bool:
    """该输入在 UI 上是否渲染成控件（可填值），而不是纯连线插槽。"""
    t = spec_type(spec)
    if isinstance(t, (list, tuple, set, frozenset)):
        return True  # 旧式 COMBO：直接把选项列表当类型
    if isinstance(t, dict):
        return True  # V3 动态 combo
    return isinstance(t, str) and t.upper() in WIDGET_TYPE_NAMES


def combo_options(spec: object) -> list[str] | None:
    """COMBO 的允许取值；无法确定时返回 None（此时跳过取值校验，不误报）。"""
    t = spec_type(spec)
    if isinstance(t, (list, tuple)):
        return [str(x) for x in t]
    candidates: list[object] = []
    if isinstance(spec, (list, tuple)) and len(spec) > 1:
        candidates.append(spec[1])
    if isinstance(spec, dict):
        candidates.append(spec.get("options"))
    for cand in candidates:
        if isinstance(cand, (list, tuple)):
            return [str(x) for x in cand]
        if isinstance(cand, dict) and isinstance(cand.get("options"), (list, tuple)):
            return [str(x) for x in cand["options"]]
    return None


def widget_slot_count(spec: object) -> int:
    """该控件在 ``widgets_values`` 里占几个槽位。

    前端会在声明了 ``control_after_generate`` 的 INT 控件后面**再插一个下拉**，
    于是那个控件占两格。本包的节点没有这种控件，但通用性留着，避免误判。
    """
    if isinstance(spec_type(spec), str) and str(spec_type(spec)).upper() == "INT":
        opts = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 else None
        if isinstance(opts, dict) and opts.get("control_after_generate"):
            return 2
    return 1


def norm_link(raw: object) -> dict | None:
    """把一条 link 规范成 dict。

    ComfyUI 前端两种序列化都支持：
    * 数组 ``[id, origin_id, origin_slot, target_id, target_slot, type]``
      —— 本仓库 127 个真实工作流共 3766 条 link **全部**是这种写法
    * 对象 ``{"id","origin_id","origin_slot","target_id","target_slot","type"}``
    """
    if isinstance(raw, (list, tuple)):
        if len(raw) < 6:
            return None
        return {
            "id": raw[0],
            "origin_id": raw[1],
            "origin_slot": raw[2],
            "target_id": raw[3],
            "target_slot": raw[4],
            "type": raw[5],
        }
    if isinstance(raw, dict):
        if not {"id", "origin_id", "target_id"} <= set(raw):
            return None
        return {
            "id": raw["id"],
            "origin_id": raw["origin_id"],
            "origin_slot": raw.get("origin_slot", 0),
            "target_id": raw["target_id"],
            "target_slot": raw.get("target_slot", 0),
            "type": raw.get("type"),
        }
    return None


# ---------------------------------------------------------------------------
# 主校验
# ---------------------------------------------------------------------------


def validate(workflow_path: Path, package: str) -> int:
    rep = Report()
    print("=" * 72)
    print("Better ComfyUI · 工作流 JSON 校验（真 import 节点注册表）")
    print("=" * 72)
    print(f"工作流  : {workflow_path}")
    print(f"节点包  : {package}")
    print(f"仓库根  : {REPO_ROOT}")

    # ---- 0. 文件与 JSON ---------------------------------------------------
    rep.section("0. 文件与 JSON 解析")
    if not workflow_path.is_file():
        rep.fail(f"文件不存在：{workflow_path}")
        return _finish(rep)
    raw = workflow_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        rep.fail("文件带 UTF-8 BOM —— 前端 JSON.parse 会直接失败（务必以无 BOM 的 UTF-8 保存）")
    else:
        rep.ok(f"编码 OK：{len(raw)} 字节，UTF-8 且无 BOM")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        rep.fail(f"json.load 失败：{type(exc).__name__}: {exc}")
        return _finish(rep)
    if not isinstance(doc, dict):
        rep.fail(f"顶层必须是 JSON 对象，收到 {type(doc).__name__}")
        return _finish(rep)
    rep.ok("json.load 成功，顶层是对象")

    # ---- 1. 节点注册表（真 import） ---------------------------------------
    rep.section("1. 节点注册表（真 import，不硬编码）")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        pkg = importlib.import_module(package)
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都要给人看
        rep.fail(f"import {package} 失败：{type(exc).__name__}: {exc}")
        rep.fail("提示：确认本仓库根目录下有 bettercomfyui_comfy/__init__.py 与 nodes.py")
        return _finish(rep)
    pkg_file = getattr(pkg, "__file__", None)
    mappings = getattr(pkg, "NODE_CLASS_MAPPINGS", None)
    if not isinstance(mappings, dict):
        rep.fail(
            f"{package}.NODE_CLASS_MAPPINGS 不是 dict"
            f"（实际 {type(mappings).__name__}）—— ComfyUI 无法注册任何节点"
        )
        return _finish(rep)
    rep.ok(f"import {package} 成功：{pkg_file}")
    if mappings:
        rep.ok(f"NODE_CLASS_MAPPINGS 共 {len(mappings)} 个节点：" + ", ".join(sorted(mappings)))
    else:
        rep.fail(
            "NODE_CLASS_MAPPINGS 为空。注意 bettercomfyui_comfy/__init__.py 会**吞掉**\n"
            "nodes.py 的导入异常（有意为之：避免拖垮 ComfyUI 启动），所以「import 成功」\n"
            "并不代表节点已注册。下面直接再 import 一次 nodes 模块，把真实原因打出来："
        )
        try:
            importlib.import_module(f"{package}.nodes")
            rep.warn("直接 import nodes 竟然成功 —— 请人工检查 __init__.py 的加载逻辑")
        except Exception as exc:  # noqa: BLE001
            rep.fail(f"  -> {package}.nodes 导入异常：{type(exc).__name__}: {exc}")

    # ---- 2. 顶层字段 ------------------------------------------------------
    rep.section("2. 顶层字段")
    missing_top = [k for k in REQUIRED_TOP_KEYS if k not in doc]
    if missing_top:
        rep.fail(f"缺少 ComfyUI 要求的顶层字段：{missing_top}")
    else:
        rep.ok("必填顶层字段齐全：" + ", ".join(REQUIRED_TOP_KEYS))
    unknown_top = sorted(k for k in doc if k not in KNOWN_TOP_KEYS)
    if unknown_top:
        rep.warn(f"出现未知顶层字段（前端会忽略）：{unknown_top}")
    if doc.get("version") != GRAPH_VERSION:
        rep.warn(f"version={doc.get('version')!r}，当前前端主版本是 {GRAPH_VERSION}")

    nodes = doc.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        rep.fail("nodes 必须是非空数组")
        return _finish(rep)

    # ---- 3. 节点 type 是否逐字命中 ----------------------------------------
    rep.section("3. 每个节点的 type 是否逐字命中 NODE_CLASS_MAPPINGS")
    by_id: dict[int, dict] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            rep.fail(f"nodes[{index}] 不是对象")
            continue
        nid = node.get("id")
        if isinstance(nid, str) and nid.isdigit():
            nid = int(nid)
            rep.warn(f"nodes[{index}] 的 id 是字符串，已按 {nid} 处理")
        if not isinstance(nid, int) or isinstance(nid, bool):
            rep.fail(f"nodes[{index}] 缺少合法的整数 id：{node.get('id')!r}")
            continue
        if nid in by_id:
            rep.fail(f"节点 id 重复：{nid}")
        ntype = node.get("type")
        if not isinstance(ntype, str) or not ntype.strip():
            rep.fail(f"节点 {nid} 缺少 type")
            continue
        if ntype != ntype.strip():
            rep.fail(f"节点 {nid} 的 type 首尾有空白：{ntype!r}（必须逐字一致）")
        if ntype in mappings:
            cls = mappings[ntype]
            rep.ok(f"节点 {nid}: {ntype}  ->  {getattr(cls, '__module__', '?')}")
        else:
            near = [k for k in mappings if k.lower() == ntype.lower()]
            hint = (
                f"；大小写/空白不同的候选：{near}"
                if near
                else f"；注册表里只有：{sorted(mappings)}"
            )
            rep.fail(f"节点 {nid} 的 type={ntype!r} 不在 NODE_CLASS_MAPPINGS 里{hint}")
        by_id[nid] = node

    # ---- 4. links ---------------------------------------------------------
    rep.section("4. links：两端节点/槽位存在，且与插槽自洽")
    raw_links = doc.get("links")
    if not isinstance(raw_links, list):
        rep.fail(f"links 必须是数组，收到 {type(raw_links).__name__}")
        return _finish(rep)
    links: dict[object, dict] = {}
    for i, raw_link in enumerate(raw_links):
        info = norm_link(raw_link)
        if info is None:
            rep.fail(f"links[{i}] 既不是长度>=6 的数组，也不是合法对象：{_short(raw_link)}")
            continue
        if info["id"] in links:
            rep.fail(f"link id 重复：{info['id']}")
        links[info["id"]] = info

    for lid in sorted(links, key=str):
        link = links[lid]
        origin = by_id.get(link["origin_id"])
        target = by_id.get(link["target_id"])
        if origin is None:
            rep.fail(f"link {lid}: origin 节点 {link['origin_id']} 不存在")
            continue
        if target is None:
            rep.fail(f"link {lid}: target 节点 {link['target_id']} 不存在")
            continue
        outs = origin.get("outputs") or []
        ins = target.get("inputs") or []
        if not 0 <= link["origin_slot"] < len(outs):
            rep.fail(f"link {lid}: origin_slot={link['origin_slot']} 越界（{origin['type']} 有 {len(outs)} 个输出）")
            continue
        if not 0 <= link["target_slot"] < len(ins):
            rep.fail(f"link {lid}: target_slot={link['target_slot']} 越界（{target['type']} 有 {len(ins)} 个输入）")
            continue
        out_slot, in_slot = outs[link["origin_slot"]], ins[link["target_slot"]]
        if lid not in list(out_slot.get("links") or []):
            rep.fail(f"link {lid}: origin 输出 {out_slot.get('name')!r}.links 里没有 {lid}（两端不一致）")
        if in_slot.get("link") != lid:
            rep.fail(
                f"link {lid}: 目标输入 {in_slot.get('name')!r}.link={in_slot.get('link')!r} "
                f"与 link id 不一致"
            )
        if link.get("type") and out_slot.get("type") and link["type"] != out_slot["type"]:
            rep.warn(f"link {lid}: link type={link['type']!r} 与 origin 输出类型 {out_slot['type']!r} 不同")
        if link.get("type") and in_slot.get("type") and link["type"] != in_slot["type"]:
            rep.warn(f"link {lid}: link type={link['type']!r} 与目标输入类型 {in_slot['type']!r} 不同")
        rep.ok(
            f"link {lid}: {origin['type']}.{out_slot.get('name')} -> "
            f"{target['type']}.{in_slot.get('name')} [{link.get('type')}]"
        )

    for node in nodes:
        if not isinstance(node, dict):
            continue
        for slot in node.get("inputs") or []:
            if not isinstance(slot, dict):
                continue
            lk = slot.get("link")
            if lk is not None and lk not in links:
                rep.fail(f"节点 {node.get('id')} 输入 {slot.get('name')!r} 的 link={lk} 在 links 里不存在")
        for slot in node.get("outputs") or []:
            if not isinstance(slot, dict):
                continue
            for lk in slot.get("links") or []:
                if lk not in links:
                    rep.fail(f"节点 {node.get('id')} 输出 {slot.get('name')!r} 引用了不存在的 link {lk}")

    # ---- 5. 必填输入是否都有值 + 控件对齐 ---------------------------------
    rep.section("5. 必填 INPUT_TYPES 是否都有值（连线 或 控件值）")
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in mappings:
            continue
        ntype = node["type"]
        cls = mappings[ntype]
        try:
            input_types = cls.INPUT_TYPES()
        except Exception as exc:  # noqa: BLE001
            rep.fail(f"{ntype}.INPUT_TYPES() 抛异常：{type(exc).__name__}: {exc}")
            continue
        if not isinstance(input_types, dict):
            rep.fail(f"{ntype}.INPUT_TYPES() 必须返回 dict，收到 {type(input_types).__name__}")
            continue
        required = input_types.get("required") or {}
        optional = input_types.get("optional") or {}

        # 控件槽位：按 INPUT_TYPES 顺序（required 先、optional 后）逐位展开
        slot_index: dict[str, int] = {}
        slot_names: list[str] = []
        ordered = list(required.items()) + list(optional.items())
        for name, spec in ordered:
            if not is_widget(spec):
                continue
            slot_index[name] = len(slot_names)
            slot_names.extend([name] * widget_slot_count(spec))
            if isinstance(spec_type(spec), str) and str(spec_type(spec)).upper() == "INT":
                if str(name).lower() in {"seed", "noise_seed"}:
                    rep.warn(f"{ntype}.{name} 名字像随机种子：某些前端版本会额外插一个下拉，槽位可能错位")

        widgets_values = node.get("widgets_values")
        wv_list: list | None = None
        wv_map: dict | None = None
        if widgets_values is None:
            wv_list = []
        elif isinstance(widgets_values, list):
            wv_list = widgets_values
        elif isinstance(widgets_values, dict):
            wv_map = widgets_values
            rep.warn(f"{ntype}: widgets_values 是对象形式（旧写法），已按名字取值")
        else:
            rep.fail(f"{ntype}: widgets_values 类型非法：{type(widgets_values).__name__}")
            wv_list = []

        def value_of(name: str) -> object:
            if wv_map is not None:
                return wv_map.get(name, _MISSING)
            idx = slot_index.get(name)
            if idx is None or idx >= len(wv_list):
                return _MISSING
            return wv_list[idx]

        inputs_by_name: dict[str, dict] = {}
        for slot in node.get("inputs") or []:
            if isinstance(slot, dict) and isinstance(slot.get("name"), str):
                inputs_by_name[slot["name"]] = slot

        allowed_names = set(required) | set(optional)
        for name in inputs_by_name:
            if name not in allowed_names:
                rep.fail(
                    f"{ntype}: 节点 inputs 里的 {name!r} 不是该节点的输入名"
                    f"（INPUT_TYPES 只有 {sorted(allowed_names)}）"
                )

        # inputs 的**顺序**必须与 INPUT_TYPES 一致（required 先、optional 后）。
        # 原因：link 的 target_slot 就是这个数组的下标，而真实工作流里
        # inputs 顺序 == INPUT_TYPES 顺序（实测 127 个真实工作流）。顺序错位会让
        # 连线悄悄落到另一个输入上 —— 界面看着有连线，执行时喂给的却是别的参数。
        serialized_order = [name for name in inputs_by_name if name in allowed_names]
        expected_order = [name for name, _ in ordered if name in inputs_by_name]
        if set(serialized_order) == set(expected_order):
            if serialized_order == expected_order:
                rep.ok(f"{ntype}: inputs 顺序与 INPUT_TYPES 一致（{len(serialized_order)} 项）")
            else:
                rep.fail(
                    f"{ntype}: inputs 顺序与 INPUT_TYPES 不一致\n"
                    f"  实际：{serialized_order}\n"
                    f"  期望：{expected_order}\n"
                    f"  （target_slot 是该数组下标，顺序错位会让连线接到别的输入上）"
                )

        for name in required:
            spec = required[name]
            entry = inputs_by_name.get(name)
            if entry is None:
                rep.fail(f"{ntype}: 必填输入 {name!r} 在节点 inputs 里完全没有条目")
                continue
            if entry.get("link") is not None:
                rep.ok(f"{ntype}.{name}：必填，已连线 link={entry['link']}")
                continue
            if not is_widget(spec):
                rep.fail(
                    f"{ntype}: 必填输入 {name!r}（类型 {spec_type(spec)!r}）不能在界面上填值，"
                    f"却又没有连线 —— ComfyUI 执行时会报缺输入"
                )
                continue
            value = value_of(name)
            if value is _MISSING:
                rep.fail(
                    f"{ntype}: 必填控件 {name!r} 取不到值（widgets_values 只有 "
                    f"{len(wv_list)} 项，至少需要 {len(slot_names)} 项）"
                )
            elif value is None:
                rep.fail(f"{ntype}: 必填控件 {name!r} 的值是 null")
            else:
                options = combo_options(spec)
                if options is not None and isinstance(value, str) and value not in options:
                    rep.fail(f"{ntype}.{name}={value!r} 不在允许值里 {options}")
                else:
                    rep.ok(f"{ntype}.{name}：必填，有值 {_short(value)}")

        if wv_list is not None:
            need = len(slot_names)
            got = len(wv_list)
            if got < need:
                rep.fail(
                    f"{ntype}: widgets_values 只有 {got} 项，控件需要 {need} 项 "
                    f"（{slot_names}）—— 少的那些会让后面的控件整体错位"
                )
            elif got > need:
                rep.warn(f"{ntype}: widgets_values 有 {got} 项，多于控件槽位 {need} 项（多余项被忽略）")
            else:
                rep.ok(f"{ntype}: widgets_values 与控件槽位逐位对齐（{need} 项：{slot_names}）")

        # 可选控件也应当在 inputs 里有条目（前端保存时的规范形态）
        for name in optional:
            if is_widget(optional[name]) and name not in inputs_by_name:
                rep.warn(f"{ntype}: 可选控件 {name!r} 在 inputs 里没有条目（仍可加载，但格式不规范）")

        # ---- 6. 输出与类声明是否一致（schema 漂移检查） -------------------
        return_types = getattr(cls, "RETURN_TYPES", None)
        return_names = getattr(cls, "RETURN_NAMES", None)
        outs = node.get("outputs") or []
        if isinstance(return_types, (list, tuple)):
            if len(outs) != len(return_types):
                rep.fail(
                    f"{ntype}: 节点声明 {len(outs)} 个输出，类声明 {len(return_types)} 个"
                    f"（RETURN_TYPES）—— 工作流与节点版本不匹配"
                )
            else:
                drift = False
                for i, slot in enumerate(outs):
                    if slot.get("type") != return_types[i]:
                        rep.fail(
                            f"{ntype} 输出[{i}] type={slot.get('type')!r} "
                            f"!= RETURN_TYPES[{i}]={return_types[i]!r}"
                        )
                        drift = True
                    if isinstance(return_names, (list, tuple)) and i < len(return_names):
                        if slot.get("name") != return_names[i]:
                            rep.fail(
                                f"{ntype} 输出[{i}] name={slot.get('name')!r} "
                                f"!= RETURN_NAMES[{i}]={return_names[i]!r}"
                            )
                            drift = True
                if not drift:
                    rep.ok(f"{ntype}: {len(outs)} 个输出的名称/类型与 RETURN_TYPES/RETURN_NAMES 一致")

    # ---- 7. 图结构 --------------------------------------------------------
    rep.section("6. 图结构（无环）与节点状态")
    adjacency: dict[object, list] = {}
    for link in links.values():
        adjacency.setdefault(link["origin_id"], []).append(link["target_id"])
    color: dict[object, int] = {}
    cycle: list | None = None

    def dfs(node_id: object, path: list) -> list | None:
        color[node_id] = 1
        for nxt in adjacency.get(node_id, []):
            if color.get(nxt) == 1:
                return path + [node_id, nxt]
            if color.get(nxt) is None:
                found = dfs(nxt, path + [node_id])
                if found:
                    return found
        color[node_id] = 2
        return None

    for nid in by_id:
        if color.get(nid) is None:
            cycle = dfs(nid, [])
            if cycle:
                break
    if cycle:
        rep.fail("连线存在环：" + " -> ".join(str(x) for x in cycle) + "（ComfyUI 无法执行）")
    else:
        rep.ok("连线无环（DAG）")
    for node in nodes:
        if isinstance(node, dict) and node.get("mode", 0) != 0:
            rep.warn(f"节点 {node.get('id')} mode={node.get('mode')}（2=静音 / 4=绕过），不会执行")

    # ---- 8. 本工作流的硬要求 ---------------------------------------------
    rep.section("7. 本工作流的硬性要求")
    types_present = [n.get("type") for n in nodes if isinstance(n, dict)]
    if len(nodes) != len(EXPECTED_NODE_TYPES):
        rep.fail(f"节点数应为 {len(EXPECTED_NODE_TYPES)} 个（最简工作流），实际 {len(nodes)} 个")
    else:
        rep.ok(f"节点数 = {len(nodes)}（最简工作流要求）")
    if sorted(types_present) != sorted(EXPECTED_NODE_TYPES):
        rep.fail(f"节点集合应为 {sorted(EXPECTED_NODE_TYPES)}，实际 {sorted(types_present)}")
    else:
        rep.ok("节点集合正确：只有 3 个本包节点，无第三方节点")
    if types_present and not all(t in mappings for t in types_present):
        rep.fail("存在不属于本包的节点类型（用户机器上可能没装）")
    for origin_type, origin_out, target_type, target_in in EXPECTED_LINKS:
        found = False
        for link in links.values():
            origin = by_id.get(link["origin_id"])
            target = by_id.get(link["target_id"])
            if origin is None or target is None:
                continue
            outs = origin.get("outputs") or []
            ins = target.get("inputs") or []
            if not (0 <= link["origin_slot"] < len(outs)) or not (0 <= link["target_slot"] < len(ins)):
                continue
            if (
                origin.get("type") == origin_type
                and outs[link["origin_slot"]].get("name") == origin_out
                and target.get("type") == target_type
                and ins[link["target_slot"]].get("name") == target_in
            ):
                found = True
                break
        if found:
            rep.ok(f"存在必需连线：{origin_type}.{origin_out} -> {target_type}.{target_in}")
        else:
            rep.fail(f"缺少必需连线：{origin_type}.{origin_out} -> {target_type}.{target_in}")

    # ---- 9. last_* 计数 ---------------------------------------------------
    rep.section("8. last_node_id / last_link_id")
    max_node_id = max(by_id) if by_id else 0
    last_node_id = doc.get("last_node_id")
    if not isinstance(last_node_id, int) or last_node_id < max_node_id:
        rep.fail(f"last_node_id={last_node_id!r} 小于最大节点 id {max_node_id}（新增节点会撞 id）")
    else:
        rep.ok(f"last_node_id={last_node_id} >= 最大节点 id {max_node_id}")
    max_link_id = max((int(l) for l in links), default=0)
    last_link_id = doc.get("last_link_id")
    if not isinstance(last_link_id, int) or last_link_id < max_link_id:
        rep.fail(f"last_link_id={last_link_id!r} 小于最大 link id {max_link_id}")
    else:
        rep.ok(f"last_link_id={last_link_id} >= 最大 link id {max_link_id}")

    return _finish(rep)


def _finish(rep: Report) -> int:
    print("\n" + "=" * 72)
    print(f"汇总：OK={rep.n_ok}  WARN={rep.n_warn}  FAIL={rep.n_fail}")
    if rep.fail_msgs:
        print("\n结论：FAIL —— 工作流未通过校验，ComfyUI 加载或执行时会出错：")
        for msg in rep.fail_msgs:
            print("  - " + msg.splitlines()[0])
        print("=" * 72)
        return 1
    print("\n结论：PASS —— 工作流结构与 bettercomfyui_comfy 的节点注册表一致。")
    print("（注意：PASS 只证明格式与注册表一致，不证明节点运行时不会报错。）")
    print("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 ComfyUI 工作流 JSON 与 bettercomfyui_comfy 节点注册表是否一致",
    )
    parser.add_argument("--workflow", default=str(DEFAULT_WORKFLOW), help="工作流 JSON 路径")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help="节点包名（默认 bettercomfyui_comfy）")
    args = parser.parse_args(argv)
    path = Path(args.workflow)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return validate(path, args.package)
    except Exception as exc:  # noqa: BLE001 - 校验器自身出错也必须可见
        print(f"\n[FAIL] 校验器自身异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
