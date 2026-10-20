# ROUND55 最终审计（只读终检）

- 审计性质：独立只读终检（未改源码/配置、未重启服务、未提交队列）；本机无 GPU，远端 4090 不可用 → 真实 H3 生成效果一律标「未验证」。
- 审计时间：2026-10-19 21:18 起（≤40 分钟）；基线：`one_stop_node.py` sha256=5ce67dfcc8da205f（3345 行，mtime 21:04:34）；单节点工作流 mtime 21:08:26。
- 依据：task-16；`docs/ROUND50-A/B`、`docs/ROUND53_已知问题修复记录.md`、`docs/ROUND54-A_流程逻辑审计.md`。
- 本轮实际执行：7 个 ROUND55 模块测试、全量 `pytest tests/`、`tests/verify`、`scripts/scan_config_wiring.py`、`GET /object_info`（只读）、静态代码/工作流对照。

## 一、回归与静态验证结果（全部实际执行）

| 检查 | 结果 |
|---|---|
| 7 个模块测试（`tests/test_round55_*.py`：subtitles/prompts/intro_outro/batch_continuity/rollback/tiled_wiring/transitions） | **145 passed, 0 failed**，EXIT=0 |
| 全量 `python -B -m pytest tests/ -q` | **EXIT=0**；其中 `tests/verify` 三态：**通过 298 / 失败 0 / 未验证 1**（未验证=空闲机器无显存驻留，禁止写通过） |
| `python -B scripts/scan_config_wiring.py` | 162 文件、82 字段，**候选未接线 0 个** |
| `GET /object_info/BetterComfyUIOneStop`（只读） | required 107 + optional 5 = **112 个输入**（与源码 `INPUT_TYPES` 一致） |
| 工作流单节点控件数（静态解析） | `00_【单节点】剧本→成片（无外部依赖）.json` 的 OneStop 节点 **widgets_values=118**（全工作流总数也是 118） |

## 二、需求逐项核对

| # | 需求 | 结论 | 证据 |
|---|---|---|---|
| 1 | 片头片尾按钮 + 生成 | ✅ 实现（离线渲染测试通过；真机 H3 成片效果未验证） | `web/js/dramacore_director_ui.js:2424-2452`（按钮/文字/演职员/说明）；`one_stop_node.py:725-733`（参数）；`:1517-1556`（生成+拼接，失败仅 warning 保留原片，不冒充成功）；`bettercomfyui/pipeline/intro_outro.py:567+ generate_intro_outro`、`:454-470 _validate_output`（校验存在/非空/有视频流/有静音音轨）、`:552`（失败返回空+warning） |
| 2 | 版本回滚入口 | ✅ 入口与安全门禁实现（并发/陈旧结论细节见 P1-3） | `routes.py:1236-1286`（GET list / POST apply）、`:1351-1352` 注册；`bettercomfyui/store/rollback.py:237 list_asset_versions`、`:296 rollback_asset`、`:385 ContentHashMismatchError`（哈希门禁）、`:126-137 _append_log`（审计日志）；UI `dramacore_director_ui.js:2456-2530`；模块测试通过 |
| 3 | tiled 采样接线且 0 默认不变 | ✅ 接线真实、默认关闭 | `one_stop_node.py:672-676`（默认 0/重叠 64）、`:917-920`（normalize 审计）、`:1338-1339`（ctx）、`:1860-1862`（`**dr.tiled_sampling_kwargs(...)` 透传给 `MiniMaxH3Director().execute`）；`:1839` 导入的是 **vendored** `vendor/h3_director/nodes/director`；`director.py:310-311,361-362` → `executor_core.py:431-432,552-555`（默认 0 → `resolve_tiled_request` 关闭、主采样器不包装）；`spatial_tiled_sampling.py:21,94-142`。**tiled>0 在真实 H3 上是否提速/等价：未验证（无 GPU）** |
| 4 | 跨批尾帧参考近似（不冒充原生 pin） | ✅ 语义诚信 | `bettercomfyui/pipeline/batch_continuity.py:2,12,80-82`（METHOD/标签明确“近似，绝不冒充原生 pin”）、`:269-298 build_cross_batch_refs`；集成 `one_stop_node.py:1889-1901`（plan）、`:1942-1959`（复用批 tail refs，`native_pin: False`）、`:2033-2047`；失败/无参考均写 warning |
| 5 | 转场扩展 + AI 逐处决策 | ✅ 实现（离线 ffmpeg 能力探测+回退；真实 H3 成片效果未验证） | `director_edit.py:181-471`（多标签映射、按本机 `ffmpeg -h filter=xfade` 探测、未知/不支持标签回退 fade 并 warning、时长钳制、时长账本）；`:662-810 splice_segments`（单遍 xfade/逐遍/回退流复制均审计）；`one_stop_node.py:715-724`（下拉含“AI自动（按剧情）”）、`:1419-1456`（AI planner → plan → finish_movie），失败回退固定/硬切并 warning |
| 6 | 硬字幕程序化（无文字漂移）+ OCR 可选 | ◐ 程序化时间轴实现，但**字幕文本来源是视频提示词而非台词/剧本**（P1-1）；OCR 仅提供可选函数、未接线 | `bettercomfyui/pipeline/hard_subtitles.py:262-371 build_timeline`（按帧预算/权重分配，非 ASR）、`:606-705 burn_hard_subtitles`（重编码、失败抛错）、`:717-835 verify_with_ocr`（可选，未装 OCR 引擎就跳过）；`one_stop_node.py:1492-1515`（`_entries = [str(p) for p in prompts]` ← 视频提示词；失败仅 warning 保留无字幕成片） |
| 7 | H3 手册提示词 / 禁止生成字幕 | ✅ 注入链真实（系统提示词+逐段提示词；失败不阻断且有 warning） | `bettercomfyui/prompt/h3_manual.py:139 inject_no_subtitle_instruction`、`:362 build_system_prompt_appendix`；`everything_node.py:531-546`（系统模板追加附录+禁字幕规则，写入 `cfg.prompt.system_prompt_template` 后才调用 `generate_script_prompts`）、`:568-586`（逐段注入并写 warning）；开关 `config.py:51-52,60-61,265`（**default.yaml 未同步这两个键，见 P2-1**） |
| 8 | 级联仍为 1 层（不得擅自增加） | ✅ 未增加 | `one_stop_node.py:2422`（`"max_cascade_depth": 1`）；全量 grep 无第二层级联循环；级联仍只对后一段做一次 |
| 9 | UI 与参数同步（新面板/字段挂载） | ◐ UI 字段名与节点参数一致，但**工作流控件数不匹配导致实际打开参数错位（P0-1）** | UI：`dramacore_director_ui.js:1398`（转场时长）、`:2424-2452`（片头片尾）、`:2456-2530`（回滚）、`:2547`（⑫ 面板挂载）；UI 用 `writeVal(node,"转场时长_秒"/"片头片尾"/...)` 与节点参数名一致 |
| 10 | “没有引入假成功”专项 | ◐ 大多数失败路径诚实（intro_outro/hard_subtitles/transition/batch_continuity 都有失败 warning 或异常）；但存在 **CNT 计数口径不一致（P1-2）** 与 **工作流参数错位（P0-1）** 两处“看起来配置生效、实际不是”的风险 | 见 P0-1/P1-2；其余：tiled 审计写 off/on 与真实 resolve 一致；回滚有哈希门禁；转场回退写 warning 且 audit `mode=lost_fallback` |

## 三、问题清单（P0/P1/P2）

| ID | 严重度 | 位置 | 问题 | 触发/后果 | 建议 |
|---|---|---|---|---|---|
| **P0-1** | **P0** | 工作流 `bettercomfyui_comfy/workflows/00_【单节点】剧本→成片（无外部依赖）.json`（OneStop 节点 widgets_values） vs `/object_info` | **单节点工作流控件数 118 ≠ 节点定义 112**，且从约第 80 项起默认值整体错位（例：期望 #95「审核阈值」=70，工作流实际为 `'按项目配置'`；#82「转场」实际为 `18`；#80「走Finalize剪辑」实际为 `null`）。工作流文件 mtime 21:08 晚于节点 21:04，说明“重新生成”并未按当前 schema 对齐 | 用户打开/执行单节点主工作流时，**从转场/片头片尾到审核阈值/失败策略/CNT/API/出片等 28+ 参数整体错位**：轻则转场/片头片尾不生效，重则审核策略被别的值覆盖（安全默认值失效）、API 参数错位；本应被“控数量校验”拦下的经典事故未被拦住 | 按当前 `/object_info`（112 参数）重新生成单节点工作流；在 `tests/` 增加“已发布 workflow JSON 的 widgets_values 数量/顺序必须与节点 schema 一致”的静态守卫（参考 `tests/test_workflow_widget_rules.py` 的 widget 分类规则），并把该守卫接入验收 |
| **P1-1** | P1 | `one_stop_node.py:1498`（硬字幕 entries 来源） | 硬字幕烧录的文本 = **视频生成提示词**（`prompts`），不是剧本台词；节点也没有“字幕台词/字幕文本”参数 | 开启「硬字幕（程序对齐）」后，屏幕字幕是长篇提示词文字（如“电影感，真实自然…”），不是台词；换行/分页按提示词长度分配，语义与用户预期不符 | 增加「字幕台词」输入（或从剧本 E5/台词结构化字段取）；无台词时明确 warning 并跳过，不要用提示词冒充台词 |
| **P1-2** | P1 | `one_stop_node.py:3096-3104`（`_video_review` 默认 `不计入项目CNT=True`）+ 调用点 `:1590/:2335`（未覆盖） | 单节点主流程的所有 R_seg/R_full 审核默认**不写入项目 CNT2/CNT3**（参数默认 True，node 本身默认 False），而 `review_nodes.py:712` 在独立审核路径仍会 increment；两处口径不一致 | 主流程项目 `counters.json` 不记录候选轮次（逻辑图/需求要求 CNT2/CNT3 独立统计并落盘）；跨运行/跨流程（S4 独立审核）看到的是另一套计数；返工候选次数无法从项目状态恢复 | 明确单一计数口径：要么主流程按“新候选版本”幂等计入 CNT（推荐），要么评审节点也不写；不要把 `不计入项目CNT` 默认设为 True 隐藏计数；补测试断言 counters.json 在 N 轮返工后 = N |
| **P1-3** | P1 | `one_stop_node.py:2370-2413`（`_wait_human_asset_decision`） | ROUND54-A 的 A4 未修：人工审批 `asset_pending_*/asset_decision_*` 只读不清理、不消费；陈旧 `approved:true` 会在无人工时立即放行；pending 残留 | 重复运行/中断恢复/重复提交时，人工审核形同虚设或被旧拒绝卡住；审核决策不可审计追溯 | decision 读取后归档/删除；pending 在 LOCK/拒绝/超时后清理；decision 增加 nonce+attempt 绑定；人工等待前检测并备份陈旧文件 |
| **P1-4** | P1 | `one_stop_node.py:1517-1556`（片头片尾拼接）→ `:1582-1590`（R_seg/R_full） | 片头/片尾先拼进成片，再做 R_seg；R_seg 以 `每段时长_秒` 从成片 0 秒切段 → **段边界整体平移片头时长**（片头 3s 后，段1 审核窗口包含标题卡；片尾段同理），`段时长列表JSON` 未传（`:3116` 仍空） | 片头片尾开启时 R_seg 段号/边界与真实生成段错位，可能误报需重生成段、或错误认为通过；级联/返工针对错段 | 审核前用实际段时长列表（含片头偏移）传给视频审核（`段时长列表JSON`），或先审正片再拼片头片尾（更简单）；报告中记录审核时是否包含片头片尾 |
| **P2-1** | P2 | `config/default.yaml` vs `bettercomfyui/config.py:51-52,60-61` | `h3_manual_enabled` / `no_subtitle` 已在代码默认值中，但 default.yaml 未列 | 用户按默认配置文档看不到这两个开关，无法发现/关闭 H3 手册/禁字幕注入 | 同步 default.yaml + user.yaml.example，并加配置文档一致性测试 |
| **P2-2** | P2 | `tests/` | 没有“已发布 workflow 与节点 schema 对齐”的回归测试（test_workflow_widget_rules 只测生成器分类函数） | P0-1 这种错位无法被 CI 发现，靠人工/Lead 声称“118 控件”不可靠 | 新增静态守卫：对所有 `bettercomfyui_comfy/workflows/*.json`，按 `/object_info`/源码 schema 计算 widget 数并与 JSON 逐一比较 |
| **P2-3** | P2 | `one_stop_node.py:1490-1515` | 硬字幕输出用原后缀（webm 时 `hardsub.webm`），`burn_hard_subtitles` 通常按 mp4 编码路径；若容器/编码不匹配，copy2 覆盖回 `final_path` 可能产生扩展名与容器不一致 | 用户选 WebM + 硬字幕时播放器/ffprobe 可能失败或无声 | 硬字幕输出统一中间 mp4，再按目标格式封装；或对 webm 明确 warning 不支持 |
| **P2-4** | P2 | `one_stop_node.py:1419-1440`（AI 转场） | `chapter_boundary` 恒 False；多段剧本章间实际是硬切，AI 规则不会把“章边界”作为转场决策信号 | 多段剧本 + AI 自动转场时，AI 决策不感知章节边界（体验/一致性小缺口） | planner 输入补 `chapter_boundary`（多段剧本调用时传 True） |

**P0=1（P0-1），P1=4（P1-1~P1-4），P2=4（P2-1~P2-4）。**

## 四、通过项（可写入“通过”，均离线/静态验证）

1. 7 个 ROUND55 模块测试 **145 passed**；全量 `pytest tests/` **EXIT=0**；`tests/verify` **298 通过/0 失败/1 未验证**；`scan_config_wiring` **0 未接线**。
2. tiled 采样：默认 0 关闭、>1 才启用、真实透传到 vendored 导演执行器（`MiniMaxH3Director().execute` 含 `tiled_n_tiles/tiled_overlap_pixels`），审计区分 on/off（不含假成功）；**真实 H3 上 tiled>0 效果未验证**。
3. 跨批尾帧：明确 `batch_tail_ref_approx`/“不是原生 22 帧 pin”，每次降级都有 warning；`native_pin: False` 落入 batch 行。
4. 转场扩展：多标签映射、按本机 xfade 能力探测、未知/不支持回退 fade 并 warning、时长钳制与时长账本、单遍/逐遍/流复制回退三级策略 + 审计 `lost_fallback` 标记——离线实现真实，**真实 H3 段素材上的成片效果未验证**。
5. 片头片尾：生成失败/校验失败返回空并 warning，保留原成片；成功拼接落盘并写 `round55.intro_outro` 报告字段——离线 ffmpeg 渲染测试通过，**真实 H3 成片叠加效果未验证**。
6. H3 手册 + 禁字幕：系统提示词模板追加附录 + 逐段视频提示词注入，注入失败 warning 不阻断；开关在 config 代码层。
7. 版本回滚：`/bettercomfyui/rollback/list|apply` 路由 + UI 面板 + 哈希门禁 + 审计日志；模块测试通过。
8. 级联仍为 1 层（`max_cascade_depth: 1`），未擅自增加。
9. 硬字幕时间轴为**程序按帧预算分配**（非 ASR/模型），真实字幕文本来源问题见 P1-1；OCR 校验为可选函数（未装引擎则跳过，不谎报）。

## 五、必须修的 5 条（按优先级）

1. **P0-1：重新生成单节点工作流并加 schema 对齐守卫**（当前 118≠112，参数整体错位；这是发布前必须修的最高优先级——用户拿到的第一入口就错）。
2. **P1-1：硬字幕文本来源改为台词/剧本，禁止用视频提示词冒充字幕**（否则新功能“硬字幕”语义不可用）。
3. **P1-2：统一 CNT2/CNT3 记账口径**（`不计入项目CNT` 默认 True 使主流程不落盘计数，与 review_nodes 独立路径不一致）。
4. **P1-3：人工审批 decision/pending 文件消费与清理**（ROUND54-A A4 仍未修；陈旧批准会导致“无人也通过”）。
5. **P1-4：片头片尾开启时 R_seg 段边界重算（传 `段时长列表JSON`）或先审正片再拼片头片尾**。

## 六、可选改进 10 条（按价值排序）

1. `tests/` 增加 workflow JSON ↔ 节点 schema 的静态守卫（P2-2，直接防 P0-1 回归）。
2. `config/default.yaml` / `user.yaml.example` 同步 `h3_manual_enabled`、`no_subtitle`（P2-1）。
3. 硬字幕：输出统一 mp4 中间文件再封装目标格式，WebM 明确支持/告警（P2-3）。
4. 硬字幕样式参数化：字号/描边/底部安全区/每行字数（`hard_subtitles.py:598 _style_opts` 可扩展），Windows 字体探测结果写入审计。
5. OCR 可选自动模式：烧录后若检测到 OCR 引擎则抽样校验“字幕确实出现”，结果写入报告（默认不阻断）。
6. AI 转场：planner 输入补 `chapter_boundary`（多段剧本章间硬切/转场策略）与素材相似度（`transition_planner.py:159 _similarity_at` 已支持，可把真实边界相似度传进去）。
7. 片头片尾与主片之间可选用转场（当前 splice 固定 `transition="无"`，`one_stop_node.py:1539`）。
8. `_video_review` 把 `段时长列表JSON` 从一进入就用“实际每段帧数/时长”填充（不止片头片尾场景；减少帧网格吸附导致段号错位）。
9. 回滚 UI：二次确认已存在（`dramacore_director_ui.js:2508`），建议增加“回滚后受影响段清单”展示（调用 rollback 返回的结论失效范围）。
10. 批次跨批尾帧参数化（`max_refs=3` 硬编码于 `one_stop_node.py:1948`），并在报告中显式列出每批使用了哪几张尾帧参考。

## 七、未验证清单（禁止写成通过）

- tiled>0 在真实 H3 上的效果/显存收益；跨批尾帧在真实 H3 的连续性效果；转场/片头片尾/硬字幕在**真实 H3 生成成片**上的最终效果（离线 ffmpeg 渲染测试通过不等于真机验证）。
- 远端 4090 不可用（本轮未做任何真机生成）；未提交队列、未开浏览器交互（仅只读 `GET /object_info`）。
- 版本回滚在**并发回滚/回滚后重新生成**的端到端行为（仅有模块测试与静态检查）。

*（本报告只读产出；未修改任何源码/配置，未重启服务，未提交队列。）*
