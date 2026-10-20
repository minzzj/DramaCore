# 剧核 DramaCore（Better ComfyUI）

> ⚠️ **v0.1.3-internal · 内测版 · 未完成完整 GPU 测试**
> 本版本可用于内测与预览，但**尚未在目标 GPU 机器完成完整端到端验收**（远端 4090 已关机）。
> 请勿将其视为稳定生产版本；重要项目请自行充分测试。

**剧核 DramaCore** 是一个 ComfyUI 大节点：**剧本进、成片出**。
单节点完成「剧本 → 一次 API → 全部提示词 → 素材生图/审核/锁定 → 导演台全功能出片（含 22 帧段间连续）
→ 段级/整片审核 → 剪辑封装（转场/字幕/水印/封面）」，不依赖外部自定义节点（导演台核心源码已随包 vendor）。

---

## 特性（当前版本）

- **一个节点** `BetterComfyUIOneStop`：剧本输入 → 最终成片，全流程自动；
- **一次 API 调用**：DeepSeek 拆解剧本（E1–E5 素材/分镜/视频提示词），默认严格 JSON；
- **素材库**：角色/场景/物品/分镜参考图，生成 → 候选审核（CNT1）→ LOCK 版本（素材ID+版本号+内容哈希）→ 挂参考图；
- **导演台**：时间轴分段、首/尾帧、参考视频/音频、22 帧段间连续（5/22/39/56 档位 + 音频上下文 24）、六种任务模式；
- **审核**：段级 R_seg + 整片 R_full（叙事/音画/节奏/跳变四维，本地启发式计算）、边界相似度、自动返工/级联；
- **剪辑**：Finalize 拼接 + 转场（淡入淡出）+ 自动字幕 + 文字水印 + 首帧封面 + MOV/WebM；
- **长片**：段数可到 360（约 1 小时@10s），分批生成 + 断点续跑；
- **项目管理**：项目选择/新建、素材库复用（换章节防人物漂移）、资产/提示词/成片清单；
- **UI**：中英双语、剧本首屏大输入框、自动生成区蒙版 + 手动接管、审核结论可视化、22 帧连续可视化；
- **开源安全**：不含任何 API Key；配置示例见 `config/user.yaml.example`。

## 安装

1. 安装 ComfyUI（开发验证 0.41 / 目标机 0.33）；
2. 将 `bettercomfyui_comfy/` 复制到 `ComfyUI/custom_nodes/`；
3. 安装 Python 依赖：`pip install -r requirements.txt`（如无该文件，按 ComfyUI 自带环境即可，插件主要依赖 ComfyUI 原生包）；
4. 启动 ComfyUI 时设置 `BETTERCOMFYUI_HOME=<本仓库目录>`（用于读取配置与项目数据）；
5. 将 `config/user.yaml.example` 复制为 `config/user.yaml`，填入你的 API Key（或在面板里配置）；
6. 重启 ComfyUI → 工作流菜单选择 `bettercomfyui_comfy/workflows/00_【单节点】剧本→成片（无外部依赖）.json`。

**模型要求**：H3 UNET / CLIP / 视频/音频 VAE / Turbo LoRA；素材生图需 SDXL 类模型（如 RealVisXL）。
模型权重**不在本仓库**，请自行按许可下载。

## 使用

1. 打开 ComfyUI → 选择上面的工作流；
2. 在节点首屏 **① 粘贴剧本**（可点「填入示例剧本」）；
3. 选择/新建项目（想复用旧项目素材就选旧项目）；
4. 确认 API Key（面板配置或节点内填写），点画布顶部 **运行**；
5. 结果：节点审核结论 + 成片（`ComfyUI/output/剧核成片/`，同时写项目目录）。

## 已知限制（v0.1.3-internal）

1. **未完成完整 GPU 真机验收**（本机仅 CPU 预览 UI；远端 4090 关机）；
2. R_seg JSON 的 `index/scores` 字段显示可能为空（分数本身有效）；
3. `tiled 采样` 参数已暴露但未接线（不改 vendor 的边界）；
4. 跨批次**原生 22 帧 pin 不成立**：跨批用「上一批尾帧作参考图」近似，审计会标注 `last_frame_ref`；
5. 级联返工深度默认 1 层；转场仅「淡入淡出」；WebM 同时字幕+水印只烧水印；
6. 默认 480p（快速预览），1080p 需手动调参；未内置超分；
7. 整片失败策略默认 `proceed`；
8. 审核为**本地启发式**（真实像素统计），非语义大模型评分；
9. 5090-32G 未实测，不承诺该机型。

## 许可证（重要）

本项目采用 **AGPL-3.0 + Commons Clause**：
- **修改必须开源**：分发/网络提供服务时必须按 AGPL-3.0 提供对应源码；
- **禁止商用**：不得直接或间接销售本软件或其衍生服务（Commons Clause）；
- 详见 [LICENSE](LICENSE)。
- **注意**：AGPL-3.0 + Commons Clause 不是 OSI 标准开源许可。若需商业授权，请单独联系作者。
- 第三方组件：`bettercomfyui_comfy/vendor/h3_director/` 来自 MiniMaxH3 Director，保留其原始 Apache-2.0 许可（见该目录 LICENSE）。

## 致谢

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
- MiniMax H3 Director（vendored，遵循其许可）
- 所有开源社区贡献者

---

# DramaCore (Better ComfyUI) — Internal Test Build

⚠️ **v0.1.3-internal — NOT fully GPU-tested.** Use at your own risk; not a stable release.

One ComfyUI node: script → prompts → assets (generate/review/LOCK) → H3 director (with 22-frame segment continuity)
→ segment/full review → final cut (transitions/subtitles/watermark/cover). Core director source is vendored; no external custom nodes required.

See Chinese section above for install/usage/known limitations. License: **AGPL-3.0 + Commons Clause** (modifications must stay open source; **commercial use prohibited**).
