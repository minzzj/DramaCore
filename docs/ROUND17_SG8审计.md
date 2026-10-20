# 第十七轮：SG8 异常与恢复审计 + 又一处「未接线」

> 目标：「不断重新阅读文档、不断完成我的愿景」。
> 本轮审 **SG8「异常与中断出口」** 整张子图，并顺带查了审核落盘。
> **未改产品代码。**

---

## 一、SG8 逐项审计

图一 SG8 原文：

```
ERR["统一异常出口"] --> ERR1["生成失败模型报错显存不足磁盘满"]
ERR --> ERR2["用户主动取消中止"]
ERR --> ERR3["断点续传"]
ERR --> ERR4["版本回滚"]
ERR1 -.->|"局部失败只重跑该段"| U
ERR1 -.->|"中程失败仅重跑依赖该素材且未通过的段已通过段用缓存"| GEN1
ERR1 -.->|"全局失败整条链重来"| A
ERR2 -.->|"保存进度"| RESUME
ERR3 -.->|"恢复"| RESUME
ERR4 -.->|"回滚素材"| G
```

| 项 | 判定 | 依据 |
|---|---|---|
| **ERR2 用户取消/中止** | ✅ **已实现** | `abort_id` / `ABORT_SIGNAL` 遍布 `review/state.py`(19)、`asset_review.py`(15)、`commit.py`(9)、`arbiter.py`(6)、执行器 `base/mock/comfyui_http`；「即使底层不可取消，其迟到结果也不得提交」有对应实现 |
| **ERR3 断点续传** | ✅ **已实现** | `store/checkpoints.py` 的 `CheckpointStore` 提供 `save / latest / list_checkpoints / resolve_resume_target`；验收 12（6 条）覆盖 |
| **ERR4 版本回滚** | ✅ **隐式支持** | 实测：加 v2 之后 **v1 的 LOCK 仍然有效** → 回退=继续引用 v1；撤坏版本可用 `clear_lock(asset_id, version, reason=...)` |
| **ERR1 局部失败**（只重跑该段） | ✅ **已实现** | `video_review.regenerate_granularity=segment` + CNT2（验收 10） |
| **ERR1 全局失败**（整条链重来） | ✅ 可行 | 重跑即可；检查点可回退 |
| **ERR1 中程失败**（仅重跑依赖该素材且未通过的段） | ❌ **算不出来** | 见下节 |

---

## 二、新发现：`references` 永远为空 → 中程失败无法计算

### 代码事实

```python
# bettercomfyui/store/assets.py
L389:  def references_of(self, asset_id: str) -> list[dict]
L1001: def record_reference(self, asset_id: str, version: Any, ref: dict) -> None
```

**全工程（`bettercomfyui/` + `bettercomfyui_comfy/`）对这两个函数的调用数：0。**
只有定义，没有任何写入者与读取者。

### 实测

跑一次完整 `run_demo`（ok=True，素材新建 3 个）后：

```
素材条目 3 个，其中 references 为空 3 个
```

### 影响（三条，都指向愿景）

1. **图七 `GF` 素材条目契约里的「引用次数」字段永远是空的。**
   原文：`GF["类型:string 序号:int 名称:string 路径:path 版本:int 审核状态:string
   评分:float 引用次数:int 锁定状态:bool"]`
   → 9 个字段里有 1 个**永远无值**。

2. **ERR1 中程失败无法实现。** 「仅重跑依赖该素材且未通过的段」需要
   「素材 → 引用它的段」这个**反向索引**，而索引源（`references`）是空的。

3. **图四 F2B 同样无法实现。** 原文：
   ```
   F2A["某角色参考图不合格"] --> F2B["仅重跑依赖该角色且未通过的段"]
   ```
   这是愿景里「爆炸半径」设计的核心 —— 只重跑受影响的部分，已通过的用缓存保留。
   **没有反向索引就只能整条重跑**，与愿景的省算力原则相冲突
   （愿景：算力成本和质量之间取中间值，只重新生成不合格的那一段）。

### 与既有能力的关系（不是从零开始）

`cascade.py` 已经实现了 **段 → 段** 的依赖边（`segment_id` = 受影响的下游段），
`Segment.dependency_refs` 也在用。缺的是 **素材 → 段** 这一维。
所以补起来不难：在段生成/审核提交时调一次 `record_reference`，
中程失败时用 `references_of` 反查即可。

---

## 三、顺带查清：审核事件**确实落盘**（无缺口）

我一度怀疑界面节点的审核不留痕迹，专门验了。结论是**我多虑了**：

| 路径 | LOCK | `review_history` | 事件 |
|---|---|---|---|
| 界面节点 `BetterComfyUIAssetReview`「人工通过」 | ✅ | **1 条** | **+2 条** |
| 库路径 `AssetReviewer`（`demo.py` 用，传 `project=paths`） | ✅ | **1 条** | **+10 条** |

两者的 `review_history` 都记了审核周期，事件都写进 `<项目>/logs/events.jsonl`。
界面节点的事件少（2 vs 10）是因为它直接 `set_lock`，没跑完整审核周期
（SCANNING → 打分 → commit），这符合它「人工一键通过」的语义。

**这条写下来的价值**：以后不用再查一遍。

### 我自己的错误（第二个，记下来）

第一版读 `review_history` 时我按 `assets/.../v1.json` 找，
而实际布局是 **`assets/<type>/<asset>/versions/<version>/meta.json`**。
于是**两边都读成 0 条**，差点写出「两条路径都不写审核记录」的错误结论。
修了读取路径后才是真结果。

> 教训与之前几次一致：**先把数据布局确认清楚，再下结论**。

---

## 四、验证汇总

| 项 | 结果 |
|---|---|
| SG8 六项 | 5 项已实现/可行；**1 项（中程失败）算不出来** |
| 审核落盘 | **无缺口**（两条路径都留记录与事件） |
| 新增缺口登记 | `references` 未接线 → 覆盖盘点 **TODO 3** |
| 新增脚本 | `scripts/audit_ui_review_persistence.py` |
| 全量测试 | **EXIT=0** |
| 产品代码改动 | **无** |
| 机器稳定性 | 未再蓝屏 |

---

## 五、累计缺口清单（等你点头再动）

| # | 缺口 | 来源 | 影响 |
|---|---|---|---|
| 1 | 剧本形状接缝（顶层 vs 章节级、`asset_type` vs `type`） | ROUND16 | **假成功**：`ok=True` 却零素材 |
| 2 | 界面路径没有结构校验（`validate_script` 零调用） | ROUND16 | 无效引用/重复 ID 不被拦 |
| 3 | **`references` 未接线**（素材→段反向索引） | **本轮** | 中程失败与图四 F2B 算不出来 |

建议优先修 #1（影响最大且最隐蔽），#3 次之（补起来不难），#2 再次。
