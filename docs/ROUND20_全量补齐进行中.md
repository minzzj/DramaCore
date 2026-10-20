# ROUND20：全部内容补齐 + 精准测试（进行中）

> 用户本轮要求（四部分）：
> **A** 补齐图一剩余 7 项；**B** 模拟浏览器操作 + 模型边界打桩的精准测试；
> **C** 新逻辑：阶段化显存调度（DeepSeek 出提示词 → 批量素材 → 卸载 → 批量视频 →
> 卸载 → 换审核模型 → 统一审核 → 不合格统一重生成，最大化 5090 32G）；
> **D** 完成后重读文档/逻辑图/愿景，补齐剩余。
>
> **验收三态：134 通过 / 0 失败 / 0 未验证**（系统 python 与 `.comfyenv` 双解释器一致）。

---

## 一、环境侦察结论（决定方案形态）

| 项 | 结果 |
|---|---|
| DeepSeek 巨人包 | **都在**：`保留备用的节点\ComfyUI-Storyboard-LLM`、`ComfyUI-DeepSeek-API-connector` |
| `POST /free` | **HTTP 200** —— 模型卸载 API 可用，阶段化显存调度**技术上可行** |
| 真实浏览器 | playwright 已装但 **chromium 未下载**（需网络）→ UI 测试走 **HTTP 等价层 + DOM 层** |
| 可用权重 | UNET 只有 `minimax_h3_ref2va` / `fl2va`；**无素材图模型、无审核模型** → 必须打桩 |
| 当前设备 | `cpu`（无 GPU）→ 真实生成不可能，显存调度用**理论计算 + 探针**验证 |

---

## 二、A 部分进度

| # | 项 | 状态 |
|---|---|---|
| A1 | **O7 背景音乐** | ✅ **完成**（详见下） |
| A2 | SG5 X 返工加料 | ⬜ 待做 |
| A3 | SG1 界面接通提示词（DeepSeek） | ⬜ 待做 |
| A4 | C6 风格标签库 / G2 素材命名 / score_dimensions 分维度 | ⬜ 待做 |
| A5 | save_vram_all / task_timeout_s / comfyui_endpoint | ⬜ 待做（并入 C 的阶段调度） |

### A1 背景音乐：做了什么

**修前**：`bgm` / `背景音乐` 全工程**命中 0 处**（愿景自己标注"后续需提进日程"的待办）。

**实现**（`bettercomfyui/pipeline/core.py`）：
* `encode(..., bgm_file=...)` —— 传了就混音（存在即意图，不加多余布尔开关）；
* BGM 比成片短且 `bgm_loop=True` → `-stream_loop -1` 循环补齐 + `-shortest` 按视频长度截断；
* `amix` 与前景（段原声 / manual 指定音轨）混音，`normalize=0` 保留相对音量；
* 可选 `bgm_ducking` → `sidechaincompress` 用前景做 sidechain；
* `bgm_volume` / `bgm_fade` 做范围校验；`bgm_fade=0` 合法（不淡）。

**配套配置**（`EncodeParams` + `default.yaml` 由 `dump_config(Config())` **自动生成**，不手改）：
`bgm_volume=0.35` / `bgm_loop=true` / `bgm_fade=1.0` / `bgm_ducking=false`

**验收 20（8 条，全过）**：混音成功、循环补齐不被拉长、mute/manual 组合、
无 BGM 回归、缺失文件报错、音量越界报错、零淡入合法。

**接线**：`demo.py` 从素材库挑已 LOCK 的 `AUDIO` 素材作 BGM，取不到就无 BGM（报告可见）。

> ⚠️ 踩坑：我手改了 `default.yaml`，被 `test_dumped_default_config_is_reproducible` 抓到 ——
> **default.yaml 必须由 `Config()` 生成**，手改必然漂移。已改用 `dump_config` 重新生成。

> ⚠️ 另一处双份真相：`scripts/scan_config_wiring.py` 仍用"按文件名排除"（报 12 个候选），
> 而测试已改"按路径排除"（报 9 个）。**两个工具数字不一致会误导人**，已对齐为 9。

---

## 三、C 部分方案（阶段化显存调度，待实现）

新增 `bettercomfyui/pipeline/phases.py` → `VramPhaseScheduler`

```
P0 提示词      DeepSeek API          （不占显存）
      ↓
P1 素材生成    素材图模型            加载 → 批量生成【全部】素材 → /free 卸载
      ↓
P2 视频生成    minimax_h3           加载 → 批量生成【全部】段 → /free 卸载
      ↓
P3 审核        审核模型              加载 → 【统一】审核全部段 → /free 卸载
      ↓
P4 重生成      minimax_h3           加载 → 只重生成【不合格】段 → /free 卸载
      ↓  （P3↔P4 循环，CNT2/CNT3 上限终止）
P5 成片        ffmpeg               （不占显存）
```

**四条硬规则**：
1. **批量优先** —— 绝不"生成一段→审一段→再生成"交替，避免模型反复加载卸载；
2. **切换必验** —— 每次 `POST /free` 后**核对显存真的降了**，没降就报错（不假设释放成功）；
3. **同时只驻留 1 个模型** —— `vram_budget_gb` 预算门禁，超了拒绝加载；
4. **可断点续跑** —— 每阶段落检查点，从"当前阶段开头"恢复，不重跑已完成阶段。

**用户明确要求**：不做 A 卡生成 + B 卡辅助的高消费决策，单卡阶段化，
靠"卸载得干净"而不是"堆硬件"。

---

## 四、B 部分方案（精准测试，待实现）

**双层模拟浏览器操作：**

1. **HTTP 等价层（真调 ComfyUI，= 前端点击后发出的全部请求）**
   `/object_info` 读 schema → 构造 workflow → `POST /prompt` → 轮询 `/history`
   → `/interrupt` 取消 → `/queue` 清空 → `/free` 卸载
2. **DOM 层（node）**：面板渲染 + 点击处理 + 无 console.error

**模型边界打桩（用户要求的"关掉模型直接返回结果"）：**
* 提交前把生成节点替换成 **Stub 节点**（立即返回占位媒体）；
* 或提交后立刻 `/interrupt` + 注入假结果；
* **目的**：验证插件/节点/系统的接线完整性，不消耗算力与显存。

**另做「模型调用检测」**：验证节点确实构造了**正确的模型调用参数**（schema + 运行时探针），
证明"它会调"，只是不让它真跑。

**理论计算**：无法真跑的（视频生成时长/显存峰值/码率）给算式与边界。

---

## 五、下一步（按序）

1. **A2** SG5 X 返工加料（按失败维度 + `@素材` 标记拼提示词）
2. **C** 阶段化显存调度（`phases.py` + 验收测试）
3. **A5** 三个配置项（`save_vram_all` 变阶段开关、`comfyui_endpoint` 合并、`task_timeout_s` 看门狗）
4. **A3** SG1 界面接通（装 DeepSeek 两巨人 + 适配层节点）
5. **A4** C6 风格标签库 / G2 命名 / score_dimensions 分维度
6. **B** 端到端 UI 测试（HTTP 等价层 + 打桩）
7. **D** 重读文档补齐 + 最终报告
---

## 六、ROUND20 第二轮进度（2026-10-17 14:15）

**验收三态：158 通过 / 0 失败 / 0 未验证**（双解释器一致；本轮从 134 -> 144 -> 158）。

### 已完成

| 项 | 内容 | 验收 |
|---|---|---|
| **A1 背景音乐（O7）** | `amix` 混音 + 循环补齐 + ducking + 四配置项接线 | 验收 20（8 条） |
| **A2 返工加料（SG5 X）** | 失败维度正向约束 + `@类型_序号_名称` 素材标记 | 验收 22（14 条） |
| **C 阶段化显存调度** | `VramPhaseScheduler`：批量生成→卸载→换模型→统一审核；预算门禁；切换必验 | 验收 21（10 条） |
| **`save_vram_all` 接线** | 通过 `VramPhaseScheduler.from_config` 真正读取（棘轮 9→8） | 棘轮守卫 |

### C 部分的关键实测证据

对**真实 ComfyUI** 调用 `POST /free`：

```
显存快照: {'device': 'cpu', 'total_gb': 15.747, 'used_gb': 11.872, 'free_gb': 3.876}
/free ok=True | status=200
verified=True | reason=已核验：已用显存下降 0.101 GB
before.used=11.903 -> after.used=11.802
```

**卸载机制是真的可用且可核验的** —— 不是"调了就当释放"。

阶段调度的核心指标（用假显存验证，不依赖真 GPU）：

```
ok = True | 卸载次数 = 4 | 最大驻留 = 19.5 GB
```

**最大同时驻留 19.5 GB = 单模型最大值**，证明"同一时刻只有一个模型驻留"。
验收 21 的 P4 还专门断言：**卸载次数不随段数增长**（1 段与 10 段卸载次数相同），
这正是"批量阶段化"相对"逐段交替"的效率所在。

### 新增模块

- `bettercomfyui/director/augment.py` —— 返工提示词加料（含 `DIMENSION_HINTS` 正向约束表）
- `bettercomfyui/pipeline/phases.py` —— `VramPhaseScheduler` / `Stage` / `default_stage_plan`
- `ComfyUIHttpExecutor.free_models()` / `vram_snapshot()` —— `/free` 客户端 + 释放核验
- 配置：`encode.bgm_*`（4 项）、`video_review.rework_prompt_augment`、`director.vram_budget_gb`

### 仍未做

- **A3** SG1 界面接通 DeepSeek（装两个巨人包 + 适配层节点）
- **A4** C6 风格标签库 / G2 素材命名 / score_dimensions 分维度
- **A5** `task_timeout_s` 看门狗 / `comfyui_endpoint` 合并
- **B** 端到端 UI 测试（HTTP 等价层模拟点击 + 模型边界打桩）
- **D** 重读文档/逻辑图/愿景补齐

### 踩坑

1. **`_rework_normal` 没有 `review` 变量** —— 加料要在返工里用审核结果，
   必须从 `handle_quality_failure` 透传，否则 `UnboundLocalError`。
2. **阶段默认 `unload_after=True` 时"切换前"不需要卸载** ——
   验收 P3 一开始断言"每次切换都卸载"，实际阶段结束已卸。
   改成用 `unload_after=False` 的计划才能验证**切换路径**本身。
---

## 七、ROUND20 第三轮：A5 + B（模拟浏览器 E2E）

**验收三态：174 通过 / 0 失败 / 1 未验证**（双解释器；本轮 158 -> 174）。

### A5 两个配置项接线完成 —— **配置真缺口清零**

| 配置项 | 修法 | 实测 |
|---|---|---|
| `global.task_timeout_s` | `SegmentGenerator.rework_timeout_s()` + 轮询**看门狗**（可注入时钟，测试不必真等 900 秒） | 超时抛错含段 id 与超时值 |
| `global.comfyui_endpoint` | `resolve_comfyui_endpoint(cfg)` + CLI `doctor` HTTP 探测 | `http://127.0.0.1:8188 [OK 可达]`（修前用硬编码 8000） |

**棘轮扫描：候选未接线 9 -> 6，且 6 条全是「设计如此」**（委托巨人或行为已硬编码），
**`KNOWN_UNWIRED` 里真缺口 = 0**。

> 更正一个之前的误判：`comfyui_base_path` 与 `comfyui_endpoint` **不是重复概念** ——
> 前者是**安装目录**（找权重），后者是**HTTP 地址**（提交任务）。不该合并。

### B 模拟浏览器操作的端到端测试（验收 24）

**为什么这样做**：真浏览器要下载 chromium（本机无网络）→ 退到**等价强度**：
ComfyUI 前端每个点击最终只产生这几个 HTTP 请求，逐条真实调用即可。

| 界面动作 | HTTP 请求 | 实测 |
|---|---|---|
| 打开节点面板、拖节点 | `GET /object_info` | 我的 **16 个节点全在**，schema 可渲染 |
| 点「队列」 | `POST /prompt` | 返回 prompt_id |
| 等结果 | `GET /history/{id}` | **`status_str=success, completed=True`** |
| 点「取消」 | `POST /interrupt` | 可用 |
| 清空队列 | `POST /queue` | 可用 |
| 释放显存 | `POST /free` | 可用（空闲时如实标未验证） |

**真实执行到了什么程度**（不是模拟）：

```
提交 BetterComfyUIOneDirector -> PreviewAny
status: success | completed: True
输出: {"project": "...", "segment_count": 2, "public_library_count": 0,
       "segments": [{"prompt": "...全片视觉风格：写实电影感", "hardcut": false, ...}]}
```

**节点真的在真 ComfyUI 里跑了**，风格注入、分段、审计 JSON 全部真实产出。

### 模型边界打桩（用户要求的"关掉模型直接返回结果"）

* 只用**不加载模型**的节点（`OneDirector` 纯 Python 计算、`PreviewAny` 只显示字符串）
  —— 这就是"把模型关掉直接给结果"；
* **依赖模型的节点绝不提交**（红线）：验收 24 的 `test_p9` 是**红线断言** ——
  提交体里出现 `UNETLoader`/`MiniMaxH3Director` 等任何模型加载节点就立刻失败。
  理由：34 GB 权重 vs 15.75 GB 内存，历史上已蓝屏三次。

### 顺带证明的守卫

* **非法 project_id 被节点自己拒绝**（R5 项目隔离）：
  `PathIsolationError: project_id 必须是 32 位小写 UUID4 hex`
* **COMBO 取值被 ComfyUI 校验**（说明节点 schema 真生效，不是摆设）。

### 踩坑

1. `SegmentOverrides` 上没有 `effective()`，真正的合并方法是 **`resolve(cfg)`**
   返回 `(生效值, 被覆盖字段)` —— 写错时异常被 `except` 吞掉，表现为"单段覆盖不生效"。
2. `SegmentGenerator` 强制要求 **R3 门禁依赖**（`store` 或 `lock_resolver`），
   测试里得给一个恒通过的假 resolver，否则构造就抛 `DirectorDependencyError`。
3. `JobRef` 需要 `(job_id, executor)` 两个参数；`/queue` 返回**空 body**，不能 `json.load`。

### 仍未做

- **A3** SG1 界面接通 DeepSeek（装两巨人 + 适配层节点）
- **A4** C6 风格标签库 / G2 素材命名 / score_dimensions 分维度
- **B 余下** 理论计算（无法真跑项的显存/时长/码率算式）
- **D** 重读文档/逻辑图/愿景补齐
---

## 八、ROUND20 第四轮：B2 理论计算 + A4 三项完成

**验收三态：200 通过 / 0 失败 / 1 未验证**（双解释器；本轮 185 -> 200，整轮 92 -> 200）。

### B2 理论计算（用户要求"不能跑就理论计算"）

新增 `scripts/theory_model.py`，**每个数字都来自磁盘实测或显式公式**：

| 项 | 结果 |
|---|---|
| 视频 UNET（ref2va int8 剪枝） | 19.53 GB |
| 文本编码器（qwen3vl-32b nvfp4） | 14.61 GB |
| 视频 VAE / 音频 VAE | 4.85 / 0.56 GB |
| **一次性全加载** | **39.55 GB** |
| **阶段化峰值** | **24.94 GB** |

**在 5090 的 32 GB（预留 2 GB，可用 30 GB）上：**

```
一次性全加载装得下？ 否（超 9.55 GB）  -> 必然 OOM
阶段化装得下？       是（余 5.06 GB）
结论：一次性全加载装不下，必须阶段化
```

> **这是本轮最有价值的结论**：阶段化显存调度**不是优化项，是必需品**。
> 一次把 UNET + 文本编码器 + VAE 全塞进去，32 GB 的卡无论如何都装不下。

**效率量化**（10 段）：

| | 加载次数 | 时长 |
|---|---|---|
| 逐段交替 | 22 次 | 1590 s |
| 阶段化批量 | **5 次** | **825 s** |
| 节省 | 17 次（4.4x） | 765 s（48.1%） |

**成片体积**（1080p24 CRF18，1 小时）：8-12 Mbps -> **3.52-5.27 GB**（给区间不给单点，CRF 是恒定质量）。

### A4 三项完成

| 项 | 修法 | 实测 |
|---|---|---|
| **C6 风格标签库** | 新增 `prompt/styles.py`：**16 个**预置画风标签，节点「统一风格」支持 `@标签名` 展开 | 真实 ComfyUI tooltip 里 16 个标签全部可见 |
| **G2 素材命名** | 新增 `AssetStore.human_label()` → `角色_1_林秀`（图一格式）；**磁盘 asset_id 保持不变**（改格式会让已有项目全部失效） | `human_label` = `角色_1_林秀` |
| **H5 分维度评分** | `submit_model_result(scores=...)` 按配置四维取平均；**缺维度/越界直接报错** | 4 维全给 → 85 分；缺 3 维 → 报错；150 分 → 报错 |

**C6 为什么不新增 COMBO 输入**：新增输入会改节点 schema，已保存工作流的连线/参数会错位。
`@标签名` 是纯文本层展开 —— schema 不变、老工作流零影响、还能混写自由文本。

### 环境事故记录（不是代码问题）

重启 ComfyUI 时用系统 python 启动失败：`ModuleNotFoundError: No module named 'comfy_aimdo'`。
**正确启动方式**（RESUME.md 第七节）：
```
cd .comfyui
& '..\.comfyenv\Scripts\python.exe' main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch
```
必须用 `.comfyenv` 的解释器（依赖装在 venv 里），且要 `--cpu`（本机无 GPU）。
修正后 4 秒启动完成。

### 仍未做

- **A3** SG1 界面接通 DeepSeek（装两个巨人包 + 适配层节点）—— **最后一项功能缺口**
- **D** 重读文档/逻辑图/愿景补齐 + 最终报告
---

## 九、ROUND20 第五轮：A3 完成（图一最后一个功能缺口闭合）

**验收三态：210 通过 / 0 失败 / 1 未验证**（双解释器；整轮 92 -> 210）。

### A3：SG1「剧本 → 提示词」界面接通

**修前**：`prompt/gateway.py` 的三个 gateway + `validate_script` 全在库里，
但**界面上没有任何节点调用** —— 用户点不出"给剧本出提示词"。

**修法（站在巨人肩膀上，不重写）**：

| 步骤 | 结果 |
|---|---|
| 装 `ComfyUI-Storyboard-LLM` | `StoryboardNode`（DeepSeek 拆剧本）已加载 |
| 装 `ComfyUI-DeepSeek-API-connector` | `DeepSeekPromptConnector` 已加载 |
| 新增 `BetterComfyUIPromptBridge` | 把巨人输出转成导演台要的 `剧本分镜` |

**节点数**：我的节点 16 -> **17**；ComfyUI 总节点 1029。

### 真实链路已贯通（不是模拟）

```
BetterComfyUIPromptBridge  ->  BetterComfyUIOneDirector  ->  PreviewAny
（吃巨人格式 video_prompts）      （成套导演组 + 审计 JSON）

真实 ComfyUI 执行：status=success, completed=True
输出：segment_count=3
  段0: 林秀推门走进客栈大堂，镜头缓推，冷蓝夜色。全片视觉风格：写实电影感
  段1: 老陈抬头看向门口，特写，油灯昏黄。全片视觉风格：写实电影感
  段2: 林秀在桌边坐下，中景，镜头轻微横移。全片视觉风格：写实电影感
```

### 桥接节点做的三件事

1. **格式转换**：巨人输出是 `[镜头N] prompt` + 空行；导演台要 `---` 独占一行 —— 桥接转换；
2. **三级降级容错**：有标签按标签切 -> 无标签按空行切 -> 都没有整段算一个（**任何一级都不丢内容**）；
3. **兜底**：`video_prompts` 为空时用 `image_prompts`。

### 踩坑：风格被注入了两次

第一版桥接把「统一风格」也拼进每段文本，结果链路跑出来是：

```
…冷蓝夜色。全片视觉风格：冷蓝色调，低饱和…。全片视觉风格：写实电影感
```

桥接注一次、导演台再注一次。**已修**：桥接只把风格写进 `提示词JSON`，
**不碰段文本** —— 风格是导演台「统一风格」的职责。
验收 27 的 P8 专门守这条（真实链路里风格只能出现一次）。

### 未验证（如实标注）

**巨人自己调用 DeepSeek API 那一步未验证** —— 需要真实 API 密钥，
而密钥不得写进代码仓库（任务书第 10 节）。
本条只验证"接线与格式转换"，**不把"API 调通了"写成已验证**。

### 图一缺口闭合情况

| 缺口 | 状态 |
|---|---|
| #1 剧本形状接缝（假成功） | ✅ 已修 |
| #2 界面路径结构校验 | ✅ 已修 |
| #3 `references` 反向索引 | ✅ 已修 |
| #4 `O7` 背景音乐 | ✅ 已修（验收 20） |
| #5 `per_class_limit` | ✅ 已修 |
| #6 `assets.resolution` | ✅ 已修 |
| #7 Z3/Z4/Z5 日志与配置 | ✅ 已修 |
| #8 SG5 `X` 返工加料 | ✅ 已修（验收 22） |
| #9 段落盘（新发现） | ✅ 已修 |
| #10 ENC1 音频拼接 | ✅ 已修 |
| **SG1 界面未接通（系统性）** | ✅ **已修（本轮）** |
| C6 风格标签库 / G2 命名 / H5 分维度 | ✅ 已修（验收 26） |
| task_timeout_s / comfyui_endpoint | ✅ 已修（验收 23） |

**配置棘轮：候选 9 -> 6，且 6 条全是「设计如此」，真缺口 = 0。**

### 仍未做

- **D** 重读全部文档/逻辑图/愿景做最后一次核对 + 最终报告
---

## 十、ROUND20 收尾：覆盖盘点 TODO 归零

更新 `bettercomfyui/gap_analysis.py`，把本轮修好的 5 条从 TODO 改为 DONE（并写明实测依据）：

| 台账条目 | 新状态 | 依据 |
|---|---|---|
| 界面路径的结构校验 | DONE | 验收 16-P5：无效引用 -> ok=False、素材 0、段 0 |
| 剧本形状接缝 | DONE | 验收 16-P2：规范剧本 -> 素材新建>0、LOCK>0（修前全 0） |
| 引用关系记录 | DONE | 验收 17：6 条引用记录；标 FAILED 后只返回该段 |
| 素材库统一分辨率 | DONE | 验收 18：3000x1200 -> 1024x410（等比不变形） |
| 每类数量上限 | DONE | 验收 18：上限 9，第 10 个被拒 |

**覆盖盘点：DONE 73 / DELEGATED 13 / TODO 0**（合计 86 条）。

> 台账不能撒谎：活干完了却还写着 TODO，比没写更糟 —— 下次接手的人会重复劳动。

**最终验收三态：210 通过 / 0 失败 / 1 未验证**（双解释器 EXIT=0）。
完整报告见 `docs/ROUND20_最终报告.md`。
---

## 十一、ROUND21：真实浏览器测试 + 成品安装 + 4 套新工作流

**用户说明本机有网络** -> 真浏览器测试解锁。
**验收三态：系统 python 233 通过 / 0 失败 / 1 未验证；venv 224 通过**（浏览器测试模块级跳过）。

### 1. 真实浏览器点击测试（验收 29，9 条全过）

用 **Playwright + 系统 Edge** 驱动真浏览器打开 `http://127.0.0.1:8188`：

| 用户动作 | 测试怎么做 | 结果 |
|---|---|---|
| 打开 ComfyUI | 真实导航 + 等画布渲染 | ✅ 标题 ComfyUI、画布存在 |
| 从面板拖节点 | `LiteGraph.createNode()`（**前端拖拽就调这个**） | ✅ 画布真的多一个节点 |
| 看节点面板渲染 | 对比界面 widget 名与 schema | ✅ 全部对上 |
| 点「队列」 | `app.queuePrompt()`（**按钮内部调的就是它**） | ✅ 提交并执行成功 |
| 打开保存的工作流 | `app.loadGraphData()` | ✅ W1/W2 都能打开 |
| 看前端有没有报错 | 收集 `console.error` / `pageerror` | ✅ 无我方错误 |

### 2. 真浏览器抓到一个**真 bug**（HTTP 层永远发现不了）

```
[vite:preloadError] Failed to fetch dynamically imported module:
/extensions/bettercomfyui_comfy/js/bettercomfyui_panel.js
```

**根因**：面板 JS 的 import 少一层 ——

```js
// 错（解析成 /extensions/scripts/app.js -> 404）
import { app } from "../../scripts/app.js";
// 对（解析成 /scripts/app.js -> 200）
import { app } from "../../../scripts/app.js";
```

实测证实：`/scripts/app.js` HTTP 200、`/extensions/scripts/app.js` 不可用。
**面板在真实界面里一直是坏的** —— 因为前端加载失败只会打一行 console.error，
页面照样能用，所以之前的 HTTP 测试、DOM 测试都没抓到。

### 3. 成品安装验证（验收 28，14 条全过）

新增 `scripts/verify_install.py`，五项逐条核实：

```
[OK] 插件已安装且逐文件哈希一致
[OK] 两个巨人包已安装
[OK] 新工作流已放进界面目录
[OK] 17 个节点在界面里可见（总节点 1029）
[OK] 巨人节点在界面里可见
结论：安装完整且可用。
```

> 第一次跑就抓到真问题：新工作流生成后**没同步到安装目录**。这正是验证脚本的价值。

### 4. 4 套全新工作流（全部按真实 schema 构建 + 三重校验 + 已安装）

| 编号 | 名字 | 节点 | 用途 |
|---|---|---|---|
| **W1** | 最简单入门 | 1 | 只有一体化导演台，第一次用 |
| **W2** | DeepSeek 全自动 | 11 | 巨人拆剧本 → 桥接 → 导演台 → 出片 |
| **W3** | 全流程出片 | 9 | OneDirector → 导演台 → CreateVideo → SaveVideo |
| **W4** | 素材参考图注入 | 10 | AssetSlots 的 IMAGE 接到导演台参考图 |

校验四件事：节点存在 / 槽位范围 / **类型匹配** / 源节点 `outputs[].links` 已记录
（最后一条不记，界面就画不出连线）。

都在 `.comfyui/user/default/workflows/`，**界面上直接能打开**（验收 29 的 P7/P7b 真浏览器验证）。

### 5. 愿景对齐矩阵（验收 28 的 P10）

把愿景要点逐条映射到**代码符号**，32 条全部命中：

站在巨人肩膀上 -> `MiniMaxH3Director`｜一键出片 -> `BetterComfyUIOneDirector`｜
尾部22帧 -> `tail_frames`｜素材全量挂载 -> `plan_ref_slots`｜
优先级 -> `asset_priority`｜LOCK 三要素 -> `resolve_lock`｜只追加不覆盖 -> `ImmutableAssetError`｜
项目隔离 -> `PathIsolationError`｜CNT 隔离 -> `CounterKind`｜超时切模型 -> `AUTO_SWITCHED`｜
唯一提交点 -> `finalization_token`｜爆炸半径 -> `segments_depending_on_asset`｜
级联上限 -> `max_cascade_depth`｜断点续跑 -> `CheckpointStore`｜版本回滚 -> `superseded_by`｜
真实vs mock -> `generation_provenance`｜阶段化显存 -> `VramPhaseScheduler`｜
卸载核验 -> `free_models`｜DeepSeek -> `BetterComfyUIPromptBridge`｜风格库 -> `STYLE_LIBRARY`｜
BGM -> `bgm_file`｜返工加料 -> `build_rework_prompt`｜统一分辨率 -> `unify_resolution`｜
每类上限 -> `per_class_limit`｜可读命名 -> `human_label`｜分维度评分 -> `_resolve_dimension_scores`｜
超时看门狗 -> `rework_timeout_s`｜地址可配 -> `resolve_comfyui_endpoint`｜
配置导入导出 -> `export_config_json`｜切换日志 -> `log_manual_or_switch`

### 6. 踩坑

1. **`loadGraphData` 是异步的** —— 不 `await` 就读 `_nodes` 会得到 0，误判"工作流打不开"。
2. **DOM 测试的临时目录结构要镜像真实 URL** —— 面板在 `/extensions/<plugin>/js/` 下，
   import 要三层才能到 `/scripts/`；临时目录只建两层就会解析错。
3. **`playwright install chromium` 反复卡锁** —— 改用系统 Edge（`channel="msedge"`），
   效果等价且不用下载。这是本机有 Edge/Chrome 的好处。
---

## 十二、ROUND22：API 配置接口 + 完整逻辑图工作流 W0

**用户指出两个真问题：**
1. 「你没有设置 DeepSeek 之类的 API 的配置接口等，并不能真正实现」
2. 「制作一套完整的工作流，可以实现我们那套逻辑图」

**验收三态：系统 python 244 通过 / 0 失败 / 1 未验证；venv 234 通过。安装验证：18 节点 + 6 套工作流。**

### 1. 为什么"填了密钥也没用"—— 找到根因

`ComfyUI-Storyboard-LLM` **没有 `api_key` 输入端口**（它的 required 只有
`chapter_text` / `save_json`）。它读自己目录下的 `settings.json`：

```json
{"api_key": "", "base_url": "https://api.deepseek.com", "output_path": ""}
```

而且自带的 `settings.json` **带 `//` 注释，不是合法 JSON** ——
巨人的 `json.load` 会失败并**静默回退到默认值**（空密钥）。
所以"在别处填密钥"永远不生效。

### 2. 补的东西

| 层 | 内容 |
|---|---|
| **HTTP 接口** | `GET /bettercomfyui/settings`（读，密钥遮罩）、`POST /bettercomfyui/settings`（写） |
| **落盘** | 写 `config/user.yaml`（**字段名 `model_name`**，不是 `model`）**并同时写巨人 `settings.json`** |
| **只读快照** | provider / model / 是否已配 / **来源（env / config / none）** / 遮罩密钥 |
| **配置节点** | `BetterComfyUISettings` —— 工作流里引用它就能拿到密钥，**密钥不写进工作流文件** |
| **面板界面** | 右侧「Better ComfyUI」面板新增「API 配置」区块：状态行 + 密钥输入（密码框）+ 模型/地址 + 保存 |

### 3. 三条安全底线（实测验证）

1. **绝不回传完整密钥**：GET/POST 响应里只有 `sk-***abcd`；验收 30 的 P1/P4 断言响应不含明文；
2. **白名单字段**：非白名单字段（如 `project_root`）被丢弃 —— P6 验证；
3. **空密钥不覆盖**：误点保存不会清掉已有密钥 —— P5 验证。

### 4. 踩到的三个真 bug

1. **`REPO_ROOT` 算错**：装到 `<repo>/.comfyui/custom_nodes/` 后
   `Path(__file__).parent.parent` 得到 `custom_nodes/`，于是写了
   `custom_nodes/config/user.yaml` —— **用户的真实配置在仓库根，写错地方等于没保存**。
   改用 `_bridge._candidate_roots()` 并加**强标记**（必须同时有 `config/default.yaml`
   与 `bettercomfyui/__init__.py`），因为 `custom_nodes` 下恰好也出现了 `config` 目录，会抢先匹配。
2. **字段名错**：我写的是 `prompt.model`，实际字段是 **`prompt.model_name`**。
   由于 `load_config` 对未知键**严格报错**，这一下把整个配置加载搞挂了 ——
   错误信息明确指出了合法键，照它改即可。**严格校验在这里救了一次。**
3. **巨人配置不是合法 JSON**：自带 `settings.json` 带注释，巨人静默回退默认值。
   我的写入是合法 JSON，反而修好了它。

### 5. 完整逻辑图工作流 W0（14 节点 / 16 连线，真浏览器可打开）

```
BetterComfyUISettings ──状态──> PreviewAny
        │
StoryboardNode(DeepSeek) ──video_prompts──> BetterComfyUIPromptBridge
        │                                          │ 剧本分镜
        │                                          ▼
BetterComfyUIAssetSlots ──参考图_1..3──> BetterComfyUIOneDirector
                                                   │ 导演组
                                                   ▼
UNET→LoRA┐                              MiniMaxH3Director
CLIP─────┼─────────────────────────────────────┤
VAE×2────┘                                      │ IMAGE/AUDIO/FLOAT
                                                ▼
                                        CreateVideo ──> SaveVideo
```

覆盖图一：**SG7** 全局配置 / **SG1** 剧本→提示词（巨人+桥接）/ **SG2** 素材库 /
**SG4** 一体化导演台→真实导演台 / **SG6** 合成→成片输出。
SG3/SG5/SG8 由编排层（审核、异常、断点）承担，工作流里没有独立节点。

**真浏览器实测**：`app.loadGraphData()` 后画布 14 个节点全部到位，
含 StoryboardNode / PromptBridge / OneDirector / MiniMaxH3Director / SaveVideo /
Settings，**页面无异常**。

### 6. 现在怎么用（三步）

1. 打开 ComfyUI 网页，右侧「Better Comfy UI」面板 →「API 配置」→ 填 DeepSeek 密钥 → 保存
   （会同时写 `config/user.yaml` 与巨人 `settings.json`，状态行会显示 `✅ 已配置`）；
2. 左侧工作流面板 → 打开 **`W0_完整逻辑图全流程`**；
3. 在 `StoryboardNode` 里写剧本 → 点队列。

> 本机无 GPU，真实出片要在 24G+ 显存机器上跑；本机可用 W1/W3 查看导演组结构。
---

## 十三、ROUND23：修界面报错（404/405）+ 去掉浮动窗口

**用户实测报错：**
```
未找到侧边栏，已用右上角浮动面板代替
读取项目列表失败：404 Not Found
体检失败：404 Not Found
打开/创建失败：405 405: Method Not Allowed
载入工作流失败：404 Not Found
```

### 1. 404/405 的根因（不在后端）

后端所有端点都返回 **200**（我直接 curl 验证过）。真正原因是：

**新版 ComfyUI 前端 `api.fetchApi()` 会自动给路径加 `/api` 前缀** ——
它请求的是 `/api/bettercomfyui/doctor`，而我的路由只挂在
`/bettercomfyui/doctor` 上，于是：

| 前端请求 | 后端实际路由 | 结果 |
|---|---|---|
| GET `/api/bettercomfyui/doctor` | 无 | **404** |
| POST `/api/bettercomfyui/project/open` | 无 | **405** |

**修法：两个前缀都注册**（`/bettercomfyui/*` 与 `/api/bettercomfyui/*`），
前端怎么写都能通。实测：

```
/api/bettercomfyui/doctor           -> 200
/api/bettercomfyui/projects         -> 200
/api/bettercomfyui/workflow/minimal -> 200
/api/bettercomfyui/settings         -> 200
POST /api/bettercomfyui/project/open-> 200
```

### 2. 用户明确要求：不要那个"类似弹窗的窗口"

**去掉浮动面板兜底**：找不到 `#comfyui-sidebar` 时**不再创建任何窗口**，
只在控制台说明一句。所有功能改走 ComfyUI **原生入口**：

* API 配置 -> **ComfyUI 原生「设置」**（齿轮）
* 跑流程 -> **原生工作流**（W0/W1/W3…，从左侧工作流面板打开）

实测：页面上不再出现我们的浮动面板，`body` 下无我方 `fixed` 元素。

### 3. API 配置到底该填在哪（重要）

复盘发现 **剧本巨人自己就在 ComfyUI 原生设置里注册了配置项**：

```
StoryBoard.api_key      （密钥）
StoryBoard.base_url
StoryBoard.output_path
```

它改完会 POST 到 `/storyboard/settings`，写回巨人自己的 `settings.json`。
**这才是原生、推荐的做法。**

我的配置接口做了**三处同步**（一次保存，全部生效，实测通过）：

| 落点 | 作用 |
|---|---|
| `config/user.yaml` | 本项目的全局配置 |
| 巨人 `custom_nodes/ComfyUI-Storyboard-LLM/settings.json` | **巨人节点实际读的那份** |
| `user/default/comfy.settings.json` 的 `StoryBoard.api_key` | **用户在原生设置里看到的那份** |

状态显示也改成**多来源**（环境变量 > user.yaml > 原生设置 > 巨人配置），
所以无论用户从哪个入口配的，状态都能如实反映。

> 安全：三处都验过「响应不含完整密钥」，只回 `sk-***abcd`。

### 4. 顺带修的 DOM 测试

移除浮动兜底后，DOM 测试的替身假 DOM 里没有侧边栏 -> 面板不挂载 -> 测试误判。
已给替身 DOM 加上 `#comfyui-sidebar`（更接近真实结构）。

**最终：系统 python 244 通过 / 0 失败 / 1 未验证；venv 234 通过。**
---

## 十四、ROUND24：修「工作流不完整 / API 密钥没连上 / 打开报错」

**用户指出三个问题，全部修复并实测：**

1. 「API key 节点没有和其他节点连接起来」
2. 「不是一个完整的工作流，很多功能没法完成」
3. 「打开之后会有报错」

### 1. API 密钥真的连上了（新增 `BetterComfyUIStoryboard` 节点）

**根因**：剧本巨人 `StoryboardNode` **没有 api_key 输入端口**（只读自己的 settings.json），
所以 `BetterComfyUISettings` 的 `api_key/model/api_base` 三个输出**无处可接** ——
工作流在「配置 → 剧本」这一环是断的。

**修法**：新增 `BetterComfyUIStoryboard`（剧本→镜头提示词）：
* 输入 `api_key` / `model` / `api_base`（**直接接配置节点**）+ 剧本 + 段时长 + 风格；
* 内部复用编排包的 `build_gateway()`（外部 API / 本地千问 / 自配接口）；
* 输出 `video_prompts`（与巨人同格式，可直接接桥接节点）+ `script_json` + 摘要；
* **无密钥也能跑**：`使用占位结果=True` 时产出确定性演示剧本，
  摘要首行大字标「⚠️ 占位（未调用真实模型）」，**绝不冒充真实输出**。

现在 W0 的链路是**真的通的**：

```
APIConfig.api_key  ─┐
APIConfig.model    ─┼→ 剧本节点 → 提示词桥接 → 一体化导演台 → 真实导演台 → 合成 → 出片
APIConfig.api_base ─┘        ↑素材参考图
APIConfig.状态 ──→ PreviewAny（状态可见）
```

### 2. 工作流打开报错的三个真因（都实测定位到了）

| 现象 | 真因 | 修法 |
|---|---|---|
| 打开后提交 500 | `graphToPrompt()` 在新版前端是 **async**，不 await 拿到 Promise，`output` 为空 | 前端脚本 `await graphToPrompt()` |
| 执行报「需要 project_id」 | 工作流里 `项目`/`project_id` 是占位字符串，节点要求真实 32 位 hex | 建真实项目 `演示项目` 并把 id 写进工作流 |
| 判「未连线输入」误报 | 把**可选**输入（`image_prompts`、`i2v_groups` 等）也当成问题 | 用 `/object_info` 的 required 清单判断，只报必填 |

### 3. 真浏览器用户行为模拟（脚本 `scripts/browser_sim.py`）

真浏览器（系统 Edge）打开 ComfyUI → 加载工作流 → 检查节点错误 → **真点队列** → 读执行结果：

```
=== W0L_本机可跑通体验版 ===
  画布节点：7 / 文件 7
  节点错误：无；未连线输入：无
  提交：200 prompt_id=87ff6919-...
  执行状态：success completed=True
  前端错误：pageerror 无 / 我方 console.error 无
  结论：通过

=== W0_完整逻辑图全流程 ===
  画布节点：15 / 文件 15
  节点错误：无；未连线输入：无
  pageerror：无
  结论：通过
```

### 4. 新增 `W0L_本机可跑通体验版`

本机无 GPU、内存 15.75 GB，跑 W0 会加载 34 GB 权重（**会挂**）。
所以拆成两套：

| 工作流 | 节点 | 用途 |
|---|---|---|
| `W0_完整逻辑图全流程` | 15 | **GPU 机器**出片（配置→剧本→桥接→素材→导演台→合成→出片） |
| `W0L_本机可跑通体验版` | 7 | **本机就能点队列**看真实产出（导演组+审计 JSON，不加载权重） |

### 5. GPU 机器效率测试脚本已就绪

`scripts/gpu_efficiency_test.py`（本地已跑通逻辑）：

```
=== 环境探测 ===
  device: cpu | vram_total 15.75 GB | comfyui 0.41.0 | Better 节点 19 | 导演台节点 25
=== 阶段调度卸载次数对比（无需 GPU）===
  段数 3：阶段化卸载 4 次 vs 逐段交替 8 次
  最大同时驻留 19.5 GB (预算 32.0 GB, 达标=True)
```

到 GPU 机器上跑（1 小时内必做）：

```bash
# 1) 环境探测
python scripts/gpu_efficiency_test.py --probe
# 2) 跑 15 秒视频（3 段 x 5 秒），限制 32G 显存，采样显存峰值
python scripts/gpu_efficiency_test.py --segments 3 --seconds 5 --budget-gb 32 --project-id <真实id>
```

输出：墙钟耗时、**每秒视频耗时**、**显存峰值**、是否在 32 GB 预算内、执行状态。

---

## 十五、ROUND25：一次调用全量提示词 + 修复「生成失败 / 结果不可视化」

**用户报障**：① API Key 已配置但工作流始终无法生成内容；② API 结果不在对应节点显示。
**用户基准**：一整段剧本、**全程仅一次 API 调用**、一次输出全部提示词（素材类 + 视频）；
按预设分隔符自动拆出人物/场景/角色等素材提示词，以**标准 JSON** 填充到对应节点。
**对照基准**：`文档/BetterComfyUI_完整总流程图.dot` SG1（A–E）。

### 根因（实测）
1. **三处 API Key 全是验收测试假密钥** `sk-acceptance-test-FAKE-…` → 真实调用必 401。
   来源：验收 30 写真实 `config/user.yaml`，且只备份两处、**漏了原生 comfy.settings.json**。
2. **打开网页会静默改写配置**：面板原生设置 `onChange` 无条件 POST，页面加载恢复设置即触发
   （实测 19:35 把 `prompt.endpoint` 改成 `/v1`，破坏测试恢复 → 假密钥残留）。
3. **W0/W0L 的剧本节点 `使用占位结果=True`** → 有 Key 也永远不调模型。
4. 界面路径只出"镜头/视频提示词"，**没有 E1–E4（角色/场景/物品/分镜图提示词）**；且
   `script_json`/`摘要`/`提示词JSON` 全部悬空 → 不可视化。
5. **W2 连线非法**（target_slot 指向不存在的输入槽）+ 模型名 `deepseek-v4-flash` 无效 + 无配置节点。
6. W0 含权重链 → 本机点队列必卡死（环境墙，不是提示词失败）。

### 修复
- 新增编排层 `bettercomfyui/prompt/script_build.py`：**一次** `gateway.complete` →
  `parse_with_report(auto)`（JSON 优先 / 预设分隔符 `=== 角色 ===` 等）→ `canonicalize` →
  `validate_script` → 按 CHARACTER/SCENE/PROP/SHOT 拆分 → 标准 JSON + `---` 视频提示词 + 统计。
- 新增节点 `BetterComfyUIScriptPrompts`（21 个节点）：8 个输出，全部可接预览/下游；
  可接 API 配置节点；无 Key 明确报错；占位模式 `api_calls=0` 且标注占位。
- 重建 4 套工作流（真实 schema 校验通过）：**P1（零权重、本机可跑）**、W0、W0L、W2；
  8 个输出全部接 `PreviewAny`，视频提示词自动填入一体化导演台「剧本分镜」。
- 面板 JS：原生设置只在**真实用户交互后**保存（A1 修复）+ 真浏览器回归（验收 30 P11）。
- 验收 30 备份/恢复扩到三处配置并自检；清空三处测试假密钥（用户需重填真实 Key）。
- 新增验收 31（15 条）。

### 证据
```
系统 python tests/verify：EXIT=0，通过=260 失败=0 未验证=1
.comfyenv  tests/verify：EXIT=0，通过=249 失败=0 未验证=3
全量 pytest tests/      ：EXIT=0
真实 ComfyUI 端到端（本地 mock OpenAI 服务）：提交 P1 → success；mock 只收到 **1 次**调用；
  11 个预览框显示四类素材 JSON + 视频提示词(---) + 两种 JSON + 摘要；导演台拿到 3 段；
  9/9 断言 PASS（含"API 调用 1 次"）。
真浏览器（系统 Edge）打开 P1：14/14 节点、零节点错误、零未连线、零我方 console.error。
verify_install：插件哈希一致 / 巨人包 / 工作流 / 21 节点可见 → 安装完整。
```
> 真实 DeepSeek Key 调用与真实出片**仍未验证**（无真实 Key / 本机内存不足），不写成通过。

### 仍未做（下一轮候选）
- `BetterComfyUIEverything`（一体化总控台）仍用自写简化 prompt（只出镜头，无 E1–E4）→ 统一到 `script_build`；
- 用户在 GPU 机器上用真实 Key 跑 P1/W0 的实测反馈；
- `scripts/gpu_efficiency_test.py` 在 GPU 机器实跑。

完整报告：`docs/ROUND25_修复报告.md`；问题清单：`docs/ROUND25_逻辑对照问题清单.md`。


---

## 十六、ROUND26：收敛成「一个大节点」（用户否掉 ROUND25 的多节点形态）

**用户反馈**：ROUND25 的「新节点 + 8 输出 + 手工接线」正是用户反对的「生硬接起来 / 重新造东西」；
用户明确要求：**一个大节点，插上模型、插上输出就能用；素材提示词要自动进「素材库」；要更自主自动化**。

**本轮改动**：
- 删除 ROUND25 新增的 `BetterComfyUIScriptPrompts` 节点（节点数 21 → **20**）；
- 升级 `BetterComfyUIEverything`（一体化总控台）= **唯一大节点**，内部完成：
  A1–A4 素材清单注入 → **一次 API 调用产出 E1–E5** → E6–E9 确定性匹配并**自动登记素材库**
  （复用/新建/疑似，绝不覆盖；占位模式不写）→ G 已 LOCK 素材全量挂每段 → 输出导演组；
- 删除大节点内自写的 `_call_llm/_placeholder`（只出镜头），改为复用 `prompt/script_build.py`；
- 工作流重建：W0（13 节点）/ W2（**12 节点，最简：大节点→导演台→出片**）/ P1（5）/ W0L（4）；
  删除旧 `rebuild_prompt_workflows.py`，新增 `scripts/build_one_node_workflows.py`。

**证据（实测）**：
```
系统 python tests/verify：EXIT=0，通过=263 失败=0 未验证=1
.comfyenv  tests/verify：EXIT=0，通过=252 失败=0 未验证=3
全量 pytest tests/      ：EXIT=0
真 ComfyUI + 本地 mock API：P1 执行 success；mock 只收到 1 次调用；3 个预览；临时项目素材库
  自动新增 5 条素材（= JSON library.created）；审计写「API 调用次数：1 次」；14/14 断言 PASS。
  W0L 占位：success、0 次 API 调用、3 个预览、明确标注占位。
verify_install：20 节点可见（总节点 1032）/ 工作流在 / 巨人包在 / 哈希一致。
真浏览器：P1、W0L、W2、W0 全部打开无节点错误、无未连线、无前端报错。
```

**仍未验证**：真实 DeepSeek Key（无真实 Key）、真实出片（环境墙）、素材图像生成需 GPU 工作流。
完整报告：`docs/ROUND26_大节点整合报告.md`。


---

## 十七、ROUND27：补齐「素材生成 / 素材审核 / 视频审核」（用户第三次补充）

**用户要求**：逻辑图不只有视频生成，还有图片/素材生成、素材审核、视频审核；可以做成两个单独审核节点；
逻辑要清晰完整；如果已有就教怎么用。

### 本轮之前 vs 之后
| 环节 | 之前 | 之后 |
|---|---|---|
| SG2 素材生成 GEN1–4 | ❌ 界面无入口 | ✅ `BetterComfyUIAssetGen`（取任务）+ 任意图像工作流 |
| SG2 入库 G/G7 | ⚠️ 仅手动/演示 | ✅ `BetterComfyUIAssetIngest`（新版本+SHA-256+PENDING_REVIEW，只追加） |
| SG3 素材审核 | ⚠️ 简化版直接 set_lock | ✅ 走 `AssetReviewer` 全状态机：人工/模型、CNT1、超时切模型、唯一提交、LOCK、FALLBACK |
| SG5 段级视频审核 | ⚠️ 回灌节点内 **常量 80 分假通过** | ✅ 真实帧统计评分（`local_scorer`）；评不出就明确失败 |
| SG6 整片 R_full | ❌ 界面没有 | ✅ `BetterComfyUIVideoReview`：R_seg + 边界 + R_full + 阻止/降级策略 |

### 另外修掉 3 个真 bug
1. `FusionIngest` 常量 80 分假审核（假通过）；
2. **ComfyUI 内 `_orch_repo_root()` 误判 `custom_nodes`** → 项目读不到、把项目写到
   `custom_nodes/projects`（杂散目录/演示项目副本的根因；S4 首次 E2E 因此报"项目不存在"）。
   已修复并把杂散目录**非破坏性隔离**到 `_stray_custom_nodes_projects/`；
3. 视频审核「整片阈值覆盖」没写进 R_full 配置（覆盖失效）→ 已修。

### 新增/修改
- 新增 `bettercomfyui/review/local_scorer.py`（真实图像/帧/整片评分，语义维度如实 skipped）、
  `bettercomfyui_comfy/review_nodes.py`（3 节点）、`scripts/build_review_workflows.py`、
  `tests/verify/test_acceptance_32_review_pipeline.py`、`docs/使用指南_素材生成与审核.md`；
- 修改 `nodes.py`（素材审核重写 + 根目录修复）、`fusion_nodes.py`（修假评分）、
  `__init__.py`（节点 20→23）、`test_config_wiring_ratchet.py`（移除已接线的 sampler/batch_count）、
  `verify_install.py`、`_harness.py`（验收 32）；
- 工作流：新增 **`S3_素材生成入库审核（GPU）`**（12 节点）、**`S4_视频审核（本机可跑）`**（5 节点）。

### 证据
```
系统 python tests/verify：EXIT=0，通过=272 失败=0 未验证=1
.comfyenv  tests/verify：EXIT=0，通过=261 失败=0 未验证=3
全量 pytest tests/      ：EXIT=0
契约检查：BLOCKER=0（修掉 review→pipeline 反向 import）
verify_install：23 节点可见（总节点 1035）/ 工作流在 / 哈希一致
节点冒烟（临时项目自动清理）：23/23 断言 PASS
真 ComfyUI S4 E2E：status=success，4 预览（结论/R_seg/R_full/审计），计数与历史落盘，7/7 PASS
真浏览器：S3、S4、W0、W2、P1、W0L 六套全部打开无错误
```

### 仍未做/未验证
- S3 真实素材生成需要**用户的图像模型**（本机无图像权重，未执行真实生成）；
- 本地评分是**启发式**（真实统计，非语义模型）；要语义审核需替换评分模型；
- 视频重生成仍需 GPU；拼接/封装界面独立节点仍未做（库/CLI 已有）。

完整报告：`docs/ROUND27_素材与视频审核补齐报告.md`；使用说明：`docs/使用指南_素材生成与审核.md`。


---

## 十八、ROUND28：两套「总流程」工作流 + 本地理论测试 + 独立审计启动

**用户本轮要求**：做一套更完整、普通用户看得懂的自动化工作流；第一阶段目标是
「人工输入一段剧本 → 完整输出全部内容」；工作流不必极端完整；补视频审核；
本地先做理论测试；然后创建**全新审计智能体**（只读愿景+逻辑图）逐项审查、自动修复；
两轮审查后由 Lead 出总文档与检查结果，用户验收后再给 SSH 做实机验证。

### 已交付（本地）
- **两套总流程**（界面工作流，含中文说明区+分组标题）：
  - `00_【总流程】剧本→全部内容（本机可跑）`（8 节点）：剧本→分镜脚本/提示词JSON(E1–E5+导演组)/
    素材清单/审计，5 个预览框；真 ComfyUI 队列执行 success，7/7 断言 PASS；
  - `00_【总流程】剧本→成片（GPU）`（26 节点）：配置+剧本 → 素材生成→入库→素材审核 →
    导演台出片 → 视频审核 → 结果；schema/browser 通过，含显存分两段跑提示。
- **本地理论测试报告**：`docs/ROUND28_阶段一总流程与理论测试报告.md`
  - 权重文件清单核对（5/5 存在；图像模型空值=必须用户选）；
  - 显存理论：H3 全加载 39.55GB；图像模型+H3 同驻留 ≈46GB → 32GB 卡必 OOM；
    阶段化峰值 24.94GB 才装得下；
  - 任务量/耗时/体积理论区间；本地已证/未证两张清单。
- 新增验收 33（4 条）已过；安装验证通过（23 节点）；双解释器与全量 pytest 均 EXIT=0。
- 新增 `scripts/build_master_flow_workflows.py`；说明：`文档/愿景部分_纯文本.txt`（仅供审计）。

### 进行中
- 独立审计智能体 `auditor-r1`（全新上下文，需求依据仅：愿景纯文本 + 完整逻辑图）：
  第一轮：通读→理解→只读盘点→问题清单→自动修复→跑既有验收→报告；
  之后由 Lead 复核，并让它开启第二轮更深层审查；两轮后 Lead 出总文档与总检查结果给用户。

### 注意
- 用户界面看不到新节点/工作流时：先 Ctrl+F5 刷新，确认连的是 127.0.0.1:8188 本实例。
- 本机仍禁止点 W0/W2/GPU 总流程的队列（34GB 权重会死锁）；本机用「00_本机可跑」。


---

## 十九、ROUND29（2026-10-18 01:30）：成片剪辑补齐 + 两轮审计收口 + 总报告

### 用户新增要求（凌晨）
成片要「剪辑好的视频」（逻辑图 SG6 SPLICE→ENC→R_full 本来就有）；
生产显存 5090-32G，测试机 4090-48G；按两卡参数出效率/质量报表；
测试机环境提前给预装清单（CUDA/cuDNN/PyTorch）；SSH 后检查剪辑/完整性/画质；
可自行租 4090-48G（≤2h，没货刷新等），不行就暂停等早上。

### 已完成
1. **成片剪辑节点** `BetterComfyUIFinalize`（`bettercomfyui_comfy/finalize_nodes.py`）：
   多段拼接（pipeline.splice）→ 封装（pipeline.encode）→ 自检（时长/段数/黑帧/冻帧/静音/
   启发式四维/占位检测）→ 成片路径自动接视频审核。节点数 23→**24**。
2. **三套总流程工作流**（界面目录 17 套）：
   - `00_【总流程】剧本→全部内容（本机可跑）` 8 节点；
   - `00_【总流程】段视频→剪好成片（本机可跑）` 11 节点（新增，本机可跑）；
   - `00_【总流程】剧本→成片（GPU）` 28 节点（含剪辑→视频审核）。
3. **验收 33/34**（总流程 + 成片剪辑），真机 E2E：剧本→全部内容 7/7；
   3 段→成片 7.54s/完整性 passed/R_full/结论 8/8。
4. **两轮零污染审计收口**（详见 docs/AUDIT_AGENT_报告_R1/R2）：
   R1 修 1 BLOCKER（ffmpeg 占位冒充真实生成）+4 MAJOR；
   R2 再修 2 BLOCKER（ComfyUI 缓存使副作用节点不执行；视频审核复用旧结论）+5 项；
   Lead 独立复核全绿。
5. **总文档**：`docs/第一阶段总交付与总检查报告.md`（目标矩阵/总检查结果/未验证清单/怎么用）。
6. **双卡与测试机**：`docs/ROUND29_双卡效率与质量测算报告.md`、
   `docs/测试机环境预装清单.md`、`docs/SSH验收清单_剪辑完整性与画质.md`。

### 总检查结果
系统验收 281 通过/0 失败/1 未验证；venv 270/0/3；全量 pytest EXIT=0；
契约 BLOCKER=0；安装 24 节点/哈希一致；3 套 00 工作流真浏览器全通过。

### 未完成 / 等用户
- 真实 H3 出片、真实图像模型、真实 API/语义审核：需上机；
- 4090-48G 实测数据回填双卡报表：需 SSH（租机自动购买未完成：Edge 正在使用+需账户/支付确认，已按用户指示暂停）；
- 级联返工自动闭环、通知 UI、SG8 分诊：第二阶段/非阻塞。


---

## 二十、ROUND30（2026-10-18 02:30）：普通用户友好化 + 实机工具链补强

- 新增 `docs/一页纸使用说明.md`（含 3 张自绘流程图 `docs/images/流程图_0*.png`）：
  三套总流程怎么选/怎么点/看什么/看不到就 Ctrl+F5；真实界面截图同时保留。
- 新增 `scripts/browser_screenshots.py`（真浏览器拍三套工作流界面）、
  `scripts/make_usage_diagrams.py`（PIL 自绘中文流程图，绕开缩放看不清节点的问题）。
- 新增 `scripts/vram_monitor.py`（32G 红线：峰值/超 30.5GB 告警/可选 /interrupt，
  已在本机 GTX1050Ti 实跑）；`scripts/remote_env_report.sh/.ps1`（Linux/Windows 体检，
  未真机验证）；`scripts/prepare_remote_bundle.py`（8MB 部署包，已实跑）；
  `scripts/ssh_verify.py`（SSH 编排，未真机验证）。
- 基线复跑：系统验收 281 通过 / 0 失败 / 1 未验证，EXIT=0。
- 租机：极智算公开页在售 4090-48G（截图证据），但 Edge 运行中 Cookie 库被锁 + 登录/支付
  需人工确认，按用户指示暂停等早上；`docs/早上开机操作单.md` 已就绪。


---

## 二十一、ROUND31（2026-10-18 02:35）：成片剪辑边界用例 + 实测回填模板

- 验收 34 从 5 条扩到 **9 条**，新增：
  - P6 单段也能出成片（segment_count_ok/duration_ok/passed）；
  - P7 计划段数与实际不符 → 明确判失败（不静默通过）；
  - P8 纯色/无音轨占位视频 → placeholder_flags 命中、passed=False（防冒充）；
  - P9 段文件不存在 → 明确报错（不静默跳过）。
- 新增 `docs/实测数据回填模板.md`：4090-48G / 5090-32G 到机后按表填，
  即可把双卡理论报表变成实测报表（含效率推算公式与质量对比）。
- 系统验收复跑：**285 通过 / 0 失败 / 1 未验证，EXIT=0**；全量 pytest EXIT=0；
  重新打包部署包 `dist/bettercomfyui_bundle_20261018_012906.zip`（326 文件/8.01MB）。
- 仍在等：SSH / 4090 文本编码器方案 / 是否允许自行租机（按用户指示暂停）。


---

## 二十二、ROUND32（2026-10-18 02:50）：成片剪辑「自动发现最新段视频」

- 成片剪辑节点新增可选开关 `自动发现最新视频` + `发现时间窗_分钟`：
  段视频路径列表留空时，自动扫描 ComfyUI 输出目录/项目输出目录里最近 N 分钟的 mp4，
  按时间排序拼接；审计**列出实际用到的文件**（防悄悄拼错）；无文件时明确报错。
- 验收 34 从 9 条扩到 **11 条**：P10 自动发现有文件→成功且证据完整；
  P11 自动发现无文件→明确报错不造假。系统验收复跑 **287 通过/0 失败/1 未验证**；
  真机 E2E 剪辑回归 8/8 通过。
- 两套剪辑相关说明区/一页纸/总报告均已补该开关说明；部署包将在下轮刷新。


---

## 二十三、ROUND33（2026-10-18 03:05）：32G 生产显存「分两段自动编排」

- 新增 `scripts/staged_run.py`：把 GPU 总流程自动拆成
  A 素材段（配置/剧本+素材生成/入库/审核）→（可选）/free→ B 出片段（导演台+剪辑+视频审核）；
  `--dry-run` 可纯本地验证拆分：28 节点 → A 14 / B 15，两段共用 Everything/Settings，
  提交 prompt 无悬空引用；真跑模式支持 `--stage a|b|all`、`--project` 覆盖。
- 新增 `scripts/free_vram.py`：段间 POST /free 释放模型显存（CPU 上验证 HTTP 200 路由通；
  显存是否真下降需 GPU + nvidia-smi 实测）。
- 新增验收 35（4 条）：拆分结构、无悬空引用、/free 路由、CLI dry-run，全过。
- 诚实边界：分两段只能降低显存，**不能在本机证明 5090 实测 <32GB**；
  必须到机用 vram_monitor + 实测回填模板确认。


---

## 二十四、ROUND34（2026-10-18 03:20）：假分数静态审计 + 音画同步节点路径锁定 + 文档索引

- **静态审计**：全产品代码 grep 常量/假成功模式（`= 80.0`、`80.0 for`、`return 80`、
  `passed=True`、`is_mock_output` 等）：**0 处常量假分数**；`占位/mock` 命中均为
  诚实溯源说明（provenance/docstring），非造假。
- 确认审计 R1 已把 `local_scorer.video_scores` 的「音画同步」改成真实测量
  （`_av_sync_score`：两条流真实时长差 + 帧容差 + 线性映射，无音轨记 0）。
- **验收 32 增 P10**：在「视频审核节点」路径上锁死音画同步——等长 A/V ≥90、
  4s视频+1s音频 ≤30、两者差值 ≥50（防回归成常量假通过）。验收 32 现 10 条全过。
- 新增 **`docs/文档索引.md`**：普通用户 3 份先读、到机 4 份、审计证据备查、命令速查。
- 系统验收复跑：**292 通过 / 0 失败 / 1 未验证**；全量 pytest EXIT=0；
  部署包刷新 `dist/bettercomfyui_bundle_20261018_015216.zip`（330 文件 / 8.03MB）。


---

## 二十五、ROUND35（2026-10-18 03:40）：成片质量审核 CLI（一条命令回答剪辑/完整/质量）

- 新增 `scripts/quality_audit.py`：输入成片（可选段清单/计划段数/时长），输出
  Markdown+JSON 质检报告：完整性（时长/段数/音轨）、剪辑性（场景切换/黑帧/冻帧/静音）、
  质量四维（本地启发式）+抽帧画质、占位检测（码率/无音轨/近单色）、结论与边界说明；
  退出码 0=通过 / 1=不通过 / 2=无法检查。
- 本机用合成视频实跑：2 段拼接成片 → 通过；纯色无音轨占位视频 → 不通过（原因明确）；
  示例报告存 `docs/示例_成片质量审核报告.md`（合成测试视频，非真实生成）。
- 新增验收 36（4 条）：正常通过 / 占位不通过 / 段数不符不通过 / 报告章节与边界齐全，全过。
- 已补进 SSH 验收清单、文档索引、环境清单工具表；系统验收与全量测试将在下一轮复跑。


---

## 二十六、ROUND36（2026-10-18 10:20）：RTX 4090-48G 目标机实机验证（用户提供 SSH）

- 机器：4090 48G / 96G RAM / 500G 数据盘 / Ubuntu22.04 / ComfyUI 0.33.0 生产镜像（H3 权重+TurbLoRA+SDXL 齐全）。
- 部署（只传源码 8MB，其余远端下载）：我们的插件 **24 节点全部注册成功**；编排包用
  BETTERCOMFYUI_HOME=/data/BetterComfyUI 正确解析。
- 环境修复（远端下载安装）：恢复系统 python3（venv 损坏）、python3.10-dev（Triton 编译）、
  apt ffmpeg、pip imageio-ffmpeg/safetensors。
- **测试机当场修复 3 个真问题**：
  1. 叙事流畅公式对单镜头成片天然误判（真实采样 12/12 恒 50）→ 片段级（≤90s）只在机械快切扣分；
  2. 视频审核遇到 5.17s 片段按 5s 切出的 0.17s 尾段抽帧失败会中断整次审核 →
     记录 too_short 并继续；本地新增验收 32 P11 回归；
  3. 镜像生产工作流自定义节点字段与通用 JSON→API 转换器顺序不一致（400）→ 改用官方前端提交流程。
- 实机结果：
  - 3 个真实 E05 take → 我们的成片剪辑节点：**成片 25.47s、完整性 passed、场景切换 2（≈3-1）、
    质量审核「通过」**（叙事100/音画99.3/节奏73.6/跳变83.3）；
  - 真实 H3 生成（镜像 turbo 工作流）：**冷 112.83s / 热 82.45s**，864x480 5.17s；
    **显存峰值 40.93 / 45.58GB** → 32G 生产机必须分阶段（实测支撑）；
  - 生成成片质检「通过」；视频审核节点修复后 R_full 正常执行。
- 报告：`docs/ROUND36_目标机实机验证报告.md`；双卡报表已补 4090 实测章节。
- 本地回归：验收 32 = 11 条全过；系统验收 **297 通过/0 失败/1 未验证**。
- 待办（需 5090 生产机）：5090 实测效率/峰值、双卡实测对比 5090 列、我方导演台节点在该镜像上的适配、
  S3 素材生成用远端 SDXL 实测。


### ROUND36 收尾（2026-10-18 09:40）
- 全量回归又发现并修复第 4 个问题：无音轨成片被其它维度平均后仍判“通过” →
  `full_review` 增加硬约束（音画同步<=0 → 整片不通过），验收 32 增 P12；审计 R2 原失败用例恢复通过。
- 最终：系统验收 **298 通过 / 0 失败 / 1 未验证**；全量 pytest EXIT=0；venv EXIT=0。
- 实机证据归档：`docs/remote_evidence_4090/`（显存 CSV、拼接质检、生成质检、视频审核 JSON 等）；
  生成的 mp4 证据在本机 TEMP：`bcu_remote_evidence/MiniMax_H3_00002_.mp4 / _00003_.mp4`。
- 远端 ComfyUI 仍在 8188（外网 8188）运行，插件 24 节点在。


---

## 二十七、ROUND38（2026-10-18 10:45）：三错误修复落地 + 高清中文宣传片 + S3 真机链路

- 用户报「全流程 剧本→成片 GPU 版」三错误：① 节点错误=缺 MiniMaxH3Director 巨人；② 两个输入错误=
  远端前端 1.48.7 多 control widget 导致 KSampler 值整体错位 + ckpt 空/CreateVideo/SaveVideo 字段错；
  ③ 模型缺失=LoRA 本地名不存在、CLIP 用了 NVFP4、ckpt 空。
- 修复：上传并安装 5 个巨人包（8 个关键节点注册成功；DNNodes 因缺 comfyui-SelfLift 跳过，本工作流不需要）；
  产出 `docs/远程适配工作流/00_剧核总流程_4090适配版.json`（28 节点/31 连线，0 缺失、模型全部命中）
  并已上传远端 UI；修复 S3 工作流「审核先于入库」依赖边并重建（12 节点/15 连线）。
- 真机验证：适配版全链前端加载 28 节点/0 badNodes、graphToPrompt 成功；S3 真机 SDXL 生成 12.36s →
  入库 v1+sha256 → 审核 60.2<70 → BLOCKED/CNT1=1/无 LOCK（符合 R3）。
- 宣传片：`docs/宣传片_剧核_DramaCore_高清中文版.mp4`（1080p/25.67s/15.83MB；5 个真实 H3 镜头 +
  RealESRGAN 模型超分 + 剪辑自检；全中文提示词；质检仅节奏项偏低，如实记录）。
- 多智能体并行：promo-agent（高清宣传片）、fix-agent（三错误诊断+适配+脚本）均已完成并回收。


---

## 二十八、ROUND39（2026-10-18 11:00）：高清效率更正 + 整片预览修复 + 单节点入日程

- 用户指正：正常生成也要高清（此前 864×480 效率测试口径不对）。4090-48G 实测：
  **720p(0.9MP) 单段 232.03s、显存峰值 41.09GB**（480p 为 82.45s）→ 1 小时片 720p ≈23.2h；
  720p 峰值 41GB>32GB → 5090-32G 不能直接 720p 全驻留，生产推荐 **480p 生成 + 模型超分 1080p**
  （实测 120~125s/段，宣传片 5/5 成功）；1080p 原生测试进行中。
- 修复用户反馈 bug：ComfyUI 网页端看不到「一整个成片」→ 成片剪辑节点导出成片到
  `ComfyUI/output/剧核成片/` 并返回 `images/animated/gifs` 预览；本地真机队列验证 PASS。
- 用户要求的「一个超长大节点」（无外部依赖、复制巨人逻辑进我们节点）：因全链测试未完
  （5090 未实测、真实 Key 整链未跑），按用户条件**先入日程** → `docs/待办_下一阶段_超长大节点.md`。
- 报告：`docs/ROUND39_高分辨率效率修正报告.md`；双卡报表已更正。


### ROUND39 补充（2026-10-18 11:20）
- **1080p 原生实测成功**：1920×1088、5.167s、**802.28s/段、显存峰值 42.43GB**。
  结论：48G 能跑但生产不可接受（360 段≈80h）；生产 1080p 走「480p 生成+模型超分」（≈120~125s/段，360 段≈12h@4090）。
  已回填 `docs/ROUND39_高分辨率效率修正报告.md` 与双卡报表。
- 修 bug：成片剪辑「自动发现最新段视频」会误扫本节点导出的成片（导致全量测试失败）→
  排除 `剧核成片/` 导出目录与宣传片/导出文件名，pytest 环境不再扫描 `.comfyui/output`；
  验收 34 恢复 11 条全过、系统验收 298 通过/0 失败。


### ROUND39 收尾（2026-10-18 11:35）
- 修复整片预览（导出 ComfyUI output + ui 预览，本机真队列 PASS）；
- 真实结果版宣传片交付：`docs/宣传片_剧核_DramaCore_真实结果版.mp4`
  （1080p/34.46s，5 真实镜头+4 真实数据卡；音画同步修复为 100；仅节奏 63.6 偏低，如实记录）；
- 修自动发现误扫导出成片 bug；系统验收 298 通过/0 失败；部署包刷新。


### ROUND40（2026-10-18 11:45）：按输出秒归一化效率
- 新增 `scripts/estimate_efficiency.py`（480p/720p/1080p/480p+超分四种路线，可算每秒/每分钟/每小时/N段/5090估算）。
- 4090 实测归一化：480p **16.0s/输出秒**、720p **44.9s/输出秒**、1080p **155.3s/输出秒**、
  480p+超分1080p **24.1s/输出秒**；单段=5.167s/124帧@24fps。
- 报告：ROUND39 §7 已补完整表；双卡报表已引用。


### ROUND41（2026-10-18 11:20）：单节点 `BetterComfyUIOneStop` 真机跑通（无外部依赖）
- 按用户要求：导演台源码 vendor 到 `bettercomfyui_comfy/vendor/h3_director/`（1.49MB，保留 LICENSE），
  新建单节点 `OneStop`：剧本→一次API/占位→提示词→素材登记→节点内加载模型→导演台出片→审核→剪辑→成片。
- 4090 真机 3 次运行，修复 2 个 bug（loader 跨版本 weight_dtype、audio 列表未归一化）后通过：
  **119.20s（含加载）/出片 93.8s、124帧/864×480/5.167s/含音频、审核通过**；
  成片导出 `ComfyUI/output/剧核成片/大节点成片_2.mp4`（网页可播放整片），本地质检亦通过。
- 远端 UI 已放单节点工作流 `【剧核】单节点_剧本到成片.json`（只有一个节点）。
- 边界：素材生图/审核尚未并入单节点（v0.1）；真实 API 路径需用户 Key 才能端到端验证。
- 报告：`docs/ROUND41_单节点首版真机验证.md`；说明：`docs/单节点_剧本到成片_使用说明.md`。


### ROUND44（2026-10-18 15:30）：真实 UI 点击测试通过 + 多段连续性结构验证 + 发布口径修复
- **真实 UI 操作测试（4090 本机 Chrome）**：载入 `【剧核】单节点_剧本到成片.json`（95 控件/0 缺失节点）
  → 只填剧本(52字) → 点 `#queue-button` → **success 160.5s**；R_full 92.08 通过，显存 44640MB；
  截图/结果：`docs/remote_evidence_4090/ui_test/`。
- **P0-2 默认系统提示词**：修复并真机验证（审计「系统模板=严格 JSON（面板默认=是）」）。
- **P0-3 幽灵尾段**：尾段 <1s 忽略 + 抽帧 <2 帧按 too_short；不再误报段2/边界失败。
- **完整性误报**：改按真实生成帧数校验（导演台 H3 帧网格取整），并输出计划/实际说明。
- **P0-6 缺段假成功**：新增计划段数/时长 vs 实产硬校验（缺段或时长<70% → 不通过）。
- **多段连续性**：2 段真机测试结构成立（248 帧、ON=True/guide/22f、pin=[2]、显存 28.8GB、R_full 97.2）；
  暴露「分段编码只出 1 段」bug，已本地修复（合并帧均分 + P0-6），**远端待复测**。
- 本地回归：verify 298 通过/0 失败/1 未验证；全量 pytest EXIT=0。
- 远端 15:05 起 502/SSH 超时（主机 ping 通），多段端到端复测待机器恢复；恢复命令见 ROUND44 §5。
- 报告：`docs/ROUND44_单节点全功能_UI真机与连续性测试.md`；发布评估：`docs/剧核_发布评估.md`（更新版）。


### ROUND47（2026-10-18 16:20）：真实 Key 全链 + UI 移植 + 逻辑对照 + 功能补齐
- 新容器端口：SSH 22；**ComfyUI 外网 http://your-gpu-host:8188**（旧 8188 已不是 ComfyUI）。
- **真实 Key 全链（P0-1 关闭）**：API 模式 185s（API 1 次、素材 9→3 LOCK、R_full 97.2 通过）；
  **真实 UI 点击 200.4s 成功**（只填剧本→点运行；R_full 98.06/R_seg 89.8；显存 44864MB）。
- **导演台 UI 移植完成并部署**：22 帧连续专区（开关/5-22-39-56/音频24/pin 可视化）、素材卡、canvas 时间轴；
  远端截图 `docs/remote_evidence_4090/real_key_ui/07_新导演台UI_远端.png`；本地 30/30 + 兼容 7/7。
- **逻辑图逐段对照**：`docs/剧核_逻辑图逐段代码对照.md`（417 行，157 节点 ✅111/◐38/❌8，带文件:行号；22 帧 16 步调用链）。
- **ROUND46 功能补齐真机验证**：转场/字幕/水印/封面/mov、长片分批+断点续跑、自动返工、22f 连续均跑通；
  唯一未闭环：**2 段返回仅 126 帧（计划 248）**，P0-6 判不通过（无假成功）；下一步改用导演台落盘分段 mp4 拼接。
- 开源安全：bundle 排除 config/user.yaml（只含 example）；.gitignore 加 user.yaml；建议轮换已泄露 Key。
- 报告：`docs/ROUND47_真机全链与UI移植_逻辑对照.md`。


### ROUND47 补充（16:35）：P0-4 修复真机验证 → 关闭
- 根因：导演台 `exportMode="segments"` 把分段 mp4 落盘、images_out 只回部分帧；修复为 `exportMode="all"`。
- 2 段真机：成片 **10.417s / 864×480 / 24fps / 含音频**（计划 10.33s）；**完整性=True、审核结论=通过**；
  R_seg 两段通过（94.49/87.38）、R_full 95.26 通过；22f 连续 ON/guide/22f/pin=[2]；显存 29600MB。
- 证据：`docs/remote_evidence_4090/round46/round46_test_fixed.json`、`二段连续_转场字幕水印_10.4s.mov`。
- 发布判定：全功能版 P0-4 关闭；剩 5090 条件项 + 已声明 P1 边界。


### ROUND47 最终补充（17:00）：新 UI + 真实 Key + strict_json 最终验收通过
- 新导演台 UI（109 控件/0 缺失）只填剧本→点运行：**success 170.3s、API 1 次**；**完整性 True、R_full 97.63 通过**；显存 44928MB。
- 真实 API 无效素材引用改为**丢弃+逐条告警**（不整次失败、不伪造），仅真实 API 路径启用，默认校验仍严格。
- 本地回归：verify **298 通过/0 失败/1 未验证**；全量 pytest **EXIT=0**。
- 证据：`docs/remote_evidence_4090/final_ui/`、`docs/ROUND47_真机全链与UI移植_逻辑对照.md` §八、`docs/剧核_DramaCore_发布说明_v0.1.md` §八。


### ROUND53（2026-10-19）：已知问题修复并发布 v0.1.1-internal
- 默认值对齐：每段 10s、1920×1088、素材最多 4、CNT2/CNT3=3；工作流重新生成（110 控件）。
- 人工审核真实等待（pending/decision 文件队列 + 超时切模型）；素材审核四维补全（一致性=参考版本相似度、合规=违禁词规则）。
- 4 个未接线配置项全部接线（normalize_names / retry_interval / regenerate_granularity / checkpoint_every_stage），扫描器 0 未接线。
- 阶段检查点、返工粒度 full/segment、回归测试 7 项；verify 298/0/1、全量 pytest EXIT=0。
- 发布：GitHub commit 051f56c，tag/Release v0.1.1-internal（内测预发布，含 bundle 附件）。
- 仍开放：GPU 真机完整回归（机器暂不可租）、片头片尾、版本回滚入口、tiled、跨批 pin 近似、级联1层等（见 docs/ROUND53_已知问题修复记录.md）。


### ROUND54（2026-10-19）：三路逻辑审计 + 多段剧本 + 素材上限 64 → v0.1.2-internal
- 三专项智能体独立审计（只读）：ROUND54-A 21 条逻辑问题（P0=4）；ROUND54-B P0=2/P1=10；ROUND54-C 多段剧本设计+Top10 建议。
- 修复：CNT 重复计数、断点指纹扩展+坏段校验、人工审批 nonce/消费、多段剧本总片 R_full+缺章策略+章检查点、按实际段时长审核、单音轨按段切分、LOCK 内容哈希校验、Finalize 自动发现收敛、输出名安全化。
- 新功能：多段剧本顺序生成（每章 1 API、同项目素材复用、章级断点、总片审核）；素材最多生成 64（默认 12，挂参考图按导演台上限 9 截断告警）。
- 测试：verify 298/0/1；全量 pytest EXIT=0；ROUND53+54 单测通过；配置扫描 0 未接线。
- 发布：GitHub main 97d0409，tag/Release v0.1.2-internal（内测预发布，含 bundle 附件）。
- 仍开放：GPU 真机完整回归（机器不可租）、章间 22 帧近似、片头片尾、版本回滚、tiled、级联 1 层、5090 等。
