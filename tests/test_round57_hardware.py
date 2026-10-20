# -*- coding: utf-8 -*-
"""ROUND57：多品牌硬件适配 + 安装部署的验收测试。

覆盖三块：

1. **能力矩阵**（`bettercomfyui.hardware`）：用 mock probe 覆盖 NVIDIA A/B、
   Ascend B、AMD Linux B / Windows C、Intel C、Apple C、CPU D，以及
   nvidia-smi / npu-smi / rocminfo / lspci 兜底。**不需要真机**；
2. **运行时适配**（`bettercomfyui_comfy.hardware_adapt`）：无 ComfyUI / 无编排包时
   不抛异常；昇腾补丁真实包裹 `comfy.model_management`、幂等、NPU 非活跃时保持原行为；
   comfy-kitchen INT8 状态检测；
3. **安装器**（`install/install_core.py`）：计划是纯函数、`--dry-run` 不落地、
   回滚真的删掉复制产物、昇腾启动参数/环境变量齐全、shell/PowerShell 语法可解析。

原则：**不联网、不装依赖、不启动 ComfyUI**；本轮也没有昇腾/AMD/Intel/Apple 真机，
所有非 NVIDIA 结论都只到「代码分支与参数正确」这一层。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.hardware import (  # noqa: E402
    H3_MIN_VRAM_MIB,
    H3_RECOMMENDED_VRAM_MIB,
    detect_hardware,
    format_report,
)
from bettercomfyui_comfy import hardware_adapt as adapt  # noqa: E402
from install import install_core  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具 / 工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_adapt_cache():
    """每个用例前后清空硬件适配缓存，避免用例之间串状态。"""
    adapt.reset_cache()
    yield
    adapt.reset_cache()


def _probe(torch: dict | None = None, smi: dict | None = None,
           platform: str = "Linux-6.8.0-generic-x86_64", machine: str = "x86_64") -> dict:
    base = {"torch_available": True}
    base.update(torch or {})
    return {"torch": base, "smi": smi or {}, "platform": platform, "machine": machine}


def _torch(*, vram_mib: int = 0, **kwargs) -> dict:
    data = {"cuda_available": False, "device_name": "", "vram_mib": vram_mib,
            "device_count": 1}
    data.update(kwargs)
    return data


def _fake_comfy_model_management(monkeypatch, **functions):
    """在 sys.modules 里造一个假的 comfy.model_management。"""
    comfy = types.ModuleType("comfy")
    mm = types.ModuleType("comfy.model_management")
    for name, func in functions.items():
        setattr(mm, name, func)
    comfy.model_management = mm
    monkeypatch.setitem(sys.modules, "comfy", comfy)
    monkeypatch.setitem(sys.modules, "comfy.model_management", mm)
    return mm


# ---------------------------------------------------------------------------
# 1. 能力矩阵（mock probe）
# ---------------------------------------------------------------------------


def test_nvidia_a_tier_full_support():
    info = detect_hardware(probe=_probe(_torch(
        cuda_available=True, cuda_version="12.6",
        device_name="NVIDIA A100-SXM4-80GB", vram_mib=81920,
    )))
    assert info.vendor == "nvidia" and info.backend == "cuda"
    assert info.tier == "A", info.tier_label
    assert info.full_h3_supported is True
    assert info.asset_generation_supported is True
    assert info.launch_args == []


def test_nvidia_24gb_reaches_a_tier_after_threshold_fix():
    """24GB 是 pipeline 的门槛：不能拿 48GB 当硬门槛（旧代码会把 40GB 卡误判 B）。"""
    info = detect_hardware(probe=_probe(_torch(
        cuda_available=True, device_name="NVIDIA A100-SXM4-40GB", vram_mib=40960,
    )))
    assert info.tier == "A"
    assert info.full_h3_supported is True
    # 达到下限但不到舒适档：必须提示，不能只说「支持」
    assert any("舒适档" in w or "48GB" in w for w in info.warnings), info.warnings


def test_nvidia_b_tier_low_vram_honest():
    info = detect_hardware(probe=_probe(_torch(
        cuda_available=True, device_name="NVIDIA GeForce GTX 1050 Ti", vram_mib=4096,
    )))
    assert info.vendor == "nvidia" and info.tier == "B"
    assert info.full_h3_supported is False
    assert info.asset_generation_supported is True
    assert any("24GB" in w for w in info.warnings), info.warnings


def test_ascend_b_tier_launch_args_and_required_patches():
    info = detect_hardware(probe=_probe(_torch(
        npu_available=True, npu_version="2.7.1", device_name="Ascend 910B3",
        vram_mib=65536,
    )))
    assert info.vendor == "ascend" and info.backend == "npu"
    assert info.tier == "B", info.tier_label
    assert info.full_h3_supported is True
    # 社区关键启动参数必须原样出现在能力矩阵里（安装器再复用它们）
    assert info.launch_args == ["--disable-mmap", "--disable-smart-memory"]
    assert info.env.get("PYTORCH_NPU_ALLOC_CONF") == "expandable_segments:True"
    required = " ".join(info.patches_required)
    assert "is_nvidia" in required and "supports_fp8_compute" in required
    assert "comfy-kitchen" in required and "npu_quant_matmul" in required
    assert any("有条件支持" in w for w in info.warnings)


def test_ascend_low_or_unknown_vram_is_c_tier():
    info = detect_hardware(probe=_probe(_torch(npu_available=True, vram_mib=0)))
    assert info.vendor == "ascend" and info.tier == "C"
    assert info.full_h3_supported is False
    assert info.launch_args == ["--disable-mmap", "--disable-smart-memory"]


def test_amd_linux_rocm_b_tier():
    """AMD Linux 的 B 级依赖「OS=linux」，而旧代码错误地拿 CPU 架构去比。"""
    info = detect_hardware(probe=_probe(_torch(
        cuda_available=True, rocm_available=True, rocm_version="7.1.1",
        device_name="AMD Instinct MI300X", vram_mib=196608,
    ), platform="Linux-6.8.0-generic-x86_64", machine="x86_64"))
    assert info.vendor == "amd" and info.backend == "rocm"
    assert info.tier == "B"
    assert info.full_h3_supported is True


def test_amd_windows_is_c_tier_experimental():
    info = detect_hardware(probe=_probe(_torch(
        cuda_available=True, rocm_available=True, rocm_version="7.1.1",
        device_name="AMD Radeon RX 7900 XTX", vram_mib=24576,
    ), platform="Windows-10-10.0.19045-SP0", machine="AMD64"))
    assert info.vendor == "amd" and info.tier == "C"
    assert info.full_h3_supported is False
    assert any("Windows" in w for w in info.warnings), info.warnings


def test_intel_xpu_is_c_tier():
    info = detect_hardware(probe=_probe(_torch(
        xpu_available=True, xpu_version="2025.1", device_name="Intel Arc A770",
        vram_mib=16384,
    )))
    assert info.vendor == "intel" and info.backend == "xpu"
    assert info.tier == "C"


def test_apple_mps_is_c_tier_even_with_large_unified_memory():
    info = detect_hardware(probe=_probe(_torch(
        mps_available=True, device_name="Apple M3 Max", vram_mib=65536,
    ), platform="Darwin-24.1.0-arm64", machine="arm64"))
    assert info.vendor == "apple" and info.backend == "mps"
    assert info.tier == "C"
    assert info.full_h3_supported is False, "统一内存不得冒充可跑 H3 全流程"
    assert "--force-fp16" in info.launch_args


def test_cpu_is_d_tier():
    # CPU 机器：torch 装了但没有加速器
    info = detect_hardware(probe=_probe(_torch(), platform="Linux-6.8.0-generic-x86_64"))
    assert info.vendor == "cpu" and info.tier == "D"
    assert info.full_h3_supported is False
    assert info.asset_generation_supported is False
    assert info.launch_args == ["--cpu"]


def test_smi_fallback_nvidia_without_torch():
    """torch 没装（本机现状）：nvidia-smi 仍要给出品牌、型号、显存，不能只报 CPU。"""
    info = detect_hardware(probe={
        "torch": {"torch_available": False},
        "smi": {"nvidia_smi": ["NVIDIA GeForce RTX 4090, 24564 MiB"]},
        "platform": "Windows-10-10.0.19045-SP0", "machine": "AMD64",
    })
    assert info.vendor == "nvidia" and info.backend == "cuda"
    assert info.device_name == "NVIDIA GeForce RTX 4090"
    assert info.vram_mib == 24564
    assert info.tier == "A"
    assert info.torch_available is False
    assert any("PyTorch" in w for w in info.warnings)


def test_smi_fallback_npu_smi():
    info = detect_hardware(probe={
        "torch": {"torch_available": False},
        "smi": {"npu_smi": ["| 0  910B3  | OK | 0 / 65536 |"]},
        "platform": "Linux-6.8.0-generic-aarch64", "machine": "aarch64",
    })
    assert info.vendor == "ascend"
    assert info.npu_available is True
    assert info.launch_args == ["--disable-mmap", "--disable-smart-memory"]


def test_smi_fallback_rocminfo_without_torch():
    info = detect_hardware(probe={
        "torch": {"torch_available": False},
        "smi": {"rocminfo": ["Name: gfx1100", "Vendor: AMD"]},
        "platform": "Linux-6.8.0-generic-x86_64", "machine": "x86_64",
    })
    assert info.vendor == "amd" and info.backend == "rocm"
    assert info.rocm_available is True
    assert info.tier == "C", "没有显存信息时按 C（不猜）"


def test_smi_fallback_lspci():
    info = detect_hardware(probe={
        "torch": {"torch_available": False},
        "smi": {"lspci": ["01:00.0 VGA compatible controller: NVIDIA Corporation GA102"]},
        "platform": "Linux-6.8.0-generic-x86_64", "machine": "x86_64",
    })
    assert info.vendor == "nvidia"
    assert "lspci" in info.device_name
    assert info.tier == "B"


def test_vram_threshold_matches_pipeline_source_of_truth():
    from bettercomfyui.pipeline.core import H3_MIN_VRAM_MIB as pipeline_min

    assert H3_MIN_VRAM_MIB == pipeline_min == 24_000
    assert H3_RECOMMENDED_VRAM_MIB == 48 * 1024


def test_format_report_has_no_gbk_hostile_glyph():
    """曾因报告里的 `⚠` 在 Windows GBK 控制台直接 UnicodeEncodeError（doctor 崩）。"""
    info = detect_hardware(probe=_probe(_torch(
        cuda_available=True, device_name="NVIDIA GeForce GTX 1050 Ti", vram_mib=4096,
    )))
    text = format_report(info)
    assert "⚠" not in text
    assert "警告:" in text and "! " in text
    # 报告里必须出现「未真机验收」的免责声明
    assert "真机" in text


# ---------------------------------------------------------------------------
# 2. 运行时适配（hardware_adapt）
# ---------------------------------------------------------------------------


def test_adapt_preflight_is_safe_without_comfyui(monkeypatch):
    """不在 ComfyUI 进程里（没有 comfy 模块）也必须返回结构化结果，不抛。"""
    monkeypatch.setitem(sys.modules, "comfy", None)  # 让 import comfy.* 失败
    result = adapt.preflight()
    assert isinstance(result, dict)
    assert result["available"] is True, result.get("error")
    assert result["patches_status"] == "not_required"  # 本机 NVIDIA，无需昇腾补丁
    assert "vendor" in result and "tier" in result


def test_adapt_ascend_patch_is_deferred_without_comfyui(monkeypatch):
    monkeypatch.setitem(sys.modules, "comfy", None)
    report = adapt.ensure_runtime_patches(report={"vendor": "ascend"}, force=True)
    assert report["needed"] is True
    assert report["status"] == "deferred"
    assert "comfy.model_management" in report["comfy_ui_import_error"]
    assert len(report["manual_steps"]) >= 3  # 不静默：必须给出人工步骤


def test_adapt_degrades_cleanly_without_orchestrator(monkeypatch):
    monkeypatch.setattr(adapt, "_load_hardware_module",
                        lambda: (None, "模拟：编排包 bettercomfyui 缺失"))
    report = adapt.hardware_report(force=True)
    assert report["available"] is False
    assert "bettercomfyui" in report["report_error"]
    assert report["tier"] == "D"
    payload = adapt.route_payload(force=True)
    json.dumps(payload, ensure_ascii=False)  # 必须可 JSON 序列化
    assert payload["ok"] is True and payload["available"] is False


def test_adapt_disabled_by_env(monkeypatch):
    monkeypatch.setenv(adapt.ENV_DISABLE, "1")
    report = adapt.hardware_report(force=True)
    assert report["available"] is False and report["disabled"] is True
    patches = adapt.ensure_runtime_patches(report={"vendor": "ascend"}, force=True)
    assert patches["status"] == "disabled"


def test_ascend_patches_apply_idempotently_and_preserve_original(monkeypatch):
    """核心保护：NPU 活跃时 is_nvidia()/supports_fp8_compute() 必须返回 False。"""
    mm = _fake_comfy_model_management(
        monkeypatch,
        is_nvidia=lambda: True,
        supports_fp8_compute=lambda device=None: True,
    )
    npu_active = {"value": True}
    monkeypatch.setattr(adapt, "_npu_active", lambda: npu_active["value"])

    first = adapt.ensure_runtime_patches(report={"vendor": "ascend"}, force=True)
    assert first["status"] == "applied", first
    assert {item["name"]: item["status"] for item in first["items"]} == {
        "is_nvidia": "applied", "supports_fp8_compute": "applied"}

    assert mm.is_nvidia() is False
    assert mm.supports_fp8_compute() is False
    assert mm.supports_fp8_compute("cuda:0") is False

    # 幂等：再次调用只能看到 already_applied，不能层层套娃
    second = adapt.ensure_runtime_patches(report={"vendor": "ascend"}, force=True)
    assert second["status"] == "already_applied", second
    assert all(item["status"] == "already_applied" for item in second["items"])

    # 非 NPU 设备：完全保持 ComfyUI 原行为（NVIDIA 路径不能被误伤）
    npu_active["value"] = False
    assert mm.is_nvidia() is True
    assert mm.supports_fp8_compute() is True


def test_ascend_patch_reports_partial_when_function_missing(monkeypatch):
    mm = _fake_comfy_model_management(monkeypatch, is_nvidia=lambda: True)
    monkeypatch.setattr(adapt, "_npu_active", lambda: True)
    result = adapt.ensure_runtime_patches(report={"vendor": "ascend"}, force=True)
    assert result["status"] == "partial", result
    statuses = {item["name"]: item["status"] for item in result["items"]}
    assert statuses["is_nvidia"] == "applied"
    assert statuses["supports_fp8_compute"] == "missing"
    assert "supports_fp8_compute" in " ".join(result["manual_steps"])


def test_ascend_patches_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv(adapt.ENV_SKIP_ASCEND_PATCHES, "1")
    _fake_comfy_model_management(monkeypatch, is_nvidia=lambda: True,
                                  supports_fp8_compute=lambda device=None: True)
    result = adapt.ensure_runtime_patches(report={"vendor": "ascend"}, force=True)
    assert result["status"] == "disabled_by_env"
    assert result["items"] == []
    assert result["manual_steps"]


def test_non_ascend_never_patches(monkeypatch):
    called = {"value": False}
    _fake_comfy_model_management(monkeypatch, is_nvidia=lambda: called.__setitem__("value", True))
    result = adapt.ensure_runtime_patches(report={"vendor": "nvidia"}, force=True)
    assert result["status"] == "not_required"
    assert called["value"] is False


def test_comfy_kitchen_int8_detection(tmp_path, monkeypatch):
    package = tmp_path / "comfy_kitchen"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    int8 = package / "tensor_int8.py"
    int8.write_text(
        "def gemm(a, b):\n    return torch._int_mm(a, b)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "comfy_kitchen", raising=False)

    result = adapt.detect_comfy_kitchen()
    assert result["installed"] is True
    assert result["uses_int_mm"] is True
    assert result["status"] == "needs_npu_branch", result
    assert "npu_quant_matmul" in result["detail"]

    int8.write_text(
        "def gemm(a, b):\n    return torch_npu.npu_quant_matmul(a, b)\n", encoding="utf-8")
    result2 = adapt.detect_comfy_kitchen()
    assert result2["status"] == "npu_branch_present", result2
    assert result2["has_npu_branch"] is True


def test_comfy_kitchen_not_installed_is_reported(monkeypatch):
    monkeypatch.delitem(sys.modules, "comfy_kitchen", raising=False)
    # 保证 find_spec 找不到（前面用例可能往 sys.path 里加过假包）
    monkeypatch.setattr(adapt.importlib.util, "find_spec", lambda name: None)
    result = adapt.detect_comfy_kitchen()
    assert result["installed"] is False
    assert result["status"] == "not_installed"
    assert "未安装" in result["detail"]


def test_boot_summary_never_raises(monkeypatch):
    import bettercomfyui_comfy as pack

    def boom(**_kwargs):
        raise RuntimeError("模拟硬件探测爆炸")

    monkeypatch.setattr(adapt, "preflight", boom)
    pack._hardware_boot_summary()  # noqa: SLF001 - 专门验证启动期兜底
    assert callable(pack._hardware_boot_summary)


def test_route_payload_contains_required_sections():
    payload = adapt.route_payload(force=True)
    for key in ("ok", "available", "report", "patches", "preflight",
                "report_text", "notice"):
        assert key in payload, key
    assert payload["report"]["tier"] in ("A", "B", "C", "D")
    assert "真机" in payload["notice"]
    json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 3. HTTP 路由（aiohttp 缺失时用假模块，不跳过）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status


class _FakeRequest:
    def __init__(self, query=None):
        self.query = query or {}


def _install_fake_aiohttp(monkeypatch):
    module = types.ModuleType("aiohttp")

    class _Web:
        @staticmethod
        def json_response(data, status=200, dumps=None):
            return _FakeResponse(data, status)

    module.web = _Web
    monkeypatch.setitem(sys.modules, "aiohttp", module)
    return module


def test_hardware_route_handler_returns_json(monkeypatch):
    """真正调用一次处理器（不是只 import）：面板请求不能 500。"""
    import bettercomfyui_comfy.routes as routes

    _install_fake_aiohttp(monkeypatch)
    response = asyncio.run(routes.h_hardware(_FakeRequest()))
    assert response.status == 200, response.data
    payload = response.data
    assert payload["ok"] is True
    assert "report" in payload and "patches" in payload


def test_route_payload_probes_only_once(monkeypatch):
    """`?refresh=1` 只应探测一次：报告/文本/preflight 必须同源，避免字段互相矛盾。"""
    calls = {"count": 0}
    real_detect = adapt._detect  # noqa: SLF001

    def counted(force: bool = False):
        calls["count"] += 1
        return real_detect(force=force)

    monkeypatch.setattr(adapt, "_detect", counted)
    adapt.reset_cache()
    payload = adapt.route_payload(force=True)
    assert calls["count"] == 1, f"一次请求探测了 {calls['count']} 次"
    assert payload["preflight"]["tier"] == payload["report"]["tier"]


def test_hardware_route_registered_on_both_prefixes(monkeypatch):
    import bettercomfyui_comfy.routes as routes

    class _Router:
        def __init__(self):
            self.gets = []
            self.posts = []

        def add_get(self, path, handler):
            self.gets.append(path)

        def add_post(self, path, handler):
            self.posts.append(path)

    class _App:
        def __init__(self):
            self.router = _Router()

    app = _App()
    server = types.ModuleType("server")
    server.PromptServer = type("PromptServer", (), {"instance": types.SimpleNamespace(app=app)})
    monkeypatch.setitem(sys.modules, "server", server)
    monkeypatch.setattr(routes, "ROUTES_REGISTERED", routes.ROUTES_REGISTERED)
    monkeypatch.setattr(routes, "ROUTES_ERROR", routes.ROUTES_ERROR)

    assert routes.register_routes() is True
    assert "/bettercomfyui/hardware" in app.router.gets
    assert "/api/bettercomfyui/hardware" in app.router.gets


# ---------------------------------------------------------------------------
# 4. 安装器：纯计划 / dry-run / 回滚 / 语法
# ---------------------------------------------------------------------------


def test_normalize_backend_aliases():
    assert install_core.normalize_backend("nvidia") == "cuda"
    assert install_core.normalize_backend("HIP") == "rocm"
    assert install_core.normalize_backend("intel") == "xpu"
    assert install_core.normalize_backend("apple") == "mps"
    assert install_core.normalize_backend("NPU") == "ascend"
    assert install_core.normalize_backend("nonsense") == ""


def test_parse_npu_hbm_mib():
    sample = "| 0  910B3 | OK | 93.1 | 36 | 0 / 65536 |"
    assert install_core.parse_npu_hbm_mib(sample) == 65536
    assert install_core.parse_npu_hbm_mib("no numbers") == 0


def test_hardware_hints_ascend_reuses_capability_matrix():
    hints = install_core.hardware_hints("ascend", {"device": "Ascend 910B3", "vram_mib": 65536})
    assert hints["launch_args"] == ["--disable-mmap", "--disable-smart-memory"]
    assert hints["env"]["PYTORCH_NPU_ALLOC_CONF"] == "expandable_segments:True"
    assert hints["tier"] == "B"
    assert hints["source"] == "bettercomfyui.hardware"


def test_launcher_ascend_contains_flags_and_env():
    hints = install_core.hardware_hints("ascend", {"vram_mib": 65536})
    filename, content = install_core.launcher_script(
        backend="ascend", comfyui=Path("D:/AI/ComfyUI"),
        python_exe=Path("D:/AI/ComfyUI/venv/Scripts/python.exe"), hints=hints)
    if install_core._os_name() == "windows":  # noqa: SLF001
        assert filename == "start_dramacore.bat"
        assert "chcp 65001" in content
        assert "--disable-mmap --disable-smart-memory" in content
        assert "PYTORCH_NPU_ALLOC_CONF=expandable_segments:True" in content
        # BAT 注释必须单行：多行会漏 rem 前缀，被 cmd 当命令执行
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("rem ", "@", "chcp ", "setlocal",
                                                     "set ", "\"", "pause")):
                pytest.fail(f"启动器里有无法识别的 BAT 行：{line!r}")
    assert "BETTERCOMFYUI_HOME" in content


def test_build_plan_attach_is_pure(tmp_path):
    target = tmp_path / "ComfyUI"
    target.mkdir()
    plan = install_core.build_plan(mode="attach", comfyui=str(target), backend="cuda",
                                   probe={"device": "RTX 4090", "vram_mib": 24564,
                                          "warnings": []})
    argv_list = [step.argv for step in plan["steps"] if step.kind == "command"]
    assert any("--index-url" in argv and "cu126" in " ".join(argv) for argv in argv_list)
    assert any(step.kind == "write" and "start_dramacore" in step.path
               for step in plan["steps"])
    assert any(step.kind == "copy" and step.dst.endswith("bettercomfyui_comfy")
               for step in plan["steps"])
    assert list(target.iterdir()) == [], "build_plan 不允许写磁盘"


def test_build_plan_bootstrap_steps(tmp_path):
    target = tmp_path / "ComfyUI"
    plan = install_core.build_plan(mode="bootstrap", comfyui=str(target), backend="cuda",
                                   probe={})
    argv_list = [" ".join(step.argv) for step in plan["steps"] if step.kind == "command"]
    assert any("git clone" in argv for argv in argv_list)
    assert any("-m venv" in argv for argv in argv_list)
    assert any("cu126" in argv for argv in argv_list)
    assert not target.exists(), "build_plan 不允许创建目录"


def test_build_plan_bootstrap_refuses_existing_comfyui(tmp_path):
    target = tmp_path / "ComfyUI"
    target.mkdir()
    (target / "main.py").write_text("# fake comfy", encoding="utf-8")
    with pytest.raises(install_core.InstallError) as excinfo:
        install_core.build_plan(mode="bootstrap", comfyui=str(target), backend="cuda")
    assert "attach" in str(excinfo.value)


def test_build_plan_ascend_has_no_torch_pip_step(tmp_path):
    plan = install_core.build_plan(mode="attach", comfyui=str(tmp_path), backend="ascend",
                                   probe={})
    argv_list = [" ".join(step.argv) for step in plan["steps"] if step.kind == "command"]
    assert not any("pip install" in argv and "torch " in argv for argv in argv_list)
    assert any("torch_npu" in step.title or "torch_npu" in " ".join(step.argv)
               for step in plan["steps"])


def test_execute_plan_rolls_back_copied_directory(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "marker.txt").write_text("x", encoding="utf-8")
    dst = tmp_path / "dest"
    plan = {
        "backend": "cuda", "os": install_core._os_name(),  # noqa: SLF001
        "comfyui": str(tmp_path / "ComfyUI"),
        "steps": [
            install_core.Step("copy", "复制测试目录", src=str(src), dst=str(dst)),
            install_core.Step("command", "必定失败", argv=[sys.executable, "-c",
                                                          "import sys; sys.exit(3)"]),
        ],
    }
    code = install_core.execute_plan(plan)
    assert code == install_core.EXIT_FAILED
    assert not dst.exists(), "失败后必须回滚掉复制产物"


def test_execute_plan_rejects_windows_rocm_without_allow(tmp_path):
    if install_core._os_name() != "windows":  # noqa: SLF001
        pytest.skip("只在 Windows 上验证实验性拦截")
    plan = {"backend": "rocm", "os": "windows", "comfyui": str(tmp_path), "steps": []}
    assert install_core.execute_plan(plan) == install_core.EXIT_ENVIRONMENT


def _run_installer(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "install" / "install_core.py"), *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env, check=False,
    )


def test_installer_help_runs():
    proc = _run_installer("--help")
    assert proc.returncode == 0, proc.stderr
    assert "--dry-run" in proc.stdout and "--backend" in proc.stdout


def test_installer_dry_run_json_in_temp_dir(tmp_path):
    target = tmp_path / "ComfyUI"
    proc = _run_installer("--mode", "attach", "--comfyui", str(target),
                          "--backend", "cuda", "--dry-run", "--json")
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    assert plan["mode"] == "attach" and plan["backend"] == "cuda"
    assert any("cu126" in " ".join(step["argv"]) for step in plan["steps"])
    assert not target.exists(), "--dry-run 不得创建目录"


def test_installer_dry_run_ascend_mentions_launch_flags(tmp_path):
    proc = _run_installer("--mode", "attach", "--comfyui", str(tmp_path / "ComfyUI"),
                          "--backend", "ascend", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "--disable-mmap --disable-smart-memory" in proc.stdout
    assert "CANN" in proc.stdout


def test_installer_rejects_unknown_backend(tmp_path):
    proc = _run_installer("--mode", "attach", "--comfyui", str(tmp_path),
                          "--backend", "nonsense", "--dry-run")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "未知后端" in proc.stderr


def _find_bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def test_shell_scripts_syntax():
    bash = _find_bash()
    if not bash:
        pytest.skip("本机没有 bash，跳过 bash -n（已用其他方式人工检查）")
    for name in ("install_linux.sh", "install_macos.sh"):
        proc = subprocess.run([bash, "-n", str(REPO_ROOT / "install" / name)],
                              capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 0, f"{name} 语法错误：{proc.stderr}"


def test_windows_ps1_parses():
    if os.name != "nt":
        pytest.skip("非 Windows，跳过 PowerShell 语法解析")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("本机没有 PowerShell")
    script_path = str(REPO_ROOT / "install" / "install_windows.ps1")
    command = (
        "$e=$null; $t=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script_path}',"
        "[ref]$t,[ref]$e)|Out-Null; "
        "if ($e.Count -gt 0) { $e | ForEach-Object { Write-Error $_.Message }; exit 1 }; exit 0"
    )
    proc = subprocess.run([powershell, "-NoProfile", "-Command", command],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
