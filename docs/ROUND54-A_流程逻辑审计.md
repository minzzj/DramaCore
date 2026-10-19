# ROUND54-A：全流程程序逻辑审计（只读）

- 审计对象：`bettercomfyui_comfy/one_stop_node.py`（主链）及 `director_runtime.py`、`review_nodes.py`、`finalize_nodes.py`、`everything_node.py`、`director_edit.py`、`director_longform.py`、`bettercomfyui/` 核心；对照 `文档/流程图-总.txt`（374 行，图一~图七）、`文档/愿景部分_纯文本.txt`。
- 只读：未改代码/配置、未重启服务、未提交队列；远端 4090 不可用，本地 8188 仅只读。
- **基线声明**：审计开始 `one_stop_node.py` sha256=99589926…（2715 行），审计过程中该文件被并行更新（新增 ROUND54 多段剧本），完成时 sha256=**be353c74…**（2901 行）。本文行号以 **be353c74（2901 行）** 为准；因文件仍在更新，行号可能小幅漂移，引用同时给出函数名。凡无法只读确证的部分标注“待核实/未验证”。
- 已有 `docs/ROUND50-A/B`、`docs/ROUND53_已知问题修复记录.md` 的已修项（默认值/人工审批等待/四维评分/阶段检查点/配置接线）不重复计入；本文只列**新发现**或**已修但路径未覆盖**的逻辑问题。

## 一、问题清单（P0/P1/P2）

| ID | 严重度 | 位置（文件:行号） | 触发条件 | 后果 | 复现步骤 | 建议修复位置 |
|---|---|---|---|---|---|---|
| **A1** | **P0** | `one_stop_node.py:1935-2037`（`_run_multi_script`） | 使用「多段剧本」且成功 ≥2 章 | **交付的总片没有被 R_full 审核**（每章在 `_run_single` 内各自做了整片审核，总片只在 1994 拼接+2002 导出）；UI 的 `dramacore_report` 是**最后一章/最后成功章**的报告（2006-2012、2027），与实际交付的总片不一致；总片只在 `output/_multi_script_<ts>/`（1991-1999），**未写项目 output 终稿名/元数据/清单** | 设置多段剧本（2 章以上）跑一次，检查返回 report：无总片 R_full；`multi_script.master` 路径在临时目录；`review_summary.video` 是单章数据 | 在 1994 拼接后对 master 执行一次 `_video_review`（整片），把结果写入 `last_report["multi_script"]["video_review"]`；总片用 `write_output`/已知输出名落盘 + 元数据 |
| **A2** | **P0** | `review_nodes.py:621-665`（每次审核未过即 `counters.increment`）+ `one_stop_node.py:1887/1927/1970/1427`（同一候选被审核 3-4 次） | 自动返工开启、有任一段不通过 | 项目计数器 `review/counters.json` 的 **CNT2/CNT3 被重复审核提前打满**：一个真实候选轮被 preview 审核、单段审核、最终复核、最终整片审核各计一次；达到 limit 后进入 `capped` 兜底（634-650），使后续**独立视频审核（S4）/跨运行**把新候选误判为“已达上限→取历史最高分兜底”，计数语义失真（流程图要求“CNT2 只统计当前问题段候选轮次”） | 离线：对同一视频用 VideoReview 节点连续审核 3 次（`counters.json` CNT2=3，实际候选仅 1）；再换新内容审核 → 走 capped 分支而非 normal 计数 | review 侧计数改为**按候选版本/尝试幂等**（同一 segment+version 重复复核不加计数，或 `capped` 只影响本次结论不写历史）；one_stop 只做“产出候选时”一次计数 |
| **A3** | **P0** | `one_stop_node.py:1692-1701`（checkpoint 指纹 payload 不含 `时间轴JSON`） + `director_longform.py:72-87`（load 只比指纹） + `director_longform.py:103-110`（reuse 仅 `Path(f).is_file()`） | ①用户用「时间轴JSON」覆盖（`seg_specs=[]`，指纹只含 prompts/尺寸/seed 等）；②同参数重跑命中旧检查点；③段文件存在但 0 字节/被截断 | 用户改时间轴（或换任务）后同参数重跑，**仍复用旧时间轴的段文件**（指纹相同）；0 字节/损坏文件被当作“已完成批次”→ `_generate_batches:1813-1816` 的 missing 检查也只查存在 → 后续拼接/审核才失败，报错点远离根因；`user_tl` 分支的 checkpoint entry 里 segment 列表还是空（`range(start,end)` 为 0），断点续跑实际抓不到 | 离线：`parameter_fingerprint({"prompts":[],...})` 对时间轴 A、B 两值返回相同 hash；`reuse_batch_files({"seg_files":["<0字节文件>"]})` 返回该文件而非 None | 指纹加入 `时间轴JSON`/任务/参考媒体/可选包/音频模式；reuse 校验 `size>0` 且 ffprobe 可读；user_tl 分支正确记录 segment 范围 |
| **A4** | **P0** | `one_stop_node.py:2370-2413`（`_wait_human_asset_decision`） | 人工审核模式下重复运行/重复提交/中断恢复 | 审批文件 `asset_decision_<id>_<ver>.json` **只读不删除**、`asset_pending_*.json` 也不清理：同 asset_id+version 再次进人工审核时**旧 approved 文件立即放行**（无人在场也“通过”）；若旧文件是拒绝，则新一次人工等待被旧拒绝影响；并发两个运行可同时读到同一 decision → 双通过（唯一提交由审核器 token 保证，但会先走错分支） | 离线：临时项目写 `asset_decision_X_v1.json={"approved":true}`，再调用 `_wait_human_asset_decision(...)` → 立即返回 approved=True，且 pending/decision 两文件仍在 | 决策消费后改名归档（`decision_consumed_*`）/删除；pending 在 LOCK/拒绝后清理；decision 增加 nonce/created_at，与本次 attempt 绑定；人工等待前检测陈旧文件并备份 |
| **B1** | P1 | `one_stop_node.py:1372-1616`（finish_movie 成功之后的 `_parse_media`、stage_checkpoint、`_review`、`_video_review`、manifest/report 写盘、`_export_to_comfyui` 均无统一兜底） | 成片已生成后任一环节异常（审核器异常、磁盘满/权限、ffprobe 缺失、报告 JSON 写入失败） | **成片已在项目 output（finish_movie 写出），但：无 review JSON/无 UI `dramacore_report`/无分段清单（若在 1432 前失败）/无 ComfyUI 预览导出**；ComfyUI 侧显示 execution error，但产物与记录不一致，用户可能只看到“失败”而磁盘上已有未登记成片（回填缺失） | 离线：让 `_parse_media` 抛异常（monkeypatch）或把 `project/review` 目录设为只读，跑一次成功生成 → 节点报错；检查 output 已有 mp4、review 目录无 one_stop_*.json | 把 1372 之后全部步骤包进 try/finally：失败时至少写“最小报告+错误原因+final_path”并保留 UI text；export 失败要 append warning（当前 `_export_to_comfyui` 返回 None 时无任何告警，1569-1571） |
| **B2** | P1 | `one_stop_node.py:1357-1371`（Finalize 异常降级分支） | `de.finish_movie` 失败（ffmpeg/磁盘/转场/水印等） | 降级只做 `splice_segments+copy2` 到 `project/output/输出文件名`：**不写 metadata.json、封面、清单（OUT3/OUT4）**，`finalize_report` 无 `output` 字段 → `report_obj["output"]=None`（1549）；虽然 warnings 如实记录，但交付目录缺少元数据/清单，属“未回填” | 离线：monkeypatch `director_edit.finish_movie` raise，跑一次 → output 有成片但无 metadata/封面；report.output=None | 降级分支也调用 `write_output`（或抽公共 `_persist_output`）：成片+元数据+封面+清单一起写；report.output 指向真实文件 |
| **B3** | P1 | `one_stop_node.py:1007-1010`、`1025-1027`（素材生图/复用失败 catch 后继续） | 图像 checkpoint 缺失/生图 OOM/素材库读失败 | 输出**无任何参考图**的成片；违反“连贯性主来源必须是素材库参考图、禁止仅前一帧”（愿景四/流程图 O2/P1）；虽然有 audit/warnings，但流程继续产出成片，用户可能把低一致性成片当正常交付 | 离线：设一个不存在的图像模型名跑 one_stop → 审计出现“继续出片，无参考图”，仍生成视频 | 区分“可降级”与“红线”：素材库已有 LOCKED 或生成失败时，至少要求前段尾帧兜底并在报告标记 `identity_reference_missing=true`；或提供 `严格素材模式`（失败即中止） |
| **B4** | P1 | `one_stop_node.py:1780-1786` + `director_longform.py:103-110` + `one_stop_node.py:1813-1816` | 段 mp4 为 0 字节/被截断/编码器中途失败 | `_encode_movie` 返回值未检查（1785 调用无返回判断）；批次仍标 `done` 写入检查点；missing 检查只 `is_file()` → 坏段通过；到 finalize/审核才失败，检查点却把坏段当完成（续跑不再重生成） | 离线：把 `_encode_movie` 替换为写 0 字节文件，跑 2 段 → 检查点 batches done、finalize 报错 | `_encode_movie` 结果检查（存在、size>0、ffprobe 时长>0）；checkpoint 只在段文件校验通过后写 done |
| **B5** | P1 | `one_stop_node.py:1834-1836`（cascade 时 `spec["continuityFromPrev"]=False`） + `1835-1836 pass` | 级联返工（N+1 段因上游变化重生成） | 级联段**不 pin 前段**、不挂上一段尾帧参考（注释明确“不再 pin”），与流程图 W「第 N+1 段用 N 段首尾帧衔接」不等价；只在 prompt 文字里写“衔接上一段”，实际连续性来自 model 自身 → 级联后画面/动作跳变风险 | 离线：静态确认 cascade 分支 continuityFromPrev=False 且无 refs/tail 注入；GPU 真机验证跳变（远端不可用） | cascade 分支用上一段（重生成后）尾帧作参考图（`image_ref`）或 continuityFromPrev=True；报告记录 cascade 衔接方式 |
| **B6** | P1 | `review_nodes.py:673-692`（边界失败把 `index` 加 need_regen）+ `one_stop_node.py:1911-1926`（all need 走 CNT2） | 相邻边界相似度不达标 | 边界触发的重生成按 **CNT2 计数**，而流程图要求第 N+1 段级联用**独立 CNT3**；CNT2 被“衔接问题”占用，统计口径错误 | 离线：造相邻段低相似度视频跑 VideoReview → need_regenerate 含后段；one_stop 对 need 全部走 CNT2 分支 | `need_regen` 区分质量失败与边界失败；边界失败段在 one_stop 走 cascade/CNT3 路径 |
| **B7** | P1 | `one_stop_node.py:1403-1416,1574-1602,1616-1619`（R_seg/R_full 结论仅记录，未控制交付） | R_seg/R_full 不通过（且整片策略=按项目配置、默认未配置） | 节点仍返回 `final_path` 并把成片导出到 ComfyUI 预览；只有 audit/报告写明“不通过”，没有执行流程图 SG6 的 FULL_DECISION（阻止/降级）对交付物的实际门禁（显式“阻止输出”时依赖 review 节点抛异常——但异常发生在成片已写入项目 output 之后，见 B1/A1） | 离线：故意使用低质量视频跑，观察 `video_review.verdict=不通过` 但 result[0] 仍是成片路径且 ComfyUI 预览存在 | 在写报告/导出前根据 `full.policy` 决定：BLOCK→不导出成片（保留项目内文件并标 blocked）、DEGRADE→导出+degrade 标记；UI 明确“未通过但仍导出/已阻止” |
| **B8** | P1 | `one_stop_node.py:2074/2260/2304/2492`（`is_file()` 后才用 LOCK 图）；`_augment_project_locked_refs:1920`；`store/assets.py verify_locked_file_content` 未在此路径调用 | LOCKED 素材文件被替换（内容哈希与 LOCK 不符）或 LOCK 后文件被删 | 素材复用/参考图补足路径**只检查文件存在**，不校验 LOCK 内容哈希（ROUND50/R2 在 asset_nodes/fusion 加了校验，但 one-stop 复用路径没接）→ 被替换的参考图静默进入导演台，身份参考失真 | 离线：LOCK 一个素材后替换其文件字节，重跑 one_stop 复用路径 → 无任何错误，tensor 来自新文件 | 复用/补足路径统一调用 `verify_locked_file_content`；失败则跳过该素材并 warning（不是静默用） |
| **B9** | P1 | `one_stop_node.py:1233`（`onestop_seg_<秒>`）、`1346`（`edit_<秒>`）、`1360`（`fallback_<秒>`）、`1881`（`rework_<秒>`）、`1991`（`_multi_script_<秒>`）、`2312/2350/1560`（stage/report/manifest 秒级文件名）、`director_longform.py:68-69`（固定 checkpoint 文件） | 同一项目并发提交两次（重复点击/队列重放） | 相同/相邻秒启动的两次运行共享 `_work` 目录、固定段名 `seg_001.mp4`、固定 checkpoint/review 文件名 → 互相覆盖段文件/报告，检查点状态错乱；输出文件默认同名直接覆盖；无每项目运行锁 | 同步并发跑两次（或把两次排队间隔<1s）→ 检查 `_work` 目录与最终文件互相覆盖（静态可证命名冲突） | 每次运行用 uuid 运行目录；项目级运行锁（存在运行中标记则拒绝/排队）；报告/检查点带 run_id；导出前比较目标存在则加版本号 |
| **B10** | P1 | `one_stop_node.py:1957-1985`（章节失败 continue） | 多段剧本某章失败 | 整体返回“成功”（result[0]=总片、ui 正常）只有 summary/warnings 标注失败；`last_report` 是“最后成功章”的报告 → 下游/UI 难区分“部分成功”；且失败章已产生的素材/LOCK 会影响后续章（设计上可接受但需记录） | 离线：3 章中第 2 章造错 → 返回成功、总片为 1+3 章拼接、报告为第 3 章 | 为多段剧本增加顶层结构化字段 `partial_success/ok_chapters/failed_chapters` 并在 UI/返回值使用；“最终 verdict”= 总片审核或章节聚合 |
| **C1** | P2 | `one_stop_node.py:973-980`（出片=False 早退） | 用户关闭出片只出提示词 | 只写 `output/仅提示词.json`；**不写 review 报告、不写阶段检查点（review）、UI 不设 `dramacore_report`** → UI 显示“待运行”而非完成；项目清单无记录 | 关闭出片跑一次 → 检查 output 只有提示词文件、无 report、ui 无 dramacore_report | 早退也写最小报告（mode/提示词/路径）并设置 ui 契约键 |
| **C2** | P2 | `one_stop_node.py` 全函数无 try/finally 中的 `_free_models`（仅 1572 成功路径 + 批次间） | 生成中途抛异常（timeline/批次/审核） | 已加载视频模型未卸载 → 显存/内存残留到进程重启；后续任务 OOM（逻辑图“爆炸半径”未覆盖资源清理） | 离线：在批次执行中注入异常，检查模型对象仍被 ctx 持有（静态可证） | run() 外层 try/finally 调 `_free_models`；异常也写 stage checkpoint“failed” |
| **C3** | P2 | `one_stop_node.py:2350-2368`（stage 文件名秒级）、`_work` 从不清理 | 快速多次运行/同秒 | 阶段检查点同秒覆盖；`_work`/`_multi_script_*` 无清理策略 → 磁盘增长；`checkpoint_every_stage` 关闭时无检查点（已设计） | 静态：`stage_{stage}_{int(time.time())}.json`；grep 无清理 | 文件名加毫秒/序号；提供按项目保留最近 N 次 `_work` 的清理 |
| **C4** | P2 | `one_stop_node.py:2385-2413` | 人工超时/通过后 | `asset_pending_*`、`asset_decision_*` 残留（与 A4 同根，单独列出便于修复验收） | 同 A4 | 同 A4 |
| **C5** | P2 | `one_stop_node.py:1692-1701` + `director_longform.py:103-110` | 正常单段（非 user_tl）0 字节段 | 同 B4 的存在性校验问题（单段场景） | 同 B4 | 同 B4 |
| **C6** | P2 | `one_stop_node.py:1323`（metadata node_version=`v0.4-longform-rework`）vs `1462`（report node_version=`v0.5-review-data-context`） | 每次运行 | 成片元数据与报告版本不一致，追溯/发布评估口径混乱 | 跑一次对比 metadata.json 与 report | 统一版本常量（单点定义） |
| **C7** | P2 | `one_stop_node.py:2500-2566`（未 LOCK 候选版本） | 多轮仍 BLOCKED/FAILED | 已入库的 `PENDING_REVIEW` 候选版本永久残留（无 ABANDONED/CLEANED 标记），素材库版本膨胀；下次复用只找 LOCKED 不影响正确性，但库状态机不整洁 | 离线：让模型审核一直不过跑一轮 → 检查 store 版本均为 PENDING_REVIEW 且无人回收 | 审核终态 BLOCKED/放弃时把候选版本标 `ABANDONED`（或保留但清单说明），避免“待审核”永久滞留 |

**P0=4（A1-A4），P1=10（B1-B10），P2=7（C1-C7）；共 21 条。**

## 二、对照逻辑图的缺口表（图一~图七，逐节点/转移一行）

> 三态：**有**=代码等价实现；**部分**=有实现但分支/计数/落盘不等价；**无**=未找到。缺口数=部分+无行数（文末脚本统计）。

| 图 | 节点/转移 | 状态 | 证据（文件:行号） | 缺口说明 |
|---|---|---|---|---|
| 图一 | SG1 A 剧本输入 | 有 | `one_stop_node.py:555`（多段剧本）/:557（剧本） | — |
| 图一 | SG1 B/B1-B4 提示词模型选择（外部/本地/自配） | 有 | `prompt/gateway.py` provider；`everything_node.py` 调用 | — |
| 图一 | SG1 C/C1-C6 系统提示词/模板/变量/语言/JSON/粒度/风格库 | 部分 | `prompt/inject.py`、`config/default.yaml:1-8`、`entities.py:124-129` | C2 的“受众/比例”变量未找到独立实现（其余有） |
| 图一 | SG1 D/E/E1-E5 拆解与五类提示词 | 有 | `everything_node.py:426`；`one_stop_node.py:753-...`；验收 27/31 | — |
| 图一 | SG2 GEN1-GEN4 四类素材图 | 有 | `one_stop_node.py:2415+`（`_generate_and_review_assets`，limit 默认 4） | 生成失败会被 B3 降级继续出片 |
| 图一 | SG2 GEN_PARAM 六参数 | 有 | `one_stop_node.py:583-585`；`config/default.yaml:21-28` | — |
| 图一 | SG2 F 特殊分隔符自动分段 | 有 | `prompt/validate.py`；`one_stop_node.py:1875`（多段切分） | — |
| 图一 | SG2 G/G1 素材库与四类分目录 | 有 | `store/assets.py`；`AssetType` 四类 | — |
| 图一 | SG2 G2 命名规则/序号 | 有 | `store/ids.py:106`；素材 human label | — |
| 图一 | SG2 G3 统一分辨率/自动缩放 | 有 | `config/default.yaml:22`；`add_version(unify_resolution)` | — |
| 图一 | SG2 G4 每类上限 9 可调 | 有 | `config/default.yaml:28`；`per_class_limit` | 导演台参考图上限 9 与“库可存更多”并存（截断告警：`one_stop_node.py:1929-1932`） |
| 图一 | SG2 G5 优先级 | 有 | `unified_director.py` `_TYPE_PRIORITY` | — |
| 图一 | SG2 G6 手动添加入口（不审核） | 有 | `nodes.py:767 BetterComfyUIManualAsset` | — |
| 图一 | SG2 GEN 失败 → ERR | 部分 | `one_stop_node.py:1007-1010` | 失败不中止而是“无参考图继续出片”（B3），与 ERR 出口不等价 |
| 图一 | SG3 H 审核方式选择 | 有 | `one_stop_node.py:576`；`config/default.yaml:30` | 人工可选；等待实现见 A4（决策文件不消费） |
| 图一 | SG3 H1 人工无限重试 | 部分 | `one_stop_node.py:2500`（`rounds` 上限） | “无限重试”被候选轮数上限约束；拒绝后行为依赖状态机 |
| 图一 | SG3 H2 模型 3 轮取最高 | 有 | `review_nodes.py:621-665`；`config/default.yaml:33` | 计数重复问题见 A2 |
| 图一 | SG3 H3/H4 超时 10min/切模型/日志/通知 | 部分 | `one_stop_node.py:2397-2413` | 切换+日志有；通知区/弹窗未实现（P1 ROUND50） |
| 图一 | SG3 H_arb 仲裁 | 有 | `review/arbiter.py`；验收 09 | — |
| 图一 | SG3 I/K/L/M 通过/合规检查/提示词优化/重生成 | 部分 | `models/enums.py` 状态机；`_generate_and_review_assets` 重生成 | K（AI 合规检查）/L（提示词优化）**未找到实现调用点**；M 由 `_generate_and_review_assets` 承担 |
| 图一 | SG3 LOCK/N 兜底锁定 | 有 | `store/assets.py set_lock`；`one_stop_node.py:2562-2564` | — |
| 图一 | SG3 H5-H7 四维/阈值/重试间隔/CNT1 | 部分 | `config/default.yaml:29-46`；ROUND53 四维补全 | 四维中语义项为确定性启发式；H7 已接线 |
| 图一 | SG3 H8 通知（弹窗/声音/QQ/飞书/钉钉） | 部分 | `review/notify.py` 渠道定义；UI 无通知区 | 端到端未实现（沿用 ROUND50 结论） |
| 图一 | SG4 J/JP1-JP7 导演台参数 | 有 | `one_stop_node.py:604-656`；`config/default.yaml:47-58`；ROUND53 默认值对齐（10s/1920×1088/22/CNT 3） | — |
| 图一 | SG4 O/O1-O4 自动分段/22 帧/素材库参考/自动处理/显存配置 | 有 | `one_stop_node.py:630-656, 1144-1183`；`director_longform.plan_batches` | 跨批用尾帧近似 pin（`director_longform.py:10` 自述） |
| 图一 | SG4 O5 显存节省全策略 | 部分 | `one_stop_node.py:650-656` | tiled 采样未接线；FP8/潜变量轻量化依赖用户换模型 |
| 图一 | SG4 O6 片头片尾角色 | 无 | 未找到 | 与 ROUND50 F1-59 同（仍未实现） |
| 图一 | SG4 O7 背景音乐 | 部分 | `config/default.yaml:89-92` encode 支持 | 一键节点参数面未见 BGM 输入 |
| 图一 | SG4 ENC/P/P1-P3/Q_OUT 封装/分段生成/身份锁/尾帧辅助 | 有 | `director_edit.py`、`finalize_nodes.py`、`one_stop_node.py:1608-1616` | — |
| 图一 | SG5 Q_MAP/R/R_seg/R_full/R1/R2 | 部分 | `review_nodes.py:560-750`；`one_stop_node.py:2656+` | R1/R2 本地启发式；重复审核导致 CNT 膨胀（A2）、边界失败计 CNT2（B6） |
| 图一 | SG5 R3/T/U/REGEN/CNT2/V/W/N1/X/Y | 部分 | `one_stop_node.py:2039-2200`（`_auto_rework`） | X（提示词自动加 @素材）未接 augment；Y “满 3 取最高分”在 review 侧有记录、one-stop 只列 fallback 清单；边界级联口径见 B6 |
| 图一 | SG5 N1 失败 → ERR | 部分 | `one_stop_node.py:2100+` warnings | 记录但不中止 |
| 图一 | SG6 S_OUT→OUT1/OUT2/OUT3/OUT4/OUT5 | 部分 | `pipeline write_output`；`one_stop_node.py:1316-1371, 1428-1436, 1557-1566` | 降级分支缺元数据（B2）；多段总片缺（A1）；命名默认“大节点成片.mp4”（P2）；封面缺省可关 |
| 图一 | SG6 OUT6 第二阶段接口预留 | 部分 | `bettercomfyui_comfy/phase2.py` | 预留未实现（符合定位） |
| 图一 | SG7 Z/Z1-Z9 全局配置注入 | 有 | `config.py` 三层；`config/default.yaml:1-103`；ROUND53 接线 0 未接线 | Z7 评分模型仍为启发式（部分） |
| 图一 | SG8 ERR/ERR1-ERR4 错误出口与爆炸半径 | 部分 | 各模块错误类；`one_stop_node.py:2039+` 局部返工；断点续跑 | ERR2 取消保存进度无显式钩子；ERR4 版本回滚无入口；中程依赖分析未接；统一 ERR 无 |
| 图二 | 角色/场景/物品/分镜素材流转、段级重生成素材不动、22 帧衔接 | 部分 | `one_stop_node.py:2415+`（入库/LOCK）、`:2039+`（只重生成段）、`:630-645`（22 帧） | 跨章节素材复用存在；cascade 不 pin（B5）；真机未验证 |
| 图三 | 默认策略（外部模型/模型审核/手动不审/首尾自动/音频拼接无转场字幕水印/配置/不中止/超时切模型/推荐参数/10s1080P24） | 部分 | `config/default.yaml:1-103`；ROUND53 默认值已对齐 | “默认不中止”无显式机制；通知缺失 |
| 图四 | 局部/中程/全局失败 | 部分 | `_auto_rework`（局部）、断点续跑（全局） | 中程依赖素材→未通过段分析未接（库层 `director/cascade.py` 有）；全局依赖 A3 指纹缺陷 |
| 图五 | 最小闭环（本机/GPU） | 部分 | `one_stop_node.py` 全链；`tests/verify` 298 通过 | GPU 真机不可用；多段总片问题 A1 |
| 图六 | 一小时 360 段规模 | 部分 | `one_stop_node.py:605-609`（360 段/180min） | 真实一小时未验证（GPU 不可用） |
| 图七 | E1-E5/F/G/J/X/P/D/ENC/R 数据契约 | 部分 | `models/entities.py`、`prompt/validate.py`、`config/default.yaml` | X（@素材格式）与 E 字段未逐项对齐；其余参数契约一致 |

> 缺口统计：**部分 21 行 / 无 1 行 = 缺口 22 行**（对照 42 行；脚本核验）。

## 三、可直接执行的离线验证清单（只读可执行，不依赖 GPU 远端）

| # | 验证项 | 命令/脚本思路 | 期望 |
|---|---|---|---|
| 1 | 全量验收回归 | `python -B -m pytest tests/verify -q` | 通过/失败/未验证三态自洽（本轮基线 298/0/1） |
| 2 | A2 CNT 重复计数 | 临时项目 + `BetterComfyUIVideoReview` 对**同一视频**连续审核 3 次；读 `projects/<id>/counters.json` | CNT2 每审一次 +1（3 次=3，候选实际仅 1）→ 复现 A2；再换新内容审核应走 `capped` 分支 |
| 3 | A3 指纹不含时间轴 | `from bettercomfyui_comfy import director_longform as lf; lf.parameter_fingerprint({...})` 用相同 prompts、不同 `时间轴JSON` 两次 | 两个 fingerprint 相同 → 复现 A3 |
| 4 | A3 0 字节复用 | `lf.reuse_batch_files({"seg_files":[<0字节临时文件>]})` | 返回该文件（非 None）→ 复现 0 字节被当完成 |
| 5 | A4 陈旧审批文件 | 临时项目写 `review/asset_decision_<id>_v1.json={"approved":true}` 后调用 `_wait_human_asset_decision(...)` | 立即 approved=True，且 pending/decision 仍在 → 复现 A4 |
| 6 | A1 多段总片无 R_full | `grep -n "_video_review" one_stop_node.py` + 静态阅读 `_run_multi_script`（1935-2037） | 拼接 master 后无 `_video_review` 调用 → 复现 A1 |
| 7 | B2 降级无元数据 | monkeypatch `director_edit.finish_movie` 抛异常后跑单段 | output 有成片但无 metadata/封面；report.output=None |
| 8 | B5 cascade 不 pin | 静态检查 `_regen_one_segment` cascade 分支 | `continuityFromPrev=False` 且无 tail ref → 复现 B5 |
| 9 | B8 LOCK 内容被替换仍挂图 | 临时项目 LOCK 素材→替换文件字节→调用 `_reuse_locked_assets` / `_augment_project_locked_refs` | 无异常、tensor 来自替换后文件 → 复现 B8 |
| 10 | B9 并发目录冲突 | 同项目两次运行目录名 `onestop_seg_/edit_/rework_ + int(time.time())` 同秒 | 目录/段名相同 → 互相覆盖（静态命名可证） |
| 11 | B3 素材失败仍出片 | 填不存在的图像模型名跑单段 | 审计出现“继续出片，无参考图”，仍产出成片 → 复现 B3 |
| 12 | C1 早退无报告 | `出片=False` 跑单段 | output 仅“仅提示词.json”，无 review 报告、ui 无 dramacore_report |
| 13 | 图六规模上限 | 读 `one_stop_node.py:605-609` 参数上限 | 360 段/180min 截断告警正确（结构验证） |
| 14 | 逻辑图缺口复核 | `grep -n "H8\|通知\|回滚\|rollback\|取消\|cancel" bettercomfyui_comfy/*.py bettercomfyui/review/*.py` | 通知/回滚/取消项仍无端到端实现（对应缺口表） |

## 四、结论

- 本轮共 **21 条新逻辑问题**：**P0=4、P1=10、P2=7**；逻辑图对照 42 行中 **缺口 22 行（部分 21、无 1）**。
- 最严重的是 **4 条 P0**：
  1. **A1** 多段剧本交付的总片没有整片审核、报告指向单章、总片未按 OUT1-4 落盘（新功能路径，`one_stop_node.py:1935-2037`）；
  2. **A2** 同一候选被多次审核导致项目 CNT2/CNT3 **重复计数提前打满**，污染跨流程/跨运行的“候选上限”判定（`review_nodes.py:621-665` + `one_stop_node.py:1887/1927/1970/1427`）；
  3. **A3** 断点续跑指纹**不含时间轴 JSON** 且批次复用只查 `is_file()`，旧时间轴段/0 字节文件会被当“已完成”复用（`one_stop_node.py:1692-1701`、`director_longform.py:72-110`）；
  4. **A4** 人工审批 decision/pending 文件**不消费不清理**，陈旧 approved 可在无人工时放行，重复提交可双通过（`one_stop_node.py:2370-2413`）。
- 其余 P1 集中在“成片已生成但报告/元数据/导出未回填”（B1/B2/B7）、“异常降级成无参考图/坏段继续”（B3/B4）、“级联衔接与边界计数口径”（B5/B6）、“并发与幂等”（B9/B10）。
- **未验证/待核实**：所有 GPU 真机效果（远端 4090 不可用）；`_video_review`/`_review`/`_export_to_comfyui` 部分异常分支未逐行核对（文件审计中仍在更新，见基线声明）；多段剧本为并行开发中功能，最终实现可能变化，建议在 ROUND54 合并完成后按本清单复测 A1/B10。

*（本报告只读产出，未修改任何代码/配置，未重启服务，未提交队列。）*
