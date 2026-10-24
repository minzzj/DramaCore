# -*- coding: utf-8 -*-
"""ComfyUI 运行时硬件适配（ROUND57）。

本模块是**插件侧**的适配层，和 `bettercomfyui/hardware.py`（纯检测/能力矩阵）分工如下：

    bettercomfyui.hardware        只做检测与口径（不碰 ComfyUI 进程状态）
    bettercomfyui_comfy.hardware_adapt  在 ComfyUI 进程里做运行时适配：
                                   * 懒加载硬件报告（不在 import 时探测）
                                   * 昇腾（Ascend NPU）设备保护补丁（幂等、可回滚、可关闭）
                                   * 检测 comfy-kitchen 的 INT8 NPU 分支状态
                                   * 给节点用的 preflight()
                                   * 给 HTTP 路由用的 route_payload()

**三条硬规则**

1. **不伪造**：补丁成功/已存在/缺失/需手动，全部逐项写入结果；没有真机验证的地方
   一律写「未真机验证」，不写成「已验证」。
2. **不改第三方源码**：comfy-kitchen 的 INT8 分支只**检测并报告**（`torch._int_mm`
   在 NPU 上不可用，需要 `npu_quant_matmul`），插件不代改别人的源码；
   能做的运行时保护（ComfyUI `is_nvidia()` / `supports_fp8_compute()`）才打补丁。
3. **绝不拖垮插件**：没装 ComfyUI、没装编排包、torch 异常、拿不到模型管理模块，
   统统收敛成 `available=False` + 原因，不抛异常。

补丁只做两件事（都是**包裹**，不改原函数实现）：
    comfy.model_management.is_nvidia()           -> NPU 活跃时返回 False
    comfy.model_management.supports_fp8_compute() -> NPU 活跃时返回 False

为什么需要：昇腾环境里 `torch_npu` 会提供 CUDA 兼容层，ComfyUI 的 `is_nvidia()`
可能误判为真，于是 FP8/`torch.cuda` 分支被走到 NPU 上，表现为报错、回退 CPU（极慢）
或文本编码器加载卡死。补丁是**设备保护**，不是「让 NPU 变成 CUDA」。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("bettercomfyui.comfy.hardware")

__all__ = [
    "hardware_report",
    "report_text",
    "preflight",
    "ensure_runtime_patches",
    "detect_comfy_kitchen",
    "route_payload",
    "reset_cache",
    "main",
]

#: 关闭整个硬件适配层（ComfyUI 启动时也不打印摘要）
ENV_DISABLE = "BETTERCOMFYUI_DISABLE_HARDWARE"
#: 只关昇腾补丁，仍保留检测/报告
ENV_SKIP_ASCEND_PATCHES = "BETTERCOMFYUI_NO_ASCEND_PATCHES"

#: 打在包裹函数上的标记 —— 幂等的唯一依据（重复调用不会层层包裹）
PATCH_MARKER = "_dramacore_npu_guard"
#: 保存原函数的属性（便于回滚/诊断，不参与运行）
PATCH_ORIGINAL = "_dramacore_original"

#: 昇腾启动参数（社区实测：文本编码器 mmap 卡住 / smart memory 行为异常）
ASCEND_LAUNCH_ARGS = ["--disable-mmap", "--disable-smart-memory"]

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {
    "hw_module": None,
    "info": None,
    "report": None,
    "report_error": None,
    "patches": None,
    "patch_attempts": 0,
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _now() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 - 时钟异常不影响主流程
        return ""


def hardware_disabled() -> bool:
    """是否被环境变量整体关闭（用户可 `BETTERCOMFYUI_DISABLE_HARDWARE=1` 退出适配）。"""
    return _truthy(os.environ.get(ENV_DISABLE))


def _ascend_patches_disabled() -> bool:
    return _truthy(os.environ.get(ENV_SKIP_ASCEND_PATCHES))


def _degraded(reason: str, *, disabled: bool = False) -> dict[str, Any]:
    """统一的降级报告：字段齐全，UI/节点不用分支判断。"""
    return {
        "available": False,
        "disabled": bool(disabled),
        "vendor": "unknown",
        "backend": "unknown",
        "device_name": "",
        "vram_mib": 0,
        "tier": "D",
        "tier_label": "D · 不可用于真实 H3 生成",
        "runtime_available": False,
        "full_h3_supported": False,
        "asset_generation_supported": False,
        "launch_args": [],
        "env": {},
        "patches_required": [],
        "warnings": [f"硬件适配不可用：{reason}"],
        "recommendations": [
            "确认已安装编排包 `bettercomfyui`，再运行 `python -m bettercomfyui.hardware doctor` 自检。"
        ],
        "summary": "硬件适配不可用（降级模式）",
        "report_error": reason,
        "source": "degraded",
        "checked_at": _now(),
    }


def _load_hardware_module() -> tuple[Any | None, str]:
    """导入 `bettercomfyui.hardware`；失败时借 `_bridge` 再定位一次。

    返回 `(module, error)`；成功时 error 为空串。**不抛异常**。
    """
    cached = _CACHE.get("hw_module")
    if cached is not None:
        return cached, ""
    try:
        module = importlib.import_module("bettercomfyui.hardware")
        _CACHE["hw_module"] = module
        return module, ""
    except Exception as first_exc:  # noqa: BLE001 - 编排包可能没装/路径不对
        first = f"{type(first_exc).__name__}: {first_exc}"
    try:
        # 插件自己的桥接层知道 orchestrator 可能在哪（site-packages / 仓库平级 / 环境变量）
        bridge = importlib.import_module(f"{__package__}._bridge") if __package__ else None
        if bridge is not None:
            bridge.load_orchestrator()
            module = importlib.import_module("bettercomfyui.hardware")
            _CACHE["hw_module"] = module
            return module, ""
    except Exception as second_exc:  # noqa: BLE001
        second = f"{type(second_exc).__name__}: {second_exc}"
        return None, f"首次导入失败（{first}）；经 _bridge 定位后仍失败（{second}）"
    return None, first


def _detect(force: bool = False) -> tuple[dict[str, Any] | None, Any | None, str]:
    """执行/复用一次硬件探测。返回 `(report_dict, HardwareInfo, error)`。"""
    if not force:
        cached = _CACHE.get("report")
        if cached is not None:
            return cached, _CACHE.get("info"), ""
    hw, error = _load_hardware_module()
    if hw is None:
        report = _degraded(error or "无法导入 bettercomfyui.hardware")
        _CACHE["report"] = report
        return report, None, report["report_error"]
    try:
        info = hw.detect_hardware()
        report = info.to_dict()
        report["summary"] = info.summary
        report["available"] = True
        report["source"] = f"bettercomfyui.hardware@{getattr(hw, '__file__', '')}"
        report["checked_at"] = _now()
        report.setdefault("report_error", None)
        _CACHE["info"] = info
        _CACHE["report"] = report
        return report, info, ""
    except Exception as exc:  # noqa: BLE001 - 探测异常也要给可用报告
        reason = f"硬件探测失败（{type(exc).__name__}: {exc}）"
        report = _degraded(reason)
        _CACHE["report"] = report
        return report, None, reason


# ---------------------------------------------------------------------------
# 昇腾补丁
# ---------------------------------------------------------------------------


def _npu_active() -> bool:
    """当前进程里 NPU 是否真的可用；任何异常按「不可用」处理（不猜）。"""
    try:
        import torch  # noqa: PLC0415 - 延迟导入

        npu = getattr(torch, "npu", None)
        return bool(npu is not None and npu.is_available())
    except Exception:  # noqa: BLE001
        return False


def _guard_is_nvidia(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def guard(*args: Any, **kwargs: Any) -> Any:
        # 只有 NPU 真实可用时才压制 CUDA 判定；普通 NVIDIA 机器行为完全不变。
        if _npu_active():
            return False
        return original(*args, **kwargs)

    return guard


def _guard_supports_fp8(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def guard(device: Any = None, *args: Any, **kwargs: Any) -> Any:
        if _npu_active():
            return False
        try:
            return original(device, *args, **kwargs)
        except TypeError:
            # 旧版 ComfyUI 的 supports_fp8_compute() 可能不接受参数
            return original()

    return guard


_GUARDS = {
    "is_nvidia": _guard_is_nvidia,
    "supports_fp8_compute": _guard_supports_fp8,
}


def _patch_one(mm: Any, name: str) -> dict[str, Any]:
    """给 `mm.<name>` 包一层设备保护。返回逐项状态（不抛异常）。"""
    factory = _GUARDS[name]
    try:
        target = getattr(mm, name, None)
    except Exception as exc:  # noqa: BLE001 - 属性访问本身可能炸（代理对象）
        return {"name": name, "status": "failed",
                "detail": f"读取 comfy.model_management.{name} 失败：{type(exc).__name__}: {exc}"}
    if target is None:
        return {
            "name": name,
            "status": "missing",
            "detail": (
                f"当前 ComfyUI 版本的 comfy.model_management 没有 `{name}`，"
                "无法自动加设备保护；需按文档手动确认设备分支（插件不猜）。"
            ),
        }
    if getattr(target, PATCH_MARKER, False):
        return {"name": name, "status": "already_applied",
                "detail": "已由本插件包裹过（幂等跳过，不会重复套娃）。"}
    if not callable(target):
        return {"name": name, "status": "missing",
                "detail": f"`{name}` 存在但不是可调用对象（{type(target).__name__}），跳过。"}
    try:
        guarded = factory(target)
        setattr(guarded, PATCH_MARKER, True)
        setattr(guarded, PATCH_ORIGINAL, target)
        setattr(mm, name, guarded)
        return {
            "name": name,
            "status": "applied",
            "detail": f"已包裹：NPU 活跃时 {name}() 返回 False；非 NPU 设备行为不变。",
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "status": "failed",
                "detail": f"包裹 {name} 失败：{type(exc).__name__}: {exc}"}


def detect_comfy_kitchen() -> dict[str, Any]:
    """检测 comfy-kitchen 是否安装、INT8 代码里是否已有 NPU 分支。

    **只读源码文本，不导入、不执行**：
    - 找 `torch._int_mm` -> 说明 INT8 走了 CUDA 专用算子（NPU 不支持）；
    - 找 `npu_quant_matmul` -> 说明已有昇腾分支；
    都没有/没装/读不到，都如实写状态，不假装可用。
    """
    result: dict[str, Any] = {
        "installed": False,
        "status": "not_installed",
        "origin": "",
        "uses_int_mm": False,
        "has_npu_branch": False,
        "scanned_files": 0,
        "detail": "",
        "int8_file": "",
        "npu_file": "",
    }
    try:
        spec = importlib.util.find_spec("comfy_kitchen")
    except Exception as exc:  # noqa: BLE001 - find_spec 可能因父包导入而抛
        result["status"] = "unknown"
        result["detail"] = f"查找 comfy_kitchen 失败：{type(exc).__name__}: {exc}"
        return result
    if spec is None or not spec.origin:
        result["detail"] = (
            "未安装 comfy-kitchen。若 H3 流程用 INT8 量化，需安装后确认其中已有 "
            "`npu_quant_matmul` 昇腾分支；否则 `torch._int_mm` 在 NPU 上会失败或回退 CPU。"
        )
        return result

    result["installed"] = True
    result["origin"] = str(spec.origin)
    root = Path(str(spec.origin)).resolve().parent
    files: list[Path] = []
    try:
        files = [p for p in root.rglob("*.py") if p.is_file()][:200]
    except Exception as exc:  # noqa: BLE001
        result["status"] = "unknown"
        result["detail"] = f"无法列出 comfy_kitchen 源码：{type(exc).__name__}: {exc}"
        return result

    for path in files:
        try:
            if path.stat().st_size > 512 * 1024:  # 单文件上限，避免读到大文件
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 单个文件读失败不影响其它
            continue
        result["scanned_files"] += 1
        if "torch._int_mm" in text or "_int_mm" in text:
            result["uses_int_mm"] = True
            if not result["int8_file"]:
                result["int8_file"] = str(path)
        if "npu_quant_matmul" in text:
            result["has_npu_branch"] = True
            result["npu_file"] = str(path)

    if result["has_npu_branch"]:
        result["status"] = "npu_branch_present"
        result["detail"] = (
            "comfy-kitchen 已安装，源码中出现 `npu_quant_matmul`，具备昇腾 INT8 分支的迹象；"
            "是否真的走通仍需真机运行确认（本插件未真机验证）。"
        )
    elif result["uses_int_mm"]:
        result["status"] = "needs_npu_branch"
        result["detail"] = (
            "comfy-kitchen 已安装，但只看到 `torch._int_mm`（CUDA/CPU 路径），"
            "没有 `npu_quant_matmul`。昇腾上 INT8 需补 NPU 分支，否则会报错或回退 CPU（极慢）。"
        )
    else:
        result["status"] = "unknown"
        result["detail"] = (
            "comfy-kitchen 已安装，但未在源码中匹配到 INT8 算子特征（可能版本结构不同）；"
            "请人工确认 INT8 路径。"
        )
    return result


def ensure_runtime_patches(*, report: dict[str, Any] | None = None,
                           force: bool = False) -> dict[str, Any]:
    """按硬件报告决定是否打昇腾补丁；幂等、可关闭、结果全量记录。

    返回结构（字段稳定，供日志/路由/节点共用）::

        {"vendor", "needed", "status", "npu_active", "items": [...],
         "comfy_kitchen": {...}, "manual_steps": [...], "reason", "checked_at",
         "attempts"}
    """
    if report is None:
        report, _info, _err = _detect(force=force)
    vendor = str(report.get("vendor") or "unknown")

    if hardware_disabled():
        return {
            "vendor": vendor, "needed": False, "status": "disabled",
            "npu_active": False, "items": [], "manual_steps": [],
            "comfy_kitchen": {"installed": False, "status": "not_checked",
                              "detail": "硬件适配已由环境变量关闭"},
            "reason": f"{ENV_DISABLE} 已设置：硬件适配整体关闭（可用 /bettercomfyui/doctor 查看插件状态）。",
            "checked_at": _now(), "attempts": _CACHE.get("patch_attempts", 0),
        }

    if vendor != "ascend":
        return {
            "vendor": vendor, "needed": False, "status": "not_required",
            "npu_active": _npu_active(), "items": [],
            "manual_steps": [], "comfy_kitchen": {"installed": False,
                                                  "status": "not_checked",
                                                  "detail": "非昇腾设备，无需 INT8 NPU 分支"},
            "reason": "当前硬件不是昇腾 NPU，无需设备保护补丁（NVIDIA/AMD/Intel/Apple 保持原样）。",
            "checked_at": _now(), "attempts": _CACHE.get("patch_attempts", 0),
        }

    if _ascend_patches_disabled():
        return {
            "vendor": vendor, "needed": True, "status": "disabled_by_env",
            "npu_active": _npu_active(), "items": [],
            "manual_steps": _ascend_manual_steps(),
            "comfy_kitchen": detect_comfy_kitchen(),
            "reason": f"{ENV_SKIP_ASCEND_PATCHES} 已设置：跳过自动补丁，按 manual_steps 手动处理。",
            "checked_at": _now(), "attempts": _CACHE.get("patch_attempts", 0),
        }

    with _LOCK:
        cached = _CACHE.get("patches")
        # 「deferred」表示当时还没进到 ComfyUI 进程/模块没加载好：下次调用应重试。
        if cached and not force and cached.get("status") not in ("deferred",):
            return cached
        _CACHE["patch_attempts"] = int(_CACHE.get("patch_attempts") or 0) + 1

        npu_active = _npu_active()
        items: list[dict[str, Any]] = []
        mm = None
        mm_error = ""
        try:
            mm = importlib.import_module("comfy.model_management")
        except Exception as exc:  # noqa: BLE001 - 不在 ComfyUI 进程里就会失败
            mm_error = f"{type(exc).__name__}: {exc}"

        if mm is None:
            status = "deferred"
            reason = (
                "无法导入 comfy.model_management（当前不在 ComfyUI 进程里，或加载顺序过早）。"
                "进入 ComfyUI 后再次调用 preflight()/访问 /bettercomfyui/hardware 会重试。"
            )
        else:
            items = [_patch_one(mm, name) for name in _GUARDS]
            bad = [i for i in items if i["status"] in ("missing", "failed")]
            applied = [i for i in items if i["status"] == "applied"]
            already = [i for i in items if i["status"] == "already_applied"]
            if bad:
                status = "partial"
                reason = "部分补丁未能自动生效，需按 manual_steps 手动确认。"
            elif applied:
                status = "applied"
                reason = "设备保护补丁已生效（NPU 活跃时压制 CUDA/FP8 判定）。"
            elif already:
                status = "already_applied"
                reason = "设备保护补丁此前已生效（幂等检查通过）。"
            else:  # 理论不可达，保守处理
                status = "unknown"
                reason = "补丁状态无法判定，请查看 items。"

        kitchen = detect_comfy_kitchen()
        manual_steps = _ascend_manual_steps()
        if status == "partial":
            manual_steps = manual_steps + [
                f"缺失的函数：{', '.join(i['name'] for i in items if i['status'] in ('missing', 'failed'))}",
            ]
        result = {
            "vendor": vendor,
            "needed": True,
            "status": status,
            "npu_active": npu_active,
            "comfy_ui_import_error": mm_error,
            "items": items,
            "comfy_kitchen": kitchen,
            "manual_steps": manual_steps,
            "reason": reason,
            "checked_at": _now(),
            "attempts": _CACHE["patch_attempts"],
        }
        _CACHE["patches"] = result
        return result


def _ascend_manual_steps() -> list[str]:
    return [
        "启动 ComfyUI 时加 `--disable-mmap --disable-smart-memory`"
        "（文本编码器在 mmap 下可能卡住、smart memory 行为异常）。",
        "确认 CANN 与 torch_npu 版本匹配（版本不匹配时 NPU 可能不可见或算子报错）。",
        "若自动补丁未生效：在 comfy/model_management.py 的 `is_nvidia()` / "
        "`supports_fp8_compute()` 里加设备判断，NPU 活跃时返回 False。",
        "comfy-kitchen 的 INT8 路径若只有 `torch._int_mm`，需补 `npu_quant_matmul` 分支"
        "（插件只检测，不代改第三方源码）。",
    ]


# ---------------------------------------------------------------------------
# 对外：报告 / 预检 / 路由载荷
# ---------------------------------------------------------------------------


def hardware_report(*, force: bool = False) -> dict[str, Any]:
    """懒加载 + 缓存的硬件报告（字段含 patches / comfy_kitchen / summary）。

    - 第一次调用才探测，import 本模块本身不探测；
    - 编排包不可用 → 降级报告（`available=False` + 原因），不抛；
    - `force=True` 强制重新探测（路由 `?refresh=1` 用）。
    """
    if hardware_disabled():
        return _degraded(f"{ENV_DISABLE} 已设置，硬件适配关闭", disabled=True)
    report, _info, _err = _detect(force=force)
    # patches 结果可能比报告更新（例如先探测、后进 ComfyUI），这里附带当前状态
    report = dict(report)
    report["patches"] = ensure_runtime_patches(report=report, force=force)
    report["comfy_kitchen"] = report["patches"].get("comfy_kitchen") or {}
    return report


def report_text(*, force: bool = False, report: dict[str, Any] | None = None) -> str:
    """中文人读报告；探测降级时返回降级说明（不抛）。

    ``report`` 可由调用方传入，避免同一次请求里重复探测。
    """
    report = report if report is not None else hardware_report(force=force)
    if report.get("available"):
        hw, _err = _load_hardware_module()
        info = _CACHE.get("info")
        if hw is not None and info is not None:
            try:
                text = hw.format_report(info)
            except Exception as exc:  # noqa: BLE001
                text = f"（报告格式化失败：{type(exc).__name__}: {exc}）\n{report.get('summary', '')}"
        else:
            text = str(report.get("summary") or "")
    else:
        text = ""
    patches = report.get("patches") or {}
    lines = [text, "", "运行时补丁（ComfyUI 进程内）"]
    lines.append(f"  状态：{patches.get('status')} · 原因：{patches.get('reason')}")
    items = patches.get("items") or []
    if items:
        lines.append("  逐项：")
        lines.extend(f"    - {i['name']}: {i['status']}（{i['detail']}）" for i in items)
    kitchen = patches.get("comfy_kitchen") or {}
    lines.append(f"  comfy-kitchen：{kitchen.get('status')}（{kitchen.get('detail')}）")
    if patches.get("status") in ("partial", "deferred", "disabled_by_env"):
        lines.append("  手动步骤：")
        lines.extend(f"    * {s}" for s in patches.get("manual_steps") or [])
    lines.append("")
    lines.append("说明：昇腾路径未做真机验证；本报告不代表对应硬件已通过验收。")
    return "\n".join(lines)


def _preflight_from_report(report: dict[str, Any],
                           patches: dict[str, Any]) -> dict[str, Any]:
    """把完整报告裁成节点需要的精简字段（纯函数，避免重复探测）。"""
    return {
        "available": bool(report.get("available")),
        "disabled": bool(report.get("disabled")),
        "vendor": report.get("vendor") or "unknown",
        "backend": report.get("backend") or "unknown",
        "device_name": report.get("device_name") or "",
        "vram_mib": int(report.get("vram_mib") or 0),
        "tier": report.get("tier") or "D",
        "tier_label": report.get("tier_label") or "D · 不可用于真实 H3 生成",
        "runtime_available": bool(report.get("runtime_available")),
        "full_h3_supported": bool(report.get("full_h3_supported")),
        "asset_generation_supported": bool(report.get("asset_generation_supported")),
        "launch_args": list(report.get("launch_args") or []),
        "env": dict(report.get("env") or {}),
        "warnings": list(report.get("warnings") or []),
        "patches_status": patches.get("status") or "unknown",
        "patches_reason": patches.get("reason") or "",
        "comfy_kitchen_status": (patches.get("comfy_kitchen") or {}).get("status") or "unknown",
        "error": report.get("report_error"),
        "summary": report.get("summary") or "",
        "checked_at": report.get("checked_at") or _now(),
    }


def preflight(*, force: bool = False) -> dict[str, Any]:
    """节点用预检：只返回决策需要的字段（不返回大段 evidence）。"""
    report = hardware_report(force=force)
    return _preflight_from_report(report, report.get("patches") or {})


def route_payload(*, force: bool = False) -> dict[str, Any]:
    """GET /bettercomfyui/hardware 的 JSON 载荷（路由处理器保持极薄）。

    报告只探测一次：文本报告与 preflight 都从同一份 report 派生，
    避免 ``?refresh=1`` 时重复探测导致前后字段不一致。
    """
    report = hardware_report(force=force)
    patches = report.get("patches") or {}
    try:
        text = report_text(report=report)
    except Exception as exc:  # noqa: BLE001 - 文本失败不影响 JSON
        text = f"（中文报告生成失败：{type(exc).__name__}: {exc}）"
    return {
        "ok": True,
        "generated_at": _now(),
        "available": bool(report.get("available")),
        "disabled": bool(report.get("disabled")),
        "report": report,
        "patches": patches,
        "preflight": _preflight_from_report(report, patches),
        "report_text": text,
        "notice": "昇腾/AMD Windows 等路径未做真机验证；能力分级见 docs/ROUND57_硬件适配指南.md。",
    }


def reset_cache() -> None:
    """清空缓存（测试/重新探测用）。不重置已打上的补丁。"""
    with _LOCK:
        for key in ("hw_module", "info", "report", "patches"):
            _CACHE[key] = None
        _CACHE["report_error"] = None
        _CACHE["patch_attempts"] = 0


# ---------------------------------------------------------------------------
# CLI：`python -m bettercomfyui_comfy.hardware_adapt [report|json|patches|preflight]`
# ---------------------------------------------------------------------------


def _configure_stdout() -> None:
    try:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    _configure_stdout()
    parser = argparse.ArgumentParser(
        prog="python -m bettercomfyui_comfy.hardware_adapt",
        description="剧核 DramaCore · ComfyUI 运行时硬件适配（检测 + 昇腾补丁状态）",
    )
    sub = parser.add_subparsers(dest="cmd")
    p_report = sub.add_parser("report", help="中文报告")
    p_report.add_argument("--refresh", action="store_true", help="强制重新探测")
    p_json = sub.add_parser("json", help="完整 JSON")
    p_json.add_argument("--refresh", action="store_true", help="强制重新探测")
    sub.add_parser("patches", help="只看补丁状态")
    sub.add_parser("preflight", help="节点预检字段")
    args = parser.parse_args(argv)

    if args.cmd in (None, "report"):
        print(report_text(force=bool(getattr(args, "refresh", False))))
    elif args.cmd == "json":
        print(json.dumps(hardware_report(force=bool(args.refresh)),
                         ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "patches":
        print(json.dumps(ensure_runtime_patches(), ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(preflight(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
