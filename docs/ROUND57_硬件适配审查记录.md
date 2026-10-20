# ROUND57/58 硬件适配 + 一键安装部署 · Lead 复核记录

- 复核人：Lead
- 日期：2026-10-20
- 范围：`bettercomfyui/hardware.py`、`bettercomfyui_comfy/hardware_adapt.py`、`bettercomfyui_comfy/routes.py` 硬件路由、`install/*`、`tests/test_round57_hardware.py`、两份 ROUND57 文档、UI 硬件状态接线。
- 原则：**代码/文档级验证 ≠ 真机验证**；未真机跑过的品牌一律标注"未验证"。

## 1. 独立复核证据

### 1.1 硬件能力矩阵（mock probe，Lead 独立复跑）
| 设备 | 结果 |
|---|---|
| NVIDIA 48GB（Windows） | vendor=nvidia / Tier A / full=True |
| NVIDIA 24.5GB（smi 兜底） | vendor=nvidia / Tier A（符合 24GB 下限口径） |
| AMD ROCm 48GB（Linux） | vendor=amd / Tier B / full=True |
| AMD ROCm（Windows） | vendor=amd / Tier C / full=False（实验性） |
| Ascend 910B3 64GB | vendor=ascend / Tier B / full=True；launch_args=`--disable-mmap --disable-smart-memory`；patches_required=2 |
| Intel XPU 16GB | vendor=intel / Tier C / full=False |
| Apple MPS 36GB | vendor=apple / Tier C / full=False；launch_args=`--force-fp16` |
| CPU | vendor=cpu / Tier D / full=False；launch_args=`--cpu` |

结论：与 `pipeline.core.H3_MIN_VRAM_MIB=24_000` 口径一致；AMD Linux/Windows 分流正确；昇腾必需补丁/启动参数如实声明。

### 1.2 运行时适配层（无 ComfyUI 环境安全测试）
- `bettercomfyui_comfy.hardware_adapt` 在无 torch、无 ComfyUI 的普通 Python 环境可 import；
- `hardware_report()` → `available=True`（本机 nvidia-smi 检出 GTX 1050 Ti，torch 缺失），`patches.status=not_required`（非昇腾，不误伤 NVIDIA）；
- `preflight()` 字段齐全、`report_text()` 输出 26 行（含"未真机验证"声明）；
- 昇腾分支：未导入 `comfy.model_management` 时 `deferred` + 4 条 manual_steps，不抛异常；补丁幂等/缺函数/失败路径均有单测。
- 说明：comfy-kitchen INT8 只做源码扫描，不代改第三方源码；缺 `npu_quant_matmul` 时明确“需人工补”，符合“不假装支持”。

### 1.3 HTTP 路由（本地 ComfyUI 实跑）
- `GET /bettercomfyui/hardware` → 200，`ok=true`，`available=true`，tier=B（本机 nvidia 4GB），patch=not_required；
- `/api` 前缀同时注册（沿用 `_both`）；
- 未在真 aiohttp 环境测试（本机工作区无 aiohttp，测试用假 aiohttp 调用处理器）；ComfyUI 本地 8188 上已验证 200。

### 1.4 一键安装器（dry-run 实测）
- `install_core.py --help` 正常；
- `attach` dry-run：7 步（校验 main.py → 装 torch → requirements → 复制插件/编排包 → 启动器 → 模型目录说明），缺 main.py 只告警不执行，dry-run 不写盘；
- `bootstrap` dry-run：10 步（git 检查/浅克隆 → venv → pip → torch → requirements → 插件 → 启动器 → 模型说明），目标存在非空会拒绝；
- `Ascend` dry-run：先执行 `import torch, torch_npu` 检查（不硬装错 wheel），启动器自动带两个昇腾参数；
- Windows+ROCm / Windows+Ascend 默认拒绝，需 `--allow-experimental`；
- 启动器生成：BAT 带 `chcp 65001`，SH 带 `chmod +x`；失败回滚只回滚本安装器创建项；pip 已装包不自动卸载（明确告知）。
- 未做：真实 pip/clone、模型下载、启动/重启 ComfyUI（本机不具备这些条件，必须标注未验证）。

### 1.5 测试与脚本
- `pytest tests/test_round57_hardware.py -q` → 47 passed；
- `bash -n` 两个 sh、PowerShell `ParseFile` 均通过（由专项执行并报告）；
- `install_core.py --dry-run --json` 临时目录保持为空（无副作用）。

## 2. 发现的问题与处理

| ID | 问题 | 处理 |
|---|---|---|
| HW-1 | 初始 `hardware.py` 用 48GB 当最低门槛，与 `pipeline.core` 的 24GB 不一致 | 专项已改为 24GB 下限 + 48GB 舒适档；Lead 复核通过 |
| HW-2 | AMD Linux 因把 CPU 架构当 OS 比较，永远判 C | 专项已新增 `_os_name()`；Lead 复核 Linux→B、Windows→C |
| HW-3 | ROCm 环境 `torch.cuda.is_available()` 可能误报为 CUDA | 专项已新增 `rocm_available`；复核通过 |
| HW-4 | Windows GBK 控制台 `doctor` 文本因 `⚠` 崩溃 | 专项已改 ASCII 标记 + stdout 错误替换；CLI 复核通过 |
| HW-5 | UI 未显示硬件状态 | Lead 已补：顶部状态 chip + 「硬件与环境（自动检测）」只读面板，浏览器实测 `nvidia · B · 4GB` |
| HW-6 | 安装器真实执行未验证 | 保留为已知限制；发布说明必须写明“dry-run 通过，真实安装未在本机验证” |
| HW-7 | 昇腾/AMD/Intel/Apple 无真机 | 能力分级 B/C，文档/发布说明必须写“未真机验证” |

## 3. 复核结论

- 硬件检测/能力矩阵/安装器 dry-run/插件降级安全性/路由 **代码级通过**；
- UI 已接线并实测；
- **不可宣称“多品牌真机验证通过”**：昇腾、AMD、Intel、Apple 均为代码/文档级适配，未真机验收；
- 昇腾已知需人工确认：CANN/torch_npu 版本、`--disable-mmap --disable-smart-memory`、ComfyUI 设备判断补丁、comfy-kitchen INT8 `npu_quant_matmul` 分支；
- 发布前必须由新的安全/发布专项复核（本记录不替代安全审计）。
