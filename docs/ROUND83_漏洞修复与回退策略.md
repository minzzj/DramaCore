# ROUND83/84 · 漏洞修复与回退策略

> 范围：审计 ROUND82 的 P1/P2 中可安全落地的项；所有代码改动都有备份，编译/检查失败自动回滚。
> 保持正式版行为不变；新功能/高风险修复只放内测版验证。

## 已修复

| 编号 | 问题 | 修复 |
|---|---|---|
| S-P1-1 / P1-7 | `/project/open`、`/run`、`/rollback/apply` 未鉴权写/执行 | 统一 CSRF 防护：同源 Origin/Host 校验 + Host 必须为 IP 字面量/localhost；远程用域名需 `BETTERCOMFYUI_ALLOW_REMOTE_PANEL=1` |
| S-P1-4 / P1-6 | SchemaGuard 静默把缺失模型名替换成别的模型 | 前端 `NEVER_HEAL`：模型名/API Key/系统提示词等保留原值并警告，不再自动替换 |
| P1-2 | 断点续跑指纹不含素材/首尾帧/参考音视频/音频模式 | ctx 补齐 `asset_refs`、首/尾帧、参考视频/音频、音频模式 6 个键 |
| P1-8 | 公开仓库含本机 `H:\` 路径/内网远程脚本 | 从公开仓库移除 9 个内部脚本（publish/probe/远程安装等） |
| P1-1 辅助 | 新功能不稳定可能影响交付 | 正式版工作流不变；内测版独立目录/独立 ZIP；报错直接回退正式版 |

## 未修复（保留，避免高风险改动）

- **P1-1 审核顺序**：R_full/整片审核仍在成片落盘/导出之后，`BLOCK_OUTPUT` 拦不住交付；需主链重排 + GPU 回归；
- **P1-3 LOCK 哈希**：`content_hash` 仅 1/4 挂图路径校验；
- **P1-9 api_key widget**：普通 STRING widget 可能被工作流保存/历史序列化；建议改面板/环境变量；
- **P1-5 BGM/通知/引用反向索引**：能力缺口；
- P2 21 条逻辑/一致性问题：按审计报告清单排期。

## 自动回退策略

1. 改代码前复制 `*.bak_r83_*` / `*.bak_r84_*`；
2. 改后必须过 `python -m py_compile`；JS 用 `node --check`；失败立刻恢复备份；
3. 工作流只新增、不改旧文件；实验/内测只放 `beta/`；
4. 内测版报错 → 重新打开正式版工作流；启动失败 → 移走 `beta/` 目录；
5. 用户新测试版（若更强但不稳定）：保留正式版；新版本只作为独立频道；报错自动切回正式版等用户验证。

## 验证记录

- `routes.py`、`one_stop_node.py` `py_compile` 通过；
- SchemaGuard JS `node --check` 通过；
- 远端 GitHub 代码 `compile()` 通过；
- 两个内测工作流浏览器加载：`widgets=121 / values=120 / 无 SchemaGuard 横幅 / 无 JS 报错`；
- 未启动 GPU 实跑，GPU 项需目标机验证。


## ROUND85 · 外部提示词对齐（内测版）
- 长段（≥10s）允许多镜头：推荐 2—3 镜、每镜≥3s，切点带 `[Shot N] At 00:SS.mmm`；
- 长段词数上限 300→360，V3 契约 A2.5/A4.5/A14 C10 同步；
- 短段/稳定版行为不变；
- 仍不能保证与外部一致：模型族（ref2va vs fl2va）、无 SageAttention、编码链次数不同，需 A/B。
