# 断点续作说明（断电/重启后从这里接上）

> **这份文件的用途**：电脑意外关机（已发生：2026-10-17 凌晨 4 点左右）后，
> 任何接手的人或 Agent 读这一份就能立刻知道**做到哪、下一步做什么**。
> 不要依赖对话记忆 —— 它不一定留得住。

最后更新：2026-10-17 上午

---

## 一、当前一句话状态

**一个统一节点做完了并跑通：`BetterComfyUIOneDirector`（一体化导演台）。**
它把「项目素材库全部已 LOCK 素材」+「多段提示词」→ 整条 `MMX_DIR_GROUP`，
直接接 `MiniMaxH3Director.r2v_groups` 就能出片。

---

## 二、方向纠正（重要，别走回头路）

我此前走偏了：**自己造了一整套插件**（30k 行编排 + 15 个节点），
而愿景第 11 行写明路线是 ——

> 插件或节点包集合。基于成熟导演节点、性能优化节点，制作集合包，
> **互相连接，仅修改少量代码即可实现真正自动化。站在巨人肩膀上。**

以及任务书第 11 行：

> 优先复用成熟 ComfyUI 节点与工作流，**只编写必要的**编排、状态、校验、审核和界面代码。

**所以要做的不是新插件，是把已有的巨人接起来。**

### 已经存在的巨人（不要重复实现）

| 能力 | 已由谁提供 |
|---|---|
| 素材卡（图/音/波形/描述） | `ComfyUI-DNNodes` → `H3MediaLoader` |
| 素材→导演组 | `ComfyUI-DNNodes` → `DNMediaToDirectorGroup` |
| 导演组拆分回逐口 | `ComfyUI-DNNodes` → `DNGroupSplit` |
| 分段调度/任务台 | `ComfyUI-DNNodes` → `DNGroupDirector` |
| SelfLift 一采/二采缓存 | `ComfyUI-DNNodes` → `selflift_cache` |
| 真实出片 + 尾部 22 帧衔接 | `ComfyUI_MiniMaxH3_Director` → `MiniMaxH3Director` |
| AI 分镜 | `ComfyUI_MiniMaxH3_Smart_Director` → `MiniMaxH3SmartDirector` |
| 自动分段 + 首帧硬锁 | `ComfyUI-MiniMaxH3-ChainDirector` → `MiniMaxH3ChainDirector` |

---

## 三、关键设计（来自 `文档/流程图-总.txt`，图一/图二）

| 机制 | 管什么 | 谁实现 |
|---|---|---|
| **素材库参考图** | **身份**（人物/服装/场景/物品） | 我的节点 |
| **尾部 22 帧**（默认 22，可调） | **风格/动作/镜头/光线**连续 | **导演台原生**，默认自动 |

两者**叠加**，不是二选一。依据（流程图原文）：

```
O1["自动取尾部22帧或前一帧无需手动切换"]
O2["主连贯性使用素材库参考图采用参考模式"]
P1["人物不在尾部22帧时由素材库锁定"]
P3["尾部22帧仅作动作镜头光线辅助连续"]
```

图二「素材复用：**所有段都用这一套**」—— 同一套 LOCK 素材被 SEG1/SEG5/SEG9 共同引用。
所以做法是：**公共素材库全量挂到每一段**，段间素材只挂到引用它的段。

---

## 四、已验证（有实际输出为证）

| 项 | 证据 |
|---|---|
| 节点注册 | ComfyUI 0.41.0 里 16 个我的节点（含 `BetterComfyUIOneDirector`） |
| 全量素材挂载 | 4 个已 LOCK 素材 → **每组槽位 [0,1,2,3]**，优先级排序正确 |
| 段间素材隔离 | `分镜_01_特写` 只在第 2 段出现（已从公共集合排除） |
| continuity | 段0=None（永不接续）、段1=True、段2=False（`[硬切]`） |
| 工作流 | `one_director_demo.json` 校验 **19 OK / 0 FAIL**，提交 ComfyUI 图结构被接受 |
| 权重 | 59.38 GB / 6 文件，`real_generation_available=True` |

## 五、未验证 / 做不了

| 项 | 原因 |
|---|---|
| **真实出片** | 实测确认不可行：最小图也需 **34.14 GB** 权重同时驻留（ref2va 19.53 + 文本编码器 14.61），本机物理内存 **15.75 GB**，进 CPU 回退路线时**完全死锁**（0 CPU / 0 磁盘 / 单线程）。换机需 ≥24GB 显存 **且** ≥64GB 内存 |
| 浏览器像素级渲染 | 本机无 headless 浏览器（DOM 级已过） |
| 高级接缝实际效果 | 二采/SelfLift/SemanticBridge/FaceRefine 都缺外部权重；**接缝类型已验证可接 4/4** |

---

## 六、下一步（按优先级）

1. **把重复的节点清掉**：`FusionGroups`/`FusionPackGroup`/`FusionCombine`/`TimelineText`/`AssetSlots`/`AssetMedia`
   与 DNNodes 的资产卡/打包/拆分/任务台功能重叠，应让位给巨人（待用户确认）。
2. **接 DNNodes 资产卡**：让我的素材库 LOCK 版本能驱动 `H3MediaLoader`，
   这样素材卡 UI（波形/裁剪/描述）也能用上。
3. **回灌接上任务台**：`DNGroupDirector` 分段产出 → Director → 我的回灌节点
   （存段版本 + 触发 CNT2/CNT3）。
4. 真实出片留到有 ≥24GB 显存 + ≥64GB 内存的机器。

---

## 七、常用命令

```powershell
# 启动（CPU 模式）
cd '[PROJECT-ROOT]\.comfyui'
& '..\.comfyenv\Scripts\python.exe' main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch

# 网页
http://127.0.0.1:8188

# 同步插件（改完源码必须同步 + 重启才生效）
cd '[PROJECT-ROOT]'
python scripts/sync_to_comfyui.py

# 全量测试
python -B -m pytest tests/ -q

# 生成 + 校验工作流（需要 ComfyUI 在跑）
python -B bettercomfyui_comfy/workflows/build_fusion_workflow.py
python -B bettercomfyui_comfy/workflows/validate_fusion_workflow.py
python -B bettercomfyui_comfy/workflows/submit_check.py
```

> ⚠️ **停止与启动必须分两条命令**：同一条命令里先停后起，会把刚起来的进程一起杀掉（踩过两次）。


---

## 八、第十四轮补充（愿景 §15 验证）

**已验证**：scripts/verify_script_to_prompts.py 把「剧本 → 四类提示词 + 视频提示词」
整条链跑通，**规范 JSON 与「分隔符+中文标签」两种格式都通过**，退出码 0。
详见 docs/ROUND14_愿景§15验证.md。

**两条实测契约事实（别重踩）**：

1. 规范 JSON **只认规范英文键**；用中文键会被判 missing_field: assets。
2. 中文标签（角色/人物/场景/物品/分镜、编号/名称/提示词/素材引用）
   **只在「分隔符文本」格式生效**，JSON 路径不认。
   → 配 output_format 时要与提示词模板一致，两者不能混。

**当前最大断链**：ettercomfyui/prompt/ 那套（gateway/inject/validate/match）
**没有任何节点调用** → 界面上做不到「给剧本自动出提示词」。
缺的只是一个把输入输出接起来的壳。**等你确认后再动手。**

---

## 九、验收取证 + 候选巨人盘点

### 1) 任务书 §9「15 条验收清单」逐条取证 → **全部 PASS**

来源 `tests/verify/_last_run.json`（92 条：PASS 92 / FAIL 0 / UNVERIFIED 0）。
逐条明细见 **`docs/验收清单证据.md`**。

> 口径提醒：验收 15「端到端演示」走的是 **mock/替代路径** ——
> 真实出片在本机不可用（内存差 18GB，见 `MACHINE_SAFETY.md`）。
> **不得把 mock 结果说成真实生成。**

### 2) 重读任务书 §7，盘点候选巨人 → **`docs/候选巨人清单.md`**

**最重要的发现**：§15「剧本 → 提示词」那条断链，答案**不是**我那套 `prompt/`，
而是**本地已经躺着**的两个巨人：

| 包 | 节点 | 关键输入/输出 |
|---|---|---|
| `ComfyUI-Storyboard-LLM` | `Storyboard Generator (DeepSeek)` | 出 `image_prompts` / `video_prompts` / `dialogues` |
| `ComfyUI-DeepSeek-API-connector` | `DeepSeek Prompt Connector` | 入 `custom_system_prompt` / `system_prompt_mode` / `output_language` |

→ 正确接法：**巨人调 DeepSeek 拆剧本，我只补「确定性注入素材清单 + 结构校验」**
（这正是本层不可替代的价值）。**不是自己再写一个剧本节点。**

### 3) 两个巨人的可用性已验（**但未安装**）

* MIT 许可；只依赖 `requests`（本机已有 2.34.2）；
* 裸进程**导入成功**、`NODE_CLASS_MAPPINGS` 拿到、`INPUT_TYPES()` 可调用；
* `DeepSeek-API-connector` **零前端风险**（无 web / 无路由 / 无依赖）；
  `Storyboard-LLM` 低风险（1 个 10.9KB 设置面板 JS + 3 条 `/storyboard/settings` 路由）。

**没有安装的原因**：① 你说过别改代码/环境；② 它们需要 API 密钥，
而本项目规定不得把真实密钥写进配置与文档 —— 装了也只能看到、连不上。
**安装只是文件复制、随时可撤销，等你一句话。**

### 4) 探测方法修正（避免假结论）

第一版探测把 `DNNodes` / `Director` / `ChainDirector` 全判成「导入失败」 ——
**假阴性**：它们缺的是 `folder_paths` / `comfy` / `server.PromptServer`
这类 **ComfyUI 自己的模块**，裸进程当然没有，而这些包此刻正在跑。

判据已改为：**已装的直接查运行中 ComfyUI 的 `/object_info`**（最权威）；
备用的裸进程导入，缺 ComfyUI 内部模块标「需运行时判定」，**不判失败**。
脚本：`scripts/probe_reserve_packs.py`。

### 5) 机器稳定

整轮探测（读大量源码、导入多个包、查 1026 个节点类型）**未触发任何崩溃**。
8:31 之后未再蓝屏 —— `MACHINE_SAFETY.md` 的两条保护看来有效。

---

## 十、接缝审计（ROUND16）—— 找到两个「假成功」级问题

**必须先修正我上一轮的口径**：15 条验收全 PASS 没错，但
**验收测试验的是「库函数」，不是「产品路径」**。实测：

| 函数 | 产品代码里调用 | 测试里调用 |
|---|---|---|
| `validate_script` | **0 次** | 13 次 |
| `build_prompt_messages` / `build_gateway` | **0 次** | 有 |
| `match_candidate` | ✅ `demo.py:297` | 有 |

所以「15 条全绿」与「界面路径真的按契约走」**是两件事**。

### 缺口 1：剧本形状接缝断了 → **假成功**（最严重）

| 环节 | `assets` 在哪级 | 用什么键 |
|---|---|---|
| `validate_script` 规范输出 | **顶层** | `asset_type` |
| `demo.py` 读取 | **章节级** | `type` |

实测三种形状喂 `run_demo`：

| 形状 | ok | 素材新建 | 复用 | LOCK | 段 |
|---|---|---|---|---|---|
| ① **规范**（顶层 + `asset_type`） | **True** | **0** | 0 | **0** | 2 |
| ② 章节级 + `type`（demo 期望） | True | 2 | 0 | 2 | 2 |
| ③ 章节级 + `asset_type` | False | 0 | 0 | 0 | 0 |

① **ok=True 却零素材** —— 素材匹配/复用/疑似/生成/LOCK 整段静默跳过，
导演台拿不到 `locked_assets`，身份一致性失去依据，而调用方看不出问题。

**界面可达**：`BetterComfyUIRun` → `demo.run_demo`，用户粘规范剧本即触发。

证据脚本：`scripts/verify_seam_script_shape.py`（退出码 1，复现缺陷）

### 缺口 2：界面路径**没有结构校验**

任务书 §B 要求「无效引用/重复 ID/缺字段 **不得直接进入素材生成**」。
但 `nodes.py` 里 `validate_script` / `parse_script_output` 调用数为 **0** ——
只做了 `json.loads` 语法检查。

### 附带发现

* 内置 `DEMO_SCRIPT` **通不过** `validate_script`（assets 在章节级，7 条错误）
  —— 演示脚本与它自己的契约不一致，只因从不被送去校验。
* `Chapter.new_asset_candidates` 是**死字段**（定义在 `entities.py:571`，全工程无人读写）。
* 图七逐字段审计另有两处小缺口：`Shot` 缺 `name`；`FF` 分隔符不可配置。

### 建议修法（等你点头）

1. **接缝归一化**（必须）：消费剧本前过 `validate_script`，或两种形状都接受；
2. **界面补校验**：`BetterComfyUIRun` 在 `json.loads` 后调 `validate_script`；
3. **演示剧本对齐契约**；
4. 把"接缝"纳入验收（提升 `verify_seam_script_shape.py` 为
   `tests/verify/test_acceptance_16_seam_shapes.py`），否则这类问题还会漏。

两处缺口已登记进 `bettercomfyui/gap_analysis.py` → 覆盖盘点变为
**83 条：DONE 68 / DELEGATED 13 / TODO 2**。

详见 `docs/ROUND16_接缝审计.md`。

---

## 十一、SG8 异常与恢复审计（ROUND17）

### SG8 六项判定

| 项 | 判定 | 依据 |
|---|---|---|
| ERR2 用户取消/中止 | ✅ 已实现 | `abort_id` / `ABORT_SIGNAL` 遍布 `review/state.py`(19)、`asset_review.py`(15)、`commit.py`(9)、`arbiter.py`(6)、各执行器 |
| ERR3 断点续传 | ✅ 已实现 | `CheckpointStore.save / latest / list_checkpoints / resolve_resume_target`；验收 12 |
| ERR4 版本回滚 | ✅ **隐式支持** | 实测：加 v2 后 **v1 的 LOCK 仍有效** → 回退=继续引用 v1；`clear_lock(aid, ver, reason=)` 可撤坏版本 |
| ERR1 局部失败（只重跑该段） | ✅ 已实现 | `regenerate_granularity=segment` + CNT2（验收 10） |
| ERR1 全局失败（整条链重来） | ✅ 可行 | 重跑 + 检查点回退 |
| **ERR1 中程失败**（仅重跑依赖该素材的段） | ❌ **算不出来** | 见下 |

### 新缺口（第 3 个）：`references` 未接线

```
store/assets.py:389  def references_of(asset_id)
store/assets.py:1001 def record_reference(asset_id, version, ref)
```

**全工程调用数 0。** 实测跑完 `run_demo`（ok=True，素材 3 个）：

```
素材条目 3 个，其中 references 为空 3 个
```

影响三条，都指向愿景：

1. **图七 `GF` 的「引用次数」字段永远无值**（9 个字段里 1 个恒空）；
2. **ERR1 中程失败**无法实现（没有「素材→段」反向索引）；
3. **图四 F2B「仅重跑依赖该角色且未通过的段」**无法实现 ——
   这是愿景「爆炸半径」的核心，**没有索引就只能整条重跑**，与省算力原则冲突。

补起来不难：`cascade.py` 已实现「段→段」依赖边，缺的只是「素材→段」这一维；
在段生成/审核提交时调一次 `record_reference`，中程失败时用 `references_of` 反查。

### 顺带查清：审核事件**确实落盘**（无缺口）

我一度怀疑界面节点的审核不留痕迹，专门验了 —— **多虑了**：

| 路径 | LOCK | review_history | 事件 |
|---|---|---|---|
| 界面节点 `BetterComfyUIAssetReview` 人工通过 | ✅ | **1 条** | **+2** |
| 库路径 `AssetReviewer`（传 `project=paths`） | ✅ | **1 条** | **+10** |

两者都写 `<项目>/logs/events.jsonl`。界面节点事件少（2 vs 10）是因为它直接
`set_lock`、没跑完整审核周期（SCANNING→打分→commit），符合「人工一键通过」语义。
**这条以后不用再查。**

### 我自己的错误（第二个）

第一版读 `review_history` 按 `assets/.../v1.json` 找，
实际布局是 **`assets/<type>/<asset>/versions/<version>/meta.json`**，
于是**两边都读成 0 条**，差点写出「两条路径都不写审核记录」的错误结论。
**先把数据布局确认清楚，再下结论。**

---

## 十二、累计缺口清单（等你点头再动）

| # | 缺口 | 来源 | 影响 |
|---|---|---|---|
| 1 | **剧本形状接缝**（顶层 vs 章节级、`asset_type` vs `type`） | ROUND16 | **假成功**：`ok=True` 却零素材，导演台拿不到 locked_assets |
| 2 | **界面路径没有结构校验**（`validate_script` 产品代码零调用） | ROUND16 | 无效引用/重复 ID 不被拦 |
| 3 | **`references` 未接线**（素材→段反向索引） | ROUND17 | 中程失败与图四 F2B 算不出来 |

建议顺序：**#1（影响最大最隐蔽）→ #3（补起来不难）→ #2**。
覆盖盘点现为 **TODO 3**。
---

## 十三、图一逐节点完成度总表

完整表见 **`docs/图一完成度总表.md`**（程序实测：图一 8 个 subgraph、**133 个节点定义**）。

**评估结果**：已评估 119 个功能节点 → ✅ 94 / 🔵 委托 15 / ⚠️ 2 / ❌ 8。

| 子图 | 结论 |
|---|---|
| SG1 剧本与提示词 | 内容齐（19/20 ✅），但**界面未接通** + `C6` 风格标签库缺 |
| SG2 素材生成与素材库 | `G3` 统一分辨率、`G4` 每类上限 两处漏执行；`G2` 命名不含名称 |
| SG3 素材审核 | **完整** |
| SG4 导演台与封装 | 骨架委托巨人；`O7` 背景音乐、`ENC1` 音频拼接 缺 |
| SG5 视频审核与重生成 | 缺 `X` 返工时自动加提示词与 @素材 |
| SG6 成片输出 | **完整** |
| SG7 全局配置 | `Z3`/`Z4`/`Z5` 日志与配置开关未接线 |
| SG8 异常与中断 | 缺 `中程失败`（依赖 `references` 未接线） |

**8 个缺口按影响排序**：
1. **剧本形状接缝**（假成功：ok=True 却零素材）
2. 界面路径无结构校验
3. `references` 未接线（→ 中程失败、图四 F2B 算不出）
4. `O7` 背景音乐
5. `G4` 每类上限
6. `G3` 统一分辨率
7. `Z3/Z4/Z5` 日志开关
8. `SG5 X` 返工加料

**最该先修 #1**，然后 #2、#3，再装两个剧本巨人。
---

## 十四、ROUND19：图一缺口修复（本轮成果）

完整报告见 **`docs/ROUND19_图一缺口修复.md`**。

**验收三态：126 通过 / 0 失败 / 0 未验证**（系统 python 与 `.comfyenv` 双解释器一致）。
产品代码已同步到 ComfyUI 副件（逐文件哈希一致），**16 个节点在真实 ComfyUI 里全部注册**。

### 已修（7 个缺口 + 1 个新发现）

| # | 缺口 | 关键改动 |
|---|---|---|
| 1 | **剧本形状接缝（假成功）** | `canonicalize_script()` 归一 + `demo.py` 改读规范形状 + `DEMO_SCRIPT` 合规 |
| 2 | 界面路径无结构校验 | `_run_flow` 进入素材生成前过校验闸门（不合法**抛异常**，不能 return） |
| 3 | `references` 未接线 | `plan_segments` 落索引 + `referencing_segments` / `segments_depending_on_asset` |
| 5 | `per_class_limit` 不执行 | `create_asset(per_class_limit=...)`，在 mutator 持锁判断 |
| 6 | `assets.resolution` 不执行 | `add_version(unify_resolution=...)` 最长边等比缩放 + 已符合不重编码 |
| 7 | Z3/Z4/Z5 日志与配置开关 | 新增 `logging_setup.py`：`apply_logging` / `log_manual_or_switch` / 脱敏导入导出 + CLI `config` |
| 10 | ENC1 音频拼接 | `encode()` 三态 auto/mute/manual + 矛盾输入报错 |
| 9 | （新发现）段落盘 | `demo.py` 计划即落盘、状态变化再更新（任务书第 79 行） |

### 仍未修（需要你定方案）

* **#4 背景音乐（O7）**：愿景自己标了"后续需提进日程"；需先定 BGM 来源 / 混音时机 / 音量策略。
* **#8 SG5 X 返工加料**：需先定"加什么"（按失败维度拼 or 按素材清单拼 `@角色_1_林秀`）。
* `save_vram_all` / `task_timeout_s` / `comfyui_endpoint` / `score_dimensions`：各自原因见报告第四节。
  **建议**：`comfyui_endpoint` 与 `comfyui_base_path` 重复表达同一件事，应合并；
  `save_vram_all` 建议删除（显存策略由导演台统一管，硬造开关等于假接线）。

### 重要经验（防止以后重犯）

1. **早 return 不等于失败** —— `run_demo` 外层无条件置 `ok=True`，校验失败必须**抛异常**。
2. **`review_status` ≠ 段是否要重做** —— 判"已通过"要看 `status`，否则 FAILED 的段会被当成 ALLOWED。
3. **棘轮按文件名排除太宽** —— 只排除具体路径，否则分层 `config.py` 会被一起漏掉。
4. **"校验式读取"也是一种漏报** —— 读过做范围校验 ≠ 业务逻辑用了它（见 `KNOWN_UNDETECTABLE`）。
---

## 十五、ROUND20：A1 背景音乐完成，其余进行中

完整进度见 **`docs/ROUND20_全量补齐进行中.md`**。

**验收三态：134 通过 / 0 失败 / 0 未验证**（双解释器一致）。

### 用户本轮四部分要求

- **A** 补齐图一剩余 7 项 → **A1 背景音乐已完成**，A2–A5 待做
- **B** 精准测试（模拟浏览器操作 + 模型边界打桩）→ 待做
- **C** 阶段化显存调度（5090 32G 最优）→ 待做
- **D** 重读文档补齐 → 待做

### 已完成：A1 背景音乐（图一 O7）

`encode(bgm_file=...)`：`amix` 混音 + `-stream_loop -1` 循环补齐 + `-shortest` 截断
+ 可选 `sidechaincompress` ducking；配置 `bgm_volume/bgm_loop/bgm_fade/bgm_ducking`
全部接线；验收 20 共 8 条全过。

### 环境侦察关键结论（后续必须遵守）

| 项 | 结论 |
|---|---|
| `POST /free` | **HTTP 200 可用** —— 阶段化显存调度技术上可行 |
| 可用权重 | 只有 `minimax_h3_ref2va`/`fl2va`；**无素材图模型、无审核模型** → 这两阶段必须打桩 |
| 真实浏览器 | playwright 已装但 **chromium 未下载** → UI 测试走 **HTTP 等价层 + DOM 层** |
| 当前设备 | `cpu`（无 GPU）→ 真实生成不可能，显存调度用**理论计算 + 探针**验证 |
| DeepSeek 巨人 | 两个包都在 `保留备用的节点\`，可装 |

### 踩坑（别重犯）

1. **`default.yaml` 必须由 `dump_config(Config())` 生成**，手改必然被
   `test_dumped_default_config_is_reproducible` 抓到。
2. **两个工具口径必须一致**：`scripts/scan_config_wiring.py` 曾仍用"按文件名排除"
   （报 12），测试已改"按路径排除"（报 9）；已对齐。

### 下一步（按序）

A2 返工加料 → C 阶段化显存调度 → A5 三配置项 → A3 界面接通 DeepSeek →
A4 C6/G2/score_dimensions → B 端到端 UI 测试 → D 重读补齐 + 最终报告

---

## 十六、ROUND25（2026-10-17 20:50）：用户报障已修，当前一句话状态

**现在能做什么**：打开 ComfyUI（CPU 即可）→ 面板「API 配置」填**真实** DeepSeek Key →
打开工作流 **`P1_剧本→全量提示词（本机可跑）`** → 贴一整段剧本 → 点队列：
**只调用 1 次 API**，画布上 8 个预览框显示「角色/场景/物品/分镜图提示词（标准 JSON）+
视频提示词（---）+ 视频提示词JSON + 全量JSON + 摘要」，视频提示词自动填入一体化导演台。
到 GPU 机器再开 **`W0_完整逻辑图全流程`** 出片。

**验收基线（本轮实测，替代旧数字）**：
```
系统 python tests/verify：EXIT=0  通过=260 失败=0 未验证=1   （旧 244）
.comfyenv  tests/verify：EXIT=0  通过=249 失败=0 未验证=3   （旧 234）
全量 pytest tests/      ：EXIT=0
真实 ComfyUI + 本地 mock API：P1 执行 success，mock 调用次数=1，11 个预览输出，9/9 断言 PASS
真浏览器：P1 14/14 节点零错误；打开页面不改写配置（A1 回归 PASS）
```

**三条必须知道的现状**：
1. **三处配置的 API Key 已清空**（原来存的是验收测试假密钥 `sk-acceptance-test-FAKE-…`，
   真实调用必 401）→ **需要用户重新填一次真实 Key**；以后跑验收不会再覆盖它。
2. ComfyUI 目前运行在 http://127.0.0.1:8188（CPU 模式，本会话为验证启动，未停止）；
   **不要点 W0 的队列**（会加载 34GB 权重 → 本机死锁/蓝屏，红线不变）。
3. 真实视频出片仍未验证（环境墙：本机 15.75GB 内存 / 4GB 显存；需 ≥24GB 显存 + ≥64GB 内存）。

**新增/变更的关键文件**：
- 新增 `bettercomfyui/prompt/script_build.py`（一次调用 + 分类拆分核心）、
  `bettercomfyui_comfy/script_prompts.py`（节点 `BetterComfyUIScriptPrompts`，节点数 21）、
  `scripts/rebuild_prompt_workflows.py`、`tests/verify/test_acceptance_31_script_prompts.py`；
- 修改 `bettercomfyui_comfy/web/js/bettercomfyui_panel.js`（不再"打开网页即改配置"）、
  验收 28/29/30（跟着新链路更新 + 新增 A1 回归 P11）、`scripts/verify_install.py`；
- 工作流：新增 `P1_剧本→全量提示词（本机可跑）`；重建 `W0`（22 节点）/`W0L`（14）/`W2`（19）。

**还剩什么**：`BetterComfyUIEverything` 仍用自写简化 prompt（只出镜头）→ 待统一到 `script_build`；
真实 Key 的首次调用需用户实测确认；GPU 机器上的实际出片与显存调度脚本实测。

完整报告：`docs/ROUND25_修复报告.md`。


---

## 十七、ROUND26（2026-10-17 23:00）：已收敛成「一个大节点」

**现在的工作流长这样（用户要的形态）**：
```
[API 配置]（可选，不接则自动读配置）
   → [一体化总控台（唯一大节点）]  ← 剧本写这里
        内部：一次 API 调用 → E1–E5 全量提示词 → 确定性匹配自动登记素材库
              → 已 LOCK 素材全量挂每段 → 输出导演组
        输出：导演组 / 分镜脚本 / 提示词JSON / 素材清单 / 审计报告（后三个已接预览）
   → [MiniMaxH3Director] ← 插模型（UNET/CLIP/VAE）
   → [CreateVideo] → [SaveVideo] ← 插输出
```
**W2_DeepSeek全自动 = 最简形态（12 节点：6 个权重/出片 + 1 个大节点 + 3 个预览 + 1 空）**；
W0=带 API 配置节点的完整版（13 节点）；P1=本机看提示词（5 节点，零权重）；W0L=无 Key 占位体验（4 节点）。

**验收基线（本轮实测，替代旧数字）**：
```
系统 python tests/verify：EXIT=0  通过=263 失败=0 未验证=1
.comfyenv  tests/verify：EXIT=0  通过=252 失败=0 未验证=3
全量 pytest tests/      ：EXIT=0
真 ComfyUI + mock API：P1 success、mock 调用=1、3 预览、临时项目素材库自动登记 5 条、14/14 断言 PASS
真浏览器：P1/W0L/W2/W0 全部打开无错误
verify_install：20 节点可见（ScriptPrompts 已删除）
```

**当前状态提醒**：
1. 三处 API Key 已清空（原来是测试假密钥）→ **需用户重新填一次真实 Key**；
2. ComfyUI 仍运行在 8188（CPU 模式）；**W0/W2 的队列不要在本机点**（会加载 34GB 权重）；
3. `BetterComfyUIScriptPrompts` 节点已删除；ROUND25 报告保留为过程记录，形态已被本轮取代。

**关键文件**：`bettercomfyui_comfy/everything_node.py`（大节点）、
`bettercomfyui/prompt/script_build.py`（一次调用核心）、
`scripts/build_one_node_workflows.py`（工作流重建）、
`tests/verify/test_acceptance_31_one_node.py`（18 条）。

完整报告：`docs/ROUND26_大节点整合报告.md`。


---

## 十八、ROUND27（2026-10-17 23:45）：素材生成 / 素材审核 / 视频审核 已补齐

**现在逻辑图的每个环节都有界面入口了**（用法见 `docs/使用指南_素材生成与审核.md`）：

| 环节 | 工作流/节点 | 在哪跑 |
|---|---|---|
| 剧本→提示词→素材库→导演组 | `BetterComfyUIEverything`（大节点），W2/P1 | 本机可看结构；出片需 GPU |
| **素材生成（图片）** | `S3_素材生成入库审核（GPU）` + `BetterComfyUIAssetGen` | **GPU 机器**（需你的图像模型） |
| **素材入库** | `BetterComfyUIAssetIngest` | 任意机器（跟图像工作流一起） |
| **素材审核** | `BetterComfyUIAssetReview`（人工/模型+CNT1+LOCK） | 本机即可（人工；模型用本地启发式） |
| 出片 | W0 / W2（大节点→导演台→CreateVideo→SaveVideo） | GPU 机器 |
| **视频审核（段级+整片）** | `S4_视频审核（本机可跑）` + `BetterComfyUIVideoReview` | **本机可跑**（任意 mp4 可测） |
| 段级回灌审核 | `BetterComfyUIFusionIngest`（本轮修掉常量 80 假评分） | GPU 机器（导演台逐段产物） |

**本轮修掉的 3 个真 bug**：
1. 回灌节点用常量 80 分假审核（假通过）→ 改真实帧统计；
2. ComfyUI 内 `_orch_repo_root()` 误判 `custom_nodes`（项目读不到/写错地方）→ 已修，
   杂散目录非破坏性隔离到 `_stray_custom_nodes_projects/`；
3. 视频审核整片阈值覆盖未生效 → 已修。

**验收基线**：系统 272/0/1、venv 261/0/3、全量 pytest EXIT=0、契约 BLOCKER=0、
verify_install 23 节点、S4 真机 E2E 7/7、六套工作流真浏览器全通过。

**仍需用户做的**：
1. 填一次真实 DeepSeek Key（三处已清空假密钥）；
2. GPU 机器上给 S3 选图像模型、确认可生成素材图；
3. 若要语义评分，替换/注入自己的评分模型（当前本地评分是真实的启发式，不是语义模型）。

详细：`docs/ROUND27_素材与视频审核补齐报告.md`、`docs/使用指南_素材生成与审核.md`。


---

## 十九、ROUND29（2026-10-18 01:30）：成片剪辑补齐 + 两轮审计收口 + 总报告

### 用户新增要求（凌晨）
成片要「剪辑好的视频」（逻辑图 SG6 SPLICE→ENC→R_full 本来就有）；
生产显存 5090-32G，测试机 4090-48G；按两卡参数出效率/质量报表；
测试机环境提前给预装清单（CUDA/cuDNN/PyTorch）；SSH 后检查剪辑/完整性/画质；
可自行租 4090-48G（≤2h，没货刷新等），不行就暂停等早上。

### 已完成
1. **成片剪辑节点** `BetterComfyUIFinalize`（`bettercomfyui_comfy/finalize_nodes.py`）：
   多段拼接（pipeline.splice）→ 封装（pipeline.encode）→ 自检（时长/段数/黑帧/冻帧/静音/
   启发式四维/占位检测）→ 成片路径自动接视频审核。节点数 23→**24**。
2. **三套总流程工作流**（界面目录 17 套）：
   - `00_【总流程】剧本→全部内容（本机可跑）` 8 节点；
   - `00_【总流程】段视频→剪好成片（本机可跑）` 11 节点（新增，本机可跑）；
   - `00_【总流程】剧本→成片（GPU）` 28 节点（含剪辑→视频审核）。
3. **验收 33/34**（总流程 + 成片剪辑），真机 E2E：剧本→全部内容 7/7；
   3 段→成片 7.54s/完整性 passed/R_full/结论 8/8。
4. **两轮零污染审计收口**（详见 docs/AUDIT_AGENT_报告_R1/R2）：
   R1 修 1 BLOCKER（ffmpeg 占位冒充真实生成）+4 MAJOR；
   R2 再修 2 BLOCKER（ComfyUI 缓存使副作用节点不执行；视频审核复用旧结论）+5 项；
   Lead 独立复核全绿。
5. **总文档**：`docs/第一阶段总交付与总检查报告.md`（目标矩阵/总检查结果/未验证清单/怎么用）。
6. **双卡与测试机**：`docs/ROUND29_双卡效率与质量测算报告.md`、
   `docs/测试机环境预装清单.md`、`docs/SSH验收清单_剪辑完整性与画质.md`。

### 总检查结果
系统验收 281 通过/0 失败/1 未验证；venv 270/0/3；全量 pytest EXIT=0；
契约 BLOCKER=0；安装 24 节点/哈希一致；3 套 00 工作流真浏览器全通过。

### 未完成 / 等用户
- 真实 H3 出片、真实图像模型、真实 API/语义审核：需上机；
- 4090-48G 实测数据回填双卡报表：需 SSH（租机自动购买未完成：Edge 正在使用+需账户/支付确认，已按用户指示暂停）；
- 级联返工自动闭环、通知 UI、SG8 分诊：第二阶段/非阻塞。


---

## 二十、ROUND30（2026-10-18 02:30）：普通用户友好化 + 实机工具链补强

- 新增 `docs/一页纸使用说明.md`（含 3 张自绘流程图 `docs/images/流程图_0*.png`）：
  三套总流程怎么选/怎么点/看什么/看不到就 Ctrl+F5；真实界面截图同时保留。
- 新增 `scripts/browser_screenshots.py`（真浏览器拍三套工作流界面）、
  `scripts/make_usage_diagrams.py`（PIL 自绘中文流程图，绕开缩放看不清节点的问题）。
- 新增 `scripts/vram_monitor.py`（32G 红线：峰值/超 30.5GB 告警/可选 /interrupt，
  已在本机 GTX1050Ti 实跑）；`scripts/remote_env_report.sh/.ps1`（Linux/Windows 体检，
  未真机验证）；`scripts/prepare_remote_bundle.py`（8MB 部署包，已实跑）；
  `scripts/ssh_verify.py`（SSH 编排，未真机验证）。
- 基线复跑：系统验收 281 通过 / 0 失败 / 1 未验证，EXIT=0。
- 租机：极智算公开页在售 4090-48G（截图证据），但 Edge 运行中 Cookie 库被锁 + 登录/支付
  需人工确认，按用户指示暂停等早上；`docs/早上开机操作单.md` 已就绪。


---

## 二十一、ROUND31（2026-10-18 02:35）：成片剪辑边界用例 + 实测回填模板

- 验收 34 从 5 条扩到 **9 条**，新增：
  - P6 单段也能出成片（segment_count_ok/duration_ok/passed）；
  - P7 计划段数与实际不符 → 明确判失败（不静默通过）；
  - P8 纯色/无音轨占位视频 → placeholder_flags 命中、passed=False（防冒充）；
  - P9 段文件不存在 → 明确报错（不静默跳过）。
- 新增 `docs/实测数据回填模板.md`：4090-48G / 5090-32G 到机后按表填，
  即可把双卡理论报表变成实测报表（含效率推算公式与质量对比）。
- 系统验收复跑：**285 通过 / 0 失败 / 1 未验证，EXIT=0**；全量 pytest EXIT=0；
  重新打包部署包 `dist/bettercomfyui_bundle_20261018_012906.zip`（326 文件/8.01MB）。
- 仍在等：SSH / 4090 文本编码器方案 / 是否允许自行租机（按用户指示暂停）。


---

## 二十二、ROUND32（2026-10-18 02:50）：成片剪辑「自动发现最新段视频」

- 成片剪辑节点新增可选开关 `自动发现最新视频` + `发现时间窗_分钟`：
  段视频路径列表留空时，自动扫描 ComfyUI 输出目录/项目输出目录里最近 N 分钟的 mp4，
  按时间排序拼接；审计**列出实际用到的文件**（防悄悄拼错）；无文件时明确报错。
- 验收 34 从 9 条扩到 **11 条**：P10 自动发现有文件→成功且证据完整；
  P11 自动发现无文件→明确报错不造假。系统验收复跑 **287 通过/0 失败/1 未验证**；
  真机 E2E 剪辑回归 8/8 通过。
- 两套剪辑相关说明区/一页纸/总报告均已补该开关说明；部署包将在下轮刷新。


---

## 二十三、ROUND33（2026-10-18 03:05）：32G 生产显存「分两段自动编排」

- 新增 `scripts/staged_run.py`：把 GPU 总流程自动拆成
  A 素材段（配置/剧本+素材生成/入库/审核）→（可选）/free→ B 出片段（导演台+剪辑+视频审核）；
  `--dry-run` 可纯本地验证拆分：28 节点 → A 14 / B 15，两段共用 Everything/Settings，
  提交 prompt 无悬空引用；真跑模式支持 `--stage a|b|all`、`--project` 覆盖。
- 新增 `scripts/free_vram.py`：段间 POST /free 释放模型显存（CPU 上验证 HTTP 200 路由通；
  显存是否真下降需 GPU + nvidia-smi 实测）。
- 新增验收 35（4 条）：拆分结构、无悬空引用、/free 路由、CLI dry-run，全过。
- 诚实边界：分两段只能降低显存，**不能在本机证明 5090 实测 <32GB**；
  必须到机用 vram_monitor + 实测回填模板确认。


---

## 二十四、ROUND34（2026-10-18 03:20）：假分数静态审计 + 音画同步节点路径锁定 + 文档索引

- **静态审计**：全产品代码 grep 常量/假成功模式（`= 80.0`、`80.0 for`、`return 80`、
  `passed=True`、`is_mock_output` 等）：**0 处常量假分数**；`占位/mock` 命中均为
  诚实溯源说明（provenance/docstring），非造假。
- 确认审计 R1 已把 `local_scorer.video_scores` 的「音画同步」改成真实测量
  （`_av_sync_score`：两条流真实时长差 + 帧容差 + 线性映射，无音轨记 0）。
- **验收 32 增 P10**：在「视频审核节点」路径上锁死音画同步——等长 A/V ≥90、
  4s视频+1s音频 ≤30、两者差值 ≥50（防回归成常量假通过）。验收 32 现 10 条全过。
- 新增 **`docs/文档索引.md`**：普通用户 3 份先读、到机 4 份、审计证据备查、命令速查。
- 系统验收复跑：**292 通过 / 0 失败 / 1 未验证**；全量 pytest EXIT=0；
  部署包刷新 `dist/bettercomfyui_bundle_20261018_015216.zip`（330 文件 / 8.03MB）。


---

## 二十五、ROUND35（2026-10-18 03:40）：成片质量审核 CLI（一条命令回答剪辑/完整/质量）

- 新增 `scripts/quality_audit.py`：输入成片（可选段清单/计划段数/时长），输出
  Markdown+JSON 质检报告：完整性（时长/段数/音轨）、剪辑性（场景切换/黑帧/冻帧/静音）、
  质量四维（本地启发式）+抽帧画质、占位检测（码率/无音轨/近单色）、结论与边界说明；
  退出码 0=通过 / 1=不通过 / 2=无法检查。
- 本机用合成视频实跑：2 段拼接成片 → 通过；纯色无音轨占位视频 → 不通过（原因明确）；
  示例报告存 `docs/示例_成片质量审核报告.md`（合成测试视频，非真实生成）。
- 新增验收 36（4 条）：正常通过 / 占位不通过 / 段数不符不通过 / 报告章节与边界齐全，全过。
- 已补进 SSH 验收清单、文档索引、环境清单工具表；系统验收与全量测试将在下一轮复跑。


---

## 二十六、ROUND36（2026-10-18 10:20）：RTX 4090-48G 目标机实机验证（用户提供 SSH）

- 机器：4090 48G / 96G RAM / 500G 数据盘 / Ubuntu22.04 / ComfyUI 0.33.0 生产镜像（H3 权重+TurbLoRA+SDXL 齐全）。
- 部署（只传源码 8MB，其余远端下载）：我们的插件 **24 节点全部注册成功**；编排包用
  BETTERCOMFYUI_HOME=/data/BetterComfyUI 正确解析。
- 环境修复（远端下载安装）：恢复系统 python3（venv 损坏）、python3.10-dev（Triton 编译）、
  apt ffmpeg、pip imageio-ffmpeg/safetensors。
- **测试机当场修复 3 个真问题**：
  1. 叙事流畅公式对单镜头成片天然误判（真实采样 12/12 恒 50）→ 片段级（≤90s）只在机械快切扣分；
  2. 视频审核遇到 5.17s 片段按 5s 切出的 0.17s 尾段抽帧失败会中断整次审核 →
     记录 too_short 并继续；本地新增验收 32 P11 回归；
  3. 镜像生产工作流自定义节点字段与通用 JSON→API 转换器顺序不一致（400）→ 改用官方前端提交流程。
- 实机结果：
  - 3 个真实 E05 take → 我们的成片剪辑节点：**成片 25.47s、完整性 passed、场景切换 2（≈3-1）、
    质量审核「通过」**（叙事100/音画99.3/节奏73.6/跳变83.3）；
  - 真实 H3 生成（镜像 turbo 工作流）：**冷 112.83s / 热 82.45s**，864x480 5.17s；
    **显存峰值 40.93 / 45.58GB** → 32G 生产机必须分阶段（实测支撑）；
  - 生成成片质检「通过」；视频审核节点修复后 R_full 正常执行。
- 报告：`docs/ROUND36_目标机实机验证报告.md`；双卡报表已补 4090 实测章节。
- 本地回归：验收 32 = 11 条全过；系统验收 **297 通过/0 失败/1 未验证**。
- 待办（需 5090 生产机）：5090 实测效率/峰值、双卡实测对比 5090 列、我方导演台节点在该镜像上的适配、
  S3 素材生成用远端 SDXL 实测。


### ROUND36 收尾（2026-10-18 09:40）
- 全量回归又发现并修复第 4 个问题：无音轨成片被其它维度平均后仍判“通过” →
  `full_review` 增加硬约束（音画同步<=0 → 整片不通过），验收 32 增 P12；审计 R2 原失败用例恢复通过。
- 最终：系统验收 **298 通过 / 0 失败 / 1 未验证**；全量 pytest EXIT=0；venv EXIT=0。
- 实机证据归档：`docs/remote_evidence_4090/`（显存 CSV、拼接质检、生成质检、视频审核 JSON 等）；
  生成的 mp4 证据在本机 TEMP：`bcu_remote_evidence/MiniMax_H3_00002_.mp4 / _00003_.mp4`。
- 远端 ComfyUI 仍在 8188（外网 8188）运行，插件 24 节点在。


---

## 二十七、ROUND38（2026-10-18 10:45）：三错误修复落地 + 高清中文宣传片 + S3 真机链路

- 用户报「全流程 剧本→成片 GPU 版」三错误：① 节点错误=缺 MiniMaxH3Director 巨人；② 两个输入错误=
  远端前端 1.48.7 多 control widget 导致 KSampler 值整体错位 + ckpt 空/CreateVideo/SaveVideo 字段错；
  ③ 模型缺失=LoRA 本地名不存在、CLIP 用了 NVFP4、ckpt 空。
- 修复：上传并安装 5 个巨人包（8 个关键节点注册成功；DNNodes 因缺 comfyui-SelfLift 跳过，本工作流不需要）；
  产出 `docs/远程适配工作流/00_剧核总流程_4090适配版.json`（28 节点/31 连线，0 缺失、模型全部命中）
  并已上传远端 UI；修复 S3 工作流「审核先于入库」依赖边并重建（12 节点/15 连线）。
- 真机验证：适配版全链前端加载 28 节点/0 badNodes、graphToPrompt 成功；S3 真机 SDXL 生成 12.36s →
  入库 v1+sha256 → 审核 60.2<70 → BLOCKED/CNT1=1/无 LOCK（符合 R3）。
- 宣传片：`docs/宣传片_剧核_DramaCore_高清中文版.mp4`（1080p/25.67s/15.83MB；5 个真实 H3 镜头 +
  RealESRGAN 模型超分 + 剪辑自检；全中文提示词；质检仅节奏项偏低，如实记录）。
- 多智能体并行：promo-agent（高清宣传片）、fix-agent（三错误诊断+适配+脚本）均已完成并回收。


---

## 二十八、ROUND39（2026-10-18 11:00）：高清效率更正 + 整片预览修复 + 单节点入日程

- 用户指正：正常生成也要高清（此前 864×480 效率测试口径不对）。4090-48G 实测：
  **720p(0.9MP) 单段 232.03s、显存峰值 41.09GB**（480p 为 82.45s）→ 1 小时片 720p ≈23.2h；
  720p 峰值 41GB>32GB → 5090-32G 不能直接 720p 全驻留，生产推荐 **480p 生成 + 模型超分 1080p**
  （实测 120~125s/段，宣传片 5/5 成功）；1080p 原生测试进行中。
- 修复用户反馈 bug：ComfyUI 网页端看不到「一整个成片」→ 成片剪辑节点导出成片到
  `ComfyUI/output/剧核成片/` 并返回 `images/animated/gifs` 预览；本地真机队列验证 PASS。
- 用户要求的「一个超长大节点」（无外部依赖、复制巨人逻辑进我们节点）：因全链测试未完
  （5090 未实测、真实 Key 整链未跑），按用户条件**先入日程** → `docs/待办_下一阶段_超长大节点.md`。
- 报告：`docs/ROUND39_高分辨率效率修正报告.md`；双卡报表已更正。


### ROUND39 补充（2026-10-18 11:20）
- **1080p 原生实测成功**：1920×1088、5.167s、**802.28s/段、显存峰值 42.43GB**。
  结论：48G 能跑但生产不可接受（360 段≈80h）；生产 1080p 走「480p 生成+模型超分」（≈120~125s/段，360 段≈12h@4090）。
  已回填 `docs/ROUND39_高分辨率效率修正报告.md` 与双卡报表。
- 修 bug：成片剪辑「自动发现最新段视频」会误扫本节点导出的成片（导致全量测试失败）→
  排除 `剧核成片/` 导出目录与宣传片/导出文件名，pytest 环境不再扫描 `.comfyui/output`；
  验收 34 恢复 11 条全过、系统验收 298 通过/0 失败。


### ROUND39 收尾（2026-10-18 11:35）
- 修复整片预览（导出 ComfyUI output + ui 预览，本机真队列 PASS）；
- 真实结果版宣传片交付：`docs/宣传片_剧核_DramaCore_真实结果版.mp4`
  （1080p/34.46s，5 真实镜头+4 真实数据卡；音画同步修复为 100；仅节奏 63.6 偏低，如实记录）；
- 修自动发现误扫导出成片 bug；系统验收 298 通过/0 失败；部署包刷新。


### ROUND40（2026-10-18 11:45）：按输出秒归一化效率
- 新增 `scripts/estimate_efficiency.py`（480p/720p/1080p/480p+超分四种路线，可算每秒/每分钟/每小时/N段/5090估算）。
- 4090 实测归一化：480p **16.0s/输出秒**、720p **44.9s/输出秒**、1080p **155.3s/输出秒**、
  480p+超分1080p **24.1s/输出秒**；单段=5.167s/124帧@24fps。
- 报告：ROUND39 §7 已补完整表；双卡报表已引用。


### ROUND41（2026-10-18 11:20）：单节点 `BetterComfyUIOneStop` 真机跑通（无外部依赖）
- 按用户要求：导演台源码 vendor 到 `bettercomfyui_comfy/vendor/h3_director/`（1.49MB，保留 LICENSE），
  新建单节点 `OneStop`：剧本→一次API/占位→提示词→素材登记→节点内加载模型→导演台出片→审核→剪辑→成片。
- 4090 真机 3 次运行，修复 2 个 bug（loader 跨版本 weight_dtype、audio 列表未归一化）后通过：
  **119.20s（含加载）/出片 93.8s、124帧/864×480/5.167s/含音频、审核通过**；
  成片导出 `ComfyUI/output/剧核成片/大节点成片_2.mp4`（网页可播放整片），本地质检亦通过。
- 远端 UI 已放单节点工作流 `【剧核】单节点_剧本到成片.json`（只有一个节点）。
- 边界：素材生图/审核尚未并入单节点（v0.1）；真实 API 路径需用户 Key 才能端到端验证。
- 报告：`docs/ROUND41_单节点首版真机验证.md`；说明：`docs/单节点_剧本到成片_使用说明.md`。


### ROUND44（2026-10-18 15:30）：真实 UI 点击测试通过 + 多段连续性结构验证 + 发布口径修复
- **真实 UI 操作测试（4090 本机 Chrome）**：载入 `【剧核】单节点_剧本到成片.json`（95 控件/0 缺失节点）
  → 只填剧本(52字) → 点 `#queue-button` → **success 160.5s**；R_full 92.08 通过，显存 44640MB；
  截图/结果：`docs/remote_evidence_4090/ui_test/`。
- **P0-2 默认系统提示词**：修复并真机验证（审计「系统模板=严格 JSON（面板默认=是）」）。
- **P0-3 幽灵尾段**：尾段 <1s 忽略 + 抽帧 <2 帧按 too_short；不再误报段2/边界失败。
- **完整性误报**：改按真实生成帧数校验（导演台 H3 帧网格取整），并输出计划/实际说明。
- **P0-6 缺段假成功**：新增计划段数/时长 vs 实产硬校验（缺段或时长<70% → 不通过）。
- **多段连续性**：2 段真机测试结构成立（248 帧、ON=True/guide/22f、pin=[2]、显存 28.8GB、R_full 97.2）；
  暴露「分段编码只出 1 段」bug，已本地修复（合并帧均分 + P0-6），**远端待复测**。
- 本地回归：verify 298 通过/0 失败/1 未验证；全量 pytest EXIT=0。
- 远端 15:05 起 502/SSH 超时（主机 ping 通），多段端到端复测待机器恢复；恢复命令见 ROUND44 §5。
- 报告：`docs/ROUND44_单节点全功能_UI真机与连续性测试.md`；发布评估：`docs/剧核_发布评估.md`（更新版）。


### ROUND47（2026-10-18 16:20）：真实 Key 全链 + UI 移植 + 逻辑对照 + 功能补齐
- 新容器端口：SSH 22；**ComfyUI 外网 http://your-gpu-host:8188**（旧 8188 已不是 ComfyUI）。
- **真实 Key 全链（P0-1 关闭）**：API 模式 185s（API 1 次、素材 9→3 LOCK、R_full 97.2 通过）；
  **真实 UI 点击 200.4s 成功**（只填剧本→点运行；R_full 98.06/R_seg 89.8；显存 44864MB）。
- **导演台 UI 移植完成并部署**：22 帧连续专区（开关/5-22-39-56/音频24/pin 可视化）、素材卡、canvas 时间轴；
  远端截图 `docs/remote_evidence_4090/real_key_ui/07_新导演台UI_远端.png`；本地 30/30 + 兼容 7/7。
- **逻辑图逐段对照**：`docs/剧核_逻辑图逐段代码对照.md`（417 行，157 节点 ✅111/◐38/❌8，带文件:行号；22 帧 16 步调用链）。
- **ROUND46 功能补齐真机验证**：转场/字幕/水印/封面/mov、长片分批+断点续跑、自动返工、22f 连续均跑通；
  唯一未闭环：**2 段返回仅 126 帧（计划 248）**，P0-6 判不通过（无假成功）；下一步改用导演台落盘分段 mp4 拼接。
- 开源安全：bundle 排除 config/user.yaml（只含 example）；.gitignore 加 user.yaml；建议轮换已泄露 Key。
- 报告：`docs/ROUND47_真机全链与UI移植_逻辑对照.md`。


### ROUND47 补充（16:35）：P0-4 修复真机验证 → 关闭
- 根因：导演台 `exportMode="segments"` 把分段 mp4 落盘、images_out 只回部分帧；修复为 `exportMode="all"`。
- 2 段真机：成片 **10.417s / 864×480 / 24fps / 含音频**（计划 10.33s）；**完整性=True、审核结论=通过**；
  R_seg 两段通过（94.49/87.38）、R_full 95.26 通过；22f 连续 ON/guide/22f/pin=[2]；显存 29600MB。
- 证据：`docs/remote_evidence_4090/round46/round46_test_fixed.json`、`二段连续_转场字幕水印_10.4s.mov`。
- 发布判定：全功能版 P0-4 关闭；剩 5090 条件项 + 已声明 P1 边界。


### ROUND47 最终补充（17:00）：新 UI + 真实 Key + strict_json 最终验收通过
- 新导演台 UI（109 控件/0 缺失）只填剧本→点运行：**success 170.3s、API 1 次**；**完整性 True、R_full 97.63 通过**；显存 44928MB。
- 真实 API 无效素材引用改为**丢弃+逐条告警**（不整次失败、不伪造），仅真实 API 路径启用，默认校验仍严格。
- 本地回归：verify **298 通过/0 失败/1 未验证**；全量 pytest **EXIT=0**。
- 证据：`docs/remote_evidence_4090/final_ui/`、`docs/ROUND47_真机全链与UI移植_逻辑对照.md` §八、`docs/剧核_DramaCore_发布说明_v0.1.md` §八。


### ROUND53（2026-10-19）：已知问题修复并发布 v0.1.1-internal
- 默认值对齐：每段 10s、1920×1088、素材最多 4、CNT2/CNT3=3；工作流重新生成（110 控件）。
- 人工审核真实等待（pending/decision 文件队列 + 超时切模型）；素材审核四维补全（一致性=参考版本相似度、合规=违禁词规则）。
- 4 个未接线配置项全部接线（normalize_names / retry_interval / regenerate_granularity / checkpoint_every_stage），扫描器 0 未接线。
- 阶段检查点、返工粒度 full/segment、回归测试 7 项；verify 298/0/1、全量 pytest EXIT=0。
- 发布：GitHub commit 051f56c，tag/Release v0.1.1-internal（内测预发布，含 bundle 附件）。
- 仍开放：GPU 真机完整回归（机器暂不可租）、片头片尾、版本回滚入口、tiled、跨批 pin 近似、级联1层等（见 docs/ROUND53_已知问题修复记录.md）。


### ROUND54（2026-10-19）：三路逻辑审计 + 多段剧本 + 素材上限 64 → v0.1.2-internal
- 三专项智能体独立审计（只读）：ROUND54-A 21 条逻辑问题（P0=4）；ROUND54-B P0=2/P1=10；ROUND54-C 多段剧本设计+Top10 建议。
- 修复：CNT 重复计数、断点指纹扩展+坏段校验、人工审批 nonce/消费、多段剧本总片 R_full+缺章策略+章检查点、按实际段时长审核、单音轨按段切分、LOCK 内容哈希校验、Finalize 自动发现收敛、输出名安全化。
- 新功能：多段剧本顺序生成（每章 1 API、同项目素材复用、章级断点、总片审核）；素材最多生成 64（默认 12，挂参考图按导演台上限 9 截断告警）。
- 测试：verify 298/0/1；全量 pytest EXIT=0；ROUND53+54 单测通过；配置扫描 0 未接线。
- 发布：GitHub main 97d0409，tag/Release v0.1.2-internal（内测预发布，含 bundle 附件）。
- 仍开放：GPU 真机完整回归（机器不可租）、章间 22 帧近似、片头片尾、版本回滚、tiled、级联 1 层、5090 等。
