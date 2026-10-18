# ROUND46 · 单节点功能补齐（长片/自动返工/SG6/元数据）

- 生成：2026-10-18 16:5x（director-agent，本地实现 + 单测；**未重启远端、未提交 GPU 队列**）
- 依据：`docs/剧核_逻辑图逐段代码对照.md` §5「用户会挑出的问题 Top10」；Lead 本轮派单 1–8 项。
- 改动文件（全部在我的写作用域内，未碰 web/ UI）：
  - `bettercomfyui_comfy/one_stop_node.py`（1644 → 2116 行；参数 + 执行逻辑）
  - **新增** `bettercomfyui_comfy/director_longform.py`（长片分批/断点检查点/Q_OUT 分段清单）
  - **新增** `bettercomfyui_comfy/director_edit.py`（SG6 剪辑：拼接/转场/字幕/水印/封面/格式/OUT3 元数据）
  - **新增** `tests/test_one_stop_round46.py`（12 个本地单测，全部通过）
- 本地验证：`py_compile` + `pyflakes`（0 warning / 0 undefined name）通过；`.comfyenv` 下 `unittest` **17/17 OK**（含 SG6 真 ffmpeg 端到端 + 批次续跑/自动返工编排冒烟；见 §4）。
- 远端真机验证：**留给 Lead 统一跑 GPU**（本环境纪律：不重启远端/不提交队列）；命令与建议参数见 §5。

---

## 1. 逐项完成情况（对应 Lead 派单 1–8）

| # | 需求 | 状态 | 实现位置 | 说明 |
|---|---|---|---|---|
| 1 | **「一次约一小时」+ 自动分段 + 多批断点续跑** | ✅ 本地实现 | `director_longform.py:plan_segment_count/plan_batches/load_checkpoint/save_checkpoint`；`one_stop_node.py::_generate_batches`；新参数 `目标总时长_分钟 / 每批段数 / 断点续跑 / 批次间卸载模型` | `段数` 上限 12→**360**；`目标总时长_分钟` >0 时按 `target*60/每段时长` 自动算段数（360 段×10s=60min）。按「每批段数」分批执行导演台（显存安全），每批结束原子写检查点（`projects/<id>/_work/onestop_batches.json`，参数指纹校验）；中断后同参数重跑自动跳过已完成批次（段文件缺失则重跑）。诚实边界：vendor 的**原生 22 帧 pin 只在同一批内**；跨批衔接用「上一批最后一帧作下一批首段额外参考图」近似，审计明确写 `cross_batch_mode=last_frame_ref`，**不冒充原生 pin**。 |
| 2 | **自动返工/级联（CNT2/CNT3）** | ✅ 本地实现（真机待验） | `one_stop_node.py::_auto_rework/_regen_one_segment`；新参数 `自动返工 / CNT2上限 / 级联返工 / CNT3上限` | 闭环：拼接预览 → 视频审核（仅段级 R_seg）得「需重生成段」→ 自动重生成该段（提示词加料+seed 偏移，单段时间轴）→ 单段重审 → **保留候选最高分** → 上游段变化后对 N+1 段做一次级联重生成（CNT3，`max_cascade_depth=1`）→ 重拼 → 下一轮 R_seg；`自动返工=False` 时只输出清单。达 CNT2 上限仍不过 → `REVIEW_FALLBACK` 记录「取最高分候选」，**不写通过**。最终整片审核透传 `本次是级联重做` 给视频审核节点（CNT2/CNT3 分开计数）。库层 `bettercomfyui/director/rework.py`、`cascade.py` 的语义（CNT2/CNT3 隔离、上限、最高分兜底）按此实现；未直接 import 其控制器（其强依赖 Segment 领域对象/生成器，单节点内复用会产生双重状态机），报告如实说明。 |
| 3 | **SG6 转场/字幕/水印/封面/非 mp4** | ✅ 实现基础版 | `director_edit.py::splice_segments/_crossfade/build_subtitle_entries/write_srt/write_watermark_png/encode_movie/finish_movie` | 转场：段间 ffmpeg `xfade`+`acrossfade` **0.5s 交叉淡化**（失败自动退回流复制并写 warning，不冒充）；字幕：各段提示词→SRT→`pipeline.core.encode(subtitle_file=)` **真烧录**；水印：PIL 生成文字水印 PNG→`encode(watermark_file=)` **真叠加**（右下角）；封面：`write_output` OUT4 首帧 jpg；格式：MP4/MOV 走 `pipeline.encode`，**WebM 本模块用 libvpx-vp9+libopus 实现**（pipeline 只登记 H.264/H.265，不伪装）。「走 Finalize 剪辑」且无高级选项时仍走 `BetterComfyUIFinalize`（保留自检 JSON）。 |
| 4 | **JP6 显存档位 / JP7 剪枝 / O5 策略** | ◐ 部分接入（如实标注） | `one_stop_node.py` 新参数 `显存档位/剪枝模型/tiled采样`；`_generate_batches` 映射 | 已接入执行：显存档位（低=批次≤4+批间卸载、高=关段间清理且批次≥16、中=默认）；剪枝模型=True → 每批结束 `unload_all_models+soft_empty_cache`（等效显存剪枝，模型文件本身剪枝版仍由用户选）；段间/二采/修脸清理沿用既有参数。**tiled 采样未接线**：vendor 执行器内部自建 sampler 对象，不改 vendor 源码无法运行时注入 → 参数已暴露并**明确记录为未接线**（`unimplemented_features`），不冒充生效。 |
| 5 | **Q_OUT 分段清单** | ✅ | `director_longform.py::build_segment_manifest`；`one_stop_node.py` 收尾处 | 每段含 `segment_id`（库层 `store/ids.segment_id`，项目 id 非法时退化并 `id_note` 如实标注）/`segment_index`/`segment_version`/`start`/`frames`/`duration_sec`/`task_type`/`prompt`/`asset_refs`/`video_path`/`final_score`/`review_scores`/`review_passed`/`cnt2`/`cnt3`/`cascade_status`/`fallback`/`degrade_flags`。落盘 `projects/<id>/review/segment_manifest_<ts>.json`，并写入 `MovieMetadata.segments`。 |
| 6 | **OUT3 走 `pipeline.write_output`** | ✅ | `director_edit.py::write_movie_output/finish_movie` | 调 `pipeline.core.write_output` → OUT1 目录 `output/项目名/日期/剧本名`、OUT2 命名 `项目_剧本_日期_v版本`、OUT3 `MovieMetadata`（项目/模型/参数/时间/版本/来源/审核/降级/分段清单）、OUT4 首帧封面；`generation_provenance` 含 `is_mock/executor/reason/unverified`（占位模式明确标 mock，不冒充真实生成）。 |
| 7 | **i2v 冗余 bug** | ✅ 已修 | `one_stop_node.py` i2v 分支 | 去掉 `赋值→pop→再赋值`，改为一次 `seg["genImage"]=first_image_ref`；`tests/test_one_stop_round46.py` 有源码级回归断言。 |
| 8 | **回归测试 + 报告** | ✅ | `tests/test_one_stop_round46.py`、本文件 | 见 §4。 |

**旧「未实现跳过」逻辑已删除**：转场/字幕/水印/封面/非 mp4 不再进入 `unimplemented_features` 的「已跳过」路径，而是真执行；只有 tiled 采样与（若发生）跨批原生 pin 保留为**明确边界**。

---

## 2. 参数新增（节点仍只有 1 个节点，参数多而全，不删功能）

长片：`目标总时长_分钟`、`每批段数`、`断点续跑`、`批次间卸载模型`；`段数` 上限 360。
显存：`显存档位`（自动/低/中/高）、`剪枝模型`、`tiled采样`（已暴露、未接线）、既有 3 个清理开关保留。
返工：`自动返工`、`CNT2上限`、`级联返工`、`CNT3上限`。
SG6：`转场`（无/淡入淡出）、`字幕`（无/自动字幕）、`水印`（无/文字水印）、`水印文字`、`封面帧`、`输出格式`（mp4/mov/webm）。
其余原有 90+ 参数（时间轴 JSON、六任务模式、22 帧连续全参数、SelfLift/Refine/FaceRefine/SemanticBridge、审核阈值/策略、Finalize/CRF 等）全部保留。

## 3. 执行链路（v0.4）

```
剧本 → 一次 API/占位 → 提示词
  → 素材生图+入库+审核 LOCK/兜底 → 参考图
  → 段数规划（显式/目标时长，≤360）→ 分批计划 + 参数指纹检查点
  → [逐批] 构造 timeline v5（批内 22 帧连续 + 可选包）→ vendored 导演台 execute
         → 拆段 → 逐段 mp4（seg_001.mp4…）→ 跨批尾帧参考 → 写检查点 → 批间卸载(可选)
  → 自动返工/级联（R_seg → 重生成 → 单段重审 → 最高分候选 → CNT3 N+1）
  → SG6：拼接（可选 xfade 转场）→ encode（字幕烧录/水印/格式/CRF）
         → pipeline.write_output（OUT1–OUT4 + MovieMetadata + 分段清单 + 封面）
  → 整片 R_seg + R_full 审核、完整性/质量自检 → ComfyUI 预览导出 → 报告 JSON
```

## 4. 本地测试证据

- `py_compile`/`compileall`：`one_stop_node.py / director_edit.py / director_longform.py` **全部 OK**。
- `.comfyenv\Scripts\python.exe -B -m unittest tests.test_one_stop_round46` → **Ran 17 tests, OK**：
  - **SG6 端到端（真实 ffmpeg，非 mock）** `TestSg6Integration`：
    1. 用 ffmpeg lavfi 合成 2 段 2s 带音轨 mp4 → `splice_segments(fade=0.5)` → 输出 **3.5s**（xfade+acrossfade 真生效）；
    2. `finish_movie`（转场=淡入淡出 + 字幕=自动字幕 + 水印=文字水印 + 封面=第一帧 + 输出格式=mov）→ 产物为 `.mov`，`MovieMetadata + cover.jpg + segment_manifest` 均落盘，steps 含「字幕→…真烧录」「水印→…真叠加」；证明 SG6/OUT1–OUT4 在本机真实可跑（远端仅 ffmpeg 能力差异）。
  - **长片分批/断点续跑冒烟** `TestOrchestrationSmoke.test_generate_batches_and_resume`：3 段/每批 2 → 2 批；第一次 2 次导演台调用；**同参数第二次运行 0 次重跑（全部检查点命中）**，段文件齐全；
  - **自动返工编排冒烟** `test_auto_rework_calls`：R_seg 标记段 2 → CNT2 返工 1 次 → 单段重审给分 → 最终复核通过 → `fallback_pass` 记录（不是假通过）；另有用户时间轴覆盖路径冒烟。
  - 以下为基础单测：
  1. 分批 `plan_batches`（含 360 段→45 批）；2. 一小时段数规划（60min/10s→360、超限截断）；3. 检查点指纹/读写/作废/文件缺失不复用；4. 分段清单字段与非法 project_id 降级；5. 尾帧提取缺失文件报错；6. 格式解析（mp4/mov/webm，avi 明确报错）；7. SRT 内容与时间码；8. 水印 PNG 生成；9. 节点新参数存在 & 段数上限≥360；10. SG6 选项真正可配置（不再「未实现」）；11. `run()` 签名覆盖全部 INPUT_TYPES（无漏参）；12. i2v 冗余 pop 源码级回归。
- 未做：真机 GPU 执行（纪律：远端由 Lead 统一验证）；WebM/xfade 需要真机 ffmpeg 能力确认（本地未跑完整视频管线）。

## 5. 交给 Lead 的远端验证（建议）

**不要重启远端前先确认**：远端部署需包含本轮的 `director_longform.py`、`director_edit.py`、`one_stop_node.py` 三个文件（以及此前 v0.3 的 `director_runtime.py`）。

建议第一轮（短平快，验 SG6 + 审核 + 返工开关，2 段）：
- 代表参数：`段数=2`、`每段时长_秒=5.167`、`每批段数=8`、`断点续跑=True`、`使用占位结果=True`、`走Finalize剪辑=True`、`转场=淡入淡出`、`字幕=自动字幕`、`水印=文字水印`、`封面帧=第一帧（默认）`、`输出格式=mov（H.264）`、`自动返工=True`、`CNT2上限=1`、`级联返工=True`、`CNT3上限=1`、`段间连续=True`、`连续帧数=22`。
- 期望证据：审计含「SG6 剪辑配置…」「段间连续实证：ON=… overlap=22f pin段=[2]」；项目 `output/<项目>/<日期>/<script>/` 有 mov + `.metadata.json` + `.cover.jpg`；`review/segment_manifest_*.json` 每段有 segment_id/video_path；视频审核 R_seg/R_full 有分数（tuple bug 不再复现）。

建议第二轮（长片/续跑，小规模先验结构）：
- `目标总时长_分钟=2`（约 12 段×10s，或 `段数=12`）、`每批段数=4`、其余同上；跑到第 1-2 批后中断（取消队列），用**同参数**重跑 → 审计应显示「批次N：检查点命中，跳过段…」且只补剩余批次。
- 期望：`_work/onestop_batches.json` 有 `fingerprint/batches/updated_at`；`longform.batch_rows` 与实际批次一致。

建议第三轮（长片全量，时间允许再做）：`目标总时长_分钟=60`（360 段/45 批 @8 段）——**耗时长，按 GPU 档期决定**；显存档位=低可把批次降到 4 段。

**远端已知风险/注意**：
1. `finish_movie` 用 `write_output` 需要 `pipeline.probe_media`/ffmpeg 在远端可用（项目既有能力，前几轮已验证）；
2. WebM 走 libvpx-vp9，远端 ffmpeg 编译需含 vp9/opus（若无会**明确报错**，不会静默出 mp4 冒充）；
3. 自动返工会额外消耗 GPU 时间（每轮至少一次单段生成 + 一次段级审核）；CNT2上限=1/CNT3上限=1 是最小验证配置；
4. 跨批原生 pin 不成立，报告已写 `last_frame_ref` 近似；若 Lead 认为必须原生跨批 pin，需要改 vendor（本轮红线：不改 vendor 源码），建议下一轮单独立项；
5. 本次**未**触碰 `web/`、`__init__.py` 显示名、vendor 源码，无 UI 冲突。

## 6. 剩余未完成/明确边界

| 项 | 状态 | 原因/建议 |
|---|---|---|
| tiled VAE/DiT 采样（O5 细分） | ❌ 未接线 | vendor 执行器自建 sampler，运行时注入需改 vendor；参数已暴露并明确记录 |
| 跨批原生 22 帧 motion-context pin | ❌ 未实现（近似替代） | vendor 连续性依赖单次 execute 内的上一段 AV latent；跨批需改 vendor 或拆段缓存接口 |
| 级联深度 >1 / REQUEST_HUMAN 策略 | ◐ 仅 1 层 | 本轮按 CNT3上限 做 1 层；库层 `CascadeController` 支持 max_cascade_depth=8，下一步可接 |
| 转场仅「淡入淡出」 | ✅ 基础版 | 已实现 xfade 交叉淡化；其他转场类型按需扩展 |
| WebM 字幕+水印同时开启 | ◐ 只烧水印 | 如实记录 warning（SRT 烧录在 WebM 路径未叠加） |
| 用户 `时间轴JSON` 覆盖模式 | ◐ 不做分批/自动返工 | 用户完全接管时间轴时没有逐段规格，分批/返工需回写时间轴；本轮如实记录「不自动返工、只给 R_seg 清单」 |
| 真机 | ⏳ 待 Lead | 本文件 §5 已给出两轮最小真机命令/参数与期望证据 |
