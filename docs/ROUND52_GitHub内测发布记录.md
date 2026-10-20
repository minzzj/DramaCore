# ROUND52 GitHub 内测版发布记录（2026-10-18 深夜）

## 一、发布结果

| 项 | 内容 |
|---|---|
| 仓库（Public） | **https://github.com/minzzj/DramaCore** |
| Release（预发布） | **https://github.com/minzzj/DramaCore/releases/tag/v0.1.0-internal** |
| Release 标题 | `v0.1.0-internal · 内测版（未完整 GPU 测试）| DramaCore / 剧核` |
| 附件 | `bettercomfyui_bundle_20261018_230944.zip`（10.72 MB，已上传 Release 资产） |
| Tag / commit | `v0.1.0-internal`（forced-updated 到含 requirements/版本统一/ROUND50-B 的提交） |
| 文件数 | 361 个（源码+测试+脚本+精选文档+工作流样例） |
| 许可证 | **AGPL-3.0 + Commons Clause**（修改必须开源；禁止商用） |

仓库内容包含：`bettercomfyui/`、`bettercomfyui_comfy/`（含 vendor 导演台与 MIT LICENSE）、`scripts/`、`tests/`、`config/user.yaml.example`、单节点工作流 JSON、README、LICENSE、requirements.txt，以及 docs（发布说明、ROUND50-A 审查、ROUND50-B 验收/发布评估、ROUND45/46/47/49 报告）。

**排除**：`config/user.yaml`（Key）、`.comfyui/`、`projects/`、`dist/`、模型权重、原始证据截图、内部 `文档/`、保留备用节点等。

## 二、发布前门禁（ROUND50-B 的 7 项）

- ✅ LICENSE（AGPL-3.0+Commons Clause 全文）
- ✅ README（内测声明/安装/使用/已知限制/回滚/许可证）
- ✅ requirements.txt（PyYAML/Pillow/numpy/imageio-ffmpeg/aiohttp）
- ✅ 版本统一：`bettercomfyui_comfy.__version__ = "0.1.0-internal"`
- ✅ 重新打包：bundle 20261018_230944（含最新 UI；已作为 Release 资产上传）
- ✅ Key 卫生：仓库/包内无 sk-* 真实密钥，仅 user.yaml.example；敏感信息扫描 0 命中
- ✅ UTF-8 安装验证：此前 `pytest tests/verify` = 298 通过/0 失败/1 未验证（GPU 项按未验证标注）

## 三、审查与验收结论（详见两文档）

- `docs/ROUND50-A_愿景与流程图_全量代码审查.md`：166 条要求（愿景 V-01~V-34 + 流程图 F1~F7）
  → ✅107 / ◐58 / ❌1；P0 阻断 5（GPU 未验证、默认值偏离、人工审核演示、4 维评分未闭环、AI 合规优化未实现）。
- `docs/ROUND50-B_完整验收与发布评估.md`：验收矩阵 166 条 → **通过 78 / 部分 55 / 未通过 5 / 未验证 28**；
  判定：**内测版 Go with caveats；对外公开发布 / GA → No-Go**。
- 未验证主因：目标 4090 已关机，当前构建无最终 GPU 全链回归（历史真机证据：真实 Key 185.3s、真实 UI 200.4s/final_ui 170.3s、2 段 22f+SG6 10.42s/248 帧/审核通过、ROUND49 真机 R_seg 四维有值）。

## 四、安全动作

- 发布用**临时 SSH key**（`dramacore-release-temp`）已从 GitHub 账号**删除**。
- 未创建 PAT/其他长期凭证；仓库内无任何密钥。
- ⚠️ 账号密码曾在对话中出现：**请尽快修改密码并开启 2FA**（本次自动化未使用密码做 git 推送，仅用于浏览器登录）。

## 五、GPU 机器恢复后的后续动作

1. 部署最新代码 + 生成 110 控件工作流（本地已验证）；
2. 用真实剧本跑完整链路（素材生成/审核/LOCK → 22f 连续 → 段级/整片审核 → 剪辑成片），记录耗时/显存/审核分数；
3. 把 Release 从「内测·未完整测试」更新为「已通过 4090-48G 真机回归」，并在 README/Release notes 更新证据；
4. 5090-32G 实机实测后补充机型支持说明。
