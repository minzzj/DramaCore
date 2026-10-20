# Better ComfyUI · 备用开源节点审计报告（AUDIT_NODES）

> 任务：task-1（负责人 teammate `audit-nodes`）
> 审计对象：`保留备用的节点/` 下 16 个 git 仓库 + `保留备用的节点/_PyPI离线包/`（4 个文件）
> 机器可读版本：`docs/audit_nodes.json`
> 生成时间：2026-10-16 23:05 +08:00

---

## 0. 结论摘要

| 结论 | 数量 | 仓库 |
|---|---|---|
| **RECOMMEND** | 2 | ComfyUI-DNNodes、ComfyUI-JobNotify |
| **OPTIONAL** | 5 | H3-Chinese-Director、ComfyUI-OpenClaw、ComfyUI-SystemOne、ComfyUI-DeepSeek-API-connector、ComfyUI-HR-Endless-Sampler |
| **DEFER** | 9 | ComfyUI-MiniMaxH3-ChainDirector、ComfyUI_MiniMaxH3_Director、ComfyUI_MiniMaxH3_Smart_Director、fxai-toolkit、ComfyUI-FlashVSR_Ultra_Fast、ComfyUI_pxtool、ComfyUI-Storyboard-LLM、comfyui-api-pack、ComfyUI-CometAPI |
| 离线包 | 2 个包均 DEFER | bizyair-cloudberry 0.3.2、nonebot-plugin-comfyui your-gpu-host |

**一句话**：16 个仓库里没有一个实现本项目的 R1/R2/R3/R4/R5（存在性判定、只追加、LOCK 绑定、计数隔离、项目隔离）。它们只能当「生成器 / 通知器 / 外部 API 适配器」，**绝不能替代 `bettercomfyui/store` 的 AssetStore 与 LOCK 判定**。

---

## 1. 方法、证据边界与「为什么全部 unverified」

### 1.1 本机环境事实（本审计实测或由 Lead 核实）

| 项 | 值 | 证据 |
|---|---|---|
| ComfyUI 有效副本 | `[USER-HOME]\ComfyUI-Installs\ComfyUI\ComfyUI` | — |
| ComfyUI 版本 | **0.22.3** | 实测读 `comfyui_version.py`：`__version__ = "0.22.3"` |
| custom_nodes 内容 | 仅 `example_node.py.example` + `websocket_image_save.py` | 实测列举，无任何第三方节点 |
| models 权重 | **无任何权重**（只有 `put_*_here` 占位） | 实测递归查找 `*.safetensors/*.ckpt/*.pth/*.pt/*.gguf/*.bin/*.onnx` 无命中 |
| V3 扩展支持 | 支持 | 实测 `comfy_api/latest/__init__.py` L118/L173 导出 `ComfyExtension`；`nodes.py` L2242 存在 `comfy_entrypoint` 分支 |
| ComfyUI 运行时解释器 | **不存在** | 实测 `ComfyUI-Installs` 全递归查找 `python*.exe` 无结果；存在 `.comfyui-desktop-2` 目录标记 |
| ComfyUI Desktop 配置 | basePath 指向 `G:\...`，**该盘不存在，实例不可用** | 由 Lead 核实（本审计未另行复现） |
| `ComfyUI-Installs\logs` | 列举返回非零退出、无内容 | 未取得运行日志作为佐证 |
| 本机 GPU | GTX 1050 Ti 4GB（**仅开发机**，非目标机） | Lead 核实；目标机显存 ≥24GB |
| 本机 Python | 3.11.9 | 实测 `python -V` |
| github.com | 连接被 reset，装包需走离线源 | Lead 核实 |

### 1.2 三条硬边界

1. **全部 16 个节点的 `runtime_compat` 一律 `unverified`。** 理由不是「偷懒」，而是：本机 custom_nodes 为空、无任何模型权重、且**不存在可调用的 ComfyUI 解释器**。任何「该节点在本机可用」的说法都是臆测。
2. **区分「文档声明」与「本机可验证」。** 例如 `ComfyUI_MiniMaxH3_Director` 的 `requires-comfyui = ">=0.30.0"` 是**文档声明**（读 pyproject.toml 得到），不是实测结论；但它足以判定「在 0.22.3 上装它是明确不兼容」。
3. **禁止臆测。** 凡读不到的项一律写 `unknown` 并注明读了哪个路径仍为空；凡本文出现的类名、依赖、许可证文本，均可回溯到具体文件。

### 1.3 统一验证前置条件（每个节点验证都要满足）

1. 一个可运行的 ComfyUI 实例（0.22.3 或满足该节点声明的版本下限），节点放入正确的 `custom_nodes` 层级；
2. 该节点所需全部权重按 `docs/MODELS.md` 或 README 落盘到 `ComfyUI/models/` 对应子目录；
3. 该节点声明的全部 Python 依赖可从离线源获取（本报告已逐项标出无法离线获取者）；
4. 目标机 GPU 显存满足要求（本机 4GB 不能代表目标机 ≥24GB）；
5. 启动后确认节点出现在节点列表中，并跑通该节点自带 example workflow。

---

## 2. 总表

| # | 仓库 | HEAD | 许可 | Python | ComfyUI 下限 | 节点数 | 联网 | 密钥 | verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 | ComfyUI-MiniMaxH3-ChainDirector | `fbcb9c43` | MIT | ≥3.10 | 未声明 | 4 | 是 | 否 | **DEFER** |
| 2 | ComfyUI_MiniMaxH3_Director | `4c6245e5` | Apache-2.0 | ≥3.10 | **≥0.30.0** | 11 | 是 | 否 | **DEFER** |
| 3 | ComfyUI_MiniMaxH3_Smart_Director | `31a3b88f` | MIT | unknown | 未声明 | 3 | 是 | 是 | **DEFER** |
| 4 | H3-Chinese-Director | `6a0b0736` | MIT | unknown | 未声明 | 52 | 是 | 否 | **OPTIONAL** |
| 5 | ComfyUI-DNNodes | `39164f25` | MIT | unknown | 未声明 | 5 | 否 | 否 | **RECOMMEND** |
| 6 | ComfyUI-JobNotify | `cb566f49` | Apache-2.0 | ≥3.9 | 未声明 | 0（全局扩展） | 是 | 否 | **RECOMMEND** |
| 7 | ComfyUI-OpenClaw | `f4ddb222` | MIT | ≥3.10 | 未声明 | 4 | 是 | 是 | **OPTIONAL** |
| 8 | ComfyUI-SystemOne | `e8263123` | MIT | unknown | 未声明 | 7 | 是 | 是 | **OPTIONAL** |
| 9 | fxai-toolkit | `e0da25a1` | **unknown**（无 LICENSE 文件） | unknown | 未声明 | 130（注册数 ≤） | 是 | 否 | **DEFER** |
| 10 | ComfyUI-FlashVSR_Ultra_Fast | `4820b3f0` | **GPL-3.0** | unknown | 未声明 | 3 | 是 | 否 | **DEFER** |
| 11 | ComfyUI_pxtool | `79915547` | Apache-2.0 | unknown | 未声明 | 21 | 是 | 否 | **DEFER** |
| 12 | ComfyUI-Storyboard-LLM | `d17c9adc` | MIT | **≥3.13** | 未声明 | 1 | 是 | 是 | **DEFER** |
| 13 | ComfyUI-DeepSeek-API-connector | `e5bc511e` | MIT | unknown | 未声明 | 1 | 是 | 是 | **OPTIONAL** |
| 14 | comfyui-api-pack | `b232c338` | **GPL-3.0** | **≥3.12** | 未声明 | 10 | 是 | 是 | **DEFER** |
| 15 | ComfyUI-CometAPI | `2e7ea2e8` | **非商用许可** | ≥3.8 | 未声明 | 17 | 是 | 是 | **DEFER** |
| 16 | ComfyUI-HR-Endless-Sampler | `1681f89a` | Apache-2.0 | unknown | 未声明 | 4 | 是 | 否 | **OPTIONAL** |

**仓库级共性（实测）**：16 个仓库全部有 `.git`（`git=True`）；**全部无 `.gitmodules`**，`git submodule status` 全部为空 → **不存在「clone 了子模块却没带进来」的情况**；**全部是 depth=1 浅克隆**（`.git/shallow` 存在，`rev-list --count HEAD` = 1）；`git status --porcelain` 计数全部为 0（工作树干净）。

---

## 3. 逐仓库详情

### 3.1 ComfyUI-MiniMaxH3-ChainDirector — DEFER

- 路径：`保留备用的节点/ComfyUI-MiniMaxH3-ChainDirector`；HEAD `fbcb9c43af42eac3126b30a38b33d2823b5199da`（2026-08-26，main）；版本 0.1.6
- 许可：**MIT**（`LICENSE` 1086 B：`MIT License Copyright (c) 2026 luxu1999`）
- 节点类（实测 `__init__.py` L32 起的映射 + 追加赋值）：`ListToAudio`、`ConcatImageBatches`、`MiniMaxH3ChainDirector`、`MiniMaxH3ChainDirectorEN`
- 依赖：**无 `requirements.txt`**；`pyproject.toml` 的 `[project]` 段**未声明 `dependencies`**；`requires-python = ">=3.10"`
- ComfyUI 下限：未声明
- torch：是（`__init__.py` L1、`chain.py` L24）；torchaudio：否
- 风险：
  - 硬依赖同族仓库 `ComfyUI_MiniMaxH3_Director`（`_autopatch.py` 的 `_REQUIRED_API` 直接引用其 `director/external_groups`、`nodes/director_common` 等**内部模块**）→ 而该仓库声明 ComfyUI ≥0.30.0；
  - `scripts/install_all.py` 从 **github.com** clone 三个仓库（Director / VideoHelperSuite / KJNodes）并 `pip install sageattention==1.0.6` → 本机 github 不可达，离线装不上；
  - `scripts/download_models.py` 需下载权重（默认 huggingface.co）；
  - `_autopatch.py` 运行时向 `sys.modules` 注入别名，属对他人包内部结构的强耦合；
  - `patches/` 下 2 个 `.patch` 需打到 Director 仓库上。
- **理由**：其硬依赖的 ComfyUI 下限 0.30.0 高于本机 0.22.3，且依赖仓库只能从 github.com 拉取（本机不可达），现在装上只会得到假成功。

### 3.2 ComfyUI_MiniMaxH3_Director — DEFER

- 路径：`保留备用的节点/ComfyUI_MiniMaxH3_Director`；HEAD `4c6245e518dd8a494f3afc4f3cccd53f22d82d10`（2026-10-13，main）；版本 1.0.0
- 许可：**Apache-2.0**（`LICENSE` 10963 B）
- 节点类（11）：`MiniMaxH3Director`、`MiniMaxH3DirectorRefine`、`MiniMaxH3DirectorSelfLift`、`MiniMaxH3DirectorSemanticBridge`、`MiniMaxH3DirectorFaceRefine`、`ComfyMiniMaxH3Director`（legacy id）、`MiniMaxH3DirectorConditioning`、`MiniMaxH3DirectorPlannerConditioning`、`MiniMaxH3DirectorGroupImageToVideo`、`MiniMaxH3DirectorGroupReferenceToVideo`、`MiniMaxH3DirectorGroupsCombine`
- 依赖（`requirements.txt`）：`opencv-python-headless>=4.8`、`imageio-ffmpeg>=0.4`、`scenedetect>=0.6.4,<0.8`、`ultralytics>=8.0`；可选 `nvidia-vfx`（`--extra-index-url https://pypi.nvidia.com`）；FaceRefine 需 `face_yolov8m.pt` 放到 `models/ultralytics/bbox/`（非 pip 可装）
- Python：`>=3.10`
- **ComfyUI 下限：`>=0.30.0`**（`pyproject.toml` L25 `requires-comfyui = ">=0.30.0"`）← 与实测 0.22.3 冲突
- torch：是；torchaudio：**惰性 import**（`director/h3_motion_context.py` L424、`director/audio_export.py` L78）
- **理由**：仓库自己声明 requires-comfyui ≥0.30.0，本机 0.22.3 低于下限；属明确不兼容，必须先升 ComfyUI 再评估。

### 3.3 ComfyUI_MiniMaxH3_Smart_Director — DEFER

- 路径：`保留备用的节点/ComfyUI_MiniMaxH3_Smart_Director`；HEAD `31a3b88f782dd108f6f944995d6d146e6db422d6`（2026-08-27，main）
- 许可：**MIT**（1089 B，`Copyright (c) 2026 Kiwi-shijie`）
- 节点类（3，实测 `__init__.py` L5）：`MiniMaxH3SmartDirectorAllInOne`、`MiniMaxH3SmartDirector`、`MiniMaxH3SmartPlanInspector`
- 依赖：**`requirements.txt` 与 `pyproject.toml` 均不存在**（实测）→ 依赖完全未声明
- Python / ComfyUI 下限：均未声明
- 风险：README 说明主节点内部调用 AIMixer `MiniMaxH3Director` 执行 → 硬依赖第 3.2 条（ComfyUI ≥0.30.0）；提示词优化可选「本地 LLM 或 API」。
- **理由**：未声明任何依赖且硬依赖 Director，在本机 0.22.3 上无法建立可复现的安装面。

### 3.4 H3-Chinese-Director — OPTIONAL

- 路径：`保留备用的节点/H3-Chinese-Director`；HEAD `6a0b07365ce08aed68a75f6004dfb631e4a919d1`（2026-10-14，main）；版本 V29.4（8197 冻结正式线）
- 许可：**MIT**（5100 B）+ `THIRD_PARTY_NOTICES.md` + `manifests/nodes.csv` 逐项归属登记
- 结构（**重要**）：**仓库根目录没有 `__init__.py`**（实测缺失）→ 不是可直接放进 `custom_nodes/` 的标准节点包。真正的节点位于 `custom_nodes/local_author/8197_production_line/` 下 **19 个子包**，每个子包各自有 `__init__.py`。
- 节点类（**52 个，逐文件实测提取**）：
  - `h3_workflow_helpers`（18，`nodes.py` L1151）：`LoadImageWithSwitch`、`H3OptionalLoadImage`、`H3OptionalImageResize`、`H3LazyImageSwitch`、`H3OperationSelector`、`H3SpeedModeSelector`、`H3SizeModeSelector`、`H3GenerationModeSelector`、`H3ImageModeSelector`、`H3ReferenceSourceSelector`、`H3Fl2vRouteSelector`、`H3Ref2vaBackendSelector`、`H3Ref2vaAttentionSelector`、`H3MemoryOptimizationSelector`、`H3GuideModeSelector`、`H3GuideImageGuard`、`H3ContinuationSelector`、`H3VideoPromptRole`
  - `H3_NativeContinuation`（7，L471）：`H3ContinuationProvider`、`H3ContinuationAudioBranch`、`H3ContinuationPrepare`、`H3ContinuationGuides`、`H3ContinuationSave`、`H3ContinuationTrimOnce`、`H3ContinuationAssemble`
  - `H3_AudioUpgrade`（7，L277）：`H3AudioModelRoute`、`H3AudioReferencePack`、`H3ExactAudioLock`、`H3AudioSampleStore`、`H3AudioRefineGate`、`H3AudioOutput`、`H3AudioFile`
  - `h3_conditioning_prebake`（5，L199）：`H3AttemptGuard`、`H3ConditioningBake`、`H3ConditioningLoad`、`H3PreBakeContinuationAudit`、`H3PreBakePreviousState`
  - `h3_transfer_audit`（4，L130）：`H3TransferAuditCLIP`、`H3TransferAuditVAE`、`H3TransferAuditReceipt`、`H3DirectorCompileAudit`
  - 单节点包：`H3Audio8ReferencePreflight`（H3_Audio8Step）、`H3PromptSidecar` + `H3SixDirectorState`（H3_Prompt_Sidecar L62/L79）、`H3V28DirectorConsole`（H3_V28_Director L54）、`H3SelectiveTEUnload`、`H3SelectiveDiffusionUnloadBeforeVAE`、`H3PartialDiffusionEvictBeforeVAE`、`H3TEResidencyProbe`、`H3SamplerToVAEResidencyProbe`
  - 已禁用（`*.disabled/`）：`H3AuditClipMarker`、`H3AuditSink`
  - 实测 `NODE_CLASS_MAPPINGS = {}`（空）：`H3_MemoryPolicy.disabled`、`h3_adaptive_host_warm_inplace`
- 依赖：**无 `requirements.txt` / `pyproject.toml`**；依赖以**第三方节点仓库 + HF 权重**形式存在。`H3_AudioUpgrade` 需运行时用 `scripts/setup/install_h3_audio_refine.py` 从 github 安装 `ComfyUI-H3-AudioRefine`（commit `fd5a8dfe8b4c636b1877abd07d11ebc985eddbed`）。
- torch：是；torchaudio：是（`H3_AudioUpgrade/nodes.py` L9）
- 风险：
  - 根目录无 `__init__.py`，安装层级必须逐个处理 19 个子包；
  - `manifests/nodes.csv` 登记了 **ComfyUI-KJNodes（GPL-3.0）**、rgthree-comfy、ComfyUI-MAINodes、ComfyUI-Ref2VA-VSA、ComfyUI-MiniMax-H3-PDD-Acc、ComfyUI-H3-Motion-Context、H3-Optimizations、comfyui-obvpm 等第三方节点为「不搬运源码但工作流依赖」；
  - `docs/MODELS.md` 明确「本仓库不包含任何模型权重」，需自行从 HF 下载大量权重（Qwen3VL-32B 文本编码器、多个 int8 diffusion 模型、VAE、LoRA、ControlNet）；
  - README 自述：公开版 V29.4 通过离线检查，**但没有重新执行完整 GPU 端到端生成**（作者自标验证边界）。
- **理由**：作为中文导演台与「六段连续生成 / 段间 latent 接续 / PreBake 缓存」的设计参考价值高且许可为 MIT，但安装面巨大，本机无法验证，故不作当前默认件。

### 3.5 ComfyUI-DNNodes — RECOMMEND

- 路径：`保留备用的节点/ComfyUI-DNNodes`；HEAD `39164f2518351155359b62e28f31b8dce0787df7`（2026-10-15，main）；版本 1.7.8
- 许可：**MIT**（1483 B，`Copyright (c) 2026 ljxdn`），并声明部分文件派生自 `ComfyUI-H3-OpenNodes`（© sunnyboxs），见 `THIRD_PARTY_NOTICES.md`
- 节点类（5）：`H3MediaLoader`（asset_card.py）、`DNMediaToDirectorGroup` / `DNGroupSplit` / `DNGroupDirector`（nodes.py L62/L271/L452 常量）、`DNSSelfLiftH3`（selflift_cache.py）
- 依赖：**无 `requirements.txt` 与 `pyproject.toml`**（实测均不存在）；README L37 自述「仅依赖 ComfyUI 本体自带 torch/numpy/Pillow，无需额外 pip 包」；`asset_card.py` L338 **惰性** `import torchaudio`
- Python / ComfyUI 下限：均未声明
- 风险：
  - `DNSSelfLiftH3` 依赖兄弟包 `comfyui-SelfLift`（`github.com/facok/comfyui-SelfLift`），本仓库不含；缺失时该节点**不注册**（只打印提示），其余 4 个照常 → 属「部分功能静默降级」，接入前必须自检节点存在性；
  - README L196 明确警告：SelfLift 一采缓存指纹只算「种子/步数/缩放/cfg/形状」，**不含提示词** → 不能替代 `content_hash` 语义；
  - 节点 id 承载工作流兼容性（`nodes.py` L452 注释：节点 id 必须保持不变）。
- **理由**：纯 Python、零额外 pip 依赖、MIT、根目录即标准节点包；「资产卡 / 分组导演台 / 分段口按连线动态增长」正好对上本项目素材与分段编排需求。

### 3.6 ComfyUI-JobNotify — RECOMMEND

- 路径：`保留备用的节点/ComfyUI-JobNotify`；HEAD `cb566f49455dd34ea15b23bade5d2b650e998ca4`（2026-07-03，main，**仅 1 个提交**）；版本 1.0.0
- 许可：**Apache-2.0**（11558 B）
- 节点类：**无图节点** —— `__init__.py` L11 实测 `NODE_CLASS_MAPPINGS = {}`；它是全局扩展，注册 `POST /jobnotify/webhook` 路由 + `WEB_DIRECTORY = "./web"`
- 依赖：`pyproject.toml` `dependencies = []`（空）
- Python：`>=3.9`；ComfyUI 下限：未声明
- 风险：
  - 能力依赖外网可达的 webhook 目标；
  - **`POST /jobnotify/webhook` 无鉴权、无目标白名单**：任何能访问该 ComfyUI 后端的客户端都能让服务端向任意 `http(s)` 地址发起 POST/PUT → **SSRF / 出站代理面**，生产部署需前置访问控制；
  - 只提供「声音 + 任意 webhook」；**QQ/飞书/钉钉需外部机器人自带 webhook 地址**，节点本身不实现这些平台协议。
- **理由**：零依赖、无版本下限、Apache-2.0，「作业完成通知」正是 CONTRACTS 第 6 节 `review/notify.py` 需要的能力；用其 webhook 代理即可承载弹窗/声音/飞书/钉钉多选。

### 3.7 ComfyUI-OpenClaw — OPTIONAL

- 路径：`保留备用的节点/ComfyUI-OpenClaw`；HEAD `f4ddb22233a89c9fec7b51595b7bf8aba7c2f140`（2026-04-09，main）；版本 0.8.6
- 许可：**MIT**（1067 B，`Copyright (c) 2026 rookiestar`）
- 节点类（4，`__init__.py` L15，位于 `if __package__` 分支内）：`MoltbotPromptPlanner`、`MoltbotBatchVariants`、`MoltbotImageToPrompt`、`MoltbotPromptRefiner`（`else` 分支实测把映射置空）
- 依赖：`cryptography>=41.0`、`defusedxml>=0.7.1`；可选 extra `wechat-aes: pycryptodomex>=3.20`
- Python：`>=3.10`；ComfyUI 下限：未声明（`web/tabs/settings_tab.js` L934 仅读 `window.COMFYUI_VERSION` 做展示）
- 风险：
  - 定位是完整「AIGC 工厂」平台（`api/ connector/ nodes/ services/ web/ tests/` 全套 + 自带 HTTP API、调度、审批、密钥托管），体量远超通知需求；
  - README 声明支持 8 个消息平台（Discord/Telegram/WhatsApp/LINE/WeChat/KakaoTalk/Slack/Feishu-Lark），每个都需平台凭据与外网；
  - 密钥托管链 `env -> 可选 1Password CLI -> 加密服务端存储 -> none`，运行时需配置；
  - `web/` 为源态 JS（无构建产物），`package.json` 只有 devDependencies 与测试脚本 → **不需要 npm 构建**；但跑其测试需联网 `npm install`；
  - 依赖 `defusedxml`，本机 host python **未安装**（实测）。
- **理由**：飞书/微信等通知与审批通道能力最完整，但它是整套平台、需要额外 pip 依赖且密钥托管要求高，属「通知需求过配」。

### 3.8 ComfyUI-SystemOne — OPTIONAL

- 路径：`保留备用的节点/ComfyUI-SystemOne`；HEAD `e8263123960e5c71eebe540fef29e72b64f1b775`（2026-10-03，main）；版本 0.2.0
- 许可：**MIT**（1089 B，`Copyright (c) 2026 dante01yoon`）
- 加载方式：根 `__init__.py` **无 `NODE_CLASS_MAPPINGS`**，改用 **V3 API**：`async def comfy_entrypoint()` 返回 `SystemOneExtension()`
- 节点类（7，取自 `systemone/nodes.py` 的 `node_id=`）：`SystemOneBackend`(L63)、`SystemOneChoice`(L88)、`SystemOneNoul`(L119)、`SystemOneScore`(L147)、`SystemOneMap`(L176)、`SystemOneImageCheck`(L196)、`SystemOnePickBestImage`(L233)
- 依赖：`laya>=0.3.24`（`requirements.txt` 与 `pyproject.toml` 一致）；`pyproject.toml` **未声明 `requires-python`**
- 风险：
  - 三种后端都要外部条件：**laya** 需本地权重（`LAYA_CHECKPOINTS` 指向 `convaiinnovations/laya`，HF 下载）；**jev** 需 `TYPESAFE_API_KEY` 且打 `https://api.typesafe.ai/v1/systemone`；**clef** 需 `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` 且打 Cloudflare Workers AI；
  - 缺环境变量时 `backends.py` 的 `_env()` **直接抛 `RuntimeError`**（行为明确、不静默降级，符合本项目取向）；
  - 本机 host python **未安装 laya**（实测）；
  - **只有 Clef 后端支持图像输入**（`_reject_images` 明示本地 laya 后端不能看图）。
- **理由**：「让程序向模型提问并分支」的思路与本项目 R1（存在性判断只能由程序做）的边界需仔细区分，且三种后端都要网络或本地权重；作为质检分支参考可用，不适合作离线目标机默认件。

### 3.9 fxai-toolkit — DEFER

- 路径：`保留备用的节点/fxai-toolkit`；HEAD `e0da25a1f0df0727b1652d5a6ad8a31b5ad11657`（2026-10-05，main）；版本 1.0.8
- 许可：**`unknown`** —— 已检查仓库根目录 `LICENSE`/`LICENCE`/`COPYING`/`NOTICE` 全部候选，**均为 NONE**（无 LICENSE 文件）；许可证仅存在于文档声明层：`pyproject.toml` `license = "MIT"`、`README.md` L133「本项目基于 MIT License 开源」。
- 节点类：`__init__.py` 的 `NODE_REGISTRY`（L53–L187）列出 **130 项**，`NODE_CLASS_MAPPINGS` 由循环 import 后逐个写入（L189–L196）；**import 失败的项只打印「节点加载失败」而不中断**，因此实际注册数 ≤ 清单数。按类别（实测清单）：
  - 音频（18）：`FxAiAudioSegmenter`、`FxAiAudioSegmenterV2`、`FxAiAudioSelector`、`FxAiMelRoformerLoader`、`FxAiMelRoformerSampler`、`FxAIAudioSegmentLoad`、`FxAiMultiAudioLoad`、`FxAiAudioManager`、`FxAiLoadAudioByIndex`、`FxAiSingleAudioProc`、`FxAiAudioMerge`、`FxAiAudioAvgSplit`、`FxAIAudioFrameCalculate`、`FxAiAudioDurationSeg`、`FxAiAudioDurationGetSeg`、`FxAiAudioListPreview`、`FxAiMiniMaxFrameCalculate`、`FxAiMiniMaxAudioSegmentLoad`
  - 场景/文本（11）：`FxAiMultiLineText`、`FxAiMultiLineTextLoad`、`FxAiMultiLineTextGrid`、`FxaiStoryBoard`、`FxaiStoryBoardV2`、`FxaiStoryBoardLoad`、`FxaiStoryBoardLoadV2`、`FxAiSceneManager`、`FxAiSceneManagerV2`、`FxAiSceneLoad`、`FxAiSceneLoadV2`
  - 图像（17）：`FxAiImageManager`、`FxAiImageManagerV2`、`FxAiLoadImageByIndex`、`FxAiImageBatchLoad`、`FxAiImageGetSingle`、`FxAiImageDownscale`、`FxAiImageBatchConcat`、`FxAiImageLoopTile`、`FxAiImageUniformSize`、`FxAiImageLoadBase64`、`FxAiMultiViewGenerator`、`FxAiQwenEditMultiView`、`FxAiQwenEditThreeView`、`FxAiImageSelectorByIndex`、`FxAiQwenImage21Edit`、`FxAiQwenFaceFeature`、`FxAiQwenImage21ThreeView`
  - 角色（7）：`FxAiCharacterAssets`、`FxAiCharacterAssetsLoad`、`FxAiCharacterBatchLoad`、`FxAiCharacterImageSelector`、`FxAiCharacterImageByIndex`、`FxAiCharacterProfileManager`、`FxAiCharacterSelector`
  - 提示词（9）：`FxAiPromptGenerator`、`FxAiPromptManager`、`FxAiLoadPromptByIndex`、`FxAiMultiPromptEditor`、`FxAiMultiPromptLoader`、`FxAiPromptRelayEncode`、`FxAiQwenEditEnhanced`、`FxAiQwenEditEnhancedV2`、`FxAiMultiLinePrompt`
  - 视频（15）：`FxAiMiniMaxImageToVideoV2`、`FxAiMiniMaxResampleCond`、`FxAiMiniMaxVideoSave`、`FxAiMiniMaxVideoSaveV2`、`FxAiMiniMaxBlockCache`、`FxAiVideoGenerator`、`FxAiVideoGeneratorV2`、`FxAiVideoGeneratorV3`、`FxAiVideoGeneratorV4`、`FxAiVideoMerger`、`FxAiVideoPreview`、`FxAiVideoManager`、`FxAiVideoLoad`、`FxAiVideoToVR`、`FxAiVideoSplitFFmpeg`
  - 控制/工具（22）：`FxAiSizeConfig`、`FxAiImageSizeConfig`、`FxAiMaxRatioSize`、`FxAiMaxRatioSizeV2`、`FxAiPhotoSizeConfig`、`FxAiPhotoSizeConfigV2`、`FxAiIntToFloat`、`FxAIGeneratorController`、`FxAiFrameGenerator`、`FxAiFrameGeneratorV2`、`FxAiStartEndIndex`、`FxAiAutoShutdown`、`FxAiSegmentTotalFrames`、`FxAiTextPreview`、`FxAiImagePreview`、`FxAiReleaseResources`、`FxAiGetImagePath`、`FxAiStrToNumber`、`FxAiInputInt`、`FxAiInputFloat`、`FxAiFrameCalculator`、`FxAiRandomSeed`
  - 潜空间 + 模型/LoRA（7）：`FxAiLatentClearReplace`、`FxAiLatentGetFrames`、`FxAiLatentGetFrameCount`、`FxAiLTX23Sampler`、`FxAiMiniMaxUpscaler`、`FxAiLoraLoader`、`FxAiLtxvGuideFrames`
  - 影视（3）：`FxAiScreenManager`、`FxAiScreenLoad`、`FxAiScreenLoadSimple`
- 依赖：**无 `requirements.txt`**；`pyproject.toml` 的 `[project]` 段**未声明 `dependencies`**。但 `__init__.py` L40–L43 在**导入时**对 `required_packages = ["soundfile","psutil"]` 逐个 import，失败即 `subprocess.check_call([sys.executable,"-m","pip","install",<pkg>])`。
- Python / ComfyUI 下限：均未声明（`pyproject.toml` L19 的 `requires-comfyui` 被注释掉）
- torch：是；torchaudio：**是**（`fxai_melroformer.py` L7、`fxai_single_audio_proc.py` L4、`fxai_minimax_image_to_video_v2.py` L14）
- 风险：
  1. **导入即联网**：ComfyUI 启动加载阶段执行 `pip install soundfile psutil` → 离线目标机必然失败；
  2. **导入即全局 monkeypatch**：`__init__.py` L18–L25 替换 `asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost`，影响整个进程的 asyncio 行为；
  3. **仓库无 LICENSE 文件**，合规举证不完整；
  4. 含 `fxai_node_update` 更新检查模块（需外网）；
  5. `torchaudio` 为硬依赖，本机 host python **未安装**。
- **理由**：启动即联网 pip 安装、导入即全局 monkeypatch `asyncio`、且仓库无 LICENSE 文件，与本项目「离线可复现 + 不隐式行为」的底线直接冲突。

### 3.10 ComfyUI-FlashVSR_Ultra_Fast — DEFER

- 路径：`保留备用的节点/ComfyUI-FlashVSR_Ultra_Fast`；HEAD `4820b3f02347bddcbbb9a5a85ab7638fe976366e`（2025-11-20，main）
- 许可：**GPL-3.0**（`LICENSE` 35823 B：`GNU GENERAL PUBLIC LICENSE Version 3`）
- 节点类（3，`nodes.py`）：`FlashVSRNode`、`FlashVSRNodeAdv`、`FlashVSRInitPipe`
- 依赖（`requirements.txt`）：`torch`、`torchvision`、`numpy`、`einops`、`safetensors`、`tqdm`、`pillow`、`huggingface_hub`、`triton; platform_system!="Windows"`、`triton-windows; platform_system=="Windows"`
- Python / ComfyUI 下限：均未声明
- torch：是（`nodes.py` L6、`src/` 下大量）；torchaudio：否
- 风险：
  - **GPL-3.0 传染性**：若随产品分发会牵动整个分发物的许可选择；
  - `nodes.py` L14 `from huggingface_hub import snapshot_download`，L39 从 `huggingface.co/JunhaoZhuang/FlashVSR` 下载全量文件（描述文本 L370/L531 也要求把 FlashVSR 目录放进 `models/`）；
  - Windows 需 `triton-windows`（本机 host python **未安装 triton**）；
  - 仓库根目录自带权重文件 `posi_prompt.pth`（非 LFS 占位）；
  - 最后提交 2025-11-20，相对 0.22.3 栈偏旧。
- **理由**：GPL-3.0 传染性许可 + 依赖 HF 在线下载权重 + Windows 需 triton-windows，三项都属于当前阶段不该引入的风险。

### 3.11 ComfyUI_pxtool — DEFER

- 路径：`保留备用的节点/ComfyUI_pxtool`；HEAD `799155472eb1ffe0562120b9f8ad45ccaf042eb5`（2025-03-02，**master**）
- 许可：**Apache-2.0**（11539 B）
- 节点类（21，由根 `__init__.py` 合并 5 组映射）：`CivitaiHelper`、`RandomArtists`、`ArtistLoader`、`DanbooruCharacterTag`、`E621CharacterTag`、`RandomTag`、`RandomArtistsAdvanced`、`QualityTag`、`NegativeTag`、`PX_Seed`（pxtool.py L780）；`deep_load`、`deep_gen`（azw_nodes.py L152）；`JanusModelLoader`、`JanusImageUnderstanding`（`__init__.py` 内联）；`CheckpointLoaderSimplePX`、`ControlNetLoaderPX`、`IPAdapterModelLoaderPX`、`CLIPLoaderPX`、`DualCLIPLoaderPX`、`TripleCLIPLoaderPX`（Loader.py L506）；`CharacterSelectLoader`（nodes.py）
- 依赖（`requirements.txt`，原文两行）：`transformers>=4.37.0`、**`git+https://github.com/deepseek-ai/Janus.git`**
- Python / ComfyUI 下限：均未声明
- torch：是（`azw_nodes.py` L10、`image_understanding.py` L1、`Loader.py` L9）+ torchvision（`zw_tool.py` L17）
- 风险：
  - **`requirements.txt` 直接依赖 `git+https://github.com/deepseek-ai/Janus.git`** → 本机 github.com 会被 reset，离线源无法提供，**硬性安装阻断**；
  - `CivitaiHelper` 打 Civitai，`DanbooruCharacterTag`/`E621CharacterTag` 打 Danbooru/E621 外部 API（需外网）；
  - `JanusModelLoader`/`JanusImageUnderstanding` 需本地 Janus 权重（`model_loader.py` 用 `from_pretrained` 读本地目录）；
  - 最后提交 2025-03-02，距今约 19 个月，对 0.22.3 的 API 变化风险高；
  - 仓库内提交了 `__pycache__/`。
- **理由**：requirements 写死 `git+github.com/Janus`（本机 github 不可达，离线装不上），且最后提交距今近两年，风险高于收益。

### 3.12 ComfyUI-Storyboard-LLM — DEFER

- 路径：`保留备用的节点/ComfyUI-Storyboard-LLM`；HEAD `d17c9adc8c650806da3dacb408a9ee0e55c9727c`（2026-05-09，main）；版本 v1.0.3
- 许可：**MIT**（1083 B，`Copyright (c) 2026 Lbunc`）
- 节点类（1，`nodes.py`）：`StoryboardNode`（显示名 `Storyboard Generator (DeepSeek)`）
- 依赖（`requirements.txt`）：`requests>=2.28.0`；另注册 3 条 `/storyboard/settings` 路由（`__init__.py` L20–L35）
- **Python：`>=3.13`**（`pyproject.toml: requires-python = ">=3.13"`）← 本机实测 3.11.9，低于下限
- ComfyUI 下限：未声明
- 风险：
  - **`requires-python >=3.13` 高于本机 3.11.9**；
  - `settings.json` 需 DeepSeek `api_key`（默认空），`config.py` 默认 `base_url = https://api.deepseek.com` → 云端 API 依赖；
  - **`settings.json` 内含 `//` 注释（非法 JSON）**，而 `config.py` 的 `load_settings` 在解析失败时 `except: pass` **静默回退默认配置** → 属「错误被吞掉」模式，接入时需自行校验配置真正生效。
- **理由**：声明 requires-python ≥3.13 而本机为 3.11.9，且能力完全依赖 DeepSeek 云 API，与本项目 `prompt/` 自建网关的分工重复。

### 3.13 ComfyUI-DeepSeek-API-connector — OPTIONAL

- 路径：`保留备用的节点/ComfyUI-DeepSeek-API-connector`；HEAD `e5bc511e2d5813e62a189b0c4867786aa42350c4`（2026-08-10，main）；版本 1.0.0
- 许可：**MIT**（1094 B，`Copyright (c) 2026 KaigorodovTuskul`）
- 节点类（1，`deepseek_prompt_node.py`）：`DeepSeekPromptConnector`
- 依赖：**无 `requirements.txt`**；`pyproject.toml` 的 `[project]` 段**未出现 `dependencies` 键**（实测读前 70 行）。源码只用标准库：`json` / `typing` / `urllib.error` / `urllib.request`
- Python / ComfyUI 下限：均未声明（`pyproject.toml` L35 的 `requires-comfyui` 被注释掉）
- torch：本次 `import torch` 扫描**未命中**该仓库 → 未发现 torch 依赖
- 风险：
  - **硬编码 `https://api.deepseek.com/chat/completions`**（`deepseek_prompt_node.py` L7）→ 必须外网 + DeepSeek API key；
  - 功能极窄（1 个提示词节点），与本项目 `prompt/gateway.py` 的自建 `external_api` provider 能力重叠。
- **理由**：零第三方依赖、MIT、标准库实现，作为 `prompt/gateway.py` 调 DeepSeek 的参考实现最干净；但同样需要外网密钥且能力被自研网关覆盖，故只作备选。

### 3.14 comfyui-api-pack — DEFER

- 路径：`保留备用的节点/comfyui-api-pack`；HEAD `b232c33825b0f4ec5f14da0091047eb926962cf8`（2026-10-06，main）；版本 1.0.0
- 许可：**GPL-3.0**（35823 B）
- 节点类（10，`__init__.py` L8）：`LoadWorkflow`、`ApiGenerate`、`ApiSubmit`、`ApiCollect`、`GrokGenerate`、`GrokSubmit`、`GrokCollect`、`OpenAIInference`、`ZeroShotClassification`、`ZeroShotAnswer`
- 依赖：`python-dotenv>=1.0.0`
- **Python：`>=3.12`**（`pyproject.toml`）← 本机 3.11.9
- ComfyUI 下限：未声明
- torch：是（`nodes/api.py` L16、`nodes/grok.py` L13、`nodes/inference.py` L14）
- 风险：GPL-3.0 传染性；`requires-python >=3.12` 高于本机；**全部节点都是外部 API 调用**（远程 ComfyUI / Grok / OpenAI 兼容推理），无本地生成能力且需密钥 —— `.env.example` 列出 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`GROK_ACCESS_TOKEN`、`GROK_REFRESH_TOKEN`、`GROK_CLIENT_ID`。
- **理由**：GPL-3.0 加 `requires-python >=3.12`（本机 3.11.9），且全部能力为外部 API 调用，与本项目 `ComfyUIHttpExecutor` 定位重复。

### 3.15 ComfyUI-CometAPI — DEFER

- 路径：`保留备用的节点/ComfyUI-CometAPI`；HEAD `2e7ea2e8c4f6c3045982a1323124b22f4b253c00`（2026-09-25，main）；版本 1.0.5
- 许可：**CometAPI Non-Commercial License Version 1.0（非 OSI 认可许可，明确禁止商用）**
  - `LICENSE` 第 1 节把 **`Commercial Use`** 定义为：「任何公司或营利组织的使用、任何**公司内部业务**/生产/运营/**交付**/培训/服务/工作流/项目工作、任何付费服务、咨询、外包、SaaS、产品、插件、打包、课程、付费社群、付费模型、付费节点、**商业内容生产**、**客户交付**，或任何直接/间接产生收入、商业价值或商业优势的使用」
  - 第 2 节只授予「非商业用途」的免费、可撤销许可
- 节点类（17，`nodes.py`）：`CometAPIUnifiedImage`、`CometAPIBatchImage`、`CometAPIAsyncImage`、`CometAPIAsyncBatchImage`、`CometAPIAsyncImageReceiver`、`CometAPIUnifiedLLMNode`、`CometAPIUnifiedVideo`、`CometAPIAsyncVideo`、`CometAPIAsyncVideoReceiver`、`CometAPIUnifiedMusic`、`CometAPISettings`、`CometAPIImageCard`、`CometAPIVideoCard`、`CometAPIAudioCard`、`CometAPITextCard`、`CometAPIBatchTextCard`、`CometAPITextAppend`
- 依赖：`requests`、`aiohttp`（均无版本下限）；`requires-python >=3.8`
- torch：是（`nodes.py` L24、`cometapi_chat.py` L17）
- 风险：
  - **许可证明确禁止商用、禁止公司内部业务与客户交付 → 本项目为公司交付型产品，直接不可用**；
  - 全部能力为外部 API（Grsai/RunningHub/ModelVerse/APIMart/自定义 OpenAI 兼容渠道等）且需逐渠道密钥（`nodes.py` L2284 可见 `MODELVERSE_API_KEY` / `YOUYUN_KEY`）；
  - `nodes.py` 超 13000 行单文件，维护与审计成本高。
- **理由**：非商用许可把「公司内部业务 / 客户交付 / 商业内容生产」列为禁止用途，与本项目性质直接冲突。

### 3.16 ComfyUI-HR-Endless-Sampler — OPTIONAL

- 路径：`保留备用的节点/ComfyUI-HR-Endless-Sampler`；HEAD `1681f89ac022371e5e12d565b1486ee104327621`（2026-08-30，main）
- 许可：**Apache-2.0**（11558 B）
- 节点类（4，`__init__.py` L8）：`HREndlessSampler`、`HREndlessSamplerPreview`、`HREndlessSamplerSaveVideo`、`HREndlessSamplerLoadVideo`
- 依赖（`requirements.txt`）：`huggingface-hub>=0.34.0`、**`llama-cpp-python==0.3.35`**、`av>=16.0.0`，首行 **`--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu125`**
- Python / ComfyUI 下限：均未声明
- torch：是（`nodes.py` L14、`video_io.py` L34、`preview.py` L10、`gemma4.py` L33）；torchaudio：否
- 风险：
  - **`llama-cpp-python==0.3.35` 只能从第三方自建索引（abetlen 的 GitHub Pages）获取** → 离线源无法提供，硬性安装阻断；
  - `gemma4.py` L40–L44 在**运行时**从 HF 下载 3 个 GGUF：`google/gemma-4-12B-it-qat-q4_0-gguf`（模型 + mmproj）与 `Janvitos/gemma-4-12B-it-qat-assistant-MTP-Q8_0-GGUF` → 12B 级权重，需外网与可观磁盘；
  - `vendor/` 目录是 MiniMax 官方 H3 prompt-writing skill 文档副本（`SKILL.md` + `references/base-en.txt` + `references/ref-en.txt`，含 SHA-256 校验表），`dependency.md` 明示「不要让渲染时自动刷新 mutable `main` URL」→ **可作本项目 prompt 资产管理的范例**（做法规范：离线内置 + 哈希锁定 + 人工评审更新流程）；
  - 依赖 `llama-cpp-python`，本机 host python **未安装**。
- **理由**：「长视频分块采样 + 用本地 Gemma 逐块续写提示词以维持段间连续性」与本项目 `director/` 段计划目标高度重合，作为设计对照很有价值；但依赖自建 wheel 索引与 12B GGUF 在线下载，离线不可装。

---

## 4. `_PyPI离线包` 审计（`保留备用的节点/_PyPI离线包/`）

目录内容（实测 4 个文件）：

| 文件 | 大小 |
|---|---|
| `bizyair_cloudberry-0.3.2-py3-none-any.whl` | 330,649 B |
| `bizyair_cloudberry-0.3.2.tar.gz` | 325,715 B |
| `nonebot_plugin_comfyui-your-gpu-host-py3-none-any.whl` | 97,830 B |
| `nonebot_plugin_comfyui-your-gpu-host.tar.gz` | 87,897 B |

### 4.1 bizyair-cloudberry 0.3.2 — DEFER

- 许可：**MIT**（METADATA `Classifier: License :: OSI Approved :: MIT License`；包内无独立 LICENSE 文件）
- `Requires-Python: >=3.10`
- 依赖（METADATA / `requires.txt`）：`websockets>=12.0`、`pydantic-settings>=2.0`、`aiohttp>=3.9`、`requests>=2.28`、`bizytrd~=0.1.15`
- 风险：
  - Summary 自述「ComfyUI custom node for cloud-based workflow execution via BizyAir API」→ 把工作流提交到**云端**执行，需 BizyAir API Key 与外网，**不是本地生成能力**；
  - **5 项直接依赖（含 `bizytrd`）均不在离线包目录内** → 该目录不构成可离线安装的依赖闭环；
  - 包内含 `hijack.py`（对宿主行为做劫持），接入前需审阅。
- **理由**：云端执行网关（需 API Key + 外网），且其 5 项依赖都不在离线包里。

### 4.2 nonebot-plugin-comfyui your-gpu-host — DEFER

- 许可：**MIT**（METADATA `License: MIT`；包内无独立 LICENSE 文件）
- `Requires-Python: >=3.10`
- 依赖（15 项）：`aiohttp>=3.10.10`、`tqdm>=4.66.5`、`aiofiles>=24.1.0`、`nonebot_plugin_alconna>=0.53.1`、`pydantic>=1.10.0`、**`nonebot2>=2.2.0`**、`pillow>=11.0.0`、`nonebot-plugin-htmlrender>=your-gpu-host`、`filetype~=1.2.0`、`packaging~=24.2`、`Jinja2~=3.1.4`、`bs4~=0.0.2`、`ruamel.yaml~=0.18.6`、`qrcode~=8.0`、`baidu-translate-api~=1.0.0`
- 风险：
  - **方向相反**：它是「QQ 群 → 调用 ComfyUI 出图」的 NoneBot2 **客户端**插件（`backend/comfyui.py`、`amusement/*`），**不是把完成通知推送到 QQ 的服务端推送器**；
  - **15 项依赖全部不在离线包目录内**，且包含 `nonebot2` 本体、`nonebot_plugin_alconna`、`nonebot-plugin-htmlrender`（后者需 Playwright/Chromium）→ 单纯这两个 wheel 完全装不起来；
  - `baidu-translate-api` 依赖百度翻译 API（需密钥与外网）；
  - 内置工作流（`build_in_wf/*.json`）与本项目架构无关。
- **理由**：对「完成通知」这个目标是反方向的（QQ 指令入口而非通知出口），且 15 项传递依赖全部缺失。

### 4.3 离线包总结论

> **`_PyPI离线包` 仅有 2 个包各自的 wheel + sdist（共 4 个文件），不含任何传递依赖。因此它不构成任何节点的离线安装闭环；两个包在当前形态下都不能在离线机上装成功。**

---

## 5. 与本项目的对接建议（按模块）

| 本项目模块（CONTRACTS） | 可用节点 | 说明 |
|---|---|---|
| `review/notify.py`（第 6 节，多选通知） | **ComfyUI-JobNotify** | 直接用其 webhook 代理投递到飞书/钉钉/QQ 机器人地址；弹窗/声音为前端能力。**必须给 `/jobnotify/webhook` 加鉴权或限制仅本机访问**（当前是无白名单出站代理）。 |
| `prompt/gateway.py`（第 5 节，external_api provider） | ComfyUI-DeepSeek-API-connector（参考） | 1 个节点、纯标准库 urllib 实现，可作为「极简 external_api 传输层」的写法参考；**不要**把它当作正式 provider（无重试/无错误分类）。 |
| `director/plan.py`（第 7 节，段计划） | **ComfyUI-DNNodes** + H3-Chinese-Director（参考） | DNNodes 的「资产卡 → 镜头打包 → 任务台 → Group 拆分 → 分段口按连线动态增长」与段编排契合；H3-Chinese-Director 的六段导演台 / V28 状态机 / PreBake 缓存可作设计对照。 |
| `director/generate.py`（分段生成 + 技术失败分类） | ComfyUI-HR-Endless-Sampler（参考） | 「同一长 latent 切块 + 逐块续写提示词保持连续性」的做法与本项目段间连续性目标一致；其 `dependency.md` 的「离线内置 + SHA-256 锁定 + 人工评审更新」流程可直接移植为本项目 prompt 资产管理规范。 |
| `pipeline/encode.py` / `director` 尾帧 | ComfyUI_MiniMaxH3_Director（暂不可用） | 首尾帧锁定 / 段连续性的现成实现，但需 ComfyUI ≥0.30.0。 |
| 质检/分支 | ComfyUI-SystemOne（远期待评估） | 「程序向模型提结构化问题」可用于生成后质检分支；但要严守 R1：**存在性判定仍必须由程序做**。 |

### 5.1 必须由自研层承担的（节点一律不提供）

1. **R1 存在性判定**：`prompt/match.py` 的三分支纯程序匹配，任何节点的「资产卡 / 角色管理器」都不做归一化名查重与 SUSPECT 队列；
2. **R2 只追加**：全部 16 个仓库都没有「新内容 = 新版本目录」的约束；
3. **R3 LOCK 绑定 `asset_id + version + content_hash`**：没有任何节点有 content_hash 概念。最接近的 DNNodes SelfLift 缓存指纹**不含提示词**（README L196 自认），fxai-toolkit 的角色资源管理器也没有版本+锁语义；
4. **R4 CNT1/CNT2/CNT3 语义隔离**：无节点有任何计数器语义；
5. **R5 项目隔离**：无节点有 `project_id` 维度。

**结论：这些节点只能作为「生成器 / 通知器 / 外部 API 适配器」，绝不能替代 `bettercomfyui/store` 的 `AssetStore` 与 LOCK 判定。**

---

## 6. 关键风险 Top 6（按影响面排序）

1. **本机不具备任何运行时验证条件**：custom_nodes 为空、无任何模型权重、`ComfyUI-Installs` 下**递归无 `python*.exe`**，且 ComfyUI Desktop 的 basePath 指向不存在的 `G:\...` → **所有节点的运行时兼容性只能标 `unverified`**。任何「已在本机验证可用」的表述都是假成功。
2. **三个可验证的版本下限冲突**：`ComfyUI_MiniMaxH3_Director` 声明 `requires-comfyui >=0.30.0`（本机 0.22.3）；`ComfyUI-Storyboard-LLM` 声明 `requires-python >=3.13`、`comfyui-api-pack` 声明 `>=3.12`（本机 3.11.9）。这三个装上去只会得到假成功。
3. **许可风险**：`ComfyUI-CometAPI` 为非商用许可，**明确禁止公司内部业务与客户交付**；`ComfyUI-FlashVSR_Ultra_Fast` 与 `comfyui-api-pack` 为 **GPL-3.0**（传染性）；`fxai-toolkit` **仓库内无 LICENSE 文件**（仅文档声明 MIT）。另 `H3-Chinese-Director` 的工作流依赖含 GPL-3.0 的 ComfyUI-KJNodes。
4. **离线可装性阻断（4 处）**：`ComfyUI_pxtool` 的 `git+https://github.com/deepseek-ai/Janus.git`；`ComfyUI-HR-Endless-Sampler` 的 `--extra-index-url https://abetlen.github.io/...`（llama-cpp-python 0.3.35）；`fxai-toolkit` **启动时执行 `pip install`**；`ComfyUI-MiniMaxH3-ChainDirector` 的 `install_all.py` 从 github.com clone 三个仓库。本机 github.com 会被 reset，这四项全部装不上。
5. **隐式/危险行为**：`fxai-toolkit` 导入时 `subprocess pip install` + 全局 monkeypatch `asyncio.proactor_events`；`ComfyUI-JobNotify` 的 `/jobnotify/webhook` 是无鉴权、无白名单的出站代理（SSRF 面）；`ComfyUI-Storyboard-LLM` 的 `settings.json` 含非法 JSON 注释而 `load_settings` 静默回退默认配置。
6. **`_PyPI离线包` 是假象**：4 个文件、2 个包、**零传递依赖**。`bizyair-cloudberry` 需 5 项依赖 + BizyAir API Key；`nonebot-plugin-comfyui` 需 15 项依赖（含 `nonebot2` 本体与需 Playwright 的 htmlrender）。且后者的方向是「QQ → ComfyUI 指令」，**不是**完成通知推送。

---

## 7. 建议的下一步（把 `unverified` 变成有证据的结论）

1. **先解决运行时前提**：在目标机（≥24GB 显存）上准备可用的 ComfyUI 实例；确认 Desktop 的 basePath 问题（`G:\` 不存在）或改用便携版/源码版，使得存在可调用的解释器。
2. **优先落地 2 个 RECOMMEND**：`ComfyUI-DNNodes`（零 pip 依赖，可直接拷贝测试节点注册与 5 个节点的加载日志）+ `ComfyUI-JobNotify`（零依赖，用一个本地 HTTP 端点验证 webhook 投递）。这两个的验证成本最低、失败模式最清楚。
3. **版本冲突三件套先做决策，不先装**：`ComfyUI_MiniMaxH3_Director` 的 `>=0.30.0` 要么升级 ComfyUI，要么向作者确认；`Storyboard-LLM`(3.13) 与 `comfyui-api-pack`(3.12) 若坚持使用，需为 ComfyUI 单独准备高版本解释器。
4. **许可前置评估**：把 `ComfyUI-CometAPI`（非商用）、`FlashVSR`/`comfyui-api-pack`（GPL-3.0）、`fxai-toolkit`（无 LICENSE 文件）交法务/产品决策，**不要先装再补**。
5. **离线源准备清单**（按节点归集）：`opencv-python-headless`、`imageio-ffmpeg`、`scenedetect<0.8`、`ultralytics`、`cryptography`、`defusedxml`、`python-dotenv`、`einops`、`triton-windows`、`llama-cpp-python==0.3.35`（或替换方案）、`torchaudio`，以及 `Janus`（需替代 `git+` 形式）。
6. **证书边界写进验收**：任何节点的 capabilities 必须按 CONTRACTS 第 7 节如实声明 `verified` 与 `notes`；未在本机跑通的节点一律 `verified=False`。

---

## 8. 未解决问题

1. `ComfyUI_MiniMaxH3_Director` 声明的 `requires-comfyui >=0.30.0` 究竟是硬门槛还是保守声明，**未向作者确认，也未实测降级加载**。
2. `H3-Chinese-Director` 的 19 个子包在 ComfyUI 0.22.3 下的实际加载结果未验证：其父目录没有 `__init__.py`，必须逐个放入 `custom_nodes` 才能被扫描，且第三方依赖节点（含 GPL-3.0 的 KJNodes）未随包提供。
3. `ComfyUI-SystemOne` 的 V3 扩展路径（`comfy_entrypoint` + `ComfyExtension`）虽在本机 0.22.3 的 `nodes.py` L2242 存在，但异步 entrypoint 实例化后**全部 7 个节点的 schema 兼容性未实测**。
4. `ComfyUI-DNNodes` 的 torchaudio 惰性分支（`asset_card.py` L338）在本机无 torchaudio 时的**实际行为未实测**。
5. `fxai-toolkit` 的 `NODE_REGISTRY` 列出 130 项，**实际注册数未逐项验证**（取决于各模块 import 是否成功）。
6. 本机 host python 3.11.9 的包探测（`torch`/`torchaudio`/`laya`/`llama_cpp`/`defusedxml`/`triton`/`einops`/`aiohttp` 等均缺失；`transformers`/`safetensors`/`cryptography`/`requests`/`psutil` 存在）**只代表系统级 Python，不代表 ComfyUI Desktop 内嵌解释器的包状态**；后者因实例不可用无法探测。
7. `_PyPI离线包` 两个包各自的传递依赖版本约束（如 `nonebot2` 与 `pydantic` 的兼容区间）未展开。
8. `ComfyUI-Installs\logs` 列举返回非零退出、无内容，**未取得 ComfyUI 运行日志**作为环境佐证。

---

_本报告所有结论均可回溯到具体文件路径与行号；机器可读版本见 `docs/audit_nodes.json`。审计者：teammate `audit-nodes`（task-1）。_
