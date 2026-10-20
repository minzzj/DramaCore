# ROUND55 实现记录（2026-10-19）

> 用户需求：片头片尾按钮、版本回滚入口、tiled 采样接线、导演台优点、跨批 22 帧尾帧参考近似、转场扩展 + AI 决定哪里转场、硬字幕程序化（防漂移）、H3 提示词手册落地 + 视频不生成字幕、级联保持 1 层。全部拆给 7 个专项智能体 + Lead 集成；本机无 GPU（等用户指令抢机）。

## 一、交付模块（均为离线实现 + 单测）

| 模块 | 文件 | 内容 |
|---|---|---|
| 片头片尾 | `bettercomfyui/pipeline/intro_outro.py` | 静态标题卡/片尾演职员卡（ffmpeg drawtext→PIL 兜底、静音音轨、mp4/mov/webm、失败 None+warning 不假成功） |
| 硬字幕 | `bettercomfyui/pipeline/hard_subtitles.py` | 程序化时间轴（句读→可见字符宽度→帧网格，60s@24fps=1440 帧精确）、ASS/SRT、ffmpeg 硬烧、OCR 可选自检（无依赖 skip） |
| 跨批尾帧参考 | `bettercomfyui/pipeline/batch_continuity.py` | 上一批最后 1-3 段抽尾帧→下一批首段参考图；`method=tail_frames_approx`、`native_pin=False`，不冒充原生 pin |
| 转场决策 | `bettercomfyui/pipeline/transition_planner.py` + `bettercomfyui_comfy/director_edit.py` | 纯规则逐接缝决策（pin 段强制轻转场、场景切换→叠化/黑场、高相似度→硬切）；xfade 真实现（淡入淡出/叠化/黑场/白场/滑像/擦除/圆开/圆闭/像素化/模糊/推近）、时长账本、失败回退审计 |
| tiled 采样 | vendor `spatial_tiled_sampling.py`/`core_sampling.py`/`executor_core.py`/`nodes/director.py` + `director_runtime.py` | `tiled_n_tiles=0` 旧行为完全不变；>0 对主采样器空间分块（上限 8、重叠可配）；SelfLift/Refine 组合明确说明 |
| H3 手册 + 禁字幕 | `bettercomfyui/prompt/h3_manual.py` | 手册 5.2/3.x/6.10 要点附录（2493 字）+ 负面词库 + `inject_no_subtitle_instruction`（保留剧本明确可见文字引号） |
| 版本回滚 | `bettercomfyui/store/rollback.py` + `routes.py` | `list_asset_versions` / `rollback_asset`：SHA-256 门禁、LOCK 迁移、`_work/rollback_log.jsonl` 审计、不删历史；HTTP `/bettercomfyui/rollback/list|apply` |
| UI | `web/js/dramacore_director_ui.js` | ⑫「片头片尾 · 素材版本回滚」面板：片头片尾按钮组 + 文字/演职员输入 + 版本列表/回滚（二次确认、hash✓/✗）；转场时长字段 |

## 二、Lead 集成点

- `one_stop_node.py`：
  - 新参数 `片头片尾/片头文字/片尾文字/片尾演职员/转场时长_秒/tiled重叠像素`；转场下拉扩展 + `AI自动（按剧情）`；字幕增加 `硬字幕（程序对齐）`；
  - 成片后处理：硬字幕（程序时间轴）→ 片头片尾拼接（失败告警、保留原成片）；
  - `_generate_batches`：跨批尾帧参考计划/落盘/下一批 refs/批次审计字段（批内原生 pin 完全不动）；
  - 转场：`AI自动` → transition_planner 逐接缝决策 → `finish_movie(transition_plan=...)`；固定标签走统一转场；
  - tiled：`normalize_tiled_sampling`（0=关）→ ctx → `_execute_timeline` 透传 vendor。
- `everything_node.py`：系统提示词追加 H3 手册附录 + 禁字幕系统规则；逐段 `inject_no_subtitle_instruction`；meta 写 `h3_manual` 审计。
- `routes.py`：回滚两个 HTTP 路由（含错误码映射）。
- 工作流：**118 控件**重生成；本地 ComfyUI 已重启、`graphToPrompt` 验证含新参数、自定义 UI 已挂载。

## 三、测试与验证

- 7 个 `tests/test_round55_*.py`：**145 passed**；
- 全量 `pytest -q`：**EXIT=0**；
- `pytest tests/verify`：**298 通过 / 0 失败 / 1 未验证**（未验证=空闲机器无显存驻留，禁止写成通过）；
- 配置接线扫描：0 未接线（历史基线）。
- **未真机验证**（等 4090-48G）：tiled>0 实际显存/耗时、跨批尾帧在真实 H3 上的连续性、转场在真实成片上的效果、片头片尾卡实际拼接、硬字幕真实字体渲染、完整单节点链路。

## 四、级联

按用户要求**不增加**级联层数：仍只 1 层（未改动 `CNT3`/级联逻辑）。

## 五、待办

1. 用户指令后抢 jygpu 4090-48G（余额 ¥11.40、¥2.86/h；单次≤3h；用已保存会话，开机后部署 v0.1.2+ROUND55 并跑真机回归，完事关机）；
2. 真机回归通过后由用户人工测试/审核 → 最终发布 GitHub（版本号届时升到 v0.1.3-internal）；
3. 独立终检智能体（task-16）正在审计，报告 `docs/ROUND55_最终审计.md`。


## 六、终检（ROUND55 最终审计）与修复

独立终检报告：docs/ROUND55_最终审计.md（P0=1 / P1=4 / P2=4）。

已修：
1. **P0-1 工作流 widgets 错位**：按节点新建默认值 + ComfyUI 加载时真实 widget 顺序重生成 118 values；按名回读全部正确；新增 	ests/test_round55_workflow_alignment.py 守卫（定义顺序 + 4 个分组按钮 + control_after_generate 重建位置逐项断言）；
2. **P1-1 硬字幕文本来源**：新增 字幕台词 参数；无台词回退提示词并明确告警；
3. **P1-2 CNT 落盘**：主 R_seg 审核恢复项目 CNT 计数（预览/返工复核不重复计数）；
4. **P1-3 人工审批清理**：pending→.done.json / decision→.used.json / 超时→.timeout.json，启动清理 7 天前归档；
5. **P1-4 片头片尾参与审核**：片头/片尾实际时长并入 段时长列表JSON，R_seg 与成片边界对齐。

修复后：全量 pytest -q EXIT=0；	ests/verify 298 通过/0 失败/1 未验证；工作流对齐守卫通过。
