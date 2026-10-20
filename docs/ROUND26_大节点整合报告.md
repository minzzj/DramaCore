# ROUND26 报告：收敛成「一个大节点」——剧本进、导演组出

> **用户反馈（本轮原话）**：
> 「你为什么……把素材都出统一输出出来，这样怎么能行呢？那个导演节点不就有一个叫素材库的功能吗？
> 为什么不能这样连接呢？……做成一个大节点就可以了，插上模型，插上输出，然后就可以用了，
> 一个大节点，能理解什么意思吗？把它们整合起来，整合的要完整，而不是重新造很多东西，
> 或者把它生硬的接起来。……我要它更加的自主化自动化。」
>
> 完成：2026-10-17 23:00（Asia/Shanghai）。所有结论可复现，命令见第六节。

---

## 一、我上一轮错在哪（先认账）

ROUND25 我把「一次调用」做成了 **一个新节点 + 8 个输出 + 一串要手工接的线**：
`API 配置 → 剧本提示词节点 → 8 个输出分别接预览 → 再接到导演台`。

这正是用户批评的两种毛病：
1. **生硬地接起来**：中间多出节点，用户要自己理解、自己接线；
2. **重新造东西**：又多了一个 `ScriptPrompts` 节点，与「站在巨人肩膀上、只做必要编排」相悖。

并且用户指出的关键点是对的：**导演节点本来就有「素材库」这个能力**，提示词生成的结果应该
**自动流进素材库**，而不是作为 8 个文本输出甩给用户。

## 二、本轮设计：只留一个真正的大节点

### 改造前（ROUND25，被否掉）

```
[API配置] → [ScriptPrompts 8输出] → 8×[PreviewAny]
                    └─视频提示词→ [OneDirector] → [MiniMaxH3Director] → [CreateVideo] → [SaveVideo]
```
节点多、线多、每个输出都要用户自己接。

### 改造后（本轮）

```
[API配置]（可选，不接就自动读服务器配置）
    │
    ▼
┌──────────────────────────────────────────────┐
│ 一体化总控台（唯一大节点 BetterComfyUIEverything） │
│  剧本 ─→ ①读项目素材库生成"已有素材清单"并注入        │
│        ②一次 API 调用 → E1–E5 全量提示词            │
│        ③确定性匹配：复用/新建/疑似（自动登记素材库）    │
│        ④已 LOCK 素材全量挂每段（身份一致性）          │
│        ⑤组装 MMX_DIR_GROUP 导演组                  │
│  输出：导演组 / 分镜脚本 / 提示词JSON / 素材清单 / 审计报告│
└──────────────────────────────────────────────┘
    │ 导演组（MMX_DIR_GROUP）
    ▼
[MiniMaxH3Director] ← 插模型（UNET/CLIP/VAE）
    │ IMAGE/AUDIO/FPS
    ▼
[CreateVideo] → [SaveVideo]   ← 插输出
```

**用户只需要接两处**：模型接到导演台、导演台输出接到成片。
`W2_DeepSeek全自动` 就是最简形态：**12 个节点，其中 6 个是权重/出片链，1 个大节点 + 3 个预览**。

### 大节点内部做了什么（对应 .dot）

| dot 节点 | 在大节点内部 |
|---|---|
| A1 / A1B / A2 / A3 / A4 | 读项目素材库 → `build_existing_asset_manifest` → 原样注入系统提示词；空库自动走空分支 |
| B / B1–B5 | API 配置三根线（可接「API 配置」节点）；不接则读服务器配置（env / user.yaml / 原生设置） |
| C1–C6 | 系统提示词模板 5 选 1 / 自定义覆盖 / 输出语言 / 分镜粒度 / JSON+分隔符（auto）/ 风格标签 `@标签名` |
| D | AI 理解与拆解（一次调用） |
| E1–E5 | `bettercomfyui/prompt/script_build.py` 一次产出四类素材提示词 + 视频段提示词 |
| E6–E9 | `prompt.match.match_candidate` 确定性匹配：已存在→复用；不存在→登记；疑似→`suspects.json`（绝不覆盖） |
| G | 已 LOCK 素材（公共素材库）按 角色>场景>物品>分镜 优先级全量挂到每一段 |
| J / P | 段计划 → 每段 `MMX_DIR_GROUP` 组数据（prompt/duration/fps/refs/continuity） |
| Q_OUT | 输出「导演组」，直接接 `MiniMaxH3Director.r2v_groups` |

## 三、素材库整合（用户点名的"导演节点素材库功能"）

生成出的 E1–E4 提示词会**自动进入当前项目素材库**（可用 `写入素材库` 开关关闭）：

| 情况 | 行为 | 证据 |
|---|---|---|
| 同名素材已存在（归一化匹配） | 复用，**不覆盖、不重复建版本**（R2） | 验收 31 P11 |
| 不存在 | 新建 DRAFT 素材条目，note 里带生成提示词（等图像生成/审核后 LOCK） | E2E：临时项目新增 5 条 |
| 疑似重名（多个候选） | 进 `suspects.json` 待人工确认，**绝不自动覆盖** | 复用 match_candidate 既有能力 |
| 占位模式（无 Key/勾选占位） | **不写素材库**（避免演示数据污染真实项目） | E2E W0L：0 条写入 |
| 每类上限（G4=9） | 由 `AssetStore.create_asset(per_class_limit)` 拦，超限记录到审计 | 复用 ROUND19 已有能力 |

## 四、删掉了什么（不再造东西）

| 项 | 处理 |
|---|---|
| `BetterComfyUIScriptPrompts` 节点（ROUND25 我加的） | **删除**（源码 + 安装副本），节点总数 21 → **20** |
| 工作流里手工串的 `OneDirector` / `PromptBridge` / `Storyboard` / `AssetSlots` | 从 W0/W2 移除（节点类保留给别的场景，不删，避免破坏既有用户工作流） |
| `scripts/rebuild_prompt_workflows.py` | 删除，改为 `scripts/build_one_node_workflows.py` |
| 大节点内自写的 `_call_llm` / `_placeholder`（只出镜头） | 删除，改为复用编排层 `script_build`（一次调用 E1–E5） |

## 五、结果（实测证据）

### 1) 双解释器验收（ComfyUI 运行中）
```
系统 python -B -m pytest tests/verify -q → EXIT=0，通过=263 失败=0 未验证=1
.comfyenv  -B -m pytest tests/verify -q → EXIT=0，通过=252 失败=0 未验证=3
全量 pytest tests/                         → EXIT=0
```
（未验证项仍如实：/free 空闲无显存下降、venv 未装 playwright。）

### 2) 真 ComfyUI + 本地 mock API 端到端（不联网、不计费）
```
临时项目 76ed7205…：
P1（真实路径，5 节点）→ status=success
  ├ mock API 收到请求 = 1 次（断言：全程只允许 1 次）
  ├ 3 个预览节点可见：提示词JSON / 素材清单 / 审计报告
  ├ 提示词JSON = bettercomfyui.one-node/v1，含 CHARACTER/SCENE/PROP/SHOT + video_prompts
  ├ 临时项目素材库新增 5 条素材条目（= JSON 里 library.created=5）
  └ 审计写「API 调用次数：1 次」
W0L（占位体验，4 节点）→ status=success，0 次 API 调用，3 个预览，明确标注占位
14 条断言全部 PASS（含"只调用一次/四类素材/视频段/导演组/素材库登记/W0L 0 次"）
```

### 3) 安装与真浏览器
```
verify_install：插件逐文件哈希一致 / 巨人包在 / 工作流在 / 20 个节点可见（总节点 1032）/ 巨人节点可见
真浏览器（系统 Edge）打开 P1、W0L、W2、W0：节点错误 无、未连线输入 无、pageerror 无、我方 console.error 无
```

### 4) 四套工作流最终形态
| 工作流 | 节点 | 连线 | 权重节点 | 大节点 | 结果预览 | 用途 |
|---|---|---|---|---|---|---|
| W0_完整逻辑图全流程 | 13 | 16 | 6 | 1 | 3 | API 配置 → 大节点 → 导演台 → 出片（GPU） |
| W2_DeepSeek全自动 | 12 | 13 | 6 | 1 | 3 | **最简形态**：大节点（自动读配置）→ 导演台 → 出片 |
| P1_剧本→全量提示词（本机可跑） | 5 | 6 | 0 | 1 | 3 | 本机只看提示词/素材库结果 |
| W0L_本机可跑通体验版 | 4 | 3 | 0 | 1 | 3 | 无 Key 也能看的占位体验 |

## 六、复现命令

```powershell
cd '[PROJECT-ROOT]'
python -B -m pytest tests/verify -q
& .comfyenv\Scripts\python.exe -B -m pytest tests/verify -q
python -B scripts/verify_install.py
python -B scripts/build_one_node_workflows.py        # 需要 ComfyUI 在 8188
python -B scripts/browser_sim.py --workflow "W2_DeepSeek全自动.json"
```

## 七、怎么用（两步）

1. 右侧「Better ComfyUI」面板 → API 配置 → 填真实 DeepSeek 密钥（一次即可，三处同步）。
2. 打开 `W2_DeepSeek全自动` 或 `W0_完整逻辑图全流程`：
   - 剧本写进大节点「剧本」；
   - 大节点「导演组」→ 导演台 `r2v_groups`；
   - 导演台插模型（UNET/CLIP/VAE），导演台输出 → `CreateVideo` → `SaveVideo`。
   点队列即可。本机无 GPU 时先用 `P1` / `W0L` 看提示词与素材库结果。

## 八、仍未做 / 未验证（不粉饰）

- **真实 DeepSeek Key 调用**：当前无真实 Key（三处已清空测试假密钥），需用户填入后首次实测；
- **真实视频出片**：环境墙（本机 15.75GB 内存 / 4GB 显存），与代码无关；
- 大节点内「素材图像生成」仍需 GPU 机器上的图像工作流配合（本节点只登记提示词与挂 LOCK 素材）；
- `OneDirector` / `Storyboard` / `PromptBridge` 节点类暂留（其它工作流可能引用），后续可评估合并。
