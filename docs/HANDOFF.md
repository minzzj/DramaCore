# 快速上手（HANDOFF）— 给下一次接手的人

> 目标：**3 分钟内搞清楚「这是什么、能不能跑、怎么跑、哪里不能信」。**
> 详细状态见 [STATUS.md](STATUS.md)；接口真源见 [CONTRACTS.md](CONTRACTS.md)；
> 验收结论见 [VERIFY.md](VERIFY.md)；节点选型见 [AUDIT_NODES.md](AUDIT_NODES.md)。

---

## 1. 这是什么

基于 ComfyUI 的 **AI 视频生产自动化流程**：输入剧本 → 自动拆解 → 增量素材匹配/生成/审核/LOCK →
分段生成视频 → 段级审核与级联返工 → 拼接封装 → 整片审核 → 交付成片。

设计原则（来自《方案》）：**以成熟开源节点拼接为主，只自研编排、状态、校验、审核与界面。**

---

## 2. 三条命令确认它能不能跑

```bash
python -m bettercomfyui.cli doctor      # 环境体检：模块就绪度 / ComfyUI / GPU / ffmpeg
python -m bettercomfyui.cli run         # 端到端最小闭环（mock 路径）
python -m pytest tests/ -q              # 全量测试
```

**本机（2026-10-16 实测）**：

| 命令 | 结果 |
|---|---|
| `doctor` | 全部 21 个期望模块就绪；ComfyUI 0.22.3、**权重 0**；GPU GTX 1050 Ti 4GB；ffmpeg 可用（imageio-ffmpeg） |
| `run` | `ok=true`；成片 **9.04 s / 1920×1080 / h264+aac**；`full_review_calls=1`；事件 39 行；检查点 4 个 |
| `pytest tests/` | **EXIT=0，0 失败** |

---

## 3. ⚠️ 不能信的部分（最重要的一节）

**真实视频生成未验证。** 原因不是代码缺失，而是环境：

1. ComfyUI 版本 **0.22.3 < 0.30**（MiniMaxH3 导演台系的下限；upstream 已到 v0.41.0，**升级即可解锁**）；
2. `models/` 下**权重文件数为 0**（注意：`configs/*.yaml` 与 `put_*_here` 占位文件**不算**权重）；
3. `custom_nodes` 无任何第三方节点；
4. 本机 ComfyUI `127.0.0.1:8000` **实测不可达**；
5. 本机 GPU 4GB（**仅开发机**；目标机显存 ≥24GB）。

因此 `run` 走的是 **mock 路径**：视频内容由 **ffmpeg 合成占位媒体**产生，
不是模型输出。它证明的是「编排逻辑、版本与审核语义、LOCK 授权门禁、计数器口径、落盘」，
**不证明**真实生成可用。

**这份限定被强制落盘**：成片元数据里有
`generation_provenance = {is_mock, executor, reason, unverified[]}`，
并且有回归测试断言元数据文本里必须出现「占位/未验证」字样。
**不要**移除它，也不要把它改成 `is_mock: false` 来「让演示好看一点」——
那正是任务书第 9 节末句禁止的行为。

---

## 4. 五条硬规则（改代码前必须知道）

| # | 规则 | 违反后果 |
|---|---|---|
| R1 | **存在性判断只能由程序做**，AI 只输出「新增素材提示词」 | 误判导致重复生成 / 覆盖角色 / 跨章节身份漂移 |
| R2 | **素材库只追加不覆盖**，新内容一律 = 新版本目录 | 破坏版本可追溯与审核结论有效性 |
| R3 | **LOCK 绑定 `asset_id + version + content_hash`**，新版本绝不继承旧 LOCK | 未审核素材进入生成 |
| R4 | **CNT1/CNT2/CNT3 语义隔离**，技术失败不消耗 CNT2/CNT3 | 计数器口径污染，掩盖真实重做次数 |
| R5 | **项目隔离**，任何路径都含 `project_id` | 数据串用 |

违反任一条即视为缺陷、验收不通过。`tests/verify/check_contracts.py` 会静态检查其中几条。

---

## 5. 模块地图（依赖方向单向，不得反向）

```
models/     枚举 + 数据模型（唯一真源，带语义守卫；他人不得修改）
  ↑
store/      项目隔离 / 文件锁 / 原子写 / 检查点 / 素材库   ← 基座
  ↑
prompt/     模型网关 / JSON 校验 / 归一化匹配 / 增量注入
review/     审核状态机 / CNT1 / H_arb 仲裁 / 唯一提交点
director/   段计划 / CNT2 / CNT3 / 级联终止
executors/  Executor 抽象 / Mock / ComfyUIHttp / DirectorNode 适配
  ↑
pipeline/   拼接 / 封装 / R_full / 交付校验      ← Lead 独占
config.py   三层配置（项目 > 用户 > 默认）
cli.py      doctor / project / asset / run
demo.py     端到端编排
```

**禁止的跨层 import** 由 `check_contracts.py::C5_FORBIDDEN_IMPORTS` 静态拦截。

---

## 6. 踩过的坑（别重复踩）

| 坑 | 症状 | 正确做法 |
|---|---|---|
| **顶层包相对导入** | `attempted relative import beyond top-level package` | 子包内用 `..`；直接位于 `bettercomfyui/` 下的文件用 `.` |
| **`_StrEnum` 继承 str** | 枚举漏转，YAML 导出报 `RepresenterError` | 判枚举必须显式 `isinstance(x, Enum)`，不能用 `isinstance(x, str)` |
| **权重计数口径** | `doctor` 一度报「权重 11」——其实是 `configs/*.yaml` | 只认 `.safetensors/.ckpt/.pt/.pth/.bin/.gguf/.onnx/.sft` |
| **`review()` 调用顺序** | `StaleSegmentVersion` | 先登记 attempt + 设 `current_version`，再 `review()`；用返回的深拷贝替换本地对象 |
| **传了 `material` 就不调 `scorer`** | 注入的 scorer 静默不生效 | 想走 scorer 就传 `material=None` |
| **`ProjectStore.delete` 前必须 `close`** | `ProjectBusyError`，且残留项目目录 | 先 `close(pid)` 再 `delete(pid, confirm_token=pid)` |
| **测试污染 `projects/`** | 跑完测试目录里残留十几个项目 | 测试用 `tmp_path` 覆盖 `cfg.global_.project_root` |

---

## 7. 纪律（对本仓库所有 Agent 生效）

1. **不得声称未验证的东西已验证。** 方案原文即写明「设计文档不等同于运行测试已通过」。
2. **mock 与真实必须可区分**，且这个区分要**落盘**，不能只在 stdout。
3. **测试必须能失败**：写完测试后故意破坏实现，确认变红 —— 凡不通过变异自检的测试不算数。
4. **不许真空测试**：断言前先证明前置条件成立（例如测级联先断言依赖边存在）。
5. 契约冲突以《方案》第七部分「计数口径、仲裁、版本、最终输出」为准；
   改契约先改 `CONTRACTS.md` 并说明理由。
6. `FAILED`（技术失败）与 `BLOCKED`（质量不合格）语义不同，**绝不混用、绝不互相计数**。

---

## 8. 下一步优先级

1. **要真实生成**：升级 ComfyUI 到 ≥0.30（建议 0.41）+ 准备权重 + 在目标机（≥24GB）装干净实例。
   然后把 `executors/` 的 `verified` 显式改真 —— 唯一放行口是 `assert_real_capabilities()`。
2. 收敛剩余 MAJOR 发现（见 [VERIFY.md](VERIFY.md) §发现清单）。
3. 补真实质量评分模型（当前审核用确定性打分器；方案要求审核**全部本地完成**）。
4. 按 `docs/AUDIT_NODES.md` 的 `RECOMMEND` 清单装节点（当前 `runtime_compat` 全为 `unverified`）。
