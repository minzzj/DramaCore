# 第十六轮：接缝审计 —— 找到两个「假成功」级问题

> 目标：「不断重新阅读文档、不断完成我的愿景」。
> 本轮按图七的数据契约做逐字段审计，**挖出两个此前所有测试都没覆盖的接缝问题**。
> 也修正了我自己上一轮的说法。

---

## 一、必须先修正我自己的话

我上一轮写了「任务书 §9 的 15 条验收清单**逐条 PASS**」。这句没错，
但**口径不够**，容易让人以为"产品路径已经全通"。实际情况是：

> **验收测试验的是「库函数」，不是「产品路径」。**

具体证据（本轮实测）：

| 函数 | 在产品代码里被调用 | 在测试里被调用 |
|---|---|---|
| `validate_script` | **0 次** | 13 次 |
| `match_candidate` | ✅ `demo.py:297` | 有 |
| `build_prompt_messages` / `build_gateway` | **0 次** | 有 |
| `parse_script_output` | **0 次** | 有 |

所以 15 条全绿与「界面路径真的按契约走」**是两件事**。
这正是「两半各自绿、接缝断了」的典型形态 —— 本轮就是去验接缝。

---

## 二、发现 1：剧本形状不一致 → **假成功**（最严重）

### 两处不一致

| 环节 | `assets` 放哪一级 | 用什么键 |
|---|---|---|
| `validate_script` 规范输出 | **顶层** | `asset_type` |
| `demo.py` 读取（`demo.py:296`） | **章节级** `chapter_obj.get("assets", [])` | `type` |

### 决定性证据（`scripts/verify_seam_script_shape.py`）

把三种形状分别喂给 `run_demo`，看素材阶段计数：

| 剧本形状 | ok | 素材新建 | 复用 | 疑似 | LOCK | 段 |
|---|---|---|---|---|---|---|
| ① **规范形状**（顶层 + `asset_type`） | **True** | **0** | 0 | 0 | **0** | 2 |
| ② 章节级 + `type`（demo 真正期望的） | True | 2 | 0 | 0 | 2 | 2 |
| ③ 章节级 + `asset_type`（键名不一致） | False | 0 | 0 | 0 | 0 | 0 |

**① 是假成功**：`ok=True`、还生成了 2 个段，**但一个素材都没建**。

### 后果（为什么这条最严重）

* 素材的「匹配 / 复用既有 / 疑似队列 / 生成 / 审核 LOCK」**整段静默跳过**；
* 导演台拿不到 `locked_assets` → **身份一致性的依据直接消失**
  （愿景原话：素材库参考图是身份一致性的核心）；
* 而 `run_demo` 仍返回 `ok=True` → 用户与上层都**看不出出了问题**。

### 附加事实：内置演示剧本自己就不符合契约

```
[B] 内置 DEMO_SCRIPT 过 validate_script：不通过
    （ScriptValidationError：$.assets [missing_field] 缺少必填字段 assets）
```

`DEMO_SCRIPT` 的 assets 在章节级，而 `validate_script` 要求顶层。
所以**演示脚本与它自己的契约不一致**，只是因为它从不被送去校验。

### 界面可达性：**可达**

`BetterComfyUIRun`（`nodes.py:309`）→ `demo.run_demo(chapter=...)`。
用户在节点的 `script_text` 里粘贴一份**规范剧本**（顶层 assets），
JSON 语法能过、`run_demo` 也会返回成功，但素材阶段空转。

---

## 三、发现 2：界面路径**没有结构校验**

任务书 §B 明确要求：

> 解析后先做结构校验；**缺字段、无效引用、重复 ID 或格式错误时，
> 不得直接进入素材生成**。

`BetterComfyUIRun` 的实现（`nodes.py:260-267`）：

```python
if script_text and script_text.strip():
    try:
        script = json.loads(script_text.strip())      # 只校验 JSON 语法
    except json.JSONDecodeError as exc:
        raise _NodeError("剧本内容不是合法 JSON：...")
```

**只有 JSON 语法检查，没有 `validate_script`。** 全工程（产品代码）对
`validate_script` / `parse_script_output` 的调用数是 **0**。

所以：

* 「无效引用」「重复 ID」「缺字段」**不会被拦**；
* 验收 02（无效 JSON 被拦截）验的是**库函数**，不是这条路径
  —— 所以它绿着，但界面上的保护其实没有。

---

## 四、发现 3：`new_asset_candidates` 是死字段

`entities.py:571` 定义了 `Chapter.new_asset_candidates: list[dict]`，
标注是「章节新素材候选」，但**全工程无人写入、无人读取**。

图七 `E1F/E2F/E3F`（角色/场景/物品图提示词：`id/name/prompt/style/ref_images`）
目前没有对应的结构化落点 —— 走的是 `validate_script` 的 `assets[]`（顶层）。

---

## 五、图七数据契约逐字段审计（其余项）

| 契约 | 字段 | 结果 |
|---|---|---|
| `PF` 素材生成参数 | 分辨率/采样步数/采样器/CFG/种子/批量数量 | ✅ 6/6（`AssetGenParams`） |
| `DF` 导演台参数 | 分段时长/分辨率/帧率/尾部帧数/首尾帧/显存档位/剪枝 | ✅ 7/7（`DirectorParams`） |
| `EF` 成片封装参数 | 音频拼接/转场/字幕/水印/格式/编码/CRF | ✅ 7/7（`EncodeParams`） |
| `RF` 审核参数 | 评分维度/阈值/重试上限/超时时间/通知方式 | ✅ 5/5（`AssetReviewParams`） |
| `JF` 导演台入参 | 素材引用/任务模式/首帧/尾帧 | ✅ 4/4（`Segment`） |
| `E5F` 最终视频提示词 | id/segments/任务类型/参考素材标签 | ✅ 4/4（`Segment`） |
| `GF` 素材条目 | 类型/名称/路径/版本/审核状态/评分/引用次数/锁定状态 | ✅ 8/9（`AssetEntry`+`AssetVersion`） |
| `GF` 素材条目 · **序号** | — | ⚠️ 无独立字段（落在 `asset_id` 里：`{项目}_{类型}_{序号}_{uuid8}`） |
| `E4F` 分镜图提示词 | id/name/prompt/shot_index/duration | ⚠️ `Shot` **缺 `name`** |
| `FF` 分隔符 | 分隔符形式/**段开始/段结束/字段分隔** | ❌ **不可配置**（`validate.py` 里是硬编码正则） |
| `XF` @素材类型_序号_名称 | — | ✅ 已按此约定实现（一体化导演台） |

---

## 六、验证汇总

| 项 | 结果 |
|---|---|
| 接缝证据 | `scripts/verify_seam_script_shape.py` 退出码 **1**（复现了缺陷） |
| 全量测试 | 仍 **EXIT=0**（这些缺陷**测试覆盖不到**，见第一节说明） |
| 新增脚本 | 1 个（接缝验证） |
| 产品代码改动 | **无**（按你的要求，只取证不动手） |
| 机器稳定性 | 未再蓝屏 |

---

## 七、建议的修法（**等你点头再动**）

按影响排序：

1. **接缝归一化（必须）**：`demo.py` 消费剧本前先过 `validate_script`，
   或同时接受两种位置与两种键名。按 `AGENTS.md`：
   *"Normalize third-party or upstream return conventions at the integration boundary."*
2. **界面路径补校验**：`BetterComfyUIRun` 在 `json.loads` 之后调 `validate_script`，
   把结构错误挡在素材生成之前（任务书 §B 的硬要求）。
3. **演示剧本对齐契约**：让 `DEMO_SCRIPT` 也满足 `validate_script`，
   免得它成为"唯一能跑的、却不符合契约的例子"。
4. `Shot` 补 `name`；`FF` 分隔符做成可配置（低优先）。
5. 清掉死字段 `new_asset_candidates`（或真正用起来）。

**另需补一条测试**：把"接缝"本身纳入验收 ——
现在 `verify_seam_script_shape.py` 是脚本，建议提升为
`tests/verify/test_acceptance_16_seam_shapes.py`，否则这类问题还会漏。
