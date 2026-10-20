# 第四轮进展记录（2026-10-17 凌晨）

> 目标轮次 2/60。本轮把《AI Agent 开发任务书》第 3 节 E + 第 4 节的
> **「单段覆盖必须显式记录」**从"契约里写着但没实现"变成真正生效，
> 并在过程中修掉两个自己代码里的真 bug。

---

## 一、为什么挑这一条

任务书第 3 节 E 段原文：

> 全局参数统一注入；**单段覆盖必须显式记录。**

第 4 节又把 `audit_scope` / `threshold` / `retry_limit` / `timeout`
列在**视频段**的字段里（不是只放全局）。

审计发现：这四项只在全局配置里有，**段上没有**；只有 `generation_params`
能被段覆盖。这正是任务书开头批评的那类问题：

> 不能靠隐式文本传递关键状态。

而且"显式"两个字是有实际意义的 —— 隐含覆盖是排查灾难：
某个段审核松了、重试多了，事后只能靠人回忆"当时是不是我改过这一段"。

---

## 二、实现：`SegmentOverrides`

新增于 `bettercomfyui/models/entities.py`：

| 字段 | 含义 | 不填时 |
|---|---|---|
| `audit_scope` | 审核范围 segment/full/both | 用全局 |
| `threshold` | 该段通过阈值 | 用全局 `threshold_segment` |
| `retry_limit` | 该段质量重做上限（CNT2） | 用全局 |
| `timeout_s` | 该段生成/审核超时 | 用全局 `task_timeout_s` |
| `generation_params` | 生成参数覆盖 | 用全局导演参数 |
| `reason` | **覆盖原因**（审核类覆盖必填） | — |
| `by` | 谁覆盖的（human/model/system） | `human` |

设计要点：

1. **`None` = 不覆盖**，语义明确，不是"没实现"；
2. `resolve(cfg)` 返回生效值 **+ 哪些字段被覆盖**（审计用）；
3. `resolve_generation(cfg.director)` **逐键合并** —— 只改步数不会把
   分辨率/fps 一起丢掉（整体替换是常见错误）；
4. 非法值在 `__post_init__` 当场报错（不等到执行时才炸）；
5. `describe()` **同时给出字段名与中文说明**，便于和人/日志/配置对照。

### 关于"原因"的要求：分两类，不是一刀切

第一版我要求**所有**覆盖都必须写原因，结果把既有测试打红了 ——
那是**过度设计**：脚本里每个生成参数微调都要写小作文，
反而会促使人绕过机制。

改成按风险分层：

* **审核类覆盖**（`audit_scope`/`threshold`/`retry_limit`/`timeout_s`）
  → **必须写明原因**。它们直接决定"这一段算不算通过"；
* **生成参数覆盖** → 只要求可追踪（`params_source=segment_override`），不强制原因。

### 剧本里的写法

```json
{
  "segment_index": 2,
  "prompt": "高潮段：更严标准",
  "asset_refs": ["A1"],
  "review_overrides": {"threshold": 91.0, "retry_limit": 6},
  "override_reason": "本段是高潮镜头，必须更高标准",
  "generation_params": {"steps": 40}
}
```

声明了 `review_overrides` 却不给 `override_reason` → **计划阶段就报错**，
不允许带病进入生成。

---

## 三、关键：只存下来是不够的（消费者必须真的用）

写完 `SegmentOverrides` 后我去查了**谁是消费者**，发现：

```python
@property
def threshold(self) -> float:
    return float(cfg_get(self.cfg, "video_review.threshold_segment", 70.0))
```

审核器**只读全局阈值**。也就是说覆盖会被存下来、被 `describe()` 打印出来、
审计日志里写着"已覆盖"—— **但对判定结果毫无影响**。

**这比不支持覆盖更糟**：用户以为改了，实际没改，而且日志还"证明"改了。

修法（`bettercomfyui/director/review.py`）：

* 新增 `threshold_for(segment)` / `audit_scope_for(segment)`：覆盖优先，否则全局；
* 段级审核路径改用它们；
* **边界（N1）取两段中更严的阈值**：边界属两段共有，必须同时满足双方标准。
  否则前段要求 95、后段用全局 70 时，边界按 70 放行 —— 前段的高标准被绕过。

---

## 四、本轮抓到的两个真 bug（都是我自己的代码）

### B-1 `describe()` 用了 `json` 但没导入

`SegmentOverrides.describe()` 里 `json.dumps(...)`，
而 `entities.py` **没有 `import json`** → `NameError`。

是测试抓到的（而不是靠读代码）。修法：补 import。

顺带改进：`describe()` 原本只输出中文标签（"生成参数"），
grep 不到；改为**字段名 + 中文**（`generation_params（生成参数）`），
既能人读也能机器对照。

### B-2 我的"必填原因"规则过严，打红了既有测试

见上文第二节。这不是测试的问题，是**设计过严**。
按风险分层后既有测试全绿。

> 这条值得记：把"严格"当成"正确"会误伤合法用法。
> 规则应该跟着**风险**走，而不是跟着"我觉得应该严格"走。

---

## 五、验证

### 新增测试 28 条（`tests/test_segment_overrides.py`）

| 分组 | 覆盖 |
|---|---|
| 不覆盖的语义 | `is_empty` / `resolve` 返回全局值 |
| 覆盖生效 | 审核四项生效；**逐键合并**（只改 steps 不动 resolution/fps） |
| 原因要求 | 4 类审核覆盖缺原因各自报错；有原因通过；生成参数覆盖不要求原因 |
| 非法值 | 5 类越界当场报错 |
| describe | 字段名/中文/原因/操作者都在 |
| Segment 集成 | dict 与对象两种入参；序列化往返 |
| **消费者** | 审核器读到段级阈值/范围；**同一 80 分在阈值 95 下必须不通过**；边界取更严者；85 分在「前段 95 / 后段 70」边界必须不通过 |

### 变异测试（证明守卫真的有效）

把边界逻辑从 `max(前段, 后段)` 改成只看后段：

```
MUTATION: boundary uses only latter segment's threshold
→ FAILED test_boundary_rejects_score_between_global_and_strict
```

杀掉，说明这条测试真的在测它声称的东西。

### 全量

| 项 | 结果 |
|---|---|
| 全量测试 | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |

---

## 六、当前状态

| 项 | 结果 |
|---|---|
| 全量测试 | EXIT=0 |
| 权重下载 | 🔄 **10.17 / 59.38 GB**（文件 1 已 9.88/19.53 GB） |
| ComfyUI | 0.41.0，14 个我的节点 + 20 个 MiniMaxH3* |
| 真实 H3 出片 | ❌ 仍需 ≥24GB 显存 |

---

## 七、仍未验证（未变）

1. 真实 H3 视频出片（需 ≥24GB 显存）。
2. 真导入 `pack_*` 的运行时字段比对（需在 ComfyUI 运行时内执行）。
3. 网页面板在浏览器里的最终渲染。
4. 参考音色链路与导演台高级模式接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出。_
