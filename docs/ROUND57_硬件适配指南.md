# ROUND57 · 多品牌硬件适配指南（检测 / 能力矩阵 / 昇腾运行时补丁）

> 适用版本：剧核 DramaCore ROUND57（插件 `bettercomfyui_comfy` + 编排包 `bettercomfyui`）
> 本文是**能力口径与操作手册**，不是「全品牌已验收」的证明。每一条结论后面都标了证据等级。

---

## 0. 先看结论（诚实分层）

| 级别 | 含义 | 品牌/后端 | H3 全流程 | 证据等级 |
|---|---|---|---|---|
| **A** | 官方主路径，项目默认验证 | NVIDIA CUDA（显存 ≥ 24GiB） | 支持 | 项目历史轮次有 RTX 4090 记录（**本轮未复测**）；本机 4GB 卡实测为 B |
| **B** | 可用，但有条件/需补丁 | AMD ROCm **Linux**（显存 ≥ 24GiB）、**Ascend NPU + 两个补丁**（显存 ≥ 24GiB） | 有条件支持 | **未真机验证**；只验证了检测/参数/补丁代码分支 |
| **C** | 有限支持，只承诺轻流程/素材 | Intel XPU、Apple MPS、AMD Windows、低显存 NVIDIA、显存未知的 NPU | 不保证 | **未真机验证** |
| **D** | 不可用于真实 H3 生成 | CPU、无 torch 且无加速卡、驱动未就绪 | 不支持 | 本机（GTX 1050 Ti 4GB + 无 torch）实测为 B/D 边界 |

**本轮的硬事实**：本机是 Windows 10 + NVIDIA GTX 1050 Ti 4GB + **未安装 PyTorch**。
`doctor` 能通过 `nvidia-smi` 认出 NVIDIA、4GB、Tier B，并明确写「未检测到 PyTorch」。
**昇腾/AMD/Intel/Apple 没有任何真机证据**，所有结论止于「代码分支与参数正确」。

显存口径（与 `bettercomfyui/pipeline/core.py` 单一来源）：

* **下限 `H3_MIN_VRAM_MIB = 24_000`（24GiB）**：低于它无法跑真实 H3；
* **舒适档 `H3_RECOMMENDED_VRAM_MIB = 48GiB`**：长段/高分辨率建议值，**不是**硬门槛。
  旧版硬件模块把 48GB 当硬门槛并声称与 pipeline 一致，已在本轮修正
  （否则 40GB 的 A100 会被误判成 B）。

---

## 1. 三个入口，同一份能力矩阵

| 入口 | 命令/URL | 用途 |
|---|---|---|
| CLI | `python -m bettercomfyui.hardware doctor --json` | 维护者自检；贴日志即可定位 |
| HTTP | `GET /bettercomfyui/hardware`（同时注册 `/api/bettercomfyui/hardware`） | 面板/远程查看；`?refresh=1` 强制重探 |
| 启动日志 | ComfyUI 启动时自动打印一行摘要 | 用户不用点任何东西就能看到「为什么不能跑」 |
| 安装器 | `python install/install_core.py --dry-run ...` | 装之前就知道会装什么、用什么 torch 源 |
| 运行时 | `python -m bettercomfyui_comfy.hardware_adapt patches` | 看昇腾补丁逐项状态 |

启动摘要示例（本机真实输出）：

```text
[剧核 DramaCore] 硬件适配摘要
  设备  : nvidia / cuda · NVIDIA GeForce GTX 1050 Ti · 4GB
  分级  : B · 可用，但需要满足条件/补丁 · H3 全流程=不保证/不可用
  注意  : 未检测到 PyTorch：请先按安装器/ComfyUI 要求安装对应后端的 torch。
  详情  : /bettercomfyui/hardware
```

---

## 2. 检测矩阵（代码覆盖 vs 真机）

| 场景 | 检测依据 | 期望结果 | 测试证据 |
|---|---|---|---|
| NVIDIA 大显存（A100 80GB） | `torch.cuda` | vendor=nvidia, tier=A, full=True | mock probe 单测 |
| NVIDIA 24–48GB | `torch.cuda` / `nvidia-smi` | tier=A + 「低于舒适档」提示 | mock probe 单测 |
| NVIDIA 小显存（4–16GB） | `torch.cuda` / `nvidia-smi` | tier=B, full=False, asset=True | mock probe 单测 + **本机实测** |
| Ascend 910B（64GB，torch_npu 可用） | `torch.npu` | tier=B, `--disable-mmap --disable-smart-memory`, 两个必需补丁写入 `patches_required` | mock probe 单测；**无真机** |
| Ascend 显存未知 | `npu-smi` | tier=C（不猜显存）, 仍给出启动参数 | mock probe 单测；**无真机** |
| AMD Linux + ROCm 7.1（64GB+） | `torch.version.hip` / `rocminfo` | vendor=amd, tier=B | mock probe 单测；**无真机** |
| AMD Windows | `torch.version.hip` | tier=C（实验性），警告建议 Linux/WSL2 | mock probe 单测；**无真机** |
| Intel XPU | `torch.xpu` / `xpu-smi` | tier=C | mock probe 单测；**无真机** |
| Apple MPS（64GB 统一内存） | `torch.backends.mps` / `system_profiler` | tier=C, full=False, `--force-fp16` | mock probe 单测；**无真机** |
| CPU | 无加速器 | tier=D, `--cpu` | mock probe 单测 |
| 有卡但没 torch | `nvidia-smi` / `npu-smi` / `rocminfo` / `lspci` | 仍识别品牌/型号/显存；明确警告缺 torch | **本机实测** + mock 单测 |

检测模块的行为约定：

* 任何探测失败都收敛为「未知/不可用 + 原因」，**不抛异常**（ComfyUI 启动不能被拖垮）；
* `probe=` 参数可注入假数据，单测不需要真卡；
* Tier 是**项目口径**，检测值是**运行时事实**，两者在 JSON 里分开（`tier` vs `evidence`）；
* 命令行兜底顺序：`nvidia-smi` → `npu-smi` → `rocminfo` → `xpu-smi` → `system_profiler`(macOS) → `lspci`(Linux)；
* ROCm 版 torch 的 `torch.cuda.is_available()` 也为真，因此单独记录 `rocm_available`，
  否则 AMD 会被误报成 NVIDIA（本轮修）。

### 本轮修掉的三个真 bug（都有单测守卫）

1. **48GB 假口径**：`hardware.py` 写死 48GB 并声称与 `pipeline.core.H3_MIN_VRAM_MIB` 一致，
   实际 pipeline 是 24GiB → 已改为同源 24_000，48GB 降级为「舒适档建议」。
2. **AMD Linux 永远不是 B**：Tier 判定把 `platform.machine()`（CPU 架构 `x86_64`/`AMD64`）
   当成操作系统去比 `"linux"` → 已改为用 `platform.platform()` 归一出的 OS 名。
3. **`doctor` 文本模式在 Windows GBK 控制台崩溃**：报告里的 `⚠`（U+26A0）无法用 cp936 编码，
   直接 `UnicodeEncodeError` 退出码 1 → 已把标记换成 ASCII `!`，并给 stdout 加 `errors="replace"` 兜底。

---

## 3. 逐品牌适配步骤

> 以下命令都在「ComfyUI 所在 Python 环境」里执行。**安装器会自动做 torch 源选择**，
> 这里给的是手动核对步骤，便于排障与离线部署。

### 3.1 NVIDIA CUDA（A · 主路径）

```bash
# 1) 驱动与卡
nvidia-smi                     # 能看到卡与 CUDA Version 即可，不要求本机装完整 CUDA Toolkit

# 2) PyTorch（cu126 索引已实测可访问，见文末来源）
python -m pip install --upgrade torch torchvision torchaudio \
  --index-url https://download.pytorch.org/whl/cu126

# 3) 校验
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))"
python -m bettercomfyui.hardware doctor --json
```

* 显存 ≥ 24GiB 才是 A；24–48GiB 会提示「低于舒适档」；
* 新卡（Blackwell 等）如 cu126 无对应 wheel，用 `--torch-index` 换成 cu128/cu130 等索引；
* Windows / Linux 都可，安装器默认走 CUDA 主路径。

### 3.2 AMD ROCm（Linux B / Windows C 实验性）

```bash
# Linux only：先按 ROCm 官方文档装驱动/运行时，再装 ROCm 版 torch
rocminfo | grep -E "Name:.*gfx|Marketing Name"   # 记下 gfx 型号
python -m pip install --upgrade torch torchvision torchaudio \
  --index-url https://download.pytorch.org/whl/rocm7.1
python -c "import torch; print(torch.version.hip, torch.cuda.is_available())"
```

* **Windows 官方 ROCm wheel 不存在**（索引里只有 manylinux），安装器默认**拒绝**执行，
  需显式 `--allow-experimental`；建议 WSL2/Linux 双系统；
* 若 ROCm 不原生支持该 gfx，可尝试 `HSA_OVERRIDE_GFX_VERSION=11.0.0`（**不保证**，会写进报告警告）；
* 启动环境变量：`PYTORCH_HIP_ALLOC_CONF=expandable_segments:True`（安装器启动器已带）。

### 3.3 Intel XPU（C）

```bash
xpu-smi discovery              # 能看到设备
python -m pip install --upgrade torch torchvision torchaudio \
  --index-url https://download.pytorch.org/whl/xpu
python -c "import torch; print(torch.xpu.is_available(), torch.xpu.get_device_name(0))"
```

* 需要较新的 Intel 驱动与 Python；Arc 显存通常不足以跑 H3 全流程，只承诺素材/轻流程；
* 安装器会注入 `SYCL_CACHE_PERSISTENT=1`。

### 3.4 Apple MPS（C）

```bash
# Apple Silicon（arm64）直接用 PyPI 默认 wheel
python -m pip install --upgrade torch torchvision torchaudio
python -c "import torch; print(torch.backends.mps.is_available())"
```

* 统一内存**不等于**能跑 48GB 级 H3；能力矩阵明确 `full_h3_supported=False`；
* 启动参数 `--force-fp16`，优先素材/轻流程；
* Intel Mac 无 MPS，按 CPU（D）处理。

### 3.5 华为昇腾 Ascend NPU（B · 有条件，**未真机验证**）

这是本轮最需要「说清楚边界」的部分。

#### 3.5.1 必须由用户先准备的两件事

1. **CANN**（昇腾计算语言/驱动运行时）：按昇腾官方文档安装与固件/驱动匹配的 CANN；
2. **torch + torch_npu**：版本必须与 CANN、Python 匹配（用 `npu-smi info` 确认卡可见）。

安装器**不代装 CANN**，也不从 PyPI 装 torch（PyPI 的 torch 不是 NPU 版）。它会：

* 生成检查步骤：`python -c "import torch, torch_npu; ..."`（失败不阻断，但会警告）；
* 生成带 `--disable-mmap --disable-smart-memory` 的启动器；
* 注入 `PYTORCH_NPU_ALLOC_CONF=expandable_segments:True`；
* 进 ComfyUI 后自动尝试设备保护补丁（见 3.5.3），并在 `/bettercomfyui/hardware` 汇报逐项状态。

#### 3.5.2 社区关键启动参数

```bash
python main.py --disable-mmap --disable-smart-memory
```

原因（社区经验，非本项目真机结论）：

* 文本编码器在 mmap 加载方式下可能卡住 → `--disable-mmap`；
* smart memory 在 NPU 上行为异常/显存回收不及时 → `--disable-smart-memory`。

这两项已写进能力矩阵的 `launch_args`，安装器与节点都复用同一份，不会出现「文档一套、启动器另一套」。

#### 3.5.3 ComfyUI 设备保护补丁（插件自动做）

**问题**：昇腾环境里 `torch_npu` 提供 CUDA 兼容层，ComfyUI 的
`comfy.model_management.is_nvidia()` 可能返回 True，于是 FP8 / `torch.cuda` 分支被走到 NPU 上，
表现为报错、回退 CPU（极慢）或加载卡死。

**插件做的事**（`bettercomfyui_comfy/hardware_adapt.py`）：

| 函数 | 补丁方式 | NPU 活跃时 | 非 NPU 设备 |
|---|---|---|---|
| `comfy.model_management.is_nvidia()` | 运行时包裹（不改源码） | 返回 `False` | **原样调用原函数** |
| `comfy.model_management.supports_fp8_compute()` | 同上，兼容 0 参/1 参签名 | 返回 `False` | **原样调用原函数** |

约束：

* **幂等**：包裹函数带 `_dramacore_npu_guard` 标记，重复调用不会层层套娃（有单测）；
* **可关闭**：`BETTERCOMFYUI_NO_ASCEND_PATCHES=1` 只关补丁；`BETTERCOMFYUI_DISABLE_HARDWARE=1` 关整个适配层；
* **不静默**：每个函数记录 `applied / already_applied / missing / failed` 状态与原因；
* **不越界**：绝不修改 ComfyUI 源码，不碰 `torch` 内部实现；
* 函数缺失（旧版 ComfyUI）→ 状态 `missing` + `partial`，并给手动步骤，而不是假装成功。

#### 3.5.4 comfy-kitchen INT8：只检测，不代改

`comfy-kitchen` 是 ComfyUI requirements 里的依赖（本项目当前锁 `0.2.37`）。
其 INT8 GEMM 走 `torch._int_mm`，**该算子在 NPU 上不可用**；昇腾需要
`torch_npu.npu_quant_matmul` 一类的分支。

插件做的是**只读检测**：扫描已安装 `comfy_kitchen` 的源码文本，看是否同时出现
`torch._int_mm` / `npu_quant_matmul`，并给出三种状态：

| 状态 | 含义 | 建议 |
|---|---|---|
| `not_installed` | 没装 comfy-kitchen | 若流程用 INT8，先按 ComfyUI 要求安装 |
| `needs_npu_branch` | 只有 `torch._int_mm` | 需要补 `npu_quant_matmul` 分支，否则报错或回退 CPU |
| `npu_branch_present` | 源码中出现 `npu_quant_matmul` | 具备分支迹象，**仍需真机跑通才算数** |

**为什么不让插件自动改**：第三方库的 INT8 实现涉及版本、dtype、量化 scale、算子语义，
盲改会制造「看起来装好了、实际算错」的假成功。本项目宁可明确报「需手动」，也不伪造能力。

#### 3.5.5 验收清单（昇腾用户照做）

```bash
# 1) 卡与驱动
npu-smi info

# 2) Python 侧
python -c "import torch, torch_npu; print(torch.__version__, torch_npu.__version__, torch.npu.is_available())"

# 3) 能力矩阵
python -m bettercomfyui.hardware doctor

# 4) 进 ComfyUI 后看补丁
curl http://127.0.0.1:8188/bettercomfyui/hardware
python -m bettercomfyui_comfy.hardware_adapt patches

# 5) 小分辨率/短段跑通，再逐步加长
```

只有 2)–5) 都通过，才能把该台昇腾机器记为「可用」；本项目目前**没有**这样的记录。

### 3.6 CPU（D）

* 只能跑编排/审核/剪辑/占位；真实 H3 生成不可用；
* 启动参数 `--cpu`；报告会明确 `full_h3_supported=False`、`asset_generation_supported=False`。

---

## 4. 运行时补丁状态机

`GET /bettercomfyui/hardware` 的 `patches` 字段（昇腾路径）：

| status | 触发条件 | 用户动作 |
|---|---|---|
| `not_required` | 非昇腾设备 | 无 |
| `applied` | 两个函数都成功包裹 | 无需操作，继续验证生成 |
| `already_applied` | 之前已包裹过（幂等） | 无 |
| `partial` | 至少一个函数 `missing`/`failed` | 按 `manual_steps` 手动加设备判断 |
| `deferred` | 当前不在 ComfyUI 进程（无 `comfy.model_management`） | 进 ComfyUI 后再查；会自动重试 |
| `disabled_by_env` | `BETTERCOMFYUI_NO_ASCEND_PATCHES=1` | 按 `manual_steps` 手动处理 |

`comfy_kitchen` 字段独立汇报（见 3.5.4），不参与补丁成功与否的判定。

---

## 5. HTTP 接口

```http
GET /bettercomfyui/hardware
GET /api/bettercomfyui/hardware        # 前端 fetchApi() 会自动加 /api，两个前缀都注册
GET /bettercomfyui/hardware?refresh=1  # 换卡/装完驱动后强制重探
```

返回关键字段：

| 字段 | 说明 |
|---|---|
| `available` | 硬件适配层是否可用（编排包缺失时为 false，仍返回结构化 JSON） |
| `report` | 完整检测报告（`vendor/backend/tier/vram_mib/warnings/evidence/env/launch_args`） |
| `patches` | 运行时补丁状态（`status/items/comfy_kitchen/manual_steps`） |
| `preflight` | 节点用的精简字段（`full_h3_supported/patches_status/...`） |
| `report_text` | 中文人读报告（含「未真机验收」声明） |
| `notice` | 固定免责声明 |

处理器薄、逻辑在 `hardware_adapt.route_payload()`：即使探测异常也返回 JSON 错误，
**不会 500 白屏**（有单测真实调用处理器）。

---

## 6. 排障顺序（按这个顺序贴日志）

```bash
python -m bettercomfyui.hardware doctor --json        # 1. 硬件/能力矩阵
python -m bettercomfyui_comfy.hardware_adapt json     # 2. 插件运行时视角 + 补丁
python -m bettercomfyui_comfy.hardware_adapt patches  # 3. 只看补丁
curl "http://127.0.0.1:8188/bettercomfyui/hardware?refresh=1"  # 4. 服务进程视角
```

常见结论：

* `vendor=nvidia` + `torch_available=false`：装了卡没装 torch → 用安装器按后端装；
* `vendor=ascend` + `patches.status=partial`：ComfyUI 版本缺函数 → 按 `manual_steps` 手改；
* `comfy_kitchen.status=needs_npu_branch`：INT8 会失败/回退 CPU → 需要 NPU 分支；
* `tier=C` + `full_h3_supported=false`：不要承诺出片，先用素材/轻流程验证链路。

---

## 7. 已验证 / 未验证（请勿混用）

**已验证（本机 Windows 10 + GTX 1050 Ti 4GB + 无 torch）**

* `doctor --json` 与文本报告都能跑通（文本模式 GBK 崩溃已修）；
* 启动摘要、`/bettercomfyui/hardware` 处理器、安装器 dry-run、47 项单测；
* NVIDIA A/B、Ascend B/C、AMD Linux B / Windows C、Intel C、Apple C、CPU D 的**代码分支**
  与 JSON 字段（mock probe）；
* 昇腾补丁的包裹、幂等、非 NPU 保持原行为（假 `comfy.model_management`）；
* 安装器计划包含正确 torch 索引、昇腾启动参数、回滚删除复制产物。

**未验证（没有任何真机证据）**

* 昇腾 910B/310P 上的实际加载、算子、显存、耗时；
* 真机 ComfyUI 环境里 `is_nvidia()` 原本是否真的误判（补丁逻辑本身有单测，触发条件靠社区经验）；
* `comfy-kitchen` 在 NPU 上的真实 INT8 行为与 `npu_quant_matmul` 分支正确性；
* AMD ROCm 7.1 / Intel XPU / Apple MPS 的真实 H3 出片能力；
* 安装器在真实网络下完整装完 torch + ComfyUI（本轮按约束不联网安装）。

---

## 8. 引用来源

**PyTorch wheel 索引（本轮实测 HTTP 200，2026-10）**

* CUDA 12.6：<https://download.pytorch.org/whl/cu126/>
* ROCm 7.1：<https://download.pytorch.org/whl/rocm7.1/>
  （目录内可见 `torch-2.10.0+rocm7.1`、`torch-2.11.0+rocm7.1` 等 manylinux wheel）
* Intel XPU：<https://download.pytorch.org/whl/xpu/>

**官方文档**

* PyTorch 安装选择器：<https://pytorch.org/get-started/locally/>
* PyTorch MPS 说明：<https://pytorch.org/docs/stable/notes/mps.html>
* PyTorch XPU 入门：<https://pytorch.org/docs/stable/notes/get_start_xpu.html>
* ROCm Linux 安装：<https://rocm.docs.amd.com/en/latest/install/rocm.html>
* ComfyUI 手动安装：<https://docs.comfy.org/installation/manual_install>
* ComfyUI 仓库（requirements / 版本）：<https://github.com/comfyanonymous/ComfyUI>
* 昇腾 CANN 文档：<https://www.hiascend.com/document>
* torch_npu（昇腾 PyTorch 适配）：<https://gitee.com/ascend/pytorch> ·
  <https://github.com/Ascend/pytorch>

**社区资料（外部、未由本项目复现，仅作线索）**

* comfy-kitchen INT8 实现：<https://github.com/Comfy-Org/comfy-kitchen/blob/main/comfy_kitchen/tensor/int8.py>
* comfy-kitchen INT8 issue：<https://github.com/Comfy-Org/comfy-kitchen/issues/9>
* ComfyUI NPU 相关上游 PR（异步 offload/stream）：<https://github.com/Comfy-Org/ComfyUI/pull/16057>
* 社区 Ascend INT8 测试仓库：<https://github.com/Aucannot/ComfyUI-Ascend-Int8-Test>

> 外部链接可能随上游更新而移动；本项目只保证「代码里写的参数/状态与本文一致」，
> 不保证外部仓库内容永久有效，也不把社区说法当作真机结论。
