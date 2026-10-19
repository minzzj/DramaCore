# ROUND53 已知问题修复记录（2026-10-19）

> 依据 ROUND50-A（166 条审查，P0×5/P1×8/P2×5）与 ROUND50-B（内测 Go with caveats / GA No-Go）逐项修复；
> 本轮无 GPU 可用（机器暂不可租），仅做**离线修复 + 单元/验收测试**；GPU 相关项保持「未验证」标注。

## 一、已修复

| 问题 | 修复内容 | 证据/测试 |
|---|---|---|
| P0-2 默认值偏离流程 | `one_stop_node.py` 默认值对齐：每段 10s、1920×1088、素材最多 4、CNT2/CNT3=3；工作流重新生成（110 控件） | `tests/test_round53_fixes.py::test_defaults_aligned_to_flow_requirements`；工作流已更新 |
| P0-3 人工审核仅「演示」 | 实现**真实人工等待**：写 `<项目>/review/asset_pending_<id>_<ver>.json`，审批写 `asset_decision_*.json`；轮询间隔=`asset_review.retry_interval`；超时（`human_timeout_minutes`）自动切模型（CNT1 从 0） | 单测两项（通过/超时切模型）；审计行「人工审核：等待审批文件/通过/超时」 |
| P0-4 四维评分未闭环 | 素材审核维度补全为确定性计算：一致性=与同素材上一 LOCK 版本灰度直方图+边缘相关性；合规=违禁词规则；缺参考/提示词如实 skipped，不编造 | `test_image_scores_four_dims_when_reference_and_prompt` |
| P1-1 配置未接线 | 4 项全部接线：`prompt.normalize_names`、`asset_review.retry_interval`、`video_review.regenerate_granularity`、`global_.checkpoint_every_stage` | `scripts/scan_config_wiring.py` → **0 未接线**；ratchet 已知欠债清空 |
| P1-3 返工粒度硬编码 | `_auto_rework` 读取 `regenerate_granularity`：full=整片所有段重生成，segment=仅 R_seg 标记段 | 代码路径 + 审计行「返工粒度=full…」 |
| 可观测性 | 每阶段写 `_work/stage_<name>_<ts>.json`（prompts/assets/timeline/encode/review），配置可关闭 | `test_stage_checkpoint_written` |
| 版本/交付 | 版本统一 `0.1.1-internal`；README/发布说明同步更新；发布包重新构建 | 本文件 + Release v0.1.1-internal |

## 二、仍开放（本轮无法修复 / 需 GPU 或后续版本）

1. **GPU 真机完整回归未做**（机器暂不可租）：真实出片/22 帧/一小时整片仍为「未验证」；
2. **片头片尾角色**（ROUND50-A F1-59）：未实现；
3. **版本回滚入口**（F1-101）：库层有版本数据，产品 UI/接口未提供回滚；
4. tiled 采样未接线；跨批原生 22 帧 pin 用尾帧参考近似；级联返工默认 1 层；转场仅淡入淡出；WebM 同字幕+水印只烧水印；
5. 5090-32G 未实测；素材审核「一致性/合规」为确定性启发式而非语义模型。

## 三、测试结果

- `python -B -m pytest tests/test_round53_fixes.py -q` → 7 passed；
- `python -B -m pytest tests/verify -q` → **298 通过 / 0 失败 / 1 未验证**；
- `python -B -m pytest -q`（全量）→ **EXIT=0**；
- `python -B scripts/scan_config_wiring.py` → 82 字段，**0 未接线**。
