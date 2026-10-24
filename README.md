# 剧核 DramaCore（Better ComfyUI）

> **版本：v0.2.0-internal（内测版）** · 许可：**CC BY-NC-SA 4.0**（允许下载/修改/再分发，**禁止商用**；修改后的作品必须以相同许可开源）
> **注意：本项目的默认「统一风格」与系统提示词中的风格锁均为示例模板，部署到你的项目时请自行修改或替换。**

**剧核 DramaCore** 是 ComfyUI 的「剧本进、成片出」大节点：
剧本 → 一次 LLM 调用 → 全部提示词 → 素材图生成/审核/锁定 → H3 导演台出片（段间连续）
→ 段级/整片审核 → 剪辑封装。导演台核心已随包 vendor，不依赖外部自定义节点。

---

## 核心变化（v0.2.0-internal）

- **系统提示词 V3.0「契约对齐」**：`h3_v3_contract` 作为唯一系统提示词（37,457 字），不再自动追加旧规则块；
- **移除自动调色硬校正（colorfix）**：保留模型自然光影，不再把每个镜头强行拉到固定亮度；
- **光影按场景自然决定**：白天自然明亮、夜晚自然偏暗（保留光源与暗部细节）、密室可低照度；禁止一刀切提亮/压暗；
- **「活人感」要求**：真实皮肤纹理、自然光影过渡、呼吸/眨眼等微动作，减少塑料/CG 感；
- **H3 官方提示词格式**：三字段、`[Shot]` 标记、运镜「类型+幅度+速度」、`(S1)(S2)`、`<d>[中文]</d>` 对白；
- **前端自动兼容**：旧工作流参数自动修复，不再弹「参数与插件节点不一致」拦截框；
- **稳定出片参数**：默认【真实参数版】1344×768 / 8 步 / ref2v 8step LoRA；【快速4步版】1152×640 / 4 步。

## 安装

1. 安装 ComfyUI（开发验证 0.41 / 目标机 0.33）；
2. 把 `bettercomfyui_comfy/` 复制到 `ComfyUI/custom_nodes/`；
3. 把 `bettercomfyui/` 放到可被 import 的位置（或与插件同级的仓库根目录）；
4. 启动 ComfyUI 时设置 `BETTERCOMFYUI_HOME=<本仓库目录>`；
5. 复制 `config/user.yaml.example` → `config/user.yaml`，填写 API Key（也可在面板里配置）；
6. 重启 ComfyUI。

**模型要求**：H3 UNET / CLIP / 视频/音频 VAE / Turbo LoRA；素材生图需 SDXL 类模型。模型权重不在本仓库。

## 完整工作流

| 文件（`bettercomfyui_comfy/workflows/`） | 用途 |
|---|---|
| `【新版】剧核_单节点_剧本到成片_快速4步版_20261024.json` | 快速测试（1152×640 / 4 步 / ref2v 4step / 段间连续） |
| `【新版】剧核_单节点_剧本到成片_真实参数版_20261024.json` | 正式出片（1344×768 / 8 步 / ref2v 8step / 段间连续） |
| `00_【单节点】剧本→成片（无外部依赖）.json` | 单节点完整流程（细节参数可改） |
| 其它 `W*/S*/P*/00_*` | 分步体验/素材/审核/剪辑等子流程 |

> 完整说明见 [`docs/工作流说明_完整工作流.md`](docs/工作流说明_完整工作流.md)。

## 自定义风格（必须修改！）

**默认风格是示例，请按你的项目自行修改或替换：**

1. 节点「统一风格」参数（推荐）：改成你的项目风格词；
2. 系统提示词 V3.0 的 `PART C · C1 风格锁`：替换为你的风格锁；
3. 导演台面板 →「提示词」→ 可查看/替换当前生效的系统提示词（默认 `h3_v3_contract`）。

详见 [`docs/自定义风格与系统提示词.md`](docs/自定义风格与系统提示词.md)。

## 配置与密钥安全

- 默认配置：[`config/default.yaml`](config/default.yaml)；示例：[`config/user.yaml.example`](config/user.yaml.example)；
- **本仓库不含任何 Token/API Key**；`config/user.yaml` 已被 `.gitignore` 排除；
- 请勿把 Key 写进工作流 JSON、截图或 Issue。

## 许可证

本项目采用 **CC BY-NC-SA 4.0**：
- ✅ 允许下载、查看、修改、非商业再分发；
- ✅ 修改后的作品必须采用相同许可（ShareAlike）并保留署名（Attribution）；
- ❌ 不允许商业使用（NonCommercial）。
- 注意：因含「禁止商用」条款，本许可**不是 OSI 定义的开源许可**，属于「源码可见 + 非商业共享」许可。

完整法律文本见 [LICENSE](LICENSE)。

## 运行截图

![ComfyUI 工作流](docs/images/comfyui-workflow.png)

![导演台 / V3 提示词面板](docs/images/director-panel-v3.png)

![GitHub 仓库主页](docs/images/repo-main.png)

## 文档

- [安装部署](docs/ROUND57_安装部署指南.md)
- [工作流说明（完整）](docs/工作流说明_完整工作流.md)
- [自定义风格与系统提示词](docs/自定义风格与系统提示词.md)
- [面板配置说明](docs/剧核_DramaCore_面板配置说明.md)
- [发布说明 v0.2.0](docs/发布说明_v0.2.0-internal.md)
- [更新日志](CHANGELOG.md)
