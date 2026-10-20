# ROUND55 发布前/测试前检查清单（2026-10-19 夜间）

> 结论：**离线代码/文档/工作流/UI 检查已完成**；唯一未完成项是「GPU 真机回归」，被 jygpu 无 4090-48G 库存阻塞（等用户指令抢机）。本机无 GPU，所有真机相关项标记为**未验证**，未写成通过。

## 一、检查结果总览

| 检查项 | 结果 | 证据 |
|---|---|---|
| 7 个专项模块（task-9..15）完成 | ✅ | `docs/ROUND55_实现记录.md` |
| 全量 `pytest -q` | ✅ EXIT=0 | 最近一次运行 |
| `pytest tests/verify` | ✅ 298 通过 / 0 失败 / 1 未验证 | `tests/verify/_last_run.json` |
| 7 个 ROUND55 专项测试 | ✅ 145 passed（+ 工作流对齐守卫 2 passed） | `tests/test_round55_*.py` |
| `scan_config_wiring.py` | ✅ 0 未接线 | 82 字段 |
| 本地 ComfyUI 服务 | ✅ 运行中 127.0.0.1:8188 | `/object_info` 可读 |
| 节点参数注册 | ✅ 113 个输入（含 7 个 ROUND55 新参数） | `/object_info/BetterComfyUIOneStop` |
| 工作流 | ✅ 118 个 widgets_values，按名回读全部正确 | `【剧核】单节点_剧本到成片.json`；守卫 `tests/test_round55_workflow_alignment.py` |
| UI 面板 | ✅ ⑫ 片头片尾·素材版本回滚 已挂载，无页面报错 | 浏览器截图 `docs/ROUND55_workflow_loaded.png` |
| 回滚 HTTP 路由 | ✅ `/bettercomfyui/rollback/list|apply` 已注册（非法 project_id 正确拒绝） | 实测 200/错误 JSON |
| 敏感信息 | ✅ 仓库内无真实 Key/密码/token（仅测试假 Key 与文档说明） | 扫描 |
| 后台自动开机/扣费进程 | ✅ 无；jygpu 仅保存会话未开机 | 进程列表 |
| 最终独立审计（task-16） | ✅ 已完成：P0=1 / P1=4 / P2=4 | `docs/ROUND55_最终审计.md` |

## 二、审计 P0/P1 修复情况（本轮已修）

| 编号 | 问题 | 修复 |
|---|---|---|
| P0-1 | 工作流 widgets 错位（旧顺序保存导致参数整体错位） | 用「节点新建默认值 + 加载时真实 widget 顺序」重生成 118 values；按名回读全部正确；新增 `tests/test_round55_workflow_alignment.py` 守卫（按定义顺序+4 按钮+control_after_generate 重建期望位置逐项断言） |
| P1-1 | 硬字幕文本来源是视频提示词（语义不对） | 新增 `字幕台词` 参数（每行一句）；有台词用台词，无台词回退提示词并**明确告警** |
| P1-2 | 主流程审核不落盘项目 CNT2/CNT3 | 主 R_seg 审核 `不计入项目CNT=False`（每次最终审核计数一次；预览/返工复核仍不重复计数） |
| P1-3 | 人工审批 pending/decision 不清理 | 审批通过：pending→`.done.json`、decision→`.used.json`；超时：pending→`.timeout.json`；启动时清理 7 天前 `.used` 归档 |
| P1-4 | 片头片尾拼接在 R_seg 前导致段边界平移 | 记录片头/片尾实际时长并并入 `段时长列表JSON`，R_seg 按真实成片边界切段；审计写明已对齐 |

## 三、审计 P2/可选改进（未做，不阻塞发布）

1. 工作流 schema 静态守卫已补（本轮）；2. default.yaml 同步 h3_manual_enabled/no_subtitle（config.py 默认 True 已生效，未手改 YAML 以免配置可复现性测试失败）；3. 硬字幕 WebM 中间文件、字幕样式参数化；4. OCR 可选校验依赖安装；5. AI 转场补 chapter_boundary 输入；6. 片头片尾可选转场；7. 回滚 UI 显示受影响段（后端已返回）；8. 跨批尾帧 max_refs 参数化；9. `_video_review` 填实际段时长（本轮已并入片头尾）；10. 更多转场标签。

## 四、未验证（禁止写成通过）

- tiled>0 的真实显存/耗时/画质；
- 跨批尾帧参考在真实 H3 成片上的接缝效果（近似，非原生 pin）；
- 多标签/AI 转场在真实成片上的效果与时长账本；
- 片头片尾卡与真实成片的拼接兼容性（分辨率/编码/音轨）；
- 硬字幕真实字体渲染与 OCR 校验（本机无 OCR 依赖）；
- 完整单节点链路（真实 DeepSeek Key + H3 模型 + 4090-48G）；
- 5090-32G 未实测。

## 五、子智能体/后台任务

- 8 个专项/审计智能体已全部结束（inactive），7 个模块任务 + 终检任务均为 completed；
- 无自动开机/监控后台进程在运行；jygpu 会话文件仅存临时目录，未开机、无扣费。

## 六、下一步（等用户指令）

1. 用户人工测试本机 UI（127.0.0.1:8188，加载 `【剧核】单节点_剧本到成片.json`）；
2. 用户说「可以抢」→ 监控 jygpu 4090-48G → 开机（预算单次≤3h、余额 ¥11.40）→ 部署本版跑真机回归 → 完事立即关机；
3. 真机回归 + 人工审核通过后 → 升版本 **v0.1.3-internal** → GitHub Release（含 bundle、ROUND55 实现记录、终检报告、发布说明）。
