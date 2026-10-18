# 剧核 DramaCore 发布说明 v0.1.0-internal

> ⚠️ 内测版：**未完成完整 GPU 真机验收**（目标 4090 已关机）；本说明与代码一致，GitHub Release 见仓库 Release 页。

> → 导演台全功能出片（含 22 帧段间连续）→ 段级/整片审核 → 剪辑封装 → 成片）。
> 单节点、不依赖外部自定义节点（导演台核心源码已 vendor 进本插件，保留原 MIT LICENSE）。

## 一、支持环境

| 项 | 要求 |
|---|---|
| ComfyUI | 0.33.0（本机开发为 0.41，两端均已验证节点注册） |
| GPU | **RTX 4090-48G 已实测**；5090-32G 未实测（条件项，发布说明限定 4090-48G 或标注 32G 待测） |
| 显存实测 | 单段峰值 ~44.8–46.2GB；2 段 ~28.0–29.6GB（分批策略） |
| 模型 | H3 UNET（`MiniMax_H3_Ref2VA*`）、CLIP（`qwen3vl_32b_minimax_h3_int8*`）、视频/音频 VAE、可选 Turbo LoRA；素材生图用 SDXL（如 RealVisXL_V5.0） |
| 外部依赖 | **无额外自定义节点**；仅 ComfyUI 自带 loader + ffmpeg/Pillow（插件内置 vendor 源码） |

## 二、安装（干净机）

1. 解压发布包 `bettercomfyui_bundle_*.zip` 到目标机任意目录（如 `/data/BetterComfyUI`）；
2. 复制 `bettercomfyui_comfy/` 到 `ComfyUI/custom_nodes/`；
3. 启动 ComfyUI 时设置 `BETTERCOMFYUI_HOME=<发布包目录>`（用于读取配置与项目）；
4. （可选）把 `config/user.yaml.example` 复制为 `config/user.yaml` 填 API Key（面板也能填）；
5. 重启 ComfyUI → 工作流菜单选 `【剧核】单节点_剧本到成片.json`（随包提供）→ Ctrl+F5 刷新。

## 三、最短使用路径（人工操作）

1. 打开 ComfyUI；
2. 右侧「剧核 / DramaCore」面板 → API 配置（Key/模型/Base）→ 保存（或直接改 `config/user.yaml`）；
3. 工作流菜单选 `【剧核】单节点_剧本到成片.json`（只有一个节点）；
4. 节点里**只填「剧本」**（Key 已在面板/配置里则不用填）；
5. 点「运行」→ 节点预览/输出区看审计与成片；成片同时导出到 `ComfyUI/output/剧核成片/`。

多段/长片：调「段数」或「目标总时长_分钟」（上限 360 段≈1 小时@10s），「每批段数」控制显存，
「断点续跑」开启后同参数重跑会跳过已完成批次。22 帧段间连续在节点顶部「① 22 帧段间连续」区开关/调档。

## 四、真机实测证据（RTX 4090-48G）

| 场景 | 结果 |
|---|---|
| 真实 Key API 全链 | success 185.3s；**API 1 次**；素材 9 候选→LOCK 3/挂图 3；R_full 97.215 通过；显存 46176MB |
| 真实 Key + 真实 UI 点击（只填剧本→点运行） | success 200.4s；API 1 次；R_full 98.06 / R_seg 89.77；显存 44864MB |
| 2 段（22f 连续 + 转场/字幕/水印/封面/MOV） | 成片 **10.42s / 864×480 / 24fps / 含音频**；完整性 True；审核通过；R_seg 94.49/87.38；R_full 95.26；显存 29600MB |
| 长片分批/断点续跑/自动返工 | 检查点写入、参数变化作废旧检查点、返工 2 轮均通过（真机审计） |

## 五、明确边界（发布说明必须保留）

1. 审核为**本地启发式**（画质/构图等真实像素统计），非语义模型；一致性/合规维度 skipped 会如实标注；
2. R_seg 结果 JSON 的 `index/scores` 已在 ROUND49 修复（旧历史数据可能为空）；
3. 跨批次**原生 22 帧 pin 不成立**（vendor 限制）：跨批用「上一批尾帧作参考图」近似，审计标 `last_frame_ref`，不冒充原生；
4. `tiled 采样` 参数已暴露但未接线（不改 vendor 的边界）；
5. 级联深度仅 1 层；转场仅「淡入淡出」；WebM 同时字幕+水印时只烧水印；
6. 默认 480p 生成（未内置超分；1080p 建议 480p+模型超分路线，见 ROUND39）；
7. 整片失败策略默认 `proceed`（可配 block/degrade）；
8. 5090-32G 未实测，不承诺该机型；
9. 长片「一次约一小时」为设计能力（360 段分批+续跑），未做整小时真机端到端（受时间/显存限制），需按批次实测。

## 六、开源与安全

- 发布包**不包含** `config/user.yaml`（含 API Key），仅含 `config/user.yaml.example`；
- `.gitignore` 已忽略 `config/user.yaml`、`config/user.yml`、`dist/*.zip`；
- 仓库内 vendored 导演台源码保留原 **MIT LICENSE**（`bettercomfyui_comfy/vendor/h3_director/LICENSE`），开源时随包保留；
- 测试用 Key 曾在对话中出现，**发布前请在 DeepSeek 控制台轮换**；生产环境请用面板/配置文件注入，不要写进工作流或文档。

## 七、回滚方案

- 插件回滚：保留旧版 `bettercomfyui_comfy` 目录备份，替换后重启即可；节点类型 ID 未改变，旧工作流兼容；
- 配置回滚：`config/user.yaml` 单独备份/还原；
- 远端发布：保留一份旧 bundle zip 与 `custom_nodes/bettercomfyui_comfy` 备份目录（发布前打快照）。


## 八、健壮性说明（真实 API 引用校验）

- 真实 API 返回的剧本若引用了不存在的素材（`asset_refs/first_frame_ref/last_frame_ref`），
  **不会整次失败**：这些无效引用会被**丢弃并逐条告警**（见节点审计「模型输出引用了不存在的素材，已丢弃该引用」），
  其余结构继续执行；单次 API 约束不变。
- 该行为只作用于真实 API 路径；`validate_script` 默认仍是严格模式（测试/直接调用不会放松 R1）。
- 最终 UI 验收（新导演台 UI + 真实 Key + strict_json）：**success 170.3s、API 1 次、完整性 True、R_full 97.63 通过**，
  证据 `docs/remote_evidence_4090/final_ui/`。
