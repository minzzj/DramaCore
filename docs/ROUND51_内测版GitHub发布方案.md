# ROUND51 内测版 GitHub 发布方案（待你确认后执行）

> 目标：把当前「剧核 DramaCore」单节点版作为 **v0.1.0-internal（内测·未完整测试）** 发布到你的 GitHub，
> 防止被抢发；许可证要求：**修改必须开源 + 禁止商用**。今晚完成除"推送需要凭证"之外的全部准备。
> 远端 4090 当前关机，**真实 GPU 完整测试未做**，发布物必须显著标注「内测/未完整测试」。

## 一、GitHub 凭证：**不能直接给密码**

- GitHub 已不支持密码推送 git；且密码在对话中出现不安全。
- 请改用 **Personal Access Token (PAT)**：
  - 路径：GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate new token；
  - 勾选 **repo**（公开/私有仓库都需要）；
  - 有效期建议 7 天，**发布完成后立刻 Revoke**；
  - 把 `ghp_REDACTED...` 发我（我只会用于本次 clone/push，不写入任何文件/不走日志）。
- 同时建议：**改掉刚在对话中发过的账号密码并开启 2FA**（我不会使用该密码）。
- 如果不想给 token：也可以你先在 GitHub 建好空仓库并把仓库地址+默认分支告诉我，我再给你一个**打包好的 zip + 逐条 git 命令**，由你本机执行推送（但今晚无法自动完成推送）。

## 二、许可证（按你的要求）

「修改必须开源 + 禁止商用」最贴合的组合：
- **AGPL-3.0**（网络使用也触发开源义务，修改必须开源）+ **Commons Clause**（禁止商业销售/商用）。
- 说明：该组合**不是 OSI 标准开源许可**，GitHub 会显示 "Other"；如果需要 GitHub 识别标准许可，只能纯 AGPL-3.0（但那样不禁止商用）。
- 请确认用：☑ **AGPL-3.0 + Commons Clause**（推荐，符合你的两条要求） 或 其他（告诉我）。
- 另外 vendored 的 MiniMaxH3 Director 保留其原 LICENSE（MIT），README 里注明第三方来源与许可。

## 三、发布内容（打包/推送范围）

**包含**：`bettercomfyui/`、`bettercomfyui_comfy/`（含 vendor 源码与 LICENSE）、`scripts/`、`tests/`、`docs/`（精简：发布说明/审查报告/验收清单）、`README.md`、`LICENSE`、`.gitignore`、`config/user.yaml.example`、工作流样例。
**排除（安全/隐私/体积）**：`.comfyui/`、`projects/`、`dist/*.zip`、`config/user.yaml`（含 Key）、`.env`/`*.key`、`docs/remote_evidence_4090/` 原始截图与远端报告（内部测试证据，先不公开；如需公开另行脱敏）、模型权重、运行日志、`__pycache__`。
**README 必须显著标注**：
1) 标题下第一行：**⚠️ v0.1.0-internal · 内测版 · 未完成完整 GPU 测试（4090 真机只跑过部分流程）**；
2) 已知边界 9 条（R_seg index/scores 字段、tiled 未接线、跨批原生 22 帧 pin 用尾帧近似、WebM 字幕+水印只烧水印、级联 1 层、转场仅淡入淡出、默认 480p 未内置超分、整片失败策略默认 proceed、审核为本地启发式）；
3) 安装/使用/模型要求/许可证/禁止商用/修改必须开源/联系方式。

## 四、今晚执行顺序（自动完成）

1. 完成 ROUND50-A 代码审查（已在补全流程图部分）→ 2. ROUND50-B 完整验收与发布评估；
3. 按审查结论修 P0（仅限不需要 GPU 的代码/文档问题；GPU 相关只标注不改）；
4. 生成发布目录 + README/LICENSE/CHECKLIST（内测声明）；
5. 本地 `git init` + 清理历史 + 首提交 + tag `v0.1.0-internal`；
6. **等你的 PAT** → 建仓库（或推到已有仓库）→ push（含 tag）；
7. 收尾：关闭定时任务、结束其他智能体任务、结束目标（发布完成后）。

## 五、需要你睡前回复的 3 件事

1. **GitHub PAT**（`ghp_REDACTED...`，repo 权限；或告诉我"你自己建仓库+本机推送"）；
2. 仓库名与可见性：例如 `DramaCore` / **Public**（内测版建议 Public 防抢发）；
3. 许可证确认：**AGPL-3.0 + Commons Clause**（推荐）还是其他。

收到 1–3 我今晚就执行到底；若你睡了没回，我会先完成 1–6 的全部本地准备（审查、README、LICENSE、git 提交、tag、发布包），**只差 push**，醒来给我 token 即可一键推送。
