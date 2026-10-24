# -*- coding: utf-8 -*-
"""剧核 DramaCore · 安装/部署脚本包（ROUND57）。

这个包只放**可执行的安装入口**，不放运行时代码：

    install_core.py      跨平台安装器核心（attach / bootstrap、探测、计划、回滚）
    install_windows.ps1  Windows 包装：找 Python、转发参数
    install_linux.sh     Linux 包装（ROCm/CUDA/昇腾）
    install_macos.sh     macOS 包装（MPS）

能力分级与启动参数复用 `bettercomfyui.hardware`；安装器本身只依赖 Python 标准库。
"""
