# 内测版（公测）· 剧核 DramaCore

> **Language** · **简体中文** | [English](README_EN.md)

> ⚠️ **内测版不是正式版**，可能不稳定，请勿直接用于正式交付。
> 正式版（稳定）：见仓库根目录 / Release [v0.2.0-internal](https://github.com/minzzj/DramaCore/releases/tag/v0.2.0-internal)。

## 版本信息

| 项 | 值 |
|---|---|
| 频道 | 内测（public beta） |
| 版本 | `0.3.0-beta.1` |
| 定位 | 更快 + 效果更好；验证新链路 |
| 基础 | 正式版 `v0.2.0-internal` + ROUND83/84 安全与稳定性修复 |
| 下载 | [内测版 ZIP](https://github.com/minzzj/DramaCore/releases/download/v0.3.0-beta.1/DramaCore-v0.3.0-beta.1.zip) |

## 两套工作流

| 文件 | 段数×时长 | 适用 |
|---|---|---|
| `workflows/内测版_高质量_2x15s.json` | 2×15s ≈ 30s | 画质优先 |
| `workflows/内测版_极速_2x10s.json` | 2×10s ≈ 20s | 速度优先 |

两者共用：t2v / 生成素材图=关 / 1376×768 / 8步 / 24fps / 转场=无 / 字幕=无 / 自动返工=关 / Refine=关。
**长段能力**：固定段长 ≥10s 时允许段内 2—3 个镜头（切点带 `[Shot N] At 00:SS.mmm`），正文可到 360 词；已与 V3 契约同步。
安装内测 ZIP 后，ComfyUI 工作流菜单也会出现【内测版】两套（`beta/workflows/` 保留原文件）。

## 安装

1. 解压内测 ZIP；
2. 将 `bettercomfyui_comfy` 放入 ComfyUI `custom_nodes/`（可与正式版复用同一目录）；
3. 重启 ComfyUI → Ctrl+F5；
4. 打开【内测版】工作流测试。

## 回退正式版（重要）

- 内测报错/效果回退 → 重新打开正式版工作流：
  - `【新版】剧核_单节点_剧本到成片_真实参数版_20261024.json`
  - 或 `…快速4步版…json`
- 正式版文件未改动，不需要卸载/重装；
- 整个内测包导致启动失败：移走/删除 `beta/` 目录，重启即回正式版。

## 测试记录建议

| 项 | 记录 |
|---|---|
| 耗时 | 总耗时 / 每段耗时 |
| 显存 | 峰值 GB |
| 画面 | 接缝、人物一致性、口型 |
| 错误 | 完整日志/截图 |

## 已知风险

- 2×15s/2×10s 长段显存压力更大，可能 OOM（自动降级会关段间连续/每批1段）；
- t2v 无参考图，人物一致性弱于正式版 r2v + LOCK 素材；
- 尚未在 GPU 上做正式 A/B，公测阶段；稳定后再并入正式版。

许可：CC BY-NC-SA 4.0（禁止商用；修改后同许可开源）。
