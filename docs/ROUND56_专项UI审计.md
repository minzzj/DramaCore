# ROUND56 专项 UI 审计（只读实测，Playwright + msedge）

- 目标：抓「添加一段按钮没用」「默认配置没配置好」「高级参数没蒙上」同类问题，给最小复现与证据。
- 环境：`http://127.0.0.1:8188`（运行中，未重启）；工作流 `.comfyui/user/default/workflows/【剧核】单节点_剧本到成片.json`（= `bettercomfyui_comfy/workflows/00_【单节点】剧本→成片（无外部依赖）.json`，同一文件 53392B / mtime 22:40）；UI 版本标签 **UI 2026-10-19-R56**（页面实测存在）。
- 方法：Playwright（system python + msedge headless，1680×1050）真实加载工作流、点击按钮、填 textarea、读 LiteGraph node.widgets、序列化→同页重载；**只读：未保存工作流到磁盘、未提交任何队列、未改代码/配置、未重启**。
- 证据：截图保存在 `[USER-HOME]\AppData\Local\Temp\round56_ui\`（文档内以文件名引用；原始 JSON 结果：`results2.json` / `results3.json` / `results4.json` / `results5.json` 同目录）。
- 限制：无法真跑（只读禁止提交队列），所以「运行后行为」只能以代码路径+UI→widget 写回验证；未做整页 F5（避免写 workflow 文件），按任务用「序列化→同页重载」验证持久化。

## 一、通过项清单（实测）

| # | 检查项 | 实测结果 | 证据 |
|---|---|---|---|
| 1 | UI 版本标签 | 页面含 `UI 2026-10-19-R56` | `01_workflow.png`；results2 `01_workflow` |
| 2 | 「＋ 添加一段」点击 1/2/3 次 | 列表行数 **2/3/4**（初始占位 1 行 + 每次 +1，与任务预期一致），每行独立 textarea | `11_multi_add1/2/3.png`；results2 `11_multi_add_123` |
| 3 | 填入每段不同文本 → 保存到 `多段剧本列表JSON` | 4 行 → `["第一段…","第二段…","第三段…","第四段…"]`；`多段剧本` 旧文本同步为 `---` 连接 | `52_multi_list.png`；results2 `12_multi_filled` / results4 `54` (list_index=3) |
| 4 | 序列化→同页重载后保持 | 重载 `loadGraphData(serialize())` 后仍 4 行、JSON 不丢 | `13_multi_reload.png`；results2 `13_multi_reload` |
| 5 | 删除 / 上移 / 下移 / 清空 | 删除第2行→JSON 3 项；上移/下移交换正确；清空→`[""]` 1 行 | `14_multi_ops.png`；results2 `14_multi_ops` |
| 6 | 连续快速点击「添加一段」5 次 | 行数 1→6（每次 +1，无丢事件） | results2 `15_multi_rapid` |
| 7 | 空段 / 只有 1 段 | 允许空段（运行入口会过滤空串：`one_stop_node.py:816-820`）；单段保持 1 行 | `16_multi_empty.png`；results2 `16_multi_edge`；code |
| 8 | 旧版「多段剧本+分隔符」导入 | 旧文本 3 章 → 列表 3 行；编辑后迁移写入 `多段剧本列表JSON` | `18_multi_legacy.png`；results2 `18_multi_legacy` |
| 9 | 默认配置核对（18 项） | 全部符合：转场=AI自动（按剧情）、字幕=硬字幕（程序对齐）、系统提示词选择=H3 手册（完整，推荐）、片头文字=剧核 DramaCore、片尾文字=剧终、片尾演职员=剧核 DramaCore 出品、输出语言=中文、分镜粒度=按镜头、统一风格已填、素材最多=12、CNT2=3、CNT3=3、每段=10s、1920×1088、审核阈值=70、tiled=0/重叠64、走Finalize=True、使用占位=False | results2 `03_defaults`（唯一"差异"是期望文案写成 H3 手册（完整），实际为（完整，推荐），语义一致） |
| 10 | 空值分类 | 22 个空值均属「确实应为空/自动项」：api_key/model/api_base（可选 Key/自配）、图像模型/视频LoRA（本机无模型；环境相关，见 P2-5）、路径类（首/尾帧/源视频/参考音视频）、缓存目录名、语义桥适配器、自定义系统提示词、时间轴JSON、多段剧本(列表)默认空、字幕台词（有自动提取回退，见 P1-4） | results2 `04_empty` |
| 11 | 工作流 values 与按名回读一致性 | raw=**119**，native widgets=**119**（node.widgets=120，多 1 个 `dc_director_ui` DOM widget；119 中含 4 个分组标题按钮），序列化后逐名比对 **0 mismatch**；`多段剧本列表JSON` 位于 index=3 且值正确 | results2 `02_widget_mapping` / results4 `54_serialize_consistency` |
| 12 | 高级区蒙版存在性 | 初始 **7 个 `.dcdu-mask` / 7 wrap / 7 解锁按钮**；⑥⑦⑨⑩⑫⑬ 面板都在活动蒙版内；鼠标点击蒙版区域 `elementFromPoint` 命中 `.dcdu-mask*`（不能直接点到输入） | results2 `20_masks_initial`/`21_mask_sections`；`20_sec_*.png` |
| 13 | 解锁按钮有效 | 点「✋ 手动选择/手动填写」后 mask 7→6，对应输入可见、可编辑 | `22_unlock.png`；results2 `22_unlock_mask` |
| 14 | 原生参数表单确认弹窗 | 存在，文案正确：「原生参数表单包含大量高级参数（采样/显存/缓存/路径等），普通用户请不要修改。…确认要显示原生参数表单吗？」 | results5 `60`；`23_native.png`/`30_native_after.png` |
| 15 | 片头片尾按钮写值 | 点「片头+片尾」→ widget `片头片尾` 由「无」变「片头+片尾」；⑫ 面板片头文字/片尾文字/演职员/字幕台词值与 widget 一致 | results5 `63`/`66`；`52_multi_list.png`、`20_sec_⑫ 片头片尾.png` |
| 16 | 「填入示例剧本」 | 点击后 `剧本` 52→315 字符，内容为示例剧本 | results5 `64` |

> **对 ROUND55 结论的修正（重要）**：ROUND55-A 曾报 P0「工作流 118/119 values ≠ 112 控件，参数错位」。本轮实测：`119 = 119 native widgets`，按名回读 **0 mismatch**；与 schema 114 的差异来自 **4 个分组标题按钮（素材/视频/输出/审核 ▾）+ 1 个 DOM widget（dc_director_ui）**，不是参数错位。该项撤销。

## 二、问题清单（P0/P1/P2）

| ID | 严重度 | 问题 | 最小复现步骤 | 期望 vs 实际 | 证据 | 建议修复位置 |
|---|---|---|---|---|---|---|
| **UI-P0-1** | **P0** | **版本回滚「查询版本」在默认配置下必失败（按钮没用 + 文案误导）** | 加载工作流 → 展开 ⑫ → 保持「项目」默认值 `剧核试用` → 点「查询版本」 | 期望：列出该项目素材版本，或提示选择项目；实际：`查询失败：500 PathIsolationError: project_id 必须是 32 位小写 UUID4 hex（无连字符） (project_id="'剧核试用'")`。UI placeholder 写「项目名或 project_id」，但后端只认 32 位 id | results5 `65_rollback_click`：notes 最后一条即 500；`99_final3.png` | 前端 `web/js/dramacore_director_ui.js:2779-2789`（把 `readVal(项目)` 显示名直接当 project_id）；后端 `routes.py:1236-1248`（未解析显示名，直接传 `list_asset_versions`）；`bettercomfyui/store/rollback.py:117-121`（要求 UUID）。修复：route 内用 `ProjectStore.resolve_reference` 解析，或 UI 先 `/bettercomfyui/projects` 查到 id；文案改为「项目 id」，错误返回 4xx 友好中文 |
| **UI-P1-1** | P1 | **蒙版可被键盘/Tab 绕过**：蒙版只挡鼠标，输入仍在 DOM 可聚焦，focus 后改值直接写入节点参数 | 展开 ⑥素材库（有蒙版）→ 用 Tab/JS focus 到「素材最多生成个数」number 输入 → 改成 7 | 期望：蒙版覆盖期间不可编辑；实际：`focused=true`，widget `素材最多生成个数` 12→**7**，蒙版仍在 | results5 `61_mask_keyboard_bypass`；`20_sec_⑥ 时间轴.png`、`36_mask_edit.png` | `web/js/dramacore_director_ui.js:2355-2382`（maskedSection）：蒙版存在时对 body 内控件设 `disabled`/`inert`/`tabindex=-1`，解除时恢复；当前仅覆盖 pointer 事件 |
| **UI-P1-2** | P1 | **「显示/隐藏原生参数表单」按钮确认后仍无效（按钮没用）**：确认弹窗出现并同意，但原生 widgets 仍全部收起（computeSize=[0,-4]），节点高度 5270→5262 几乎不变，画布上看不到任何原生参数 | 展开 ⑬ → 点「显示/隐藏原生参数表单（高级，普通用户勿动）」→ 确认 → 观察节点/滚动 | 期望：显示全部原生参数（按钮文案「一键恢复」）；实际：无任何原生控件出现（上/下滚动亦然）；右侧 ComfyUI「参数」面板本来就能看到原生值，按钮承诺与实际不一致 | results5 `60_native_toggle`（sample 全部 `cs:[0,-4]`, `restored:false`, size 5270→5262）；`51_canvas_native.png`、`60_native_scrolled.png`、`23_native.png` | `web/js/dramacore_director_ui.js:652-666`（`hideNativeWidgets` 用替换 computeSize，`__dcduOrigComputeSize` 恢复链失效；且 DOM widget 覆盖画布区域）；`:1786-1801`（按钮逻辑）。建议改用 LiteGraph `collapsed`/`hidden` 布局，或删除该按钮改为提示“在右侧参数面板查看”（避免假承诺） |
| **UI-P1-3** | P1 | **多段剧本超过 64 段：UI 无上限提示，后端静默截断（丢用户输入）** | 勾选多段 → 连续添加 72 行（脚本连点）→ 观察计数；后端运行会只取前 64 章 | 期望：UI 达 64 段时提示/禁止，或运行后明确告警；实际：UI 显示「已添加 71 段」无任何提示；`one_stop_node.py:2276-2277` `chapters=chapters[:64]` 静默截断；`:2315` 的 `_chapters_override` 告警因 `:2272 kwargs.pop()` 永远取不到，**告警永不触发** | results3 `38_multi_over64_ui`（rows=71/72、json_len=71/72、无 dialog、无 64 提示）；`17_multi_over64.png`；code `one_stop_node.py:2272-2277,2315-2316` | UI `js:742-754`（addMultiChapter 加 64 上限与提示）；`one_stop_node.py:811-828`（解析后检查 >64 并写 warnings 或直接报错）；`_run_multi_script:2315` 改用局部 `chapters_raw_len` |
| **UI-P1-4** | P1 | **默认「硬字幕」+ 空字幕台词 + 默认样例剧本无引号台词 → 首跑硬字幕文本回退成视频提示词**（仅审计 warning，UI 被蒙版隐藏，用户看不到） | 加载工作流不改默认值 → 检查：字幕=硬字幕（程序对齐）、`字幕台词=""`、默认剧本无引号；运行时 `one_stop_node.py:1544-1559`：先字幕台词（空）→剧本引号提取（无）→**回退视频提示词**并 warning | 期望：默认要么字幕=无，要么样例剧本带引号台词/默认台词填充，且 UI 明确显示硬字幕文本来源；实际：默认首跑字幕=提示词长文本（语义错误），只在 audit 里有一行 warning | results3 `37_round55_ui_values`（字幕=硬字幕、字幕台词=""）、`39_default_script`（默认剧本无引号）；code `one_stop_node.py:1544-1559` | 工作流默认值（`config`/workflow）：字幕默认改「无」或样例剧本写入引号台词；`one_stop_node.py:1555-1559` 回退时同时在 UI 顶层状态/首屏提示；UI ⑫ 显示“硬字幕文本来源” |
| **UI-P1-5** | P1 | **⑭「提示词」高级区没有蒙版**：系统提示词 42263 字符 H3 手册默认可见但只读；点「✋ 手动编辑（高级）」直接移除 readonly，无确认弹窗、无蒙版，与 ⑨⑩⑫⑬ 的保护级别不一致 | 展开 ⑭ → 点「手动编辑（高级）」→ 直接进入可编辑状态 | 期望：任务要求 ⑭ 也属高级内容，解除前应不可编辑（蒙版/确认）；实际：无蒙版、无确认，一键可改，且旁边还有「保存为面板默认」（写 config/user.yaml） | results3 `34`/`35_textareas_ro`（ro=true len=42263 为 ⑭ 系统提示词）、results5 `62`（该面板 masked=false）；UI code `js:789-793`；`20_sec_⑭ 提示词.png` | `js:789-793`：加 `window.confirm` 或包进 `maskedSection("prompts", ...)`；「保存为面板默认」需二次确认并显示写入路径 |

### P2（非阻断、建议改进）

| ID | 问题 | 证据/位置 |
|---|---|---|
| UI-P2-1 | **ComfyUI 右侧「参数」面板始终展示并可直接编辑全部原生 widgets（含所有高级参数）**，绕过画布蒙版设计；用户可从右侧改 `多段剧本列表JSON`/转场/CNT 等，无确认 | `52_multi_list.png` 右侧「输入/多段剧本/多段剧本列表JSON」可直接编辑（框架原生行为）；无对应隐藏机制 |
| UI-P2-2 | 页面 4 个 404 资源（`/api/userdata/user.css`、`/user.css`、`/api/userdata?dir=subgraphs…`、`/api/userdata/comfy.templates.json`）→ 控制台 error（不影响本节点功能，但污染错误收集） | results3 `32_404` |
| UI-P2-3 | **⑤ 22 帧段间连续区没有蒙版**：`连续模式` select 与 `重绘幅度` number 可直接改（该区不在 maskedSection 列表） | results5 `62_panels_mask_class`：⑤ masked=false、editable_inputs=2；`52_multi_list.png` |
| UI-P2-4 | 页面底部有一个**浮动预览窗（渐变图）**常驻，来源不明（疑似 ComfyUI 预览/历史状态残留），可能让用户困惑 | `52_multi_list.png`、`01_workflow.png` 右下角 |
| UI-P2-5 | 默认工作流把**环境相关空值固化**：`图像模型=""`、`视频LoRA=""`（CPU 机器无模型时生成的工作流）；换到有模型的机器打开工作流仍为空 → 素材图默认不生成（需手动选）；另 `片头片尾="无"`（文字已配置但默认不插卡，符合按需按钮设计） | results2 `04_empty`；results3 `37`；`01_workflow.png` |
| UI-P2-6 | 文案不精确：⑫ 写「片头/片尾由 Finalize 在成片首尾拼接」，实际是 one_stop 在 Finalize 之后的后处理拼接（`one_stop_node.py:1517-1556`），行为一致但表述易误解 | `20_sec_⑫ 片头片尾.png`；code |

## 三、检查项逐条对照（任务要求 vs 实测）

| 任务要求 | 结论 |
|---|---|
| 点「＋ 添加一段」1/2/3 次 → 2/3/4 行、每行独立 textarea | ✅ 通过（实测 2/3/4 行，独立 textarea） |
| 删除、上移、下移、清空真的生效 | ✅ 通过（JSON 与行数同步正确） |
| 填不同文本并保存到 `多段剧本列表JSON`（读 widget 验证） | ✅ 通过（4 段不同文本写入，index=3） |
| 刷新/重新加载后还在（序列化→重载同页） | ✅ 通过（重载后 4 行/JSON 保持；未做 F5 落盘） |
| 边界：连续快速点击 | ✅ 通过（5 连点 +5 行） |
| 边界：空段、只有 1 段 | ✅ 通过（允许空段，运行端过滤；单段 1 行） |
| 边界：超过 64 段提示 | ❌ **未实现**（UI 无提示、后端静默截断 → UI-P1-3） |
| 旧版「多段剧本+分隔符」导入兼容 | ✅ 通过（3 行导入 + 编辑迁移 JSON） |
| 默认值 18 项核对 | ✅ 全部符合（见通过项 #9） |
| 找出空且"看起来没配置"项并分类 | ✅ 22 项均为可选/自动/环境相关；唯一组合风险=硬字幕台词（UI-P1-4） |
| 工作流 values 与按名回读一致（新增参数/片头片尾/字幕台词/多段列表/系统提示词） | ✅ 119=119 native，0 mismatch；**撤销 ROUND55 P0 错位结论** |
| `.dcdu-mask` 数量 | 实测 **7**（6 自动生成区 + 项目上下文区；见结果） |
| 逐段展开 ⑨⑩⑫⑬ 确认高级内容解除蒙版前不可编辑 | ✅ 鼠标层面：蒙版命中测试通过；但**键盘/Tab 可绕过**（UI-P1-1），⑭ 无蒙版（UI-P1-5） |
| 原生参数表单显示前有确认弹窗 | ✅ 弹窗存在且文案正确；但确认后原生表单**没有显示**（UI-P1-2） |
| 找出高级参数没有蒙版/可直接改的漏洞 | 找到：⑭（无蒙版一键解锁）、⑤（无蒙版）、右侧参数面板（全部）、键盘绕过（已列为问题） |

## 四、最值得修的 10 条（按价值排序）

1. **UI-P0-1 回滚查询默认 500**（新功能默认路径不可用；按钮无用感最强）。
2. **UI-P1-2 原生参数表单按钮确认后无效**（明确承诺“一键恢复”却看不到任何原生参数）。
3. **UI-P1-1 蒙版键盘绕过**（高级参数保护形同虚设，Tab 可改）。
4. **UI-P1-3 >64 段无提示 + 后端静默截断**（用户输入被丢且无任何提示；改 `js:742-754` + `one_stop_node.py:2272-2315`）。
5. **UI-P1-4 默认硬字幕文本回退成视频提示词**（默认配置组合导致首跑字幕语义错误，只在 audit 有 warning）。
6. **UI-P1-5 ⑭ 提示词无蒙版、一键解锁无确认**（与 ⑨⑩⑫⑬ 保护级别不一致，且能“保存为面板默认”写配置）。
7. **UI-P2-1 右侧「参数」面板绕过所有蒙版**（ComfyUI 原生行为；至少文档/文案上不要宣称“普通用户无法修改所有高级参数”）。
8. **UI-P2-5 工作流把环境相关空值（图像模型/LoRA）固化**（换机器默认不生成素材图；建议首次打开时若本机有模型则提示选择）。
9. **UI-P2-2 404 资源**（清控制台错误，便于真实错误暴露）。
10. **UI-P2-3/P2-4 ⑤ 22帧参数无蒙版 + 浮动预览窗**（保护一致性/界面整洁）。

## 五、复现脚本

- 审计脚本（只读，不提交队列）：`C:\Users\ADMINI~1\AppData\Local\Temp\round56_ui_audit.py`、`...audit2.py`、`...audit3.py`、`...audit4.py`、`...audit5.py`；结果 JSON：同目录 `results2/3/4/5.json`；截图同目录 `*.png`（本文引用文件名）。
- 复现 P0（回滚 500）：加载工作流 → ⑫ → 保持项目=剧核试用 → 点「查询版本」→ 观察 `.dcdu-note` 文本。
- 复现 P1-1：展开 ⑥ → `Tab` 或 JS `input.focus()` 到素材最多生成个数 → 输入新值 → 读 `node.widgets` 值已变。
- 复现 P1-2：展开 ⑬ → 点原生参数表单按钮 → 接受确认 → 观察画布无原生控件、节点高度几乎不变。
- 复现 P1-3：多段列表连续点击 70+ 次（或 JS 连点）→ 计数「已添加 71 段」→ 无任何上限提示；运行入口只取前 64 章（code）。
- 复现 P1-4：默认值检查（字幕=硬字幕、字幕台词空、剧本无引号）→ code 路径回退视频提示词。

*（本轮只读；未修改源码/配置、未重启服务、未提交队列；未做整页 F5 落盘验证。）*
