# ROUND50-A：对照《愿景部分_纯文本.txt》《流程图-总.txt》的全量代码审查（只读）

- 审计性质：独立专项只读审查（**未改任何代码/配置、未重启服务、未提交队列**）
- 需求依据（均完整通读）：`文档/愿景部分_纯文本.txt`（65 行）、`文档/流程图-总.txt`（374 行，含图一~图七）
- 审查范围：`bettercomfyui/`、`bettercomfyui_comfy/`（含 one_stop_node.py / director_runtime.py / review_nodes.py / finalize_nodes.py / everything_node.py / director_edit.py / director_longform.py / routes.py / __init__.py / web/）、`bettercomfyui_comfy/vendor/h3_director/`、`scripts/`、`tests/`
- 审计日期：2026-10-18；环境：ComfyUI 0.41.0 @127.0.0.1:8188（CPU，无大模型）；**远端 4090 关机，本轮不得假设可用**。
- 状态口径：✅ 有直接实现且有测试/可执行证据；◐ 部分实现（库层有/产品默认偏离/仅结构无真机证据）；❌ 未找到实现。
- 编号：愿景 V-01~V-34；流程图 F1-01~F1-102（图一）、F2-01~F2-04（图二）、F3-01~F3-10（图三）、F4-01~F4-03（图四）、F5-01（图五）、F6-01（图六）、F7-01~F7-11（图七）。

## 〇、本轮只读验证（实际执行）

| # | 命令/操作 | 结果 |
|---|---|---|
| 1 | `python -B -m pytest tests/verify -q` | **298 通过 / 0 失败 / 1 未验证**，退出码 0；未验证=验收24 free_models 显存下降（CPU 无显存可观测） |
| 2 | `python -B scripts/scan_config_wiring.py` | 82 配置字段，**4 个候选未接线**：`prompt.normalize_names`、`asset_review.retry_interval`、`video_review.regenerate_granularity`、`global_.checkpoint_every_stage` |
| 3 | `GET /object_info`（只读） | BetterComfyUI* 节点 **25 个**（含 OneStop/Finalize/OneDirector/VideoReview 等），已注册可见 |
| 4 | `dist/bettercomfyui_bundle_20261018_182024.zip` 内容检查 | 517 条目；敏感文件名仅 `config/user.yaml.example`；**未发现 sk-* 真实密钥字面量**；`.gitignore` 已排除 `config/user.yaml`/`.env`/`*.key` |
| 5 | 关键红线源码 grep（无代码修改） | 见下表证据列（One API、FAILED≠BLOCKED、CNT1/2/3、LOCK/版本/哈希、R_seg/R_full、22帧、素材复用、断点续跑等） |

**未做**（受只读/环境限制）：未排队任何工作流（包括本机可跑），未开浏览器点击，未在 GPU 机器执行；因此凡「真实生成/真实出片」项一律标注“未验证”，不写通过。

## 一、统计总览（脚本逐行统计，含本文件全部要求行）

- 愿景：**V-01~V-34 共 34 条** → ✅ 18 / ◐ 16 / ❌ 0
- 流程图：**F1 102 条 + F2 4 + F3 10 + F4 3 + F5 1 + F6 1 + F7 11 = 132 条** → ✅ 89 / ◐ 42 / ❌ 1
  - 分节：F1 73/28/1、F2 1/3/0、F3 8/2/0、F4 0/3/0、F5 0/1/0、F6 0/1/0、F7 7/4/0
- **合计 166 条：✅ 107 / ◐ 58 / ❌ 1**（脚本核验；与本文件表体逐行一致）

**口径说明**：✅=代码实现+测试/可执行证据；◐=部分实现（库层有/产品默认偏离/仅结构无真机证据）；❌=未找到实现。「真机未验证」不等于未实现，已在证据列标注。

---

## 二、愿景清单（V-01~V-34）

| 编号 | 原文摘要 | 实现位置（文件:行号） | 状态 | 证据/缺口 |
|---|---|---|---|---|
| V-01 | 给一段剧本自己跑完、最后给一部成片（真正自动化生产线） | `bettercomfyui_comfy/one_stop_node.py:527,739`（BetterComfyUIOneStop.run 全链）；`finalize_nodes.py:87`；`pipeline/core.py` | ◐ | 单节点实现存在；真实 GPU 端到端未验证（远端 4090 关机） |
| V-02 | 以成熟节点拼接为主，不全部自研 | `vendor/h3_director/`（vendored 导演台执行器）；`fusion_nodes.py` 融合接线 | ◐ | 已 vendor 并接线；`one_stop_node.py:652-653` 自述 tiled 采样「vendor 当前自建 sampler，未接线会明确记录」→ 部分自研 |
| V-03 | 低风险、快落地、可开源；开源用户拿到能直接跑 | `dist/bettercomfyui_bundle_*.zip`（517 条目）；`tests/verify/test_acceptance_28/34` | ◐ | 安装/打包测试在跑；真实可用依赖用户自备图像模型/GPU（本机 checkpoints 为空的历史结论未复测本轮） |
| V-04 | DeepSeek 拆解人物+系统提示词+skill→角色/场景/物品/分镜/最终视频提示词 | `one_stop_node.py:15`（复用 Everything=一次API→E1–E5）；`everything_node.py:323`；`prompt/gateway.py` | ✅ | 验收 27/31 覆盖；OneStop 直接调用 Everything |
| V-05 | 提示词/素材用特殊分隔符标记，程序识别自动分段 | `prompt/validate.py`（parse_script_output/分隔符校验）；`one_stop_node.py:1961 _split_segments`；`prompt/script_build.py` | ✅ | 验收 31/33 断言 `---` 分镜与 JSON 解析 |
| V-06 | 素材库=核心参考源，含角色/场景/物品/分镜参考图 | `store/assets.py`（AssetType 四类）；`one_stop_node.py:1986 _reuse_locked_assets` | ◐ | 四类类型与引用存在；OneStop `素材最多生成个数` 默认 3（`one_stop_node.py:572`），单次不保证四类都生成 |
| V-07 | 素材审核可选人工/模型，人工可持续重试 | `review/asset_review.py`（HUMAN/MODEL 状态机）；`review/state.py` | ◐ | 库层完整（验收 07）；OneStop 的人工选项为「人工通过（演示：模拟操作者）」`one_stop_node.py:576` → 一键路径无真实人工等待 |
| V-08 | 模型审核独立 CNT1，只统计模型候选轮次，默认最多3轮取最高分 | `review/counters.py:1-14,157-163`；`config/default.yaml:33`（model_candidate_limit:3）；`review/asset_review.py` FALLBACK | ✅ | 验收 08/32；计数绑定逻辑任务 ID，版本递增不清零（`counters.py` docstring） |
| V-09 | 人工超时自动切模型，CNT1 从0；此前人工次数不计入 | `config/default.yaml:31,40`；`review/asset_review.py` `on_human_timeout/_switch_to_model`；验收 07 | ◐ | 库层实现（验收 07 通过）；OneStop 一键路径无真实人工超时场景 |
| V-10 | 人工迟到 H_arb 统一后端时间戳仲裁，最终结果只提交一次 | `review/arbiter.py:1-119`；`review/commit.py`；`tests/verify/test_acceptance_09` | ✅ | 只认 backend_ts、平局模型胜、finalization_token 条件更新 |
| V-11 | 通知放报错栏/通知区，不霸屏，完整记录日志 | `store/events.py`、`logging_setup.log_manual_or_switch`；`review/notify.py:26-121` | ◐ | 日志/事件 ✅；popup/sound 默认 `UnimplementedChannel`（notify.py:71-83），**UI 无通知区**（web/*.js 无 notification 实现） |
| V-12 | 不通过→先 AI 识别提示词是否合规→优化提示词→生成新版本 | `models/enums.py:99`（`optimized_new_version_created` 转移） | ◐ | 仅找到状态机转移/事件；**未找到 AI 合规检查与提示词优化的实际调用点**（产品代码 grep 未命中实现） |
| V-13 | 重生成/手动替换→版本+1、重算内容指纹、重审；旧结论/LOCK 不授权新版本 | `store/assets.py`（add_version/set_lock/resolve_lock/verify_locked_file_content）；验收 05/06 | ✅ | `test_acceptance_05/06` 通过；LOCK=素材ID+版本号 |
| V-14 | 人工可持续重试；模型3轮最高分+FALLBACK 来源 | `models/enums.py:128`（FALLBACK_PASS）；`review/asset_review.py _conclude_fallback`；验收 08/32 | ✅ | 来源字段可追溯 |
| V-15 | 用户可调阈值/候选上限/通知/超时/降级策略 | `config/default.yaml:29-46,59-79`；`config.py` 校验 | ✅ | 上述项均已接线（另有 4 个无关开关未接线，见 P1-1） |
| V-16 | 导演台=ChainDirector 自动分段+首帧硬锁 / AIMixer 多段时间轴六模式 / SmartDirector 自动任务+MMX_DIR_GROUP | `vendor/h3_director/`（执行器/时间轴）；`fusion.py`、`unified_director.py`；本地 custom_nodes 已装三巨头 | ◐ | 六种任务模式/时间轴在 OneStop `one_stop_node.py:6,17,537,587`；AIMixer 未被直接检索到独立实现/接线（未找到），主要走 vendor H3 执行器 |
| V-17 | 自动取前段尾部22帧/前一帧衔接，无需手动切换，参考模式均可 | `one_stop_node.py:630-645`（段间连续/连续帧数22/每段引用上段/音频上下文）；vendor continuity | ✅ | 默认 `连续帧数=22`（one_stop_node.py:633）；真实 GPU 衔接效果未验证 |
| V-18 | 连贯性主来源必须是素材库参考图，不能是前一帧；禁止仅用前一帧替代 | `one_stop_node.py:12,17`（docstring：素材库参考图身份 + 尾部22帧辅助）；`one_stop_node.py:1986 _reuse_locked_assets`（只挂已 LOCK） | ✅ | 设计/代码路径成立；真机效果未验证（GPU 关机） |
| V-19 | 尾部22帧只作动作/镜头/光线辅助 | `one_stop_node.py:7,12`；`vendor/.../executor_core.py`（continuity 缓存/清理） | ✅ | docstring 与参数一致 |
| V-20 | 导演台支持一次性约一小时完整内容 | `one_stop_node.py:605-609`（段数≤360、目标总时长≤180分钟）；`director_longform.py:25 plan_batches/62 checkpoint` | ◐ | 结构支持 360 段≈1h@10s；真实 1 小时生成未验证 |
| V-21 | 视频自动 AI 审核（不人工）；段级版本审核；重生成新版重审、旧结论失效 | `review_nodes.py` VideoReview；`one_stop_node.py:1376,1849,1885,1928`（多次 R_seg）；`director/review.py StaleSegmentVersion` | ◐ | 自动审核与重审路径存在；真实 GPU 未验证 |
| V-22 | CNT2 独立当前问题段；级联第N+1段独立 CNT3 不混用 | `review/counters.py:160-162`；`config/default.yaml:73-74`；`one_stop_node.py:410,441,1836-1837,1905,1920`；验收 08/10/11 | ✅ | OneStop 输出含 cnt2/cnt3 与级联记录 |
| V-23 | 段审通过后验证相邻边界；整片审核 S_OUT 前执行一次 | `one_stop_node.py:720,2374`（edgelist/边界相似度）；`review_nodes.py` 边界验证；`pipeline/core.py full_review` | ✅ | 边界阈值/历史落盘；R_full 由最终审核调用（多次 R_seg 用「仅段级」scope） |
| V-24 | 局部重生成后按基础配置重新连接，保持画面/音频/动作连续 | `one_stop_node.py:1792-1815 _regen_one_segment`（continuityFromPrev/cascade 提示）；vendor | ◐ | 参数路径存在；真实效果未验证 |
| V-25 | 算力/质量取中间值；只重新生成不合格段；新段新版本必重审；级联候选独立计数 | `one_stop_node.py:1819-1933 _auto_rework`（只处理 need 段、重审、CNT2/3） | ✅ | 结构闭环；真机未验证 |
| V-26 | 整片审核检查叙事流畅/音画同步/节奏/明显跳变 | `config/default.yaml:68-72`；`review/local_scorer.py`（真实统计评分） | ◐ | 四维可配置且本地启发式计算；**无真实语义评分模型**（见 P0-4） |
| V-27 | 显存任何情况都节省：导演节点显存配置+剪枝/分段采样/潜变量轻量化/FP8/tiled VAE/tiled DiT/unload DiT | `one_stop_node.py:647-656`（显存档位/剪枝/批次间卸载/显存清理）；`vendor/.../vram_cleanup.py`、`executor_core.py:319,427` | ◐ | 卸载/批次/清理已接；tiled 采样自述“未接线会明确记录”，FP8/潜变量轻量化依赖用户选模型（未找到自动策略） |
| V-28 | 低显存仅特殊兼容，显存节省是常态 | `one_stop_node.py:647-656`；`config/default.yaml:54-58`（vram_tier/save_vram_all） | ✅ | 默认开启节省选项 |
| V-29 | 视频审核/图片审核/视频生成/图片生成全部本地；不依赖外部API审核/无云端/无多用户/无商业化 | `review/local_scorer.py`（无网络调用）；ComfyUI 本地执行；`prompt` 外部调用仅用于剧本拆解（需求允许） | ✅ | 审核侧本地；未发现云调度/多用户/商业化代码 |
| V-30 | 多章节生成（因 DeepSeek 记忆有限分章节） | `models/entities.py Chapter`；`store/` chapters；`one_stop_node.py`（单剧本→时间轴） | ◐ | 章节数据结构存在；一键节点未见多章节循环参数（未找到），需人工多次运行 |
| V-31 | 增量素材由程序确定性注入（读清单原样注入+明确指令；程序名称确定性匹配；AI 只输出新增） | `store/inject.py build_existing_asset_manifest`；`prompt/match.py match_candidate`；`prompt/normalize.py`；`config/default.yaml:7-8` | ✅ | 验收 03/04；存在性判断由程序做 |
| V-32 | 项目管理：节点/UI 开关项目、选择项目生成；每项目独立素材库/章节/进度/成片；互不干扰 | `store/projects.py`（create/open/close/switch/resolve_reference）；`web/js/bettercomfyui_panel.js:438-452`（项目选择）；验收 01 | ✅ | 项目隔离测试通过；UI 有项目下拉 |
| V-33 | 开源用户：手动操作/自动切换只记日志；手动素材不审核直接入库；公共/单独库不冲突 | `logging_setup.py:103 log_manual_or_switch`；`bettercomfyui_comfy/nodes.py BetterComfyUIManualAsset`（默认“新增素材(不审核)”）；`LibraryScope.PROJECT/PUBLIC` | ✅ | 日志与手动入库路径存在；库作用域在 store/entities |
| V-34 | 第一阶段只跑通主流程；第二阶段预留自动获取剧本/发布接口 | `bettercomfyui_comfy/phase2.py`（fetch_next_chapter/publish_movie 接缝，默认 NotImplemented）；`one_stop_node.py` 第一阶段闭环 | ◐ | 接缝存在且如实标注未实现；符合“第二阶段预留”定位 |

**V 统计**：✅ 18 / ◐ 16 / ❌ 0（共 34 条；由脚本统计）

---
## 三、流程图清单（图一 F1-01~F1-102）

> 说明：图一按节点/状态/转移拆分编号；「实现位置」只写源码中真实存在的文件:行号，按静态阅读与只读验证确认；依赖 GPU 真机才能判定的项在证据列标注“未验证”。

### SG1 剧本与提示词生成

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-01 | A 剧本输入 | `one_stop_node.py:555`（剧本 widget）；`everything_node.py:426` | ✅ | 单节点/总控均接收剧本 |
| F1-02 | B 用户选择提示词生成模型 | `config/default.yaml:2`（model_provider）；`prompt/gateway.py` provider 注册 | ✅ | external_api/local_qwen/custom 三档 |
| F1-03 | B1/B3 外部大模型 + 开发者自配接口 | `prompt/gateway.py:8,396-457`（custom provider：自定义请求模板+响应取值路径） | ✅ | 验收 27 覆盖 DeepSeek 桥 |
| F1-04 | B2/B4 本地模型千问 | `prompt/gateway.py`（local_qwen provider） | ✅ | 本地推理走 provider；未真机验证 |
| F1-05 | C 读取用户自定义系统提示词 | `prompt/inject.py`（SYSTEM_PROMPT_PRESETS/build_system_prompt）；`models/entities.py:127` | ✅ | 空则用内置模板 |
| F1-06 | C1 预置模板 5 种（写实/电影/动漫/短剧/纪录片/广告） | `prompt/inject.py` SYSTEM_PROMPT_PRESETS | ✅ | 本轮未逐套核对名称（静态存在性证据） |
| F1-07 | C2 可替换变量（风格画风/时长/比例/受众/语言） | `models/entities.py:124-129`（output_language/normalize 等）；`prompt/script_build.py` | ◐ | 语言/风格可配；受众/比例未找到独立变量 |
| F1-08 | C3 输出语言默认中文可选英文 | `models/entities.py:124`；`config/default.yaml:3` | ✅ | zh/en 校验 |
| F1-09 | C4 输出格式结构化 JSON+特殊分隔符 | `config/default.yaml:4`；`prompt/validate.py parse_script_output`；验收 02 | ✅ | JSON+分隔符解析 |
| F1-10 | C5 分镜粒度默认按镜头可切场景 | `config/default.yaml:5`；`config.py:236-237` | ✅ | shot/scene 二选一校验 |
| F1-11 | C6 风格标签库预置供勾选 | `prompt/styles.py`；`everything_node.py` 风格标签 | ✅ | 标签展开（@标签 库） |
| F1-12 | D AI 理解与拆解剧本 | `prompt/gateway.py` + `everything_node.py:426 run` | ✅ | 一次 API 返回结构化结果 |
| F1-13 | E 生成提示词与素材描述 | `prompt/script_build.py`；`prompt/validate.py`；验收 27/31 | ✅ | E1–E5 一次产出 |
| F1-14 | E1 角色图提示词 | `everything_node.py:426`（assets_by_type）；验收 31/33 | ✅ | E1–E5 输出可见 |
| F1-15 | E2 场景图提示词 | 同上 | ✅ | 同上 |
| F1-16 | E3 物品图提示词 | 同上 | ✅ | 同上 |
| F1-17 | E4 分镜图提示词 | 同上；`one_stop_node.py`（分镜/段映射） | ✅ | 同上 |
| F1-18 | E5 最终视频提示词 | `one_stop_node.py`（timeline/segment prompts）；`unified_director.py` | ✅ | 同上 |

### SG2 素材生成与素材库

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-19 | GEN1–GEN4 四类参考图生成 | `one_stop_node.py:2109 _generate_and_review_assets`；`nodes.py BetterComfyUIImageGen`？→ `one_stop_node.py:342 _generate_image` | ◐ | 四类由 E1–E4 驱动；默认 `素材最多生成个数=3`（`one_stop_node.py:572`）单次可能缺一类；真机未验证 |
| F1-20 | GEN_PARAM 六参数（分辨率/步数/采样器/CFG/种子/批量） | `config/default.yaml:21-28`；`one_stop_node.py:583-585`；`one_stop_node.py:200-208`（AssetGen 参数 JSON） | ✅ | 参数齐全且可调 |
| F1-21 | F 特殊分隔符自动分段 | `prompt/validate.py`；`one_stop_node.py:1961 _split_segments`；验收 31/33 | ✅ | `---` 分段可见 |
| F1-22 | G 素材库核心参考源 | `store/assets.py`；`one_stop_node.py:1986 _reuse_locked_assets` | ✅ | 仅 LOCK 素材可作参考 |
| F1-23 | G1 四类分目录（角色/场景/物品/分镜） | `models/enums.py AssetType`（CHARACTER/SCENE/PROP/SHOT）；`store/assets.py` | ✅ | 类型枚举+索引 |
| F1-24 | G2 命名规则 类型_序号_名称 | `store/ids.py:106`（标识符清洗）；素材 human label（`store/assets.py`） | ✅ | ID 含类型/序号；显示名规则存在 |
| F1-25 | G3 格式与尺寸统一分辨率/自动缩放 | `config/default.yaml:22`；`store/assets.py add_version(unify_resolution=...)`；验收 18 | ✅ | 最长边等比缩放 |
| F1-26 | G4 每类默认最多 9 可调 | `config/default.yaml:28`；`store/assets.py create_asset(per_class_limit=...)`；验收 18 | ✅ | 超限拒绝 |
| F1-27 | G5 优先级 角色>场景>物品>分镜 | `unified_director.py:100 _TYPE_PRIORITY`；`review_nodes.py` AssetGen 选取顺序 | ✅ | 常量排序 |
| F1-28 | G6 手动添加入口默认不审核直接入库 | `nodes.py:767 BetterComfyUIManualAsset`（新增素材(不审核)）；`store/assets.py add_manual_asset` | ✅ | 手动路径存在；验收/单测覆盖 |
| F1-29 | GEN 失败 → ERR | `one_stop_node.py` 素材生成失败写 warnings/audit；`review_nodes.py` 抛 `_NodeError` | ◐ | 有异常与可见记录；无独立 ERR 节点/统一错误页（见 F1-98） |

### SG3 素材审核

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-30 | H 素材审核方式（人工/模型） | `config/default.yaml:30`（default_mode MODEL）；`one_stop_node.py:576` 素材审核模式 | ◐ | 库层可选；一键节点人工=「演示：模拟操作者」 |
| F1-31 | H1 人工审核无限重试 | `review/asset_review.py`（HUMAN 状态机，无轮次上限）；验收 07 | ◐ | 库层实现；一键路径无真实人工等待 |
| F1-32 | H2 模型审核最多 3 次取最高分 | `config/default.yaml:33`（model_candidate_limit:3）；`review/asset_review.py` FALLBACK；验收 08/32 | ✅ | 计数+兜底 |
| F1-33 | H3 无人响应超时默认 10 分钟 | `config/default.yaml:31`；`review/asset_review.py` 超时判定 | ✅ | 可调 |
| F1-34 | H4 自动切模型+日志+报错栏小通知 | `review/asset_review.py`（switch_to_model）；`config/default.yaml:40` | ◐ | 切换+日志 ✅；报错栏小通知无 UI 实现（P1-2） |
| F1-35 | H_arb 超时后人工又返回（以人工/模型为准） | `review/arbiter.py`（backend_ts、平局模型胜）；验收 09 | ✅ | 仲裁器真实实现 |
| F1-36 | I 审核通过 | `review/state.py`/`asset_review.py`（ALLOWED） | ✅ | 状态机 |
| F1-37 | K/L/M 不通过→AI合规检查→提示词优化→重新生成素材→更新库 | `models/enums.py:99`（optimized_new_version_created 转移） | ◐ | 仅状态机/事件；**未找到 AI 合规检查与提示词优化的实际实现调用点** |
| F1-38 | LOCK 素材锁定冻结只此一次 | `store/assets.py set_lock/LockAlreadyExistsError/resolve_lock`；验收 06 | ✅ | 版本级 LOCK |
| F1-39 | N 三次以上仍不合格→直接取最高分不再审核 | `review/asset_review.py`（candidate limit→FALLBACK）；验收 08/32 | ✅ | FALLBACK_PASS 来源 |
| F1-40 | H5 评分维度 画质/一致性/合规/构图 | `config/default.yaml:35-39` | ◐ | 四维配置存在；本地启发式仅真实计算部分维度（一致性/合规跳过），见 P0-4 |
| F1-41 | H6 阈值默认 70 可调 | `config/default.yaml:34`；`one_stop_node.py:578`（-1=跟随配置） | ✅ | 可覆盖 |
| F1-42 | H7 重试间隔默认立即可调 | `config/default.yaml:46`；`scan_config_wiring.py` 结果 | ◐ | **配置存在但未接线**（裸词零命中，P1-1） |
| F1-43 | CNT1 重试计数器 | `review/counters.py:157-163`；验收 08 | ✅ | 只计模型候选轮次 |
| F1-44 | H8 通知弹窗+声音+QQ/飞书/钉钉 | `review/notify.py:26-121`（渠道/别名/Webhook 类） | ◐ | 渠道定义在；popup/sound 默认 Unimplemented，QQ/飞书/钉钉需注入 HTTP 传输；**UI 无通知区** |

### SG4 导演台生成与成片封装

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-45 | J_IN 进入导演台（LOCK/E5） | `one_stop_node.py:1608 _execute_timeline`；vendor H3 执行器 | ✅ | 时间轴→vendor 执行 |
| F1-46 | JP1 默认分段时长 10s 可调 5/10/15 | `config/default.yaml:48`；`one_stop_node.py:604` | ◐ | 配置默认 10 ✅；**一键节点默认 5.167s 且无 5/10/15 预设**（P0-2） |
| F1-47 | JP2 默认分辨率 1080P 可选 720P/4K | `config/default.yaml:49`；`one_stop_node.py:617-618` | ◐ | 配置 1080P ✅；**一键节点默认 864×480 自由整数**（P0-2） |
| F1-48 | JP3 默认帧率 24fps 可选 30/60 | `config/default.yaml:50`；`one_stop_node.py:616` | ✅ | 默认 24，范围 8–60 |
| F1-49 | JP4 尾部帧数默认 22 可调 | `config/default.yaml:51`；`one_stop_node.py:633`（[5,22,39,56] 默认 22） | ✅ | 默认 22 |
| F1-50 | JP5 首尾帧默认第一帧/最后一帧可手动指定 | `config/default.yaml:52-53`；`one_stop_node.py:593-595`（首帧/尾帧路径） | ✅ | 手动指定路径存在 |
| F1-51 | JP6 显存档位自动检测可选低/中/高 | `one_stop_node.py:647`（自动/低/中/高）；`cli.py:285` VRAM_TIERS | ✅ | 自动检测+档位 |
| F1-52 | JP7 剪枝模型默认开启可关 | `config/default.yaml:55`；`one_stop_node.py:650` | ✅ | 默认 True，可关（批次间卸载等效） |
| F1-53 | O 导演台自动分段与衔接 | `director_longform.py:25 plan_batches`；`one_stop_node.py:1638 _generate_batches` | ✅ | 自动分批/逐段 |
| F1-54 | O1 自动取尾部 22 帧/前一帧，无需手动切换 | `one_stop_node.py:630-645`（段间连续/引用上段/音频上下文）；vendor continuity | ✅ | 默认开启；GPU 效果未验证 |
| F1-55 | O2 主连贯性使用素材库参考图（参考模式） | `one_stop_node.py:12,17,1986` | ✅ | 只挂已 LOCK 素材图 |
| F1-56 | O3 参考模式均可由节点自动处理 | `one_stop_node.py`（自动挂参考图）；`fusion.py` | ✅ | 自动选择 |
| F1-57 | O4 导演节点自带显存配置 | `one_stop_node.py:647-656`；vendor `vram_cleanup.py` | ✅ | 显存策略参数面 |
| F1-58 | O5 显存节省策略（剪枝/分段采样/潜变量轻量化/FP8/tiledVAE/tiledDiT/unloadDiT） | `one_stop_node.py:650-656`；`vendor/.../executor_core.py:319,427` | ◐ | 卸载/分批/清理已接；**tiled 采样自述未接线**（`one_stop_node.py:652-653`）；FP8/潜变量轻量化依赖用户换模型（无自动策略） |
| F1-59 | O6 片头片尾角色由导演节点集合解决 | 未找到 | ❌ | 产品代码 grep 未命中“片头/片尾角色”专门实现（P1-7） |
| F1-60 | O7 背景音乐需提进日程 | `config/default.yaml:89-92`（bgm_volume/loop/fade）；`pipeline` encode 支持 | ◐ | 配置/编码层支持；一键节点未见 BGM 输入（未找到） |
| F1-61 | ENC 成片封装参数（音频拼接/转场/字幕/水印/格式/编码/CRF） | `config/default.yaml:80-88`；`finalize_nodes.py:97-127`；`director_edit.py:29,70-79`；`one_stop_node.py:691-703` | ✅ | 参数齐全并有实现 |
| F1-62 | P 分段视频生成 | `one_stop_node.py:1608/1638`；vendor H3 执行器 | ◐ | 实现路径存在；**GPU 真机未验证**（远端 4090 关机） |
| F1-63 | P1 人物不在尾部 22 帧时由素材库锁定身份 | `one_stop_node.py:12,17`；`fusion.py locked_asset_ref_images` | ✅ | 只挂 LOCK 素材图（设计满足） |
| F1-64 | P2 场景/物品/服装由素材库锁定 | 同上（四类素材均来自库） | ✅ | 同上 |
| F1-65 | P3 尾部 22 帧仅辅助 | `one_stop_node.py:7,12` | ✅ | docstring 明确 |
| F1-66 | Q_OUT 一次输出约一小时整片 | `one_stop_node.py:605-609`（360 段/180 分钟） | ◐ | 结构支持；真实长片未验证 |
| F1-67 | P 生成失败→ERR | `one_stop_node.py:1879-1880` 等 warnings/异常记录 | ◐ | 有可见失败记录；无统一 ERR 处理节点 |

### SG5 视频审核与重生成

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-68 | Q_MAP 整片→段映射表 | `one_stop_node.py:406 _map_video_segment_scores`；`one_stop_node.py:1500-1562` 段级汇总 | ◐ | 段索引/评分映射存在；映射表字段完整度需真机验证 |
| F1-69 | R 视频生成后自动 AI 审核 | `review_nodes.py` VideoReview；`one_stop_node.py:2331 _video_review` | ✅ | 自动审核节点 |
| F1-70 | R_scope 段级/整片级 | `one_stop_node.py:47`（3 种 scope）；`review_nodes.py` 审核范围 | ✅ | 可切换 |
| F1-71 | R_seg 按段审核 | `review_nodes.py` 段级评分；验收 32 | ✅ | 段级结论 |
| F1-72 | R_full 整片审核 | `review_nodes.py` full_review；`pipeline/core.py full_review`；验收 13/32 | ✅ | 整片审核实现 |
| F1-73 | R1 四维（画面连贯/角色一致/动作合理/无闪烁） | `config/default.yaml:63-67`；`review/local_scorer.py` | ✅ | 维度定义存在；本地启发式近似（真语义未验证） |
| F1-74 | R2 阈值默认 70 可调 | `config/default.yaml:61-62`；`one_stop_node.py:716-718` | ✅ | 段/整片阈值可覆盖 |
| F1-75 | R3 通过→S_OUT | `one_stop_node.py` 审核后进入导出/封装（1376 后流程） | ✅ | 通过则继续出片 |
| F1-76 | T 定位不合格段 | `review_nodes.py` need_regenerate；`one_stop_node.py:448 _parse_need_regenerate` | ✅ | 段索引列表 |
| F1-77 | U 只重生成该段素材不动不重审 | `one_stop_node.py:1845-1928 _auto_rework`（只对 need 段） | ✅ | 非问题段不重生成；素材库不动 |
| F1-78 | REGEN 重新生成视频 + CNT2 | `one_stop_node.py:1792 _regen_one_segment`；CNT2 见 1845-1910 | ✅ | 计数+重生成 |
| F1-79 | CNT2 未满3→V 首尾帧参考→W N+1段用N段首尾帧→N1重生成→X 提示词加@素材→R | `one_stop_node.py:1885-1933`（级联重生成+重审）；`director/augment.py`（返工提示词增强） | ◐ | 级联重生成有；**默认 CNT2 上限 1**（P0-2）；一键节点未走 `augment.py` 的 @素材增强（ReworkController 未接线，P1-3） |
| F1-80 | CNT2 满3→Y 取三次最高分→S_OUT | `review/asset_review.py` FALLBACK 机制（素材侧）；视频侧 `_auto_rework` 上限后保留最后版本 | ◐ | 视频侧“取最高分候选”不完整（只保留/复核最新，未显式候选最高分选择） |
| F1-81 | N1 失败→ERR | `one_stop_node.py:1879-1880`（异常记录）；`director/rework.py` 状态 | ◐ | 记录存在；无统一 ERR 出口 |

### SG6 成片输出

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-82 | S_OUT→OUT1 输出目录 output/日期/剧本名 | `pipeline/core.py write_output`；`one_stop_node.py:1535,2508 _export_to_comfyui`；`finalize_nodes.py:358`（剧核成片） | ✅ | 项目 output + ComfyUI output 双导出 |
| F1-83 | OUT2 文件命名 剧本名/日期/版本 | `pipeline/core.py write_output`；`one_stop_node.py:559` 输出文件名（默认“大节点成片.mp4”） | ◐ | 有命名/时间戳；一键节点默认名不含剧本名/版本（部分） |
| F1-84 | OUT3 元数据 剧本/模型/参数/时间 | `one_stop_node.py:1299-1329`（metadata_extra/provenance）；`models/entities.py MovieMetadata`；`pipeline write_output` | ✅ | 元数据落盘 |
| F1-85 | OUT4 封面默认第一帧可指定 | `config/default.yaml:88`；`one_stop_node.py:699`（默认第一帧） | ✅ | INPUT 默认第一帧；run 签名默认值不同（P2-1 非阻断） |
| F1-86 | OUT5 第一阶段结束（本地成片交付） | `one_stop_node.py`（Finalize→审核→导出）；验收 34 | ✅ | 第一阶段闭环实现 |
| F1-87 | OUT6 第二阶段接口预留（自动获取剧本/发布） | `bettercomfyui_comfy/phase2.py`（预留接缝，默认未实现） | ◐ | 预留存在且如实标注 |

### SG7 全局配置

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-88 | Z 全局配置注入点 C/GEN_PARAM/H2/J_PARAM/ENC/R | `config.py:3`（三层优先级）；各模块读取 cfg；`config/default.yaml` | ✅ | 82 字段注入面；4 个开关未接线（P1-1） |
| F1-89 | Z1 default.yaml | `config/default.yaml:1-103` | ✅ | 默认配置齐全 |
| F1-90 | Z2 用户覆盖默认 | `config.py:3`（项目 config.yaml > user.yaml > 默认） | ✅ | 覆盖顺序 |
| F1-91 | Z3 JSON 导入导出 | `config.py dump_config`；`cli.py config 子命令`；验收 19 | ✅ | 导出清密钥 |
| F1-92 | Z4 日志级别 INFO/DEBUG | `logging_setup.py:66-83`；`config/default.yaml:94` | ✅ | 真正应用到 root logger |
| F1-93 | Z5 记录所有手动操作和自动切换 | `logging_setup.py:103 log_manual_or_switch`；`config/default.yaml:95` | ✅ | 手动/切换日志 |
| F1-94 | Z6 提示词默认配置由需求方写（可配置模板） | `models/entities.py:127`（system_prompt_template）；`config/default.yaml:6`；`prompt/inject.py` | ✅ | 模板可替换 |
| F1-95 | Z7 评分模型由需求方写（可注入） | `review/local_scorer.py`（评分器接口）；`review_nodes.py` 评分来源 | ◐ | 可注入设计存在；默认仅本地启发式，真实语义评分模型未接入（P0-4） |
| F1-96 | Z8 本地提示词模型千问 | `prompt/gateway.py`（local_qwen provider） | ✅ | provider 存在；真机未验证 |
| F1-97 | Z9 外部模型接口开发者自配 | `prompt/gateway.py:396-457`（custom provider） | ✅ | 自定义模板/取值路径 |

### SG8 爆炸半径与错误处理

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F1-98 | ERR 统一异常出口（生成失败/模型报错/显存不足/磁盘满） | 各模块错误类（`store/errors.py`、`director/errors.py`、`review/errors.py`）；`one_stop_node.py` audit/warnings | ◐ | 有分类错误与可见记录；无统一 ERR 节点/错误面板；磁盘满未专门处理（未找到） |
| F1-99 | ERR2 用户取消中止→保存进度 | `one_stop_node.py:612,1670,1765`（断点续跑/检查点）；无取消钩子 | ◐ | 检查点可近似保存进度；**未找到显式取消信号处理** |
| F1-100 | ERR3 断点续传→恢复 | `one_stop_node.py:612,1670-1672,1765-1766`；`director_longform.py:22,62-80`；`store/checkpoints.py` | ✅ | 批次检查点+断点续跑参数 |
| F1-101 | ERR4 版本回滚→回滚素材 | `store/assets.py`（版本历史/LOCK）；未找到显式 rollback API | ◐ | 无专门回滚入口；需手工用旧版本文件重审（P1-8） |
| F1-102 | ERR1 爆炸半径：局部只重跑该段；中程仅重跑依赖素材未通过段/已通过缓存；全局整链重来 | `one_stop_node.py:1819 _auto_rework`（局部✅）；`director/cascade.py`、`director/`（`segments_depending_on_asset` 库层）；`one_stop` 断点续跑（全局） | ◐ | 局部闭环在；中程依赖分析未接入一键节点（未找到）；全局重跑靠重跑节点/断点 |

---

## 四、流程图清单（图二~图七）

### 图二：素材复用与衔接（F2-01~F2-04）

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F2-01 | 角色素材流转：提示词→生成→只审一次→LOCK→多段 @ 引用→下一部剧本复用 | `one_stop_node.py:1986 _reuse_locked_assets`；`store/assets.py` LibraryScope；`unified_director.py` @引用 | ◐ | 流程存在；跨剧本复用依赖 PUBLIC 库/同项目，未真机验证 |
| F2-02 | 场景/物品/分镜素材同流程 | `models/enums.py AssetType` 四类；素材审核/入库通用 | ◐ | 类型枚举统一；真机素材图生成未验证 |
| F2-03 | 段级重生成素材不动不重审；首尾帧衔接；段9复用段8首尾帧 | `one_stop_node.py:1845-1920`（只重生成视频段）；首尾帧/连续参数 | ◐ | 素材不动✅；段间首尾帧在 vendor，真机未验证 |
| F2-04 | 视频段衔接：尾部 22 帧辅助 | `one_stop_node.py:633,642`（22帧/引用上段）；vendor motion context | ✅ | 默认 22 帧；效果未验证 |

### 图三：默认参数策略（F3-01~F3-10）

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F3-01 | 默认外部大模型 | `config/default.yaml:2` | ✅ | model_provider: external_api |
| F3-02 | 默认模型审核 | `config/default.yaml:30` | ✅ | default_mode: MODEL |
| F3-03 | 手动素材默认不审核直接入库 | `nodes.py:767 BetterComfyUIManualAsset`；`config` | ✅ | 默认“新增素材(不审核)” |
| F3-04 | 默认首尾帧自动取（first/last） | `config/default.yaml:52-53` | ✅ | first/last |
| F3-05 | 默认音频拼接/无转场/无字幕/无水印 | `config/default.yaml:81-84` | ✅ | auto/none/false/false |
| F3-06 | 默认配置（JSON/配置文件） | `config/default.yaml` | ✅ | 103 行完整默认 |
| F3-07 | 默认不中止（一直跑） | `one_stop_node.py` 无主动中止逻辑；ComfyUI 服务端 interrupt 属于外部 | ◐ | 行为默认持续；未找到显式“不中止”机制/说明 |
| F3-08 | 人工超时默认切模型 | `config/default.yaml:31,40` | ✅ | 10min + switch_to_model |
| F3-09 | 默认推荐生成参数（1024/25步/DPM++/CFG7/随机种子/batch4） | `config/default.yaml:21-28` | ✅ | 与流程一致 |
| F3-10 | 默认导演台 10s/1080P/24fps/尾22 | `config/default.yaml:47-53` | ◐ | 配置默认 ✅；**一键节点实际默认 5.167s/864×480**（P0-2） |

### 图四：失败场景与爆炸半径（F4-01~F4-03）

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F4-01 | 局部失败→只重跑该段素材不动不重审→N+1 复用首尾帧 | `one_stop_node.py:1819 _auto_rework`；`_regen_one_segment` | ◐ | 局部重生成实现；N+1 级联在 `one_stop_node.py:1905-1933`；真机未验证 |
| F4-02 | 中程失败→仅重跑依赖角色且未通过段→已通过段缓存→重新锁定新版本 | `director/cascade.py`、`director/`（`segments_depending_on_asset` 库层）；`one_stop` 未调用 | ◐ | 库层有依赖索引；**一键节点未接中程依赖分析**（P1-3） |
| F4-03 | 全局失败→整条链重来→从最近断点恢复或重跑 | `one_stop_node.py:612,1670-1766`（断点续跑/检查点） | ◐ | 断点恢复✅；全局重跑靠重跑节点 |

### 图五：最小闭环验收（F5-01）

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F5-01 | 最小闭环：剧本→提示词→素材生成→审核一次→LOCK→三段落复用→视频审核→不合格只重生成该段→合格输出→扩展一小时 | `one_stop_node.py:527,739`（全链）；`finalize_nodes.py`；验收 31/32/33/34 | ◐ | 单节点实现闭环且测试通过；**真实 GPU 真机未验证**（远端关机） |

### 图六：一小时规模能力（F6-01）

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F6-01 | 一小时约 360 段的采样/审核/衔接/整片拼接规模 | `one_stop_node.py:605-609`（360段/180min）；`director_longform.py`（分批+检查点） | ◐ | 参数与分批/续跑支持；真实一小时生成未验证 |

### 图七：数据契约（F7-01~F7-11）

| 编号 | 原文摘要 | 实现位置 | 状态 | 证据/缺口 |
|---|---|---|---|---|
| F7-01 | E1/E2/E3 字段 id/name/prompt/style/ref_images | `prompt/validate.py`（结构化输出解析）；`prompt/script_build.py` | ◐ | 输出结构校验存在；ref_images 逐字段未核对 |
| F7-02 | E4 分镜字段 id/name/prompt/shot_index/duration | `prompt/validate.py`；`one_stop_node.py` 分镜段 | ◐ | 分镜/场景数据存在；字段名未逐项核对 |
| F7-03 | E5 最终视频字段 id/segments/task_type/ref_asset_tags | `one_stop_node.py` 时间轴/任务类型；`unified_director.py` 分段结构 | ◐ | 结构近似；字段名未逐项核对 |
| F7-04 | F 分隔符形式（段开始/结束/字段分隔） | `prompt/validate.py`；`config/default.yaml:4` | ✅ | JSON+分隔符解析有测试（验收 02/31） |
| F7-05 | G 素材条目字段（类型/序号/名称/路径/版本/审核状态/评分/引用次数/锁定状态） | `models/entities.py` AssetEntry/AssetVersion；`store/assets.py` 索引 | ✅ | 字段齐全（引用次数/锁定状态有） |
| F7-06 | J 导演台入参（素材引用/任务模式/首帧/尾帧） | `one_stop_node.py:587-595`；`vendor/h3_director` 时间轴 | ✅ | 参数 widget 存在 |
| F7-07 | X @素材格式 `@素材类型_序号_名称` | `unified_director.py` `_AT_RE`（@引用）；`store/ids.py:106` 标识符规则 | ◐ | @引用语法存在；显示名规则相近但非严格“类型_序号_名称”（ID 为类型_序号_短码，human label 为类型_序号_名称） |
| F7-08 | P 生成参数契约（分辨率/步数/采样器/CFG/种子/批量） | `config/default.yaml:21-28`；`one_stop_node.py:583-585`；AssetGen 参数 JSON | ✅ | 契约一致 |
| F7-09 | D 导演台参数契约（分段/分辨率/帧率/尾帧/首尾/显存/剪枝） | `config/default.yaml:47-58`；`one_stop_node.py:604-656` | ✅ | 参数面齐全 |
| F7-10 | E 封装参数契约（音频拼接/转场/字幕/水印/格式/编码/CRF） | `config/default.yaml:80-88`；`finalize_nodes.py`；`director_edit.py` | ✅ | 契约一致 |
| F7-11 | R 审核参数契约（维度/阈值/重试上限/超时/通知） | `config/default.yaml:29-46,59-79` | ✅ | 参数齐全（通知端到端未实现，见 P1-2） |

---

## 五、严重问题清单（P0/P1/P2）

### P0（发布阻断：不解决不得宣称“完整验收通过/可发布”）

| 编号 | 问题 | 证据（文件:行号） | 为什么阻断 |
|---|---|---|---|
| P0-1 | **真实 GPU 链路全部未验证**：本轮远端 4090 关机，只读环境为 CPU；所有“真机出片/22帧连续/一小时整片/CNT 返工效果”只有代码与 CPU/单测证据 | 远端不可用（环境事实）；`one_stop_node.py:1608/1638` 等生成路径；本轮只读验证仅 `pytest tests/verify`（298 通过/0 失败/1 未验证） | 发布评估不得把这些项写成“通过”；必须补 GPU 真机证据，否则“从剧本到成片”未验收 |
| P0-2 | **一键节点默认值与流程图默认冲突**：每段时长默认 5.167s（流程要求 10s 可调 5/10/15）；宽高默认 864×480（流程要求 1080P 可选 720P/4K）；CNT2/CNT3 上限默认 1（流程要求最多 3）；素材最多生成个数默认 3（四类参考图可能缺一） | `one_stop_node.py:604`、`one_stop_node.py:617-618`、`one_stop_node.py:723,727`、`one_stop_node.py:572`；对照 `config/default.yaml:47-53,73-74` | 用户默认路径直接违反需求默认值；属于“看起来实现、默认行为不符” |
| P0-3 | **人工审核在一键产品路径是“演示：模拟操作者”**，未接真实人工等待/超时切换/仲裁/通知链路 | `one_stop_node.py:576`（人工通过（演示：模拟操作者））；库层 `review/asset_review.py` 有真实状态机但未在一键节点使用 | 需求 V-07/V-09/F1-30 要求“人工审核可选”；发布说明若宣称支持人工审核则构成不实 |
| P0-4 | **评分维度只部分真实计算**：需求 4 维（画质/一致性/合规/构图），本地启发式仅计算可量化维度，语义维度跳过（如实标注），且需求方真实评分模型未接入 | `config/default.yaml:35-39`；`review/local_scorer.py`；`one_stop_node.py:576` 等评分来源 | 需求“素材评分 4 维、评分模型可注入”未闭环；不能据此宣称审核质量达标 |
| P0-5 | **K/L 失败后 AI 合规检查 + 提示词优化未找到实现**：仅状态机事件/转移 | `models/enums.py:99`（`optimized_new_version_created`）；产品代码 grep 未命中合规检查/提示词优化实现 | 需求 V-12/F1-37 的“不合格→合规检查→优化→新版本”闭环缺失，素材返工只能人工/重生成 |

### P1（发布前应修/必须写入已知边界）

| 编号 | 问题 | 证据（文件:行号） |
|---|---|---|
| P1-1 | 4 个配置字段“候选未接线”：`prompt.normalize_names`、`asset_review.retry_interval`、`video_review.regenerate_granularity`、`global_.checkpoint_every_stage` | `python -B scripts/scan_config_wiring.py` 输出；`config/default.yaml:8,41,46,75,98` |
| P1-2 | 通知 H8 端到端未实现：popup/sound 未注册、QQ/飞书/钉钉需注入传输层、UI 无通知区 | `review/notify.py:26-121`；`web/js/bettercomfyui_panel.js` 无 notification 逻辑 |
| P1-3 | 返工/级联不完整：一键节点 CNT2/CNT3 默认 1、`max_cascade_depth` 硬编码 1；中程失败（依赖素材→未通过段）未接 `segments_depending_on_asset`；视频侧“满 3 次取最高分候选”未显式实现 | `one_stop_node.py:723,727,1838`；`director/cascade.py`（库层） |
| P1-4 | 显存策略部分未接线：`tiled采样` 自述 vendor 未接线会记录；FP8/潜变量轻量化依赖用户换模型，无自动策略 | `one_stop_node.py:652-653`；`config/default.yaml:54-58` |
| P1-5 | O6 片头片尾角色未找到实现；O7 背景音乐配置存在但一键节点无 BGM 输入 | `config/default.yaml:89-92`；未找到 O6 实现 |
| P1-6 | 取消中止保存进度无显式钩子；素材版本回滚无入口（仅旧版本文件可手工重审） | `one_stop_node.py:612,1670`（检查点）；未找到 rollback/cancel 实现 |
| P1-7 | 多章节（V-30）在一键节点无多章节参数：仅单剧本→时间轴，多章节需人工多次运行 | `one_stop_node.py` 参数面（555-740）无 chapter 循环 |
| P1-8 | 阶段二接口为预留未实现（符合定位，但发布说明必须写明） | `bettercomfyui_comfy/phase2.py` |

### P2（非阻断，改进项）

| 编号 | 问题 | 证据 |
|---|---|---|
| P2-1 | 一键节点封面帧默认值两处不一致：INPUT_TYPES 默认“第一帧”，`run()` 签名默认“无” | `one_stop_node.py:699` vs `one_stop_node.py:824` |
| P2-2 | 品牌双轨：产品内品牌“剧核/DramaCore”，仓库/文档/包名仍 Better ComfyUI，未统一 | `web/js/bettercomfyui_panel.js:2`；`web/js/dramacore_i18n.js`；仓库名/包名 |
| P2-3 | `gap_analysis.py` 自评 DONE/DELEGATED 与本次审查口径不一致（自我声明不能当作证据） | `bettercomfyui/gap_analysis.py:44-170` |
| P2-4 | 输出文件命名默认“大节点成片.mp4”，未含剧本名/日期/版本（流程 OUT2 期望）；覆盖时依赖用户填写 | `one_stop_node.py:559`；`pipeline/core.py write_output` |
| P2-5 | 报错栏/通知区缺失（与 P1-2 关联），日志有但 UI 不可见 | 同 P1-2 |

---

## 六、可直接执行的验收检查清单（发布评估阶段逐项打勾）

> 说明：以下为只读/可执行检查；GPU 项在远端 4090 可用时执行，证据归档到 `docs/remote_evidence_4090/`；未执行项一律标“未验证”。

| # | 检查项 | 命令/操作 | 通过标准 | 当前状态（本轮） |
|---|---|---|---|---|
| 1 | 验收测试套件 | `python -B -m pytest tests/verify -q` | 退出码 0；失败=0；未验证项保持单列 | ✅ 298 通过/0 失败/1 未验证 |
| 2 | 契约/安装 | `python -B tests/verify/check_contracts.py`；`python -B scripts/verify_install.py` | BLOCKER=0；安装完整 | 未在本轮执行（上一轮 R2 曾 0 BLOCKER/5-5 OK，需重跑） |
| 3 | 配置接线审计 | `python -B scripts/scan_config_wiring.py` | 未接线字段逐一定性（P1-1） | 4 个候选未接线 |
| 4 | 一键节点默认值 | 读 `one_stop_node.py:555-740` 对照 `config/default.yaml:47-88` | 默认值=需求默认（10s/1080P/24fps/尾22/CNT 3） | ❌ 5.167s/864×480/CNT1（P0-2） |
| 5 | 一次 API 红线 | 跑单节点（任一环境）检查审计/`tests/verify/test_acceptance_31_one_node.py::test_p10` | API 调用 ≤1 且审计显示 | 单测通过（P10），真机未验 |
| 6 | 人工审核端到端 | 人工模式→不操作→超时→切模型；人工迟到仲裁 | 超时切模型且 CNT1 从 0；仲裁单次提交 | 库层单测通过；一键节点未接（P0-3） |
| 7 | 素材审核 4 维评分 | 构造一致性/合规不合格样本 | 4 维均被计算且阈值生效 | ◐ 仅部分维度真实计算（P0-4） |
| 8 | 素材四类生成+入库 | 一键节点开启生成素材图跑一次 | E1–E4 四类均入库且可 LOCK | ◐ 默认最多 3 个（P0-2） |
| 9 | LOCK/版本/哈希 | `pytest tests/verify/test_acceptance_05/06`；手工篡改文件 | 篡改被拒、旧 LOCK 不授权新版本 | ✅ 单测通过 |
| 10 | CNT1/2/3 隔离 | `pytest tests/verify/test_acceptance_08/10/11` | 计数互不混用、级联独立 | ✅ 单测通过 |
| 11 | R_seg/R_full 一次 | `pytest tests/verify/test_acceptance_13/32`；检查 one_stop 审计 | R_full 只在最终一次；段级路径不触发 | ✅ 单测通过 |
| 12 | 22 帧衔接真机 | 远端 4090：两段生成检查首 22 帧/连续性参数生效 | 段间连续、无跳变（人工抽检） | 未验证（4090 关机） |
| 13 | 一小时规模 | 远端 4090：小分辨率下 30–60 段回归 | 全部完成、断点续跑可恢复 | 未验证 |
| 14 | 显存策略 | 远端 4090：检查批次间 unload/峰值；tiled 采样是否生效 | 峰值不超目标机预算；tiled 生效或明确记录未接线 | 未验证 |
| 15 | 通知 H8 | 触发人工审核/切换，观察弹窗/声音/通知区 | UI 可见通知 + 日志完整 | ❌ 未实现（P1-2） |
| 16 | 开源安全 | 解压 `dist/bettercomfyui_bundle_*.zip` 检查 `user.yaml`/`sk-*`；`git check-ignore config/user.yaml` | 无真实密钥、无 user.yaml | ✅ 无密钥（仅 user.yaml.example）；gitignore 覆盖 |
| 17 | UI 只读可测性 | 浏览器打开 8188：面板项目下拉/剧本输入/审核结论区/中英切换；`GET /object_info` | 25 个 BetterComfyUI* 节点；UI 元素可操作（只读不可提交队列） | 节点 25 个 ✅（浏览器操作本轮未做） |
| 18 | 品牌与多语言 | 检查面板/节点标题/文档品牌与 i18n（zh/en） | 品牌统一、双语可切换 | ◐ 品牌双轨（P2-2）；i18n 存在 |
| 19 | 阶段二边界 | 读 `phase2.py` + 发布说明 | 明确“预留未实现” | ◐ 预留存在，需文档写明 |
| 20 | 远端证据归档 | 所有 GPU 复验写入 `docs/remote_evidence_4090/` 并引用 | 发布报告只引用有证据结论 | 待执行 |

---

## 七、本轮未验证清单（禁止写成通过）

1. 真实 GPU 出片、22 帧连续性、CNT2/CNT3 真机返工、R_full 真机、**一小时整片**（远端 4090 关机，本机 CPU 无大模型）。
2. 真实外部提示词 API（DeepSeek/千问）在线链路与一次 API 真机审计。
3. 真实语义评分模型对 4 维评分的效果。
4. 浏览器交互（本轮仅静态阅读 web/ 与只读 GET `/object_info`，未点击 UI、未提交任何队列）。
5. `check_contracts.py` / `verify_install.py` 本轮未重跑（上一轮历史结果不能替代本轮）。

## 八、结论（本审查部分）

- 需求 166 条（V34 + F132）中，**代码层有实现证据的占多数**；核心编排（一次 API、素材库/LOCK、CNT1/2/3、R_seg/R_full、素材复用、断点续跑、项目隔离、本地审核、开源安全）在**库层与单测层通过**。
- **发布前必须解决 5 个 P0**：GPU 真机全链路未验证、一键节点默认值偏离需求、人工审核仅演示、4 维评分未闭环、失败后 AI 合规检查/提示词优化未实现。
- 1 个环境性阻断（远端 4090 关机）不是代码缺陷，但发布评估不得绕过。
- 本文件所有结论均可回溯到源码行号或本轮可执行命令输出；未验证项已单列。

*（本审查为只读专项；未修改任何产品代码/配置，未重启服务，未提交队列。）*
