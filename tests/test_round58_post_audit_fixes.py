# -*- coding: utf-8 -*-
"""ROUND58 发布后终审修复回归：DNS rebinding Host 校验 + 硬件运行时门禁。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_host_is_loopback_helper_blocks_dns_rebinding_hosts():
    from bettercomfyui_comfy.routes import _host_is_loopback

    assert _host_is_loopback("127.0.0.1:8188") is True
    assert _host_is_loopback("localhost:8188") is True
    assert _host_is_loopback("[::1]:8188") is True
    assert _host_is_loopback("evil.example") is False
    assert _host_is_loopback("evil.example:8188") is False
    assert _host_is_loopback("192.168.1.10:8188") is False
    assert _host_is_loopback("") is False


def test_sensitive_guards_require_loopback_host_header():
    """静态守卫：三个敏感写接口都必须校验 Host 是 loopback（DNS rebinding 防护）。"""
    routes = (ROOT / "bettercomfyui_comfy/routes.py").read_text(encoding="utf-8")
    assert "_host_is_loopback" in routes
    assert "DNS rebinding" in routes
    # _panel_sensitive_guard 内同时检查 Host 与 Origin
    guard = routes[routes.index("def _panel_sensitive_guard"):routes.index("async def h_panel_config_save")]
    assert "_host_is_loopback(host)" in guard
    assert "Origin 非本机" in guard
    api = (ROOT / "bettercomfyui_comfy/api_settings.py").read_text(encoding="utf-8")
    save = api[api.index("async def handle_save"):api.index("def _dumps")]
    assert "DNS rebinding" in save
    assert "Origin 非本机" in save


def test_hardware_does_not_promote_cpu_torch_to_full_h3():
    from bettercomfyui.hardware import detect_hardware

    info = detect_hardware(probe={
        "torch": {"torch_available": True, "torch_version": "2.14.1+cpu",
                  "cuda_available": False, "rocm_version": "", "device_count": 0},
        "smi": {"nvidia_smi": ["NVIDIA GeForce RTX 4090, 24564 MiB"]},
        "platform": "Windows-10", "machine": "AMD64",
    })
    assert info.vendor == "nvidia"
    assert info.runtime_available is False
    assert info.tier == "C"
    assert info.full_h3_supported is False
