# 独立审计 · 第二轮报告（R2：第一阶段真实可用性与诚实性）

- 审计人：独立第三方审计工程师（未参与实现；不接受实现方口头解释）
- 需求依据：`文档/愿景部分_纯文本.txt` + `文档/BetterComfyUI_完整总流程图.dot`（均逐字通读）
- 上一轮：`docs/AUDIT_AGENT_报告_R1.md`（R1 的 5 项修复本轮逐项复核）
- 审计日期：2026-10-18（本机，ComfyUI 0.41.0 CPU 模式）
- 未修改 `文档/` 下需求文件与逻辑图；未把未验证写成已验证。

## 〇、本轮环境说明（重要）

- 本轮开始前 8188 加载的是 **R1 修复后**代码（Lead 已重启）；但 R2 的代码/数据改动需要再次重启才能真机生效。
- 为完成真机安全队列验证并保持环境与代码一致，本轮**主动重启了 8188 两次**（均使用与原实例一致的命令：
  `.comfyenv\Scripts\python.exe main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch`，
  工作目录 `.comfyui`）。当前 8188 正在运行**R2 最终代码**（detached 进程），
  `GET /bettercomfyui/doctor` 返回 `node_count=23`、`real_generation_available=false`。
- 全程未排队任何含 H3 权重加载的工作流；真机队列只用 `00 本机版`、`S4 视频审核`、
  素材取任务/入库、以及单节点 `BetterComfyUIRun`（失败闭合分支）。
- 未验证边界不变：真实 H3 出片、真实外部提示词 API、真实语义审核模型、24GB 目标机显存行为。

## 一、R1 五项修复复核（新证据，不只跑旧测试）

| R1 问题 | R1 修复位置 | 本轮新证据（代码路径 + 可执行） | 是否闭合 |
|---|---|---|---|
| **Q1 真实执行器假成功** | `demo.py` 失败闭合；节点/路由/面板拒绝；provenance 固定占位 | ① 真机排队 `BetterComfyUIRun(executor="真实 ComfyUI")` → ComfyUI `status=error`、`execution_error=True`，错误信息「…继续运行会把占位产出标成真实生成（假成功），因此被拒绝」；② 真机 `00 本机版` 成功且在审计首行标「⚠️ 占位（未调用真实模型）」、`API 调用次数：0 次`；③ `tests/test_demo_executor_honesty.py`（6 条）全绿；④ 全局 grep 无第二条输出成片路径 | ✅ 闭合 |
| **Q2 音画同步常量 80** | `local_scorer._stream_end_seconds/_av_sync_score` 真实测量 | ① 真机 S4：等长 A/V → `音画同步=100.0`；同路径替换为「8s 视频 + 2s 音频」→ `音画同步=0.0`（**且内容变化后被重新审核**）；② `tests/test_local_scorer_audio_sync.py`（3 条：≥90 / ≤30 / 无音轨=0）全绿；③ 全产品 review 路径 grep 无其它常量分（`image_scores` 的两维为真实统计，跳过维度如实列出） | ✅ 闭合（本地启发式边界如实标注，非语义审核） |
| **Q3 doctor/面板「真实生成可用」假阳性** | `evaluate_compute_devices` + CLI/插件路由并入计算设备判定 | ① 真机 `GET /bettercomfyui/doctor` → `real_generation_available=false`，`compute.reason="只有 CPU 设备…"`；② CLI `doctor` → 「真实生成：不可用 —— 只有 CPU 设备」；③ `tests/test_cli_doctor.py`（设备判定 4 条 + 权重齐全但设备不足→不可用）与 `tests/test_comfy_routes.py`（/doctor 必须含 compute 与原因）全绿 | ✅ 闭合 |
| **Q4 审核跨运行状态丢失**（候选最高分/人工超时/切换后模型轮） | 审核器状态落盘 `review/reviewer_state.json`；每节点 tick 超时；SCANNING 续跑模型轮；新增「发起人工审核（等待）」 | ① **真跨进程**验证（3 个独立 python 进程）：三轮候选累积 3 条，FALLBACK 选最高分版本且非最高分版本无 LOCK；② 跨进程人工等待：进程1 落盘 AWAITING_REVIEW，进程2（时间戳改为 11 分钟前）自动切模型模式并**真的执行模型轮**（CNT1=1）；③ 状态文件损坏：不崩、摘要明确「审核状态恢复失败」；④ 故意删状态模拟崩溃：重放不重复提交、LOCK 不变、`finalizations.json` 不变；⑤ 配套 `tests/test_review_persistence_cross_run.py` 3 条 + `tests/test_audit_r2_fixes.py` 4 条全绿 | ✅ 闭合（限制：超时推进依赖下一次进入审核节点，ComfyUI 无后台调度器；已在节点摘要写明） |
| **Q9 验收脚本 GBK 控制台崩溃** | `check_contracts._make_console_safe()` | 默认控制台（无 PYTHONIOENCODING）实测 `python -B tests/verify/check_contracts.py` **退出码 0**、报告完整；`tests/test_acceptance_script_console.py` 以 `PYTHONIOENCODING=gbk` 子进程回归全绿 | ✅ 闭合 |

**复核结论**：R1 的 5 项修复均**真实闭合**（不是只改测试/换地方造假）。但复核过程中发现 R1 修复仍有一个**未覆盖的产品路径**（见 R2-01/R2-02：ComfyUI 缓存会让这些节点/审核根本不执行），本轮已补修。

## 二、R2 新发现缺陷（对抗式审查）

### R2-01【BLOCKER｜已修复】ComfyUI 节点缓存导致「有副作用节点」重复排队根本不执行

- **逻辑图依据**：SG2 `GEN1–GEN4 → G（素材库只追加版本）`、SG3 `H/H1/H2` 审核推进与超时、SG5 `CNT2/CNT3` 记账。
- **对抗思路**：ComfyUI 按输入哈希缓存节点输出。若节点有副作用（读/写素材库、审核状态机、计数器）却没有 `IS_CHANGED`，重复排队会直接复用旧输出 → 界面成功、实际没做。
- **真机证据**（本轮实测，8188 CPU 模式）：
  - 素材取任务：首次返回 `R2缓存验证C_character_2_…`（进度 `0/2`）；给该素材入库后再排队**相同图**，仍返回同一素材（进度曾 0/2；真实推进应为下一个待生成项）——修复前缓存命中，不重跑；
  - 素材入库：同一 IMAGE 输入连续排队两次 → 第二次仍返回 `v1`，素材库 `versions=['v1']`（**没有 v2**，违反 G7「同一素材多次入库 = 多个版本」）；
  - 回灌/审核：同类副作用节点（FusionIngest / AssetReview / Run / ManualAsset / Project）同样会被缓存。
- **影响**：S3「取下一个待生成素材」循环不推进、入库不追加、审核状态机/超时不 tick、回灌不记账 —— 产品路径「看起来在工作，实际没执行」（标准假成功形态）。
- **严重度**：BLOCKER（第一阶段核心闭环依赖这些节点）。
- **修复**：
  - 为有副作用节点声明 `IS_CHANGED`，返回每次都变化的值绕过缓存：
    `BetterComfyUIProject`、`BetterComfyUIRun`、`BetterComfyUIAssetReview`、`BetterComfyUIManualAsset`（`nodes.py`）；
    `BetterComfyUIAssetGen`、`BetterComfyUIAssetIngest`（`review_nodes.py`）；
    `BetterComfyUIFusionIngest`、`BetterComfyUIFusionGroups`（`fusion_nodes.py`）；
    `BetterComfyUIOneDirector`（`unified_director.py`，其导演组依赖素材库 LOCK 状态）。
  - `BetterComfyUIVideoReview` 使用**内容感知**缓存键（见 R2-02）。
- **验证**：
  - `tests/test_audit_r2_fixes.py::test_r2_stateful_nodes_declare_is_changed`（清单 + `**kwargs` 签名 + always 型两次调用必不同）；
  - 真机复测（重启后）：同输入第二次入库 → 返回 `v2`、库内 `versions=['v1','v2']`（修复生效）；素材取任务在状态变化后进度由 `0/2→1/2`（节点确实重跑）。

### R2-02【BLOCKER｜已修复】视频审核缓存复用旧结论：同路径内容被替换后仍返回上一次「通过」

- **逻辑图依据**：SG5「段级重生成后新版本必须重新审核，旧版本审核结果失效」「CNT2 只计当前问题段候选」。
- **对抗思路**：段重生成通常写回同名文件（路径不变）→ ComfyUI 认识输入没变 → 复用上次审核输出。
- **真机证据**（修复前，8188）：先审核「8s 视频+8s 音频」→ `音画同步=100、passed=True`；随后把**同一路径**文件替换成「8s 视频+2s 音频」（SHA-256 与 mtime 均已变），再次排队同样输入 → 返回**完全相同**的旧结论（`音画同步=100、passed=True、final=86.82`）。即：新内容拿到旧内容的「通过」。
- **影响**：直接违反「旧版本审核结论失效」；用户/流水线会把未审核的新内容当已通过。
- **严重度**：BLOCKER。
- **修复**：`BetterComfyUIVideoReview.IS_CHANGED` 以「文件大小 + mtime_ns + SHA-256 + 项目 + 级联标记」为缓存键；文件缺失时返回变化值（强制执行并报错，不静默）。
- **验证**：真机复测同场景 → 内容 A `音画同步=100/passed=True`，替换内容 B 后重审 → `音画同步=0.0/passed=False/policy=unconfigured`（结果随内容变化）；`tests/test_audit_r2_fixes.py::test_r2_video_review_is_changed_tracks_file_content`（同内容稳定 / 改内容必变 / 缺文件不恒等）。

### R2-03【MAJOR｜已修复】10 个发布工作流硬编码开发机 32 位项目 id → 新机器「能打开但跑不通」/静默空库/幽灵项目风险

- **逻辑图依据**：SG0（项目作用域）、SG1 `A1 读取当前项目素材库`、SG3 `G_VALID`；愿景「开源用户拿到之后能直接跑起来，而不是面对一堆需要自己填的坑」。
- **证据**：`W0/W0L/W2/S1/S2/S3/S4/P1/00 本机版/00 GPU` 共 10 个工作流把开发机 id `a93ddac1a70046b0b6b8b916e9f8cdbb` 写进 widget；该 id 在新机器不存在：
  - `BetterComfyUIEverything._resolve_project` 旧实现对该 id 直接原样返回、不校验存在性；
  - `_open_asset_store` 旧实现捕获一切异常返回 None → 审计只显示「空库/未注入」，不说明原因（静默）；
  - 素材类节点 `_store_for_project` 直接 `ps.open(id)` → 报 ProjectNotFound（工作流跑不通）；
  - 真实 API 模式下还可能往不存在的项目目录写素材（幽灵项目，ProjectStore.list 看不到）。
- **严重度**：MAJOR（第一阶段两套「总流程」在新环境不可用）。
- **修复**：
  - `ProjectStore.resolve_reference()`：32 位 id 必须存在（不存在报错，**不隐式新建**）；其他字符串按**显示名**查找，同名复用、不存在则新建并返回 resolution=CREATED_NAME；
  - `nodes._store_for_project` 使用该解析并给出可操作错误；`everything_node._resolve_project` 同样解析并把「自动新建/按名解析/坏 id」写进 warnings/审计；素材库打开失败不再静默；
  - 10 个工作流与 3 个构建脚本中的开发机 id 全部替换为显示名「我的短剧」；两套总流程说明区新增「项目怎么填（不用手抄 project_id）」；
  - `00 本机版/00 GPU` 已用构建脚本重新生成（diff 仅说明区变化，节点/连线完全一致），并同步到 `bettercomfyui_comfy/workflows`、`.comfyui/user/default/workflows`、安装副本。
- **验证**：
  - `tests/test_audit_r2_fixes.py`：`resolve_reference` 创建/复用/坏 id 报错；**扫描所有发布工作流不得再出现开发机 id**；两套总流程项目 widget 使用显示名且说明区含「项目怎么填」；Everything 节点在临时项目根下「自动新建→按名复用→坏 id 不新建」的审计措辞；
  - **真机**：重启后排队 `00 本机版` → `status=success`，素材清单首行 `项目：我的短剧（id=6e32…）`，审计含「已自动新建项目」，5 个预览含 E1–E5/director_groups/`---`/占位标记/API 0 次。

### R2-04【MAJOR｜已修复】LOCK 指纹门禁形同虚设：`resolve_lock` 的 hash 校验是「记录与自身比较」，生成侧只查文件存在

- **逻辑图依据**：SG2 `G_META SHA-256`、`G7 新版本追加`；SG3 `G_VALID 有效LOCK`；SG4 参考图/参考音视频只允许 LOCK 版本进入导演台（R3）。
- **证据**：
  - `store/assets.py:1151-1159`（修复前）`resolve_lock` 的「content_hash 与版本元数据不一致」判断是 `parse_content_hash(entry["content_hash"]) != entry["content_hash"]`——同一字段自比较，恒相等（只验证格式）；
  - 生成入口 `asset_nodes._locked_file` 与 `fusion.locked_asset_ref_images/locked_ref_media.resolve_one` 只检查 `is_file()`；LOCK 后把文件替换成别的字节，节点仍按旧 LOCK 的指纹信息加载**不同内容**；
  - 审计 R2 攻击验证（修复前）：LOCK 后替换文件 → 消费端不会报错（无任何不一致提示）。
- **严重度**：MAJOR（版本/指纹/LOCK 语义静默失效，审计/追溯被破坏）。
- **修复**：
  - `store/assets.py` 新增 `verify_locked_file_content(locked, file_path)`：重算磁盘 SHA-256 与 LOCK 声明比对，不一致抛 `ContentHashMismatchError`（内容被替换属损坏/越权，不降级为 None）；
  - 在三个生成前置门禁调用：`asset_nodes._locked_file`、`fusion.locked_asset_ref_images`、`fusion.locked_ref_media.resolve_one`；
- **验证**：`tests/test_audit_r2_fixes.py::test_r2_locked_content_tamper_is_rejected`：未篡改正常加载；篡改后素材节点抛 AssetNodeError（含「内容校验失败/指纹」），融合参考图入口在读取图像前抛 FusionError（指纹不一致）。

### R2-05【MAJOR｜已修复】S4 视频审核在默认配置下对「整片未通过」直接执行失败，而不是给出审核结论

- **逻辑图依据**：SG6 `R_full → FULL_DECISION → FULL_POLICY`（失败必须二选一：阻止/降级）；SG5 视频审核应产出结论。
- **证据**（修复前真机）：对「8s 视频+2s 音频」排队 S4（默认「按项目配置」、`video_review.full_review_policy=None`）→ ComfyUI `execution_error: 整片审核失败：ConfigError: video_review.full_review_policy 未显式配置…`。即：任何低于阈值的视频都会让审核节点报错，用户拿不到「未通过」结论。
- **严重度**：MAJOR（第一阶段「视频审核可用」不成立；不是假通过，但可用性缺陷）。
- **修复**：`BetterComfyUIVideoReview` 在「整片未通过 + 策略未配置」时不再抛错；返回 `policy="unconfigured"`、`policy_message`，结论写明「整片未通过（失败策略未配置：未自动放行/降级；请配置 video_review.full_review_policy 或在上方选择阻止/降级）」；显式选择阻止/降级时仍按策略返回。交付流水线（pipeline）保持「未配置策略必须报错」不变。
- **验证**：真机复测 desync 视频 → `status=success`，`整片结论：整片未通过（失败策略未配置…）`，`policy=unconfigured`；`tests/test_audit_r2_fixes.py::test_r2_video_review_unconfigured_policy_returns_conclusion`。

### R2-06【MINOR｜已修复】视频审核 CNT2/CNT3 达到上限后仍无限累加

- **逻辑图依据**：SG5 `CNT2_CHECK 已达到3次重生成上限？` → 兜底后**不再新增候选**（dot `N`/`CNT2 + 相邻边界`）。
- **证据**（修复前）：同一段版本重复审核（或每次内容变化后的再审核）`CNT2` 会 1→2→3→4→5… 无上限累加，并把「重复复核同一版本」当成新候选；与「达到上限后从候选取最高分、不再新增候选」不符。
- **严重度**：MINOR（不影响 PASS/质量判定，但计数语义失真；且可能掩盖「该换新版本」的提示）。
- **修复**：`review_nodes.py` 中达到 limit 后不再 `increment`，记录 `{"capped": True}` 并直接走历史候选兜底；未达上限才 +1。输出摘要明确「已达上限（本次不新增候选）」。
- **验证**：`tests/test_audit_r2_fixes.py::test_r2_video_review_cnt2_caps_at_limit`（5 次强制不合格 → 计数 `[1,2,3,3,3]`、后两次 capped=True、落盘计数=3）。

### R2-07【MINOR｜已修复】两个产品 .py 文件存在控制字符污染（\x08/\x0c）

- **证据**：`bettercomfyui_comfy/unified_director.py` 与 `bettercomfyui_comfy/workflows/validate_fusion_workflow.py` 中分别存在 `\x08`（替换了 'b'）与 `\x0c`（替换了换行/字符），把注释/文档字符串污染成非法片段；`unified_director.py` 源码因 `\x0c` 被某些工具按行边界处理后直接 SyntaxError（本轮打补丁时暴露；安装副本仅因 form-feed 在注释内才侥幸可导入）。
- **严重度**：MINOR（当前可运行，但任何按 `splitlines()` 处理的工具/补丁都会破坏文件；属隐藏地雷）。
- **修复**：字节级修复两处污染并校验全产品目录无 `\x08/\x0c` 残留；`unified_director.py` 补充可读注释；本轮新增的 `IS_CHANGED` 一并写入。
- **验证**：全产品 `.py` `py_compile` 通过；控制字符扫描 0 残留。

### R2-08【MAJOR｜未修复】级联返工/局部重生成的**产品工作流未接线**（纠正 R1 的乐观措辞）

- **逻辑图依据**：SG5 全段：`R3 否 → U 旧结论失效 → REGEN 仅重生成问题段 → CNT2`；`N1_EDGE → Y2 下一受影响段 → N1 → CNT3`；`N1_FALLBACK/CASCADE_DEGRADED`。
- **证据**：`ReworkController`/`CascadeController` 在产品代码中**没有任何调用者**（只有定义、`director/__init__` 导出与单测/集成测试）；产品可见节点 `BetterComfyUIVideoReview` 只输出「需重生成段」文本，`FusionIngest` 只做「回灌 + 段级审核 + CNT 记账」并给出「应只重做本段/检查后续段」的建议；**没有任何工作流包含自动重生成循环/级联传播执行**（ComfyUI 无原生循环，需编排层驱动）。R1 报告将 SG5 判为「已实现」是基于库层验收（10/11/32），对产品路径过度乐观。
- **影响**：第一阶段「不合格段自动局部重做 + 影响后续段级联复核」在 UI 上仍需人工按建议重跑；不是假成功（节点如实输出需返工段），但**未达到逻辑图的自动闭环**。
- **未修复原因**：需要新增「按需返工驱动」编排（读需返工段 → 只重跑对应段 → 触发 N1_EDGE/CNT3 → 回到审核），涉及执行顺序/队列控制与 GPU 实测；属功能开发而非最小修复，本轮不改。

### R2-09【MAJOR｜未修复】两套总流程工作流仍存在的第一阶段可用性缺口（承 R1 Q5/Q6/Q7）

- **`W0_完整逻辑图全流程`（旧版）**：节点类型仍缺 SG3/SG5（无 AssetGen/Ingest/AssetReview/VideoReview）；真正完整的是 `00_【总流程】剧本→成片（GPU）`（R1 已记录，本轮复核未变）。
- **`00 GPU` 的素材图生成分支**：`CheckpointLoaderSimple` 的 ckpt 为空且本机 `/object_info` 无任何 checkpoint；安装脚本不下载图像模型 → 素材图生成开箱不可运行（说明区已要求「必须选一个你已安装的图像模型」，是已知人工前置而非隐藏）。
- **`00 GPU` 的视频审核**：node 无输入连线、视频路径为空 → **首跑必然在该节点报错**；说明区「视频审核怎么用」已写明需出片后复制路径再点一次（如实但非一键）。
- **影响**：GPU 全链在开箱状态下不能「一段剧本 → 全部内容（含审核）」一键完成；不是假成功（均有明确报错/说明），但与愿景「直接跑起来」有差距。
- **未修复原因**：需图像模型依赖方案 + 工作流执行顺序重排 + GPU 实测；本轮保持诚实说明，未做未验证的编排改动。

### R2-10【MINOR｜未修复，且非第一阶段阻塞】通知 H8（弹窗/声音/通知区）仍缺失

- 默认渠道 popup/sound 为 `UnimplementedChannel`，仅日志记录；人工审核需要用户时无主动提醒。不影响第一阶段主流程与假成功判定，按 R2 范围「不扩大」保留记录。

## 三、两套用户可见总流程工作流审查结论

### 3.1 `00_【总流程】剧本→全部内容（本机可跑）`

| 审查点 | 结论 | 证据 |
|---|---|---|
| 诚实性 | ✅ 不假装出片：首行「⚠️ 占位（未调用真实模型）」、`API 调用次数：0 次`；审计列出「素材库登记跳过（占位模式不写素材库）」；未 LOCK 素材明示「导演组 refs 为空」 | 真机排队输出（本轮） |
| 第一阶段定位 | ✅ 人工输入一段剧本 → 输出分镜脚本/提示词 JSON（E1–E5）/素材清单/审计报告/导演组结构；**本机无 GPU 时仅文本产出**，说明区明确「不能出片」 | 真机 5 个预览 + 验收 33 P3 |
| 普通用户可读性 | ✅ 说明区（含「项目怎么填」「普通用户三步」「本机提醒」）+ 5 个中文分组 + 5 个结果预览；节点仅 8 个 | 工作流 JSON + 真机 |
| 可运行性 | ✅ 真机 `status=success`，无权重节点；项目显示名自动新建（本轮修复后） | 真机排队 |
| 风险 | 占位模式的文本是确定性拆分（非模型输出），已在模式标记与说明区如实呈现 | 输出审计 |

### 3.2 `00_【总流程】剧本→成片（GPU）`

| 审查点 | 结论 | 证据 |
|---|---|---|
| 阶段完整性 | ✅ 含 SG1–SG7 可见节点（Everything/AssetGen/AssetIngest/AssetReview/MiniMaxH3Director/CreateVideo/SaveVideo/VideoReview），视频审核**可见** | 工作流 JSON（26 节点/29 连线）+ 验收 33 P2/P4 |
| 诚实性 | ✅ 说明区明确 GPU≥24GB、图像模型必须自选、视频审核需手动填路径、显存不足需分两段跑；未假装本机可跑 | 说明区原文 |
| 普通用户可读性 | ✅ 说明区（新增「项目怎么填」）+ 5 个阶段分组 + 结果预览 | 工作流 JSON |
| 可运行性 | ⚠️ **未验证/不可开箱一键**：本机无 GPU 且无图像 checkpoint；素材图分支缺模型即明确失败；首跑视频审核路径为空会明确报错（不是静默）；R2-09 已记录 | 静态校验 + 节点实现 + 说明区 |
| 参数正确性 | ✅ 全部节点/连线/枚举通过真实 `/object_info` schema 校验；项目引用本轮改为显示名（不再依赖开发机项目） | `build_master_flow_workflows.validate` 通过 |

**总体评价**：两套总流程在「诚实性」上合格（占位/未验证/失败都有显式标记与说明，本轮又补了缓存与项目引用两个隐蔽缺陷）；「本机版」真实可跑且输出完整；「GPU 版」结构完整、视频审核可见，但受图像模型/GPU/人工两步限制，**未达到开箱一键出片+审核**。第一阶段结论：**编排与审核语义真实可用，真实生成链路未验证且开箱依赖补齐模型**。

## 四、本轮修复清单与改动文件

### 4.1 产品代码（开发态；已 `sync_to_comfyui.py` 同步，逐文件哈希一致）

| 文件 | 改动 |
|---|---|
| `bettercomfyui/store/projects.py` | 新增 `ProjectStore.resolve_reference()`：id 必须存在 / 显示名查找或新建，返回 resolution；工作面：工作流可移植性 |
| `bettercomfyui/store/assets.py` | 新增 `verify_locked_file_content()`：LOCK 指纹与磁盘 SHA-256 真实比对（`ContentHashMismatchError`） |
| `bettercomfyui_comfy/nodes.py` | `_store_for_project` 支持显示名解析 + 可操作错误；Project/Run/AssetReview/ManualAsset 增加 `IS_CHANGED`（防缓存） |
| `bettercomfyui_comfy/everything_node.py` | `_resolve_project` 校验 id/显示名并写审计；`_open_asset_store` 返回错误不再静默；素材库失败写 warnings |
| `bettercomfyui_comfy/review_nodes.py` | AssetGen/AssetIngest `IS_CHANGED`；VideoReview 内容感知 `IS_CHANGED`；整片失败策略未配置→返回结论（不再崩）；CNT2/3 上限后不再新增候选；审计增加「整片结论/失败策略」行 |
| `bettercomfyui_comfy/fusion_nodes.py` | FusionIngest/FusionGroups `IS_CHANGED`；回灌入口加 LOCK 指纹校验 |
| `bettercomfyui_comfy/fusion.py` | 参考图/参考音视频 LOCK 指纹校验（3 处生成前置门禁之一） |
| `bettercomfyui_comfy/unified_director.py` | OneDirector `IS_CHANGED`；修复历史遗留控制字符污染（\x08/\x0c） |
| `bettercomfyui_comfy/asset_nodes.py` | `_locked_file` 增加 LOCK 内容指纹校验 |
| `bettercomfyui_comfy/workflows/validate_fusion_workflow.py` | 修复历史遗留控制字符污染（\x08） |
| `scripts/build_one_node_workflows.py`、`scripts/build_simple_workflows.py`、`scripts/build_w0_v2.py` | 构建常量从开发机 project id 改为显示名「我的短剧」 |
| `scripts/build_master_flow_workflows.py` | 两套总流程说明区新增「项目怎么填（不用手抄 project_id）」；GPU 说明补充「图像模型必须自选/首跑视频审核会明确报错」 |
| 10 个工作流 JSON（`bettercomfyui_comfy/workflows` + `.comfyui/user/default/workflows`） | 开发机 project id → 显示名「我的短剧」；两套 00 总流程由构建脚本重新生成并同步 |
| `bettercomfyui_comfy/workflows/00_【总流程】*` | 同上（08 节点/26 节点、连线数不变；diff 仅说明区） |

### 4.2 测试

| 文件 | 内容 |
|---|---|
| `tests/test_audit_r2_fixes.py`（新增，13 条） | 跨进程 R1 复核 4 条；项目引用/工作流无开发机 id/两套总流程可读性 4 条；LOCK 指纹门禁 1 条；S4 未配置策略 1 条；CNT2 封顶 1 条；有副作用节点 `IS_CHANGED` 清单 + 视频内容感知缓存键 2 条 |
| `tests/verify/test_acceptance_31_one_node.py` | 适配 `_open_asset_store` 新签名（返回三元组）的测试替身（断言不变） |

### 4.3 运行态

- `bettercomfyui/`、`bettercomfyui_comfy/` 已同步到 `.comfyui/custom_nodes/`（哈希一致，`verify_install` 通过）。
- 8188 已重启两次并运行最终代码（理由与命令见〇节）；当前服务保持运行。
- 清理：本轮审计创建的项目目录与临时备份已删除（`projects/` 下不再有 R2 审计项目；证据保留在报告与测试中）。

## 五、验证证据（全部在最终代码 + 最终重启后运行）

### 5.1 任务要求的 5 条既有验收

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `python -B -m pytest tests/verify -q` | **0** | 三态：**通过=276 / 失败=0 / 未验证=1**（同 R1：free_models VRAM 下降，CPU 后端无显存可观测） |
| 2 | `& .comfyenv\Scripts\python.exe -B -m pytest tests/verify -q` | **0** | 三态：**通过=265 / 失败=0 / 未验证=3**（多 2 条：comfyenv 未安装 playwright） |
| 3 | `python -B -m pytest tests/ -q` | **0** | 附加精确统计：**1384 passed, 16 skipped, 0 failed in 160.06s**（比 R1 的 1371 多 13 条 = 本轮新增 R2 回归） |
| 4 | `python -B tests/verify/check_contracts.py` | **0** | PASS=15 / FAIL=3，BLOCKER=0（既有债务，同 R1），结论「无 BLOCKER 级契约问题」 |
| 5 | `python -B scripts/verify_install.py` | **0** | 5/5 OK：「安装完整且可用」（哈希一致、巨人包、工作流、23 节点、巨人节点） |

### 5.2 本轮新增回归（13 条，全绿）

`python -B -m pytest tests/test_audit_r2_fixes.py -o addopts= -q` → **13 passed**（另 R1 回归集合
`tests/test_demo_executor_honesty.py / test_local_scorer_audio_sync.py / test_review_persistence_cross_run.py /
test_acceptance_script_console.py / test_cli_doctor.py / test_comfy_routes.py / test_comfy_node_controls.py`
在本轮全量 pytest 中也全部通过）。

### 5.3 真机（8188，安全队列）验证摘要

| 验证项 | 结果 |
|---|---|
| `00 本机版` 剧本→全部内容 | `status=success`，5 个预览：E1–E5/导演组/`---` 分镜/素材清单/审计；首行「⚠️ 占位（未调用真实模型）」、API 0 次、项目显示名自动新建 |
| `S4 视频审核`（重启后） | 等长 A/V → `音画同步=100`、通过；「8s+2s」→ `音画同步=0`、整片未通过且 `policy=unconfigured`（不再抛错） |
| 视频审核缓存（R2-02） | 同路径内容替换后重新审核（不再复用旧通过） |
| 素材入库缓存（R2-01） | 同输入第二次入库返回 `v2`、库内 `versions=['v1','v2']` |
| 素材取任务（R2-01） | 状态变化后进度 `0/2→1/2`（节点确实重跑） |
| `BetterComfyUIRun` 真实执行器（R1-Q1） | ComfyUI `status=error`、`execution_error=True`，错误含「占位媒体/假成功」与真实出片指引；未产出任何成片 |
| `/bettercomfyui/doctor` | `real_generation_available=false` + `compute.reason=只有 CPU 设备` |
| 权重安全 | 全程未排队含 H3 权重加载的图；`--cpu` 服务；无权重进入内存 |

### 5.4 关键修复前后对照（真机）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| Run 节点选「真实 ComfyUI」 | 返回「成功」+ 占位媒体（假成功） | ComfyUI execution_error（明确拒绝） |
| 视频审核同路径换内容 | 复用旧「通过」（音画同步 100） | 重新审核（音画同步 0 / 未通过） |
| 视频审核低分片（默认策略） | execution_error（ConfigError） | 返回结论「整片未通过（策略未配置）」 |
| 素材入库同输入第二次 | 返回 v1、无新版本 | 返回 v2、`versions=['v1','v2']` |
| 素材取任务状态变化后 | 缓存旧结果、不推进 | 重跑，进度/待生成项更新 |
| 新机器跑总流程 | 硬编码开发机 project id：空库/报错 | 显示名自动新建/复用，审计说明 |
| LOCK 后替换文件内容 | 生成侧照常加载（无提示） | 生成前置门禁报「指纹不一致」并拒绝 |

## 六、剩余风险与未完成项

1. **R2-08 级联返工自动闭环未接线**（MAJOR）：产品工作流仍靠人工按「需重生成段」建议重跑；`ReworkController`/`CascadeController` 仅库层+测试覆盖。需新增返工驱动编排（读需返工 → 只重跑该段 → N1_EDGE/CNT3 → 回审核），并 GPU 实测。
2. **R2-09 GPU 全链开箱不可一键**（MAJOR）：图像模型 checkpoint 缺省为空；视频审核需出片后手工填路径；W0 旧版缺 SG3/SG5。说明区已如实告知，但距「直接跑起来」仍有差距。
3. **R2-10 通知 H8 缺失**（MINOR/非第一阶段阻塞）：人工审核无弹窗/声音提醒，仅有日志。
4. **真实语义审核模型未接入**：素材「一致性/合规」与视频语义维度仍由本地启发式近似/跳过（如实标注）；真实评分模型由需求方提供后注入。
5. **未验证**：真实 H3 出片、24GB 目标机显存/性能、真实 DeepSeek/千问 API 链路、目标机上的两套 GPU 工作流真实执行顺序。
6. **ComfyUI 缓存语义提醒**：`IS_CHANGED` 已让有副作用节点每次重跑；但纯计算节点仍会正常缓存（预期行为）。若后续新增有副作用节点，必须同时声明 `IS_CHANGED`（本轮新增了回归测试清单守卫）。
7. **环境说明**：8188 当前由本轮 detached 进程运行（`--cpu`），若机器人/Lead 需要特定启动方式请按原命令重启；`GET /bettercomfyui/doctor` 可用于确认加载的是最终代码。

## 七、复现命令

```powershell
cd '[PROJECT-ROOT]'
# R2 回归
python -B -m pytest tests/test_audit_r2_fixes.py -o addopts= -q
# 5 条既有验收
python -B -m pytest tests/verify -q
& .comfyenv\Scripts\python.exe -B -m pytest tests/verify -q
python -B -m pytest tests/ -q
python -B tests/verify/check_contracts.py
python -B scripts/verify_install.py
# 真机（需 8188 运行且加载最终代码）：
python -B -m bettercomfyui.cli doctor
#   GET /bettercomfyui/doctor → real_generation_available 必须 false（CPU 机器）
#   工作流内排队：00_【总流程】剧本→全部内容（本机可跑）、S4_视频审核（本机可跑）
```

## 八、审计声明

- 所有「已修复」结论均有可执行证据（真机队列历史/测试/命令输出）；未验证项均标注「未验证 + 原因」。
- 本轮发现并修复的 R2-01/R2-02 是**对 R1 修复的补充**：R1 修复了逻辑本身，但 ComfyUI 缓存会让节点根本不执行——已用真机复现并修复。
- 未修改 `文档/` 需求文件与逻辑图；未删除关键能力；未用常量/占位分数冒充审核通过。
