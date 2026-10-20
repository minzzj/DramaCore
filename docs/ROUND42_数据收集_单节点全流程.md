# ROUND42 数据收集：单节点（剧本→成片）全流程证据

> 自动生成于 **2026-10-18 16:39:22**；来源=本地/远端真实证据文件；缺失字段一律写「未采集到」，无任何估算。
>
> 生成器：`scripts/collect_onestop_evidence.py`（一键重跑命令见文末）。

## 零、关键发现（自动摘要，均来自证据文件）

- 共收集 **18** 条运行记录：local_quality 1 条、node_review 13 条、ui_test 1 条、ui_test_real_key 1 条、doc_run 2 条。
- 最新一次运行 **run16**（2026-10-18 16:31:49）：总耗时 285.5s（节点内 280.48s）、出片 262.23s、编码 未采集到s、成片 1917354 B、节点审核结论 通过。
- 素材环节 **run04**：素材生图+审核 13.8s，候选生成 未采集到 张 / 复用已 LOCK 未采集到 张，LOCK 0 / 未 LOCK 3，挂载参考图 0 张；结论：部分/全部未 LOCK（LOCK 0，未 LOCK 3，共 3）
- 素材环节 **run05**：素材生图+审核 37.49s，候选生成 9 张 / 复用已 LOCK 0 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 9，复用 0）
  - run05 候选审核明细：BLOCKED 6 / FALLBACK 3 / LOCKED 0；FALLBACK 候选被回退选中并 LOCK（见「六、匹配依据」候选清单）
- 素材环节 **run06**：素材生图+审核 37.61s，候选生成 9 张 / 复用已 LOCK 0 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 9，复用 0）
- 素材环节 **run07**：素材生图+审核 2.39s，候选生成 0 张 / 复用已 LOCK 3 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 0，复用 3）
- 素材环节 **run08**：素材生图+审核 1.78s，候选生成 0 张 / 复用已 LOCK 3 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 0，复用 3）
- 素材环节 **run09**：素材生图+审核 1.91s，候选生成 0 张 / 复用已 LOCK 3 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 0，复用 3）
- 素材环节 **run11**：素材生图+审核 38.12s，候选生成 9 张 / 复用已 LOCK 0 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 9，复用 0）
  - run11 候选审核明细：BLOCKED 6 / FALLBACK 3 / LOCKED 0；FALLBACK 候选被回退选中并 LOCK（见「六、匹配依据」候选清单）
- 素材环节 **run13**：素材生图+审核 38.63s，候选生成 9 张 / 复用已 LOCK 0 张，LOCK 3 / 未 LOCK 0，挂载参考图 3 张；结论：LOCK 3 / 未LOCK 0（生成 9，复用 0）
  - run13 候选审核明细：BLOCKED 6 / FALLBACK 3 / LOCKED 0；FALLBACK 候选被回退选中并 LOCK（见「六、匹配依据」候选清单）
- ⚠️ **run04 视频审核异常**（段级/整片审核因此未产出）：`'tuple' object has no attribute 'get'`
- 排查提示（人工诊断，非证据文件内容）：`BetterComfyUIVideoReview.run()`（`bettercomfyui_comfy/review_nodes.py:454`）返回 5 元组（结论/段JSON/整片JSON/需重生成/审计），而单节点 `_video_review()` 按 ComfyUI 常规 dict 结构取 `result.get('result')` → `'tuple' object has no attribute 'get'`；属节点侧兼容问题，修复后重跑本管线即可补齐段级/整片审核分数。
- **UI 真实点击测试（run09）**：只填剧本→点运行→status=success，浏览器记录开始时间 2026-10-18 06:51:47，耗时 160.5s、控件 95 个、节点 1 个、缺失节点 0 个、截图 4 张、节点内总耗时 153.51s、成片 未采集到 B、R_full 92.08、视频审核 需返工。
- **UI 真实点击测试（run13）**：只填剧本→点运行→status=success，浏览器记录开始时间 2026-10-18 07:55:24，耗时 200.4s、控件 95 个、节点 1 个、缺失节点 0 个、截图 2 张、节点内总耗时 197.14s、成片 1258662 B、R_full 98.06、视频审核 通过。
- **多段连续性（run10）**：提示词段数 3、导演台 2 段/248 帧、连续性 ON=是 mode=guide overlap=22f pin=[2]、Finalize 剪辑 1 段、段级 R_seg [段1:95.12]、整片 R_full 97.2、显存峰值 28768 MB、视频审核 通过。
- ⚠️ run10 时长不足：计划 10.33s，实际生成 5.71s（导演台返回 248 帧，Finalize 1 段）→ 完整性 是；出片帧数 vs 计划帧数见「一·F」。
- **多段连续性（run12）**：提示词段数 3、导演台 2 段/248 帧、连续性 ON=是 mode=guide overlap=22f pin=[2]、Finalize 剪辑 2 段、段级 R_seg [段1:78.34]、整片 R_full 96.39、显存峰值 28000 MB、视频审核 通过。
- ⚠️ run12 时长不足：计划 10.33s，实际生成 5.75s（导演台返回 248 帧，Finalize 2 段）→ 完整性 否；出片帧数 vs 计划帧数见「一·F」。
- **多段连续性（run14）**：提示词段数 3、导演台 2 段/248 帧、连续性 ON=是 mode=guide overlap=22f pin=None、Finalize 剪辑 未采集到 段、段级 R_seg [段1:83.82]、整片 R_full 94.56、显存峰值 29600 MB、视频审核 通过。
- ⚠️ run14 时长不足：计划 10.33s，实际生成 5.25s（导演台返回 248 帧，Finalize 未采集到 段）→ 完整性 否；出片帧数 vs 计划帧数见「一·F」。
- **多段连续性（run15）**：提示词段数 3、导演台 2 段/248 帧、连续性 ON=是 mode=guide overlap=22f pin=[2]、Finalize 剪辑 未采集到 段、段级 R_seg [段1:84.2]、整片 R_full 96.01、显存峰值 29600 MB、视频审核 通过。
- ⚠️ run15 时长不足：计划 10.33s，实际生成 5.25s（导演台返回 248 帧，Finalize 未采集到 段）→ 完整性 否；出片帧数 vs 计划帧数见「一·F」。
- **多段连续性（run16）**：提示词段数 3、导演台 2 段/248 帧、连续性 ON=是 mode=guide overlap=22f pin=[2]、Finalize 剪辑 未采集到 段、段级 R_seg [段1:94.49；段2:87.38]、整片 R_full 95.26、显存峰值 29600 MB、视频审核 通过。
- **真实 Key 全链（run11）**：提示词模式=真实模型（一次调用）、API 调用 1 次、API配置=是；提示词 5.81s、素材候选 9 张→LOCK 3/挂图 3、出片 113.02s、R_seg 78.44、R_full 97.22、显存 46176 MB、视频审核 通过、完整性 是。
- **真实 Key + UI 点击（run13）**：status=success、浏览器耗时 200.4s、控件 95、缺失节点 0、截图 2 张；API 1 次、素材候选 9→LOCK 3/挂图 3、R_seg 89.77、R_full 98.06、显存 44864 MB、视频审核 通过。
- **ROUND46 功能真机（run14）**：格式=MOV/CRF=未采集到、转场=淡入淡出、字幕=自动字幕、水印=文字水印、封面=第一帧；长片分批 1 批、断点续跑=「未采集到」、自动返工 2 轮；连续性 ON=是/22f pin=None；实际 126 帧/5.25s vs 计划 10.33s（Finalize 未采集到 段）、R_full 94.56、显存 29600 MB、完整性 否。
- **ROUND46 功能真机（run15）**：格式=MOV/CRF=18、转场=淡入淡出、字幕=自动字幕、水印=文字水印、封面=第一帧；长片分批 1 批、断点续跑=「参数指纹变化 → 旧检查点作废，全部批次重跑」、自动返工 2 轮；连续性 ON=是/22f pin=[2]；实际 126 帧/5.25s vs 计划 10.33s（Finalize 未采集到 段）、R_full 96.01、显存 29600 MB、完整性 否。
- **ROUND46 功能真机（修复后 P0-4）（run16）**：格式=MOV/CRF=18、转场=淡入淡出、字幕=自动字幕、水印=文字水印、封面=第一帧；长片分批 1 批、断点续跑=「参数指纹变化 → 旧检查点作废，全部批次重跑」、自动返工 2 轮；连续性 ON=是/22f pin=[2]；实际 250 帧/10.42s vs 计划 10.33s（Finalize 未采集到 段）、R_full 95.26、显存 29600 MB、完整性 是。
- ✅ **P0-4 2 段帧数缺口已修复（run16，2026-10-18 16:31:49）**：修复后实际 250 帧/10.42s（ffmpeg 实测 864×480/24fps/音轨=有/1917354 B≈1.92MB；计划 248 帧/2×5.167s）；完整性=True、审核结论=通过；段级 R_seg 94.493/87.377 双段通过、R_full 95.26 通过；连续性 ON/guide/22f/pin=[2]；显存 29600 MB。修复原因：导演台 `exportMode="segments"` 只回部分帧 → `exportMode="all"`。
- **2 段帧数缺口（历史问题，P0-4 已修复）**：run10 实际 137/248 帧（缺 111）；run12 实际 138/248 帧（缺 110）；run14 实际 126/248 帧（缺 122）；run15 实际 126/248 帧（缺 122）；修复后 250 帧/10.417s（见「一·F」修复前后对比）。

## 一、运行总览

| 运行 | 证据等级 | 时间 | 总耗时(s) | 节点耗时(s) | 出片(s) | 段数(计划/实产) | 分辨率/fps/时长 | 成片大小 | 审核结论 |
|---|---|---|---|---|---|---|---|---|---|
| run01 | local_quality | 2026-10-18 11:15:44 | 未采集到 | 未采集到 | 未采集到 | 未采集到/未采集到 | 864x480 @ 24fps / 5.17s | 1229036 B | 通过 / 视频审核=未采集到 |
| run02 | node_review | 2026-10-18 11:17:19 | 119.2 | 未采集到 | 93.83 | 3/1 | 864x480 @ 24fps / 5.17s | 851727 B | 通过 / 视频审核=未采集到 |
| run03 | node_review | 2026-10-18 11:31:10 | 未采集到 | 未采集到 | 92.53 | 未采集到/1 | 864x480 @ 24fps / 5.17s | 未采集到 B | 通过 / 视频审核=未采集到 |
| run04 | node_review | 2026-10-18 11:40:10 | 137.27 | 131.46 | 94.8 | 3/1 | 864x480 @ 24fps / 5.17s | 775815 B | 通过 / 视频审核=审核失败：AttributeError: 'tuple' object has no attribute 'get' |
| run05 | node_review | 2026-10-18 13:16:08 | 180.19 | 177.04 | 112.81 | 3/1 | 864x480 @ 24fps / 5.18s | 2400029 B | 通过 / 视频审核=需返工 |
| run06 | node_review | 2026-10-18 14:23:17 | 未采集到 | 未采集到 | 123.57 | 3/1 | 864x480 @ 24fps / 5.89s | 未采集到 B | 不通过 / 视频审核=审核失败：_NodeError: 第 2 段评分失败：帧序列形状不合法（需要 [N>=2,H,W,3]），实际 (1, 88, 160, 3) |
| run07 | node_review | 2026-10-18 14:42:42 | 未采集到 | 未采集到 | 128.4 | 3/1 | 864x480 @ 24fps / 5.89s | 未采集到 B | 不通过 / 视频审核=审核失败：_NodeError: 第 2 段评分失败：帧序列形状不合法（需要 [N>=2,H,W,3]），实际 (1, 88, 160, 3) |
| run08 | node_review | 2026-10-18 14:50:09 | 未采集到 | 未采集到 | 127.36 | 3/1 | 864x480 @ 24fps / 5.89s | 未采集到 B | 不通过 / 视频审核=需返工 |
| run09 | ui_test | 2026-10-18 14:54:12 | 160.5 | 153.51 | 124.92 | 3/1 | 864x480 @ 24fps / 5.89s | 未采集到 B | 通过 / 视频审核=需返工 |
| run10 | node_review | 2026-10-18 15:03:32 | 294.2 | 287.67 | 274.02 | 3/2 | 864x480 @ 24fps / 5.7s | 未采集到 B | 通过 / 视频审核=通过 |
| run11 | node_review | 2026-10-18 15:49:58 | 185.3 | 182.7 | 113.02 | 4/1 | 864x480 @ 24fps / 5.18s | 1562905 B | 通过 / 视频审核=通过 |
| run12 | node_review | 2026-10-18 15:55:17 | 275.8 | 271.92 | 257.39 | 3/2 | 864x480 @ 24fps / 5.73s | 1226162 B | 不通过 / 视频审核=通过 |
| run13 | ui_test_real_key | 2026-10-18 15:58:34 | 200.4 | 197.14 | 124.34 | 5/1 | 864x480 @ 24fps / 5.89s | 1258662 B | 通过 / 视频审核=通过 |
| run14 | node_review | 2026-10-18 16:09:55 | 未采集到 | 未采集到 | 276.97 | 3/2 | 864x480 @ 24fps / 5.25s | 未采集到 B | 不通过 / 视频审核=通过 |
| run15 | node_review | 2026-10-18 16:15:44 | 286 | 279.2 | 265.61 | 3/2 | 864x480 @ 24fps / 5.25s | 未采集到 B | 不通过 / 视频审核=通过 |
| run16 | node_review | 2026-10-18 16:31:49 | 285.5 | 280.48 | 262.23 | 3/2 | 864x480 @ 24fps / 10.42s | 1917354 B | 通过 / 视频审核=通过 |
| run17 | doc_run | 未采集到 | 131.65 | 未采集到 | 105.9 | 未采集到/未采集到 | 未采集到x未采集到 @ 未采集到fps / 未采集到s | 未采集到 B | 未采集到 / 视频审核=未采集到 |
| run18 | doc_run | 未采集到 | 119.31 | 未采集到 | 94.2 | 未采集到/未采集到 | 未采集到x未采集到 @ 未采集到fps / 未采集到s | 未采集到 B | 通过 / 视频审核=未采集到 |

## 一·B、UI 真实点击测试（浏览器只填剧本→点运行）

| 运行 | 状态 | 记录开始时间 | 耗时(s) | 控件数 | 节点数 | 缺失节点 | 只填剧本 | 点击方式 | 截图 | 节点内总耗时(s) | R_full | 视频审核 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| run09 | success | 2026-10-18 06:51:47 | 160.5 | 95 | 1 | 0 | 是（52 字） | button:#queue-button | 4 张 | 153.51 | 92.08 | 需返工 |

- 工作流文件：`/model/h3-persistent/user/default/workflows/【剧核】单节点_剧本到成片.json`；节点类型：['BetterComfyUIOneStop']；prompt_id：`01a5f334-e7af-41f2-aafb-9f18f9a34218`；截图：['01_opened.png', '02_workflow_loaded.png', '04_finished.png', '06_workflow_generated.png']
- 注：`记录开始时间` 取自 UI 测试脚本自身记录（未标注时区，可能为远端 UTC）；运行轮次的 `时间` 列以证据文件 mtime（本机时间）为准。
- 真实 Key 的 UI 点击测试另见「一·D」；两轮均为浏览器真实点击（非 API 直调）。

## 一·C、真实 Key 全链（真实模型一次 API → 素材生图审核 → 出片 → 审核）

| 运行 | 提示词模式 | API调用 | API配置 | 节点内总耗时(s) | 提示词(s) | 素材候选/复用/LOCK/挂图 | 出片(s) | 编码(s) | 段级R_seg | 整片R_full | 显存峰值(MB) | 视频审核 | 完整性 | 审核结论 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| run11 | 真实模型（一次调用） | 1 | 是 | 182.7 | 5.81 | 9/0/3/3 | 113.02 | 1.93 | 段1:78.44(过) | 97.22 | 46176 | 通过 | 是 | 通过 |

- 项目：`747651ba63164d5db87c167bfd5a6fe6`；证据：`/data/BetterComfyUI/projects/747651ba63164d5db87c167bfd5a6fe6/review/one_stop_1792309798.json`；提示词段数（节点实际段）1、导演台 1 段；素材候选明细见「六、匹配依据」。
- 真实 Key 判定依据（来自审计文本）：`提示词模式：真实模型（一次调用）` + `API 调用次数：1 次` + `API：已配置`；提示词耗时 5.81s 含真实 API 往返。
- 成片大小 1562905 B 来自本地证据副本 `docs\remote_evidence_4090\real_chain\真实全链成片_Key.mp4`（远端 output 未拉取时用本地副本）。

## 一·D、真实 Key + 真实 UI 点击（浏览器只填剧本→点运行→真实模型）

| 运行 | 状态 | 耗时(s) | 控件/节点/缺失 | 只填剧本 | 截图 | API调用 | 素材候选/复用/LOCK/挂图 | 出片(s) | 段级R_seg | 整片R_full | 显存峰值(MB) | 视频审核 | 完整性 | 结论 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| run13 | success | 200.4 | 95/1/0 | 是（52 字） | 2 张 | 1 | 9/0/3/3 | 124.34 | 段1:89.77(过) | 98.06 | 44864 | 通过 | 是 | 通过 |

- 工作流：`/model/h3-persistent/user/default/workflows/【剧核】单节点_剧本到成片.json`；prompt_id：`c538ab79-3bc5-41a4-9068-1848f84d63d6`；截图：['04_finished_real_key.png', '07_新导演台UI_远端.png']；节点内总耗时 197.14s。
- 真实 Key 判定：审计文本 `提示词模式：真实模型（一次调用）` + `API 调用次数：1 次`；UI 侧仍为浏览器只填剧本→点 `#queue-button` 的真实点击。

## 一·E、ROUND46 功能真机（转场/字幕/水印/封面/MOV、长片分批、断点续跑、自动返工、22f 连续）

| 运行 | 阶段 | 输出格式 | CRF | 转场 | 字幕 | 水印 | 封面 | 长片分批 | 断点续跑 | 自动返工 | 连续性 | 2段帧数(实际/计划)/Fina段数 | 段级R_seg | 整片R_full | 显存峰值(MB) | 完整性 | 结论 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| run14 | 修复前 | MOV | 未采集到 | 淡入淡出 | 自动字幕 | 文字水印 | 第一帧 | 1 批 / 未采集到 段 | 未采集到 | 2 轮 | ON=是/22f pin=None | 126 帧/5.25s vs 计划 10.33s（Finalize 未采集到 段） | 段1:83.82(过) | 94.56 | 29600 | 否 | 不通过 |
| run15 | 修复前 | MOV | 18 | 淡入淡出 | 自动字幕 | 文字水印 | 第一帧 | 1 批 / 2 段 | 参数指纹变化 → 旧检查点作废，全部批次重跑 | 2 轮 | ON=是/22f pin=[2] | 126 帧/5.25s vs 计划 10.33s（Finalize 未采集到 段） | 段1:84.2(过) | 96.01 | 29600 | 否 | 不通过 |
| run16 | 修复后（P0-4） | MOV | 18 | 淡入淡出 | 自动字幕 | 文字水印 | 第一帧 | 1 批 / 2 段 | 参数指纹变化 → 旧检查点作废，全部批次重跑 | 2 轮 | ON=是/22f pin=[2] | 250 帧/10.42s vs 计划 10.33s（Finalize 未采集到 段） | 段1:94.49(过)；段2:87.38(过) | 95.26 | 29600 | 是 | 通过 |

- 封面：`/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/output/剧核ROUND46验证/2026-10-18/9a1e778dfac04c76abf753e8/剧核ROUND46验证_9a1e778dfac04c76abf753e8_2026-10-18_vround46.cover.jpg`
- 成片输出：`/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/output/剧核ROUND46验证/2026-10-18/9a1e778dfac04c76abf753e8/剧核ROUND46验证_9a1e778dfac04c76abf753e8_2026-10-18_vround46.mov`
- 元数据：`/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/output/剧核ROUND46验证/2026-10-18/9a1e778dfac04c76abf753e8/剧核ROUND46验证_9a1e778dfac04c76abf753e8_2026-10-18_vround46.metadata.json`；分段清单：`/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/review/segment_manifest_1792312309.json`；剪辑收尾 steps=4
- 自动返工轮次明细：`[{"attempt": 1, "need": [], "verdict": "通过"}, {"attempt": 2, "need": [], "verdict": "通过", "final_verify": true}]`
- 长片分批结果：`[{"batch": 1, "segment_range": "段1-2", "seconds": 262.11}]`

## 一·F、2 段帧数缺口（P0-4 修复证据：修复前后对比）

| 阶段 | 运行 | 证据 | 导演台计划帧 | 实际出片帧数 | 实际时长(s) | Finalize段数 | 完整性 | 结论 |
|---|---|---|---|---|---|---|---|---|
| 修复前 | run10 | v1 | 248 | 137 | 5.71 | 1 | 是 | 通过 |
| 修复前 | run12 | v2 | 248 | 138 | 5.75 | 2 | 否 | 不通过 |
| 修复前 | run14 | ROUND46旧 | 248 | 126 | 5.25 | 未采集到 | 否 | 不通过 |
| 修复前 | run15 | ROUND46旧 | 248 | 126 | 5.25 | 未采集到 | 否 | 不通过 |
| **修复后（P0-4）** | run16 | round46_test_fixed.json + 本地 mov | 248 | 250 | 10.42 | 未采集到 | 是 | 通过 |

- **修复前后对比**：修复前 v1=137 帧/5.71s（完整性判定通过但帧数不足 111 帧）、v2=138 帧/5.75s、旧 ROUND46=126 帧/5.25s（后两者完整性不通过）→ **修复后 = 250 帧 / 10.417s（ffmpeg 实测）**，约为计划 248 帧（2×5.167s），完整性=True、审核结论=通过。
- 修复版实测规格（ffmpeg 抽检本地 `docs\remote_evidence_4090\round46\二段连续_转场字幕水印_10.4s.mov`）：250 帧 / 10.42s / 864×480 / 24fps / 音轨=有 / 1917354 B（≈1.92MB）/ 编码 h264。
- 修复版审核：段级 R_seg 双段通过（94.493 / 87.377）、R_full 95.26（阈值 70，通过）、连续性 ON/guide/22f/pin=[2]、显存峰值 29600 MB；SG6 MOV/淡入淡出/自动字幕/文字水印/封面均生效（见「一·E」）。
- **修复原因**：导演台 `exportMode="segments"` 只回部分帧 → 改为 `exportMode="all"`（P0-4 修复说明，Lead 提供；修复版节点 timeline 的 `output.exportMode` 作为佐证）。
- **结论**：2 段帧数缺口（历史问题）**已修复并关闭**；修复前 126/138 帧记录保留在表中作为对比证据。

## 一·G、多段连续性（导演台连续段间 + 段级/整片审核）

| 运行 | 提示词段数 | 导演台段数 | 连续性ON | 模式/重叠 | pin段 | 导演台返回帧数 | Finalize段数 | 段级R_seg | 整片R_full | 显存峰值(MB) | 视频审核 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| run10 | 3 | 2 | 是 | guide/22f | [2] | 248 | 1 | 段1:95.12(过) | 97.2（叙事:100、音画:93.52、节奏:95.29、跳变:100） | 28768 | 通过 |
| run12 | 3 | 2 | 是 | guide/22f | [2] | 248 | 2 | 段1:78.34(过) | 96.39（叙事:100、音画:95.31、节奏:90.27、跳变:100） | 28000 | 通过 |
| run14 | 3 | 2 | 是 | guide/22f | 未采集到 | 248 | 未采集到 | 段1:83.82(过) | 94.56（叙事:100、音画:100、节奏:78.24、跳变:100） | 29600 | 通过 |
| run15 | 3 | 2 | 是 | guide/22f | [2] | 248 | 未采集到 | 段1:84.2(过) | 96.01（叙事:100、音画:100、节奏:84.06、跳变:100） | 29600 | 通过 |
| run16 | 3 | 2 | 是 | guide/22f | [2] | 248 | 未采集到 | 段1:94.49(过)；段2:87.38(过) | 95.26（叙事:100、音画:100、节奏:81.05、跳变:100） | 29600 | 通过 |

- 说明：run05、run06、run07、run08、run09、run11、run13 为单段运行（导演台 1 段、连续性 OFF），不列入本表；其导演台字段与段级/整片审核仍见总览与「三、审核分数」。

## 二、分项耗时与素材（全流程新增字段）

| 运行 | 提示词(s) | 素材生图+审核(s) | 候选生成/复用/LOCK/未LOCK | 素材审核结论 | 挂参考图 | 加载模型(s) | 编码(s) | 显存峰值(MB) |
|---|---|---|---|---|---|---|---|---|
| run01 | 未采集到 | 未采集到 | 未采集到/未采集到/未采集到/未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |
| run02 | 未采集到 | 未采集到 | 未采集到/未采集到/未采集到/未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |
| run03 | 未采集到 | 未采集到 | 未采集到/未采集到/未采集到/未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |
| run04 | 0.12 | 13.8 | 3/未采集到/0/3 | 部分/全部未 LOCK（LOCK 0，未 LOCK 3，共 3） | 0 | 2.65 | 1.47 | 未采集到 |
| run05 | 0.14 | 37.49 | 9/0/3/0 | LOCK 3 / 未LOCK 0（生成 9，复用 0） | 3 | 2.6 | 1.97 | 46112 |
| run06 | 0.13 | 37.61 | 9/0/3/0 | LOCK 3 / 未LOCK 0（生成 9，复用 0） | 3 | 2.79 | 2.15 | 45088 |
| run07 | 0.19 | 2.39 | 0/3/3/0 | LOCK 3 / 未LOCK 0（生成 0，复用 3） | 3 | 2.12 | 2.47 | 44640 |
| run08 | 0.2 | 1.78 | 0/3/3/0 | LOCK 3 / 未LOCK 0（生成 0，复用 3） | 3 | 2.3 | 2.21 | 44640 |
| run09 | 0.14 | 1.91 | 0/3/3/0 | LOCK 3 / 未LOCK 0（生成 0，复用 3） | 3 | 2.15 | 2.3 | 44640 |
| run10 | 0.11 | 未采集到 | 0/0/0/0 | 素材生图未启用（0 生成 / 0 LOCK / 0 挂图） | 0 | 2.68 | 2.02 | 28768 |
| run11 | 5.81 | 38.12 | 9/0/3/0 | LOCK 3 / 未LOCK 0（生成 9，复用 0） | 3 | 2.57 | 1.93 | 46176 |
| run12 | 0.11 | 未采集到 | 0/0/0/0 | 素材生图未启用（0 生成 / 0 LOCK / 0 挂图） | 0 | 2.23 | 2.5 | 28000 |
| run13 | 9.11 | 38.63 | 9/0/3/0 | LOCK 3 / 未LOCK 0（生成 9，复用 0） | 3 | 2.57 | 2.07 | 44864 |
| run14 | 0.12 | 未采集到 | 0/0/0/0 | 素材生图未启用（0 生成 / 0 LOCK / 0 挂图） | 0 | 未采集到 | 未采集到 | 29600 |
| run15 | 0.07 | 未采集到 | 0/0/0/0 | 素材生图未启用（0 生成 / 0 LOCK / 0 挂图） | 0 | 未采集到 | 未采集到 | 29600 |
| run16 | 0.13 | 未采集到 | 0/0/0/0 | 素材生图未启用（0 生成 / 0 LOCK / 0 挂图） | 0 | 未采集到 | 未采集到 | 29600 |
| run17 | 未采集到 | 未采集到 | 未采集到/未采集到/未采集到/未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |
| run18 | 未采集到 | 未采集到 | 未采集到/未采集到/未采集到/未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |

## 三、审核分数

| 运行 | 完整性 | 段级审核分数 | 叙事 | 音画 | 节奏 | 跳变 | 整片均分 | 阈值 | 整片R_full | 结论 | 视频审核 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| run01 | 未采集到 | 未采集到 | 100 | 100 | 90.14 | 100 | 97.53 | 未采集到 | 未执行/未采集到 | 通过 | 未采集到 |
| run02 | 是 | 未采集到 | 100 | 94.82 | 97.01 | 100 | 97.96 | 70 | 未执行/未采集到 | 通过 | 未采集到 |
| run03 | 是 | 未采集到 | 100 | 94.82 | 86.07 | 100 | 95.22 | 70 | 未执行/未采集到 | 通过 | 未采集到 |
| run04 | 是 | 未采集到 | 100 | 94.82 | 98.34 | 100 | 98.29 | 70 | 未执行/未采集到 | 通过 | 审核失败：AttributeError: 'tuple' object has no attribute 'get' |
| run05 | 是 | 段1:74.83；段2:0 | 100 | 94.82 | 88.1 | 100 | 95.73 | 70 | 叙事:100、音画:94.82、节奏:88.1、跳变:100（final 95.73） | 通过 | 需返工 |
| run06 | 否 | 未采集到 | 100 | 97.15 | 91.5 | 100 | 97.16 | 70 | 未执行/未采集到 | 不通过 | 审核失败：_NodeError: 第 2 段评分失败：帧序列形状不合法（需要 [N>=2,H,W,3]），实际 (1, 88, 160, 3) |
| run07 | 否 | 未采集到 | 100 | 97.15 | 71.15 | 100 | 92.08 | 70 | 未执行/未采集到 | 不通过 | 审核失败：_NodeError: 第 2 段评分失败：帧序列形状不合法（需要 [N>=2,H,W,3]），实际 (1, 88, 160, 3) |
| run08 | 否 | 段1:66.76 | 100 | 97.15 | 71.15 | 100 | 92.08 | 70 | 叙事:100、音画:97.15、节奏:71.15、跳变:100（final 92.08） | 不通过 | 需返工 |
| run09 | 是 | 段1:66.76 | 100 | 97.15 | 71.15 | 100 | 92.08 | 70 | 叙事:100、音画:97.15、节奏:71.15、跳变:100（final 92.08） | 通过 | 需返工 |
| run10 | 是 | 段1:95.12 | 100 | 93.52 | 95.29 | 100 | 97.2 | 70 | 叙事:100、音画:93.52、节奏:95.29、跳变:100（final 97.2） | 通过 | 通过 |
| run11 | 是 | 段1:78.44 | 100 | 94.82 | 94.04 | 100 | 97.22 | 70 | 叙事:100、音画:94.82、节奏:94.04、跳变:100（final 97.22） | 通过 | 通过 |
| run12 | 否 | 段1:78.34 | 100 | 95.31 | 90.27 | 100 | 96.39 | 70 | 叙事:100、音画:95.31、节奏:90.27、跳变:100（final 96.39） | 不通过 | 通过 |
| run13 | 是 | 段1:89.77 | 100 | 97.15 | 95.09 | 100 | 98.06 | 70 | 叙事:100、音画:97.15、节奏:95.09、跳变:100（final 98.06） | 通过 | 通过 |
| run14 | 否 | 段1:83.82 | 100 | 100 | 78.24 | 100 | 94.56 | 70 | 叙事:100、音画:100、节奏:78.24、跳变:100（final 94.56） | 不通过 | 通过 |
| run15 | 否 | 段1:84.2 | 100 | 100 | 84.06 | 100 | 96.01 | 70 | 叙事:100、音画:100、节奏:84.06、跳变:100（final 96.01） | 不通过 | 通过 |
| run16 | 是 | 段1:94.49；段2:87.38 | 100 | 100 | 81.05 | 100 | 95.26 | 70 | 叙事:100、音画:100、节奏:81.05、跳变:100（final 95.26） | 通过 | 通过 |
| run17 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未执行/未采集到 | 未采集到 | 未采集到 |
| run18 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未执行/未采集到 | 通过 | 未采集到 |

## 四、字段覆盖情况

共 **18** 条运行记录；下表统计每个字段在多少条记录中采集到：

| 字段 | 已采集/总数 | 状态 |
|---|---|---|
| 运行时间 | 16/18 | ⚠️ 部分 |
| 总耗时(s) | 12/18 | ⚠️ 部分 |
| 节点内总耗时(s) | 9/18 | ⚠️ 部分 |
| 提示词生成耗时(s) | 13/18 | ⚠️ 部分 |
| 素材生图+审核耗时(s) | 8/18 | ⚠️ 部分 |
| 加载视频模型耗时(s) | 10/18 | ⚠️ 部分 |
| 出片耗时(s) | 17/18 | ⚠️ 部分 |
| 编码耗时(s) | 10/18 | ⚠️ 部分 |
| 提示词段数(计划) | 14/18 | ⚠️ 部分 |
| 实产段数 | 15/18 | ⚠️ 部分 |
| 总帧数 | 15/18 | ⚠️ 部分 |
| 成片时长(s) | 16/18 | ⚠️ 部分 |
| 帧率 | 16/18 | ⚠️ 部分 |
| 宽 | 16/18 | ⚠️ 部分 |
| 高 | 16/18 | ⚠️ 部分 |
| 音轨 | 18/18 | ✅ 已采集 |
| 素材生成数 | 13/18 | ⚠️ 部分 |
| 素材候选生成数 | 12/18 | ⚠️ 部分 |
| 复用已LOCK素材数 | 12/18 | ⚠️ 部分 |
| 素材审核 LOCK 数 | 13/18 | ⚠️ 部分 |
| 素材未 LOCK 数 | 13/18 | ⚠️ 部分 |
| 素材审核结论 | 13/18 | ⚠️ 部分 |
| 挂载参考图数 | 13/18 | ⚠️ 部分 |
| 完整性 passed | 15/18 | ⚠️ 部分 |
| 整片四维分数 | 16/18 | ⚠️ 部分 |
| 整片均分 | 16/18 | ⚠️ 部分 |
| 审核阈值 | 15/18 | ⚠️ 部分 |
| 段级审核分数 | 12/18 | ⚠️ 部分 |
| 整片 R_full 分数 | 10/18 | ⚠️ 部分 |
| 审核结论 | 17/18 | ⚠️ 部分 |
| 显存峰值(MB) | 12/18 | ⚠️ 部分 |
| 成片大小(bytes) | 8/18 | ⚠️ 部分 |
| 码率(bps) | 2/18 | ⚠️ 部分 |
| 导演台导出模式 | 12/18 | ⚠️ 部分 |
| 成片实测时长(s) | 2/18 | ⚠️ 部分 |
| 成片实测帧数 | 2/18 | ⚠️ 部分 |
| 视频审核结论 | 13/18 | ⚠️ 部分 |
| 需重生成段 | 10/18 | ⚠️ 部分 |
| UI真实点击成功 | 2/18 | ⚠️ 部分 |
| UI真实点击耗时(s) | 2/18 | ⚠️ 部分 |
| UI控件数 | 2/18 | ⚠️ 部分 |
| UI缺失节点数 | 2/18 | ⚠️ 部分 |
| UI截图数 | 2/18 | ⚠️ 部分 |
| 段间连续性 ON | 12/18 | ⚠️ 部分 |
| 连续性模式 | 12/18 | ⚠️ 部分 |
| 连续性重叠帧 | 5/18 | ⚠️ 部分 |
| 导演台段数 | 12/18 | ⚠️ 部分 |
| 导演台返回帧数 | 12/18 | ⚠️ 部分 |
| Finalize剪辑段数 | 9/18 | ⚠️ 部分 |
| 计划时长(s) | 6/18 | ⚠️ 部分 |
| 实际生成时长(s) | 6/18 | ⚠️ 部分 |
| API调用次数 | 14/18 | ⚠️ 部分 |
| API已配置 | 14/18 | ⚠️ 部分 |
| 真实Key/真实模型 | 14/18 | ⚠️ 部分 |
| 剪辑输出格式 | 3/18 | ⚠️ 部分 |
| 转场 | 3/18 | ⚠️ 部分 |
| 字幕 | 3/18 | ⚠️ 部分 |
| 水印 | 3/18 | ⚠️ 部分 |
| 封面 | 3/18 | ⚠️ 部分 |
| 长片批次数 | 3/18 | ⚠️ 部分 |
| 自动返工尝试次数 | 3/18 | ⚠️ 部分 |
| 导演台合并帧数 | 1/18 | ⚠️ 部分 |

## 五、证据清单

### 本地
- **运行报告 JSON**：`remote_reports\continuity_test.json`、`remote_reports\one_stop_full_test.json`、`remote_reports\one_stop_full_test_v3.json`、`remote_reports\one_stop_test.json`、`docs\remote_evidence_4090\real_chain\real_chain_test.json`、`docs\remote_evidence_4090\real_key_ui\continuity_test_v2.json`、`docs\remote_evidence_4090\round46\round46_test.json`、`docs\remote_evidence_4090\round46\round46_test_fixed.json`
- **节点内审核 JSON**：`docs\remote_evidence_4090\real_chain\one_stop_审核_真实Key.json`、`remote_reports\remote_pulled\5eb547463acd495b9de10e3f551c825e\one_stop_1792294810.json`、`remote_reports\remote_pulled\6d0e8befee6c4670987ed9359d115f7a\one_stop_1792307012.json`、`remote_reports\remote_pulled\6d0e8befee6c4670987ed9359d115f7a\one_stop_1792310117.json`、`remote_reports\remote_pulled\9a1e778dfac04c76abf753e87b9a32e7\one_stop_1792310995.json`、`remote_reports\remote_pulled\9a1e778dfac04c76abf753e87b9a32e7\one_stop_1792311344.json`、`remote_reports\remote_pulled\9a1e778dfac04c76abf753e87b9a32e7\one_stop_1792312309.json`、`remote_reports\remote_pulled\c9439a259ebe4a8899fa34fca1f5fae9\one_stop_1792293439.json`、`remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792294270.json`、`remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792304597.json`、`remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792305762.json`、`remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792306209.json`、`remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792306452.json`、`remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792310314.json`、`remote_reports\remote_pulled\e49b5197d8994793a94dd8531611c052\one_stop_1792300568.json`
- **本地复核 quality JSON/MD**：`docs\remote_evidence_4090\one_stop_quality_第一次.json`、`docs\remote_evidence_4090\one_stop_quality_第二次.json`
- **UI 真实点击测试 JSON**：`docs\remote_evidence_4090\real_key_ui\ui_test_result_real_key.json`、`docs\remote_evidence_4090\ui_test\ui_test_result.json`
- **文档级运行记录（ROUND41 表）**：`docs\ROUND41_单节点首版真机验证.md`
- **显存 CSV**：未采集到
- **本地成片副本（真实 Key / UI / ROUND46 证据目录）**：`docs\remote_evidence_4090\real_chain\真实全链成片_Key.mp4（1562905 B）`、`docs\remote_evidence_4090\round46\二段连续_转场字幕水印_10.4s.mov（1917354 B）`
- **跳过/异常文件**：
  - `docs\remote_evidence_4090\one_stop_review_第一次.json` — 空文件或结构无法识别（质量/审核 JSON）
  - `docs\remote_evidence_4090\real_chain\真实提示词.json` — 新证据目录内无法识别的 JSON

### 远端（只读扫描）
- 拉取尝试时间：2026-10-18 16:37:52；缓存目录：`remote_reports\remote_pulled`
- 远端 review/one_stop_*.json：15 个
  - `/data/BetterComfyUI/projects/c9439a259ebe4a8899fa34fca1f5fae9/review/one_stop_1792293439.json`（2195 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/review/one_stop_1792294270.json`（2191 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/review/one_stop_1792304597.json`（23966 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/review/one_stop_1792305762.json`（16496 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/review/one_stop_1792306209.json`（19722 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/review/one_stop_1792306452.json`（19817 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/review/one_stop_1792310314.json`（28072 B）
  - `/data/BetterComfyUI/projects/5eb547463acd495b9de10e3f551c825e/review/one_stop_1792294810.json`（3851 B）
  - `/data/BetterComfyUI/projects/e49b5197d8994793a94dd8531611c052/review/one_stop_1792300568.json`（27727 B）
  - `/data/BetterComfyUI/projects/6d0e8befee6c4670987ed9359d115f7a/review/one_stop_1792307012.json`（20109 B）
  - `/data/BetterComfyUI/projects/6d0e8befee6c4670987ed9359d115f7a/review/one_stop_1792310117.json`（20467 B）
  - `/data/BetterComfyUI/projects/747651ba63164d5db87c167bfd5a6fe6/review/one_stop_1792309798.json`（28563 B）
  - `/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/review/one_stop_1792310995.json`（31380 B）
  - `/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/review/one_stop_1792311344.json`（31374 B）
  - `/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/review/one_stop_1792312309.json`（31693 B）
- 远端 output/*（mp4/mov）：8 个
  - `/data/BetterComfyUI/projects/c9439a259ebe4a8899fa34fca1f5fae9/output/大节点成片.mp4`（851727 B）
  - `/data/BetterComfyUI/projects/804a41d71dfc4ff98a9a488e4ec634cc/output/remote_stitched.mp4`（16131515 B）
  - `/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/output/单节点成片.mp4`（1258662 B）
  - `/data/BetterComfyUI/projects/5eb547463acd495b9de10e3f551c825e/output/全流程单节点成片.mp4`（775815 B）
  - `/data/BetterComfyUI/projects/e49b5197d8994793a94dd8531611c052/output/全流程V3成片.mp4`（2400029 B）
  - `/data/BetterComfyUI/projects/6d0e8befee6c4670987ed9359d115f7a/output/多段连续验证.mp4`（1226162 B）
  - `/data/BetterComfyUI/projects/747651ba63164d5db87c167bfd5a6fe6/output/真实全链成片.mp4`（1562905 B）
  - `/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/output/剧核ROUND46验证/2026-10-18/9a1e778dfac04c76abf753e8/剧核ROUND46验证_9a1e778dfac04c76abf753e8_2026-10-18_vround46.mov`（1917354 B）

## 六、每条运行的匹配依据与冲突记录

### run01（local_quality）
- 本地复核报告 docs\remote_evidence_4090\one_stop_quality_第一次.json；无对应远端/节点证据，独立列出
- 本地复核：独立本地复核记录
### run02（node_review）
- 节点内审核报告 remote_reports\remote_pulled\c9439a259ebe4a8899fa34fca1f5fae9\one_stop_1792293439.json
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/c9439a259ebe4a8899fa34fca1f5fae9/output/大节点成片.mp4（851727 B（未下载视频））
- 运行报告 remote_reports\one_stop_test.json 按 project_id=c9439a25 匹配
- 本地复核：按成片大小 851727 bytes 与远端项目 c9439a25 唯一匹配
- 文档原文：**运行 3（路径修复后）** | **119.20s，出片 93.8s** → 音轨=有，审核通过
### run03（node_review）
- 节点内审核报告 remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792294270.json
### run04（node_review）
- 节点内审核报告 remote_reports\remote_pulled\5eb547463acd495b9de10e3f551c825e\one_stop_1792294810.json
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/5eb547463acd495b9de10e3f551c825e/output/全流程单节点成片.mp4（775815 B（未下载视频））
- 运行报告 remote_reports\one_stop_full_test.json 按 project_id=5eb54746 匹配
- ⚠️ 冲突 `asset_review_verdict`：保留 部分/全部未 LOCK（LOCK 0，未 LOCK 3，共 3），另有 全部未 LOCK（3 张被 BLOCKED）（来自 remote_reports\one_stop_full_test.json）
- ⚠️ 视频审核错误（来自审计文本，如实记录）：'tuple' object has no attribute 'get'
- 素材候选 3 张：主角/v1(4.9s)、配角/v1(3.2s)、主场景/v1(3.3s)
- ⚠️ 审计告警：⚠️ 视频审核失败（如实记录，不冒充通过）：AttributeError: 'tuple' object has no attribute 'get'
### run05（node_review）
- 节点内审核报告 remote_reports\remote_pulled\e49b5197d8994793a94dd8531611c052\one_stop_1792300568.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/e49b5197d8994793a94dd8531611c052/output/全流程V3成片.mp4（2400029 B（未下载视频））
- 运行报告 remote_reports\one_stop_full_test_v3.json 按 project_id=e49b5197 匹配
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 74.832, 'passed': True, 'dimensions': {'画面连贯': 68.921, '角色一致': 41.711, '动作合理': 88.698, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}, {'segment_index': 2, 'score': 0.0, 'passed': False, 'dimensions': {}, 'duration_s': 0.02, 'too_short': True}]，另有 [{'segment_index': 1, 'score': 74.832, 'passed': True, 'dimensions': {}}, {'segment_index': 2, 'score': 0.0, 'passed': False, 'dimensions': {}}]（来自 remote_reports\one_stop_full_test_v3.json）
- 段级 R_seg：段1:74.83(过)；段2:0(未过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 94.822, '节奏合理': 88.096, '无明显跳变': 100.0}，final=95.73，passed=是，policy=未采集到
- 连续性：导演台 1 段 / 124 帧，ON=否，mode=None，overlap=未采集到f，pin=[]，Finalize 1 段，计划 未采集到s / 实际 未采集到s，显存 46112 MB
- 审计原文：段间连续实证：ON=False mode=None overlap=Nonef keep_tail=None pin段=[]
- 素材候选 9 张：主角/v1(4.7s)、主角/v2(3.5s)、主角/v3(4s)、配角/v1(3.2s)、配角/v2(3.7s)、配角/v3(4.2s)、主场景/v1(3.1s)、主场景/v2(3.7s)、主场景/v3(4.1s)
- 候选审核：BLOCKED 58.89、BLOCKED 61.2、FALLBACK 61.2、BLOCKED 62.03、BLOCKED 55.23、FALLBACK 62.03、BLOCKED 55.81、BLOCKED 57.08、FALLBACK 58.68
- ⚠️ 审计告警：⚠️ 边界 1→2 取帧失败：ffmpeg 抽帧失败（seg_0002.mp4）：Output file #0 does not contain any stream
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 1 timing: cond=10.7s sample=87.5s decode=12.6s cache=skipped cleanup=skipped
### run06（node_review）
- 节点内审核报告 remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792304597.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- ⚠️ 视频审核错误（来自审计文本，如实记录）：第 2 段评分失败：帧序列形状不合法（需要 [N>=2,H,W,3]），实际 (1, 88, 160, 3)
- 连续性：导演台 1 段 / 141 帧，ON=否，mode=None，overlap=未采集到f，pin=None，Finalize 1 段，计划 未采集到s / 实际 未采集到s，显存 45088 MB
### run07（node_review）
- 节点内审核报告 remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792305762.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- ⚠️ 视频审核错误（来自审计文本，如实记录）：第 2 段评分失败：帧序列形状不合法（需要 [N>=2,H,W,3]），实际 (1, 88, 160, 3)
- 连续性：导演台 1 段 / 141 帧，ON=否，mode=None，overlap=未采集到f，pin=None，Finalize 1 段，计划 未采集到s / 实际 未采集到s，显存 44640 MB
### run08（node_review）
- 节点内审核报告 remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792306209.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 段级 R_seg：段1:66.76(未过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 97.151, '节奏合理': 71.15, '无明显跳变': 100.0}，final=92.08，passed=未采集到，policy=未采集到
- 连续性：导演台 1 段 / 141 帧，ON=否，mode=None，overlap=未采集到f，pin=None，Finalize 1 段，计划 未采集到s / 实际 未采集到s，显存 44640 MB
### run09（ui_test）
- 节点内审核报告 remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792306452.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- UI 点击测试 docs\remote_evidence_4090\ui_test\ui_test_result.json 按 project_id=d2e42257 + 时间相近（≤900s）与节点审核合并
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 66.757, 'passed': False, 'dimensions': {'画面连贯': 92.716, '角色一致': 9.596, '动作合理': 64.718, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 66.757, 'passed': False, 'dimensions': {}}]（来自 docs\remote_evidence_4090\ui_test\ui_test_result.json）
- 段级 R_seg：段1:66.76(未过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 97.151, '节奏合理': 71.15, '无明显跳变': 100.0}，final=92.08，passed=是，policy=未采集到
- UI 步骤：读取工作流文件(06:51:47) → 浏览器打开 ComfyUI 并初始化完成(06:51:52) → 载入工作流(06:51:54) → 只填剧本(06:51:54) → 点击运行(06:51:54) → 队列确认(06:51:54) → 运行结束(06:54:35)
- UI 结论：status=success，elapsed=160.5s，控件 95，节点 1，缺失节点 []，只填剧本 ok=是，点击 ok=是，prompt_id=01a5f334-e7af-41f2-aafb-9f18f9a34218
- 连续性：导演台 1 段 / 141 帧，ON=否，mode=None，overlap=未采集到f，pin=[]，Finalize 1 段，计划 5.2s / 实际 5.88s，显存 44640 MB
- 审计原文：段间连续实证：ON=False mode=None overlap=Nonef keep_tail=None pin段=[]
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 1 timing: cond=9.9s sample=97.4s decode=15.1s cache=skipped cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.69s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 未读到 API 密钥：本次自动回退占位模式（**没有调用真实模型**）。请在右侧「Better ComfyUI」面板配置 API 密钥后重跑。
### run10（node_review）
- 节点内审核报告 remote_reports\remote_pulled\6d0e8befee6c4670987ed9359d115f7a\one_stop_1792307012.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 运行报告 remote_reports\continuity_test.json 按 project_id=6d0e8bef 匹配
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 95.123, 'passed': True, 'dimensions': {'画面连贯': 93.122, '角色一致': 99.443, '动作合理': 87.925, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 95.123, 'passed': True, 'dimensions': {}}]（来自 remote_reports\continuity_test.json）
- 段级 R_seg：段1:95.12(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 93.522, '节奏合理': 95.294, '无明显跳变': 100.0}，final=97.2，passed=是，policy=未采集到
- 连续性：导演台 2 段 / 248 帧，ON=是，mode=guide，overlap=22f，pin=[2]，Finalize 1 段，计划 10.33s / 实际 5.71s，显存 28768 MB
- 审计原文：段间连续实证：ON=True mode=guide overlap=22f keep_tail=True pin段=[2]
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 2 timing: cond=26.8s sample=102.5s decode=15.5s cache=0.9s cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.53s）已忽略：不参与段级评分与边界验证
### run11（node_review）
- 节点内审核报告 docs\remote_evidence_4090\real_chain\one_stop_审核_真实Key.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/747651ba63164d5db87c167bfd5a6fe6/output/真实全链成片.mp4（1562905 B（未下载视频））
- 运行报告 docs\remote_evidence_4090\real_chain\real_chain_test.json 按 project_id=747651ba 匹配
- 成片大小来自本地证据副本 docs\remote_evidence_4090\real_chain\真实全链成片_Key.mp4（1562905 B，mtime 2026-10-18 15:57:28；按文件名匹配）
- 成片实测（ffmpeg 抽检 docs\remote_evidence_4090\real_chain\真实全链成片_Key.mp4）：124 帧 / 5.167s / 864x480 / 24.0fps / 音轨=有 / 1562905 B
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 78.442, 'passed': True, 'dimensions': {'画面连贯': 60.228, '角色一致': 61.72, '动作合理': 91.818, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 78.442, 'passed': True, 'dimensions': {}}]（来自 docs\remote_evidence_4090\real_chain\real_chain_test.json）
- 段级 R_seg：段1:78.44(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 94.822, '节奏合理': 94.038, '无明显跳变': 100.0}，final=97.22，passed=是，policy=未采集到
- 连续性：导演台 1 段 / 124 帧，ON=否，mode=None，overlap=未采集到f，pin=[]，Finalize 1 段，计划 未采集到s / 实际 未采集到s，显存 46176 MB
- 审计原文：段间连续实证：ON=False mode=None overlap=Nonef keep_tail=None pin段=[]
- 素材候选 9 张：林秀/v1(5s)、林秀/v2(3.8s)、林秀/v3(4.3s)、老陈/v1(3.2s)、老陈/v2(3.8s)、老陈/v3(4.2s)、雪夜客栈内景/v1(3.1s)、雪夜客栈内景/v2(3.6s)、雪夜客栈内景/v3(4s)
- 候选审核：BLOCKED 61.61、BLOCKED 60.29、FALLBACK 61.61、BLOCKED 56.39、BLOCKED 56.68、FALLBACK 61.96、BLOCKED 68.67、BLOCKED 65.08、FALLBACK 69.29
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 1 timing: cond=10.3s sample=87.9s decode=12.7s cache=skipped cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.02s）已忽略：不参与段级评分与边界验证
### run12（node_review）
- 节点内审核报告 remote_reports\remote_pulled\6d0e8befee6c4670987ed9359d115f7a\one_stop_1792310117.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/6d0e8befee6c4670987ed9359d115f7a/output/多段连续验证.mp4（1226162 B（未下载视频））
- 运行报告 docs\remote_evidence_4090\real_key_ui\continuity_test_v2.json 按 project_id=6d0e8bef 匹配
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 78.343, 'passed': True, 'dimensions': {'画面连贯': 55.236, '角色一致': 66.293, '动作合理': 91.843, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 78.343, 'passed': True, 'dimensions': {}}]（来自 docs\remote_evidence_4090\real_key_ui\continuity_test_v2.json）
- 段级 R_seg：段1:78.34(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 95.313, '节奏合理': 90.266, '无明显跳变': 100.0}，final=96.39，passed=是，policy=未采集到
- 连续性：导演台 2 段 / 248 帧，ON=是，mode=guide，overlap=22f，pin=[2]，Finalize 2 段，计划 10.33s / 实际 5.75s，显存 28000 MB
- 审计原文：段间连续实证：ON=True mode=guide overlap=22f keep_tail=True pin段=[2]
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 2 timing: cond=12.4s sample=102.9s decode=15.3s cache=0.9s cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.56s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 时长不足：计划 10.33s，实际 5.73s（<70%） → 完整性不通过
### run13（ui_test_real_key）
- 节点内审核报告 remote_reports\remote_pulled\d2e4225733cc496e88246e8d0ccbbb2f\one_stop_1792310314.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/d2e4225733cc496e88246e8d0ccbbb2f/output/单节点成片.mp4（1258662 B（未下载视频））
- UI 点击测试 docs\remote_evidence_4090\real_key_ui\ui_test_result_real_key.json 按 project_id=d2e42257 + 时间相近（≤900s）与节点审核合并
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 89.765, 'passed': True, 'dimensions': {'画面连贯': 70.539, '角色一致': 95.249, '动作合理': 93.273, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 89.765, 'passed': True, 'dimensions': {}}]（来自 docs\remote_evidence_4090\real_key_ui\ui_test_result_real_key.json）
- 段级 R_seg：段1:89.77(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 97.151, '节奏合理': 95.095, '无明显跳变': 100.0}，final=98.06，passed=是，policy=未采集到
- UI 步骤：读取工作流文件(07:55:24) → 浏览器打开 ComfyUI 并初始化完成(07:55:28) → 载入工作流(07:55:30) → 只填剧本(07:55:30) → 点击运行(07:55:30) → 队列确认(07:55:31) → 运行结束(07:58:51)
- UI 结论：status=success，elapsed=200.4s，控件 95，节点 1，缺失节点 []，只填剧本 ok=是，点击 ok=是，prompt_id=c538ab79-3bc5-41a4-9068-1848f84d63d6
- 连续性：导演台 1 段 / 141 帧，ON=否，mode=None，overlap=未采集到f，pin=[]，Finalize 1 段，计划 5.2s / 实际 5.88s，显存 44864 MB
- 审计原文：段间连续实证：ON=False mode=None overlap=Nonef keep_tail=None pin段=[]
- 素材候选 9 张：林秀/v1(4.9s)、林秀/v2(3.9s)、林秀/v3(4.4s)、老陈/v1(3.4s)、老陈/v2(3.8s)、老陈/v3(4.3s)、客栈/v1(3.2s)、客栈/v2(3.7s)、客栈/v3(4.2s)
- 候选审核：BLOCKED 60.49、BLOCKED 60.37、FALLBACK 60.49、BLOCKED 59.82、BLOCKED 58.5、FALLBACK 63.28、BLOCKED 65.53、BLOCKED 61.92、FALLBACK 65.53
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 1 timing: cond=10.2s sample=97.7s decode=14.1s cache=skipped cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.69s）已忽略：不参与段级评分与边界验证
### run14（node_review）
- 节点内审核报告 remote_reports\remote_pulled\9a1e778dfac04c76abf753e87b9a32e7\one_stop_1792310995.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 段级 R_seg：段1:83.82(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 100.0, '节奏合理': 78.239, '无明显跳变': 100.0}，final=94.56，passed=未采集到，policy=未采集到
- 连续性：导演台 2 段 / 248 帧，ON=是，mode=guide，overlap=22f，pin=None，Finalize 未采集到 段，计划 10.33s / 实际 5.25s，显存 29600 MB
### run15（node_review）
- 节点内审核报告 remote_reports\remote_pulled\9a1e778dfac04c76abf753e87b9a32e7\one_stop_1792311344.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=segments（P0-4 修复点）
- 运行报告 docs\remote_evidence_4090\round46\round46_test.json 按 project_id=9a1e778d 匹配
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 84.203, 'passed': True, 'dimensions': {'画面连贯': 80.412, '角色一致': 85.943, '动作合理': 70.458, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 84.203, 'passed': True, 'dimensions': {}}]（来自 docs\remote_evidence_4090\round46\round46_test.json）
- 段级 R_seg：段1:84.2(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 100.0, '节奏合理': 84.059, '无明显跳变': 100.0}，final=96.01，passed=是，policy=未采集到
- 连续性：导演台 2 段 / 248 帧，ON=是，mode=guide，overlap=22f，pin=[2]，Finalize 未采集到 段，计划 10.33s / 实际 5.25s，显存 29600 MB
- 审计原文：段间连续实证：ON=True mode=guide overlap=22f keep_tail=True pin段=[2]
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 2 timing: cond=10.4s sample=103.5s decode=15.6s cache=0.9s cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.56s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 尾段过短（0.56s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 尾段过短（0.08s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 时长不足：计划 10.33s，实际 5.25s（<70%） → 完整性不通过
### run16（node_review）
- 节点内审核报告 remote_reports\remote_pulled\9a1e778dfac04c76abf753e87b9a32e7\one_stop_1792312309.json
- 导演台导出模式证据：节点 timeline_summary.output.exportMode=all（P0-4 修复点）
- 成片大小来自远端 output 列表：/data/BetterComfyUI/projects/9a1e778dfac04c76abf753e87b9a32e7/output/剧核ROUND46验证/2026-10-18/9a1e778dfac04c76abf753e8/剧核ROUND46验证_9a1e778dfac04c76abf753e8_2026-10-18_vround46.mov（1917354 B（未下载视频））
- 运行报告 docs\remote_evidence_4090\round46\round46_test_fixed.json 按 project_id=9a1e778d 匹配
- 成片大小来自本地证据副本 docs\remote_evidence_4090\round46\二段连续_转场字幕水印_10.4s.mov（1917354 B，mtime 2026-10-18 16:32:54；按证据目录 round46 + 修复版标记兜底匹配（文件名不同））
- 成片实测（ffmpeg 抽检 docs\remote_evidence_4090\round46\二段连续_转场字幕水印_10.4s.mov）：250 帧 / 10.417s / 864x480 / 24.0fps / 音轨=有 / 1917354 B
- ⚠️ 冲突 `segment_scores`：保留 [{'segment_index': 1, 'score': 94.493, 'passed': True, 'dimensions': {'画面连贯': 86.447, '角色一致': 97.245, '动作合理': 94.28, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}, {'segment_index': 2, 'score': 87.377, 'passed': True, 'dimensions': {'画面连贯': 70.552, '角色一致': 99.416, '动作合理': 79.539, '无闪烁': 100.0}, 'duration_s': None, 'too_short': None}]，另有 [{'segment_index': 1, 'score': 94.493, 'passed': True, 'dimensions': {}}, {'segment_index': 2, 'score': 87.377, 'passed': True, 'dimensions': {}}]（来自 docs\remote_evidence_4090\round46\round46_test_fixed.json）
- 段级 R_seg：段1:94.49(过)；段2:87.38(过)
- 整片 R_full：scores={'叙事流畅': 100.0, '音画同步': 100.0, '节奏合理': 81.047, '无明显跳变': 100.0}，final=95.26，passed=是，policy=未采集到
- 连续性：导演台 2 段 / 248 帧，ON=是，mode=guide，overlap=22f，pin=[2]，Finalize 未采集到 段，计划 未采集到s / 实际 未采集到s，显存 29600 MB
- 审计原文：段间连续实证：ON=True mode=guide overlap=22f keep_tail=True pin段=[2]
- ⚠️ 审计告警：⚠️ 段间连续被跳过：Segment 2 timing: cond=11.1s sample=102.9s decode=15.2s cache=0.9s cleanup=skipped
- ⚠️ 审计告警：⚠️ 尾段过短（0.58s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 尾段过短（0.58s）已忽略：不参与段级评分与边界验证
- ⚠️ 审计告警：⚠️ 尾段过短（0.08s）已忽略：不参与段级评分与边界验证
### run17（doc_run）
- 仅文档记录（ROUND41 表 运行1），无独立 JSON 证据文件
- 文档原文：运行 1 | 131.65s（含加载模型），出片 105.9s → 发现音轨缺失 bug
### run18（doc_run）
- 仅文档记录（ROUND41 表 运行2），无独立 JSON 证据文件
- 文档原文：**运行 2（修复后）** | **119.31s（含加载模型），出片 94.2s** → 音轨=有，审核通过

## 七、缺失项（未采集到）

无：所有字段均有至少一条运行采集到。

部分采集（全流程测试完成后应补齐）：
- 运行时间：16/18
- 总耗时(s)：12/18
- 节点内总耗时(s)：9/18
- 提示词生成耗时(s)：13/18
- 素材生图+审核耗时(s)：8/18
- 加载视频模型耗时(s)：10/18
- 出片耗时(s)：17/18
- 编码耗时(s)：10/18
- 提示词段数(计划)：14/18
- 实产段数：15/18
- 总帧数：15/18
- 成片时长(s)：16/18
- 帧率：16/18
- 宽：16/18
- 高：16/18
- 素材生成数：13/18
- 素材候选生成数：12/18
- 复用已LOCK素材数：12/18
- 素材审核 LOCK 数：13/18
- 素材未 LOCK 数：13/18
- 素材审核结论：13/18
- 挂载参考图数：13/18
- 完整性 passed：15/18
- 整片四维分数：16/18
- 整片均分：16/18
- 审核阈值：15/18
- 段级审核分数：12/18
- 整片 R_full 分数：10/18
- 审核结论：17/18
- 显存峰值(MB)：12/18
- 成片大小(bytes)：8/18
- 码率(bps)：2/18
- 导演台导出模式：12/18
- 成片实测时长(s)：2/18
- 成片实测帧数：2/18
- 视频审核结论：13/18
- 需重生成段：10/18
- UI真实点击成功：2/18
- UI真实点击耗时(s)：2/18
- UI控件数：2/18
- UI缺失节点数：2/18
- UI截图数：2/18
- 段间连续性 ON：12/18
- 连续性模式：12/18
- 连续性重叠帧：5/18
- 导演台段数：12/18
- 导演台返回帧数：12/18
- Finalize剪辑段数：9/18
- 计划时长(s)：6/18
- 实际生成时长(s)：6/18
- API调用次数：14/18
- API已配置：14/18
- 真实Key/真实模型：14/18
- 剪辑输出格式：3/18
- 转场：3/18
- 字幕：3/18
- 水印：3/18
- 封面：3/18
- 长片批次数：3/18
- 自动返工尝试次数：3/18
- 导演台合并帧数：1/18

## 八、数据来源与口径

- **节点内审核 JSON**（最权威）：远端 `/data/BetterComfyUI/projects/<id>/review/one_stop_<epoch>.json`，字段 `integrity` / `quality` / `assets` / `video_review` / `timings` / `generation`；由 `bettercomfyui_comfy/one_stop_node.py` 真实写出。
- **运行报告 JSON**：`remote_reports/one_stop*.json`，解析 `elapsed_s` 与 `outputs_text` 审计文本（总耗时/分项耗时/出片耗时/编码/段数/素材生图与审核/视频审核结论与错误）。全流程版为 `remote_reports/one_stop_full_test.json`、`one_stop_full_test_v3.json` 等；新增审计行（素材结论/显存峰值 reserved/R_seg/R_full/Finalize 段数/时长说明）已在本管线解析。
- **真实 UI 点击测试**：`docs/remote_evidence_4090/ui_test/ui_test_result.json` ——浏览器脚本只填剧本→点 `#queue-button` 运行，记录 steps / loaded(控件/节点/缺失节点) / prompt_id / status / elapsed_s 与截图（`ui_test/*.png`、`real_key_ui/*.png`）；本管线产出「一·B」（占位模式）与「一·D」（真实 Key）表。
- **多段连续性**：`remote_reports/continuity_test.json` —— 解析导演台段数/总帧、连续性 ON/mode/overlap/pin 段、Finalize 剪辑段数、段级 R_seg、整片 R_full、显存峰值；该文件若被新版结果覆盖，重跑本管线会自动取最新内容重算（旧版结论不保留）。
- **本地复核**：`docs/remote_evidence_4090/one_stop_quality_*.json|md`，由 `scripts/quality_audit.py` 对本地成片 mp4 重新计算（与节点内审核是两套口径，不混为同一分数）。
- **文档级证据**：`docs/ROUND41_*.md` 运行表，只有总耗时/出片耗时/通过与否，属弱证据，单独标注。
- **真实 Key 全链**：`docs/remote_evidence_4090/real_chain/real_chain_test.json`（运行审计）+ `one_stop_审核_真实Key.json`（节点 v0.3-full-director 结构化审核：integrity/quality/video_review/timings/vram/finalize/continuity）+ `真实全链成片_Key.mp4`（本地成片副本，只取大小）；真实 Key 判定看审计 `提示词模式：真实模型（一次调用）`、`API 调用次数：1 次`。
- **真实 Key + 真实 UI 点击**：`docs/remote_evidence_4090/real_key_ui/ui_test_result_real_key.json`（浏览器只填剧本→点 `#queue-button`+#真实模型）与 `real_key_ui/*.png`；解析同 UI 点击测试。
- **ROUND46 功能真机**：`docs/remote_evidence_4090/round46/round46_test.json`（旧版）与 `round46_test_fixed.json`（P0-4 修复版），解析 `SG6 剪辑配置`（格式/CRF/转场/字幕/水印/封面）、长片分批、断点续跑、自动返工轮次、封面/元数据/分段清单路径。
- **P0-4 修复证据**：`round46_test_fixed.json`（修复后 2 段运行）+ 本地成片 `docs/remote_evidence_4090/round46/二段连续_转场字幕水印_10.4s.mov`；本管线用 imageio_ffmpeg 自带的 ffmpeg 对该 mov 做实测（帧数/时长/分辨率/fps/音轨/大小），并对比修复前 v1=137/v2=138/ROUND46旧=126 帧。修复原因：导演台 `exportMode="segments"` 只回部分帧 → 改 `exportMode="all"`（P0-4 修复说明，Lead 提供）。
- **运行环境**：远端 RTX 4090；SSH 端口 **22**；ComfyUI 外网 **http://your-gpu-host:8188**（旧 8188 已不再服务 ComfyUI，旧地址仅历史记录）。
- 匹配规则：优先 project_id；其次成片字节数唯一匹配远端 output；文档表按耗时数值匹配；无法唯一定位的一律独立列条并在「匹配依据」里说明，不强行合并。

## 九、全流程测试完成后：一键重跑

```powershell
cd "[PROJECT-ROOT]"
# 一键：只读拉远端新证据 + 重算 summary + 重新生成本报告
python -B scripts/collect_onestop_evidence.py --pull-remote --round42 docs/ROUND42_数据收集_单节点全流程.md
```

只重算本地（不连远端）：

```powershell
python -B scripts/collect_onestop_evidence.py --round42 docs/ROUND42_数据收集_单节点全流程.md
```

新增文件识别规则（无需改脚本）：

- 本地 `remote_reports/one_stop_full_*.json`、`remote_reports/vram_onestop*.csv` 按文件名通配自动纳入；
- `docs/remote_evidence_4090/` 下新的 `one_stop_quality_*` / `one_stop_review_*` 自动纳入；
- 远端新的 `review/one_stop_*.json` 由 `--pull-remote` 自动发现并拉回（只读 SFTP get，不下载 mp4，只记录其大小）；
- 远端 `/data/BetterComfyUI/remote_reports/*.json` 若存在也会拉回。

## 十、当前可支撑 / 不可支撑的结论

- run01：总耗时 未采集到s、出片 未采集到s、段数 未采集到、成片 1229036 B、结论 通过；缺失 51 项。
- run02：总耗时 119.2s、出片 93.83s、段数 1、成片 851727 B、结论 通过；缺失 41 项。
- run03：总耗时 未采集到s、出片 92.53s、段数 1、成片 未采集到 B、结论 通过；缺失 48 项。
- run04：总耗时 137.27s、出片 94.8s、段数 1、成片 775815 B、结论 通过；缺失 31 项。
- run05：总耗时 180.19s、出片 112.81s、段数 1、成片 2400029 B、结论 通过；缺失 19 项。
- run06：总耗时 未采集到s、出片 123.57s、段数 1、成片 未采集到 B、结论 不通过；缺失 24 项。
- run07：总耗时 未采集到s、出片 128.4s、段数 1、成片 未采集到 B、结论 不通过；缺失 24 项。
- run08：总耗时 未采集到s、出片 127.36s、段数 1、成片 未采集到 B、结论 不通过；缺失 22 项。
- run09：总耗时 160.5s、出片 124.92s、段数 1、成片 未采集到 B、结论 通过；缺失 13 项。
- run10：总耗时 294.2s、出片 274.02s、段数 2、成片 未采集到 B、结论 通过；缺失 18 项。
- run11：总耗时 185.3s、出片 113.02s、段数 1、成片 1562905 B、结论 通过；缺失 17 项。
- run12：总耗时 275.8s、出片 257.39s、段数 2、成片 1226162 B、结论 不通过；缺失 16 项。
- run13：总耗时 200.4s、出片 124.34s、段数 1、成片 1258662 B、结论 通过；缺失 12 项。
- run14：总耗时 未采集到s、出片 276.97s、段数 2、成片 未采集到 B、结论 不通过；缺失 16 项。
- run15：总耗时 286s、出片 265.61s、段数 2、成片 未采集到 B、结论 不通过；缺失 14 项。
- run16：总耗时 285.5s、出片 262.23s、段数 2、成片 1917354 B、结论 通过；缺失 13 项。
- run17：总耗时 131.65s、出片 105.9s、段数 未采集到、成片 未采集到 B、结论 未采集到；缺失 59 项。
- run18：总耗时 119.31s、出片 94.2s、段数 未采集到、成片 未采集到 B、结论 通过；缺失 58 项。

> 当前完全未采集到的字段：无。
> 部分采集：运行时间（16/18）、总耗时(s)（12/18）、节点内总耗时(s)（9/18）、提示词生成耗时(s)（13/18）、素材生图+审核耗时(s)（8/18）、加载视频模型耗时(s)（10/18）、出片耗时(s)（17/18）、编码耗时(s)（10/18）、提示词段数(计划)（14/18）、实产段数（15/18）、总帧数（15/18）、成片时长(s)（16/18）、帧率（16/18）、宽（16/18）、高（16/18）、素材生成数（13/18）、素材候选生成数（12/18）、复用已LOCK素材数（12/18）、素材审核 LOCK 数（13/18）、素材未 LOCK 数（13/18）、素材审核结论（13/18）、挂载参考图数（13/18）、完整性 passed（15/18）、整片四维分数（16/18）、整片均分（16/18）、审核阈值（15/18）、段级审核分数（12/18）、整片 R_full 分数（10/18）、审核结论（17/18）、显存峰值(MB)（12/18）、成片大小(bytes)（8/18）、码率(bps)（2/18）、导演台导出模式（12/18）、成片实测时长(s)（2/18）、成片实测帧数（2/18）、视频审核结论（13/18）、需重生成段（10/18）、UI真实点击成功（2/18）、UI真实点击耗时(s)（2/18）、UI控件数（2/18）、UI缺失节点数（2/18）、UI截图数（2/18）、段间连续性 ON（12/18）、连续性模式（12/18）、连续性重叠帧（5/18）、导演台段数（12/18）、导演台返回帧数（12/18）、Finalize剪辑段数（9/18）、计划时长(s)（6/18）、实际生成时长(s)（6/18）、API调用次数（14/18）、API已配置（14/18）、真实Key/真实模型（14/18）、剪辑输出格式（3/18）、转场（3/18）、字幕（3/18）、水印（3/18）、封面（3/18）、长片批次数（3/18）、自动返工尝试次数（3/18）、导演台合并帧数（1/18）。
> 以上字段需全流程版真机测试产出对应证据后由本管线自动补齐；本报告不做任何估算。

