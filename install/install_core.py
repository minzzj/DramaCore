# -*- coding: utf-8 -*-
"""剧核 DramaCore · 一键安装/部署器（ROUND57）。

两种模式
--------
* ``attach``：装到**已有** ComfyUI（``--comfyui PATH``）。会按后端装 torch、
  装 ComfyUI requirements、把插件（和编排包）放进 ``custom_nodes/``、写启动器。
* ``bootstrap``：从零开始 —— ``git clone`` ComfyUI、建 venv、装 torch/依赖、
  装插件、写启动器。

设计约束（全部有对应实现/测试）
------------------------------
* **不做假动作**：``--dry-run`` 只打印计划，不建目录、不跑 pip、不联网；
* **不静默下载模型**：只打印模型目录指引；权重由用户自己放；
* **不覆盖已有安装**：目标已存在时默认跳过并说明，只有 ``--force`` 才先备份再覆盖；
* **失败可回滚**：clone/venv/复制/写启动器都有 undo；pip 安装**不**自动卸载
  （会明确说明，避免把用户环境搅乱）；
* **跨平台**：Windows / Linux / macOS 通用；昇腾给出 CANN/torch_npu 指引与启动参数；
* **能力分级复用** ``bettercomfyui.hardware``（若可导入），避免两套口径。

退出码：0 成功 / 2 参数或确认问题 / 3 环境不满足 / 4 执行失败（已回滚）。

示例::

    python install/install_core.py --mode attach --comfyui D:/ComfyUI --backend auto --dry-run
    python install/install_core.py --mode bootstrap --comfyui D:/ComfyUI --backend cuda --yes
    python install/install_core.py --mode attach --comfyui ./.comfyui --backend ascend --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIRNAME = "bettercomfyui_comfy"
ORCH_DIRNAME = "bettercomfyui"
DEFAULT_COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI.git"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_FAILED = 4

BACKENDS = ("auto", "cuda", "rocm", "xpu", "mps", "ascend", "cpu")

#: 后端 -> PyTorch 安装源（2026-10 实测这三个索引可访问；见 docs/ROUND57_安装部署指南.md）
TORCH_SOURCES: dict[str, dict[str, str]] = {
    "cuda": {
        "index": "https://download.pytorch.org/whl/cu126",
        "label": "CUDA 12.6（cu126）",
        "note": "默认 CUDA 主路径；新卡也可用 --torch-index 指定 cu128/cu130 等索引。",
    },
    "rocm": {
        "index": "https://download.pytorch.org/whl/rocm7.1",
        "label": "ROCm 7.1（Linux）",
        "note": "官方 ROCm wheel 只有 Linux；Windows 为实验性，建议 WSL2。",
    },
    "xpu": {
        "index": "https://download.pytorch.org/whl/xpu",
        "label": "Intel XPU",
        "note": "需要较新的 Intel 驱动与 Python 3.10+。",
    },
    "mps": {"index": "", "label": "PyPI 默认（Apple MPS）", "note": "macOS 用默认 wheel 即可。"},
    "ascend": {
        "index": "",
        "label": "CANN + torch_npu（不使用 PyPI torch）",
        "note": "必须先装 CANN 与匹配的 torch/torch_npu；安装器只检查、不代装。",
    },
    "cpu": {"index": "", "label": "PyPI 默认（CPU）", "note": "只能编排/占位，真实 H3 生成不可用。"},
}

#: 模型目录指引（**不下载**，只打印）
MODEL_DIRS = (
    ("diffusion_models", "H3 扩散模型权重"),
    ("text_encoders", "文本编码器（H3 为 34GB 级，务必留足磁盘）"),
    ("vae", "VAE"),
    ("loras", "LoRA（可选）"),
    ("checkpoints", "旧式单文件 checkpoint（可选）"),
)

#: 启动器里统一注入的环境变量（插件用它定位编排包）
ENV_ORCH_HOME = "BETTERCOMFYUI_HOME"


class InstallError(RuntimeError):
    """可预期的安装错误（带修复指引，不打印 traceback 吓用户）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _configure_stdout() -> None:
    """Windows 控制台默认 GBK：中文/符号打印不应让安装器崩掉。"""
    try:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _say(message: str = "") -> None:
    print(message, flush=True)


def _warn(message: str) -> None:
    _say(f"[警告] {message}")


def _os_name() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    """运行探测命令；找不到可执行文件或失败返回空串（绝不抛）。"""
    exe = shutil.which(cmd[0])
    if not exe:
        return ""
    try:
        proc = subprocess.run(  # noqa: S603 - 参数列表，无 shell
            [exe, *cmd[1:]], capture_output=True, text=True,
            timeout=timeout, errors="replace", check=False,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception:  # noqa: BLE001
        return ""


def _first_int(text: str) -> int:
    for token in str(text).replace(",", " ").split():
        token = token.strip()
        if token.isdigit():
            return int(token)
    return 0


def parse_npu_hbm_mib(text: str) -> int:
    """从 `npu-smi info` 里解析 HBM 总量（MiB）。

    典型输出里有 ``0 / 65536`` 这样的 HBM-Usage 字段；取所有 ``a / b`` 的最大 b。
    解析不到返回 0（不猜）。
    """
    best = 0
    for match in re.finditer(r"(\d+)\s*/\s*(\d+)", str(text or "")):
        total = int(match.group(2))
        if total > best:
            best = total
    return best


def normalize_backend(value: str | None) -> str:
    """别名归一：nvidia/hip/intel/apple/npu 等 -> 标准后端名。返回空串表示不认识。"""
    raw = str(value or "").strip().lower()
    aliases = {
        "auto": "auto", "cuda": "cuda", "nvidia": "cuda",
        "rocm": "rocm", "hip": "rocm", "amd": "rocm",
        "xpu": "xpu", "intel": "xpu", "ipex": "xpu",
        "mps": "mps", "apple": "mps", "metal": "mps",
        "ascend": "ascend", "npu": "ascend", "cann": "ascend",
        "cpu": "cpu",
    }
    return aliases.get(raw, "")


def _venv_python(comfyui: Path) -> Path:
    if _os_name() == "windows":
        return comfyui / "venv" / "Scripts" / "python.exe"
    return comfyui / "venv" / "bin" / "python"


def find_comfy_python(comfyui: Path) -> Path:
    """优先用 ComfyUI 自带 venv 的 Python；没有就退回当前解释器。"""
    for candidate in (
        _venv_python(comfyui),
        comfyui / ".venv" / "Scripts" / "python.exe",
        comfyui / ".venv" / "bin" / "python",
        comfyui / "python_embeded" / "python.exe",   # Windows 便携版
    ):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def default_comfyui() -> str:
    """按环境变量/常见位置猜 ComfyUI 目录（猜不到返回空串，调用方必须报错）。"""
    for env_name in ("COMFYUI_PATH", "COMFYUI_DIR"):
        env_value = os.environ.get(env_name)
        if env_value and (Path(env_value) / "main.py").is_file():
            return str(Path(env_value))
    cwd = Path.cwd()
    for base in (REPO_ROOT, cwd, cwd.parent):
        for name in (".comfyui", "ComfyUI", "comfyui"):
            candidate = base / name
            if (candidate / "main.py").is_file():
                return str(candidate)
    return ""


# ---------------------------------------------------------------------------
# 硬件探测
# ---------------------------------------------------------------------------


def detect_backend() -> dict[str, Any]:
    """命令行探测 GPU/NPU：nvidia-smi / rocminfo / xpu-smi / npu-smi / lspci / system_profiler。

    返回 ``{"backend", "device", "vram_mib", "evidence", "warnings"}``；
    探测不到任何加速卡时返回 ``cpu``（不假装）。
    """
    evidence: dict[str, Any] = {}
    warnings: list[str] = []
    device = ""
    vram_mib = 0
    backend = "cpu"

    nvidia = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if nvidia.strip():
        evidence["nvidia_smi"] = nvidia.strip().splitlines()[:4]
        first = nvidia.strip().splitlines()[0]
        device = first.split(",")[0].strip()
        vram_mib = _first_int(first.split(",")[-1].replace("MiB", "").strip())
        backend = "cuda"

    npu = _run(["npu-smi", "info"])
    if npu.strip():
        evidence["npu_smi"] = npu.strip().splitlines()[:12]
        if backend == "cpu":
            backend = "ascend"
            device = device or "Ascend NPU（npu-smi 检出）"
            vram_mib = parse_npu_hbm_mib(npu)

    rocm = _run(["rocminfo"])
    if rocm.strip() and backend == "cpu" and ("gfx" in rocm.lower() or "amd" in rocm.lower()):
        evidence["rocminfo"] = rocm.strip().splitlines()[:8]
        backend = "rocm"
        device = device or "AMD GPU（rocminfo 检出）"
    elif "gfx" in rocm.lower() and backend == "cuda":
        # 同一台机器同时有 NVIDIA 和 AMD：默认 CUDA 主路径，但记录证据
        evidence["rocminfo"] = rocm.strip().splitlines()[:4]

    xpu = _run(["xpu-smi", "discovery"])
    if xpu.strip() and backend == "cpu":
        evidence["xpu_smi"] = xpu.strip().splitlines()[:8]
        backend = "xpu"
        device = device or "Intel XPU（xpu-smi 检出）"

    if backend == "cpu" and sys.platform == "darwin" and platform.machine() == "arm64":
        screens = _run(["system_profiler", "SPDisplaysDataType"], timeout=8.0)
        if screens.strip():
            evidence["system_profiler"] = screens.strip().splitlines()[:12]
        backend = "mps"
        device = device or "Apple Silicon（MPS）"

    if backend == "cpu":
        lspci = _run(["lspci"])
        vga = [ln for ln in lspci.splitlines()
               if "vga" in ln.lower() or "display" in ln.lower() or "3d controller" in ln.lower()]
        if vga:
            evidence["lspci"] = vga[:6]
            hint = " ".join(vga).lower()
            if "nvidia" in hint:
                backend = "cuda"
                warnings.append("lspci 检出 NVIDIA 卡，但 nvidia-smi 不可用：驱动可能未安装，请先装驱动。")
            elif "advanced micro devices" in hint or "amd/ati" in hint:
                backend = "rocm"
                warnings.append("lspci 检出 AMD 卡，但 rocminfo 不可用：请先安装 ROCm 驱动（Linux）。")
            elif "intel" in hint:
                backend = "xpu"
                warnings.append("lspci 检出 Intel 显卡，但 xpu-smi 不可用：请先安装 XPU 驱动。")

    if backend == "cpu":
        warnings.append("未检测到 CUDA/ROCm/XPU/NPU/MPS 加速卡，按 CPU 处理（真实 H3 生成不可用）。")
    if backend == "rocm" and _os_name() == "windows":
        warnings.append("AMD + Windows 为实验性路径：官方 ROCm wheel 仅 Linux，建议 WSL2 或双系统。")
    if backend == "ascend" and _os_name() == "windows":
        warnings.append("昇腾 CANN 主路径为 Linux；Windows 建议用 WSL2 或 Linux 服务器。")

    return {
        "backend": backend,
        "device": device,
        "vram_mib": vram_mib,
        "evidence": evidence,
        "warnings": warnings,
    }


def _load_hardware_module():
    """导入 `bettercomfyui.hardware` 复用能力矩阵；失败返回 None（安装器仍可独立工作）。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import bettercomfyui.hardware as hardware  # noqa: PLC0415

        return hardware
    except Exception:  # noqa: BLE001 - 安装器不能依赖编排包一定可导入
        return None


def hardware_hints(backend: str, probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """给出启动参数/环境变量/能力分级；优先复用硬件模块，失败则用内置兜底。"""
    probe = probe or {}
    device = str(probe.get("device") or "")
    vram = int(probe.get("vram_mib") or 0)
    torch_info: dict[str, Any] = {"torch_available": True, "device_count": 1,
                                 "device_name": device, "vram_mib": vram}
    if backend == "cuda":
        torch_info.update({"cuda_available": True, "cuda_version": "12.6"})
    elif backend == "rocm":
        torch_info.update({"cuda_available": True, "rocm_available": True,
                          "rocm_version": "7.1"})
    elif backend == "xpu":
        torch_info.update({"xpu_available": True, "xpu_version": "xpu"})
    elif backend == "mps":
        torch_info.update({"mps_available": True})
    elif backend == "ascend":
        torch_info.update({"npu_available": True, "npu_version": "torch_npu"})
    # cpu 保持 torch_available=True 但没有加速器：硬件模块会给出 --cpu

    hardware = _load_hardware_module()
    if hardware is not None:
        try:
            info = hardware.detect_hardware(probe={
                "torch": torch_info, "smi": {},
                "platform": platform.platform(), "machine": platform.machine(),
            })
            return {
                "source": "bettercomfyui.hardware",
                "tier": info.tier,
                "tier_label": info.tier_label,
                "launch_args": list(info.launch_args),
                "env": dict(info.env),
                "full_h3_supported": bool(info.full_h3_supported),
                "warnings": list(info.warnings),
            }
        except Exception:  # noqa: BLE001 - 复用失败就退回内置表
            pass

    fallback = {
        "cuda": ([], {}),
        "rocm": ([], {"PYTORCH_HIP_ALLOC_CONF": "expandable_segments:True"}),
        "xpu": ([], {"SYCL_CACHE_PERSISTENT": "1"}),
        "mps": (["--force-fp16"], {}),
        "ascend": (["--disable-mmap", "--disable-smart-memory"],
                   {"PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True"}),
        "cpu": (["--cpu"], {}),
    }.get(backend, ([], {}))
    return {
        "source": "builtin-fallback",
        "tier": "C" if backend in ("mps", "xpu", "ascend") else ("D" if backend == "cpu" else "B"),
        "tier_label": "（内置口径，未加载 bettercomfyui.hardware）",
        "launch_args": list(fallback[0]),
        "env": dict(fallback[1]),
        "full_h3_supported": False,
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 计划步骤
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """一条可打印、可执行、可回滚的安装步骤。"""

    kind: str                     # command / check_path / copy / write / info
    title: str
    argv: list[str] = field(default_factory=list)
    path: str = ""
    src: str = ""
    dst: str = ""
    content: str = ""
    optional: bool = False
    force: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "argv": list(self.argv),
            "path": self.path, "src": self.src, "dst": self.dst,
            "optional": self.optional, "force": self.force,
        }

    def display(self) -> str:
        if self.kind == "command":
            return f"$ {' '.join(self.argv)}"
        if self.kind == "check_path":
            return f"检查文件：{self.path}"
        if self.kind == "copy":
            action = "备份后覆盖" if self.force else "已存在则跳过"
            return f"复制：{self.src} -> {self.dst}（{action}）"
        if self.kind == "write":
            return f"写入：{self.path}"
        return "说明"


def torch_steps(backend: str, python_exe: Path) -> list[Step]:
    """按后端生成 torch 安装步骤（昇腾只检查，不装 PyPI torch）。"""
    if backend == "ascend":
        return [
            Step("info", "昇腾不使用 PyPI torch：请先安装 CANN 与匹配的 torch_npu"
                         "（参考 https://www.hiascend.com/document 与 https://gitee.com/ascend/pytorch）"),
            Step("command", "验证 torch / torch_npu 可导入（失败不阻断，但需按指引修复）",
                 argv=[str(python_exe), "-c",
                       "import torch, torch_npu; print('torch', torch.__version__, "
                       "'torch_npu', torch_npu.__version__)"],
                 optional=True),
        ]
    if backend == "cpu":
        return [Step("command", "安装 CPU 版 PyTorch",
                     argv=[str(python_exe), "-m", "pip", "install", "--upgrade",
                           "torch", "torchvision", "torchaudio"])]
    if backend == "mps":
        return [Step("command", "安装 PyTorch（macOS 默认 wheel，MPS 支持）",
                     argv=[str(python_exe), "-m", "pip", "install", "--upgrade",
                           "torch", "torchvision", "torchaudio"])]
    source = TORCH_SOURCES[backend]
    return [Step("command", f"安装 PyTorch（{source['label']}）",
                 argv=[str(python_exe), "-m", "pip", "install", "--upgrade",
                       "torch", "torchvision", "torchaudio",
                       "--index-url", source["index"]])]


def plugin_steps(comfyui: Path, *, force: bool, include_orchestrator: bool = True) -> list[Step]:
    custom_nodes = comfyui / "custom_nodes"
    plugin_dst = custom_nodes / PLUGIN_DIRNAME
    steps: list[Step] = []
    plugin_src = REPO_ROOT / PLUGIN_DIRNAME
    try:
        same = plugin_src.resolve() == plugin_dst.resolve()
    except OSError:
        same = False
    if same:
        steps.append(Step("info", "插件目录与安装目标相同，跳过复制。"))
    else:
        steps.append(Step("copy", "安装剧核插件到 custom_nodes/", src=str(plugin_src),
                          dst=str(plugin_dst), force=force))
    orch_src = REPO_ROOT / ORCH_DIRNAME
    if include_orchestrator and orch_src.is_dir():
        steps.append(Step(
            "copy", "安装编排包 bettercomfyui（放进插件目录，_bridge 可直接找到）",
            src=str(orch_src), dst=str(plugin_dst / ORCH_DIRNAME), force=force))
    return steps


def launcher_script(*, backend: str, comfyui: Path, python_exe: Path,
                    hints: dict[str, Any]) -> tuple[str, str]:
    """生成启动器（返回文件名+内容）。昇腾自动带 --disable-mmap --disable-smart-memory。"""
    args = list(hints.get("launch_args") or [])
    env = dict(hints.get("env") or {})
    env[ENV_ORCH_HOME] = str(REPO_ROOT)
    main_py = comfyui / "main.py"
    # 注释必须是**单行**：BAT 里多行注释会漏掉 rem 前缀，变成一条会被执行的命令。
    header = (f"剧核 DramaCore 启动器 / backend={backend} / tier={hints.get('tier', '?')} / "
              "由 install_core.py 生成，可按需修改")
    if _os_name() == "windows":
        # 中文路径（本仓库路径就含中文）在 cmd 默认 GBK 下会乱码/找不到文件：
        # 文件按 UTF-8 写，并在第一行切到 65001；注释同样只用单行。
        lines = ["@echo off", "chcp 65001 >nul", f"rem {header}", "setlocal"]
        for key, value in env.items():
            lines.append(f'set "{key}={value}"')
        cmd = f'"{python_exe}" "{main_py}"' + ((" " + " ".join(args)) if args else "")
        lines += [cmd, "pause"]
        return "start_dramacore.bat", "\r\n".join(lines) + "\r\n"
    lines = ["#!/usr/bin/env bash", f"# {header}", "set -euo pipefail"]
    for key, value in env.items():
        lines.append(f"export {key}={_shell_quote(value)}")
    cmd = f"{_shell_quote(str(python_exe))} {_shell_quote(str(main_py))}"
    if args:
        cmd += " " + " ".join(args)
    lines += [cmd]
    return "start_dramacore.sh", "\n".join(lines) + "\n"


def _shell_quote(value: str) -> str:
    """最小 POSIX 引号：单引号包裹并转义内部单引号。"""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _models_note(comfyui: Path) -> str:
    lines = ["模型不会自动下载：请把 H3 权重放到下列目录（不存在就自己建）："]
    for name, why in MODEL_DIRS:
        lines.append(f"  - {comfyui / 'models' / name}  （{why}）")
    lines.append("  未放权重时编排/审核/剪辑可用，但真实生成会明确报缺权重（不会假装成功）。")
    return "\n".join(lines)


def build_plan(*, mode: str, comfyui: str, backend: str, python_exe: str | None = None,
               force: bool = False, skip_torch: bool = False, skip_launcher: bool = False,
               probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成安装计划（**纯函数，不做任何写入/网络**；dry-run 与执行共用）。"""
    comfy = Path(comfyui).expanduser()
    os_name = _os_name()
    hints = hardware_hints(backend, probe)
    warnings = list((probe or {}).get("warnings") or []) + list(hints.get("warnings") or [])
    steps: list[Step] = []
    notes: list[str] = []

    if not comfy.name:
        raise InstallError("--comfyui 路径为空，无法确定安装目标。")
    if backend == "rocm" and os_name == "windows":
        warnings.append("Windows + ROCm 为实验性：官方 ROCm wheel 仅 Linux；"
                        "真实安装需要 --allow-experimental，且大概率失败，建议 WSL2。")
    if backend == "ascend" and os_name == "windows":
        warnings.append("Windows + 昇腾：CANN 主路径为 Linux；真实安装需要 --allow-experimental。")

    if mode == "attach":
        steps.append(Step("check_path", "确认目标目录里有 ComfyUI 的 main.py", path=str(comfy / "main.py")))
        if not (comfy / "main.py").is_file():
            warnings.append(
                f"{comfy} 里没有 main.py：dry-run 仍可查看计划，但真实安装会在第 1 步失败。"
                "请用 --comfyui 指向正确的 ComfyUI 目录。")
        py = Path(python_exe) if python_exe else find_comfy_python(comfy)
        notes.append(f"使用 Python：{py}")
        if not skip_torch:
            steps.extend(torch_steps(backend, py))
        requirements = comfy / "requirements.txt"
        steps.append(Step("command", "安装 ComfyUI 依赖（requirements.txt）",
                          argv=[str(py), "-m", "pip", "install", "-r", str(requirements)],
                          optional=True))
    elif mode == "bootstrap":
        if (comfy / "main.py").is_file():
            raise InstallError(
                f"{comfy} 已经是 ComfyUI 安装目录 —— 请改用 --mode attach（不会覆盖已有安装）。")
        if comfy.exists() and any(comfy.iterdir()):
            raise InstallError(
                f"{comfy} 已存在且非空，拒绝在陌生目录里 clone（避免污染）。请换一个空目录。")
        steps.append(Step("command", "检查 git 可用", argv=["git", "--version"]))
        steps.append(Step("command", "拉取 ComfyUI（浅克隆）",
                          argv=["git", "clone", "--depth", "1",
                                DEFAULT_COMFYUI_REPO, str(comfy)]))
        steps.append(Step("command", "创建虚拟环境 venv",
                          argv=[python_exe or sys.executable, "-m", "venv", str(comfy / "venv")]))
        py = _venv_python(comfy)
        notes.append(f"使用 venv Python：{py}")
        steps.append(Step("command", "升级 pip",
                          argv=[str(py), "-m", "pip", "install", "--upgrade", "pip"]))
        if not skip_torch:
            steps.extend(torch_steps(backend, py))
        steps.append(Step("command", "安装 ComfyUI 依赖（requirements.txt）",
                          argv=[str(py), "-m", "pip", "install", "-r",
                                str(comfy / "requirements.txt")], optional=True))
    else:
        raise InstallError(f"未知模式 {mode!r}（只支持 attach / bootstrap）。")

    steps.extend(plugin_steps(comfy, force=force))
    if mode == "attach":
        py_for_launcher = Path(python_exe) if python_exe else find_comfy_python(comfy)
    else:
        py_for_launcher = _venv_python(comfy)
    if not skip_launcher:
        filename, content = launcher_script(
            backend=backend, comfyui=comfy, python_exe=py_for_launcher, hints=hints)
        steps.append(Step("write", "生成启动器（含后端参数与环境变量）",
                          path=str(comfy / filename), content=content))
    steps.append(Step("info", _models_note(comfy)))

    return {
        "mode": mode,
        "backend": backend,
        "comfyui": str(comfy),
        "python": str(py_for_launcher),
        "os": os_name,
        "hints": hints,
        "warnings": _dedupe(warnings),
        "notes": notes,
        "steps": steps,
        "torch_source": TORCH_SOURCES.get(backend, {}),
    }


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def plan_to_dict(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": plan["mode"], "backend": plan["backend"], "comfyui": plan["comfyui"],
        "python": plan["python"], "os": plan["os"],
        "torch_source": plan["torch_source"],
        "hints": {k: plan["hints"].get(k) for k in
                  ("source", "tier", "tier_label", "launch_args", "env", "full_h3_supported")},
        "warnings": plan["warnings"], "notes": plan["notes"],
        "steps": [step.to_dict() for step in plan["steps"]],
    }


def render_plan(plan: dict[str, Any]) -> str:
    hint = plan["hints"]
    lines = [
        "剧核 DramaCore · 安装计划",
        "=" * 40,
        f"模式      : {plan['mode']}",
        f"后端      : {plan['backend']}（torch 源：{plan['torch_source'].get('label', '—')}）",
        f"目标      : {plan['comfyui']}",
        f"Python    : {plan['python']}",
        f"能力分级  : {hint.get('tier_label')}",
    ]
    if hint.get("launch_args"):
        lines.append("启动参数  : " + " ".join(hint["launch_args"]))
    if hint.get("env"):
        lines.append("环境变量  : " + "  ".join(f"{k}={v}" for k, v in hint["env"].items()))
    if plan["warnings"]:
        lines.append("警告:")
        lines.extend(f"  ! {w}" for w in plan["warnings"])
    lines.append("")
    lines.append(f"步骤（共 {len(plan['steps'])} 步）：")
    for index, step in enumerate(plan["steps"], 1):
        suffix = "（可选）" if step.optional else ""
        lines.append(f"  {index:>2}. [{step.kind}]{suffix} {step.title}")
        detail = step.display()
        if detail and detail != "说明":
            lines.append(f"      {detail}")
    lines.extend([
        "",
        "说明：dry-run 不会创建目录、不跑 pip、不联网；真实执行请确认后加 --yes。",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 执行 + 回滚
# ---------------------------------------------------------------------------


class Transaction:
    """记录可回滚动作；失败时逆序执行（只回滚本安装器创建的产物）。"""

    def __init__(self) -> None:
        self._undo: list[tuple[str, Any]] = []

    def add(self, title: str, undo) -> None:  # noqa: ANN001 - 内部闭包
        self._undo.append((title, undo))

    def rollback(self) -> list[str]:
        messages: list[str] = []
        for title, undo in reversed(self._undo):
            try:
                undo()
                messages.append(f"已回滚：{title}")
            except Exception as exc:  # noqa: BLE001 - 回滚失败要报告，不能吞
                messages.append(f"回滚失败：{title}（{type(exc).__name__}: {exc}）")
        return messages


def _tail(text: str, limit: int = 1200) -> str:
    text = str(text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _execute_step(step: Step, txn: Transaction, *, force: bool = False) -> None:
    """执行单步；失败抛 InstallError，并在调用方统一回滚。"""
    if step.kind == "info":
        _say(f"\n[说明] {step.title}")
        return

    if step.kind == "check_path":
        path = Path(step.path)
        if not path.is_file():
            raise InstallError(
                f"目标缺少 {path}：{Path(step.path).parent} 看起来不是 ComfyUI 安装目录。\n"
                "  修复：用 --comfyui 指向含 main.py 的 ComfyUI 目录；"
                "或改用 --mode bootstrap 新建。")
        return

    if step.kind == "command":
        argv = list(step.argv)
        executable = argv[0]
        resolved = shutil.which(executable) or (executable if Path(executable).is_file() else "")
        if not resolved:
            message = f"找不到可执行文件：{executable}（{step.title}）"
            if step.optional:
                _warn(message + "；可选步骤，跳过。")
                return
            raise InstallError(message + "。请先安装它并加入 PATH。")
        # 不存在的 requirements.txt 只是可选步骤，跳过并说明
        if "-r" in argv:
            req = Path(argv[argv.index("-r") + 1])
            if not req.is_file() and step.optional:
                _warn(f"未找到 {req}；可选步骤，跳过。")
                return
        _say(f"\n$ {' '.join(argv)}")
        try:
            proc = subprocess.run(  # noqa: S603 - 参数列表，无 shell
                argv, capture_output=False, text=True, errors="replace", check=False)
        except Exception as exc:  # noqa: BLE001
            if step.optional:
                _warn(f"可选步骤执行失败：{type(exc).__name__}: {exc}")
                return
            raise InstallError(f"命令启动失败：{exc}") from exc
        if proc.returncode != 0:
            if step.optional:
                _warn(f"可选步骤返回 {proc.returncode}，继续（详见上方输出）。")
                return
            raise InstallError(
                f"命令失败（退出码 {proc.returncode}）：{' '.join(argv)}\n"
                "  修复：看上方 pip/git 输出；网络问题可换镜像或稍后重试（本安装器不自动回滚 pip 安装）。")
        return

    if step.kind == "copy":
        src = Path(step.src)
        dst = Path(step.dst)
        if not src.exists():
            raise InstallError(f"复制源不存在：{src}（安装器目录不完整？）")
        if dst.exists():
            if not (step.force or force):
                _warn(f"目标已存在，未覆盖：{dst}（用 --force 先备份再覆盖）")
                return
            backup = dst.with_name(f"{dst.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            _say(f"\n备份 {dst} -> {backup}")
            shutil.move(str(dst), str(backup))

            def restore_backup(backup_path: Path = backup, original: Path = dst) -> None:
                if backup_path.exists():
                    if original.exists():
                        shutil.rmtree(original, ignore_errors=True)
                    shutil.move(str(backup_path), str(original))

            txn.add(f"恢复备份 {backup.name}", restore_backup)
        else:
            created = dst
            txn.add(f"删除新建目录 {created.name}",
                    lambda p=created: shutil.rmtree(p, ignore_errors=True))
        dst.parent.mkdir(parents=True, exist_ok=True)
        _say(f"\n复制 {src} -> {dst}")
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        return

    if step.kind == "write":
        path = Path(step.path)
        previous = path.read_bytes() if path.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        # 不用 Path.write_text(newline=...)：那是 3.10+ 才有的参数，安装器要在 3.9 也能跑
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(step.content)
        if path.suffix == ".sh":
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        if previous is None:
            txn.add(f"删除新建文件 {path.name}", lambda p=path: p.unlink(missing_ok=True))
        else:
            txn.add(f"还原文件 {path.name}",
                    lambda p=path, data=previous: p.write_bytes(data))
        _say(f"\n写入 {path}")
        return

    raise InstallError(f"未知步骤类型：{step.kind}")


def execute_plan(plan: dict[str, Any], *, allow_experimental: bool = False,
                 keep_partial: bool = False) -> int:
    """执行计划；失败回滚并返回 EXIT_FAILED。"""
    backend = plan["backend"]
    if backend == "rocm" and plan["os"] == "windows" and not allow_experimental:
        _say("拒绝执行：Windows + ROCm 属实验性路径。")
        _say("  另选其一：1) Linux/WSL2 + ROCm 7.1；2) 改用 --backend cuda；"
             "3) 明确接受风险后加 --allow-experimental。")
        return EXIT_ENVIRONMENT
    if backend == "ascend" and plan["os"] == "windows" and not allow_experimental:
        _say("拒绝执行：Windows + 昇腾属实验性路径。")
        _say("  建议 Linux + CANN；确需尝试请加 --allow-experimental 并自行承担风险。")
        return EXIT_ENVIRONMENT

    txn = Transaction()
    try:
        for index, step in enumerate(plan["steps"], 1):
            _say(f"\n[{index}/{len(plan['steps'])}] {step.title}")
            _execute_step(step, txn)
    except InstallError as exc:
        _say(f"\n安装失败：{exc}")
        if keep_partial:
            _say("已保留中间产物（--keep-partial）。")
        else:
            messages = txn.rollback()
            if messages:
                _say("\n回滚结果：")
                for message in messages:
                    _say(f"  - {message}")
            _say("  （pip 已下载/安装的包不会自动卸载；如需干净环境请重建 venv。）")
        return EXIT_FAILED
    except KeyboardInterrupt:
        _say("\n已中断，正在回滚……")
        for message in txn.rollback():
            _say(f"  - {message}")
        return EXIT_FAILED

    _say("\n安装完成。")
    _say(_models_note(Path(plan["comfyui"])))
    launcher = "start_dramacore.bat" if plan["os"] == "windows" else "start_dramacore.sh"
    _say(f"\n启动：{Path(plan['comfyui']) / launcher}")
    _say("自检：python -m bettercomfyui.hardware doctor --json")
    _say("说明：本安装器不启动/重启 ComfyUI，也不下载模型。")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python install/install_core.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="剧核 DramaCore · ComfyUI 硬件适配安装器（attach / bootstrap）",
        epilog=(
            "示例:\n"
            "  python install/install_core.py --mode attach --comfyui D:/ComfyUI --dry-run\n"
            "  python install/install_core.py --mode bootstrap --comfyui D:/ComfyUI --backend cuda --yes\n"
            "  python install/install_core.py --mode attach --comfyui ./.comfyui --backend ascend --dry-run\n"
        ),
    )
    parser.add_argument("--mode", choices=("attach", "bootstrap"), default="attach",
                        help="attach=装到已有 ComfyUI；bootstrap=clone+venv 全新安装（默认 attach）")
    parser.add_argument("--comfyui", default="", metavar="PATH",
                        help="ComfyUI 目录（attach 必须存在；bootstrap 将创建于此）")
    parser.add_argument("--backend", default="auto", metavar="NAME",
                        help=f"后端：{'/'.join(BACKENDS)}；默认 auto 自动探测")
    parser.add_argument("--python", default="", metavar="PATH",
                        help="指定 Python 解释器（默认用 ComfyUI venv 或当前解释器）")
    parser.add_argument("--torch-index", default="", metavar="URL",
                        help="覆盖 PyTorch 安装源（默认按后端选择）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何写入/联网")
    parser.add_argument("--yes", action="store_true", help="跳过确认（非交互环境必须显式给出）")
    parser.add_argument("--force", action="store_true",
                        help="目标已存在时先备份再覆盖（默认跳过，不覆盖）")
    parser.add_argument("--skip-torch", action="store_true", help="跳过 torch 安装（已有可用环境时）")
    parser.add_argument("--skip-launcher", action="store_true", help="不生成启动器")
    parser.add_argument("--keep-partial", action="store_true", help="失败时保留中间产物（默认回滚）")
    parser.add_argument("--allow-experimental", action="store_true",
                        help="允许 Windows + ROCm / Windows + 昇腾 的实验性安装")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（dry-run 时输出计划）")
    parser.add_argument("--version", action="version", version="DramaCore installer round57")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    _configure_stdout()
    if sys.version_info < (3, 9):
        _say(f"需要 Python 3.9+，当前 {sys.version.split()[0]}；请升级后重试。")
        return EXIT_ENVIRONMENT
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    backend = normalize_backend(args.backend)
    if not backend:
        parser.error(f"未知后端 {args.backend!r}；可选：{', '.join(BACKENDS)}")

    comfyui = args.comfyui or default_comfyui()
    if not comfyui:
        _say("找不到 ComfyUI 目录：请用 --comfyui 指定（或设置环境变量 COMFYUI_PATH）。")
        _say("  全新安装：python install/install_core.py --mode bootstrap --comfyui D:/ComfyUI --yes")
        return EXIT_USAGE

    probe: dict[str, Any] = {}
    if backend == "auto":
        probe = detect_backend()
        backend = probe["backend"]
        if not args.json:
            _say(f"自动探测后端：{backend}"
                 + (f"（{probe.get('device')}）" if probe.get("device") else ""))
    else:
        # 显式指定时仍探测一次，只为给出证据/警告（失败不影响）。
        # 但如果用户选的卡和探测到的不是同一类，就不要把另一张卡的显存/警告拿来用
        # （典型：在 NVIDIA 机器上规划昇腾安装，4GB 显存警告毫无意义）。
        detected = detect_backend()
        if detected.get("backend") == backend:
            probe = detected
        else:
            probe = {"backend": backend, "device": "", "vram_mib": 0,
                     "evidence": detected.get("evidence", {}), "warnings": []}
    probe = dict(probe)
    probe.setdefault("warnings", [])

    if args.torch_index:
        TORCH_SOURCES[backend] = {
            "index": args.torch_index,
            "label": f"自定义源（{args.torch_index}）",
            "note": "由 --torch-index 覆盖",
        }

    try:
        plan = build_plan(
            mode=args.mode, comfyui=comfyui, backend=backend,
            python_exe=args.python or None, force=args.force,
            skip_torch=args.skip_torch, skip_launcher=args.skip_launcher, probe=probe,
        )
    except InstallError as exc:
        _say(f"无法生成安装计划：{exc}")
        return EXIT_ENVIRONMENT

    if args.json:
        _say(json.dumps(plan_to_dict(plan), ensure_ascii=False, indent=2))
    else:
        _say(render_plan(plan))

    if args.dry_run:
        if not args.json:
            _say("\n[dry-run] 未执行任何步骤。")
        return EXIT_OK

    if not args.yes:
        if not sys.stdin.isatty():
            _say("\n非交互环境：真实安装必须显式加 --yes（或先用 --dry-run 看计划）。")
            return EXIT_USAGE
        answer = input("\n确认按上述计划安装？输入 yes 继续：").strip().lower()
        if answer not in ("y", "yes", "是"):
            _say("已取消。")
            return EXIT_USAGE

    return execute_plan(plan, allow_experimental=args.allow_experimental,
                        keep_partial=args.keep_partial)


if __name__ == "__main__":
    raise SystemExit(main())
