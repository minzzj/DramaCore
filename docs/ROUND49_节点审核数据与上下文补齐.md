# ROUND49 · 节点审核数据与项目上下文补齐（用户验收反馈）

- 生成：2026-10-18 17:5x（director-agent；节点侧+审核数据；**未碰 web/**，未重启远端、未提交 GPU 队列）
- 依据：Lead 本轮派单 1–6（用户验收反馈）；ui-agent 的前端契约请求。
- 改动文件：
  - `bettercomfyui_comfy/one_stop_node.py`（v0.5-review-data-context；参数 + 结构化输出 + 审计汇总 + UI 契约）
  - `bettercomfyui_comfy/review_nodes.py`（R_seg JSON 补 `index/scores` 别名，评分逻辑零改动）
  - 新增 `tests/test_one_stop_round49.py`（13 个单测）
- 本地验证：`py_compile` OK、pyflakes（one_stop）0 warning；`unittest` **13/13**、ROUND46 回归 **17/17**、相关 review 测试 **35 通过**（含 verify 12/12）；远端 GPU 由 Lead 验证（未重启远端）。

---

## 1. 逐项完成情况（对应 Lead 1–6）

| # | 要求 | 状态 | 实现位置 |
|---|---|---|---|
| 1 | **素材审核分数/状态结构化输出** | ✅ | `_generate_and_review_assets` 新增 `assets[]`（每张：`asset_id/type/name/version/versions/rounds/score/avg_score/scores/state/lock_status/fallback/note`）+ `review_summary`（`generated/locked/blocked/fallback/avg_score/best_score/threshold/review_mode/states[]`）；`report_obj["assets"]` 直接携带；审计**末尾**追加「素材审核汇总：生成 N / LOCK N（FALLBACK n）/ 平均分 X / 阈值 Y / 状态 名=state(score);…」 |
| 2 | **修 P1：R_seg `index/scores=None`** | ✅ | `review_nodes.py` R_seg record 补 `index`（段号）与 `scores`（`dimensions` 别名，四维结构原样），**不改评分逻辑**；`one_stop._video_review` 用 `_map_video_segment_scores()` 兼容两种键，输出 `segments/segment_scores` 每项含 `index/segment_index/score/scores/dimensions/threshold/passed/score_source/file/fallback/degrade/counters/cnt2/cnt3/too_short` |
| 3 | **提示词/导演台/视频审核/返工结果结构化** | ✅ | 见 §2 契约：`prompts.{mode,api_calls,api_call_limit,assets_by_type,assets_total,segments}`；`timeline_summary.{segmentCount,totalFrames,segment_seconds,task_type,output,continuity,continuity_config}`；`video_review.{verdict,segments[],segment_scores[],full,full_review,need_regenerate_segments[],rework_rounds[],cnt2[],cnt3[],fallback[],fallback_pass[],edges}`；`auto_rework.*`；`review_summary`（UI 汇总卡） |
| 4 | **项目续写/上下文** | ✅ | Everything 传入 `写入素材库=True, 启用素材库=True`（只追加不覆盖，不清库）；项目显示名→同 project_id 复用（Everything `_resolve_project` 既有行为，未改 id）；新增 `project_context`（`display_name/project_id/mode/api_calls/manifest_injected/library/locked_assets/locked_count/max_refs/note`）写入 report，并在审计打「素材库：复用已 LOCK N 张（项目 名）」「项目上下文：project_id=… 清单注入=… 库登记=…」「E1–E5 素材提示词数量」 |
| 5 | **素材审核阈值可配** | ✅（运行时注入，失败如实回退） | 新参数 `素材审核阈值`（INT，-1=跟随配置，≥0=本次生效）；实现 `_review_threshold_override()` 运行时 patch `review.asset_review.resolve_review_params`（**不改库源码**），返回 restore 回调；注入失败→审计告警「按项目配置阈值审核；可写 config/user.yaml asset_review.score_threshold」并记录 `threshold_injected=false`；实际生效值写审计与 `report.assets.threshold_used` |
| 6 | 回归 + 报告 + UI 合同 | ✅ | 本文件 §2 为给 ui-agent 的 JSON 合同；§4 测试证据 |

**额外（ui-agent 请求）**：`ui["dramacore_report"] = report_obj` 已加（完整 JSON，前端优先读它；键缺失显示「待运行」）。

## 2. 给 ui-agent 的 JSON 合同（字段名，向后兼容只增不改）

落盘位置（**未变化**）：
- `projects/<project_id>/review/one_stop_<ts>.json` ← 完整报告（= `ui.dramacore_report`）
- `projects/<project_id>/review/timeline_<ts>.json` ← 完整 timeline_data
- `projects/<project_id>/review/segment_manifest_<ts>.json` ← Q_OUT 分段清单
- `projects/<project_id>/review/video_reviews.json` ← 视频审核历史（含 `segments{}`/`edges{}`）
- `projects/<project_id>/output/提示词.json` ← E1–E5
- 成片：`projects/<project_id>/output/<项目名>/<日期>/<script>/<项目_script_日期_v版本>.mp4|.mov|.webm` + `.metadata.json` + `.cover.jpg`（pipeline.write_output）；`_work/onestop_seg_*/seg_NNN.mp4` 为逐段文件；ComfyUI 预览副本 `output/剧核成片/`

`ui.dramacore_report` 关键键：
```jsonc
{
  "node_version": "v0.5-review-data-context",
  "project_id": "…", "task_type": "r2v — …", "task_key": "r2v",
  "project_context": {            // 老项目上下文展示（ui-agent 可用）
    "display_name": "项目", "project_id": "…", "mode": "占位/真实",
    "api_calls": 0, "manifest_injected": true,
    "library": {"created":1,"reused":2,"suspect":0,"errors":0,"skipped":""},
    "locked_assets": [{"asset_id":"…","type":"CHARACTER","name":"主角","version":"v2"}],
    "locked_count": 1, "max_refs": 9, "note": "同项目继续生成：…"
  },
  "prompts": { "mode":"…", "api_calls":0, "api_call_limit":1,
               "assets_by_type":{"CHARACTER":2,"SCENE":1}, "assets_total":3,
               "segments":["段1提示词","段2提示词"] },
  "timeline_summary": { "segmentCount":2, "totalFrames":248, "frameRate":24,
    "width":864, "height":480, "segment_seconds":5.167, "task_type":"r2v — …",
    "output":{"mode":"fixed","exportMode":"segments","audioMode":"generate", …},
    "continuity": {"continuity_enabled_in_plan":true,"mode":"guide","overlap_frames":22,
                   "keep_tail":true,"pinned_segments":[2],"hard_cut_segments":[]},
    "continuity_config": {"enabled":true,"effective":true,"overlap_frames_snapped":22,
                          "pinned_segments":[2],"hard_cut_segments":[]},
    "segments":[{"id":"s0","start":0,"length":124,"refs":3,"prompt":"…"}] },
  "continuity": { /* 同 timeline_summary.continuity 的导演台实证 */ },
  "assets": {
    "created":[…], "locked":[{asset_id,version,png,state}], "blocked":[…], "reused":[…],
    "rounds":[{asset_id,name,version,round,state,score,scores,reason,lock_info}],
    "assets":[{asset_id,type,name,version,versions,rounds,score,avg_score,scores,
               state,lock_status,fallback,note}],
    "review_summary":{generated,locked,blocked,fallback,avg_score,best_score,
                      score_count,threshold,review_mode,states:[{asset_id,name,type,
                      version,state,lock_status,score}]},
    "threshold_used": null, "threshold_injected": false, "errors":[]
  },
  "video_review": {
    "verdict":"通过/需返工/…", "segments":[/* 同 segment_scores */],
    "segment_scores":[{index,segment_index,score,scores,dimensions,threshold,passed,
                       score_source,file,fallback,degrade,counters,cnt2,cnt3,too_short}],
    "full":{executed,r1b_scores,final_score,threshold,passed,policy,policy_message,
            degrade_flags,score_source,note},
    "full_review":{…同上}, "need_regenerate":"无|2,3",
    "need_regenerate_segments":[2,3],
    "rework_rounds":[{attempt,need:[…],verdict,final_verify?}],
    "cnt2":[…], "cnt3":[…], "fallback":[…], "fallback_pass":[…], "edges":{…}
  },
  "auto_rework": {enabled,rounds,cnt2,cnt3,fallback,fallback_pass,regen_files,cascade,policy},
  "quality": {"scores":{…},"average":…,"threshold":70,"passed":true},
  "integrity": {"passed":true,"frames":…,"duration_s":…,"expected_segments":…,
                "actual_segments":…,"segment_count_ok":…},
  "review_summary": {"assets":{…},"video":{verdict,segments_total,segments_passed,
                    need_regenerate:[…],full:{…}},"rework":{…},"integrity":{…},"quality":{…}},
  "edit": {executed,steps,subtitle_file,watermark_file,encode?,finalize?,warnings},
  "output": {"out_path","metadata_path","cover_path","checks","content_hash"} | null,
  "segment_manifest":[{segment_id,segment_index,segment_version,start,frames,duration_sec,
                       task_type,prompt,asset_refs,video_path,final_score,review_scores,
                       review_passed,cnt2,cnt3,cascade_status,fallback,degrade_flags,source}],
  "warnings":[…], "audit":"多行审计文本", "raw_director_report":"…"
}
```
**ui-agent 降级解析**：`ui.dramacore_report` 不存在时仍可解析 `ui.text` 审计里的固定行（均在审计末尾）：`素材审核汇总：…`、`段级审核汇总：…`、`审核结论：…`、`时间轴：…`、`证据：…`。

## 3. 审计输出变化（用户验收「审核结果尾部加素材审核多少分」）

run() 审计末尾新增两行（在「证据：」之前）：
- `素材审核汇总：生成 N / LOCK N（FALLBACK n）/ 平均分 X / 阈值 Y / 状态 主角=FALLBACK(64.5)；场景=LOCKED(71.0)…`
- `段级审核汇总：M 段，通过 k，需重生成 [2]；整片 R_full=True（策略=none）`

## 4. 本地测试证据

- `py_compile` / `compileall` 通过；`pyflakes bettercomfyui_comfy/one_stop_node.py` **0 warning**。
- `.comfyenv\Scripts\python.exe -B -m unittest tests.test_one_stop_round49` → **13/13 OK**：
  旧 JSON 结构 `segment_index/dimensions` → `index/scores` 填满；新结构别名；无需重生成解析；素材汇总数值/LOCK-FALLBACK 计数；项目上下文/锁定素材注入/library；阈值注入+恢复+负值 noop；节点新参数与 run 签名；`ui["dramacore_report"]` 源码契约；`_video_review` 桩节点端到端（index/scores/full/need 全非空）。
- `unittest tests.test_one_stop_round46` 回归 **17/17 OK**（含 SG6 真 ffmpeg 端到端/分批续跑/自动返工冒烟）。
- `pytest tests/test_audit_r2_fixes.py tests/test_vision_defaults_conformance.py tests/verify/test_acceptance_32_review_pipeline.py` → **35 通过**；verify 静态评审 12 通过 0 失败（review_nodes 加字段不改评分逻辑）。
- 未做：真机 GPU（按纪律由 Lead 跑；本机无远端重启/队列提交）。

## 5. 交给 Lead 的远端验证（建议，部署 `one_stop_node.py` + `review_nodes.py` 后重启）

第一轮（2 段、占位、约 2-3 分钟 GPU）：`段数=2`、`段间连续=True/22`、`生成素材图=True/素材审核轮数=3`、**`素材审核阈值=55`（验证节点级阈值注入：低阈值应比默认 70 更易 LOCK，审计打印「节点参数」）**、`自动返工=True/CNT2上限=1`、`走Finalize剪辑=True`。
期望证据：
1. 审计末尾出现「素材审核汇总：生成 3 / LOCK n（FALLBACK m）/ 平均分 …（r2v 用真实图时 n≥1）」；
2. 审计「素材库：复用已 LOCK N 张（项目 …）」——**第二次用同一项目名重跑**时 N 递增或复用（证明项目续写不清库、id 不变）；
3. `review/one_stop_*.json` 里 `video_review.segment_scores[0].index==1` 且 `scores` 为四维非空（P1 回归）；`need_regenerate_segments` 为数组；
4. `ui.dramacore_report` 随队列结果返回（ComfyUI `/history` 可读取），键齐全。

## 6. 剩余边界/风险（不假成功）

1. 素材审核阈值注入依赖 `review.asset_review.resolve_review_params` 模块级符号；若库大改则注入失败 → 会写 `threshold_injected=false` + 审计告警并回退配置阈值（**不静默**）；长期建议把 threshold 作为 `BetterComfyUIAssetReview` 正式入参（需改 nodes.py，属库层，可下轮）。
2. Everything 的 `素材审核模式/阈值` 只影响提示词口径/审计；单节点内部实际审核按本节点的 `素材审核模式/阈值` 执行（已透传，审计写实际生效值）。
3. 用户 `时间轴JSON` 覆盖模式下 `prompts/timeline_summary` 仍输出；但素材参考图不自动注入、不自动返工（沿用 ROUND46 边界，报告如实写）。
4. 旧项目上下文展示由 ui-agent 只读扫描实现；本节点保证 `project_id` 只由 Everything 解析一次并全程复用，不清空/不改写素材库索引。


---

## 真机验证与补充（18:15）

- 远端 4090 首轮 2 段（占位，素材审核阈值=55）：审计末尾出现「素材审核汇总：生成 9 / LOCK 3（FALLBACK 3）/ 平均分 62.23 / 阈值 55」；
  段级 R_seg 的 index=1、scores 四维已填充；project_context 输出；审核通过；显存 32832MB。
- 同项目续写（第二章，1 段）：审计「素材库：复用已 LOCK 2 张（项目 剧核ROUND49验证）」，project_id 不变。
- 补充修复：生成素材图=False 且选择旧项目时，节点新增 _reuse_locked_assets() 从项目库复用 LOCKED 素材
  作为参考图（名称匹配优先、名称不匹配时项目库补足），真机验证「挂参考图 3」且不重新生成。
