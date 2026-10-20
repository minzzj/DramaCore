# ROUND19：图一终审缺口修复报告

> 起点：`docs/图一完成度总表.md` 列出 **8 个缺口 + 3 处小偏差**。
> 本轮修完 **7 项**，另**新发现并当场修掉 1 项**，剩 5 项如实登记。
> 验收三态：**126 通过 / 0 失败 / 0 未验证**（系统 python 与 `.comfyenv` 双解释器一致，EXIT=0）。
> 产品代码已同步到 ComfyUI 副件，逐文件哈希一致；16 个节点在真实 ComfyUI 里全部注册。

---

## 一、修好的缺口

### #1 剧本形状接缝（**最严重：假成功**）

**修前实测**：规范剧本 → `run_demo` → `ok=True`，但 素材新建 **0**、复用 **0**、LOCK **0**，段 2。
素材阶段整段**静默跳过**，调用方看不到任何异常。

**根因**：两半各自都能过，接缝对不上 ——

| 来源 | `assets` 在哪 | 键名 |
|---|---|---|
| `validate_script` 规范输出 | **顶层** | `asset_type` |
| `demo.py` 消费 / `DEMO_SCRIPT` | **章节级** | `type` |

**修法**（按 AGENTS.md「在集成边界归一约定」）：
* 新增 `canonicalize_script()`：章节级 → 顶层合并、`type` → `asset_type`、吸收后删除章节级副本（避免第二份真相）、按 id 去重且顶层优先；
* `demo.py` 先归一、再按**规范形状**消费；
* 内置 `DEMO_SCRIPT` 改成契约形状 —— **它自己必须先通过自己的校验**。

**修后实测**：规范形状 / 章节级+`type` / 章节级+`asset_type` **三种形状全部产出素材**（新建 2、LOCK 2），向后兼容未破。

### #2 界面路径结构校验闸门

**修前**：`validate_script` 在产品代码里**零调用**；`run_demo` 只做 `json.loads`。

**修后**：`_run_flow` 在进入素材生成**之前**过校验闸门；不合法直接抛错。
实测无效引用 → `ok=False`、素材 0、段 0、错误信息引用任务书 §B。

> ⚠️ 踩坑记录：第一版写成 `report.ok = False; return`，结果 `ok` 仍是 `True` ——
> `run_demo` 在外层 `try` 之后**无条件**置 `report.ok = True`，提前 return 只退出内层函数。
> 改成**抛异常**走已有错误处理才对。

### #3 `references` 素材 → 段反向索引

**修前**：`record_reference()` / `references_of()` 早就实现，但**产品代码零调用** ——
跑完 demo 所有素材 `references` 全空。导致图七 `GF`「引用次数」恒空、
图四 `F2B`、SG8「中程失败」**算不出来**（只能整条重跑，违背"只重跑不合格那段"）。

**修后**：
* `plan_segments` 在建完段后调 `record_segment_asset_references()` 落索引；
* 新增 `AssetStore.referencing_segments()` / `reference_count()`（按**不同段**计数，同段重复引用不虚高）；
* 新增 `segments_depending_on_asset()`（`director/cascade.py`）—— 默认只返回**未通过**的段，`include_passed` 可取全部，幽灵引用（段已删）忽略。

**实测**：跑完 demo → 6 条引用记录（林秀 2 段 / 老陈 1 段 / 客栈大堂 3 段）；
把一段标 FAILED 后，查询**只返回那一段**。

> ⚠️ 踩坑记录：`_segment_passed` 第一版写成「status **或** review_status 任一通过就算通过」。
> 但段被标 `FAILED` 后 `review_status` 仍是上次的 `ALLOWED`，于是"要重做的段"被误判成"已通过"，
> 查询返回空集。**`status` 才是生命周期真相，`review_status` 只是上一版结论。**

### #5 `per_class_limit` 每类上限（图一 G4）

**修前**：配置 9，实测连续造出 **12 个**同类素材**无一被拦**。

**修后**：`create_asset(per_class_limit=...)`，在 `mutator`（持索引锁）里判断，
避免并发下两个创建者都读到 n-1 一起越限。`demo.py` 从 `cfg.assets.per_class_limit` 传入。
**实测**：上限 9，第 10 个被拒，错误信息含具体上限值与来源说明。

### #6 `assets.resolution` 统一分辨率（图一 G3）

**修前**：产品代码读取点 **0**，全工程无 resize/scale。

**修后**：`add_version(unify_resolution=...)` —— 按**最长边等比**缩放（不强拉成正方形，避免把角色拉变形）；
尺寸已符合**不重编码**（避免无谓画质损失）；非图片（音频）原样入库；缩放前后尺寸写进
`extra`（`original_size` / `unified_size`）可追溯；临时文件用完即删。
**实测**：3000×1200 → 1024×410（等比）。

### #7 日志与配置开关（图一 Z3/Z4/Z5）

**修前**：`log_level` 只校验取值、从没设置日志级别；`log_all_manual_and_auto_switch` 从没被读；
`config_import_export` 无实现。

**修后**：新增 `bettercomfyui/logging_setup.py` ——
* `apply_logging(cfg)`：真正设置根 logger 级别（幂等，不会反复叠 handler）；
* `log_manual_or_switch()`：开关为 False 时**真的不记**（开关不是摆设）；
* `export_config_json()` / `import_config_json()`：导出**必然脱敏**（`api_key` → `***`），
  导入时 `***` **丢弃**（否则会写成字面量密钥，看起来"配了"实际是坏的）；
* CLI 新增 `config export|import` 子命令；
* 接线点：CLI、`run_demo`、`AssetReviewer.review_version`（记录人工审核/自动切换）。

### #10 `encode.audio_join` 音频拼接（图一 ENC1）

**修前**：`encode()` 里**没有任何音频分支**，配置项从未被读。

**修后**：三态语义并加校验 ——

| 值 | 行为 | 实测 |
|---|---|---|
| `auto`（默认） | 保留各段音轨拼接 | 有音轨 ✅ |
| `mute` | `-an`，成片静音 | 无音轨 ✅ |
| `manual` | 用 `audio_source` 整条替换 | 有音轨 ✅ |

两种矛盾输入都**报错而非静默忽略**：`manual` 不给音轨、`auto` 却传了音轨。

### #9（本轮新发现）段落盘：状态只在内存

**发现过程**：写验收 17 时 `SegmentStore.list_segments()` 返回空 —— 项目目录里**根本没有 `segments/`**。
`SegmentStore` 只被插件回灌节点使用，**主链路 `demo.py` 完全不落盘段状态**，
检查点只存段 ID。

这违反任务书第 79 行：「每段可以独立生成、审核和返工；**状态不能只保存在 UI 内存中**」。
进程一退，"哪些段已通过、哪些要重做"就没了。

**修后**：`demo.py` 计划出来即落盘、每次状态变化（审核结论/兜底）再更新；
落盘失败不阻断出片但写 `degrade_flags`（不静默成功）。

---

## 二、验收套件从 92 扩到 126

| 新增 | 条数 | 覆盖 |
|---|---|---|
| 验收 16 接缝形状 | 8 | 归一化、三种形状消费、内置剧本过校验、校验闸门、每类上限 |
| 验收 17 反向索引 | 7 | references 非空、指向真实段、按段计数、只返未通过、幽灵引用忽略 |
| 验收 18 素材库约束 | 8 | 每类上限（含"每类非全局"、"可选"）、等比缩放、已符合不重编码、非图片放行 |
| 验收 19 日志/配置/音频 | 11 | 日志级别生效、开关真能关、导出不泄密、`***` 被丢弃、音频三态与两种矛盾输入 |

`tests/verify` 元测试同步更新：`ACCEPTANCE_ITEMS` 由 15 条扩到 19 条。
> 顺手修掉一处**双份真相**：`summarize_run.py` 复制了一份 `ACCEPTANCE_ITEMS`，
> 加第 16 条时只改了一处，元测试读的是 `_harness` 那份 → 报「存在未登记的验收文件编号：[16]」。
> 改成从 `_harness` 导入。

---

## 三、棘轮守卫的自我修正

本轮棘轮（`tests/test_config_wiring_ratchet.py`）**抓到了自己两个 bug**：

**① 按文件名排除太宽**：`SKIP_NAMES = {"config.py"}` 把 `bettercomfyui/review/config.py`
也排除了 —— 而 `score_dimensions` 正是在那里被读的，被误报成"未接线"。
**修正**：改为按**具体路径**排除（只排除顶层 `bettercomfyui/config.py`、`models/entities.py`、`gap_analysis.py`）。
修正后候选 **12 → 9**。

**② "校验式读取"是新的一类漏报**：`score_dimensions` / `on_timeout` / `new_version_requires_review`
在分层 `config.py` 里被读来做取值校验，扫描认为"已接线"，但业务逻辑从没消费。
**修正**：新增 `KNOWN_UNDETECTABLE` 记录这类，并写明**为什么扫描看不到**；配套交叉校验测试。
其中 `score_dimensions` 标为**真缺口**（评分维度实际不可配：`AssetReviewer` 只接收单一 `score`）。

当前状态：**候选未接线 9 个**，全部有定性：

| 分类 | 条数 | 例子 |
|---|---|---|
| 设计上由别层承担 | 2 | `assets.sampler`、`assets.batch_count` |
| 行为已硬编码 | 4 | `retry_interval`、`regenerate_granularity`、`checkpoint_every_stage`、`normalize_names` |
| **真缺口（未修）** | 3 | `save_vram_all`、`task_timeout_s`、`comfyui_endpoint` |
| **已知漏报（真缺口）** | 1 | `score_dimensions` |

---

## 四、仍未修（如实列出，不粉饰）

| # | 项 | 为什么没修 |
|---|---|---|
| **#4** | **背景音乐（图一 `O7`）** | 愿景**自己标了"后续需提进日程"**，且需要先定方案：BGM 从哪来（素材库音频类？用户上传？）、在哪一步混音（拼接前/封装时）、音量/淡入淡出策略。**这是设计决策，不该我替你拍板。** |
| **#8** | **SG5 `X` 返工时自动加提示词与 @素材** | 需要先确认"加什么"：按失败维度拼（如"角色一致性不合格"→ 强调参考图权重），还是按素材清单拼 `@角色_1_林秀`。也需要与提示词巨人的 `custom_system_prompt` 注入点对齐。 |
| — | `save_vram_all`（显存档位全开） | 需要落到真实导演台的显存参数上；融合节点已把 `vram_tier`/`pruned_model` 传过去，但 `save_vram_all` 这个"一次全开"的开关没有对应落点。**建议直接删掉这个配置项**（显存策略由导演台统一管），而不是硬造一个假开关。 |
| — | `task_timeout_s` | 任务超时需要在执行器层做看门狗；当前 mock 执行器瞬间返回，真实执行器才是它该生效的地方。 |
| — | `comfyui_endpoint` | 执行器实际只用 `comfyui_base_path`。要么让执行器改用它，要么删掉。**两个键重复表达同一件事，应该合并。** |
| — | `score_dimensions` | 要把 `AssetReviewer` 从"单一 score"改成"分维度分"才能真配。改动会扩散到审核结果结构与 UI。 |
| 小偏差 | `C6` 风格标签库、`G2` 目录名不含名称 | 不影响可用性，优先级低。 |

---

## 五、当前结论

* **图一 8 个缺口 → 修 7 个**（#1/#2/#3/#5/#6/#7/#10），另修掉新发现的 #9；
* **验收 92 → 126，全过**，双解释器一致；
* **产品代码未破坏**：既有 92 条验收全绿、16 个节点在真实 ComfyUI 里正常注册；
* **剩下 5 项都写清了原因**，其中 2 项（BGM、返工加料）需要你先定方案。

**最该先看的**：#1 接缝修好后，「真实剧本 → 素材 → 段」第一次能真正跑通 ——
这是整条链从"各自能跑"到"能串起来"的关键一步。
