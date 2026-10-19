# ROUND54-B 逻辑错误与边界审计（独立第二路 · 攻击性边界视角）

> 只读审计，不改代码/配置、不重启服务、不提交队列；远端 4090 未使用。本地 8188 仅作背景（未提交任何 prompt）。
> 审计对象：`bettercomfyui_comfy/one_stop_node.py`（ROUND54 最新版，含多段剧本/参考图补足）、`director_runtime.py`、`director_longform.py`、`review_nodes.py`、`finalize_nodes.py`、`bettercomfyui/store/*`、`bettercomfyui/review/*`、`tests/*`。
> 对照：`文档/流程图-总.txt`（图一~图七）、`文档/愿景部分_纯文本.txt`。
> 结论口径：**P0 = 会产生"假完成/错误成片/错误返工"或违反愿景红线的逻辑错误；P1 = 高概率用户踩到且结果不可信；P2 = 边界/体验缺陷**。行号以本轮快照为准。

## 0. 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| **P0** | **2** | B-01、B-02 |
| **P1** | **10** | B-03 ~ B-12 |
| **P2** | **6** | B-13 ~ B-18 |

不在本文范围（避免与 ROUND54-A 重复）：纯 UI/文案问题、面板 DOM、工作流 JSON 字段适配；也未做真实 GPU/远端复现（远端不可用），凡"未动态复现"的条目在表格里标注。

---

## 1. 问题表

### P0

| ID | 严重度 | 位置 | 触发条件 | 后果 | 最小复现 | 建议 |
|---|---|---|---|---|---|---|
| **B-01** | **P0** | `one_stop_node.py:1423-1424`、`2156-2161`（把标称 `每段时长_秒` 传给审核）；`review_nodes.py:506`、`513-514`、`558-559`（按固定时长 ffmpeg 切段）；`bettercomfyui/review/local_scorer.py::split_video_into_segments`（`count=ceil(duration/segment_seconds)`，`-ss index*segment_seconds`）；`director_runtime.py:60-70`（帧数吸附 17k+5） | 实际段长 ≠ 标称段长：默认 10s@24fps → `duration_to_frames`=**243 帧 = 10.125s**（每段 +1.25%）。整片按标称 10s 切段 → 边界逐段漂移；1 小时 360 段累计 **+45s**，切出 **365 段**（尾段 4.5s，不会因 <1s 被丢弃）。R_seg 段号与真实拼接点错位 → 返工错段；`need_regen` 出现 361~365 时超出 `len(seg_files)` | 本地离线可复现（无需 GPU/模型）：① `dr.duration_to_frames(10.0,24)==243`；② 生成 360×10.125s 的合成视频（testsrc），按 10.0s 切 → 365 段；③ 在 `_auto_rework` 打印 `last_need`，可见 `need` 含 >360 的段号 | ① 审核切段改为"按真实拼接点清单"（把每段实际时长/frames 传进 VideoReview，而不是标称时长）或审核前先按 ffprobe 实际段长重建映射表；② 返工循环显式过滤 `1<=i<=len(seg_files)` 并记审计；③ 长片（>80 段）必须做映射核对断言 |
| **B-02** | **P0** | `one_stop_node.py:1957-1988`（章失败 continue）、`1989-1999`（总片只拼成功章）、`2012-2037`（`last_report` 取**最后成功章**的 verdict 当整份返回；总片未做 R_full/元数据/清单） | 多段剧本中任一章失败（剧本/API/显存/超时） | 总片静默缺章（中间章缺失也成片），返回的 `verdict`/审核分数来自最后一章而非总片；总片没有整片审核、无 metadata/分段清单 → **"假完成"**；违反图一 SG5「整片级审核在 S_OUT 之前执行一次」 | 本地可复现：`使用占位结果=True` + 3 段剧本（`---` 分隔）；把第 2 章剧本设为空/触发异常（例如把第 2 章写成一个会让 Everything 抛错的内容或模拟异常）→ 观察返回 `result[0]`=总片、`result[1].verdict`=第 3 章结论、审计只有 warning | ① 任一章失败应返回 FAILED/部分完成状态（`result` 结论写"缺章 n"，不返回"成片已通过"）；② 至少对总片跑一次 R_full + 写 metadata/分段清单；③ 或提供参数"失败即终止"（默认） / "跳过失败章"（显式勾选） |

### P1

| ID | 严重度 | 位置 | 触发条件 | 后果 | 最小复现 | 建议 |
|---|---|---|---|---|---|---|
| **B-03** | P1 | `one_stop_node.py:1692-1701`（检查点指纹只含 prompts/frames/尺寸/fps/seed/steps/cfg/模型/continuity/batch/task） | 改素材（重新生成/LOCK 新版本）、改「复用已锁定素材」、换首帧/尾帧/参考视频/参考音频、改音频模式后，用同 prompts 重跑且「断点续跑=True」 | 指纹不变 → 已完成批次全部复用**旧视频**（旧素材/旧首帧/旧音轨），审计仍写"检查点命中"；用户以为按新素材出片，实际人物漂移/音轨未更新（旧版本结论未失效，违反 R2/R3 精神） | 离线：构造 2 批的 ctx（fake executor 写 dummy mp4），先保存检查点；修改 `_augment_project_locked_refs` 的素材输入路径后重跑，观察 audit="检查点命中，跳过段1-8"、seg_files 仍是旧文件 | 指纹补入：已 LOCK 素材的 `asset_id/version/content_hash`、首尾帧/参考视频/音频路径+sha、`音频模式/export/refs` 等所有影响画面的参数；指纹变化时审计必须写"旧批次作废" |
| **B-04** | P1 | `director_longform.py:103-110`（`reuse_batch_files` 只判 `is_file()`）；`one_stop_node.py:1793-1800`（先 encode 再写检查点，encode 非原子） | 段 mp4 编码中取消队列/断点/磁盘满 → 留下 0 字节或半截 mp4；重跑 | 0 字节/半截文件被当成"已完成批次"复用 → 后续 Finalize 拼接失败（或更糟：拼接出坏片）→ "假完成"；检查点不会自愈 | 离线：造 3 个 dummy mp4 的批次检查点，把其中 1 个 `truncate -s 0`，重跑 `_generate_batches` → 观察 `reuse` 命中且不重跑 | `reuse_batch_files` 增加 `size>0` + ffprobe 可解码/时长>0 校验；段文件写临时名再 rename；检查点记录每段 size/hash |
| **B-05** | P1 | `one_stop_node.py:2189-2211`（`_split_segments` 只收集 `list` 中的 audio dict；单 audio 不拆）+ `1782-1786`（`item_audio = auds[k] if k < len(auds) else _normalize_audio(aud)`） | 导演台一次返回**单个整批音频**（或音频列表长度 < 段数） | 每段 mp4 都写入**整批音频**（同一段音频重复 N 次）或整片音频 → 音画不同步/音频重复；Finalize 拼接后声音错乱；审计无提示 | 离线 fake executor 返回 `audio={"waveform": 整批}` 单 dict，`_generate_batches` 输出 N 段 mp4 → ffprobe 显示每段音轨时长=整批时长 | 按段切音频（waveform 按段 frame 边界切片）；无法切时明确降级：该批统一一条音轨并在审计告警，禁止每段重复整批音频 |
| **B-06** | P1 | `one_stop_node.py:2567-2589`（LOCK 后直接 `store.get_version().file_path` 挂图，未校验指纹）；`1887-1932`（续写补参考图同样只判 `is_file`）；对照 `bettercomfyui/store/assets.py:157-180` 已提供 `verify_lock_material`（R3 门禁）但此路径未调用 | LOCK 之后版本文件被外部替换/篡改（同名覆盖），或手工改图 | 参考图挂的是**未授权内容**，R3「LOCK 绑定 content_hash」在单节点链路静默失效；人物一致性看似正常但实际用了错图 | 离线：造资产 v1 LOCK（记录 hash）→ 直接覆写版本 png 字节（不改元数据）→ 调 `_generate_and_review_assets`/`_augment_project_locked_refs` → 当前会照常挂图、无任何报错 | 挂参考图前调用 `verify_lock_material(...)`；失败时该素材不挂、写 BLOCKED 审计并计入 warnings |
| **B-07** | P1 | `one_stop_node.py:2385-2413`（人工审批：写 pending/decision 文件后轮询；超时返回 forced_model；**decision/pending 文件从不删除、也不校验时间戳/operato r归属**） | ① 人工超时切模型后，操作者稍晚才写 `asset_decision_*.json`；② 同 asset_id+version 重跑 | ① 超时后迟到的"人工通过"文件留在磁盘，**下一次重跑同版本会立即读取并当人工通过**，绕过 H_arb「统一后端时间戳仲裁、最终只提交一次」；② 旧决定被重复消费 | 离线：先放一个 `asset_decision_<aid>_v1.json`（approved=true，时间早于本次运行），再跑素材审核 → 立即返回 approved=True；或超时后检查 review 目录仍有 decision/pending 文件 | decision 文件必须带 `created_at/operator/nonce`，只接受"本次运行创建、且在超时前"的文件；消费后归档/删除；超时后到达的人工结果走 H_arb 仲裁并记录"人工迟到"事件；pending 清理 |
| **B-08** | P1 | `one_stop_node.py:946`（`video_prompts = ...[:max(1,int(段数))]`）+ `1070-1071`（`prompts = [_base_prompts[i % len] ...]`）+ `1058-1061`（`plan_segment_count` 取显式段数/目标时长较大者） | 「目标总时长_分钟」算出的段数 > 剧本拆出的提示词段数（例如剧本 3 个分镜、目标 1 分钟、每段 10s → 6 段） | 同一提示词被循环使用生成 2~3 遍 → 画面重复、剧情原地打转；只有审计里"采用 N 段"，没有任何"提示词不足、已循环"告警 | 离线纯函数即可复现：按 946/1071 逻辑，`_base_prompts=["A","B","C"]`、`seg_count=6` → `prompts=["A","B","C","A","B","C"]`，无 warning | 段数 > 提示词数时：① 显式告警+审计"提示词循环 X 次"；② 或改为要求补足剧本/自动扩写；③ 默认禁止循环（宁可少出段） |
| **B-09** | P1 | `finalize_nodes.py:141-168`（`_discover_latest`：扫描 `folder_paths.get_output_directory()` 全局目录 + 项目 output，`rglob("*.mp4")`，按 mtime 窗口） | 成片剪辑开「自动发现最新视频」，机器上同时有别的项目/别人的生成任务（ComfyUI output 全局目录共享） | 把**其它项目/别人的 mp4** 拼进成片（跨项目污染）；`skip_words=("成片_","spliced","final","acceptance","e2e_")` 会**误杀**名字里含 `final` 的合法段（如 `scene_final.mp4`），被跳过的文件不列审计 | 离线可复现：mock `folder_paths.get_output_directory()` 指向 temp；放 `other_project_x.mp4`（新）+ 本项目段；开启自动发现 → integrity.auto_discovered_files 含 other_project_x.mp4 | 只扫本项目 output（+显式项目目录），不要扫全局 output；跳过项写入审计；`skip_words` 改为精确后缀/白名单，避免 "final" 子串误杀 |
| **B-10** | P1 | `finalize_nodes.py:239-242`（`final_name = 输出文件名 or 成片_ts.mp4`；未用 `safe_component`） | 用户在「输出文件名」填 `../../x.mp4`、`sub/dir/y.mp4`；或连续两次同名成片 | ① 路径穿越写到项目 output 之外（可覆盖任意可写路径）；② 同名重复运行**静默覆盖**旧成片，无版本/时间戳，旧证据丢失 | 离线：`输出文件名="../../evil.mp4"` 跑 Finalize（mock core.encode 直接 touch 目标）→ `final_path` 落在项目外 | 用 `store.paths.safe_component` 清洗 + `assert_within(paths.output_dir)`；同名存在时自动加时间戳/版本号（或拒绝并提示） |
| **B-11** | P1 | `one_stop_node.py:973-980`（出片=False 时返回 `仅提示词.json` 作为 result[0]）+ `1957-1999`（多段剧本把每章 result[0] 当视频拼） | 勾选「多段剧本」+「出片=False」（只想批量出提示词） | 每章 result[0] 是 json，`de.splice_segments` 失败→warning→`master=第一章 json 路径`，最终把 **json 文件当"成片"返回**；UI/下游 Finalize 拿到非视频路径 | 离线：多段剧本 + 出片=False（使用占位结果=True）→ 观察 result[0] 以 `.json` 结尾 | 多段剧本分支先判断 `出片=False`：只汇总各章提示词 JSON，直接返回、不拼片；或要求出片=True 才能用多段模式 |
| **B-12** | P1 | `one_stop_node.py:1935-1961`（逐章独立 `_run_single`，不传上一章尾帧/continuity 状态）+ `2016-2017`（note 明说"22 帧连续性只在章内，章间硬切"） | 多章节剧本（愿景八的核心用法：因 DeepSeek 记忆有限而分章） | 章与章之间是**硬切**：角色/动作/光线跳变——与图一 SG4「前段尾部 22 帧衔接」语义不一致，用户分三章生成时会看到三次跳变；目前只有审计 note，没有可选项 | 离线/真机：两章各 1 段，检查章间拼接处无 overlap（`_run_single` 未把章节 N 尾帧注入章节 N+1） | 产品决策：① 默认可选「跨章接续」（把上章尾帧作为下章首段 refs/tail_ref，并标注近似非 vendor 原生 pin）；② 或在报告里明确"章间硬切"并给出后处理转场建议 |

### P2

| ID | 严重度 | 位置 | 触发条件 | 后果 | 最小复现 | 建议 |
|---|---|---|---|---|---|---|
| **B-13** | P2 | `director_runtime.py:60-70`；`director_longform.py:34-59`（`planned_minutes=capped*seg_s/60`） | 每段时长不是 17k+5 网格的整点（如 1s/5s/7.3s） | 实际每段帧数被吸到网格：1.0s@24 → 39 帧=**1.625s（+62%）**，5.0s → 124 帧=5.167s；审计"计划 X 分钟"用标称值，与实际成片偏差大（1h 目标可差十几分钟） | 离线纯函数：`dr.duration_to_frames(1.0,24)`=39；`plan_segment_count(target=1min, seg=1s)` 说 planned_minutes=1.0，但实际 60 段×1.625s=1.63min | 审计里同时打印"标称/网格化后"两个时长；必要时按网格反推段数 |
| **B-14** | P2 | `director_longform.py:44-58`（`chosen=max(requested,auto)`，`capped=min(chosen,360)`，仅 `capped_by_max_segments` 标记） | 目标 3 小时 + 每段 1s → auto=10800，被截到 360（只 6 分钟内容） | 用户以为"目标总时长"生效，实际只生成 1/30 内容，仅一行"超上限已截断"；无强制告警/报错 | 离线纯函数：`plan_segment_count(requested_segments=1,target_minutes=180,segment_seconds=1.0)` → chosen_segments=360、planned_minutes=6.0、capped=True（已核） | 截断比例过大（如 <50% 目标）时升级为 warnings/报错，要求用户确认 |
| **B-15** | P2 | `one_stop_node.py:1094-1103`（用户时间轴JSON 覆盖模式）、`1747-1757`（按 timeline segmentCount 改 seg_files 长度）、`1423-1424`/`2156-2161`（审核仍用标称 `每段时长_秒`） | 用户提供的 timeline_data 各段 `durationSec` 不同（如 5s/10s/15s 混排） | R_seg 仍按统一的「每段时长」切片 → 段号与段边界**必然错位**（不是漂移而是确定性错位）；返工按错段执行 | 离线：构造 3 段 5/10/15s 的用户时间轴 → 审核按 10s 切出 3 段，第 2 段实际覆盖 10-20s（跨第 1 段尾+第 2 段） | 用户时间轴模式：从 timeline 解析每段 durationSec，构造真实段边界清单传给审核；或该模式强制审核范围=仅整片并明确提示 |
| **B-16** | P2 | `one_stop_node.py:1961`（`sub["输出文件名"]=f"{stem}_第{idx:02d}章{suffix}"`） | 同一多段剧本重跑/改参数重跑 | 章成片同名覆盖，无版本/时间戳；与"旧版本结论失效/可追溯"原则不符 | 离线：连跑两次多段剧本，第二次覆盖第一次章文件（mtime 变、内容变、无 `.v2`） | 章输出加运行时间戳/版本号（`stem_ch01_20261019_120000.mp4`）；旧章归档 |
| **B-17** | P2 | `review_nodes.py:75-90`（项目级 `counters.json` 持久化）+ `624-663`（CNT2/CNT3 在复核时 increment）；`one_stop_node.py:2056` 附近（返工自持 `cnt2/cnt3` 数组） | 同项目多轮运行/断点续跑后再次审核 | 两套计数口径（review 层持久化 vs 节点内本轮数组）并存：跨运行 CNT2 可能被 review 层继续累加，而节点侧从 0 开始报告 → 报告计数与实际候选轮次不一致；《检查清单》要求的"CNT 跨运行恢复"未在单节点链路上闭环 | 离线：同一项目连续两次跑视频审核（同一成片），读 `counters.json` 看 CNT2 是否增长，同时看节点报告 `cnt2` 是否从 0 开始 | 单节点链路统一从 `counters.json` 读取/回写 CNT2/CNT3；报告里区分"本轮轮次/历史累计" |
| **B-18** | P2 | `one_stop_node.py:1905-1926`（续写补参考图逐项 `continue`，缺文件静默跳过） | 项目素材版本文件被删/移动后项目续写 | 参考图悄悄少挂（审计只写"补足 X 张"，不写"跳过 Y 张及原因"）→ 人物一致性下降且难排查 | 离线：删掉某 LOCKED 版本 png 后重跑续写 → 观察该素材未挂、无 warning | 跳过要记 warnings（文件名+原因）；补足后打印"已挂/跳过"明细 |

---

## 2. "用户最可能踩到的 10 个坑"（按概率/影响排序）

1. **一小时整片跑完，审核给出的"需返工段"根本不是坏的那段**（B-01：段长 10s→10.125s 网格漂移，长片累计几十秒，段号错位）。
2. **多章节剧本中间一章失败，最终仍拿到"成片"，结论显示通过**（B-02：静默缺章 + verdict 取最后成功章 + 总片无整片审核）。
3. **重新生成了素材/LOCK 了新版本，重跑后视频还是旧人物**（B-03：断点续跑指纹不含素材/LOCK/首尾帧）。
4. **生成中取消队列/断电，重跑直接"续跑"成功，但成片拼接坏/黑屏**（B-04：0 字节段被当完成）。
5. **成片有声音但音画不同步/每段重复同一段声音**（B-05：单条 audio 被整批套到每段）。
6. **LOCK 后素材被替换（同名覆盖）仍然挂进导演台**（B-06：挂图前未校验 content_hash，R3 门禁被绕过）。
7. **人工审核超时后自己才点"通过"，下次运行直接被算人工通过**（B-07：无时间戳/nonce/仲裁，旧决定文件残留）。
8. **想生成 1 小时，结果只出了一小段，剧本同一镜重复好几遍**（B-08 提示词循环 + B-14 段数上限截断）。
9. **成片剪辑"自动发现"把别的项目的视频拼进来，或输出文件名覆盖旧片/路径穿越**（B-09/B-10）。
10. **分章节生成（愿景八的标准用法）章间硬切，角色动作跳变**（B-12：跨章 22 帧连续性缺失，只有 note）。

---

## 3. 离线可复现测试建议（不用 GPU、不用远端）

> 目标：把上面 P0/P1 变成可回归的 pytest 用例（放 `tests/test_round54b_boundaries.py`，全部用 fake executor/mock folder_paths，不写真实项目目录）。

1. **段数/时长纯函数表驱动**（B-01/B-13/B-14）
   - `dr.duration_to_frames`：断言 (10,24)=243、(5,24)=124、(1,24)=39，并把"网格化后时长"写入断言文档；
   - `lf.plan_segment_count`：覆盖 requested∈{0,1,360}、target∈{0,0.01,180}、seg∈{0.1,1,10,30} 的组合表，断言"截断比例"与 planned_minutes 口径。
2. **审核段映射一致性**（B-01/B-15）
   - 用 ffmpeg `testsrc2` 生成 3 段 **2.000s/2.125s/2.000s** 的 mp4 → 按标称 2s 切（`split_video_into_segments`）→ 断言切点与真实拼接点不一致、段数多 1；再断言"按真实清单切"的实现能对齐（作为修复验收）。
   - 用户时间轴 5/10/15s 混排：构造 `normalize_timeline_json` 输入 → 断言审核输入应为 [5,10,15] 而非统一值。
3. **检查点完整性**（B-03/B-04）
   - fake executor 直接写 dummy 文件；`save_checkpoint` 后把 1 个段 `truncate -s 0`，重跑 `_generate_batches` 断言"不复用该批"（当前会复用 → 用例先红，修复后绿）；
   - 修改素材路径清单后重跑，断言指纹变化/旧批次作废。
4. **音频切分**（B-05）
   - fake executor 返回单 audio（waveform 长度=整批 frames）→ 断言每段 mp4 音轨时长=段时长（当前=整批时长 → 红）。
5. **LOCK 指纹门禁**（B-06）
   - 建临时 ProjectPaths+AssetStore，v1 写 png + LOCK；覆写 png 字节；调挂参考图路径函数 → 断言抛 `LockViolation`/不挂（当前会挂 → 红）。
6. **人工审批残留**（B-07）
   - 预先放旧 `asset_decision_<aid>_v1.json`（approved=true）；跑 `_wait_human_asset_decision` → 断言不消费旧文件（当前立即通过 → 红）；超时后断言 pending/decision 被归档。
7. **多段剧本缺章**（B-02/B-11/B-12）
   - monkeypatch `_run_single` 让第 2 章抛错 → 断言返回状态=partial/failed 且总片结论不是"通过"；出片=False 断言不拼片；两章断言总片有跨章衔接（或显式硬切标记）。
8. **Finalize 自动发现隔离**（B-09/B-10）
   - monkeypatch `folder_paths.get_output_directory` 到 temp；放第三方 recent mp4，断言不被选中；`输出文件名="../../x.mp4"` 断言被拒/被清洗；同名连续两次断言产生版本而不是覆盖。
9. **提示词循环告警**（B-08）：构造 `_base_prompts=3` + `seg_count=6`，断言审计含"提示词循环"warning。
10. **CNT 跨运行**（B-17）：同项目连续两次视频审核，读 `counters.json` 与节点报告计数，断言两者一致且区分本轮/累计。

---

## 4. 覆盖与方法说明（诚实边界）

- 本文全部结论来自**静态阅读 + 纯函数/数学推演**；除 `duration_to_frames`/`plan_segment_count` 的公式核对（`director_runtime.py:60-70`、`director_longform.py:34-59`）与切段实现（`local_scorer.split_video_into_segments` 用 `-ss index*segment_seconds`）外，**未实际执行生成/审核链路**，未在远端复现。
- 已阅读并作为"已处理项"排除的边界（避免误报）：素材 LOCK 的三方指纹校验（`store/assets.py:157-180` 有 `verify_lock_material`，问题只在单节点链路未调用，见 B-06）；素材版本文件缺失的 LOCKED_BUT_FILE_MISSING 兜底（`one_stop_node.py:2591-2594` 有告警）；检查点 JSON 损坏/指纹不匹配（`director_longform.py:72-87` 已作废）；尾段 <1s 的审核兜底（`review_nodes.py:518-531`、`569-601` 已处理）；`safe_component`/`assert_within`（`store/paths.py:59,100` 提供，问题是 Finalize 未使用，见 B-10）。
- `tests/` 现状：已有较全的 store/review/director/counter 单测与 acceptance 1–36，但**未覆盖**：长片段长≠标称时的 R_seg 映射、多段剧本缺章/总片审核、断点续跑指纹缺素材、0 字节段复用、单 audio 整批套段、人工审批残留文件、Finalize 自动发现跨项目、输出文件名路径穿越。建议优先补第 3 节 1/2/3/5/7 六组（覆盖全部 P0+P1 中的 6 条）。
