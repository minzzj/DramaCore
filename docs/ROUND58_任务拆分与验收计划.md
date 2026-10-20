# ROUND58 任务拆分与验收计划

> 背景：用户要求把庞大需求拆分给**新建的专项智能体**（旧智能体上下文不复用），并指出"很多项目一起进行不对"。  
> 当前策略：**主线程只做集成/验收/发布**；专项子智能体（隔离上下文）完成各自写域；硬件适配+安装部署为当前第一优先，其余专项收敛后串行验收。

## 0. 为什么用 subagent 而不是新 teammate

- Team member limit = 8，已有 8 个 teammate（旧项目上下文，用户认为会污染）；
- 因此改用 `subagent`：**隔离上下文、独立工作目录、不改 Lead 对话上下文**，可并行也可随时重开；
- 旧 teammate 全部保持 inactive，不参与本 ROUND；如需长期角色，后续可在有空位时新建。

## 1. 专项拆分

| 编号 | 专项 | 负责人/子智能体 | 写域 | 状态 |
|---|---|---|---|---|
| R58-A | H3 精简版提示词 docx 集成（12,890 字，默认系统提示词） | prompt-docx 专项 | `bettercomfyui/prompt/data/h3_concise_v1.txt`、`h3_handbook.py`、`everything_node.py`、`one_stop_node.py` 默认、UI 提示词面板、相关测试/文档 | 运行中 |
| R58-B | DeepSeek JSON 输出契约修复（"很多 JSON 配置不对"） | json-schema 专项 | `bettercomfyui/prompt/{script_build,validate,inject,normalize,gateway}.py`、`config/default.yaml`、JSON 契约测试/文档 | 运行中 |
| R58-C | 多品牌硬件适配 + Windows/Linux/macOS 一键安装部署 | hardware/install 专项 | `bettercomfyui/hardware.py`（Lead 已建）、`bettercomfyui_comfy/hardware_adapt.py`、`__init__.py`、`routes.py`、`install/*`、硬件测试、适配/安装文档 | 运行中（当前第一优先） |
| R58-D | 发布前安全审计（密钥/漏洞/许可证） | security 专项（只读+审计报告） | 只写 `docs/ROUND58_安全审计.md` | 运行中 |
| R58-E | GitHub 发布预检（stage 差异/打包/凭据流程，不 push） | release-prep 专项 | 只写预检清单/Release 草稿，不执行发布 | 运行中 |
| Lead | 集成 UI/跨专项补丁、回归测试、版本/发布、用户报告 | Lead | 其余集成文件 | 进行中 |

## 2. 硬件适配 + 安装部署审查清单（Lead 复核用）

### A. 检测能力（`bettercomfyui/hardware.py`）
- [ ] import 时不强制 import torch（无 torch 环境可安全 import）；
- [ ] 品牌优先级：NPU > (AMD/NVIDIA CUDA) > Intel XPU > Apple MPS > CPU；
- [ ] 能通过 torch / nvidia-smi / rocminfo / xpu-smi / npu-smi 兜底识别；
- [ ] Tier 口径与显存口径一致（H3 ≥48GB；低显存为 B/C）；
- [ ] CLI `doctor --json/--markdown`、`matrix` 可用；JSON 可序列化；
- [ ] 不把"检测到卡"当作"真机验证过"。

### B. 运行时适配（`hardware_adapt.py`）
- [ ] Ascend 仅在 `torch_npu` 可用时打补丁；补丁幂等、失败可回退/可解释；
- [ ] ComfyUI `is_nvidia()` / `supports_fp8_compute()` 设备保护有明确记录；
- [ ] comfy-kitchen INT8 NPU 状态：要么真实修复，要么明确"需手动/未验证"，不静默；
- [ ] 其他品牌不改 ComfyUI 核心；只给启动参数/环境变量；
- [ ] 无 ComfyUI 环境下 import/调用不崩溃。

### C. 安装器
- [ ] `--dry-run` 可执行、不写系统/不装包/不下载模型；
- [ ] `attach` 模式验证已有 ComfyUI（`main.py`），不覆盖用户文件；
- [ ] `bootstrap` 模式：venv、clone、torch 后端选择、requirements、插件部署、启动器；
- [ ] NVIDIA/AMD/Intel/MPS/Ascend 分支正确；Ascend 先检查 CANN/torch_npu，不硬装错 wheel；
- [ ] 生成启动器包含正确 flags（Ascend：`--disable-mmap --disable-smart-memory`，env）；
- [ ] 失败给出可操作错误/回滚说明，不破坏现有环境；
- [ ] Windows PowerShell / Linux bash / macOS 入口均可读、参数一致。

### D. 路由与 UI
- [ ] `GET /bettercomfyui/hardware` 与 `/api/...` 都能返回 JSON（无密钥输出）；
- [ ] 节点启动日志报告硬件摘要；UI 显示当前硬件/Tier/补丁状态（Lead 集成）；
- [ ] 任何"支持"文案必须与 Tier 一致，禁止夸大。

### E. 测试与文档
- [ ] `tests/test_round57_hardware.py` 覆盖 mock 品牌/兜底/安装器 dry-run；
- [ ] 文档逐品牌步骤、命令、未真机验证声明、引用来源；
- [ ] 不重启/不实际安装/不下载模型/不调用外部 API。

## 3. 发布闸门（全部通过才 push）

1. 安全审计：**无 P0 明文凭据/密钥泄漏、无发布包泄漏**；P1 有处理或明确记录；
2. 全量 `pytest -q` + `tests/verify` 通过（未验证项如实保留）；
3. 提示词新默认 + JSON 契约测试通过；UI 冒烟通过；
4. 硬件检测/安装器 dry-run/文档完成；无"已真机验证"虚假声明；
5. 版本号 `0.1.3-internal`、README/Release notes/许可证正确；
6. stage 与源码同步、Secret scan、文件清单核验；
7. 使用既有 GitHub 凭据（临时 SSH/PAT）push + 预发布；发布后验证仓库/下载，销毁临时 key/token。

## 4. 已知风险（发布前必须写进 Release notes）

- 无 4090/昇腾/AMD/Intel 真机可用：硬件适配为代码+文档级，**未真机验收**；
- CPU 本地只能跑编排/UI/剪辑/占位，H3 真实生成需加速卡；
- 旧工作流/旧 UI 缓存需重新加载最新工作流并硬刷新；
- 提示词切换为 12,890 字精简版后，仍保留历史 41,966 字完整版可选。
