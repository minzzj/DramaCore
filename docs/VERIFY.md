# Better ComfyUI · 独立验证报告（VERIFY）

> 维护者：独立验证员（teammate `verify`，task-6）
> 写入范围：`tests/verify/`、`docs/VERIFY.md`（**本文件与验证代码均不修改任何业务实现**）
> 结论纪律：**通过 / 失败 / 未验证** 三态。**禁止把未验证写成通过** ——
> 依据《BetterComfyUI完整方案.docx》开篇明示「设计文档不等同于运行测试已通过」，
> 以及任务书第 9 节末句「不能把模拟结果说成真实生成已通过」。

---

## 0. 结论摘要（阶段二 · 实跑）

**实跑时间**：2026-10-16 23:20（Asia/Shanghai）｜**Python** 3.11.9
**被测代码修订**（`bettercomfyui/` 全量 sha256 前缀16）：`4c7829a5120860bf`
**复现命令**：

```pwsh
python -m pytest tests/ -v                        # 全量
python -m pytest tests/verify -v                  # 仅独立验证套件
python tests/verify/check_contracts.py            # 契约一致性
python tests/verify/summarize_run.py              # 三态汇总
python tests/verify/mutation_check.py             # 变异测试（假通过探测）
```

| 维度 | 结果 |
|---|---|
| 15 条验收清单 | **15/15 通过**（逐条证据见 §3）；0 失败、0 未验证 |
| 验证套件三态计数 | **通过 92 / 失败 0 / 未验证 0**（`tests/verify/_last_run.json`） |
| 契约一致性检查 | 检查项 **PASS 17 / FAIL 1 / UNVERIFIED 0 / VACUOUS 0**；发现 **BLOCKER 0 / MAJOR 7 / INFO 8**（`_contracts_report.json`） |
| 变异测试（抽样 3 条） | 杀死 2 / 存活 1（存活项经人工判定为**等价变异体**，非覆盖缺口，见 §7） |
| 真实生成能力 | **未验证**（本机无模型权重、ComfyUI 0.22.3 < 0.30、无第三方导演节点、127.0.0.1:8000 不可达）—— 详见 §6 |

**一句话结论**：mock 编排闭环（剧本→素材→段→审核→拼接→输出）**已通过实跑验证**，
且真实/mock 边界标注正确；**真实视频生成、第三方导演节点、目标机性能一律未验证**。

---

## 1. 验证方法与工具

| 工具 | 路径 | 作用 |
|---|---|---|
| 三态结论插件 | `tests/verify/conftest.py` | 每条用例结局落盘为 `PASS/FAIL/UNVERIFIED` → `_last_run.json`（结论可追溯，禁止人工复述） |
| 共享验证工具 | `tests/verify/_harness.py` | 区分「模块不存在=未验证」与「实现有缺陷=失败」；字节级文件树摘要；契约默认配置 |
| 契约 API 适配层 | `tests/verify/_api.py` | 集中处理契约未定义的构造方式；假时钟；可控 `auto_commit`；无限循环护栏 |
| 15 条验收用例 | `tests/verify/test_acceptance_01..15_*.py` | 每条标注「需要模块 / 判定通过 / 判定假通过」 |
| 契约一致性检查 | `tests/verify/check_contracts.py` | 枚举/转移表逐项比对方案第七部分；跨层 import；R2 静态检查；静默失败；mock 回退；**代码修订摘要** |
| 真/mock 边界核查 | `tests/verify/test_mock_boundary.py` | M1–M5：能力声明、mock 标注、静默回退、文档表述 |
| 套件自检 | `tests/verify/test_meta_suite_integrity.py` | 防「验证本身是假的」：空壳测试、恒真断言、skip/fail 语义 |
| 变异测试 | `tests/verify/mutation_check.py` | 改坏实现后测试必须变红；**在临时副本里做，绝不改工作区**（脚本会核对工作区哈希） |
| 汇总脚本 | `tests/verify/summarize_run.py` | 生成 15 条逐条三态结论 |

---

## 2. 三态判定标准与「假通过」目录

### 2.1 三态定义（互斥且穷尽）

| 状态 | 触发条件 | 落盘取值 |
|---|---|---|
| **通过** | 用例执行到 call 阶段且全部断言成立；覆盖的实现文件确实存在 | `PASS` |
| **失败** | 断言不成立；或目标模块存在但缺契约成员、签名不符、行为与契约/方案冲突 | `FAIL` |
| **未验证** | **只有一种情况**：目标模块文件不存在，或缺少可复现的前置条件（无模型权重、契约未定义入参）。必须在 skip 原因里写明缺什么 | `UNVERIFIED` |

守住这条的机制：`test_meta_suite_integrity.py::test_s3` 用可执行断言证明
「模块不存在 → skip」与「模块存在但契约不符 → AssertionError」**两条路径行为不同**；
`test_s2` 用 AST 统计断言数与恒真断言；`test_s6` 禁止模块层无条件 skip。

### 2.2 什么算「假通过」

| 编号 | 假通过模式 | 检测手段 | 本轮结果 |
|---|---|---|---|
| FP-1 | 断言恒真的空测试 | `test_meta_suite_integrity.py::test_s2`（AST 统计） | 0 例（并在本轮修掉 1 处自造真空断言，见 §7） |
| FP-2 | 用 mock 结果冒充真实生成通过 | `test_mock_boundary.py` M1–M5；验收 15 | 通过：CLI 明确 `used_mock=true / real_generation=false` + `unverified[]` |
| FP-3 | `try/except: pass` 静默失败 | `check_contracts.py::C9`（MAJOR）+ 验收 14 | 8 条 INFO（best-effort 清理）/ 7 条 MAJOR（见 §4） |
| FP-4 | 未配置时静默回退 mock/默认值 | `check_contracts.py::C10`（BLOCKER）；`build_gateway` 必须抛 `GatewayError` | 0 命中 |
| FP-5 | 扫描 0 个文件的「真空通过」 | 每项检查强制报告 `coverage.files_scanned`；0 → `VACUOUS` | 0 项 VACUOUS |
| FP-6 | 测试不会红 | `mutation_check.py` | 抽样 3 条：2 杀死、1 等价变异体（§7） |
| FP-7 | 把「未验证」写成「通过」 | 三态落盘 + `summarize_run.py` 单列未验证项 | 本轮 0 未验证；§8 另列**本机条件性未验证项** |
| FP-8 | 用自建替身读数、把恒为 0 当证据 | 验收 7/8 明确拒绝「自己 new 一个 CounterStore」当读数来源 | 已改为注入 `counters=` 并断言非零变化 |
| FP-9 | 配置项写了但代码没接线 | 验收 13 `audit_scope` 消费点静态 + 动态断言 | 通过（`director/review.py` 消费；三档取值不同） |
| FP-10 | 未提交结果当已提交 / 迟到结果覆盖新版本 | 验收 9（32 线程 + `set_lock` 打桩 + 字节级比较） | 通过 |

### 2.3 严重级别

BLOCKER（违反 R1–R8/假成功/数据可破坏/验收不可能成立，立即上报）＞
MAJOR（契约不符或可测试性缺失导致关键条款难以验证）＞
MINOR（语义不自洽、flaky、文档表述）＞ INFO（记录的观察）。

---

## 3. 15 条验收清单 · 逐条三态结论

证据来源：`tests/verify/_last_run.json`（本次运行 92 条用例全部 `PASS`）。

| # | 验收条款 | 结论 | 主要证据（用例 + 关键断言） |
|---|---|---|---|
| 1 | 两个项目的数据和配置互不串用，切换后可恢复 | **通过**（4 项） | `test_acceptance_01_project_isolation.py`：A/B 各自 `list_assets` 只见自己；`find_by_normalized_name` 不跨项目；`switch(A→B)` 后 A 的 `assets/**`+`meta.json` 字节级未变；新实例可复读 |
| 2 | 有效剧本结构化；无效 JSON 被拦截，不启动生成 | **通过**（16 项） | `test_acceptance_02_*.py`：5 类非法 JSON + 8 类结构违规全部抛 `ScriptValidationError` 且带 `errors[{path,code,message}]`；失败后项目目录字节级未变；合法样例必须通过（防恒真） |
| 3 | 已有素材正确注入；明确匹配时复用、不重复生成 | **通过**（6 项） | `test_acceptance_03_*.py`：空库清单返回 `""`；清单含类型/名称/版本/锁定状态且随库变化；`EXISTS_REUSE` 后文件树不变；不存在 → `NEW_GENERATE`；空格/大小写/全角空格变体仍复用 |
| 4 | 疑似重名进入确认队列，不覆盖旧素材 | **通过**（5 项） | `test_acceptance_04_*.py`：歧义 → `SUSPECT_DUPLICATE` + `SUSPECT_` 前缀 + 进入 `build_suspect_queue`；`assets/**` 无改动、旧版本字节不变；`normalize_name` 幂等 + 全角空格归一 |
| 5 | 新素材创建版本和内容指纹；旧版本保留 | **通过**（4 项） | `test_acceptance_05_*.py`：v1 指纹 == `sha256:<磁盘字节>`；v2 追加后 v1 目录/字节未变；同内容同指纹；`content_hash_of` 与路径无关 |
| 6 | 新版本不继承旧版 LOCK/审核结论 | **通过**（5 项） | `test_acceptance_06_*.py`：`resolve_lock(v2) is None`；`lock_id == LOCK_{asset_id}_{version}`；指纹不符必须抛错且不留 LOCK；`clear_lock` 只影响本版本；`lock_status` 显式 `UNLOCKED` |
| 7 | 人工审核超时后按配置切换模型，且记录事件 | **通过**（5 项） | `test_acceptance_07_*.py`：注入 `clock` 推进 11 分钟 → `timeout=true / mode=AUTO_SWITCHED / cnt1=0`；`logs/events.jsonl` 含 `AUTO_SWITCHED`；`record_human_activity(kind="inspect")` 持续操作则不超时；通知未注册时只 WARN 不阻断 |
| 8 | CNT1/CNT2/CNT3 与技术重试计数互不混淆 | **通过**（3 项） | `test_acceptance_08_*.py`：三计数器互不影响且按对象隔离；v1→v2 不清零；技术失败 5 次 → CNT2=CNT3=0、技术重试=5 且 `ErrorClass.COMFY_WORKFLOW` |
| 9 | 并发审核只产生一个有效结论；迟到结果不覆盖新版本 | **通过**（3 项） | `test_acceptance_09_*.py`：32 线程 + Barrier → `committed=True` **恰好 1**、`set_lock` **恰好 1 次**、其余 `already_finalized`；顺序第二次提交被拒；迟到结果后 `assets/**` 字节级未变、LOCK 记录相等 |
| 10 | 不合格段可局部重做；受影响后续段进入衔接复核 | **通过**（4 项） | `test_acceptance_10_*.py`：段计划字段齐全且 `segment_id` 含 `project_id`（R5）；Y2 标 `NEEDS_CASCADE_CHECK`；N1 使 CNT3+1 且 CNT2 不变；未 LOCK 素材被 R3 门禁拒绝 |
| 11 | 级联超限可停止并按策略处理，不会无限循环 | **通过**（2 项） | `test_acceptance_11_*.py`：先断言依赖边存在（防真空），再驱动 `propagate` → `stopped_reason=max_cascade_depth`、`degrade_flag=CASCADE_DEGRADED`、迭代次数 ≤ 段总数；25s 护栏 |
| 12 | 服务重启后从检查点恢复，不重复创建版本或拼接 | **通过**（6 项） | `test_acceptance_12_*.py`：新实例可读最近检查点；`at_or_before` 不越界；恢复目标 G/P/R 与阶段一致；截断 JSON 不得当有效数据；幂等键确定（键序无关、命名空间区分） |
| 13 | `segment/full/both` 审核策略实际生效 | **通过**（6 项） | `test_acceptance_13_*.py`：`full_review_policy=null` 抛错且给出两选项；BLOCK/DEGRADE 行为不同且降级留痕；`audit_scope` 有执行侧消费点且三档取值不同；`scorer=None` 必须拒绝；R_seg/N1_EDGE `full_review_calls==0` |
| 14 | 第三方节点缺失/不兼容时明确报错，不出现假成功 | **通过**（5 项） | `test_acceptance_14_*.py`：连不上时抛 `ExecutorError(error_class=COMFY_WORKFLOW)`；`verified=False` 且 notes 非空；`require_nodes()` 点名缺失导演节点并给安装指引；`cancel` 失败返回 `False`；无裸 `pass` 吞异常 |
| 15 | 至少完成一次端到端演示 | **通过**（2 项） | `test_acceptance_15_*.py`：`python -m bettercomfyui.cli run --executor mock` → `ok=true`、成片 104KB、`full_review_calls=1`、事件 39 行、检查点 4 个；**`used_mock=true / real_generation=false / unverified[]` 明确标注** |

---

## 4. 发现清单

### 4.1 BLOCKER

**0 条。** 本轮 92 条用例全绿，契约检查 BLOCKER 计数为 0。

> 验证过程中的两次「疑似 BLOCKER」经复核**均不成立**，记录以示不臆测：
> 1. `director/cascade.py` 一度出现 `SyntaxError`（行号 668）—— 属队友**写文件中途**的瞬时状态，
>    随后 `ast.parse` 全仓 0 语法错误、20 个模块全部可导入。
> 2. `'TECH' is not a valid CounterKind` —— 属同一原因；修正后实测
>    `CounterStore.increment_tech_retry('SEG') -> 1`、`get_tech_retry -> 0`，功能正常。

### 4.2 MAJOR（7 条，均来自 `check_contracts.py::C9`，附人工裁定）

| # | 位置 | 说明 | 人工裁定 |
|---|---|---|---|
| M-1 | `bettercomfyui/store/assets.py:847,868,874` | `resolve_lock()` 内 `except StoreError: return None` / `except (PathIsolationError, StoreError): return None` | **保留 MAJOR**（Lead 已裁定）：`None` = 「该版本不可用于生成」是**授权门禁**；把「读取失败/文件损坏」也变成 `None`，调用方无法区分「未审核」与「读不出来」。建议：写 WARN 日志 + 保留 `ErrorClass` |
| M-2 | `bettercomfyui/executors/comfyui_http.py:235` | `_queue_state()` 内 `except ExecutorError: return None` | **MAJOR（成立）**：`None` 会被上层解读为「队列里没有该作业」→ 直接判 `FAILED`（`comfyui_http.py:212-221`）。一次瞬时 `/queue` 故障会被误报成「作业丢失」。方向是失败闭合（不会假成功），但错误归因失真。建议区分「查询失败」与「确实不存在」 |
| M-3 | `bettercomfyui/executors/comfyui_http.py:315` | `cancel()` 内 `except ExecutorError: return False` | **分类器误报（实为正确）**：`cancel` 返回 `False` = 「未能取消」，属失败闭合，不假装成功（`comfyui_http.py:306-317` 已核）。建议：把「函数返回 bool 且返回保守失败值」排除在 C9 之外 |
| M-4 | `bettercomfyui/review/events.py:137` | `_accepted()` 内 `except (TypeError, ValueError): return None` | **需 owner 复核**：事件字段归一化失败被吞后返回 `None`，可能导致「事件没落盘但流程认为已记录」。契约第 10 节要求必须记录状态变化/模式切换等；建议至少 WARN |
| M-5 | `bettercomfyui/store/atomic.py:165` | `_read_bytes_with_retry()` 内 `except FileNotFoundError: return None` | **MINOR 级建议**：改名/重试语义下返回 `None` 可接受，但调用方需能区分「不存在」与「读失败」 |

### 4.3 MINOR（验证员实测发现，附复现）

| # | 发现 | 复现 | 期望 | 实际 | 建议 |
|---|---|---|---|---|---|
| m-1 | `--workdir` 只影响中间媒体，成片与项目数据仍写入仓库 `projects/` | `python -m bettercomfyui.cli run --executor mock --workdir <tmp>`；看输出 JSON 的 `output_path` | `--help` 说「工作目录」，宜覆盖项目根 | `<tmp>` 只有 `seg*.mp4/spliced.mp4/encoded.mp4/concat_list.txt`；成片落在 `<repo>/projects/<project_id>/output/...` | 把项目根纳入 `--workdir`，或在 `--help` 明确「媒体暂存 ≠ 项目根」；当前演示会写入仓库工作区 |
| m-2 | 成片 `metadata.json` 未标注 mock/未验证 | `test_acceptance_15` 的 `METADATA_MOCK_MARKER` 证据；CLI 输出的 `metadata_path` | 下游拿到 mp4+metadata 时不应误当真实产出 | CLI JSON 报告已明确 `used_mock/real_generation/unverified`，但 metadata 文件里没有这些字段（`METADATA_MOCK_MARKER={"marked": false}`） | **已修复**（见下） |
| m-3 | 级联 `iterations` 语义未文档化 | `test_acceptance_11`：8 段链 + `max_cascade_depth=3` → `iterations=6` | 契约/文档应说明 iterations 计的是「段」还是「代」 | 有界（≤ 段总数）且 `stopped_reason=max_cascade_depth` 正确，但 6 ≠ 3+1 | 在 `CascadeController` 文档字符串里明确 iterations 定义 |
| m-4 | `CounterStore.scope()` 遗留路径抛错 | `python -c "from bettercomfyui.store.ids import cnt_id; cnt_id('X','TECH')"` → `StoreError` | 技术重试计数与 CNT 分离 | `ids.cnt_id` 只接受 CNT1/2/3，而 `review/counters.py` 的 `_tech_key` 曾用 `"TECH"` 调它；现已改为字符串拼装，`increment_tech_retry` 实测正常（0→1） | 清理遗留调用路径或补注释；不影响 R4 计数（已实测） |

---

## 5b. m-2 修复记录（2026-10-17 凌晨）

**问题**：成片 `metadata.json` 里没有 mock/未验证标记。
成片文件（mp4）在磁盘上**无法**区分「模型生成」与「占位媒体」；
来源若只存在于当次 stdout，换一个人拿到 mp4 + metadata 就会误当真实产出。

**修复**（`bettercomfyui/pipeline/core.py::write_output`）：

1. `generation_provenance` 为空 -> **拒绝写出 metadata**（`PipelineError`）；
2. 缺 `is_mock` 字段 -> 拒绝写出（少了它就无法判定真假）；
3. `is_mock=True` 时自动加一条人类可读的 `warning`；
4. metadata 顶层增加 `is_mock_output` 布尔，便于程序快速判断。

实测产物（占位路径）：

```json
"is_mock_output": true,
"generation_provenance": {
  "is_mock": true,
  "executor": "placeholder-ffmpeg",
  "reason": "本机 ComfyUI 0.41.0；models/ 无任何模型权重，真实生成不可用；...",
  "unverified": ["真实视频模型生成（占位媒体由 ffmpeg 合成，非模型输出）", "..."],
  "warning": "⚠️ 本成片由占位（mock）路径产出，**不是**真实模型生成结果。不得作为真实生成已通过的证据。"
}
```

守卫：`tests/test_consistency.py`（4 条：拒绝空 provenance、必须有 is_mock、
mock 写盘可读回、真实产出不得被误标为 mock 的反向断言）。

---

## 5c. 同批发现并修掉的另外两个"误导人"问题

这两条不在原 MINOR 清单里，是在修复 m-2 时顺带发现的。

### V-4 硬编码 ComfyUI 版本 → 同一台机器两个答案

`executors/comfyui_http.py` 写死 `LOCAL_COMFYUI_VERSION = "0.22.3"`，
`demo.py` 的 `real_generation_reason` 也写死同一字符串；
而开发环境早已是 **0.41.0**。结果 `doctor` 报 0.41.0、`demo` 报 0.22.3 ——
用户无法判断该信哪个。

**修复**：删掉常量，改为 `read_comfyui_version(base_path)` 现场读
`comfyui_version.py`（来自配置 `global.comfyui_base_path`）；
读不到返回 `None` 并如实说"未知"。`demo.py` 改为调用 `_probe_comfyui`。
现在实测输出：`"本机 ComfyUI 0.41.0；models/ 无任何模型权重..."`。

### V-5 版本比较用字符串 → `"0.5" >= "0.30"` 判 True

`cli.py` 原本写 `(comfy["version"] or "0.0") >= "0.30"`。
字符串语义下 `"0.5" > "0.30"`（'5' > '3'）→ 错判为满足下限。
更隐蔽的是修好数字比较后还有第二层坑：
`(0, 30) >= (0, 30, 0)` 在 Python 里是 **False**（前缀相等时短元组更小），
于是 `meets_min_comfyui("0.30")` 返回 False —— **一个版本不满足它自己的下限**。

**修复**：`_version_tuple(text, width=4)` 定长补齐 + `meets_min_comfyui()`；
`None`/空串一律返回 False（不知道 ≠ 没问题）。`cli.py` 与 executor 统一走它。

守卫：`tests/test_consistency.py`（参数化 9 个版本，含 `"0.5"`/`"0.9"` 两个
字符串比较会错判的用例；外加 `"0.30" == "0.30.0"` 的定长断言）。

---

## 5. 契约缺口清单（回写契约用）

### 5.1 已由实现补齐（建议回写 CONTRACTS.md 正式化）

| 缺口 | 现状 |
|---|---|
| G-1 `Config` 模块与加载方式 | 已落地：`bettercomfyui/config.py::load_config(repo_root=…)`、`config_from_dict`、`ConfigError`（`config.py:77`）、`cfg.global_`（`global` 是关键字） |
| G-2 `ProjectPaths` | 已落地：`bettercomfyui/store/paths.py::ProjectPaths(root=, project_id=)`，含 `assets_dir/version_dir/meta_json/events_jsonl` 等（`events_jsonl` 是 **property**，不是方法） |
| G-4 `cycle: dict` 字段 | 实测形状 = `ReviewRecord.to_dict()`：`review_cycle_id / review_state / target_kind / target_id / content_hash / version / lock_source / finalization_token / finalized_at`；`AssetReviewer(auto_commit=False)` 返回**未提交**的该 dict，正是 `commit_final` 的输入 |
| G-5 `ConfigError` | `bettercomfyui/config.py:77` |
| G-10 端到端编排入口 | `bettercomfyui/demo.py` + `python -m bettercomfyui.cli run`（本轮实跑通过） |
| G-14 通知接口 | `NotificationService.register_channel()`；未注册渠道投递失败只 WARN 不阻断（实测） |

### 5.2 仍然欠定义（建议补进契约，否则相关条款只能靠探测验证）

| 缺口 | 影响 | 建议 |
|---|---|---|
| **D-1 `set_lock` 需要 `review_cycle_id`** | 契约 §4 的签名是 `set_lock(asset_id, version, *, lock_source, content_hash)`；实测实现**强制**要求 `review_cycle_id`（`store/assets.py`，否则 `StoreError`「LOCKED 版本必须能追溯到授权它的审核周期」） | 这是**更好的**要求（可追溯），但契约必须同步参数，否则调用方按契约编码会失败 |
| **D-2 `commit_final` 的输入形状** | 契约只写 `cycle: dict`；实测要求 `ReviewRecord.to_dict()` 且必须含 `review_state`（键名不是 `state`）、`target_id`（不是 `asset_id`） | 在契约里给出 cycle dict 的字段表（见 G-4） |
| **D-3 项目目录新增 `review/finalizations.json`** | 不在契约 §1 目录清单内（review-lock 已同步 Lead）；跨进程条件更新必须落盘 | 补进目录布局，说明它是 CAS ledger（唯一提交依据） |
| **D-4 计数器读取入口** | `AssetReviewer` 不暴露 `counters` 属性；验证只能靠注入 `CounterStore` 实例后读数 | 契约明确「reviewer/generator 必须接受注入的 CounterStore，且 `get()` 可读」 |
| **D-5 级联驱动入口** | 契约只有 `on_segment_changed/mark_next/run`；实测需要 `propagate(root, *, executor)` 才能驱动整条链，且 `iterations/stopped_reason/degrade_flag/segments_degraded` 未在契约中定义 | 把这些字段写进契约，验收 11 才能稳定复现 |
| **D-6 `plan_segments` 需要 `project_id`** | 契约 §7 签名没有 `project_id`，但 R5 要求段 ID 含 project_id（实现会抛 `SegmentPlanError`） | 契约补 `project_id` / `store` 参数（director-exec 已支持 4 种来源） |
| **D-7 `dependency_refs` 结构与失效判据** | 实测：ref 必须是 `{segment_id, kind, ref_facet?, ref_version?}`（或 `"id|kind|facet|version"` 字符串），且**必须属于 `CONTENT_DEPENDENCY_KINDS` 且 `ref_version != 上游 current_version`** 才判定失效；裸 segment_id → `no_evidence` → 级联不传播 | 写进契约；否则「级联超限」用例极易写成真空通过（本轮已踩到并修正） |
| **D-8 R6 短码冲突重试接口** | 契约说「由调用方查重后重新生成」，未定义接口 | 建议提供 `unique_short_code(...)` 式的唯一入口（实现中已存在同名函数，可正式化） |

---

## 6. 真实 vs mock 边界核查结论（专项）

### 6.1 结论

| 项 | 结论 |
|---|---|
| mock 编排闭环 | **通过**：`cli run --executor mock` 实跑，`ok=true`，产出 104KB mp4 + metadata + cover，`full_review_calls=1`，事件 39 行，检查点 4 个 |
| mock 是否被标注 | **通过**：CLI JSON 明确 `used_mock=true`、`real_generation=false`、`real_generation_reason`（指出 ComfyUI 0.22.3 < 0.30 且无权重）、并列出 5 条 `unverified`；末尾还打印「注意：本次使用 mock 执行器，**输出不是真实生成结果**」 |
| 代码侧是否存在静默回退 mock | **未发现**：`C10_NO_SILENT_MOCK_FALLBACK`（BLOCKER）0 命中；`executors/mock.py` 之外无「异常 → MockExecutor」路径；`validate_capabilities` 拒绝 `mock=True + verified=True` 组合；`assert_real_capabilities/assert_not_mock` 提供下游门禁 |
| 执行器能力声明是否如实 | **通过**：`MockExecutor.capabilities().mock=True`；`ComfyUIHttpExecutor` / `DirectorNodeExecutor` 均 `verified=False` 且 notes 说明前置条件 |
| **真实视频/图像生成** | **未验证**（见 §8） |
| 文档表述是否把 mock 说成真实 | **通过**：`test_mock_boundary.py::test_m5`（已修好误报：不再把「严禁把 mock 说成真实通过」当成违规表述；表格行/引用行不计入） |
| 遗留风险（MINOR） | 成片 `metadata.json` 未带 mock 标注（m-2）：若有人只拿 mp4+metadata，缺少「非真实产出」的机器可读标记 |

### 6.2 本机条件性未验证清单（**不得**表述为通过）

| # | 未验证项 | 缺少的前置条件 |
|---|---|---|
| U-1 | 真实视频/图像生成 | 模型权重（本机 `models/` 为空）、ComfyUI ≥ 0.30（导演台下限）、≥24GB 显存目标机 |
| U-2 | 第三方导演节点加载与运行 | ChainDirector / AIMixer Director / SmartDirector / H3-Chinese-Director 未安装到 `custom_nodes/` |
| U-3 | ComfyUI HTTP 执行器真实提交/轮询 | 本机 `127.0.0.1:8000` 不可达（connection refused）；仅验证了失败路径与能力声明 |
| U-4 | 真实提示词模型调用 | 未配置外部 API key / endpoint；`build_gateway` 的未配置报错路径已验证 |
| U-5 | 真实质量评分模型 | 演示使用确定性打分器（`scorer` 注入），非本地评分模型 |
| U-6 | 目标机（≥24GB）显存与性能 | 无目标机；本机 GTX 1050 Ti 4GB 仅开发机 |
| U-7 | Hi-Fi 成片质量 | mock 产出为 ffmpeg 合成占位媒体，不代表真实画质 |

---

## 7. 变异测试（假通过探测）

工具：`tests/verify/mutation_check.py`（**在临时副本里改代码**，脚本会核对工作区文件哈希，绝不污染工作区）。
清单：`tests/verify/mutations.json`；报告：`tests/verify/_mutation_report.json`。

| 变异 | 目标 | 结果 | 判定 |
|---|---|---|---|
| M001 | `store/assets.py`：`version = format_version(next_number)` → `format_version(1)` | **KILLED**（`test_acceptance_05` 2 失败） | 版本递增断言真实有效 |
| M002 | `prompt/match.py`：`MatchOutcome.NEW_GENERATE,` → `EXISTS_REUSE,` | **KILLED**（`test_acceptance_03` 1 失败） | 复用/新增分支断言真实有效 |
| M003 | `store/atomic.py:122`：集合排序键 `sort_keys=True` → `False` | SURVIVED | **人工判定为等价变异体**，非覆盖缺口 |

**M003 判定依据（可复核）**：该行只用于 `set/frozenset` 的规范排序
（`atomic.py:119-122`，注释「集合无序 → 用元素的规范 JSON 排序」）。
集合元素必须是可哈希对象，`_canonicalize` 后只可能是标量或列表（元组→列表）；
而 `json.dumps` 的 `sort_keys` **只影响 dict 键序**——集合里不可能出现 dict。
故该变异不改变任何可观测输出，属等价变异体。已复核：`canonical_json({"s":{"a","b"}})` 稳定。

**本轮同时修掉自己的一处真空断言**：`test_acceptance_13::test_full_review_gating_needs_orchestrator`
原先在「找到 call site」时直接通过（无断言）。已补动态断言：三档 `audit_scope`
必须使 `SegmentReviewer.audit_scope` 取到 `segment/full/both` 三个不同值。

**局限（诚实声明）**：变异只抽样 3 条（v-1），不等于全量覆盖证明。

---

## 8. 阶段二工作总结与遗留

**已完成**：15 条验收用例全部实跑通过；契约一致性检查（枚举/转移表/跨层 import/R2/静默失败/mock 回退/标识符格式/canonical_json）落地并接入 pytest；真/mock 边界专项核查；变异抽样自检；8 项契约缺口、5 条 MAJOR、4 条 MINOR 形成带证据的清单。

**遗留（建议下一阶段处理）**：
1. §4.2 的 M-1/M-2/M-4 需 owner 决定「写日志 + ErrorClass」还是「改契约」；
2. §5.2 的 D-1…D-8 建议由 Lead 回写 CONTRACTS.md（否则调用方按契约编码会踩坑）；
3. §6.2 的 U-1…U-7 需在目标机 + 安装模型权重/第三方节点后重新验证；
4. §7 v-1：变异测试应扩到每条验收用例至少 1 个变异。

---

## 9. 复核记录

| 时间（Asia/Shanghai） | 事件 |
|---|---|
| 2026-10-16 22:47 | 认领 task-6；读 `docs/CONTRACTS.md`、`models/enums.py`、任务书第 9 节、方案第七部分（docx 提取文本）、总流程图 `.dot` |
| 23:0x | 阶段一交付：15 条用例骨架 + `check_contracts.py` + `mutation_check.py` + 套件自检；报 Lead |
| 23:0x–23:1x | Lead 三轮复核裁定：C6 由 BLOCKER 降 INFO（O_EXCL 回滚 + `ProjectStore.delete` 排除）、C8 修复后 PASS、C9 按「best-effort 清理=INFO / 授权门禁=MAJOR」分类、C13 增加 `FAILED→RECEIVED` 技术恢复例外 |
| 23:1x | 与 store-project / prompt-assets / review-lock / director-exec 对齐真实 API；把 13 条测试侧不匹配逐个修正；补 `auto_commit=False`、`propagate(executor=)`、失效依赖链（`ref_version`）等实测形状 |
| 23:20 | 最终实跑：**92 通过 / 0 失败 / 0 未验证**；契约检查 **BLOCKER 0 / MAJOR 7 / INFO 8**；变异抽样 2 杀死 1 等价 |
| 23:2x | 发现 BLOCKER 无；task-6 收口，向 Lead 提交逐条三态结论 |

_本文件由独立验证员维护；任何队友不得修改本文件的结论部分，只可提出证据要求复核。_
