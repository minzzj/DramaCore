# ROUND57 · 一键安装部署指南（attach / bootstrap）

> 适用：剧核 DramaCore ROUND57，安装器 `install/install_core.py`
> 目标：在 **NVIDIA CUDA（主路径）/ AMD ROCm / Intel XPU / Apple MPS / 昇腾 Ascend NPU / CPU**
> 上把 ComfyUI + 剧核插件装起来，并生成**带正确启动参数**的启动器。
>
> **本轮没有联网执行真实安装**（按任务约束），也没有启动/重启 ComfyUI。
> 所有「能装」的结论来自计划生成、dry-run、语法解析与单测，不代表已在每类真机上装通。

---

## 0. 一分钟快速开始

```bash
# 0) 先看计划（不写磁盘、不跑 pip、不联网）
python install/install_core.py --mode attach --comfyui D:/AI/ComfyUI --dry-run

# 1) 确认后真的装（非交互环境必须 --yes）
python install/install_core.py --mode attach --comfyui D:/AI/ComfyUI --backend auto --yes

# 2) 全新机器：clone + venv + 装依赖（目标目录必须不存在或为空）
python install/install_core.py --mode bootstrap --comfyui D:/AI/ComfyUI --backend cuda --yes
```

Windows / Linux / macOS 的包装脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 `
  -Mode attach -ComfyUI "D:\AI\ComfyUI" -DryRun
```

```bash
# Linux（ROCm/CUDA/昇腾）
./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --dry-run
BACKEND=rocm YES=1 ./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI

# macOS（Apple Silicon → MPS）
./install/install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --dry-run
```

---

## 1. 前置条件

| 项 | 要求 | 检查 |
|---|---|---|
| Python | 3.9+（安装器与 ComfyUI 共用环境） | `python -V` |
| ComfyUI | attach 模式需已有 `main.py`；bootstrap 需 `git` | `Test-Path D:/AI/ComfyUI/main.py` |
| 磁盘 | H3 文本编码器 34GB 级 + 扩散权重 + VAE，建议 ≥ 150GB 空闲 | — |
| 网络 | 真实安装会访问 PyPI / `download.pytorch.org`；dry-run 不联网 | — |
| 驱动 | 按后端准备（NVIDIA 驱动 / ROCm / XPU / CANN） | `nvidia-smi` 等 |
| ComfyUI 状态 | **安装前先停止 ComfyUI**（避免加载到半装的插件） | — |

安装器只依赖 Python 标准库；能导入 `bettercomfyui.hardware` 时会复用它做能力分级，
导入失败则用内置兜底表（所以只拷 `install/` 目录也能工作）。

---

## 2. 两种模式

| 行为 | `attach`（装到已有 ComfyUI） | `bootstrap`（从零装） |
|---|---|---|
| 目标目录 | 必须已有 `main.py` | 必须不存在或为空；已有 `main.py` 会拒绝 |
| Python | 优先 `ComfyUI/venv/…/python`，否则当前解释器 | 先 `python -m venv ComfyUI/venv`，后续都用 venv |
| torch | 按后端选索引安装 | 同左 |
| ComfyUI 依赖 | `pip install -r requirements.txt`（缺失则跳过并警告） | 同左 |
| 插件 | 复制到 `custom_nodes/bettercomfyui_comfy` | 同左 |
| 编排包 | 复制到 `custom_nodes/bettercomfyui_comfy/bettercomfyui`（`_bridge` 可直接找到） | 同左 |
| 启动器 | 写 `start_dramacore.bat/.sh` | 同左 |
| 模型 | **不下载**，只打印目录指引 | 同左 |

> `bootstrap` 用 `git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git`。
> 如果目标目录里有你自己的文件，安装器会拒绝，**不会**在陌生目录里乱写。

---

## 3. 后端 → torch 安装源（代码里的单一映射）

| `--backend` | torch 源 | 默认命令（安装器实际生成） |
|---|---|---|
| `cuda`（auto 探测到 NVIDIA） | `https://download.pytorch.org/whl/cu126` | `python -m pip install --upgrade torch torchvision torchaudio --index-url …/cu126` |
| `rocm`（Linux） | `https://download.pytorch.org/whl/rocm7.1` | 同上换 `…/rocm7.1` |
| `xpu` | `https://download.pytorch.org/whl/xpu` | 同上换 `…/xpu` |
| `mps` | PyPI 默认 | `python -m pip install --upgrade torch torchvision torchaudio` |
| `ascend` | **不用 PyPI torch** | 只做 `import torch, torch_npu` 检查 + CANN 文档指引 |
| `cpu` | PyPI 默认 | `pip install --upgrade torch …` |

**索引可访问性（本轮实测，2026-10）**

* `cu126` → HTTP 200
* `rocm7.1` → HTTP 200，目录内确认存在 `torch-2.10.0+rocm7.1` / `2.11.0+rocm7.1` 等 manylinux wheel
* `xpu` → HTTP 200

如需其它 CUDA 版本（新卡）：

```bash
python install/install_core.py --mode attach --comfyui D:/AI/ComfyUI \
  --backend cuda --torch-index https://download.pytorch.org/whl/cu128 --yes
```

`--torch-index` 只覆盖本次进程的映射，不写任何配置文件。

### 3.1 自动探测顺序

`--backend auto` 时按以下顺序探测（取第一个命中的）：

1. `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader` → `cuda`
2. `npu-smi info` → `ascend`（并从 HBM 字段解析显存，如 `0 / 65536` → 65536MiB）
3. `rocminfo`（含 `gfx`/`amd`） → `rocm`
4. `xpu-smi discovery` → `xpu`
5. macOS + arm64 + `system_profiler` → `mps`
6. `lspci` 兜底：NVIDIA/AMD/Intel → 对应后端，但会明确警告「驱动/运行时未就绪」
7. 都没有 → `cpu`，并警告真实生成不可用

显式 `--backend` 时仍会探测一次作为证据；若你选的卡和探测到的不是同一类，
安装器**不会**把另一张卡的显存/警告套到你头上（例如在 NVIDIA 机器上规划昇腾安装）。

---

## 4. 参数表

| 参数 | 说明 |
|---|---|
| `--mode attach\|bootstrap` | 默认 `attach` |
| `--comfyui PATH` | ComfyUI 目录。未给时依次查 `COMFYUI_PATH`、`COMFYUI_DIR`、`./.comfyui`、`./ComfyUI`、`./comfyui` |
| `--backend auto\|cuda\|rocm\|xpu\|mps\|ascend\|cpu` | 也接受别名 `nvidia/hip/amd/intel/ipex/apple/metal/npu/cann` |
| `--python PATH` | 指定解释器（默认 ComfyUI venv 或当前解释器） |
| `--torch-index URL` | 覆盖 PyTorch 索引 |
| `--dry-run` | 只打印计划；**不建目录、不跑 pip、不联网** |
| `--yes` | 跳过确认；非交互环境（CI/脚本）真实安装必须给 |
| `--force` | 目标已存在时**先备份**再覆盖（默认跳过、不覆盖） |
| `--skip-torch` | 已有可用 torch 时跳过 torch 安装 |
| `--skip-launcher` | 不生成启动器 |
| `--keep-partial` | 失败时保留中间产物（默认回滚） |
| `--allow-experimental` | 允许 Windows + ROCm / Windows + 昇腾（默认拒绝） |
| `--json` | 机器可读输出（dry-run 输出完整计划） |
| `--version` | 版本 |

退出码：`0` 成功 / `2` 参数或确认问题 / `3` 环境不满足 / `4` 执行失败（已回滚）。

---

## 5. 安装器会写什么、不会写什么

**会写**

* `custom_nodes/bettercomfyui_comfy/`（插件；已存在且未 `--force` 时**跳过**）
* `custom_nodes/bettercomfyui_comfy/bettercomfyui/`（编排包，便于 `_bridge` 定位）
* `start_dramacore.bat` / `start_dramacore.sh`
* bootstrap 模式：`ComfyUI/` 克隆目录与 `ComfyUI/venv/`
* `--force` 覆盖时：`<目标>.bak-YYYYmmdd-HHMMSS` 备份目录

**不会写**

* 不下模型、不建 `models/*` 子目录、不写 `extra_model_paths.yaml`
* 不改 ComfyUI 源码、不 patch 第三方库源码
* 不启动/重启 ComfyUI
* 不自动卸载 pip 包（回滚只撤目录/文件；pip 安装会明确说明「不自动卸载」）
* 不在非空且不是 ComfyUI 的目录里 clone

---

## 6. 启动器

### Windows（`start_dramacore.bat`）

```bat
@echo off
chcp 65001 >nul
rem 剧核 DramaCore 启动器 / backend=cuda / tier=A / 由 install_core.py 生成，可按需修改
setlocal
set "BETTERCOMFYUI_HOME=H:\...\Better comfyUI"
"D:\AI\ComfyUI\venv\Scripts\python.exe" "D:\AI\ComfyUI\main.py"
pause
```

昇腾会自动多出：

```bat
set "PYTORCH_NPU_ALLOC_CONF=expandable_segments:True"
"...\python.exe" "...\main.py" --disable-mmap --disable-smart-memory
```

> 实现细节：BAT 按 UTF-8 写并 `chcp 65001`，否则含中文的路径（本仓库路径就含中文）
> 在 cmd 默认 GBK 下会找不到文件；注释只允许单行，避免漏 `rem` 被当命令执行。
> 以上两点都有测试守卫。

### Linux / macOS（`start_dramacore.sh`）

```bash
#!/usr/bin/env bash
# 剧核 DramaCore 启动器 / backend=rocm / tier=B
set -euo pipefail
export PYTORCH_HIP_ALLOC_CONF='expandable_segments:True'
export BETTERCOMFYUI_HOME='/opt/Better comfyUI'
'/opt/ComfyUI/venv/bin/python' '/opt/ComfyUI/main.py'
```

生成时会给 `.sh` 加执行位（`0o755`）。

---

## 7. 逐平台步骤

### 7.1 Windows（NVIDIA CUDA / AMD 实验 / CPU）

```powershell
# 1) 驱动确认
nvidia-smi
# 2) 计划（推荐先跑）
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 `
  -Mode attach -ComfyUI "D:\AI\ComfyUI" -Backend cuda -DryRun
# 3) 执行
powershell -ExecutionPolicy Bypass -File .\install\install_windows.ps1 `
  -Mode attach -ComfyUI "D:\AI\ComfyUI" -Backend cuda -Yes
```

* 包装脚本会自动找 `python`，找不到再试 `py -3`；
* AMD + Windows 默认**拒绝**（ROCm wheel 仅 Linux），确认要试需 `-AllowExperimental`；
* 昇腾 + Windows 同样默认拒绝，建议 WSL2/Linux。

### 7.2 Linux

```bash
chmod +x install/install_linux.sh

# NVIDIA
BACKEND=cuda ./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes

# AMD ROCm（先按 ROCm 官方文档装驱动）
BACKEND=rocm ./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes

# 昇腾（先装 CANN + torch_npu，安装器只检查）
BACKEND=ascend ./install/install_linux.sh --mode attach --comfyui /opt/ComfyUI --yes
```

包装脚本会逐个尝试 `python3` / `python3.12` / … / `python`，并**真的执行一次版本检查**
（部分系统的 `python3` 只是 Microsoft Store 占位符，直接退出 49，会被跳过）。

### 7.3 macOS

```bash
chmod +x install/install_macos.sh
./install/install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --yes
```

* Apple Silicon → `mps`（PyPI 默认 wheel）；
* Intel Mac → 自动探测为 `cpu`，安装器会提示 MPS 不可用；
* 不承诺 H3 48GB 级全流程，先跑素材/轻流程。

### 7.4 昇腾 Ascend（**未真机验证**）

安装器**不做**：装 CANN、装 torch_npu、改 comfy-kitchen 源码。
安装器**会做**：检查能否 `import torch, torch_npu`、生成正确启动器、进 ComfyUI 后自动加设备保护补丁。

手动顺序：

```bash
# 1) 按昇腾官方文档安装 CANN（版本与驱动匹配）
# 2) 安装匹配的 torch / torch_npu（不要用 PyPI 的普通 torch）
python -c "import torch, torch_npu; print(torch.__version__, torch_npu.__version__, torch.npu.is_available())"
npu-smi info

# 3) 用安装器装 ComfyUI 依赖与插件（torch 步骤只做检查）
python install/install_core.py --mode attach --comfyui /opt/ComfyUI \
  --backend ascend --skip-torch --yes

# 4) 启动（启动器已带参数；手动等价命令）
python main.py --disable-mmap --disable-smart-memory
```

进 ComfyUI 后检查：

```bash
curl "http://127.0.0.1:8188/bettercomfyui/hardware"
```

关注 `patches.status`：

* `applied` / `already_applied`：设备保护已生效；
* `partial` / `deferred` / `disabled_by_env`：按 `manual_steps` 处理；
* `comfy_kitchen.status=needs_npu_branch`：INT8 需补 `npu_quant_matmul` 分支（插件不代改）。

详见 [ROUND57_硬件适配指南.md](ROUND57_硬件适配指南.md) 第 3.5 节。

---

## 8. 模型目录（不自动下载）

安装完成后，启动器同目录会打印以下指引；权重由用户自己放：

```text
<ComfyUI>/models/diffusion_models   H3 扩散模型权重
<ComfyUI>/models/text_encoders      文本编码器（34GB 级）
<ComfyUI>/models/vae                VAE
<ComfyUI>/models/loras              LoRA（可选）
<ComfyUI>/models/checkpoints        旧式单文件 checkpoint（可选）
```

未放权重时：编排/审核/剪辑可用，真实生成会**明确报缺权重**，不会用占位结果冒充实拍。

---

## 9. 失败与回滚

| 失败点 | 行为 |
|---|---|
| 目标不是 ComfyUI（attach） | 第 1 步 `check_path` 失败，退出码 3，**不写任何东西** |
| 目标已有 `main.py`（bootstrap） | 直接拒绝，提示改用 `--mode attach` |
| 目标非空且不是 ComfyUI（bootstrap） | 直接拒绝，避免污染 |
| git 不可用 | 第 1 步失败，退出码 4，无中间产物 |
| pip 网络失败 | 退出码 4；回滚已建的 venv/目录；**已下载的包不会卸载**（明确说明） |
| 复制插件时目标已存在 | 默认跳过并警告；`--force` 才备份覆盖 |
| 用户 Ctrl+C | 捕获后执行回滚（clone/venv/复制/启动器），退出码 4 |
| 想保留中间产物排障 | 加 `--keep-partial` |

回滚只撤安装器**自己创建**的东西，且逐条打印「已回滚 / 回滚失败」。
`--force` 覆盖前的备份目录不会自动删除，便于找回。

---

## 10. 本轮验收命令与结果

```bash
python -B -m pytest tests/test_round57_hardware.py -q
python -B -m py_compile install/install_core.py bettercomfyui_comfy/hardware_adapt.py
python -B install/install_core.py --help
python -B install/install_core.py --mode attach --comfyui <临时目录> --backend cuda --dry-run
bash -n install/install_linux.sh && bash -n install/install_macos.sh
# PowerShell：[System.Management.Automation.Language.Parser]::ParseFile(...)
```

本轮实际结果：

* `pytest tests/test_round57_hardware.py -q` → **47 passed**；
* `py_compile` 全部通过；
* `install_core.py --help` 正常，退出码 0；
* `--dry-run` 在临时目录可跑，且**临时目录仍为空**（不落地）；
* `bash -n` 两个脚本通过（Git Bash：`C:\Program Files\Git\bin\bash.exe`，不在 PATH 里）；
* PowerShell 解析器 `ParseFile` 无错误；`install_windows.ps1 -DryRun` 实际跑通；
* `bash install_linux.sh ... --dry-run` 实际跑通（自动跳过 Store 占位 `python3`，选到可用解释器）。

**未做**：没有执行真实 pip 安装、没有 clone、没有下载模型、没有启动/重启 ComfyUI、
没有发布到 GitHub。因此「各品牌都能装通」仍需真机验收，不在本轮结论内。

验收时额外的检查点：

* 临时目录为空（`--dry-run` 无副作用）；
* 计划里的 torch 索引与后端匹配（`cu126` / `rocm7.1` / `xpu`）；
* 昇腾计划包含 `--disable-mmap --disable-smart-memory` 与 `PYTORCH_NPU_ALLOC_CONF`；
* 复制步骤失败后目标目录被真的删除（回滚单测）。

---

## 11. 已知限制（诚实清单）

1. **没有任何昇腾/AMD/Intel/Apple 真机安装记录**；参数来自项目口径与社区资料；
2. AMD + Windows 官方无 wheel，安装器默认拒绝；`--allow-experimental` 也不保证成功；
3. 昇腾的 CANN/torch_npu 必须用户自己装，安装器只检查；版本不匹配时它会明确报失败；
4. comfy-kitchen 的 INT8 NPU 分支需要人工补，安装器不代改第三方源码；
5. 安装器不 `pip uninstall`，失败回滚只撤目录/文件（避免破坏用户已有环境）；
6. `bootstrap` 只支持 git 克隆官方 ComfyUI 仓库，不处理私有镜像/代理认证；
7. 模型权重、`extra_model_paths.yaml`、端口/监听参数都**不由安装器**决定；
8. 本轮未联网执行完整装包，pip 源的真实下载速度/可用性未测（仅确认索引 URL 可访问）。

---

## 12. 引用来源

* ComfyUI 手动安装（官方文档，实测 HTTP 200）：<https://docs.comfy.org/installation/manual_install>
* ComfyUI 仓库与 requirements：<https://github.com/comfyanonymous/ComfyUI>
* ROCm Linux 安装：<https://rocm.docs.amd.com/en/latest/install/rocm.html>
* PyTorch CUDA 12.6 索引：<https://download.pytorch.org/whl/cu126/>
* PyTorch ROCm 7.1 索引：<https://download.pytorch.org/whl/rocm7.1/>
* PyTorch XPU 索引：<https://download.pytorch.org/whl/xpu/>
* PyTorch 安装选择器：<https://pytorch.org/get-started/locally/>
* 昇腾 CANN 文档：<https://www.hiascend.com/document>
* torch_npu：<https://gitee.com/ascend/pytorch> · <https://github.com/Ascend/pytorch>
* 硬件适配与补丁细节：[ROUND57_硬件适配指南.md](ROUND57_硬件适配指南.md)
