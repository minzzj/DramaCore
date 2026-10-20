# ROUND47 真机全链（真实 Key）+ UI 移植 + 逻辑图 1:1 对照 + 功能补齐

> 2026-10-18 15:40–16:20（Asia/Shanghai）
> 新容器：SSH `ssh -p PORT user@your-gpu-host`；**ComfyUI 外网端口改为 `http://your-gpu-host:8188`**
> （旧 8188 已不是 ComfyUI；端口映射：8188→8188、22→22、8000→8000、8288→8288、8388→8388、8888→8888、9999→8188）
> 本报告所有数字来自真机证据文件；远端 Key 已配置（日志/面板只显示遮罩 `sk-***f9f7`）。

## 一、真实 Key 全链测试（P0-1 关闭）

### 1) API 触发（占位=false，真实 DeepSeek）
证据：`docs/remote_evidence_4090/real_chain/real_chain_test.json`、`one_stop_审核_真实Key.json`、`真实全链成片_Key.mp4`、`真实提示词.json`

| 项目 | 结果 |
|---|---|
| 提示词 | **真实模型（一次调用）**；**API 调用次数=1**（基准：全程只允许 1 次） |
| 素材 | 9 候选（3 素材×3 轮）→ BLOCKED 6 + **FALLBACK 3 → LOCK 3、挂参考图 3** |
| 出片 | 113.02s，124 帧 / 864×480 / 24fps / 有音轨 |
| 剪辑 | Finalize 1 段 → `真实全链成片.mp4` |
| 审核 | R_seg 78.44 通过；**R_full 97.215 通过**（叙事100/音画94.82/节奏94.04/跳变100）；完整性 True |
| 耗时/显存 | 总 182.7s（提示词 5.81 / 素材 38.12 / 出片 113.02 / 剪辑 3.22）；显存峰值 46176 MB |

### 2) 真实 UI 点击（4090 本机 Chrome，模拟用户）
证据：`docs/remote_evidence_4090/real_key_ui/ui_test_result_real_key.json` + `04_finished_real_key.png`

- 载入 `【剧核】单节点_剧本到成片.json`（95 控件，**缺失节点 0**）→ **只填剧本 52 字** → 点 `#queue-button` 运行；
- **status=success，200.4s**；**API 1 次（真实模型）**；素材 LOCK 3/挂图 3；Finalize 1 段；
- **R_seg 89.765 通过；R_full 98.06 通过**；完整性 True；显存 44864 MB；
- 审计：`视频审核：通过`、`需重生成段：无`。

## 二、导演台 UI 移植（用户点名项，已完成并部署）

原导演节点 UI 已移植到单节点（`bettercomfyui_comfy/web/js/dramacore_director_ui.js`，~1.1k 行；只做前端呈现，不改执行逻辑）：

- **① 22 帧段间连续专区（常驻）**：开关 + 档位 `5/22/39/56` chips（22 高亮官方基线）+ 音频上下文 24 + 音频同步 24 + pin 可视化条 `S1⇄S2⇄…`（关节徽标 `22f ⇄ / audio 24f`，首段标「不 pin」，关闭开关变「硬切/无重叠」）；
- **② 素材库 UI**：首帧/尾帧/参考视频/参考音频 4 卡，真实图片预览、LOCK/未设置徽标、生成审核参数（生成素材图/复用已 LOCK/素材审核轮数/图像模型/分辨率/步数/CFG）；
- **③ canvas 时间轴/分段**：S1..Sn 绘制、时长/提示词/参考图数/首尾帧/音频轨，分段卡可编辑/增删/上下移；时间轴 JSON 双向（写 JSON 需显式确认）；
- ④ 原生长表单默认收起、一键展开（91 参数不删）；运行状态/审核结论徽标。
- 远端已部署并真机截图验证：`docs/remote_evidence_4090/real_key_ui/07_新导演台UI_远端.png`（可见 22 帧区/素材卡/canvas 时间轴全部渲染）；
- 本地验证：`node --check` ✅、面板 DOM 测试 9/9 ✅、Playwright 真实操作 **30/30** ✅、旧工作流兼容 **7/7** ✅；报告 `docs/ROUND45_导演台UI移植.md`。

## 三、逻辑图逐段 1:1 对照（用户要求「每一段都要在源码里找痕迹」）

交付：`docs/剧核_逻辑图逐段代码对照.md`（417 行）：
- 完整重读 `文档/` 全部（docx/dot 逻辑图 516 行/愿景/流程图/AI 任务书）；
- **157 个逐节点行**：✅111 / ◐38 / ❌8（每项带 `文件:行号` 证据）；
- **22 帧连续专项**：逻辑图/文档原文 → 16 步调用链（参数暴露 one_stop:466-481 → timeline v5 director_runtime:227-444 → vendored execute → plan.py:941-990 → executor pin/裁前缀 executor_core:683-782/855-960 → 审计 continuity_evidence）全部有源码痕迹；
- ❌ 8 项与 UI 缺口 Top5 已分别派给功能补齐与 UI 移植智能体（UI 已完成，功能见 §四）。

## 四、未完成内容补齐（ROUND46，已真机验证）

证据：`docs/remote_evidence_4090/round46/round46_test.json` + 项目产物（`.mov` / `.metadata.json` / `.cover.jpg` / `segment_manifest_*.json`）
真机运行：2 段 / 占位模式 / 转场=淡入淡出 / 字幕=自动字幕 / 水印=文字水印 / 封面=第一帧 / 输出=mov / 自动返工 / 22f 连续。

| 能力 | 真机结果 |
|---|---|
| SG6 剪辑 | 审计：`SG6 剪辑配置：格式=MOV CRF=18 转场=淡入淡出 字幕=自动字幕 水印=文字水印 封面=第一帧`；产物 `.mov`（889KB）+ `.metadata.json` + `.cover.jpg` 真实存在 |
| 长片分批/断点续跑 | 审计：`长片分批：批次1=段1-2（共1批，每批≤2段）`；检查点 `_work/onestop_batches.json` 已写；第二次「参数指纹变化 → 旧检查点作废」正确 |
| 自动返工/级联 | 审计：`自动返工轮次：[{attempt:1,verdict:通过},{attempt:2,verdict:通过,final_verify:true}]`（本轮 R_seg 一次通过，未触发重生成） |
| 22f 连续 | `段间连续实证：ON=True mode=guide overlap=22f keep_tail=True pin段=[2]` |
| 视频审核 | `视频审核：通过`；R_seg 84.2 通过；R_full 96.01 通过 |
| 局部边界 | 显存峰值 29600 MB；总 279.2s（出片 265.6 / 返工 3.2 / 封装 2.7） |

**唯一未闭环**：2 段计划 248 帧，但导演台实际返回/编码只 **126 帧（5.25s）**；P0-6 硬校验正确判「完整性=False、审核结论不通过」（**没有假成功**）。
已排查：排除缓存（已清空 `minimax_seg_cache` + 每次唯一 cache_name，仍 126 帧）。初步判断 vendored 导演台在 2 段 `exportMode` 下把分段 mp4 落盘、`images_out` 只返回部分帧（报告称 248 帧）。下一步方案：**直接用导演台落盘的分段 mp4 交给 Finalize**（不再从 images_out 二次编码），预计可彻底修好。

## 五、开源安全

- `scripts/prepare_remote_bundle.py` 已排除 `config/user.yaml`（含 API Key），bundle 只含 `config/user.yaml.example`；
- `.gitignore` 增加 `config/user.yaml`、`config/user.yml`、`dist/*.zip`；
- Key 只存在目标机 `config/user.yaml`（面板遮罩显示）；**该 Key 已在对话中出现，建议测试完成后在 DeepSeek 控制台轮换一次**，开源仓库不要提交 user.yaml。

## 六、当前可用状态 / 人工测试

**单段、单节点、真实 Key、真实 UI 点击全链可用**（两轮证据：API 185s / UI 200.4s，审核通过）。
人工测试（新地址）：
1. 打开 **http://your-gpu-host:8188** → Ctrl+F5（新 UI/端口）；
2. 工作流菜单选 `【剧核】单节点_剧本到成片.json`；
3. 节点里只改「剧本」（Key 已在这台机器配置好，无需再填）；
4. 点运行 → 等待节点内审计/预览；成片在节点预览与 `ComfyUI/output/剧核成片/`。

**仍不建议发布的部分**：2 段/长片端到端（上述帧数 bug）、跨批原生 22f pin（vendor 限制，已用尾帧参考近似并标注）、tiled VAE 未接线、5090-32G 未实测。


---

## 七、P0-4 修复与真机验证（2026-10-18 16:35 追加）

**问题**：2 段计划 248 帧，成片只 126~138 帧（约一半）。
**根因**：`one_stop_node.py` 给导演台的 `output.exportMode="segments"`；该模式会把分段 mp4 落盘、
`images_out` 只返回剩余片段 → 本节点二次编码时只拿到部分帧（跨 v1/v2/ROUND46 三版复现）。
**修复**：统一 `exportMode="all"`（导演台返回全部帧，由本节点按计划段数切分编码）。

**真机验证（2 段 / 占位模式 / 转场+字幕+水印+封面+MOV / 自动返工 / 22f 连续）**：
证据：`docs/remote_evidence_4090/round46/round46_test_fixed.json`、成片 `二段连续_转场字幕水印_10.4s.mov`

| 项目 | 结果 |
|---|---|
| 段数/帧数 | **2 段 / 248 帧计划**，导演台 `2 segment(s), 248 frames @ 24 fps` |
| **成片规格（ffprobe）** | **duration=10.417s、864×480、24fps、含音频、1.92MB**（计划 10.33s + 0.5s 转场叠加） |
| 22 帧连续 | `ON=True mode=guide overlap=22f keep_tail=True pin段=[2]` |
| 审核 | **审核结论=通过（完整性=True，质量均分 95.26，视频审核=通过）**；`段级 R_seg` 两段均通过（94.493/87.377）；`R_full 95.26` 通过；显存 29600 MB |
| SG6 | `MOV + 淡入淡出 + 自动字幕 + 文字水印 + 封面=第一帧`；`.mov/.metadata.json/.cover.jpg` 均在 |
| 长片/返工 | 批次1=段1-2；断点续跑检查点写入；自动返工 2 轮（均通过） |

**结论：P0-4（2 段端到端）关闭。** 发布口径更新：单节点全功能版可转 **Go with caveats**；
剩余仅 5090-32G 实机条件项与已声明 P1 边界（R_seg JSON 字段 index/scores 显示、tiled 未接线、
跨批原生 22f pin 不成立、级联 1 层、转场仅淡入淡出等）。


---

## 八、最终 UI 验收（新导演台 UI + 真实 Key + strict_json）：2026-10-18 16:43

证据：`docs/remote_evidence_4090/final_ui/ui_test_result_final.json`、`04_finished_final.png`

- 新 UI 工作流 **109 控件、0 缺失节点**；只填 52 字剧本 → 点 `#queue-button` → **status=success，170.3s**；
- **API 1 次（真实模型，strict_json 模板）**；模型输出引用了不存在素材（客栈/风雪夜/老陈…）→
  **自动丢弃 13 条无效引用并逐条告警**（不中断、不伪造），素材 3 张生成 + 复用 LOCKED 1 → LOCKED 2/挂图 2；
- Finalize 1 段 → 成片；**R_seg 82.782 通过、R_full 97.63 通过、完整性 True、审核结论 通过**；显存 44928MB；
- 同时验证 ROUND46 能力：自动返工 1 轮通过、断点续跑检查点写入。

**健壮性修复（真实 API）**：`validate_script(allow_drop_invalid_refs=True)` 仅在真实 API 路径启用（默认仍严格），
无效素材引用被丢弃并写入 `meta.dropped_invalid_refs`，总控台审计逐条展示（不静默）。
