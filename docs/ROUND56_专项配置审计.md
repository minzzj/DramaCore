# ROUND56 专项配置审计（只读静态 + 只读运行时推演）

> 审计时间：2026-10-19 22:50–23:55（Asia/Shanghai）｜审计者：ui-agent
> 目标：专门排查 ROUND56 用户反馈的三类问题——**「添加一段无效」「默认配置没配置好」「高级参数没蒙上」**，以及同类逻辑漏洞。
> **本次审计未修改任何代码/配置、未重启任何服务、未跑 GPU/网络**；仅在系统临时目录运行只读分析脚本（`sys.path` 导入模块 + monkeypatch 假执行，不落盘、不调用模型）。仓库内唯一新增文件即本报告。
> 证据文件：`bettercomfyui_comfy/one_stop_node.py`、`.../web/js/dramacore_director_ui.js`、`.../workflows/00_【单节点】剧本→成片（无外部依赖）.json`、`bettercomfyui/pipeline/*`、`director_edit.py`、`config/default.yaml`、`tests/test_round56_features.py`、`tests/test_round55_workflow_alignment.py`。

## 0. 结论速览

| 级别 | 数量 | 说明 |
|---|---|---|
| **P0（会直接产出错误成片/首跑失败）** | **2** | 默认配置下硬字幕被烧两遍（双字幕重叠）；默认工作流模型/任务三处错配 |
| **P1（用户大概率会骂的配置/交互缺陷）** | **10** | 多段空列表静默跑单段、64 段截断无提示、断点续跑吞配置变化、提示词保存不生效、旧工作流错位、双源覆盖、高级项不可见、字幕回退用提示词、Finalize 被绕过、⑭提示词面板未蒙版 |
| **P2（体验/一致性/测试盲区）** | **7** | 默认值两处漂移 14 项、config/default.yaml 漂移、蒙版数量说明不符、模型选择无校验、默认摘要不全、测试只断言源码字符串、combo 空列表无预检 |
| **通过项** | 12 项 | 见第 4 节（含 119 values 1:1 对齐、H3 手册全文真注入、AI 转场真实现、ROUND56 测试 8/8 通过等） |

**最严重 5 条**：P0-1 双字幕重叠；P0-2 默认工作流 视频VAE=音频VAE / 图像模型为空 / UNET 与任务类型不匹配；P1-1 多段空列表静默跑默认单段剧本；P1-3 断点续跑忽略字幕/转场/提示词变化；P1-4 提示词面板保存后实际不生效。

---

## 1. 审计方法（可复现、只读）

```powershell
cd '[PROJECT-ROOT]'
# ① 现有 ROUND56 / 工作流对齐测试（只读运行）——8 passed
python -B -m pytest tests/test_round56_features.py tests/test_round55_workflow_alignment.py -q -o addopts=""
# ② 只读分析脚本（系统临时目录；用 AST 提取 114 个 INPUT_TYPES，用 monkeypatch 验证 run() 路由）
python -B <临时目录>/audit_round56_runtime.py
# ③ UI 绑定/蒙版映射脚本（正则扫描 dramacore_director_ui.js + 与 114 输入求差集）
python -B <临时目录>/audit_ui_map.py
```

- 114 个输入（required 109 + optional 5）由 AST 从 `one_stop_node.py::INPUT_TYPES` 提取（含 default/tooltip/multiline）。
- `run()` 多段路由用 `object.__new__` + monkeypatch `_run_single/_run_multi_script` 验证，不触发任何生成。
- 工作流 `widgets_values` 按 `INPUT_TYPES` 定义顺序 + 4 个分组 button + `control_after_generate` 重建（与 `tests/test_round55_workflow_alignment.py::_expected_order` 同逻辑），实测 **119 == 119 1:1 对齐**。

---

## 2. 问题表

### P0

| ID | 位置 | 复现 | 期望 | 实际 | 建议 |
|---|---|---|---|---|---|
| **AUD-56-01** 默认硬字幕被烧两遍 → 双字幕/文字重叠 | `one_stop_node.py:975`（`"subtitle_label": str(字幕)`），`director_edit.py:1033`（`subtitle = not startswith("无")`），`director_edit.py:1037`（`advanced = subtitle or …`），`director_edit.py:1092-1095`（用 **options["prompts"]** 生成 SRT 并烧录），`one_stop_node.py:1538-1577`（ROUND55 后处理再烧一次硬字幕并 `copy2` 覆盖成片）；默认工作流 `:1989` 字幕=`硬字幕（程序对齐）` | 用默认工作流点 Queue 跑任意 2 段以上视频 | 成片只有**一层**程序字幕 | finish_movie 因 `subtitle=True` 走 advanced 分支，先把「各段提示词」烧成 SRT；随后节点 ROUND55 后处理又按台词把程序时间轴硬字幕烧一遍 → **同位置两层文字重叠**（提示词长句 + 台词短句），是用户最恨的"文字漂移/叠字" | 硬字幕模式下 `_edit_options["subtitle_label"]` 传 `"无"`（烧录只由后处理唯一负责）；或后处理检测 `finalize_report` 已烧录则跳过；加回归测试（ffmpeg 抽帧 OCR/像素差断言只有一层字幕） |
| **AUD-56-02** 默认工作流：视频VAE 用了音频VAE、图像模型为空、UNET 与任务类型不匹配 | 工作流 `:1901` 视频UNET=`minimax_h3_fl2va_pruned_int8_convrot.safetensors`、`:1903` 视频VAE=`minimax_h3_audio_vae_fp32.safetensors`、`:1904` 音频VAE=**同一文件**、`:1908` 图像模型=`""`、`:1919` 任务类型=`r2v`；`one_stop_node.py:1094` `enabled = bool(生成素材图 and 图像模型)`；INPUT_TYPES `:611` 图像模型 default=default_ckpt | 用默认工作流首次 Queue（新项目） | 视频 VAE 用 video_vae、音频 VAE 用 audio_vae；r2v 用 ref2va 权重；图像模型有可用 checkpoint，素材生成开启 | ① 视频解码槽填的是 **音频 VAE** 文件（`minimax_h3_video_vae` 未用），VAELoader 解视频潜空间会形状/语义不符 → 花屏或直接报错；② 任务=`r2v`（参考主体生视频）但 UNET 是 `fl2va`（首尾帧生视频）权重，运行时**没有任何任务↔权重族校验**（grep `ref2va/fl2va` 仅 task 文本）；③ 图像模型空 → 走 `else` 分支只写审计「素材生图：未启用（无图像模型…）」，新项目无 LOCK 素材 → r2v 参考图为空仍继续出片（`one_stop_node.py:1280` 只赋空 refs，不报错） | 工作流重新选择正确权重/VAE；INPUT_TYPES 或 run() 增加**任务↔权重族预检**（r2v→ref2va、fl2v→fl2va、t2v/i2v…），视频/音频 VAE 分槽校验文件名；图像模型为空且 `生成素材图=True` 时给出明确前置错误（或自动挑选可用 checkpoint） |

### P1

| ID | 位置 | 复现 | 期望 | 实际 | 建议 |
|---|---|---|---|---|---|
| **AUD-56-03** 「添加一段」后不填内容，点运行静默跑**默认单段剧本** | `one_stop_node.py:811-829`（run 路由）、`:815-828`（列表 JSON 解析成 `chapters_override`，空数组/空段被过滤后 `if chapters_override` 不成立，`multi` 也是空 → `_run_single(**kwargs)`，而 `剧本` 默认=`_DEFAULT_SCRIPT` 非空） | UI：勾多段→「＋ 添加一段」两次→两段都留空→点 Queue。运行时推演：`多段剧本列表JSON='["",""]'` → route=**single**，`剧本='【默认演示剧本】'`（见附录 A 输出 ②③④） | 明确报错「多段列表有 2 段但都为空」或阻止运行 | 静默忽略用户的多段意图，跑默认演示剧本；用户以为"添加一段"没生效 | run() 区分 3 态：`多段列表JSON 非空但解析后无有效段` → 抛可操作错误；`多段开关开` 也要作为状态传下来；UI 侧运行前置校验提示 |
| **AUD-56-04** 多段 >64 段**静默截断、警告永不触发** | `one_stop_node.py:2272` `chapters = list(kwargs.pop("_chapters_override", []) or [])`；`:2276` `if len(chapters) > 64: chapters = chapters[:64]`；`:2315` `if len(kwargs.get("_chapters_override") or []) > 64:` → 此时键已被 pop，**恒为 0**；UI `addMultiChapter`（`dramacore_director_ui.js:742-755`）无 64 上限 | 构造 70 段（UI 可一直点添加）→ 静态推演：生成 64 段… | 截断时 warnings + 审计写明「仅生成前 64 段，其余请拆分」；UI 在 64 段时禁用添加 | 第 65+ 段被静默丢弃，警告代码是死代码（永远不触发），用户看不出少段 | 把判断移到 `pop` 之前（`raw_override = kwargs.pop(...)` 后判断 `raw_override`）；UI 加 64 上限与提示 |
| **AUD-56-05** 断点续跑指纹漏参 → 改字幕/转场/提示词/水印/显存策略后**多段仍复用旧章节视频** | `one_stop_node.py:2287-2295` fingerprint 只含 `chapters/project/stem/segment_seconds/width/height/task/continuity/steps/seed`；`:2357` 缓存命中直接 `continue`（不重新生成、不重新烧字幕/转场） | 多段跑一次→修改「字幕=硬字幕→无」或「转场=AI自动→无」或换「系统提示词选择」→ 再跑 | 配置变化应失效缓存；至少命中时明确提示"使用了旧缓存，忽略本次 X 改动" | 用户改了默认配置（ROUND56 核心诉求）却看不到任何变化，误以为"配置没生效/默认没配好" | fingerprint 纳入所有影响成片的参数（转场/字幕/字幕台词/水印/片头片尾/系统提示词/审核阈值/显存档位…），或缓存命中时对比差异并在审计 warnings 明示 |
| **AUD-56-06** 提示词面板「保存为面板默认（下次运行生效）」**实际不生效** | UI `dramacore_director_ui.js:794,847-858`（保存写 `config/user.yaml` → `POST /panel/config {system_prompt}`）；`one_stop_node.py:1025-1032` 只有 `系统提示词选择` **以「跟随」开头**时才读面板/user.yaml；而 INPUT_TYPES `:775-778` 默认=`H3 手册（完整，推荐）`（不是跟随）；而 `系统提示词选择` 的控件只在**蒙版⑩**里（`buildEnhance`），⑭面板本身不提供该选择器 | ⑭面板点「✋手动编辑」改文本→「保存为面板默认」→ 再运行 | 保存的提示词下次运行生效（UI 文案承诺），或明确要求先切「跟随面板」并自动切换 | 保存成功弹窗，但运行仍用 H3 手册全文，用户改的提示词被忽略；且 UI 不告诉用户要切选择项 | 保存时同步把 `系统提示词选择` 置为「跟随面板配置（推荐）」（写 widget 并刷新），或改文案为"需将系统提示词选择切到跟随面板"；⑭面板加选择器 |
| **AUD-56-07** 旧工作流（ROUND49/54 保存）加载后参数**整体错位**风险 | `one_stop_node.py` INPUT_TYPES 在 required 头部新增 4 个输入（`多段剧本/多段剧本分隔符/多段剧本列表JSON`，`:593-599`），输入总数 114；ComfyUI 按 `widgets_values` **位置**映射；`tests/test_round55_workflow_alignment.py` 只守护新工作流 | 用 ROUND54 之前保存的含本节点的旧工作流加载：旧 values[1]=`项目"剧核试用"` 现在落到 `多段剧本` → `multi` 非空 → 走 `_run_multi_script`，把「剧核试用」当一章剧本生成；旧 values[2]=`输出文件名` 落到 `多段剧本分隔符`……后续全部错位 | 旧工作流要么可迁移/提示，要么新输入追加到定义末尾不改变既有位置 | 加载旧工作流后参数全错且无任何提示（静默把项目名当剧本、把输出文件名当分隔符），跑出来的东西不可预期 | 给节点加 workflow 版本标记/在 web 端 onConfigure 检测 values 长度与定义不匹配时弹告警并提示"请重新从插件工作流加载"；短期至少把 4 个新输入移到 required 末尾 |
| **AUD-56-08** 多段列表 JSON 与旧文本**双源互相覆盖**，编辑旧文本框无效 | `dramacore_director_ui.js:669-690`（`readMultiChapters` 优先 JSON，非空即返回）、`:691-706`（`syncMultiChapters` 同时写 JSON 和 `多段剧本` 文本）、`:1935-1940`（旧文本框 `bindWidget(multiTa, "多段剧本")` 仍可编辑并写入 widget）；`:1943-1945` 文案称"旧的…文本仍兼容，这里以列表为准" | 先点「＋ 添加一段」（JSON 变成 `[""]`），再在旧「多段剧本」文本框粘两章（含 `---`）→ 列表/运行都看不到这两章（JSON 优先且非空） | 只有一个真源；旧文本框要么隐藏只读，要么编辑时同步回列表 | 用户可见的两个输入框，一个有效一个被静默忽略；"添加一段"后粘文本 → 无效果（同类"添加一段无效"体验） | 旧文本框改只读/隐藏并标注"仅迁移用"；或在 legacy 文本 onchange 时解析覆盖 JSON 列表并重渲染 |
| **AUD-56-09** 高级参数**完全无 UI**（不是"没蒙上"，而是"根本没露出"），与"默认配置可查看"矛盾 | 114 输入中以下 13 项在 UI 中**无任何绑定**（grep 全文件无控件）：`素材审核阈值`、`目标总时长_分钟`、`每批段数`、`断点续跑`、`批次间卸载模型`、`显存档位`、`剪枝模型`、`tiled重叠像素`、`水印文字`、`自动返工`、`级联返工`、`源视频路径`、`出片`；`CNT3上限` 仅只读摘要（`dramacore_director_ui.js:1748-1775` 的 `buildDefaultsSummary` 只列 12 项） | 想改显存档位/断点续跑/自动返工 → 翻遍 UI 找不到；只能展开蒙版⑬→确认原生表单（且这些参数在原生表单里位置靠后） | 常用高级项应在"高级"折叠区可见可改；默认配置摘要应覆盖关键项 | 用户报"功能不全/默认配置没配好"时的直接来源：13 个参数根本没有 UI 入口；摘要只展示 12 项还宣称"默认配置（程序已配置）" | 在「⑬高级」加一个不蒙版的"高级配置"折叠面板，列出这 13 项（分组：长片/显存/返工/水印/路径）；`buildDefaultsSummary` 补 `显存档位/断点续跑/自动返工/目标总时长` |
| **AUD-56-10** 默认「硬字幕」+ 字幕台词空 + 默认剧本无引号台词 → **把视频提示词当字幕烧** | 默认工作流 `:1989` 字幕=硬字幕、`:1990` 字幕台词=`""`；默认剧本 `one_stop_node.py:38-42`（`_DEFAULT_SCRIPT` 三行叙述，无「」引号）；回退链 `one_stop_node.py:1544-1560`：字幕台词空→`_extract_dialogue_lines(剧本)` 空→用 `prompts`（视频提示词）当字幕并 warning | 全新环境用默认工作流跑一次 | 默认应能产出正常台词字幕：要么默认字幕=无，要么默认剧本带引号台词，要么默认预填字幕台词 | 首次体验字幕是 E1–E5 生成的长描述提示词（可能含英文结构行/负面词），又长又怪，必被用户骂"字幕乱七八糟" | 默认工作流与 `_DEFAULT_SCRIPT` 补「」台词（与宣传片脚本一致）；或默认 字幕=`无`（用户主动开）；回退用提示词时在 UI 顶部状态条也报警（不能只在审计） |
| **AUD-56-11** 默认 `走Finalize剪辑=True` 但**永远不会走 Finalize**（字幕/转场默认值使 advanced=True） | `director_edit.py:1037` `advanced = subtitle or watermark or fmt!="MP4" or not transition.startswith("无") or transition_plan`；默认 字幕=硬字幕→subtitle=True、转场=AI自动→transition_plan 非空 → advanced=True；`finish_movie` 中 `if use_finalize and not advanced` 才走 Finalize 节点 | 默认工作流运行后看报告 | 参数 `走Finalize剪辑=True` 应真的走 Finalize（含完整性/质量自检），或被明确改成 False/提示 | 实际走自定义 splice+encode 分支，**Finalize 的完整性/质量/占位自检不执行**；审计里只在 `finalize.executed=False` 隐含体现，UI 默认摘要仍显示"走Finalize剪辑"相关 | 明确策略：Finalize 分支支持字幕/转场传参；或当 advanced=True 时在审计/UI 明确写"因字幕/转场等高级项，跳过 Finalize 自检（改走 pipeline 拼接封装）"，并把默认值改一致 |
| **AUD-56-12** 高级的「⑭提示词面板」**没有蒙版**（仅折叠+手动编辑按钮），与"高级参数蒙版保护"不一致；同时蒙版说明写 5 处、实际 6 处 | UI `dramacore_director_ui.js:776-806`（buildPromptsPanel：只读 textarea + 「✋手动编辑（高级）」按钮直接 removeAttribute("readonly")，**无斜纹蒙版、无 confirm**）；`build()` `:2859-2863` 用普通 `collapsible` 而非 `maskedSection`；`:1864-1878` `buildMaskLegend` 文案「5 处：素材库/时间轴/采样/提示词增强/高级」；实际 `:2847-2866` 有 **6 个** `maskedSection`（assets/timeline/generation/enhance/intro_outro/advanced） | 展开⑭→点「✋手动编辑（高级）」→可直接改系统提示词并保存（无确认、无蒙版） | 系统提示词编辑属高危高级操作，应蒙版保护 + confirm；蒙版说明与实际数量一致 | 小白可无门槛改坏系统提示词/覆盖面板默认；说明文案少列 intro_outro，且"5 处"与实际 6 处不符 | ⑭纳入 `maskedSection("prompts", …)` 或手动编辑加 confirm + "改坏可恢复 H3 手册"按钮；文案改"6 处"并列出全部 |

### P2

| ID | 位置 | 问题 | 证据 / 建议 |
|---|---|---|---|
| AUD-56-13 | `one_stop_node.py::_run_single` 签名（`:831-956`） vs `INPUT_TYPES`（`:585-808`） | **14 项默认值两处漂移** | 实测：转场 `无` vs `AI自动（按剧情）`、字幕 `无` vs `硬字幕（程序对齐）`、封面帧 `无` vs `第一帧`、系统提示词选择 `跟随面板配置（推荐）` vs `H3 手册（完整，推荐）`、输出语言 `跟随` vs `中文`、分镜粒度 `跟随` vs `按镜头`、统一风格 `""` vs 电影感…、片头文字/片尾文字/片尾演职员 `""` vs 非空、整片失败策略 `按项目配置` vs `_FULL_POLICIES[0]`、任务类型 `""` vs `_default_task_type()`、图像模型 `""` vs `default_ckpt`、人脸检测器 `""` vs 首个检测器。脚本/API 直调与 UI/工作流行为不同。建议：签名默认与 INPUT_TYPES 对齐（或签名必填、只从 kwargs 取） |
| AUD-56-14 | `config/default.yaml` prompt 段；`bettercomfyui/config.py:48-67` | **config 默认漂移**：`config.py` ROUND55 新增 `h3_manual_enabled/no_subtitle` 且注释称"字段与 config/default.yaml 一一对应"，但 default.yaml `prompt:` 无这两个键（实测缺失）；`encode.transition: none`/`encode.subtitle: false` 与节点默认 `AI自动`/`硬字幕` 语义不一致 | 建议：`dump_config` 重新生成 default.yaml 并加字段漂移测试；文档区分"config 层默认（给 pipeline/其它节点）"与"节点层默认" |
| AUD-56-15 | UI `1864-1878` vs `2847-2866` | 蒙版说明 5 处 vs 实际 6 处（漏列片头片尾/字幕台词区） | 文案改 6 处并列出 intro_outro；或把 intro_outro 合并进"高级" |
| AUD-56-16 | `one_stop_node.py:585-611`（`视频UNET/CLIP名/视频VAE/音频VAE/图像模型` 用 `_filename_list(...) or [""]`） | 运行环境缺模型时 combo 退化为 `[""]`，UI 蒙版⑨里显示空选项，默认直接跑 → 首次报错但错误发生在加载模型深处；无"缺模型"预检 | 在 UI 状态条/运行前置校验里检查四个模型槽非空（当前 `refreshNextStep` 只查剧本/项目/API Key，见 `dramacore_director_ui.js` refreshNextStep） |
| AUD-56-17 | `dramacore_director_ui.js:1748-1775` | 默认摘要只 12 项且对空字符串显示"（未设置）"；未覆盖显存档位/断点续跑/自动返工/目标总时长/片头文字/水印等；但标题写"默认配置已设置（查看）" | 扩展为分组摘要（长片/显存/转场字幕/审核），并加"以上均为只读，需改点高级" |
| AUD-56-18 | `tests/test_round56_features.py` | 测试仅断言源码字符串存在（`"＋ 添加一段"`、`"⑭ 提示词"`、`"transition_decision"` 等）与少数默认值，**没有行为测试**覆盖：空段列表静默单段、>64 截断、双字幕、断点续跑指纹、提示词保存生效 | 建议新增：`test_run_blank_multi_list_errors`、`test_multi_cap_warns`、`test_hardsub_burned_once`（ffmpeg 抽帧断言单层）、`test_resume_fingerprint_includes_subtitle` |
| AUD-56-19 | `_run_multi_script`（`:2264-2400`） | 多章节时把**全局「字幕台词」**原样传给每一章 `_run_single` → 每章都烧同一份字幕（若台词按章不同则重复/错配）；每章各自片头片尾也会重复（默认无，无碍） | 多段模式建议按章拆分字幕台词（如 JSON 支持 `[{"chapter":1,"lines":[...]}]`），或至少在审计提示"多段模式字幕台词为全局共用" |

---

## 3. 逐项核对结果（对照 Lead 检查清单）

### 3.1 参数分类与蒙版覆盖（114 输入）
- 高级/危险参数共 **66 项**（采样 8、显存/缓存/长片 12、剪辑输出 14、审核/返工 10、路径/Key 8、提示词 4、增强 15、时长/段 3、生成模型 10，去重后 66）。
- **蒙版保护（M）**：素材库/时间轴/采样生成/提示词增强/片头片尾/高级 6 区，覆盖其中约 50 项（模型/采样/显存清理/缓存/转场/水印/封面/审核阈值/增强开关/片头片尾文字/字幕台词/时间轴 JSON…）。
- **仅折叠未蒙版（C）**：⑭提示词面板（系统提示词可见/可手动解锁编辑）→ AUD-56-12。
- **主界面可见但属高级**：仅 `时间轴JSON` 的辅助方法（`syncJsonFromUi/clearJson`）出现在非蒙版代码路径，但按钮本身在蒙版⑬内 → **不算暴露**；`api_key/model/api_base` 属用户必填（③区，正常）。
- **完全无 UI（13 项）** → AUD-56-09。
- **原生表单确认**：`buildAdvanced` 首次显示原生表单有 `window.confirm`（js:1788）拦截 ✅；但该按钮本身在蒙版⑬内（两层门），确认有效。

### 3.2 默认配置完整性
- `default==""` 的输入共 **15 项**：`多段剧本`、`多段剧本列表JSON`、`时间轴JSON`、`首/尾帧图片路径`、`源视频路径`、`参考视频/音频路径`、`缓存目录名`、`语义桥适配器`、`自定义系统提示词`、`字幕台词`、`api_key/model/api_base`、`输出文件名?（有默认）` 中的 15 项。
  - **可空合理**：多段剧本/列表JSON/时间轴JSON/路径/缓存目录/语义桥适配器/自定义提示词/Key（走面板）。
  - **应配置但为空**：`字幕台词`（默认字幕=硬字幕）→ AUD-56-10；`图像模型` 在工作流里为空（INPUT_TYPES 有 default_ckpt）→ AUD-56-02。
- 工作流 119 values 与定义顺序 **1:1 对齐**（实测），与静态默认值仅 5 项不同：模型 4 项（环境路径，预期）+ `control_after_generate=randomize`（预期）；其中 **视频VAE 值错误、图像模型为空** 属配置错误 → AUD-56-02。
- `config/default.yaml` 缺 ROUND55 两字段、encode 默认与节点默认不一致 → AUD-56-14。

### 3.3 「添加一段」逻辑
- `readMultiChapters/syncMultiChapters/render/add/remove/move` 静态推演：
  - ✅ 空段**保留**在 JSON（`syncMultiChapters` 不过滤，注释正确）；删除最后一段会补一个空段；上移下移交换正确。
  - ❌ 空段被 `run()` 过滤后**整列表消失**→静默单段（AUD-56-03）。
  - ❌ 64 段截断警告死代码（AUD-56-04）。
  - ❌ JSON 与 legacy 文本双源，legacy 编辑被静默忽略（AUD-56-08）。
  - ❌ 无"重复 add"去重问题（每次点击都是有意的追加），但无上限提示（与 AUD-56-04 同源）。
  - ❌ 多段模式 `字幕台词` 全局复用（AUD-56-19）。
  - ⚠️ `_run_multi_script` 单章（`len(chapters)<=1`）时把 `多段剧本` 文本当 `剧本` 传给 `_run_single`，若用户既有单段剧本又有 1 段多段文本 → 以多段为准（符合文档），但**无审计提示**（P2 建议记录）。

### 3.4 工作流对齐
- 119 == 119 ✅（`widgets_values` 与定义顺序 + 4 button + control_after_generate）。
- 仍可能错位/缺失：旧工作流兼容（AUD-56-07）、模型/VAE 默认值错误（AUD-56-02）、图像模型空（AUD-56-02）。
- `test_round55_workflow_alignment.py` 未断言 `视频VAE/音频VAE/图像模型/任务类型` 四者组合 → 建议补断言。

### 3.5 提示词可见性 / 手册
- ✅ 完整手册真实存在：`bettercomfyui/prompt/data/h3_handbook_full.txt` 69936 字节 / **41966 字**；`h3_handbook.resolve_preset("h3_handbook")` 返回全文（PREFIX+正文+SUFFIX）。
- ✅ 默认注入真实全文：`everything_node.py:533-552`（`_handbook_mode=="h3_handbook"` → `cfg.prompt.system_prompt_template = 手册全文`，审计写 handbook_chars；非要点摘要）。
- ✅ 契约可用：`routes.py:1296-1305` `GET /bettercomfyui/prompt/handbook?mode=full|core` 返回 `{ok,mode,key,chars,text}`，已注册 `/api` 前缀（`:1372`）；UI `loadHandbookPrompt` 调 `/bettercomfyui/prompt/handbook?mode=`（`apiGet` 自带 `/api`）→ 静态契约匹配。
- ✅ UI 可看/可复制/可手动编辑/可保存（`buildPromptsPanel:776-806`）；⚖️ 但「保存生效」条件缺陷 → AUD-56-06；面板未蒙版 → AUD-56-12。
- ✅ 禁字幕规则：`no_subtitle=True` 时系统提示词追加 `NO_SUBTITLE_SYSTEM_RULE`（everything:559，默认硬字幕=程序外部烧录、提示词内禁止模型生成字幕，设计一致）。

---

## 4. 通过项（审计确认无问题）

1. 119 个 `widgets_values` 与节点定义顺序 **1:1 对齐**（复算 + `test_round55_workflow_alignment.py` 8 passed 中的 2 项）。
2. ROUND56 既有测试 **8/8 通过**（`test_round56_features.py` 6 项 + `test_round55_workflow_alignment.py` 2 项，只读运行）。
3. H3 手册**全文 41966 字**真实作为系统提示词（非摘要），默认 `系统提示词选择=H3 手册（完整，推荐）` 生效。
4. `/bettercomfyui/prompt/handbook` 契约存在且返回全文/chars，UI 加载/复制/手动编辑入口齐全。
5. **AI 自动转场真实实现**：`transition_planner.decide_transitions` → `transition_plan` → `director_edit.splice_segments`（逐接缝 cuts、审计逐条原因），非空开关。
6. 硬字幕程序化时间轴模块（ROUND55）存在且默认启用：帧网格/句读切分/中英换行，符合"不靠模型/ASR"。
7. 程序化**台词提取**：`_extract_dialogue_lines` 按引号规则提取（「」/“”/""），确定性规则。
8. 多段列表 UI：空段保留、删除最后一段补空、上移/下移交换、每段独立 textarea 且同步 JSON（空段问题仅路由层，见 AUD-56-03）。
9. 蒙版体系真实存在：6 个 `maskedSection` + 斜纹蒙版 + 「✋手动选择/手动填写」按钮 + 顶部图例；解除后仅本次会话生效（已知边界）。
10. 原生表单确认拦截存在（`window.confirm`，js:1788）；隐藏只改 `computeSize`，不删参数、不改工作流值。
11. 图像模型为空时审计**如实写**「素材生图：未启用（无图像模型或 生成素材图=False）」，不静默（诚实边界保持）。
12. 默认摘要面板、状态灯/下一步、项目上下文、22 帧连续、审核结论等 ROUND49–55 UI 均在位（本轮不改 UI 结构）。

---

## 5. 十处「最可能再被用户骂」的示例

1. **字幕叠两层**：默认工作流点 Queue，成片底部同时出现"提示词长句"和"台词短句"两层文字（AUD-56-01）。
2. **首次运行就失败/花屏**：视频 VAE 用了音频 VAE 文件、r2v 却加载 fl2va 权重、图像模型为空 → 报错信息还埋在模型加载深处（AUD-56-02）。
3. **"添加一段"没反应**：加了 2 段没填内容点运行 → 视频却是默认演示剧本，用户以为按钮坏了（AUD-56-03）。
4. **改配置重跑没变化**：把字幕从硬字幕改成无、转场改掉、换提示词后再跑，多段续跑仍用旧缓存章节 → "默认配置还是没配好"（AUD-56-05）。
5. **提示词白保存**：⑭面板改完保存弹"下次生效"，下次运行仍是 H3 手册 → 用户骂"骗子按钮"（AUD-56-06）。
6. **旧工作流打开全乱**：加载 ROUND54 工作流后，项目名变成多段剧本、输出文件名变成分隔符 → 跑出来不可预期（AUD-56-07）。
7. **多段超过 64 段少片**：加了 70 章，成片只到 64 章且**没有任何提示**（AUD-56-04）。
8. **想调显存档位/自动返工找不到**：UI 翻不到、原生表单又藏在蒙版+确认后面（AUD-56-09）。
9. **字幕变成提示词**：默认剧本没引号台词 → 烧出来的"字幕"是 E1–E5 提示词（AUD-56-10）。
10. **说法与实现不一致**：蒙版图例说 5 处实际 6 处；「走Finalize剪辑=True」实际永远不走 Finalize；提示词面板高级操作没蒙版（AUD-56-11/12/15）。

---

## 6. 一句结论

ROUND56 的**方向是对的**（H3 手册全文、AI 自动转场、程序化硬字幕、多段列表、蒙版/默认摘要都已真实落地并通过现有测试），但**默认配置链路存在 2 个 P0 级自相矛盾**（硬字幕双烧、模型/任务/VAE 错配）和 **10 个 P1 级"看起来配了实际没生效/没入口"问题**，恰好都对应本轮用户的三句原话——修掉 AUD-56-01/02/03/05/06 这 5 条，用户的"添加一段无效/默认配置没配好"就能被实质解决。
