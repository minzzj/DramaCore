# -*- coding: utf-8 -*-
"""硬件检测 / 能力矩阵 / 多品牌适配建议（ROUND57）。

设计目标：
- **不假装支持**：检测到什么就报什么，支持级别、缺失依赖、推荐启动参数、需要打的补丁全部写清楚；
- **可测试**：``detect_hardware(probe=...)`` 允许注入探测结果，单测不需要真实 GPU/NPU；
- **可复用**：插件启动、HTTP 路由、安装器、CLI 都调用同一份能力矩阵。

支持矩阵（Tier）：
    A = 官方主路径（NVIDIA CUDA，当前项目默认验证路径）
    B = 可用但有条件（AMD ROCm Linux / Ascend NPU + 补丁 / 大显存）
    C = 有限支持（Intel XPU / Apple MPS / 低显存 / NPU 缺补丁）
    D = 不可用于真实 H3 生成（CPU / 无 torch / 缺依赖）

只有检测信息是"运行时事实"；Tier/建议是"项目口径"，两者分开返回，避免误读。
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# H3 真实生成的**最低**显存口径：与 `pipeline.core.H3_MIN_VRAM_MIB`（24_000 MiB）保持一致。
# 早前这里写成 48GB 并声称与 pipeline 一致 —— 是错的：pipeline 的门槛是 24GiB。
# 48GB 只是「舒适/长段」建议值，不能拿它当硬门槛，否则 40GB 的 A100 会被误判成 B。
H3_MIN_VRAM_MIB = 24_000
#: 舒适档：长段/高分辨率/多模型同时驻留的建议显存（不是硬门槛）。
H3_RECOMMENDED_VRAM_MIB = 48 * 1024

VENDOR_LABELS = {
    "nvidia": "NVIDIA（CUDA）",
    "amd": "AMD（ROCm / HIP）",
    "intel": "Intel（XPU / IPEX）",
    "apple": "Apple Silicon（MPS）",
    "ascend": "华为昇腾（Ascend NPU / CANN）",
    "cpu": "CPU（无加速卡）",
    "unknown": "未知",
}

TIER_LABELS = {
    "A": "A · 官方主路径（推荐）",
    "B": "B · 可用，但需要满足条件/补丁",
    "C": "C · 有限支持（部分流程可用）",
    "D": "D · 不可用于真实 H3 生成",
}


@dataclass
class HardwareInfo:
    """一次硬件探测的完整结果。"""

    platform: str = ""
    machine: str = ""
    python: str = ""
    torch_available: bool = False
    torch_version: str = ""
    runtime_available: bool = False
    vendor: str = "unknown"
    backend: str = "unknown"
    device_name: str = ""
    vram_mib: int = 0
    device_count: int = 0
    cuda_available: bool = False
    cuda_version: str = ""
    rocm_version: str = ""
    rocm_available: bool = False
    xpu_available: bool = False
    xpu_version: str = ""
    mps_available: bool = False
    npu_available: bool = False
    npu_version: str = ""
    tier: str = "D"
    tier_label: str = ""
    full_h3_supported: bool = False
    asset_generation_supported: bool = False
    placeholder_supported: bool = True
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    launch_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    patches_required: list[str] = field(default_factory=list)
    patches_optional: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @property
    def summary(self) -> str:
        name = self.device_name or VENDOR_LABELS.get(self.vendor, self.vendor)
        vram = f"{self.vram_mib / 1024:.0f}GB" if self.vram_mib else "显存未知"
        return f"{VENDOR_LABELS.get(self.vendor, self.vendor)} · {name} · {vram} · {self.tier_label or self.tier}"


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    """运行检测命令（失败返回空串，绝不抛异常）。"""
    exe = shutil.which(cmd[0])
    if not exe:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 - 命令由白名单拼接，无 shell
            [exe, *cmd[1:]], capture_output=True, text=True, timeout=timeout,
            errors="replace", check=False,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception:  # noqa: BLE001
        return ""


def _first_int(text: str) -> int:
    for token in str(text).replace(",", " ").split():
        token = token.strip()
        if token.isdigit():
            return int(token)
    return 0


def _first_gfx(lines: Any) -> str:
    """从 rocminfo 输出里取出 gfx 型号（如 gfx1100），取不到返回空串。"""
    for line in lines or []:
        lowered = str(line).lower()
        idx = lowered.find("gfx")
        if idx >= 0:
            token = str(line)[idx:].split()[0].strip(" ,;()")
            if token:
                return token
    return ""


def _probe_torch() -> dict[str, Any]:
    """探测 torch / 各加速后端；任何异常都收敛为 False/空值。"""
    info: dict[str, Any] = {
        "torch_available": False,
        "torch_version": "",
        "cuda_available": False,
        "cuda_version": "",
        "rocm_version": "",
        "rocm_available": False,
        "xpu_available": False,
        "xpu_version": "",
        "mps_available": False,
        "npu_available": False,
        "npu_version": "",
        "device_name": "",
        "vram_mib": 0,
        "device_count": 0,
    }
    try:
        import torch  # noqa: PLC0415 - 延迟导入：无 torch 环境也要能 import 本模块

        info["torch_available"] = True
        info["torch_version"] = str(getattr(torch, "__version__", "") or "")
        info["cuda_version"] = str(getattr(getattr(torch, "version", None), "cuda", "") or "")
        info["rocm_version"] = str(getattr(getattr(torch, "version", None), "hip", "") or "")
        # ROCm 版 torch 同样会让 torch.cuda.is_available() 为真（HIP 兼容层），
        # 因此必须单独记录 rocm_available，否则 AMD 会被误报成 NVIDIA。
        info["rocm_available"] = bool(info["rocm_version"])
        info["mps_available"] = bool(
            getattr(getattr(torch, "backends", None), "mps", None)
            and torch.backends.mps.is_available()
        )
        # 先探测 NPU：torch_npu 存在且可用时优先使用 NPU（与 ComfyUI 设备管理一致）
        try:
            import torch_npu  # noqa: F401,PLC0415 - 可选依赖

            npu = getattr(torch, "npu", None)
            if npu is not None and bool(npu.is_available()):
                info["npu_available"] = True
                info["npu_version"] = str(
                    getattr(torch_npu, "__version__", "") or getattr(npu, "__version__", "") or ""
                )
                info["device_count"] = int(npu.device_count() or 0)
                try:
                    props = npu.get_device_properties(0)
                    info["device_name"] = str(getattr(props, "name", "") or "Ascend NPU")
                    total = int(getattr(props, "total_memory", 0) or 0)
                    info["vram_mib"] = total // (1024 * 1024) if total > 1024 * 1024 else total
                except Exception:  # noqa: BLE001
                    info["device_name"] = "Ascend NPU"
        except Exception:  # noqa: BLE001
            pass
        try:
            # Ascend 环境里 torch_npu 会接管 cuda 兼容层，此时不能再按 NVIDIA 读显存，
            # 否则会把 NPU 误标成 CUDA GPU（ComfyUI 的 is_nvidia() 也是同一个坑）。
            if not info["npu_available"] and torch.cuda.is_available():
                info["cuda_available"] = True
                info["device_count"] = int(torch.cuda.device_count() or 0)
                props = torch.cuda.get_device_properties(0)
                info["device_name"] = str(getattr(props, "name", "") or "CUDA GPU")
                total = int(getattr(props, "total_memory", 0) or 0)
                info["vram_mib"] = total // (1024 * 1024) if total > 1024 * 1024 else total
        except Exception:  # noqa: BLE001
            pass
        try:
            xpu = getattr(torch, "xpu", None)
            if xpu is not None and bool(xpu.is_available()):
                info["xpu_available"] = True
                info["xpu_version"] = str(getattr(getattr(torch, "version", None), "xpu", "") or "")
                info["device_count"] = int(xpu.device_count() or 0)
                try:
                    props = xpu.get_device_properties(0)
                    info["device_name"] = str(getattr(props, "name", "") or info["device_name"] or "Intel XPU")
                    total = int(getattr(props, "total_memory", 0) or 0)
                    if total:
                        info["vram_mib"] = total // (1024 * 1024) if total > 1024 * 1024 else total
                except Exception:  # noqa: BLE001
                    info["device_name"] = info["device_name"] or "Intel XPU"
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        info["torch_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _probe_smi() -> dict[str, Any]:
    """命令行兜底探测（torch 不可用/设备未初始化时仍能给出品牌与显存）。"""
    evidence: dict[str, Any] = {}
    nvidia = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if nvidia.strip():
        evidence["nvidia_smi"] = nvidia.strip().splitlines()
    rocm = _run(["rocminfo"])
    if "gfx" in rocm.lower() or "amd" in rocm.lower():
        evidence["rocminfo"] = rocm.strip().splitlines()[:8]
    xpu = _run(["xpu-smi", "discovery"])
    if xpu.strip():
        evidence["xpu_smi"] = xpu.strip().splitlines()[:8]
    npu = _run(["npu-smi", "info"])
    if npu.strip():
        evidence["npu_smi"] = npu.strip().splitlines()[:12]
    # Linux 兜底：驱动/运行时没装时 rocminfo 也没有，lspci 仍能看出卡是谁家的
    lspci = _run(["lspci"])
    if lspci.strip():
        vga = [ln for ln in lspci.splitlines() if "vga" in ln.lower() or "display" in ln.lower()
               or "3d controller" in ln.lower()]
        if vga:
            evidence["lspci"] = vga[:6]
    # macOS 兜底：MPS 不可用（旧系统/无 torch）时仍能看出是不是 Apple Silicon
    if sys.platform == "darwin":
        displays = _run(["system_profiler", "SPDisplaysDataType"], timeout=6.0)
        if displays.strip():
            evidence["system_profiler"] = displays.strip().splitlines()[:12]
    return evidence


def _detect_vendor(info: dict[str, Any]) -> tuple[str, str]:
    if info.get("npu_available"):
        return "ascend", "npu"
    # AMD 的 ROCm 版 torch 走 HIP，`torch.version.hip` 有值；rocminfo/lspci 兜底时
    # rocm_available 也可能为真 —— 两种都要判成 AMD，不能落进 NVIDIA 分支。
    if info.get("rocm_available") or info.get("rocm_version"):
        return "amd", "rocm"
    if info.get("cuda_available"):
        return "nvidia", "cuda"
    if info.get("xpu_available"):
        return "intel", "xpu"
    if info.get("mps_available"):
        return "apple", "mps"
    return "cpu", "cpu"


def _os_name(plat: str) -> str:
    """把 `platform.platform()` 归一成 linux / windows / darwin（Tier 判定要用 OS，不是 CPU 架构）。"""
    lowered = str(plat or "").lower()
    if "windows" in lowered:
        return "windows"
    if "darwin" in lowered or "macos" in lowered or "mac os" in lowered:
        return "darwin"
    if "linux" in lowered:
        return "linux"
    return str(sys.platform or "").lower()


def _classify(info: dict[str, Any], os_name: str,
              runtime_available: bool) -> tuple[str, bool, bool]:
    """返回 (tier, full_h3_supported, asset_generation_supported)。

    门槛用 `H3_MIN_VRAM_MIB`（24GiB，与 pipeline.core 同一口径）；
    48GB 只是舒适档建议，不参与 Tier 判定。

    **运行时优先**：即使 nvidia-smi/lspci 看到了卡，只要当前 PyTorch 没有对应后端
    （CPU 版 torch、驱动未装、CUDA 不可用），就不能报 A/B 全流程可用——
    否则会误导用户去跑必然失败的 H3。这种情况统一降为 C 且 full=False。
    """
    vendor = info["vendor"]
    vram = int(info.get("vram_mib") or 0)
    enough_vram = vram >= H3_MIN_VRAM_MIB or (vendor == "apple" and vram >= 24 * 1024)
    if not runtime_available:
        # CPU / 无 torch / 检测到卡但 torch 后端不可用
        return "D" if vendor == "cpu" else "C", False, False
    if vendor == "nvidia":
        return ("A" if enough_vram else "B"), enough_vram, True
    if vendor == "ascend":
        # NPU 主路径需要 torch_npu/CANN；H3 还需要 ComfyUI 设备保护 + comfy-kitchen INT8 补丁。
        return ("B" if enough_vram else "C"), enough_vram, enough_vram
    if vendor == "amd":
        linux = str(os_name).lower() == "linux"
        return ("B" if (linux and enough_vram) else "C"), bool(linux and enough_vram), True
    if vendor == "intel":
        return "C", bool(enough_vram), True
    if vendor == "apple":
        # 统一内存不等于可跑 H3 全流程；只承诺轻流程/素材生成的可能性。
        return "C", False, True
    return "D", False, False


def _recommendations(vendor: str, tier: str) -> tuple[list[str], list[str], dict[str, str],
                                                       list[str], list[str]]:
    launch_args: list[str] = []
    env: dict[str, str] = {}
    warnings: list[str] = []
    required: list[str] = []
    optional: list[str] = []
    if vendor == "ascend":
        # 社区在 Ascend 910B3 + ComfyUI 0.30.2 + MiniMax-H3 INT8 上的关键参数：
        # - 文本编码器加载在 mmap 下卡住 → --disable-mmap
        # - 显存/内存行为异常 → --disable-smart-memory
        launch_args = ["--disable-mmap", "--disable-smart-memory"]
        env["PYTORCH_NPU_ALLOC_CONF"] = "expandable_segments:True"
        required = [
            "ComfyUI is_nvidia()/supports_fp8_compute() 设备保护（防止 NPU 误走 torch.cuda）",
            "comfy-kitchen INT8 NPU 分支（torch._int_mm 在 NPU 不支持，需 npu_quant_matmul）",
        ]
        optional = ["ASCEND_RT_VISIBLE_DEVICES 指定可见卡", "CANN 与 torch_npu 版本匹配检查"]
        warnings.append(
            "昇腾为有条件支持：必须安装匹配的 CANN + torch_npu，并确认上面的两个补丁生效；"
            "未打补丁时可能出现文本编码器加载卡死或 INT8 回退 CPU（极慢）。"
        )
    elif vendor == "amd":
        env.setdefault("PYTORCH_HIP_ALLOC_CONF", "expandable_segments:True")
        required = ["ROCm 版 PyTorch（Linux 官方支持；Windows 实验性）"]
        optional = ["HSA_OVERRIDE_GFX_VERSION（仅当 ROCm 不原生支持该 gfx 型号时）"]
        if tier != "B":
            warnings.append("AMD 建议 Linux + ROCm 7.x；Windows 为实验性支持，H3 大模型可能失败或极慢。")
    elif vendor == "intel":
        env.setdefault("SYCL_CACHE_PERSISTENT", "1")
        required = ["Intel XPU 版 PyTorch / IPEX（ComfyUI 官方支持）"]
        warnings.append("Intel Arc 显存通常不足以跑 H3 全流程；可优先用于素材生成/轻流程。")
    elif vendor == "apple":
        launch_args = ["--force-fp16"]
        warnings.append("Apple MPS 统一内存与性能有限：不要承诺 H3 48GB 级全流程，可做轻量流程/素材生成。")
    elif vendor == "cpu":
        launch_args = ["--cpu"]
        warnings.append("CPU 只能跑编排/审核/剪辑/占位；真实 H3 生成不可用（会非常慢或直接失败）。")
    if tier in ("C", "D"):
        warnings.append("当前硬件不在 H3 全流程的保证范围内；节点会如实审计，不会冒充成功。")
    return launch_args, warnings, env, required, optional


def detect_hardware(probe: dict[str, Any] | None = None) -> HardwareInfo:
    """检测硬件并生成能力矩阵。

    ``probe`` 仅供测试注入（键名与内部探测结果一致）；正式运行传 None。
    """
    if probe is None:
        probe = {"torch": _probe_torch(), "smi": _probe_smi()}
    torch_info = dict(probe.get("torch") or {})
    smi = dict(probe.get("smi") or {})
    plat = probe.get("platform") or platform.platform()
    machine = str(probe.get("machine") or platform.machine())
    py = str(probe.get("python") or sys.version.split()[0])

    info = HardwareInfo(platform=plat, machine=machine, python=py)
    info.evidence = {"torch": torch_info, "smi": smi}
    info.torch_available = bool(torch_info.get("torch_available"))
    info.torch_version = str(torch_info.get("torch_version") or "")
    info.cuda_available = bool(torch_info.get("cuda_available"))
    info.cuda_version = str(torch_info.get("cuda_version") or "")
    info.rocm_version = str(torch_info.get("rocm_version") or "")
    info.rocm_available = bool(torch_info.get("rocm_available") or info.rocm_version)
    info.xpu_available = bool(torch_info.get("xpu_available"))
    info.xpu_version = str(torch_info.get("xpu_version") or "")
    info.mps_available = bool(torch_info.get("mps_available"))
    info.npu_available = bool(torch_info.get("npu_available"))
    info.npu_version = str(torch_info.get("npu_version") or "")
    info.device_name = str(torch_info.get("device_name") or "")
    info.vram_mib = int(torch_info.get("vram_mib") or 0)
    info.device_count = int(torch_info.get("device_count") or 0)

    # 命令行兜底：torch 没拿到信息时从 nvidia-smi / rocminfo / xpu-smi / npu-smi / lspci 推断。
    # 只是「看到硬件」，不等于运行时可用；报告里的 torch 警告与 Tier 会同时体现。
    if not info.torch_available or not (info.cuda_available or info.npu_available
                                        or info.xpu_available or info.mps_available
                                        or info.rocm_available):
        smi_text = " ".join(str(v) for v in smi.values()).lower()
        if smi.get("npu_smi"):
            info.npu_available = info.npu_available or True
            info.device_name = info.device_name or "Ascend NPU（npu-smi 检出，torch_npu 未就绪）"
        if smi.get("nvidia_smi"):
            info.cuda_available = info.cuda_available or True
            if not info.device_name:
                line = str(smi["nvidia_smi"][0])
                info.device_name = line.split(",")[0].strip()
                if not info.vram_mib:
                    tail = line.split(",")[-1].replace("MiB", "").strip()
                    info.vram_mib = _first_int(tail)
        elif smi.get("rocminfo") and ("amd" in smi_text or "gfx" in smi_text):
            info.rocm_available = True
            info.rocm_version = info.rocm_version or "rocm（rocminfo 检出）"
            info.device_name = info.device_name or _first_gfx(smi["rocminfo"]) or "AMD GPU（rocminfo 检出）"
        elif smi.get("xpu_smi"):
            info.xpu_available = info.xpu_available or True
            info.device_name = info.device_name or "Intel XPU（xpu-smi 检出）"
        elif smi.get("lspci"):
            # 连 rocminfo/xpu-smi 都没有时，lspci 至少能告诉我们卡是谁家的（驱动没装也能看）
            hint = " ".join(str(x) for x in smi["lspci"]).lower()
            if "nvidia" in hint:
                info.cuda_available = info.cuda_available or True
                info.device_name = info.device_name or "NVIDIA GPU（lspci 检出，驱动/运行时未就绪）"
            elif "advanced micro devices" in hint or "amd/ati" in hint:
                info.rocm_available = True
                info.rocm_version = info.rocm_version or "rocm（lspci 检出，运行时未就绪）"
                info.device_name = info.device_name or "AMD GPU（lspci 检出，驱动/运行时未就绪）"
            elif "intel" in hint:
                info.xpu_available = info.xpu_available or True
                info.device_name = info.device_name or "Intel GPU（lspci 检出，运行时未就绪）"
        elif smi.get("system_profiler") or (
                info.torch_available and getattr(sys, "platform", "").startswith("darwin")):
            info.mps_available = info.mps_available or True
            info.device_name = info.device_name or "Apple GPU（MPS 候选）"

    info.vendor, info.backend = _detect_vendor({
        "npu_available": info.npu_available, "cuda_available": info.cuda_available,
        "rocm_version": info.rocm_version, "rocm_available": info.rocm_available,
        "xpu_available": info.xpu_available, "mps_available": info.mps_available,
    })
    # 运行时可用性：必须来自 torch 的真实能力，而不是 smi/lspci 的“看到了卡”
    runtime = False
    if info.torch_available:
        if info.vendor == "nvidia":
            runtime = bool(torch_info.get("cuda_available")) and not bool(torch_info.get("rocm_version"))
        elif info.vendor == "amd":
            runtime = bool(torch_info.get("cuda_available")) and bool(
                torch_info.get("rocm_version") or info.rocm_available)
        elif info.vendor == "ascend":
            runtime = bool(torch_info.get("npu_available"))
        elif info.vendor == "intel":
            runtime = bool(torch_info.get("xpu_available"))
        elif info.vendor == "apple":
            runtime = bool(torch_info.get("mps_available"))
    info.runtime_available = runtime
    info.tier, info.full_h3_supported, info.asset_generation_supported = _classify(
        info.to_dict(), _os_name(plat), runtime)
    info.tier_label = TIER_LABELS[info.tier]
    info.placeholder_supported = True
    launch, warns, env, required, optional = _recommendations(info.vendor, info.tier)
    info.launch_args = launch
    info.warnings.extend(warns)
    info.env = env
    info.patches_required = required
    info.patches_optional = optional

    # 通用检查
    if not info.torch_available:
        info.warnings.append("未检测到 PyTorch：请先按安装器/ComfyUI 要求安装对应后端的 torch。")
    if info.vendor not in ("cpu", "unknown") and not info.runtime_available:
        info.warnings.append(
            f"检测到 {VENDOR_LABELS.get(info.vendor, info.vendor)} 硬件，但当前 PyTorch "
            f"运行时不可用（torch_available={info.torch_available}, "
            f"cuda_available={info.cuda_available}, rocm={info.rocm_version or '-'}, "
            f"npu={info.npu_available}, xpu={info.xpu_available}, mps={info.mps_available}）："
            "本机现在不能真实生成 H3，只是“看到了硬件”；请安装对应后端的 torch/驱动后再跑。")
    elif info.vendor == "cpu" and info.torch_available:
        info.warnings.append("PyTorch 是 CPU 版：无加速设备，H3 真实生成不可用（仅编排/UI/占位）。")
    if info.vendor == "unknown":
        info.warnings.append("无法确定显卡厂商；请运行 `python -m bettercomfyui.hardware doctor --json` 并附日志排查。")
    if info.vendor == "ascend" and not info.npu_version:
        info.warnings.append("检测到 NPU 但未拿到 torch_npu 版本：请确认 torch_npu 与 torch/CANN 版本匹配。")
    if info.vendor == "nvidia" and info.vram_mib and info.vram_mib < H3_MIN_VRAM_MIB:
        info.warnings.append(
            f"显存 {info.vram_mib / 1024:.0f}GB 低于 H3 真实生成的 {H3_MIN_VRAM_MIB // 1000}GB 下限；"
            "可能需要更低分辨率/分段/tiled 或换卡（舒适档建议 48GB）。")
    elif info.vendor == "nvidia" and H3_MIN_VRAM_MIB <= info.vram_mib < H3_RECOMMENDED_VRAM_MIB:
        info.warnings.append(
            f"显存 {info.vram_mib / 1024:.0f}GB 达到 H3 下限（{H3_MIN_VRAM_MIB // 1000}GB），"
            "但低于长段/高分辨率舒适档 48GB；先按短段验证。")
    # 推荐项（面向用户的操作建议）
    if info.vendor == "nvidia" and info.tier == "A":
        info.recommendations.append("使用 CUDA 版 PyTorch（ComfyUI 官方推荐 cu126/cu130 wheel），默认路径即可。")
    if info.vendor not in ("cpu", "unknown") and not info.runtime_available:
        info.recommendations.append(
            "当前只是检测到硬件，运行时还不可用：请按安装器安装对应后端的 PyTorch"
            "（NVIDIA CUDA / AMD ROCm / Intel XPU / 昇腾 torch_npu），或检查驱动后重跑自检。")
    if info.vendor == "ascend":
        info.recommendations.extend([
            "安装 CANN + 匹配的 torch/torch_npu（参考昇腾官方 ComfyUI 文档），启动加 --disable-mmap --disable-smart-memory。",
            "确认 ComfyUI 设备判断与 comfy-kitchen INT8 补丁已生效（插件启动日志会报告）。",
            "先用小分辨率/短段跑通，再逐步加长；保留日志便于定位算子回退。",
        ])
    if info.vendor == "amd":
        info.recommendations.append("优先 Linux + ROCm 7.x；安装 ROCm wheel 版 PyTorch，Windows 仅实验性尝试。")
    if info.vendor == "intel":
        info.recommendations.append("安装 XPU 版 PyTorch/IPEX；只承诺素材/轻流程，H3 全流程按实测为准。")
    if info.vendor == "apple":
        info.recommendations.append("使用 MPS 默认路径；H3 大模型不建议期待，优先素材/轻流程。")
    if info.vendor == "cpu":
        info.recommendations.append("使用占位结果跑通编排/UI/剪辑；真实生成请换 NVIDIA/AMD/昇腾等加速卡。")
    info.recommendations.append(
        "统一自检：`python -m bettercomfyui.hardware doctor --json`（把输出发给维护者即可定位）。")
    return info


def format_report(info: HardwareInfo) -> str:
    """中文人读报告（UI/CLI/文档共用）。"""
    lines = [
        "剧核 DramaCore · 硬件适配报告",
        "=" * 36,
        f"平台      : {info.platform} ({info.machine})",
        f"Python    : {info.python}",
        f"PyTorch   : {info.torch_version or '未安装'}",
        f"运行时    : {'可用' if info.runtime_available else '不可用（只检测到硬件，不能真实生成 H3）'}",
        f"品牌/后端 : {VENDOR_LABELS.get(info.vendor, info.vendor)} / {info.backend}",
        f"设备       : {info.device_name or '未识别'}",
        f"显存       : {info.vram_mib // 1024 if info.vram_mib else 0} GB"
        f"（{info.vram_mib} MiB） · 设备数 {info.device_count}",
        f"驱动/版本 : CUDA={info.cuda_version or '-'} ROCm={info.rocm_version or '-'} "
        f"XPU={info.xpu_version or '-'} NPU={info.npu_version or '-'} MPS={info.mps_available}",
        f"支持级别   : {info.tier_label}",
        f"H3 全流程 : {'支持' if info.full_h3_supported else '不保证/不可用'}",
        f"素材生成   : {'支持' if info.asset_generation_supported else '不保证'}",
        f"占位/编排  : {'支持' if info.placeholder_supported else '不支持'}",
    ]
    if info.launch_args:
        lines.append("推荐启动参数: " + " ".join(info.launch_args))
    if info.env:
        lines.append("推荐环境变量: " + "  ".join(f"{k}={v}" for k, v in info.env.items()))
    if info.patches_required:
        lines.append("必需补丁/条件:")
        lines.extend(f"  - {x}" for x in info.patches_required)
    if info.patches_optional:
        lines.append("可选优化:")
        lines.extend(f"  - {x}" for x in info.patches_optional)
    if info.warnings:
        lines.append("警告:")
        # 不用 ⚠ 等非 GBK 字符：Windows 默认 cp936 控制台会 UnicodeEncodeError 直接崩掉。
        lines.extend(f"  ! {x}" for x in info.warnings)
    if info.recommendations:
        lines.append("建议:")
        lines.extend(f"  - {x}" for x in info.recommendations)
    lines.append("")
    lines.append("说明：本报告是运行时检测 + 项目能力口径，不代表对应硬件已通过真机验收。")
    return "\n".join(lines)


def format_markdown(info: HardwareInfo) -> str:
    """给 docs/Issue 用的 Markdown 片段。"""
    rows = [
        ("平台", f"{info.platform} ({info.machine})"),
        ("Python", info.python),
        ("PyTorch", info.torch_version or "未安装"),
        ("品牌/后端", f"{VENDOR_LABELS.get(info.vendor, info.vendor)} / {info.backend}"),
        ("设备", info.device_name or "未识别"),
        ("显存", f"{info.vram_mib // 1024 if info.vram_mib else 0} GB"),
        ("支持级别", info.tier_label),
        ("H3 全流程", "支持/有条件" if info.full_h3_supported else "不保证"),
        ("素材生成", "支持/有条件" if info.asset_generation_supported else "不保证"),
    ]
    out = ["| 项 | 值 |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    if info.launch_args:
        out.append(f"| 推荐启动参数 | `{' '.join(info.launch_args)}` |")
    if info.patches_required:
        out.append("| 必需补丁 | " + "；".join(info.patches_required) + " |")
    return "\n".join(out)


def _configure_stdout() -> None:
    """Windows 控制台默认 GBK，打印中文/符号可能 UnicodeEncodeError；这里退化为替换而不是崩。"""
    try:
        stream = sys.stdout
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - 输出流不可写时忽略
        pass


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    _configure_stdout()
    parser = argparse.ArgumentParser(
        prog="python -m bettercomfyui.hardware",
        description="剧核 DramaCore 硬件检测 / 多品牌适配建议",
    )
    sub = parser.add_subparsers(dest="cmd")
    doc = sub.add_parser("doctor", help="检测硬件并输出适配报告")
    doc.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    doc.add_argument("--markdown", action="store_true", help="输出 Markdown 表格")
    sub.add_parser("matrix", help="打印支持矩阵")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "matrix":
        print("Tier A: NVIDIA CUDA（官方主路径）")
        print("Tier B: AMD ROCm Linux / 大显存 / Ascend NPU（需补丁，未真机验证）")
        print("Tier C: Intel XPU / Apple MPS / 低显存（有限支持）")
        print("Tier D: CPU / 无 torch（不可用于真实 H3 生成）")
        return 0
    info = detect_hardware()
    if args.json:
        print(info.to_json())
    elif args.markdown:
        print(format_markdown(info))
    else:
        print(format_report(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
