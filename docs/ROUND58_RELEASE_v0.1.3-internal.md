# DramaCore / 剧核 v0.1.3-internal 发布说明（内测版）

> ⚠️ **内测版 v0.1.3-internal · 未完成完整 GPU 真机验收**
> 本版可用于内部测试与预览，但**尚未在目标 GPU 机器上完成端到端验收**（远端 4090-48G 实例当前不可租用，本轮全程无 GPU）。
> 请**不要**把它当作稳定生产版本；重要项目请自行充分测试后再用。
> 已知限制与「未验证项」见 §5，**我们不会把未验证的东西写成通过**。

---

## 一、一句话说明本版做了什么

- **ROUND55**：补齐成片后期能力 —— 片头片尾、硬字幕、规则引擎自动转场、跨批尾帧连续性、素材版本回滚。
- **ROUND56**：易用性重做 —— 多段剧本动态列表、转场自动决策、默认值收敛并加防误调蒙版、H3 手册全文进系统提示词、提示词可视化。
- **ROUND57**：新增硬件自检与多品牌适配矩阵（**代码级交付，尚未接线到 UI / HTTP / CLI**，也不代表已在对应硬件上验证）。
- **ROUND58**：修掉一个会导致参数整体错位的 P0（工作流 widget 槽位 112 → 119），并补上历史三次内测包一直缺失的 `config/default.yaml`。

---

## 二、新增功能

### 2.1 ROUND55：后期链路

| 能力 | 说明 |
|---|---|
| **片头片尾** | 可挂片头/片尾片段拼接到成片；记录片头/片尾实际时长，并写入「段时长列表JSON」，供审核按真实边界切段 |
| **硬字幕（程序对齐）** | 按 ffmpeg 时间网格烧录硬字幕（不依赖 ASR / 大模型）；文本来源优先级：`字幕台词` → 剧本引号台词程序化提取 → 视频提示词（并明确告警） |
| **规则引擎自动转场** | `transition_planner`：pin 连续段强制轻转场/硬切、场景切换→叠化、时间跳跃→黑场、边界高相似→硬切；决策逐条写入报告 `transition_decision`，UI 汇总区展示 |
| **跨批尾帧连续性** | `batch_continuity`：跨批用「上一批尾帧作参考图」近似原生 22 帧 pin（**近似，不冒充原生**，审核标记 `last_frame_ref`） |
| **素材版本回滚** | `GET/POST /bettercomfyui/rollback/list|apply`；后端在 `bettercomfyui/store/rollback.py`；UI ⑫ 区可查版本并回滚。契约见 `docs/ROUND55_版本回滚接口契约.md` |

### 2.2 ROUND56：易用性

| 能力 | 说明 |
|---|---|
| **多段剧本动态列表** | UI 提供「＋ 添加一段 / 删除 / 上移 / 下移 / 清空」，每段独立 textarea；实时显示「N 段 · 预计 N 次 API（每段 1 次）」；后端优先读 `多段剧本列表JSON`，为空则兼容旧的「多段剧本 + 分隔符」文本 |
| **转场自动** | 节点「转场」默认 = **「AI自动（按剧情）」**，运行前调用规则引擎决策；仍保留手动指定覆盖 |
| **默认值收敛 + 高级蒙版** | 提示词编辑、生成/采样、增强、高级表单统一收进可折叠/蒙版区；「手动编辑提示词」需先解锁，降低误调风险 |
| **H3 手册全文进系统提示词** | 新增 `bettercomfyui/prompt/h3_handbook.py` + `prompt/data/h3_handbook_full.txt`（全文 41,966 字）；预设「H3 手册（完整，推荐）」直接作为系统提示词，不再只追加 2,493 字要点 |
| **提示词可见** | 新增 `GET /bettercomfyui/prompt/handbook?mode=full|core`；UI「⑭ 提示词（系统/分镜/素材）」区默认展示当前生效的系统提示词全文，可编辑并「保存为面板默认」（写 `config/user.yaml`）；运行后自动回填每段实际视频提示词供核对 |
| **字幕台词参数** | 新增「字幕台词」（每行一句），解决 ROUND55 审计 P1-1「字幕文本误用视频提示词」 |

### 2.3 ROUND57：硬件自检与适配矩阵（**代码级，未接线**）

新增 `bettercomfyui/hardware.py`（498 行），提供：

- `detect_hardware(probe=...)`：可注入探测结果的硬件检测（单测不需要真 GPU/NPU）；
- `HardwareInfo` / `format_report()` / `format_markdown()`：文本与 Markdown 报告；
- 唯一入口（**目前仅此一个**）：`python -m bettercomfyui.hardware doctor --json`。

**适配矩阵（Tier）**——注意这是**项目口径**，不是「已验证」：

| Tier | 含义 | 覆盖硬件 |
|---|---|---|
| **A** | 官方主路径（推荐） | NVIDIA CUDA |
| **B** | 可用但有条件 | AMD ROCm（Linux）、昇腾 NPU + 补丁、大显存 |
| **C** | 有限支持 | Intel XPU、Apple Silicon MPS、低显存、NPU 缺补丁 |
| **D** | 不可用于真实 H3 生成 | CPU、无 torch、缺依赖 |

- H3 全流程最低显存口径：`H3_MIN_VRAM_MIB = 48 * 1024`（≈48 GB）。

> ⚠️ **重要**：B/C 档全部是**代码与文档级适配，未在任何 AMD / 昇腾 / Intel / Apple 真机上验证过**。详见 §5.2。

---

## 三、本版修复

| # | 问题 | 处置 |
|---|---|---|
| 1 | **【P0】单节点工作流 widgets 错位**：工作流槽位数与节点 schema 不一致，导致转场/片头片尾/审核阈值/失败策略/CNT/API/出片等大批参数整体错位 | 按当前 **119** 参数 schema 重新生成工作流；新增静态守卫 `tests/test_round55_workflow_alignment.py`（对已发布 workflow 逐一校验 widget 数/顺序） |
| 2 | **【P0】`config/default.yaml` 缺失**：v0.1.0–v0.1.2 三个内测包都没有该文件，但 6 处代码在运行时读取它 | 本版随包提供 `config/default.yaml`，并同步 ROUND55 新开关 `h3_manual_enabled` / `no_subtitle` |
| 3 | 【P0】版本回滚「查询版本」按项目显示名必报 500（`PathIsolationError: project_id 必须是 32 位 UUID4 hex`） | `routes.py` 增加 `_resolve_rollback_project()`：显示名 → 32 位 UUID 解析 |
| 4 | 【P1】硬字幕文本来源错误（烧的是视频提示词，不是台词） | 新增「字幕台词」参数；无台词时回退提示词并**明确告警** |
| 5 | 【P1】主流程审核不落盘项目 CNT2/CNT3 | 统一口径：主流程 R_seg/R_full 审核显式写入计数，单节点统一管理 |
| 6 | 【P1】人工审批 pending/decision 只读不清理，陈旧 `approved:true` 会在无人工时立即放行 | 审批通过 `pending→.done.json`、decision→`.used.json`；超时→`.timeout.json`；启动时清理 7 天前 `.used` 归档 |
| 7 | 【P1】片头片尾拼接导致 R_seg 段边界整体平移 | 记录片头/片尾实际时长并并入「段时长列表JSON」，审核按真实成片边界切段 |
| 8 | 【P2】跨批尾帧连续性、转场、断电续跑指纹等边界问题 | 按 ROUND54-A/B 审计逐条修（详见 `docs/ROUND54-A_流程逻辑审计.md`、`docs/ROUND54-B_逻辑错误与边界审计.md`） |

---

## 四、安装与升级

### 4.1 全新安装

1. 解压 `dramacore_v0.1.3-internal_source.zip`（顶层目录 `DramaCore-v0.1.3-internal/`）到任意位置，例如 `D:\DramaCore`；
2. 把 `bettercomfyui_comfy/` 复制到 `ComfyUI/custom_nodes/`；
3. 启动 ComfyUI 时设置 `BETTERCOMFYUI_HOME=<包目录>`（用于定位 `config/` 与项目数据）；
4. （可选）`pip install -r requirements.txt`；ComfyUI 自带 torch/aiohttp，通常无需额外安装；
5. 把 `config/user.yaml.example` 复制为 `config/user.yaml` 并填自己的 API Key（也可在面板里填）；
6. 重启 ComfyUI → 工作流菜单选 `00_【单节点】剧本→成片（无外部依赖）.json`。

### 4.2 从 v0.1.2-internal 升级

- **直接替换** `custom_nodes/bettercomfyui_comfy/` 整目录（**先备份旧目录**，回滚要用）；
- `config/` 保留原来的 `user.yaml`（含你的 Key，发布包里**不含**此文件）；
- **新增** `config/default.yaml`（从包内复制），否则部分面板/设置节点会报「找不到 default.yaml」；
- **必须换新工作流**：旧工作流是 112 槽位版本，本版节点是 119 槽位，**旧工作流会导致参数错位**，请删除旧 JSON 后用包内新版；
- 重启后 Ctrl+F5 强刷浏览器，清 ComfyUI 前端缓存。

### 4.3 模型要求

H3 UNET / CLIP / 视频 VAE / 音频 VAE / 可选 Turbo LoRA；素材生图需 SDXL 类模型（如 RealVisXL）。
**模型权重不在本仓库**，请自行按各自许可证下载。

---

## 五、⚠️ 未完成事项与诚实声明

### 5.1 GPU 真机验收：未完成

| 项 | 状态 |
|---|---|
| 完整单节点链路（真实 DeepSeek Key + H3 模型 + 4090-48G） | ❌ **本轮未验收**（远端实例不可用，全程无 GPU） |
| 多段剧本 10+ 段真实生成 | ❌ 未验证 |
| tiled 采样 >0 的真实显存/耗时/画质收益 | ❌ 未验证（参数与单测已接线，真机收益未知） |
| 跨批尾帧参考在真实 H3 成片上的接缝效果 | ❌ 未验证（近似实现，非原生 pin） |
| 转场 / 片头片尾 / 硬字幕 在真实成片上的最终效果 | ❌ 未验证（离线 ffmpeg 渲染测试通过 ≠ 真机验证） |
| 硬字幕真实字体渲染与 OCR 校验 | ❌ 未验证（本机无 OCR 依赖） |
| 版本回滚在「并发回滚 / 回滚后重新生成」下的端到端行为 | ❌ 未验证（仅模块测试 + 静态检查） |
| H3 手册全文（41,966 字）作系统提示词的真实 API 效果与 token 成本 | ❌ 未验证（本机无 Key） |
| 5090-32G | ❌ 未实测，**不承诺**该机型可用 |

> 历史参考（非本版结论）：v0.1.0 时期曾在 4090-48G 上跑通单节点全链（success 185.3s、R_full 97.2、显存 46,176 MB）。**那是 ROUND47 的代码，不是本版代码**，不能当作 v0.1.3 的验收证据。

### 5.2 非 NVIDIA 硬件：**代码/文档级适配，未真机验证**

**这一节请务必读完再决定是否用于生产：**

| 硬件 | 我们做了什么 | 我们**没有**做什么 |
|---|---|---|
| **NVIDIA CUDA（Tier A）** | 唯一的开发/调试路径；`hardware.py` 能识别厂商/显存/torch 版本 | 本轮**没有**在真机上回归（见 §5.1） |
| **AMD ROCm（Tier B）** | `hardware.py` 检测 `torch.version.hip`、`rocminfo`，产出 Tier 标注与建议参数 | ❌ **没有 AMD 显卡**，未安装 ROCm，未跑过一次生成；H3 相关算子在 ROCm 上的兼容性未知 |
| **华为昇腾 Ascend NPU（Tier B，需补丁）** | `hardware.py` 能识别 `torch.npu` 与 NPU 设备名；文档标注「需补丁」 | ❌ **没有昇腾设备**，未验证 CANN 版本、未写/未测任何 NPU 补丁；「需补丁」意味着**开箱即用不成立** |
| **Intel XPU（Tier C）** | 检测 `torch.xpu`、`xpu-smi discovery` | ❌ **没有 Intel 独显**，未验证 IPEX 路径与显存行为 |
| **Apple Silicon MPS（Tier C）** | 检测 `torch.backends.mps.is_available()` | ❌ **没有 Mac**，未验证 MPS 上 H3 是否可跑（48 GB 统一内存口径亦未验证） |
| **CPU（Tier D）** | 明确标注不可用于真实 H3 生成 | 仅用于 UI 预览 |

> 换句话说：**本版对昇腾 / AMD / Intel / Apple 的「支持」全部停留在代码分支与文档口径层面**。`hardware.py` 的 Tier 标注是「我们认为它属于哪一档」，不是「我们验证过它在这一档能跑」。
> 如果你在这些平台上有真机，欢迎按 §9 的自检命令产出报告反馈给我们 —— 那是把 Tier 从「口径」变成「事实」的唯一途径。

### 5.3 已知限制（使用前请知悉）

1. **审核是本地启发式**（真实像素统计：构图/色彩/节奏/跳变），**不是语义大模型评分**；一致性/合规维度可能为 `skipped`，会如实标注；
2. **R_seg 结果 JSON 的 `index/scores` 字段可能为空**（分数本身有效，属历史数据兼容问题）；
3. **跨批「原生 22 帧 pin」不成立**（vendor 限制）：跨批用「上一批尾帧作参考图」近似，审核标 `last_frame_ref`，**不冒充原生**；
4. **转场全是 ffmpeg 级**（硬切 / 叠化 / 黑场 / 轻转场），**没有 AI 生成转场**；多标签 AI 转场在真实成片上的效果未验证；
5. **级联返工深度默认 1 层**；
6. **整片失败策略默认 `proceed`**（可改 `block` / `degrade`）；
7. **默认 1920×1088 / 每段 10 秒**（显存吃紧可手动降 480p / 5.167s）；**未内置超分**；
8. **硬字幕为程序对齐**（时间网格），非 ASR / 语义分句；字幕样式参数覆盖有限；
9. **UI-P1-1**：折叠区蒙版只挡鼠标，**可用键盘 Tab 聚焦后绕过**修改隐藏参数；
10. **UI-P1-2**：「显示/隐藏原生参数表单（高级）」按钮**无效**（点了不会真的显示原生控件）；要看原生值请用右侧 ComfyUI「参数」面板；
11. **UI-P1-3**：多段剧本 **超过 64 段会被后端静默截断**，UI 不提示；
12. **多段剧本每段 1 次 API**，段数越多调用与费用越高（UI 会显示预计次数）；
13. **5090-32G 未实测**，不承诺；
14. 本轮**未跑**全量回归：引用的历史结论（ROUND55：全量 pytest 通过、`tests/verify` 298 通过 / 0 失败 / 1 未验证）**不代表 v0.1.3 的当前状态**，发布执行者应在打包前真跑一次并记录真实数字。

---

## 六、许可证（重要）

本项目采用 **AGPL-3.0 + Commons Clause**：

- **修改必须开源**：分发或通过网络提供服务时，必须按 AGPL-3.0 提供对应源码；
- **禁止商用**：不得直接或间接销售本软件或其衍生服务（Commons Clause）；
- 详见仓库根 `LICENSE`；
- **注意**：AGPL-3.0 + Commons Clause **不是 OSI 标准开源许可证**，属于「源码可见 + 限制商用」。若需商业授权，请单独联系作者；
- **第三方组件**：`bettercomfyui_comfy/vendor/h3_director/` 来自 MiniMax H3 Director，**保留其原始 Apache-2.0 LICENSE**（见该目录 `LICENSE`），随包分发。

---

## 七、附件与校验

| 附件 | 说明 | SHA256 |
|---|---|---|
| `dramacore_v0.1.3-internal_source.zip` | 源码包（代码 / 工作流 / 配置模板 / 测试 / 精选文档） | `（打包后填入）` |
| `dramacore_v0.1.3-internal_source.zip.sha256` | 校验和文件（`sha256sum -c` 兼容） | — |
| `dramacore_v0.1.3-internal_manifest.json` | 逐文件清单与 SHA256、生成时间、git HEAD | — |

校验方法：

```powershell
# Windows
Get-FileHash .\dramacore_v0.1.3-internal_source.zip -Algorithm SHA256
```
```bash
# Linux / macOS
sha256sum -c dramacore_v0.1.3-internal_source.zip.sha256
```

**包内绝对不含**：`config/user.yaml`（你的 API Key）、`projects/`（项目与素材）、`output/`（成片）、任何模型权重（`*.safetensors` / `*.pt` / `*.pth` / `*.gguf`）、任何 `.env` / 私钥 / token。

---

## 八、回滚方案

| 层面 | 做法 |
|---|---|
| 插件 | 保留旧版 `custom_nodes/bettercomfyui_comfy` 备份目录，替换后重启即可；节点类 ID 未变，旧工作流仍能加载（但**参数槽位数会与新版节点不匹配**，见 §4.2） |
| 配置 | `config/user.yaml` 单独备份 / 还原；`config/default.yaml` 随包更新 |
| 成片 | 保留上一版 bundle zip 与 `custom_nodes/bettercomfyui_comfy` 备份目录（发布前打快照） |

---

## 九、给内测用户的验证建议

1. 先只跑**单段短剧本 + 480p + 5.167s**，确认能出片再放大参数；
2. 跑一次硬件自检，把输出连同问题一起反馈（这是最省沟通成本的一条）：

```bash
python -m bettercomfyui.hardware doctor --json
```

3. 报问题时请附：`docs/` 里对应的报告 JSON、`段时长列表JSON`、以及 ComfyUI 控制台里 `[bettercomfyui]` 前缀的日志；
4. 首次使用请**先在旧项目上试**，不要直接上正式项目。

---

## 十、致谢

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
- MiniMax H3 Director（vendored，遵循其 Apache-2.0 许可）
- 所有开源社区贡献者

---

*本说明由 ROUND58 发布预检流程生成草稿，发布执行者需在打包后填入 §7 的真实 SHA256，并核对 §5.3 第 14 条的实测数字。*

---

## 附：ROUND58 并行增量（发布前必须复核）

> 本节是草稿写就之后补充的。原因：撰写本说明时（19:33）ROUND58 的多项交付**尚未落地**，
> 到 19:46 观测到以下新增/变更。**发布前请由 Lead 逐条核实是否真的进了包、是否真的可用**，
> 确认后再把本节合并进正文 §2，或删掉本节。

### F.1 观测到的新增交付

| 时间 | 落地物 | 猜测定位（需核实） |
|---|---|---|
| 19:35 | `bettercomfyui/prompt/data/h3_concise_v1.txt`（20.6 KB） | H3 提示词手册**精简版 v1**，配合 `h3_handbook.py`（3.7→8.7 KB）做成可选系统提示词预设 |
| 19:35 | `docs/ROUND58_H3提示词精简版集成.md` | 上述精简版的集成说明 |
| 19:37 | `bettercomfyui_comfy/hardware_adapt.py`（26 KB） | 插件侧硬件适配层（与 `bettercomfyui/hardware.py` 的检测层分工：**检测 = hardware.py，适配动作 = hardware_adapt.py**） |
| 19:40 | `bettercomfyui/hardware.py`（22.4→28.5 KB） | ROUND57 检测模块继续扩展 |
| 19:43 | `install/install_core.py`（40.5 KB） | 跨平台安装器核心 |
| 19:45 | `install/install_windows.ps1` / `install_linux.sh` / `install_macos.sh` | 三平台安装脚本 |
| 19:44 | `tests/test_round58_concise_prompt.py`（11.1 KB） | 精简版提示词的守卫测试 |

### F.2 观测到的功能性改动（改动量大，需重点复核）

| 文件 | 体量变化 | 风险 |
|---|---|---|
| `bettercomfyui_comfy/one_stop_node.py` | 169 → **198 KB** | 单节点主逻辑大改，**必须重跑 `tests/test_round55_workflow_alignment.py`** 确认工作流槽位仍是 119 |
| `bettercomfyui_comfy/web/js/dramacore_director_ui.js` | 124 → **164 KB** | UI 大改，蒙版/折叠/Tab 绕过等旧问题状态可能变化 |
| `bettercomfyui_comfy/routes.py` | 52 → **61.7 KB** | 新增路由，需确认无新增未鉴权入口 |
| `bettercomfyui/prompt/validate.py` | 50 → **78.6 KB** | 校验逻辑大改，确认红线规则未放松 |
| `bettercomfyui_comfy/everything_node.py` | 40 → **49.1 KB** | — |

### F.3 因此对「已知限制」的时效性提醒

正文 §5.3 的 14 条已知限制，是依据 **ROUND55/56 审计文档（截至 2026-10-19 23:06）** 写的。
ROUND58 的上述改动**有可能已经修掉其中若干条**（尤其 UI-P1-1/P1-2/P1-3 与硬字幕、提示词相关项）。

**发布执行者必须做的事**：

1. 逐一复核 §5.3 的 14 条，已修的删掉、未修的保留、新引入的补上；
2. **不得**沿用本文的旧结论直接发布 —— 否则要么「已修还写着限制」（对用户不公平），要么「没修却删了限制」（误导）；
3. 复核依据：`docs/ROUND58_任务拆分与验收计划.md`、`docs/ROUND58_H3提示词精简版集成.md`、以及 ROUND58 各子智能体的审计结论。

### F.4 安装方式变更提醒

如果 `install/` 的跨平台安装器可用，正文 §4.1 的「手动复制 `bettercomfyui_comfy/`」应改为**优先推荐安装器**，
手动复制作为备选。请在 Release notes 定稿前确认安装器的实际可用性与支持的平台矩阵（并如实区分
「脚本已提供」与「已在对应平台实测」——与 §5.2 同样的诚实标准）。
