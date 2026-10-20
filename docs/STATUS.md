# Better ComfyUI · 开发状态（STATUS）

> 供下一位 Agent 接手的开发状态说明（任务书第 10 节要求）。
> 维护者：Lead。最近更新：2026-10-16 深夜（Asia/Shanghai）。

---

## 1. 一句话现状

> **第一阶段 15 条验收清单：15/15 通过，0 失败、0 未验证**（独立验证员 `verify` 实跑，逐条证据见
> [VERIFY.md](VERIFY.md) §3）。全量测试 **EXIT=0，0 失败**（909 用例 / 57 文件）。
> 契约一致性检查：**无 BLOCKER 级问题**。
> **真实视频生成仍未验证** —— 本机无模型权重 + ComfyUI 0.22.3 < 0.30，属环境限制，非实现缺失。
> MAJOR 发现已从 7 条收敛到 2 条（均为验证员自判的误报 / MINOR 级建议）。

复现命令与实测结果：

```
python -m bettercomfyui.cli doctor                    # 如实报告：权重 0、0.22.3 < 0.30、真实生成不可用
python -m bettercomfyui.cli run                       # ok=true；成片 9.04s / 1920x1080 / h264+aac
python tests/probes/probe_isolation.py                # 验收 1：12/12 条独立证据通过
python tests/probes/probe_counters.py                 # CNT1/2/3 隔离 + R_full 只跑一次：16/16 通过
python -m pytest tests/ -q                            # EXIT=0，0 失败（909 用例）
python -m pytest tests/verify -v                       # 通过 92 / 失败 0 / 未验证 0
python tests/verify/check_contracts.py                 # 无 BLOCKER 级契约问题，exit 0
```

`run` 的实际产物（已用 ffmpeg 校验，**不是「文件存在即通过」**）：

| 项 | 实测值 |
|---|---|
| 成片时长 | **9.04 s**（3 段 × 3 s，证明三段真的拼接成功，没有漏段） |
| 分辨率 / 帧率 | 1920×1080 / 24 fps |
| 编码 | h264（视频）+ aac（音频），音轨存在 |
| 文件大小 | 101.9 KB |
| 内容指纹 | `sha256:5f6a7a1b…` |
| 事件日志 | 39 行 `logs/events.jsonl` |
| 检查点 | ASSET_REVIEW / DIRECTOR_PLAN / SEGMENT_REVIEW / OUTPUT |
| `full_review_calls` | 段级路径 **0**，最终拼接后 **1**（R_full 只在 S_OUT 前跑一次） |

**限定（必须一并理解）**：视频内容是 ffmpeg 合成的**占位媒体**，不是模型输出。
因此这个闭环证明的是「编排逻辑、版本与审核语义、LOCK 授权门禁、计数器口径、落盘」，
**不证明**真实视频生成可用。

---

## 2. 环境事实（已实测，非推断）

| 项 | 实测结果 | 影响 |
|---|---|---|
| 工作区 | `[PROJECT-ROOT]` | 起始状态**只有文档 + 16 个备用节点仓库，无任何代码** |
| ComfyUI（有效副本） | `[USER-HOME]\ComfyUI-Installs\ComfyUI\ComfyUI`，版本 **0.22.3** | 低于 Director 要求的 0.30.0 |
| ComfyUI Desktop 配置 | `basePath = G:\...\客户\短剧\comfyui`，**G: 盘不存在** | 该实例不可用 |
| custom_nodes | 只有 `example_node.py.example`、`websocket_image_save.py` | 无任何第三方节点 |
| 模型权重 | `models/` 下**0 个权重文件**（11 个 `configs/*.yaml` 与 24 个 `put_*_here` 占位文件均不算权重） | **无法进行任何真实生成** |
| 本机 GPU | GTX 1050 Ti / 4096 MiB / 驱动 560.94 | **仅开发机**；目标机显存 ≥24GB（用户确认） |
| Python | 3.11.9（另有 3.13.9 可执行） | 满足 ChainDirector(`>=3.10`)、Director(`>=3.10`) |
| pytest / PyYAML / Pillow / numpy / requests | 均已安装 | 测试可用 |
| ffmpeg | 不在 PATH，但 `imageio-ffmpeg` 提供 `ffmpeg-win-x86_64-v7.1.exe` | `pipeline` 已**真实**拼接/封装/探测 |
| 网络 | **GitHub API 可达**（返回 upstream 最新 tag `v0.41.0`）、**PyPI 可达** | 可升级 ComfyUI、可装依赖 |
| 历史日志 | ComfyUI Manager 曾 `git clone` 失败（Connection was reset） | 属**时点性**问题，非持续封锁 |

> 注：`models/` 的权重计数口径是踩过坑后修正的。早期实现只排除 `put_*` 占位文件，
> 把 11 个 yaml 配置当成模型，`doctor` 一度输出「模型权重文件数: 11」，
> 差点得出「有模型可用」的错误结论。现在只认
> `.safetensors/.ckpt/.pt/.pth/.bin/.gguf/.onnx/.sft`，并有回归测试守住
> （`tests/test_cli_doctor.py::test_probe_counts_only_real_weights`）。

---

## 3. 三个必须解决的问题（阻塞真实生成，不阻塞自研层）

1. **ComfyUI 版本偏低**：`ComfyUI_MiniMaxH3_Director` 声明 `requires-comfyui >=0.30.0`，
   本机 0.22.3。upstream 已到 **v0.41.0**，因此**升级即可解锁**。
   → 需用户决策：升级现有 Desktop 实例 / 在目标机（≥24GB 显存）另装干净实例。
   **决定：自研层不依赖具体 ComfyUI 版本**，通过 `executors/` 适配层隔离。
2. **无模型权重**：任何真实出图/出片都不可能，本阶段以 `MockExecutor` 证明编排闭环，
   `ComfyUIHttpExecutor` 标 `verified=False`。**严禁把 mock 结果说成真实生成通过。**
3. **Python 版本冲突（局部）**：`Storyboard-LLM` 要求 `>=3.13`、`comfyui-api-pack` 要求 `>=3.12`，
   本机 3.11.9。两者均已被审计判为 `DEFER`，不进入第一阶段路径。

---

## 4. 已完成的交付物

### 4.1 冻结的契约与数据模型（Lead 独占，他人不得修改）

| 文件 | 内容 |
|---|---|
| [docs/CONTRACTS.md](CONTRACTS.md) | 接口契约 **v1.1**：5 条硬规则 R1–R5、目录布局、18 个 ID 规范、原子写/文件锁、7 个模块的公开接口、配置键与优先级、事件日志 schema、变更流程。已含 R9（stage→ResumeTarget 映射）与「顶层包相对导入陷阱」 |
| [bettercomfyui/models/enums.py](../bettercomfyui/models/enums.py) | **枚举唯一真源**：12 个审核状态码、22 条状态转移、LOCK 来源 / 段来源 / 审核模式 / 降级标记 / 三个计数器 / 素材与视频段枚举 |
| [bettercomfyui/models/entities.py](../bettercomfyui/models/entities.py) | **数据模型唯一真源**：`AssetEntry`/`AssetVersion`/`ReviewRecord`/`ReviewAttempt`/`SuspectRecord`/`Segment`/`SegmentAttempt`/`MovieMetadata`/各参数段 `Config`。含**语义守卫**，非法组合在构造时即抛错 |
| [bettercomfyui/config.py](../bettercomfyui/config.py) | 配置加载：三层优先级、未知键报错、数值范围校验、跨字段陷阱校验、密钥不落盘 |
| [config/default.yaml](../config/default.yaml) | 由 dataclass **程序生成**（避免手写键漂移），与方案参数表逐项对应 |
| [bettercomfyui/pipeline/](../bettercomfyui/pipeline/) | 整片阶段：拼接门禁、封装、R_full、失败策略、交付校验 |

### 4.2 审核裁定记录（本轮真实歧义的裁决，均已写入契约）

| # | 歧义 | 裁定 |
|---|---|---|
| 1 | 原子写临时文件名 | 改为 `{name}.{pid}.{token}.tmp`；并明确**唯一 tmp 不能替代文件锁** |
| 2 | 疑似重复记录落哪 | 落**独立文件 `suspects.json`** + 独立锁；`meta.json` 字节保持不变，使「绝不覆盖」可被字节断言 |
| 3 | `Config` / `ProjectPaths` 归属 | `Config` 定义在 `models/entities.py`；`ProjectPaths` 在 `store/paths.py` |
| 4 | 检查点恢复目标映射 | 新增 R9 映射表 + **检查点自描述**（存档当时的 resume_target） |
| 5 | `AWAITING_REVIEW` 无入边（规格缺口） | 确认为**初始状态**；人工周期**不经过 SCANNING**；不得伪造转移边 |
| 6 | FALLBACK 时 `ReviewRecord.version` 指向谁 | 指向**被选中的最高分候选版本**，且 `content_hash` 必须与之一致 |

### 4.3 测试

`tests/test_foundation.py` — **39 条全部通过**。

**变异测试自检（证明测试不是恒真）**：故意破坏 3 处实现，对应测试全部变红，恢复后全绿：
| 变异 | 被杀的测试 |
|---|---|
| 去掉「ALLOWED 必须带 lock_source」守卫 | `test_review_record_allowed_must_carry_lock_source` |
| 让任意状态的段都能进拼接 | `test_splice_gate_rejects_unusable_segments_and_orders_by_index` |
| 允许隐式整片审核策略 | `test_full_policy_requires_explicit_config` |

### 4.4 节点审计（teammate `audit-nodes`，task-1 已完成）

[docs/AUDIT_NODES.md](AUDIT_NODES.md)（425 行）+ [docs/audit_nodes.json](audit_nodes.json)（16 仓库，全部 `runtime_compat=unverified`）。
**verdict：RECOMMEND 2 / OPTIONAL 5 / DEFER 9。**

对自研层最要紧的三条结论：
1. **16 个仓库无一实现 R1–R5**（无 content_hash / LOCK / 项目隔离 / 计数语义）。
   DNNodes 的缓存指纹按其 README 自认「不含提示词」，**绝不能替代 `AssetStore`**。
   → 这从外部证据上确认了「自研编排层不是重复造轮子，而是唯一承载数据正确性的地方」。
2. `ComfyUI-JobNotify` 的 `POST /jobnotify/webhook` 是**无鉴权、无白名单的出站代理（SSRF 面）**，
   接入 `review/notify.py` 前必须加访问控制。
3. **许可证**：`ComfyUI-CometAPI` 为非商用许可（**明文禁止公司内部业务/客户交付**）；
   `FlashVSR_Ultra_Fast` 与 `comfyui-api-pack` 为 GPL-3.0；`fxai-toolkit` 仓库内**无 LICENSE 文件**。
   → 面向商业交付时这三类不可直接采用。

---

## 5. 团队与分工（写范围互不重叠）

| 成员 | 独占写入范围 | 任务 | 状态 |
|---|---|---|---|
| **Lead** | `models/`、`config.py`、`config/`、`pipeline/`、`docs/CONTRACTS.md`、`docs/STATUS.md` | 契约冻结、集成、最终验收 | 地基完成 |
| `store-project` | `store/`、`tests/test_store_*.py` | task-2 项目隔离/文件锁/原子写/检查点 | 进行中 |
| `prompt-assets` | `prompt/`、`tests/test_prompt_*.py` | task-3 网关/校验/归一化匹配/注入 | 进行中 |
| `review-lock` | `review/`、`tests/test_review_*.py` | task-4 状态机/CNT1/仲裁/唯一提交 | 进行中 |
| `director-exec` | `director/`、`executors/`、`tests/test_*_*.py` | task-5 段计划/CNT2/CNT3/级联/执行器 | 进行中 |
| `verify` | `tests/verify/`、`docs/VERIFY.md` | task-6 独立验证 15 条验收清单 | 阶段一进行中 |
| `audit-nodes` | `docs/AUDIT_NODES.md`、`docs/audit_nodes.json` | task-1 节点审计 | **已完成** |

---

## 6. 下一步（按优先级）

1. 收齐 store / prompt / review / director 四个模块，跑通**集成闭环**（mock 执行器）。
2. 补 `bettercomfyui/cli.py` 与端到端演示脚本（任务书验收 15）。
3. `verify` 逐条实跑任务的 **15 条验收清单**，给出通过/失败/未验证三态结论。
4. `pipeline` 的 ffmpeg 真实拼接/封装在本机验证（本机有 imageio-ffmpeg，可验证）。
5. 就「ComfyUI 升级到 ≥0.30（建议 0.41）」与「目标机 ≥24GB 显存实例」向用户取得决策后，
   再推进真实模型路径。

---

## 7. 纪律（对本仓库所有 Agent 生效）

1. **不得声称未验证的东西已验证。** 方案原文即写明「设计文档不等同于运行测试已通过」。
2. **mock 与真实必须可区分**；`MockExecutor` 产出必须带标记。
3. 契约冲突以《方案》第七部分「计数口径、仲裁、版本、最终输出」为准。
4. 改契约先改 `docs/CONTRACTS.md` 并说明理由，由 Lead 裁定。
5. 技术失败（`FAILED`）与质量不合格（`BLOCKED`）语义不同，绝不混用，绝不互相计数。
