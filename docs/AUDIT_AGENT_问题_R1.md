# 独立审计 · 现实盘点与问题清单（R1，第 2/3 步产出）

- 审计人：独立第三方审计工程师（未参与实现；不采用实现方文档结论作为证据）
- 需求依据：`文档/愿景部分_纯文本.txt`（65 行）+ `文档/BetterComfyUI_完整总流程图.dot`（516 行），均已逐字通读（见 `docs/AUDIT_AGENT_理解_R1.md`）
- 本文件 = 第 2 步「只读盘点代码/工作流」+ 第 3 步「逐项问题清单」的合并产出。
- 证据口径：`文件:行号` 或可直接复现的命令输出；「推断」均显式标注。

## 0. 环境与未验证边界（先说清楚，避免把未验证当已验证）

现场事实（命令：`python -m bettercomfyui.cli doctor`、`GET http://127.0.0.1:8188/system_stats`、`GET /object_info`）：

| 事实 | 值 | 证据 |
|---|---|---|
| ComfyUI 版本 | 0.41.0，以 `--cpu` 启动 | `/system_stats.system.argv = ["main.py","--cpu",...]`，`pytorch_version=2.14.1+cpu` |
| 计算设备 | **只有 CPU**（devices: `[{"type":"cpu",...}]`） | `/system_stats` 输出 |
| 宿主机 GPU | NVIDIA GTX 1050 Ti / 4096 MiB | `nvidia-smi`（doctor 输出） |
| 模型权重 | 6 个 H3 权重（diffusion_models×2 / text_encoders×1 / vae×2 / loras×1） | `.comfyui/models/**` + doctor |
| 已装第三方节点 | MiniMaxH3_Director / Smart_Director / ChainDirector / DNNodes / Storyboard-LLM / DeepSeek-API-connector + bettercomfyui 包 | `.comfyui/custom_nodes/` |
| 节点注册 | `/object_info` 共 1035 个节点，其中 BetterComfyUI* 23 个 | `/object_info` |
| ffmpeg | 可用（imageio-ffmpeg 7.1） | doctor |
| H3 真实生成 | **不可运行**：服务跑在 CPU 上，且 H3 权重（含 34GB 级文本编码器）需 ≥24GiB 显存 | `/system_stats` + 项目自身 `VRAM_TIERS` 定义（`bettercomfyui/cli.py:226-231`） |

**未验证（本轮明确不写成通过）**：
1. 真实 H3 视频生成（无可用 GPU/CPU 服务，权重加载会耗尽内存 —— 按要求未点任何 W0/W2 类含权重的队列）；
2. 真实外部提示词 API 调用（`config/user.yaml` 的 `prompt.api_key` 为空，未发起外网调用）；
3. 真实语义审核模型（本机只有本地启发式评分；方案要求「评分模型由需求方写」，实现提供注入点）；
4. 导演台/融合节点在 24GB 目标机上的真实出片与显存行为；
5. 验收 24 的 `free_models` VRAM 下降观测（`tests/verify/_last_run.json`：`Skipped: 未验证：/free 调用成功，但未观察到显存下降…`，CPU 后端无显存可观测）。

## 1. 现实产品盘点：入口 / 节点 / 工作流

### 1.1 可执行入口（已确认存在）

| 入口 | 位置 | 说明 |
|---|---|---|
| CLI | `python -m bettercomfyui.cli` | `doctor` / `project list/create/show/delete` / `asset list/suspects` / `run --executor` / `config export/import`（`bettercomfyui/cli.py:567-601`） |
| ComfyUI 节点 | `bettercomfyui_comfy/` 23 个节点 | `/object_info` 全部可见（`scripts/verify_install.py:39-63` 期望清单） |
| 网页面板 | `bettercomfyui_comfy/web/js/bettercomfyui_panel.js` + `routes.py` | `/bettercomfyui/{doctor,projects,settings,workflow/minimal,project/open,run,phase2/status}` |
| 工作流 | `bettercomfyui_comfy/workflows/*.json` 共 18 个 | 结构校验：节点类型全部在 `/object_info` 注册、无悬空 link、无重复 link id（审计脚本实测 18/18 通过） |

工作流清单：`00_【总流程】剧本→全部内容（本机可跑）`、`00_【总流程】剧本→成片（GPU）`、`P1`、`S1`~`S4`、`W0_完整逻辑图全流程`、`W0L`、`W1`~`W4`、`fusion_A/B/C`、`minimal_workflow`、`one_director_demo`。

### 1.2 与逻辑图逐段对应（实现载体 → 状态）

| 图段 | 实现载体（文件/节点/工作流） | 状态 |
|---|---|---|
| SG0 项目管理 | `store/projects.py`、`store/paths.py`、`BetterComfyUIProject`、`/bettercomfyui/projects`、CLI project | **已实现**（项目隔离/切换/删除确认；验收 01） |
| SG1 剧本拆解与提示词 | `prompt/gateway.py`（外部/本地千问/自配）、`prompt/inject.py`（清单注入+5套模板）、`prompt/validate.py`（JSON/分隔符/规范形状）、`prompt/match.py`+`normalize.py`（确定性匹配）、`prompt/script_build.py`、`BetterComfyUIEverything`、DeepSeek 巨人+`prompt_bridge.py` | **已实现**（本机占位模式真实执行可产出 E1–E5 结构；真实 API 调用未验证） |
| SG2 素材生成与增量库 | `store/assets.py`（版本目录/只追加/SHA-256/9上限/统一分辨率/命名/优先级/手动入库）、`prompt/match.py`（E6–E9）、`BetterComfyUIAssetGen`/`AssetIngest`、S3 工作流 | **部分实现**：库与节点已实现并被测试覆盖；但 GPU 素材图生成分支 `CheckpointLoaderSimple` 的 ckpt 为空且本机 `/object_info` 无任何 checkpoint → 素材图生成开箱不可运行（见 Q6） |
| SG3 素材版本审核与 LOCK | `review/asset_review.py`（状态机/CNT1/人工超时/仲裁/唯一提交）、`review/arbiter.py`、`review/commit.py`、`review/counters.py`、`review/state.py`、`review/local_scorer.py`、`review/notify.py`、`BetterComfyUIAssetReview` | **部分实现→本轮修复补强**：审核语义完整（验收 05–09/32），但**跨节点执行的状态持久化、人工等待超时链、切换后模型轮**此前在产品路径不可达（Q4，已修）；**通知 UI（H8）仍缺失**（Q8） |
| SG4 导演台与分段生成 | `director/plan.py`（段计划/LOCK 门禁/引用索引）、`director/generate.py`（提交/轮询/技术重试/超时看门狗）、`director/cascade.py`、融合节点（`fusion.py`/`fusion_nodes.py`/`unified_director.py`）、`MiniMaxH3Director`/`ChainDirector`/`SmartDirector` 工作流 | **已实现（结构/参数/参考图接线）**；真实出片**未验证**（无 GPU） |
| SG5 视频段审核与级联 | `director/review.py`（R_seg/边界/阈值/作用域）、`director/rework.py`（CNT2/只重做该段/旧结论失效）、`director/cascade.py`（CNT3 独立/级联终止/降级标记）、`BetterComfyUIVideoReview`（R_seg+R_full 本地启发式） | **已实现**（验收 10/11/32；本机启发式真实计算） |
| SG6 拼接/整片审核/输出 | `pipeline/core.py`（splice/encode/`full_review`/`apply_full_policy`/`write_output`）、`pipeline/phases.py`（阶段化显存调度） | **已实现**（验收 15/19/20/32/33；整片审核只执行一次有守卫） |
| SG7 全局配置注入 | `config.py`（三层优先级/校验）、`config/default.yaml`、`logging_setup.py`、`store/events.py`、CLI config 导入导出、`BetterComfyUISettings`、六个注入点 | **大部分已实现**；**4 个开关配了不用**（Q11） |
| SG8 异常/中断/断点恢复 | `store/checkpoints.py:193 resolve_resume_target`（PROJ7→RESUME 接线于 `store/projects.py:253`）、`director/generate.py` 技术重试/超时、`director/errors.py`、`models/enums.py ErrorClass`、`director/cascade.py:85 segments_depending_on_asset` | **部分实现**：断点/回滚/局部-中程路径有；**ERR1A_B/ERR1B_B「爆炸半径」分诊无显式实现**（Q10） |

### 1.3 逻辑图的跨段硬约束落点（抽查结论）

- 版本/LOCK 绑定版本：`store/assets.py`（`add_version`/`set_lock`/`resolve_lock`，LOCK=asset_id+version），验收 05/06 变红可证 → 已实现。
- CNT1/2/3 隔离与「版本递增不清零」：`review/counters.py`（reset 仅允许 CNT1）、`director/rework.py`/`cascade.py` → 已实现（验收 08）。
- 唯一提交点/迟到结果/仲裁：`review/commit.py` + `review/arbiter.py` + `asset_review.py`（H_ARB/H_COMMIT/迟到归档）→ 已实现（验收 09）。
- 确定性注入/名称归一化/疑似重复：`prompt/inject.py` + `store/inject.py` + `prompt/match.py` + `normalize.py` → 已实现（验收 03/04）。
- 连贯性主来源＝素材库参考图：融合节点 `fusion.py:locked_asset_ref_images` + `unified_director.plan_ref_slots` + `Everything` 节点把已 LOCK 素材挂进每段 refs → **结构已接线**；本机无法真实验证出片是否真的以参考图为主（未验证）。
- 全部本地：`review/local_scorer.py` 零网络调用（grep 无 urllib/requests/socket）；通知 webhook 需显式注入传输层（默认不联网）→ 审核本地成立。
- 全量日志：`store/events.py` + `projects/<id>/logs/events.jsonl`（跑 demo 后实测有事件行）→ 已实现（验收 19）。
## 2. 问题清单（逐项）

> 状态说明：**已修复** = 本轮改动 + 可执行验证；**未修复** = 如实保留并写原因。
> 修复前的行号标注为「修复前」，修复后行号标注为「现」。

### Q1【BLOCKER｜已修复】`run_demo(executor="comfyui")` 把占位媒体标成真实生成（假成功）

- **逻辑图依据**：SG4 `P 分段视频生成` → SG6 `SPLICE/ENC/R_full` 链；愿景「不能把模拟结果说成真实生成已通过」（任务书第 9 节）与「全部本地真实生成」。
- **修复前证据**（代码）：
  - `bettercomfyui/demo.py:179-242`：`executor` 只用于 `report.used_mock = executor == "mock"`；`_run_flow()` 不接收 executor；
  - `bettercomfyui/demo.py:474-475`（修复前）：无论执行器是什么，一律 `_make_placeholder_video(...)`（ffmpeg 纯色占位）；
  - `bettercomfyui/demo.py:630-635`（修复前）：`generation_provenance={"is_mock": bool(report.used_mock), "executor": "placeholder-ffmpeg" if report.used_mock else "comfyui"}`；
  - `bettercomfyui_comfy/nodes.py:341-347`（修复前）+ `cli.py:590-596`（修复前）：节点/CLI 都对外提供「真实 ComfyUI」选项。
- **修复前证据**（运行时复现，本轮实际执行）：
  - 调用 `BetterComfyUIRun().run(..., executor="真实 ComfyUI", max_segments=2)` →
    节点 `status="成功"`、报告 `used_mock=False`、`real_generation=False`；
    成片元数据：`is_mock_output=false`、`generation_provenance.executor="comfyui"`；
    而成片实际是 ffmpeg 合成的纯色占位 mp4（代码路径决定）。
- **影响**：用户/验收拿到「is_mock=False 的成片」，会误当真实模型产出；这是审计标准里最严重的「看起来有但实际不工作/假成功」。
- **修复**：
  - `bettercomfyui/demo.py:187-259`（现）：`run_demo` 只有占位媒体一条路径 → `executor != "mock"` 时**失败闭合**（`ok=False` + 可操作错误 + 不建项目/不出成片）；`used_mock` 恒为 True；
  - `bettercomfyui/demo.py:665-676`（现）：`generation_provenance` 固定 `is_mock=True / executor=placeholder-ffmpeg`（不再随参数漂移）；
  - `bettercomfyui_comfy/nodes.py:329-347`（现）：节点选「真实 ComfyUI」→ 显式 `_NodeError`（说明未接入 + 指向 S2/S3/W3/00 总流程）；
  - `bettercomfyui_comfy/routes.py:355-369`（现）：`POST /bettercomfyui/run executor=comfyui` 返回 400 + 明确错误；
  - 面板：真实执行器选项置灰并标注「未接入本闭环」；CLI `--executor` 帮助文本改为「mock 唯一支持」。
- **验证**：`tests/test_demo_executor_honesty.py`（4 条参数化 + 节点报错 + mock 元数据诚实性，全绿）；`tests/test_comfy_node_controls.py::test_executor_choice_selects_real_path`（改为断言显式报错、且不调用 run_demo）。

### Q2【MAJOR｜已修复】整片审核 R1b「音画同步」是常量 80 分

- **逻辑图依据**：SG6 `R1b 整片评分R1b：叙事流畅/音画同步/节奏合理/无明显跳变`；愿景 §五「最终整片审核用于检查…音画同步」。
- **修复前证据**：`bettercomfyui/review/local_scorer.py:337-359`（修复前）：
  ```
  if info["has_audio"]:
      sync = 100.0 if duration > 0 else 0.0
      sync = 80.0     # ← 无条件覆盖为常量
  ```
  只要文件有音轨，音画同步恒 80 分（阈值默认 70 → 永远通过），音频被截断/错位也照样通过。
- **影响**：用占位常量冒充审核通过，直接违反「禁止常量/占位分数」纪律；整片审核对音画同步完全失效。
- **修复**：`bettercomfyui/review/local_scorer.py:315-336`（现，`_stream_end_seconds`）+ `:337-430`（现，`_av_sync_score`）：用 `ffmpeg -map 0:v:0/0:a:0 -c copy -f null -` 真实测量视频流/音频流最后时间戳，视频按 1/fps 修正，时长差先扣一帧容差，再按长流 10% 归零线性映射；无音轨=0 分、测不到=0 分（不编分）。文档中如实标注「只检测流时长对齐，不做帧级唇形同步」。
- **验证**：`tests/test_local_scorer_audio_sync.py`：等长 A/V ≥90 分；4s 视频+1s 音频 ≤30 分；两者差 ≥50 分（证明非常量）；无音轨=0。

### Q3【MAJOR｜已修复】doctor / 面板「真实生成可用」假阳性（CPU 服务 + 4GB 卡却报可用）

- **逻辑图依据**：SG7 全局配置/体检；愿景 §六「显存」；「有文件 ≠ 能出片」纪律。
- **修复前证据**：
  - `bettercomfyui/cli.py:369-374`（修复前）：`real_generation_ok = comfyui found and kinds available and version ok` —— **不含计算设备**；
  - `bettercomfyui_comfy/routes.py:206`（修复前）：`out["real_generation_available"] = bool(kinds["available"] and meets_min_comfyui(ver))` —— 同样不看 GPU；
  - 运行时实测（本轮）：`GET /bettercomfyui/doctor` 返回 `real_generation_available=True`、`model_kinds_present={'扩散模型':2,'文本编码器':1,'VAE':2}`，而 `/system_stats.devices=[{"type":"cpu"}]`、宿主机 GPU 仅 4096 MiB —— 实际必然跑不了 H3；面板 `refreshDoctor()` 会显示「真实生成可用：是」。
- **影响**：用户在界面看到「可用」后点真实出片必然失败/耗尽内存；是「诊断级假成功」。
- **修复**：
  - `bettercomfyui/pipeline/core.py:120-190`（现）：新增 `H3_MIN_VRAM_MIB=24000` + `evaluate_compute_devices()`（只认非 CPU 设备且显存 ≥24GiB；解析不出=不可用，不猜）；
  - `bettercomfyui/cli.py:234-276`（现）：`_probe_compute_capability()` 通过 `/system_stats` 探测；doctor 结论并入计算设备（实测输出：「真实生成：不可用 —— 只有 CPU 设备…」）；
  - `bettercomfyui_comfy/routes.py:102-155`（现）：在服务进程内用 torch 探测设备并并入 `/doctor`；面板显示计算设备判定与原因。
- **验证**：`tests/test_cli_doctor.py`（新增 5 条：设备判定函数 CPU/4GB/24GB/空；权重版本 OK 但设备不足→不可用；JSON 含 compute 字段）；`tests/test_comfy_routes.py`（/doctor 含 compute 且不可用必须给原因）。

### Q4【MAJOR｜已修复】素材审核跨 ComfyUI 节点执行时状态丢失：三轮取最高分、人工等待/超时、切换后模型轮均不可达

- **逻辑图依据**：SG3 `H1B/H1C/H3/H4`（人工等待、暂停计时、超时切模型）、`N 从三轮候选中取最高分`、`CNT1` 语义；SG3 出口 `G_LOCK`。
- **修复前证据**：
  - `bettercomfyui/comfy/nodes.py:542-549`（修复前）：每次节点执行新建 `AssetReviewer`，**未 restore/snapshot**；
  - `grep -r "reviewer.snapshot\\|reviewer.restore"`：产品代码零调用（只有 `scripts/audit_ui_review_persistence.py` 的库层脚本）；
  - `bettercomfyui/review/asset_review.py:268-272`（修复前）：`review_version` 对已存在且非终态周期一律 `reused` 返回；人工超时切换后周期停在 SCANNING，**再次调用不会执行模型轮** → H4→H2 断链；
  - `bettercomfyui/review/asset_review.py:842 poll_timeouts()` 修复前**无任何产品调用者** → 人工超时永不自动触发（节点只有「强制切换」手动入口）；
  - 后果：CNT1 计数虽然持久化（`nodes.py:_counters`），但候选分数列表在内存 → 跨运行 FALLBACK 只能看到当前轮候选（取当前分，而非三轮最高分）。
- **影响**：违反 P3/P7/P8 三条硬原则；「三轮取最高分」「人工超时自动切模型」在真实产品路径上不可达。
- **修复**：
  - `bettercomfyui_comfy/nodes.py:485-514`（现）：新增审核器状态落盘/恢复（`<项目>/review/reviewer_state.json`，原子写；坏状态按新状态继续但必须说明）；
  - `bettercomfyui_comfy/nodes.py:596-640`（现）：每次进入审核节点先 `restore` 再 `poll_timeouts()` 推进超时；新增 action「发起人工审核（等待）」；人工通过/拒绝前调用 `record_human_activity()` 顺延计时（H1C）；
  - `bettercomfyui/review/asset_review.py:270-281`（现）：`review_version` 对 SCANNING 周期在模型模式下**真正执行一轮模型审核**并返回（H4→H2 续跑）。
- **验证**：`tests/test_review_persistence_cross_run.py`（3 条）：
  P1 三轮 v2/v3/v4 跨运行候选累积，FALLBACK 选中最高分 v3(60) 而非当前轮 v4(55)，且只给最高分版本写 LOCK；
  P2 人工等待跨运行保留 + 闲置 11 分钟自动切模型 + CNT1 归零；
  P3 切换后再审真正执行模型轮（85 分 → ALLOWED + MODEL_PASS）；
  P4 ComfyUI 审核节点跨两次执行：等待态落盘、强制切换后 CNT1≥1 且离开等待态。
- **保留限制**：ComfyUI 没有后台调度器，超时推进是「下次进入审核节点/操作」时触发（已写入节点摘要说明）。真正后台守护需额外调度进程，本轮未做（避免引入常驻进程改变部署形态）。

### Q5【MAJOR｜未修复】`W0_完整逻辑图全流程` 名不副实：缺 SG3（素材审核）与 SG5（视频审核）

- **逻辑图依据**：dot 全图必须覆盖 SG0–SG8；用户明确要「一套完整工作流实现逻辑图」。
- **现实证据**（命令实测，本轮）：
  - `W0_完整逻辑图全流程.json` 节点类型 = `{UNETLoader, LoraLoaderModelOnly, CLIPLoader, VAELoader, MiniMaxH3Director, CreateVideo, SaveVideo, BetterComfyUISettings, BetterComfyUIEverything, PreviewAny}` —— **没有** `BetterComfyUIAssetGen/AssetIngest/AssetReview/VideoReview`；
  - 对照 `00_【总流程】剧本→成片（GPU）.json` 含全部阶段（AssetGen/Ingest/AssetReview/VideoReview/…）；
  - 验收 30 的 P9 覆盖清单本身只要求 W0 有 SG1/SG2/SG4/SG6/SG7（`tests/verify/test_acceptance_30_api_settings_and_master_workflow.py:...P9`），即现有验收并不要求 W0 覆盖 SG3/SG5。
- **影响**：用户打开名为「完整逻辑图全流程」的工作流，看不到素材审核与视频审核 → 误以为逻辑图已完整覆盖；与 `00_总流程` 角色重复且旧者更弱。
- **未修复原因**：文件名被 `scripts/verify_install.py:72-87`（EXPECTED_WORKFLOWS）与多个验收测试引用；改名/删除会破坏既有验收与安装清单。建议由需求方裁定：① 把 W0 重命名/标注为「旧版：提示词→导演台段」，并把 00_总流程作为唯一「完整流程」；② 或给 W0 补 SG3/SG5 节点（工作流体积变大，仍需 GPU 验证）。本轮不擅自改验收引用。

### Q6【MAJOR｜未修复】GPU 素材生成分支开箱不可运行：`CheckpointLoaderSimple` 为空 + 安装不提供/不检测图像模型

- **逻辑图依据**：SG2 `GEN1–GEN4 生成角色/场景/物品/分镜参考图`（素材库唯一核心参考源，P11 的前置）；愿景「开源用户拿到之后能直接跑起来」。
- **现实证据**（命令实测，本轮）：
  - `00_【总流程】剧本→成片（GPU）.json` node id=5 `CheckpointLoaderSimple.widgets_values=['']`；`S3_素材生成入库审核（GPU）.json` node id=2 同样 `['']`；
  - `GET /object_info`：`CheckpointLoaderSimple.ckpt_name` 选项 = `[]`（本机 models/checkpoints 为空）；
  - `scripts/download_models.py:47-64` 只下载 6 个 H3 权重，**不下载任何图像 checkpoint**；
  - `scripts/verify_install.py` 只验证「文件/节点/工作流存在」，**不验证模型类别可用性**，因此该分支不可运行不会被安装验收发现；
  - 工作流说明区（`00_...GPU` 的说明节点）已写明「使用前准备：图像模型节点选一个你已安装的图像模型」——即人工前置条件，属已知限制而非隐藏假成功。
- **影响**：SG2 素材图生成（P11 参考图来源）在开箱状态无执行路径；素材库只能靠手动添加/占位，导演台首跑无 LOCK 参考图 → 连贯性主来源无法满足。
- **未修复原因**：补齐图像模型需要下载 GB 级模型并需 GPU 实测（超出本轮安全边界：本机无可用 GPU，禁止加载权重）；改安装验收为「缺模型即失败」会把环境性限制伪装成代码失败。建议需求方提供图像模型清单/下载脚本，或在 S3 中改用可用的出图节点并在安装验证里增加「必需模型类别」检查（本报告已记录风险）。

### Q7【MAJOR｜未修复】GPU 总流程里的视频审核需要人工两步（首跑空路径必然报错）

- **逻辑图依据**：SG5 `Q_OUT → R_seg → … → READY_TO_SPLICE`（全自动审核链，无人工填参步骤）。
- **现实证据**（本轮）：
  - `00_【总流程】剧本→成片（GPU）.json` node id=21 `BetterComfyUIVideoReview`：`inputs=[]`（没有任何连线）、`widgets_values` 中视频路径为 `''`；
  - `bettercomfyui_comfy/review_nodes.py:394-397`：路径为空/文件不存在 → 直接 `_NodeError("视频文件不存在…")`；
  - 该工作流说明区原文写明「出片后，从 SaveVideo 的输出目录复制 mp4 绝对路径，填到视频审核→视频文件路径，再点一次队列即可」——即首次点队列会在审核节点报错，需第二次运行。
- **影响**：「输入一段剧本→全部内容（含审核结论）一键完成」不成立；首跑报错会被普通用户当失败。
- **未修复原因**：要自动衔接需重排执行依赖（SaveVideo 无输出端口，或加一个「取最新成片」节点并保证执行顺序），且真实出片顺序需 GPU 实测；本轮不改工作流编排（避免未验证改动）。

### Q8【MAJOR｜未修复】逻辑图 SG3 H8「通知：弹窗＋声音（可选QQ/飞书/钉钉）」在产品里未实现

- **逻辑图依据**：SG3 `H8 通知：弹窗＋声音；可选QQ／飞书／钉钉`、`H4 非阻断通知`；愿景「通知放入报错栏或通知区，不霸占主屏，完整记录日志」。
- **现实证据**（本轮 grep/读码）：
  - `config/default.yaml:43-45` 默认 `notify_channels: [popup, sound]`；
  - `bettercomfyui/review/notify.py:169-178`：`popup/sound` 未注册时一律映射为 `UnimplementedChannel`（`send()` 抛 `NotificationError`）→ 默认配置下所有通知投递都是 **WARN 失败**；
  - `bettercomfyui_comfy/web/js/bettercomfyui_panel.js` / `routes.py`：无任何通知区/弹窗/声音/轮询通知的实现（grep `notification|toast` 无命中）；
  - 已有的是「失败不阻断 + 事件日志 + WARN」，满足「完整记录日志」，但**不满足**弹窗/声音/通知区。
- **影响**：人工审核需要用户时没有任何主动提示；「通知」能力整体缺失。
- **未修复原因**：需要前端通知中心（轮询/推送 + 声音 + 非阻断 UI）与后端事件接口，属新增 UI 能力，改动面大且需真实浏览器验收；本轮不做未验证的大改动。
### Q9【MAJOR｜已修复】验收命令 `check_contracts.py` 在默认 Windows 控制台崩溃（退出码 1，且不是契约违规）

- **逻辑图依据**：SG7 Z3/Z4（配置/日志/验收可复现性）。
- **修复前证据**（本轮命令实测）：
  - `Remove-Item Env:PYTHONIOENCODING; python -B tests/verify/check_contracts.py > $null 2> $null` → **退出码 1**；
  - `PYTHONIOENCODING=utf-8` 同命令 → **退出码 0**；
  - 崩溃原因：`tests/verify/check_contracts.py:1057`（修复前）`render()` 输出 `✔/✘/∅`，GBK 控制台编码不了 → `UnicodeEncodeError`，在 `print(render(report))` 处炸掉（traceback 见本轮日志）。
- **影响**：任务要求的验收命令在默认终端下**不可复现通过**；真实契约结论（无 BLOCKER）被崩溃掩盖；CI/用户环境不一致。
- **修复**：`tests/verify/check_contracts.py:33-55`（现）：`_make_console_safe()` —— 重定向/管道时改用 UTF-8（保证证据文件内容正确），TTY 时保持原编码 + `errors="replace"`（不乱码不崩溃）；`main()` 首行调用（`:1116` 现）。
- **验证**：`tests/test_acceptance_script_console.py`：以 `PYTHONIOENCODING=gbk` 子进程运行 `check_contracts.py`，断言退出码 0、报告标题出现、无 `UnicodeEncodeError`；并实测默认控制台命令退出码 0（输出正文正确）。

### Q10【MINOR｜未修复】SG8 `ERR1A_B / ERR1B_B`「爆炸半径（局部/中程/全局）」无显式分诊实现

- **逻辑图依据**：dot L207-215 与边 L488-495：`ERR1A_B`（局部→GEN1 / 中程角色级→GEN1 / 全局剧本级→A）、`ERR1B_B`（局部→REGEN / 中程→N1 / 全局→A）。
- **现实证据**：全产品代码搜索「局部/中程/全局/blast/ERR1A/ERR1B」无分诊函数；现有的是隐式路径：
  - 技术失败分类 `director/generate.py:65-90 classify_error/classify_failure` → `ErrorClass`；
  - 局部：技术重试 `director/generate.py:93-145 TechRetryCounter`、单段重做 `director/rework.py:93 handle_quality_failure`；
  - 中程：`director/cascade.py:85 segments_depending_on_asset`（素材→段反向索引）+ 级联传播；
  - 全局：异常向上抛给编排层/CLI（无「剧本级回退到 A」的显式动作）。
- **影响**：出错时无法按图给出确定的爆炸半径与恢复目标；行为靠隐式约定，难以审计/测试。
- **未修复原因**：属于失败路径语义重构（需要给每个错误定义半径并驱动 GEN1/REGEN/N1/A），改动面大且本机无法验证真实失败恢复；本轮只做可验证的最小修复。

### Q11【MINOR｜未修复】4 个配置开关「配了不用」（SG7 配置注入不完整）

- **逻辑图依据**：SG7 Z1–Z9「全局配置注入」；愿景「用户可调整阈值、候选上限、通知、超时与降级策略」。
- **现实证据**：
  - `python -B scripts/scan_config_wiring.py` 输出 4 个未接线字段：`prompt.normalize_names`、`asset_review.retry_interval`、`video_review.regenerate_granularity`、`global_.checkpoint_every_stage`；
  - `tests/test_config_wiring_ratchet.py:66-69` 自我登记这 4 条为「行为已硬编码/开关未接」；
  - 对应 `config/default.yaml:8/46/75/98` 仍对外暴露这些开关。
- **影响**：用户改配置无效果（与「配置注入」原则冲突）；不是主流程阻塞。
- **未修复原因**：接线会改变现有行为语义（如 `regenerate_granularity=full` 与「只重做问题段」硬原则冲突、`normalize_names=false` 与 E6 归一化硬约束冲突）；正确做法是删掉误导性开关或由需求方确认语义，本轮不擅自决定。

### Q12【MINOR｜未修复】既有契约检查债务：C6/C9/C14 共 10 MAJOR + 1 MINOR（非本轮新增）

- **依据**：`python -B tests/verify/check_contracts.py` 现状：`检查项状态：PASS=15 FAIL=3 UNVERIFIED=0 VACUOUS=0`，结论「无 BLOCKER 级契约问题」，退出码 0；`tests/verify/_contracts_report.json` totals=`{BLOCKER:0, MAJOR:10, MINOR:1, INFO:8}`。
- **典型项**（人工复核认为多数属过宽匹配或已有设计裁定，不逐项改动）：
  - C6 `cli.py:548` / `logging_setup.py:163` 用 `write_text` 写配置/导出文件（R2 的本意是「不得原地覆盖**素材文件**」，配置导出不在其列 → 匹配过宽）；
  - C9 `comfyui_http.py:476 cancel()` 捕获 `ExecutorError → False`（验收 14 P4 明确要求 cancel 失败返回 False，属设计）；
  - C9 `store/atomic.py:165`、`store/lockfile.py` 等清理/探测类返回保守值（已按裁定降为 INFO，但扫描仍计入）；
  - C14 `tests/test_comfyui_live_integration.py:102` 固定 `time.sleep(0.5)`（属轮询等待，非长固定 sleep）。
- **影响**：验收输出长期带 3 个 FAIL 项，容易被误读为「验收不通过」。
- **未修复原因**：改契约检查口径属于改验收标准，应由 Lead/需求方裁定；本轮只如实记录。

### Q13【MINOR｜未修复】封面提取失败被静默忽略（只在返回值里是 None）

- **逻辑图依据**：SG6 `OUT4 封面：默认第一帧，可指定`。
- **现实证据**：`bettercomfyui/pipeline/core.py:856-878`（现）：`_run(cmd)` 抛错时 `cover_path=None`，注释写「要如实标记」，但返回字典只有 `cover_path: None`；`write_output` 依赖的 metadata/报告没有封面缺失字段，`demo._step("deliver", checks=…)` 也不含 cover 状态 → 用户不会被告知封面失败。
- **影响**：成片缺封面但流程显示成功（小的静默降级）。
- **未修复原因**：需同时改 metadata 契约与调用方展示；风险低但改动点多，留作后续。

### Q14【MINOR｜推断风险】GPU 总流程中「素材审核」节点没有输入依赖边，执行顺序未被约束

- **逻辑图依据**：SG2→SG3 必须「入库（新版本/待审）之后才审核」。
- **现实证据**：`00_【总流程】剧本→成片（GPU）.json` 中 `BetterComfyUIAssetGen` → `BetterComfyUIAssetIngest` 有连线，但 `BetterComfyUIAssetReview`（id=12）`inputs=[]`，与 Ingest 无依赖边；两者都是输出节点。
- **推断**：ComfyUI 执行顺序可能按节点 id/`order` 决定（本图 id 11 < id 12，通常可先入库），但**没有依赖边保证**；若执行器先跑审核，`_pick_pending()` 找不到待审版本 → 返回 NONE、本轮无 LOCK。
- **未修复原因**：需给 AssetReview 增加「素材ID/等待入库」输入边或显式顺序机制，并需真实 GPU 工作流验证；本轮不影响本机可跑工作流（占位路径不经这些节点）。

## 3. 本轮修复汇总（全部带可执行验证）

| # | 文件 | 修复 | 验证 |
|---|---|---|---|
| Q1 | `bettercomfyui/demo.py`、`bettercomfyui/cli.py`、`bettercomfyui_comfy/nodes.py`、`bettercomfyui_comfy/routes.py`、面板 JS | 非 mock 执行器失败闭合；元数据 provenance 常量诚实；节点/路由/UI 显式拒绝 | `tests/test_demo_executor_honesty.py`、`tests/test_comfy_node_controls.py` |
| Q2 | `bettercomfyui/review/local_scorer.py` | 音画同步真实测量（流时长对齐），去掉常量 80 | `tests/test_local_scorer_audio_sync.py` |
| Q3 | `bettercomfyui/pipeline/core.py`、`bettercomfyui/cli.py`、`bettercomfyui_comfy/routes.py`、面板 JS | 真实生成可用性并入计算设备（≥24GiB 非 CPU 设备）判定 | `tests/test_cli_doctor.py`、`tests/test_comfy_routes.py` |
| Q4 | `bettercomfyui_comfy/nodes.py`、`bettercomfyui/review/asset_review.py` | 审核器状态落盘/恢复；超时 tick；H1C 活动顺延；SCANNING 周期续跑模型轮；新增「发起人工审核（等待）」 | `tests/test_review_persistence_cross_run.py` |
| Q9 | `tests/verify/check_contracts.py` | 非 UTF-8 控制台安全输出 | `tests/test_acceptance_script_console.py` |
| 附带 | `bettercomfyui_comfy/api_settings.py` | 消除 2 个非法转义 `DeprecationWarning`（pytest 不再刷 warning） | 全量 pytest（warning 消失） |
| 附带 | `bettercomfyui/demo.py` | `full_review_state` 由 `str(True)` 改为 `PASSED/FAILED` 语义字符串 | `tests/test_demo_executor_honesty.py::test_mock_demo_metadata_declares_placeholder_provenance` |

## 4. 未完成项与剩余风险（不给结论就完了）

1. **真实 H3 生成链**：本机无可用 GPU（CPU 服务 + 4GB 卡），真实出片/显存策略/导演台参考图效果**未验证**；本轮所有「已实现」结论仅指编排/结构/本地评分/占位媒体闭环。
2. **GPU 素材生成分支**（Q6）与**视频审核两步**（Q7）在开箱状态下不可一键跑通；需需求方补图像模型与工作流重排。
3. **通知 UI（H8）**缺失（Q8）；人工审核目前只能靠用户主动进节点操作。
4. **SG8 爆炸半径分诊**（Q10）未显式落地；失败恢复语义仍靠隐式约定。
5. **4 个配置开关**（Q11）仍无效果；有误导性但非阻塞。
6. **运行中的 ComfyUI 仍加载改动前的插件模块**：本轮只同步了文件（`scripts/sync_to_comfyui.py`，逐文件哈希一致），未重启 8188 服务（按只读约定）。因此 `GET /bettercomfyui/doctor` 在**重启前**仍会返回旧逻辑（本机实测重启前为 `real_generation_available=true`）；重启后才会返回新的计算设备判定。**任何以运行中服务为准的结论都应重启后复测。**
7. 真实外部提示词 API、真实语义审核模型未验证；内置演示/占位路径的「常量打分」只在 mock 元数据（`is_mock=true`）下出现，并已在报告 `unverified` 中列出。
