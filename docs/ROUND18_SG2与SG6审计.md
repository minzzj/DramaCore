# 第十八轮：SG2 素材库结构 + SG6 成片输出审计

> 目标：「不断重新阅读文档、不断完成我的愿景」。
> 本轮审 **SG2**（素材库：G1–G5）与 **SG6**（成片输出：OUT1–OUT4）。
> **未改产品代码**，只取证。

---

## 一、SG2 逐项判定

图一 SG2 原文：

```
G1["目录结构角色场景物品分镜四类分目录"]
G2["命名规则类型_序号_名称"]
G3["格式与尺寸默认统一分辨率可选自动缩放"]
G4["数量上限每类默认最多9张可调"]
G5["素材优先级角色>场景>物品>分镜"]
G6["手动添加素材入口"] -->|"默认不审核直接入库"| G
```

| 项 | 判定 | 依据 |
|---|---|---|
| **G1 四类分目录** | ✅ **已实现** | 实测四类各建一个，磁盘上得到 `assets/character/`、`assets/scene/`、`assets/prop/`、`assets/shot/` |
| **G2 命名规则** | ⚠️ **部分** | ID 形如 `SG2审计_character_1_d5e13773` —— 含类型 ✅、含序号 ✅、**不含名称** ❌（名称是独立字段 `name='李明'`） |
| **G3 统一分辨率 / 自动缩放** | ❌ **未执行** | 全工程 `assets.resolution` 读取点为 **0**（只有类定义与配置装配）；没有任何 resize/scale 代码 |
| **G4 每类上限 9** | ❌ **未执行** | 实测：`per_class_limit=9`，连续造出 **12 个** CHARACTER 素材，**无一被拦** |
| **G5 素材优先级** | ✅ **已实现** | 融合·一体化导演台按 角色>场景>物品>分镜 排槽位（有 10 条守卫测试） |
| **G6 手动添加不审核** | ✅ **已实现** | `BetterComfyUIManualAsset` 的「新增素材(不审核)」→ `MANUAL_UNREVIEWED`，且不可用于生成 |

### 缺口细节

**G4（每类上限不执行）** —— 静态扫描显示 `per_class_limit` 只出现在两处：

* `config.py:60` —— 配置边界 `("assets", "per_class_limit"): (1, 64)`
* `entities.py:156` —— 字段默认值 `per_class_limit: int = 9`

**没有任何代码读它做判断。** 实测证据：

```
配置值 per_class_limit = 9
实际造出 CHARACTER 素材数：12（上限设定 9）
=> G4 是否执行：❌ 未拦截（配置值不生效）
```

**G3（分辨率/缩放不执行）** —— `AssetGenParams` 有 7 个字段
（`resolution/steps/sampler/cfg/seed/batch_count/per_class_limit`），
全工程对 `assets.resolution` 的读取为 **0**。

> 口径说明：`steps/sampler/cfg` 属于**采样参数**，已按设计 **DELEGATED** 给
> `MiniMaxH3Director`（导演台才是采样方），这不算缺口。
> 但 **`resolution` 与 `per_class_limit` 是素材库级约束**，
> 无论谁生成素材都该由素材库执行 —— 这两项才是真缺口。

### 为什么这两条重要

* **G4 不执行** → 素材库无边界膨胀。愿景反复强调省显存/省算力
  （「显存在任何情况下都要节省」），素材数量失控会让后续匹配、审核、
  注入清单全部变慢变大。
* **G3 不执行** → 素材尺寸参差。愿景要求「素材库是核心参考源」，
  参考图尺寸不统一会让导演台每段都要重新 fit（`_fit_image` 每次都在跑），
  既慢又可能引入画质损失。

---

## 二、SG6 逐项判定：**四项全部已实现**

图一 SG6 原文：

```
S_OUT --> OUT1["输出目录output日期剧本名"]
S_OUT --> OUT2["文件命名剧本名日期版本"]
S_OUT --> OUT3["元数据记录剧本模型参数时间"]
S_OUT --> OUT4["封面默认取第一帧可指定"]
```

| 项 | 判定 | 实现位置 |
|---|---|---|
| **OUT1 输出目录** | ✅ | `core.py:645` `out_dir = workdir / display / date_str / script_name` → `output/项目名/日期/剧本名` |
| **OUT2 文件命名** | ✅ | `core.py:649` `f"{display}_{script_name}_{date_str}_v{workflow_version}{ext}"` |
| **OUT3 元数据** | ✅ | `MovieMetadata`：`script_id` / `output_time` / `workflow_version` / `generation_provenance` / `segments` / 审核分数 / 降级标记 |
| **OUT4 封面** | ✅ | `core.py:696-702` `if cfg.encode.cover == "first_frame": cover_path = out_dir / f"{stem}.cover.jpg"` |

SG6 还**比要求更严**：`write_output` 会**拒绝**写出 `generation_provenance`
为空的元数据 —— 理由（代码注释原文）：

> 成片文件（mp4）在磁盘上**无法**区分「模型生成」与「占位媒体」，
> 来源若只打印在控制台，下次有人拿到 mp4 + metadata 就会误当真实产出。

这正是任务书第 9 节末句「必须明确区分真实集成与 mock 测试」的落地。

---

## 三、顺带核对：`OUT2` 与图一的差异（不是缺口）

图一写「文件命名**剧本名**日期版本」，实现是
「**项目名**\_剧本名\_日期\_v版本」—— 多了项目名前缀。

这是**改进而非缺口**：项目之间要隔离（任务书 §A），文件名带项目名
才能在 `output/` 下区分不同项目产出的同名剧本。

---

## 四、验证汇总

| 项 | 结果 |
|---|---|
| SG2 六项 | G1 ✅ / G2 ⚠️ / **G3 ❌** / **G4 ❌** / G5 ✅ / G6 ✅ |
| SG6 四项 | **全部 ✅**（且比要求更严） |
| 新增缺口登记 | G3 分辨率、G4 每类上限 → 覆盖盘点 **TODO 5** |
| 新增脚本 | `scripts/audit_sg2_asset_library.py` |
| 全量测试 | 见本轮结尾（未改产品代码，预期不变） |
| 产品代码改动 | **无** |
| 机器稳定性 | 未再蓝屏 |

---

## 五、累计缺口清单（等你点头再动）

| # | 缺口 | 来源 | 影响 |
|---|---|---|---|
| 1 | **剧本形状接缝**（顶层 vs 章节级、`asset_type` vs `type`） | ROUND16 | **假成功**：`ok=True` 却零素材 |
| 2 | **界面路径没有结构校验**（`validate_script` 零调用） | ROUND16 | 无效引用/重复 ID 不被拦 |
| 3 | **`references` 未接线**（素材→段反向索引） | ROUND17 | 中程失败与图四 F2B 算不出来 |
| 4 | **`per_class_limit` 不执行** | **本轮** | 素材库无边界膨胀 |
| 5 | **`assets.resolution` 不执行** | **本轮** | 素材尺寸参差，导演台每段重复 fit |

**观察**：这 5 条有共同形态 —— **配置/函数/字段都在，就是没接上执行点**。
这不是"没写"，而是"写了没接线"。若要一次修干净，
建议加一条**通用守卫**：扫描"定义了但产品代码零引用的配置项/公开函数"，
把结果纳入 CI —— 这类问题就能被自动发现，而不是靠一轮轮人肉审计。

---

## 六、追加：把审计变成永久守卫（棘轮）

前面 5 条缺口的形态完全一样 —— **配置/字段/函数都在，就是没接上执行点**。
人肉一轮轮审计太慢，修好一批还会再长出新的。所以本轮做了一条**棘轮**：

### 新增 `tests/test_config_wiring_ratchet.py`（7 条测试，全过）

* 扫描产品代码，找出「配置项存在但产品代码零引用」的字段；
* `KNOWN_UNWIRED` 登记**当前已知 17 条**，每条写明定性原因；
* 断言：**不允许新增**未接线项（棘轮只紧不松）；
* 反向守卫：已接线的条目必须从清单删掉（清单不许变成垃圾场）；
* 真缺口必须标 `**【真缺口】**`，不许淡化。

### 当前定性结果（17 条）

| 分组 | 条数 | 性质 |
|---|---|---|
| 设计上由别层承担 | 2 | `assets.sampler`（导演台采样）、`assets.batch_count` |
| 行为已硬编码、开关没接 | 6 | `on_timeout`、`retry_interval`、`new_version_requires_review`、`regenerate_granularity`、`checkpoint_every_stage`、`normalize_names` |
| **真缺口** | 7 | `per_class_limit`、`audio_join`、`save_vram_all`、`log_all_manual_and_auto_switch`、`task_timeout_s`、`config_import_export`、`comfyui_endpoint` |
| **覆盖盘点误标 DONE** | 2 | `score_dimensions`、`log_level` |

> 最后一行值得单独说：`gap_analysis.py` 把「评分维度」和「日志级别」标成了 **DONE**，
> 但产品代码**根本不读**这两个配置。**覆盖盘点本身也会乐观误标** —— 这条已修正认知。

### 判据的局限（如实记录）

两种自动判据都有偏差：

| 判据 | 偏差方向 |
|---|---|
| 裸字段名 | **假阳性**：跨组同名会误判已接线 |
| 完整路径 `.group.field` | 假阴性：中间变量读不到 |

所以用**裸词**（宁可多报），再人工定性。另有 1 条**已知漏报**：

* `assets.resolution` —— 确实未接线，但 `resolution` 与 `director.resolution` 重名，
  裸词命中率不为 0，扫描检测不到。
* 单列在 `KNOWN_UNDETECTABLE` 里，并注明漏报原因；
  另有一条**交叉校验**测试：若哪天它被扫描检测到，就必须移出该清单。

**不假装扫描全能** —— 漏报写清楚，比覆盖率高更重要。

### 这一步的价值

以后谁新加一个配置项却忘了读它，`test_no_new_unwired_config_fields` 会直接红。
**这类"配了不用"的债不会继续增长。**
