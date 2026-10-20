# THIRD PARTY NOTICES / 第三方来源声明

本仓库（DramaCore / 剧核）包含以下第三方组件与内容；发布包必须随附本文件。

## 1. 剧核自身许可证

- 根 `LICENSE`：**AGPL-3.0 + Commons Clause**（修改必须开源；**禁止商用**）。
- 该许可证与 GPL/AGPL 生态的兼容性请使用者自行评估；商业授权需另议。

## 2. `bettercomfyui_comfy/vendor/h3_director`

- 来源：MiniMax H3 导演台（vendored 副本）。
- 许可证：**Apache License 2.0**（见 `bettercomfyui_comfy/vendor/h3_director/LICENSE`，10963 B）。
- 发布说明/文档中如出现 "MIT" 属于历史错误，**以 Apache-2.0 为准**。
- 保留其原始 LICENSE、README/来源链接；本仓库未声明对其拥有版权。

## 3. 模型与提示词手册

- MiniMax H3 权重与官方 API 归 MiniMax 所有；本仓库不包含权重文件。
- `bettercomfyui/prompt/data/h3_concise_v1.txt` 来自用户提供的《MiniMax H3 提示词工程精简版》V1.0（2026-10），用于系统提示词；随包分发前请确认你拥有该文档的分发权。
- `bettercomfyui/prompt/data/h3_handbook_full.txt` 为 ROUND55 用户提供的历史完整手册文本，同上。

## 4. 其它依赖

- ComfyUI：GPL-3.0（如发布包引用/安装 ComfyUI，遵循其许可证）。
- comfy-kitchen、torch、transformers 等通过 pip 安装，遵循各自许可证；本发布包不重打包这些依赖。

## 5. 发布包注意

- 不包含 `config/user.yaml`、`projects/`、`output/`、模型、日志、API Key、SSH Key。
- 不包含真实服务器 IP/SSH 端口（文档已脱敏为 `your-gpu-host`）。
- 详细安全审计见 `docs/ROUND58_安全审计_v2.md`（脱敏版）。
