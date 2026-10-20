# 第六轮进展记录（2026-10-17 凌晨）

> 目标轮次 4/60。本轮补齐 goal 第 3 项的**后半句**：
> 「把 Director 的 images/audio 存成段版本**并触发段级审核与 CNT2/CNT3**」。
> 上一轮只做到"存成段版本"；审核与计数是空的。

---

## 一、缺口：回灌只存文件，不审核、不计数

上一轮的回灌节点把产物写成了真实段版本（`SEG0001@v1`），但：

* **不触发段级审核** —— 那么「未审核前不得进入拼接」这条规则根本无从执行：
  谁都不知道审没审、通过没有；
* **不计 CNT2/CNT3** —— 返工次数在任何地方都看不到。

而任务书第 3 节 F 明确要求 `CNT2`（本段质量不合格重做）与
`CNT3`（因前段变化触发衔接重做）**分开累计**（硬规则 R4）。

---

## 二、实现：审核 + 记账接进回灌路径

### 新增界面控件

| 控件 | 取值 | 作用 |
|---|---|---|
| `review_and_count` | 默认开 | 回灌后立即做段级审核；重做版本按规定记账 |
| `retry_kind` | 自动判断 / 质量重做(CNT2) / 级联重做(CNT3) / 首次生成不计 | **这一段为什么重做** |

`自动判断` 按段的 `cascade_marked` 决定：被级联标记过 → CNT3，否则 → CNT2。

### 记账时机（一个容易搞错的地方）

**第 2 个及以后的版本才算"重做"**；第 1 版是首次生成，不计 CNT2/CNT3
—— 首次生成不是返工，把它算进去会让"返工率"这个指标一开始就是错的。

### 计数器持久化

`CounterStore` 本身不落盘（只是内存表 + 事件日志），所以显式用
`<project>/counters.json` 做持久化 —— 否则重启后返工次数全丢。
文件损坏时**如实警告并从零开始**，不静默假装有数据。

### 审核失败绝不假装通过

审核抛异常时输出：

```
审核：**未完成** —— RuntimeError: 模拟评分器崩溃
      该段不得进入拼接（审核未通过不等于可以放行）。
```

并且审核结论会**写回段存储**（`review_status` / `review_result`），
而不是只存在于当次输出。

---

## 三、因此抓到一个真 bug：新版本**永远无法被审核**

实测第一次跑到第 3 版时：

```
审核：**未完成** —— StaleSegmentVersion:
      审核对象版本 v3 与段 SEG0001 当前版本 v1 不一致：旧版本审核结论不得用于新版本
```

### 根因

上一轮写的 `SegmentStore.add_version` 只推进了 `candidate_version`，
**没有推进 `current_version`**。

而审核器的契约是（`director/review.py` docstring 原文）：

> **先登记候选尝试并把 `segment.current_version` 设成待审版本**，再调用本方法；
> 否则 `version != current_version` 会抛 `StaleSegmentVersion`（守卫是对的）。

所以每个新版本都会被判为 stale —— **回灌越多次，越审不了**。

### 修法：让新版本成为 current，而不是放宽守卫

守卫本身是对的，而且它正是「迟到结果不得覆盖新版本」（验收 9）的实现基础。
新版一落地，旧版的审核结论应当**立刻失效** —— 这正是我们想要的。

所以修的是 `add_version`：新版本同时成为 `candidate_version` 与 `current_version`。

并补了一条**反向断言**测试，防止后来者为了"修 bug"而放宽守卫：
`test_stale_review_is_still_rejected_after_new_version` 要求
新版落地后旧版审核**仍然必须**被拒绝。

---

## 四、另一个一致性问题：配置与路径来自两份来源

测试里我发现计数写到了**真实的 `projects/`** 而不是临时目录。

原因：`_resolve_project_paths(project_id)` 内部**自己加载配置**，
而节点用的是 `fusion_nodes._cfg()`。两边一旦不同（例如换了项目根），
就会"审核用这份配置、路径用那份配置"。

修法：让 `_resolve_project_paths(project_id, cfg=None)` 支持显式传入配置，
节点把自己的配置传下去 —— 保证调用方与路径解析**必然同一份**。

> 这类问题在真实使用里也会发生：多项目切换时若有任何一处用了缓存配置，
> 就会出现"审的是 A 项目、写的是 B 项目"。所以这不是只为测试而改。

---

## 五、验证

### 实测（ComfyUI venv，真 torch）

三种 `retry_kind` 连续回灌同一段：

```
retry_kind = 首次生成不计
   记账：本版不计 CNT2/CNT3（retry_kind=首次生成不计）
   审核：通过（score=80.0 / threshold=70.0，audit_id=AUDIT_SEG0001_v4）

retry_kind = 质量重做(CNT2)
   记账：CNT2（质量重做）= 3（第 5 版，前 4 次属重做）
   审核：通过（audit_id=AUDIT_SEG0001_v5）

retry_kind = 级联重做(CNT3)
   记账：CNT3（级联重做）= 1（第 6 版，前 5 次属重做）
   审核：通过（audit_id=AUDIT_SEG0001_v6）
```

`counters.json` 落盘结果：

```json
{ "counts": { "CNT_SEG0001_CNT2": 3, "CNT_SEG0001_CNT3": 1 }, "tech_retry": {} }
```

**两个计数器完全分开**（R4），且每个版本都有自己的 `audit_id`。

### 新增测试 12 条（`tests/test_ingest_review_counters.py`）

首次不计数、审核结论写回段、审核结论进材料、CNT2 只动 CNT2、
CNT3 只动 CNT3、两者独立累计、自动判断用 cascade 标记、计数可重载、
新版本成为 current、**旧版审核仍被拒绝（反向断言）**、
审核崩溃不假装通过、显式跳过审核必须说明。

### 双解释器全量

| 解释器 | 结果 |
|---|---|
| 系统 Python（mock 路径） | **EXIT=0** |
| ComfyUI venv（真 torch，新测试在这里跑） | **EXIT=0** |
| 独立验收（两者） | 92 通过 / 0 失败 / 0 未验证 |

### ComfyUI 实测

```
ingest required: project_id, segment_index, images, fps
ingest optional: audio, frame_count, source, run_report, save_preview_png,
                 review_and_count, retry_kind
retry_kind opts: 自动判断 | 质量重做(CNT2) | 级联重做(CNT3) | 首次生成不计
```

---

## 六、当前状态

| 项 | 结果 |
|---|---|
| 全量测试（系统 Python） | EXIT=0 |
| 全量测试（ComfyUI venv） | EXIT=0 |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| goal 第 3 项 | ✅ 回灌 → 段版本 **+ 段级审核 + CNT2/CNT3** 全部落地并实测 |
| ComfyUI | 0.41.0，我的 14 节点，`MiniMaxH3*` 20 个 |
| 权重下载 | 🔄 **17.67 / 59.38 GB** |
| 真实 H3 出片 | ❌ 仍需 ≥24GB 显存 |

---

## 七、仍未验证（未变）

1. 真实 H3 视频出片（需 ≥24GB 显存）。
2. 真导入 `pack_*` 的运行时字段比对（需在 ComfyUI 运行时内执行）。
3. 网页面板在浏览器里的最终渲染。
4. 参考音色链路与导演台高级模式（二采/Guide/SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出。_
