# 架构修正与融合设计（INTEGRATION）

> **本文修正一处根本性架构错误。** 由用户在 2026-10-17 凌晨指出：
> 「你没有发现你这里不是都不是那个导演节点。功能不全也不强大……要把他们融合起来，真正的实现自动化。」
>
> 结论：我此前把 **AI 视频生成放在自己的编排器里（mock 落地）**，把用户给的 4 个导演节点
> 当成「候选组件」审计了一遍就放过 —— **这是把核心引擎降级成了可选依赖。**
> 本文给出正确架构、证据、以及融合方案。

---

## 一、我错在哪（有证据）

### 1.1 真实存在的导演台是什么规模

| 包 | 实测规模 | 性质 |
|---|---|---|
| `H3-Chinese-Director` | **181 节点 / 218 连线** 的生产工作流（`workflows/current/V29_chinese_director_production_8197.json`，344.8 KB）；`custom_nodes/local_author/8197_production_line/` 下 **141 个文件** | **完整生产线**：六段剧情、原生 AV latent 接续、PreBake 缓存、TE 卸载、坏段检测、二采修复、自动合并 |
| `ComfyUI_MiniMaxH3_Director` | `nodes/director.py` 等 40+ 模块，`director/executor_core.py` 单个文件 86.6 KB | **真正的生成引擎** |
| `ComfyUI_MiniMaxH3_Smart_Director` | `nodes.py` 111.5 KB + 37.4 KB 测试 | AI 语义 → 导演参数桥接 |
| `ComfyUI-MiniMaxH3-ChainDirector` | `chain.py` 23.1 KB | 自动分段 + 首帧硬锁定接力 |

### 1.2 生成引擎的真实接口（`nodes/director.py`）

```python
class MiniMaxH3Director:
    RETURN_TYPES = ('IMAGE', 'AUDIO', 'FLOAT', 'INT', 'IMAGE', 'STRING', 'IMAGE', 'IMAGE')
    RETURN_NAMES = ('images', 'audio', 'fps', 'frame_count',
                    'source_images', 'report', 'images_pre_refine', 'images_pre_face_refine')
    FUNCTION = 'execute'
```

**它输出的是真实视频帧（IMAGE 批次）+ 音频（AUDIO）** —— 也就是说「真实生成」这件事
**本来就由这套节点承担**，根本不是我该 mock 的地方。

它的输入（`INPUT_TYPES`）：

| 类别 | 输入 | 类型 | 说明 |
|---|---|---|---|
| required | `model` | MODEL | MiniMax H3 UNET（`UNETLoader`） |
| required | `video_vae` | VAE | `minimax_h3_video_vae` |
| required | `audio_vae` | VAE | `minimax_h3_audio_vae`（r2v/v2v/rv2v 必需） |
| required | `clip` | CLIP | `CLIPLoader type=minimax`（qwen3vl） |
| required | `*director_timeline_required_inputs()` | — | 时间轴/采样等必需项 |
| **optional** | **`i2v_groups`** | **`MMX_DIR_GROUP`** | **外部图生视频组；连上后覆盖 UI 卡片（外部优先）** |
| **optional** | **`r2v_groups`** | **`MMX_DIR_GROUP`** | **外部参考生视频组；连上后覆盖 UI 卡片** |
| optional | `semantic_bridge` / `selflift` / `refine` / `face_refine` | 各自类型 | 增强链 |
| optional | `steps` / `bd_grp_advanced` … | INT/BDGROUP | 采样参数 |

### 1.3 融合的接缝已经存在（这是关键发现）

`i2v_groups` / `r2v_groups` 是 `MMX_DIR_GROUP` 类型，tooltip 明确写着
**「When connected, overrides UI cards for execution (external priority)」**。

**这意味着导演台官方就设计好了「被外部程序驱动」的接口。**
我的编排层不需要碰采样，只需要**产出正确的 `MMX_DIR_GROUP`**。

### 1.4 `MMX_DIR_GROUP` 的载荷契约（实测）

组节点（`nodes/director_groups.py`）：

```python
class MiniMaxH3DirectorGroupImageToVideo:   # RETURN_TYPES = (MMX_DIR_GROUP,)  FUNCTION='pack'
class MiniMaxH3DirectorGroupReferenceToVideo:
class MiniMaxH3DirectorGroupsCombine:       # RETURN_TYPES = (MMX_DIR_GROUP,)  FUNCTION='combine'
```

每段的字段结构（`director/pack.py::_group_json`，line ~417）：

```python
{
  "id": str, "prompt": str, "negativePrompt": str,
  "durationSec": number, "frameCount": int, "length": int, "start": number,
  "taskType": str,                       # t2v / i2v / fl2v / r2v / v2v / rv2v
  "refs": list,                          # 参考图
  "refAudios": list, "refVideos": list,  # 参考音频 / 视频
  "continuityFromPrev": bool,            # 从上段接续
  "refImageSize": ..., "loras": list,
  "genImage": {"imageFile": str, "fileName": str},   # 可选
  "imageFile": str,                                  # 可选
}
```

---

## 二、正确架构

```
┌─────────────────────────────────────────────────────────────────────┐
│  我的编排层（本项目自研——方案明确「只自研编排/状态/校验/审核/界面」）      │
│                                                                     │
│  store/     项目隔离、文件锁、原子写、素材库、检查点                     │
│  prompt/    剧本拆解、程序确定性匹配、疑似重复队列、增量注入               │
│  review/    版本级审核、LOCK、CNT1、H_arb 仲裁、唯一提交点               │
│  director/  段计划、CNT2/CNT3、级联终止                                │
│  pipeline/  拼接、整片审核、交付（R_full 在 S_out 前一次）               │
│                                                                     │
│  职责：**决定「拍什么、用哪一版素材、审没审过、要不要重做」**              │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  产出 MMX_DIR_GROUP（i2v_groups / r2v_groups）
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  导演台引擎（用户给的开源节点，**不重写**）                              │
│                                                                     │
│  ComfyUI_MiniMaxH3_Director       MiniMaxH3Director.execute()        │
│    ├─ 输入 model / video_vae / audio_vae / clip                       │
│    ├─ 输入 i2v_groups / r2v_groups  ← 我们的接缝                      │
│    └─ 输出 images(IMAGE) + audio(AUDIO) + fps + frame_count + report   │
│                                                                     │
│  ComfyUI_MiniMaxH3_Smart_Director  中文剧本 → 导演参数（AI 语义桥接）    │
│  ComfyUI-MiniMaxH3-ChainDirector   自动分段 + 首帧硬锁定接力            │
│  H3-Chinese-Director V29           六段生产线 / 接续 / 缓存 / 二采      │
│  ComfyUI-DNNodes                   素材卡 → MMX_DIR_GROUP 打包          │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  images + audio
                            ▼
                   CreateVideo / SaveVideo  →  成片
                            │
                            ▼
              我的编排层接管：段级审核 → 级联 → 拼接 → R_full → 交付
```

**分工一句话**：**导演台负责「怎么拍」，我负责「拍什么、用哪个版本、能不能过审」。**

---

## 三、我原来的做法要改什么

| # | 原来（错） | 改成（对） |
|---|---|---|
| 1 | `demo.py` 用 ffmpeg 合成占位视频当"分段视频生成" | **产出 `MMX_DIR_GROUP`**，交给 `MiniMaxH3Director` 执行；占位只保留为「无权重时的降级路径」，且必须显式标注 |
| 2 | `executors/` 把导演台当成「可插拔执行器之一」 | 导演台是**主路径**；`ComfyUIHttpExecutor` 负责**驱动图**（提交含 Director 节点的工作流） |
| 3 | 插件节点只有"项目/运行/审核/手动素材/结果/体检" | 增加**融合节点**：`素材卡打包 → 组 → 交给导演台`，并提供**可直接拖入的 H3 工作流模板** |
| 4 | `phase2.py` 把"自动抓取/发布"当第二阶段 | 不变（这部分用户确认是第二阶段）；但要补一句：**真实生成不是第二阶段，是第一阶段的主路径** |
| 5 | 文档说「真实生成未验证（缺权重）」 | 补上正确表述：**缺的是权重与外部节点，不是集成代码**；集成路径已明确为 `MMX_DIR_GROUP` |

---

## 四、融合的落地顺序

### 第 1 步：`MMX_DIR_GROUP` 生产者（本项目）
把 `Segment` + 已 LOCK 的素材版本 → `MMX_DIR_GROUP` 载荷。
**只有已取得 LOCK 的版本才允许进组**（R3 门禁在此处生效）。

### 第 2 步：融合节点（ComfyUI 插件）
新增节点，让用户在 ComfyUI 里就能把「我的编排层」接到「他的导演台」：

| 新节点 | 作用 |
|---|---|
| `BetterComfyUITimeline` | 项目 + 剧本 → 段计划 → **输出 `MMX_DIR_GROUP`**（连到 Director 的 `i2v_groups`/`r2v_groups`） |
| `BetterComfyUIAssetCard` | 素材库条目 → 参考图（连到组节点的 `ref_image_*`） |
| `BetterComfyUIReviewIn` | 接收 Director 的 `images`/`audio` → 存为段版本 → 触发段级审核 → 记录 CNT2/CNT3 |

### 第 3 步：可拖入的融合工作流模板
`workflows/fusion_h3_minimal.json`：**我的节点 + 他的导演台 + 标准加载器**，用户拖进去填剧本就能跑。

### 第 4 步：安装 H3 依赖
按 `docs/AUDIT_NODES.md` 的结论与 H3 的 `THIRD_PARTY_NOTICES.md` 装齐外部节点；
权重放到 `.comfyui/models/`。

---

## 五、仍然成立的部分（不推翻）

之前做的东西**不是白做**，但定位要修正：

| 资产 | 新定位 |
|---|---|
| `store/` 项目隔离 + 文件锁 + 原子写 | ✅ 完全成立，且是导演台**没有**的能力（审计已确认 16 个包无一实现 R1–R5） |
| `review/` 版本级 LOCK + CNT1 + 仲裁 | ✅ 完全成立 —— 这是「素材版本级审核」的地基 |
| `prompt/` 确定性匹配 + 疑似重复队列 | ✅ 完全成立 |
| `director/` CNT2/CNT3 + 级联终止 | ✅ 成立，但要改成**驱动导演台**而非自己生成 |
| `pipeline/` 拼接 + R_full + 交付 | ✅ 成立（但整片拼接也可交给 `ChainDirector` 的拼接能力） |
| `demo.py` 的 mock 闭环 | ⚠️ **降级为「无权重时的降级路径」**，不再是主路径；文档已如实标注 |
| 15 条验收 | ✅ 仍然通过，但**不覆盖导演台融合** → 需要新增验收项 |

> 审计结论仍然有效且更显重要：**16 个开源包无一实现 R1–R5（无 content_hash / LOCK / 项目隔离 / 计数语义）**，
> 所以「编排层自研」不是重复造轮子 —— **但生成必须用他们的。**

---

## 六、待研究项（已派研究任务）

- [ ] ChainDirector / SmartDirector 的 `INPUT_TYPES` 完整结构与输出结构
- [ ] H3 V29 的 schema 契约（`v28_schemas.py` 与 `test_v28_schemas.py`）
- [ ] `H3OperationSelector.MODES` 六个状态的精确取值
- [ ] 六段剧情的数据结构与"组"的字段
- [ ] V29 工作流依赖的**外部节点清单**（决定换机器要装什么）

---

_本文件由 Lead 于 2026-10-17 凌晨撰写，用于纠正架构方向。_
