# ROUND41 单节点（剧本→成片）首版：真机验证报告

> 交付：`BetterComfyUIOneStop`（单节点，无外部自定义节点依赖）
> 真机：远端 RTX 4090 48G，ComfyUI 0.33.0，`http://your-gpu-host:8188`

## 一、做了什么（按用户要求）

用户原话：「不是要大节点吗？不是用自己的源码吗？你也没弄啊。为什么要用其他节点做依赖呢？」

本版回应：

1. **只有一个节点**：`BetterComfyUIOneStop`，工作流里仅它一个；输入剧本/模型文件名/参数，输出成片。
2. **自己的源码**：把导演台核心源码**复制进本插件** `bettercomfyui_comfy/vendor/h3_director/`
   （1.49MB，保留原 MIT LICENSE），不再要求安装 `ComfyUI_MiniMaxH3_Director` 等外部包。
3. **无外部依赖**：节点内部完成
   - 剧本 → 一次 API（或占位）→ E1–E5 提示词 → 素材库登记（复用本包 `BetterComfyUIEverything`）；
   - 视频提示词 → 导演台 r2v 组（本包 `fusion.make_r2v_group`）；
   - 按文件名加载 UNET/CLIP/视频VAE/音频VAE/LoRA（ComfyUI 自带 loader，按各版本 INPUT_TYPES 自动适配）；
   - 调用 vendored 导演台核心出片（含音频）；
   - 编码 mp4 → 完整性/质量审核 → 导出到项目 output 与 `ComfyUI/output/剧核成片/`（网页可播放整片）。

## 二、真机验证结果（占位提示词模式，不消耗 API）

| 项目 | 结果 |
|---|---|
| 节点注册 | 远端 `BetterComfyUIOneStop` OK（output_node=True；本包共 25 个节点） |
| 运行 1 | 131.65s（含加载模型），出片 105.9s → 发现音轨缺失 bug |
| **运行 2（修复后）** | **119.31s（含加载模型），出片 94.2s** → 音轨=有，审核通过 |
| **运行 3（路径修复后）** | **119.20s，出片 93.8s** → 音轨=有，审核通过 |
| 输出规格 | 124 帧 / 864×480 / 24fps / 5.167s / 含音频 |
| 审核（节点内） | 完整性=True；质量 叙事100 / 音画94.82 / 节奏97.01 / 跳变100，**通过** |
| 本地复核 | `scripts/quality_audit.py`：叙事100 / 音画100 / 节奏97.0 / 跳变100 → **通过** |
| 成片导出 | `ComfyUI/output/剧核成片/大节点成片_2.mp4`（网页可预览整片）；项目 output 同名 |
| 证据 | `docs/remote_evidence_4090/one_stop_成片_第二次.mp4`、`one_stop_quality_第二次.md`、`one_stop_review_*.json` |

## 三、使用方法

1. 远端 UI 刷新 → 工作流菜单 **`【剧核】单节点_剧本到成片`**（只有一个节点）；
2. 节点上填剧本、api_key（或留空读配置）、模型文件名（默认已填远端可用文件）；
3. 想先验证结构：勾「使用占位结果」（不调用 API，但出片是真的）；
4. 点运行 → 节点输出「成片路径/审核报告/提示词JSON/审计报告」，预览区直接播放整片。

详细参数见 `docs/单节点_剧本到成片_使用说明.md`。

## 四、当前边界（如实记录）

- **素材图生成尚未收进节点**（v0.1）：目前只做提示词→素材登记；素材图生成/审核仍用
  `S3_素材生成_4090适配版`（已真机验证生成→入库→审核）。下一步把素材生图+审核也并入单节点。
- **真实 API 路径尚未端到端验证**：需要用户 DeepSeek Key；本次验证的是「占位提示词→真实出片→审核→成片」
  全链路，以及节点内部一次 API 的代码路径（Everything 节点已在 ROUND25 真机验证过一次 API）。
- 审核是本地启发式（真实帧统计，非语义模型），报告中如实标注。
- 480p 出片为默认（快）；1080p 走「480p 生成+模型超分」路线（见 ROUND39）。

## 五、相关文件

- 节点：`bettercomfyui_comfy/one_stop_node.py`
- vendored 源码：`bettercomfyui_comfy/vendor/h3_director/`（含 LICENSE）
- 单节点工作流：`bettercomfyui_comfy/workflows/00_【单节点】剧本→成片（无外部依赖）.json`
- 远端 UI 工作流：`【剧核】单节点_剧本到成片.json`
