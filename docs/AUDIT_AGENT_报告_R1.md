# 独立审计 · 第一轮报告（R1，第 6 步产出）

- 审计人：独立第三方审计工程师（未参与实现；不接受实现方口头解释）
- 需求依据：`文档/愿景部分_纯文本.txt` + `文档/BetterComfyUI_完整总流程图.dot`（均逐字通读）
- 配套文档：`docs/AUDIT_AGENT_理解_R1.md`（第 1 步：需求理解）、`docs/AUDIT_AGENT_问题_R1.md`（第 2/3 步：现实盘点 + 逐项问题清单）
- 审计日期：2026-10-18（本机）
- 未修改 `文档/` 下任何需求文件与逻辑图。

## 一、结论摘要

1. **产品主链是真实存在的**：23 个 ComfyUI 节点全部在 `/object_info` 注册；18 个工作流 JSON 结构全部通过（节点类型全部已注册、无悬空 link）；项目/素材/审核/导演台/拼接/整片审核/输出均有真实代码与可落盘数据，不是空壳。验收级测试（277 项）在审计前即 276 通过 / 1 未验证（环境原因）。
2. **本轮发现并修复 4 个真实缺陷 + 1 个验收基础设施缺陷**，全部附可执行回归测试：
   - **BLOCKER**：`run_demo(executor="comfyui")` 用 ffmpeg 占位媒体冒充真实生成（元数据 `is_mock=false / executor=comfyui`，节点状态「成功」）→ 已改为失败闭合；
   - **MAJOR**：整片审核 `R1b 音画同步` 恒为常量 80（无论音轨是否截断）→ 已改为真实测量音视频流时长；
   - **MAJOR**：doctor / 面板在「CPU 服务 + 4GB 显卡」环境下报「真实生成可用」→ 已并入计算设备（≥24GiB 非 CPU）判定；
   - **MAJOR**：素材审核器状态不跨节点落盘 → 「模型三轮取最高分」「人工等待超时→切模型→模型轮」「H4→H2」在产品路径不可达 → 已落盘 restore/超时 tick/续跑模型轮；
   - **MAJOR**：验收命令 `check_contracts.py` 在默认 Windows 控制台因 `UnicodeEncodeError` 退出码 1（不是契约问题）→ 已修 + 回归测试。
3. **仍有 6 类未完成项**（见第四节），其中影响「开箱即用/完整逻辑图覆盖」的 3 项为：`W0_完整逻辑图全流程` 缺 SG3/SG5；GPU 素材生成分支缺图像 checkpoint（开箱不可运行）；视频审核在 GPU 总流程需人工两步 + 通知 UI（H8）缺失。
4. **本轮未验证项（如实声明）**：真实 H3 出片、真实外部提示词 API、真实语义审核模型、24GB 目标机显存行为 —— 本机 ComfyUI 以 `--cpu` 运行、无可用 GPU（`/system_stats.devices=[cpu]`），按安全要求未点任何含 H3 权重的队列。

## 二、完成项（本轮实际改动 + 验证）

### 2.1 修复 Q1（BLOCKER）：真实执行器假成功

- 修复前复现（运行时证据）：`BetterComfyUIRun().run(..., executor="真实 ComfyUI")` → `status="成功"`、`used_mock=False`、成片元数据 `is_mock_output=false / generation_provenance.executor="comfyui"`；实际媒体由 `_make_placeholder_video()`（ffmpeg 纯色+正弦）生成。
- 修复：`run_demo` 只支持占位媒体，非 mock 执行器**失败闭合**（ok=False、明确错误、不建项目、不出成片）；mock 路径 provenance 固定 `is_mock=true / executor=placeholder-ffmpeg`；ComfyUI 节点「真实 ComfyUI」、`POST /bettercomfyui/run`、面板选项同步显式拒绝；`full_review_state` 由 `str(True)` 改为 `PASSED/FAILED`。
- 验证：`tests/test_demo_executor_honesty.py`（6 条）+ `tests/test_comfy_node_controls.py::test_executor_choice_selects_real_path`（改为断言显式报错且不调用 run_demo）。

### 2.2 修复 Q2（MAJOR）：音画同步常量 80

- 修复前：`local_scorer.video_scores()` 中有音轨即 `sync = 80.0`（常量覆盖），阈值 70 → 永远通过。
- 修复：`_stream_end_seconds()` 用 `ffmpeg -map 0:v:0/0:a:0 -c copy -f null -` 真实测量两流最后时间戳；`_av_sync_score()` 时长对齐评分（1/fps 容差、长流 10% 归零、无音轨/测不到=0，不编分）。
- 验证：`tests/test_local_scorer_audio_sync.py`（3 条）：等长 ≥90；4s 视频+1s 音频 ≤30；两者差 ≥50（证明非常量）；无音轨=0。

### 2.3 修复 Q3（MAJOR）：真实生成可用性假阳性

- 修复前：`GET /bettercomfyui/doctor` 实测 `real_generation_available=True`（权重齐全+0.41），而 `/system_stats.devices=[cpu]`、宿主 GPU 仅 4096 MiB → 真实 H3 必不可能运行；CLI doctor 同样只查权重/版本。
- 修复：新增 `pipeline/core.py::evaluate_compute_devices()` + `H3_MIN_VRAM_MIB=24000`；`cli._probe_compute_capability()`（通 `/system_stats`）与插件 `routes._server_compute_capability()`（服务进程内 torch 探测）并入判定；面板显示计算设备与原因。
- 验证（实测输出）：修复后 `python -m bettercomfyui.cli doctor` → `真实生成：不可用 —— 只有 CPU 设备（ComfyUI 以 --cpu 启动或无可用加速器）——真实 H3 生成不可用`；`tests/test_cli_doctor.py`（新增 5 条设备判定用例 + 权重齐全但设备不足→不可用）；`tests/test_comfy_routes.py`（/doctor 含 compute 字段与原因）。

### 2.4 修复 Q4（MAJOR）：素材审核跨运行状态丢失

- 修复前：`BetterComfyUIAssetReview` 每次执行新建 `AssetReviewer` 且不 restore/snapshot（产品代码零调用）；`poll_timeouts()` 无产品调用者；`review_version` 对 SCANNING 周期一律 reused（切换后模型轮不执行）→ 三轮最高分/人工超时/H4→H2 在产品路径不可达。
- 修复：
  - 节点新增审核器状态落盘 `<项目>/review/reviewer_state.json`（原子写；损坏时按新状态继续并显式说明）；
  - 每次进入审核节点先 restore + `poll_timeouts()` 推进超时；
  - 新增 action「发起人工审核（等待）」；人工通过/拒绝前 `record_human_activity()` 顺延超时（H1C）；
  - `review_version` 对 SCANNING 周期在模型模式下**真正执行一轮模型审核**（H4→H2 续跑）。
- 验证：`tests/test_review_persistence_cross_run.py`（3 条）：三轮 v2/v3/v4 跨运行候选累积、FALLBACK 选中最高分 v3(60) 而非当前轮 v4(55)、只给最高分版本写 LOCK；人工等待跨运行保留 + 闲置 11 分钟自动切模型 + CNT1 归零；切换后再审 85 分 → ALLOWED + MODEL_PASS；节点跨两次执行等待态落盘、强制切换后 CNT1≥1 且离开等待态。

### 2.5 修复 Q9（MAJOR）：验收脚本 GBK 控制台崩溃

- 修复前实测：默认控制台下 `python -B tests/verify/check_contracts.py` 退出码 **1**（`UnicodeEncodeError`，`render()` 输出 ✔/✘/∅）；`PYTHONIOENCODING=utf-8` 时退出码 0。
- 修复：`_make_console_safe()`（重定向→UTF-8；TTY→原编码 + errors=replace），`main()` 首行调用。
- 验证：`tests/test_acceptance_script_console.py`（以 `PYTHONIOENCODING=gbk` 子进程运行，断言退出码 0、报告输出、无 UnicodeEncodeError）；并实测默认控制台退出码 0。

### 2.6 附带修复

- `bettercomfyui_comfy/api_settings.py`：两个 docstring 的非法转义序列（`\S`）改为 raw string，消除 pytest 的 2 条 DeprecationWarning。
- 运行时同步：`python scripts/sync_to_comfyui.py` → `.comfyui/custom_nodes/{bettercomfyui,bettercomfyui_comfy}` 逐文件哈希一致（安装验收第 1 项 OK）。
## 三、未完成项（如实保留，含原因与建议）

| # | 项 | 严重度 | 原因（为什么不改） | 建议 |
|---|---|---|---|---|
| Q5 | `W0_完整逻辑图全流程` 缺 SG3/SG5（无 AssetGen/AssetIngest/AssetReview/VideoReview 节点）；真正的全流程在 `00_【总流程】剧本→成片（GPU）` | MAJOR | 文件名被 `scripts/verify_install.py` 与多个验收测试引用，改名/删除会破坏既有验收链 | 由 Lead/需求方裁定：重命名 W0 为「旧版」并声明 00 总流程为唯一完整流程，或给 W0 补 SG3/SG5 |
| Q6 | GPU 素材生成分支开箱不可运行：`CheckpointLoaderSimple` 的 ckpt 为空且 `/object_info` 无任何 checkpoint；`download_models.py` 只下 H3 权重；安装验收不检测模型类别 | MAJOR | 补图像模型需下载 GB 级模型 + GPU 实测；本机无可用 GPU，禁止加载权重 | 需求方提供图像模型清单/下载脚本；安装验收增加「素材生成所需模型类别」检查 |
| Q7 | GPU 总流程视频审核需人工两步：节点 id=21 无输入连线、路径为空，首跑必然报错（`review_nodes.py:394-397`） | MAJOR | 需重排执行依赖（SaveVideo 无输出端口）并需真实 GPU 出片顺序实测 | 增加「取最新成片」节点/自动发现最近输出；或在首跑将审核设为静音并显式提示 |
| Q8 | 通知 H8（弹窗+声音/可选 QQ/飞书/钉钉）未实现：默认 popup/sound 为 `UnimplementedChannel`，无通知区 UI | MAJOR | 需新增前端通知中心 + 后端事件接口，改动面大且需真实浏览器验收 | 第二阶段或单独任务实现；短期至少把 `log` 加入默认渠道保证「完整记录」 |
| Q10 | SG8 `ERR1A_B/ERR1B_B` 爆炸半径（局部/中程/全局）无显式分诊 | MINOR | 属失败路径语义重构，改动面大、本机无法验证真实失败恢复 | 定义 `ErrorClass → 半径 → GEN1/REGEN/N1/A` 映射并接入事件与测试 |
| Q11 | 4 个配置开关配了不用（`prompt.normalize_names`、`asset_review.retry_interval`、`video_review.regenerate_granularity`、`global_.checkpoint_every_stage`） | MINOR | 接线会与硬原则冲突或改变行为语义，需需求方确认 | 删除误导性开关，或按需求明确语义后接执行点 |
| Q12 | 既有契约检查债务：C6/C9/C14（MAJOR 10 / MINOR 1 findings，BLOCKER 0，退出码 0） | MINOR | 多数属检查口径过宽或已有设计裁定（如 cancel 失败返回 False 是验收 14 要求），改口径属改验收标准 | 由 Lead 裁定收紧/豁免清单 |
| Q13 | 封面提取失败只返回 None、不写入元数据/报告（`pipeline/core.py:856-878`） | MINOR | 需同时改 metadata 契约与调用方展示，改动点多 | 在 metadata 增加 `cover_status/cover_error` 并展示 |
| Q14 | （推断风险）GPU 总流程素材审核节点无输入依赖边，执行顺序未被约束 | MINOR | 需给 AssetReview 增加依赖输入并 GPU 实测 | 增加「等待入库」输入边或显式顺序机制 |

## 四、改动文件清单

### 4.1 产品代码（开发态；已同步到运行态）

| 文件 | 改动 |
|---|---|
| `bettercomfyui/demo.py` | 非 mock 执行器失败闭合；provenance 固定占位；`full_review_state` 语义化；docstring 更新 |
| `bettercomfyui/cli.py` | `run --executor` 帮助文本明确仅 mock；新增 `_probe_compute_capability()`；doctor 真实生成判定并入计算设备 |
| `bettercomfyui/pipeline/core.py` | 新增 `H3_MIN_VRAM_MIB` + `evaluate_compute_devices()`（cli/插件路由共用） |
| `bettercomfyui/review/local_scorer.py` | 音画同步真实测量（`_stream_end_seconds`/`_av_sync_score`），删除常量 80 |
| `bettercomfyui/review/asset_review.py` | `review_version` 对 SCANNING 周期在模型模式下续跑模型轮（H4→H2） |
| `bettercomfyui_comfy/nodes.py` | Run 节点真实执行器显式报错；AssetReview 节点新增「发起人工审核（等待）」、审核器状态落盘/恢复、超时 tick、H1C 活动顺延 |
| `bettercomfyui_comfy/routes.py` | `POST /run` 拒绝真实执行器（400 + 指引）；`/doctor` 并入计算设备判定 |
| `bettercomfyui_comfy/api_settings.py` | 消除 2 个非法转义 DeprecationWarning |
| `bettercomfyui_comfy/web/js/bettercomfyui_panel.js` | 真实执行器选项置灰；doctor 展示计算设备判定与原因 |
| `tests/verify/check_contracts.py` | 非 UTF-8 控制台安全输出（重定向 UTF-8 / TTY errors=replace） |

### 4.2 测试

| 文件 | 类型 |
|---|---|
| `tests/test_demo_executor_honesty.py` | 新增（6 条：非 mock 拒绝×4、节点报错、mock 元数据诚实性） |
| `tests/test_local_scorer_audio_sync.py` | 新增（3 条：等长/截断/无音轨 + 流测量） |
| `tests/test_review_persistence_cross_run.py` | 新增（3 条：三轮最高分、人工超时、节点跨执行） |
| `tests/test_acceptance_script_console.py` | 新增（GBK 控制台回归） |
| `tests/test_cli_doctor.py` | 更新/新增（计算设备判定 5 条 + 打桩隔离） |
| `tests/test_comfy_routes.py` | 更新（/doctor 必须含 compute 与原因） |
| `tests/test_comfy_node_controls.py` | 更新（真实执行器改为断言显式报错、且不调用 run_demo） |

### 4.3 运行态同步

- `bettercomfyui/`、`bettercomfyui_comfy/` → `.comfyui/custom_nodes/`：`python scripts/sync_to_comfyui.py`，逐文件哈希一致。
- **未重启** 127.0.0.1:8188 的 ComfyUI 服务（遵守只读约定）：运行中进程仍加载旧模块，`/bettercomfyui/doctor` 需重启后才返回新的计算设备判定。

### 4.4 审计文档（本轮产出）

- `docs/AUDIT_AGENT_理解_R1.md`（251 行，第 1 步：需求理解）
- `docs/AUDIT_AGENT_问题_R1.md`（261 行，第 2/3 步：盘点 + 问题清单）
- `docs/AUDIT_AGENT_报告_R1.md`（本文件）

## 五、验证证据（第 5 步：项目既有验收 + 本轮回归）

### 5.1 任务要求的 5 条验收命令（修复后实测）

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `python -B -m pytest tests/verify -q` | **0** | 三态：**通过=276 / 失败=0 / 未验证=1**；未验证=验收24 `test_p8_free_models_verifies_vram_drop`（CPU 后端无显存可观测，skip 原因已写） |
| 2 | `& .comfyenv\Scripts\python.exe -B -m pytest tests/verify -q` | **0** | 三态：**通过=265 / 失败=0 / 未验证=3**；多 2 条为 comfyenv 未装 playwright（验收30 P10/P11 浏览器用例，skip 原因「未安装 playwright」） |
| 3 | `python -B -m pytest tests/ -q` | **0** | 全量（含 tests/verify）；附加精确统计运行：**1371 passed, 16 skipped, 0 failed in 166.38s**（skip 均为环境/未验证类） |
| 4 | `python -B tests/verify/check_contracts.py` | **0** | 检查项 PASS=15 / FAIL=3 / UNVERIFIED=0 / VACUOUS=0；结论「无 BLOCKER 级契约问题」；findings：BLOCKER=0, MAJOR=10, MINOR=1, INFO=8（既有债务，见 Q12） |
| 5 | `python -B scripts/verify_install.py` | **0** | 5/5 OK：插件逐文件哈希一致、两个巨人包、工作流在界面目录、23 节点在界面可见、巨人节点可见；结论「安装完整且可用」 |

### 5.2 本轮新增/更新回归

`pytest tests/test_demo_executor_honesty.py tests/test_local_scorer_audio_sync.py tests/test_acceptance_script_console.py tests/test_review_persistence_cross_run.py tests/test_comfy_node_controls.py tests/test_cli_doctor.py tests/test_comfy_routes.py` → **46 passed, 1 skipped, 0 failed**（24.03s）。

### 5.3 关键运行时复现证据（审计前后对照）

| 检查 | 修复前 | 修复后 |
|---|---|---|
| `BetterComfyUIRun(executor="真实 ComfyUI")` | `status=成功`、`used_mock=False`、元数据 `is_mock_output=false / executor=comfyui`（实际是 ffmpeg 占位） | 显式 `_NodeError`（说明未接入、给真实出片指引），不产出成片 |
| `python -m bettercomfyui.cli doctor` | `真实生成：环境看起来具备条件` | `真实生成：不可用 —— 只有 CPU 设备…` |
| `check_contracts.py` 默认控制台 | 退出码 1（UnicodeEncodeError） | 退出码 0，报告完整 |
| 音画同步（4s 视频 + 1s 音频） | 恒 80 分（通过阈值 70） | ≤30 分（等长 ≥90 分） |
| 素材审核三轮（v2/v3/v4 分数 40/60/55） | 跨运行只剩当前轮 → 兜底选 55 | 候选历史 3 条 → 兜底选最高分 60（v3） |
| 人工等待 11 分钟后 | 无自动切换（poll_timeouts 无产品调用） | 下次进入审核节点自动切模型（CNT1 清零）并继续执行模型轮 |

### 5.4 结构盘点证据

- 18 个工作流 JSON：节点类型全部在 `/object_info` 注册；links 无悬空/重复（本轮脚本遍历全部 18 个文件）。
- 23 个 BetterComfyUI 节点在 `/object_info` 可见、`scripts/verify_install.py` 5/5 OK。
- 未点任何含 H3 权重的队列（W0/W2 等）；本轮所有 ComfyUI 交互均为只读查询或本机可跑的占位/纯 core 工作流。

## 六、逻辑图覆盖矩阵（SG0–SG8 → 现实产品）

| 图段 | 判定 | 依据（节选） |
|---|---|---|
| SG0 项目管理与上下文 | ✅ 已实现 | `store/projects.py`、`store/paths.py`（越界校验）、`BetterComfyUIProject`、`/projects` 路由、CLI project；验收 01 变红可证 |
| SG1 剧本拆解与提示词 | ✅ 已实现（真实 API 调用未验证） | `prompt/gateway|inject|validate|match|normalize|script_build`、`BetterComfyUIEverything`、DeepSeek 巨人+桥接；验收 02/03/04/16/27/31/33 |
| SG2 素材生成与增量库 | ⚠️ 部分实现 | 库/版本/哈希/9上限/统一分辨率/命名/优先级/手动入库均已实现（验收 05/18/32）；**GPU 素材图生成分支缺图像 checkpoint（Q6）** |
| SG3 素材版本审核与 LOCK | ⚠️ 部分实现（本轮补强） | CNT1/人工超时/仲裁/唯一提交/LOCK/FALLBACK 全有（验收 05–09/32）；本轮修好跨运行状态与 H4→H2；**H8 通知 UI 仍缺（Q8）** |
| SG4 导演台与分段生成 | ✅ 已实现（真实出片未验证） | `director/plan|generate`、融合节点、三大导演台包已安装；LOCK 门禁/参考图接线/尾部帧参数/显存策略；真实 H3 出片未验证 |
| SG5 视频段审核与级联 | ✅ 已实现（真实语义审核未验证） | `director/review|rework|cascade`（R_seg/边界/CNT2/CNT3/级联终止/降级标记）、`BetterComfyUIVideoReview`（真实本地统计评分）；验收 10/11/32 |
| SG6 拼接/整片审核/输出 | ✅ 已实现 | `pipeline/core.py`（splice/encode/full_review/apply_full_policy/write_output/probe_media）；R_full 仅一次有守卫；验收 15/19/20/32/33 |
| SG7 全局配置注入 | ⚠️ 大部分实现 | 三层配置/校验/日志级别/导入导出/事件落盘/六个注入点已实现；**4 个开关未接（Q11）** |
| SG8 异常/中断/断点恢复 | ⚠️ 部分实现 | 断点续传 `resolve_resume_target`（PROJ7→RESUME）、技术重试/超时看门狗、素材→段反向索引有；**ERR1A_B/ERR1B_B 爆炸半径显式分诊缺（Q10）** |

**总体判定**：第一阶段「剧本 → 素材 → 分段生成 → 审核（含 LOCK/计数/仲裁/级联）→ 拼接 → 整片审核 → 输出」的**编排闭环与数据契约是真实、可运行（占位/本机路径）的**；真实 H3 生成、真实语义审核与「开箱即用」尚有明确缺口（Q5–Q8），不能对外宣称「完整逻辑图已全部真实落地」。

## 七、剩余风险与建议（给 Lead 的决策项）

1. **必须由 Lead/需求方裁定**：
   - Q5：`W0_完整逻辑图全流程` 改名/补节点，明确「唯一完整流程 = 00_【总流程】剧本→成片（GPU）」；
   - Q6：图像模型清单/下载脚本与安装验收口径；
   - Q11/Q12：4 个配置开关与契约检查口径（改口径=改验收标准）。
2. **上线前必须做的验证**（本机无法做）：
   - 在 ≥24GiB GPU 目标机：`doctor` 应报「真实生成可用」；跑 `S2/S3/W3/00_总流程（GPU）` 真实出片；验证 H3 参考图注入（P11）与尾部 22 帧衔接；验证显存阶段化调度；
   - 真实 DeepSeek/千问 API：一次调用产出 E1–E5（验证 SG1 全链）；
   - 重启 ComfyUI 后复测 `/bettercomfyui/doctor`（确认计算设备判定生效）。
3. **风险提示**：运行中 ComfyUI 未重启 → 面板/`/doctor` 仍是旧逻辑；本轮所有针对运行中服务的行为结论以**文件+直接 import 测试**为准，运行态复测需重启。
4. **验收 24 的 `free_models` VRAM 观测**在 CPU 后端不可验证（已如实 skip），换 GPU 机器后应重新验证并纳入三态结论。

## 八、复现命令（审计证据如何重放）

```powershell
cd '[PROJECT-ROOT]'
# 需求理解 / 问题清单 / 本报告
# docs/AUDIT_AGENT_理解_R1.md  docs/AUDIT_AGENT_问题_R1.md  docs/AUDIT_AGENT_报告_R1.md
# 1) 体检（真实生成判定）
python -B -m bettercomfyui.cli doctor
# 2) 验收（5 条）
python -B -m pytest tests/verify -q
& .comfyenv\Scripts\python.exe -B -m pytest tests/verify -q
python -B -m pytest tests/ -q
python -B tests/verify/check_contracts.py
python -B scripts/verify_install.py
# 3) 本轮回归
python -B -m pytest tests/test_demo_executor_honesty.py tests/test_local_scorer_audio_sync.py tests/test_acceptance_script_console.py tests/test_review_persistence_cross_run.py -q
# 4) 运行态同步（改动后）
python -B scripts/sync_to_comfyui.py
```

## 九、审计声明

- 本报告所有「已实现/已修复」结论均给出可执行证据；未实测的能力一律标为「未验证」。
- 本轮未修改 `文档/` 下需求文件与逻辑图；未新增/删除关键能力；未把未验证写成已验证；未用常量/占位分数冒充审核通过（发现并修复了 1 处历史常量分）。
- 本审计不接受实现方口头说明；如与实现方文档结论冲突，以本报告的文件行号、命令输出与测试结果为准。
