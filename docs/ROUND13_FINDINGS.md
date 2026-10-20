# 第十三轮进展记录（2026-10-17 凌晨）

> 目标轮次 11/60。本轮补上整晚最后一条功能性缺口：**导演台的四个高级接缝
> （二采 / SelfLift / SemanticBridge / FaceRefine）**。
> 过程中我犯了两个判断错误，都被自己的验证抓出来并修正。

---

## 一、四个高级接缝不止"存在"，是**真的能接上**

`MiniMaxH3Director` 有 4 个可选输入：
`refine` / `selflift` / `semantic_bridge` / `face_refine`，
各自由一个专门节点产出对应类型。

新增 `scripts/probe_advanced_seams.py`：用**真实的高级节点**产出对应类型，
接到 Director 上提交给 ComfyUI，看它是否接受。

```
[MiniMaxH3DirectorRefine]            有
[MiniMaxH3DirectorSelfLift]          有
[MiniMaxH3DirectorSemanticBridge]    有
[MiniMaxH3DirectorFaceRefine]        有

[refine]          图被接受（已入队）
[face_refine]     图被接受（已入队）
[semantic_bridge] 图被接受（已入队）
[selflift]        图被接受（已入队）
```

**4/4 接缝全部被 ComfyUI 接受** —— 这比读 schema 强一层：
schema 里有这个输入，只说明"声明了"；提交被接受才说明**类型真的兼容**。

> 探测过程中还顺手修正了 3 个我用错的控件取值
> （`canvas_mode` 要用 `auto_capped_768`、`select` 要用 `largest_face`、
> `detector` 要用真实的 `face_yolov8m.pt`）。
> 若不做这一步，这些取值错误会直接写进融合说明里误导用户。

---

## 二、能力检测：必须**按选项**判断，不能一刀切

任务书第 7 节原文：

> Agent 应检测能力并说明不支持项；不要硬编码为不可修改的规则。

新建 `check_advanced_modes()`：**真的去查权重目录**，而不是只看 schema。
理由是这些 COMBO 在缺权重时会退化：

* `latent_upscale_model: ['(将 3D 权重放入 models/latent_upscale_models)']` —— 明显是提示文字
* `detector: [['face_yolov8m.pt'], ...]` —— **显示了一个名字，但磁盘上并没有**

第二类特别危险：光看 schema 会以为"有这个名字就能用"。

### 实测报告（本机）

```
[高级模式能力检测] 导演台的 4 个高级接缝：
  [可用] 二采 / 放大（Refine）  (接缝 refine ← MiniMaxH3DirectorRefine)
           可用选项：refine、upscale（不需要额外权重）
           选项 latent_upscale 不可用：需要 3D latent 放大模型
             -> 把权重放进 models/latent_upscale_models/
           选项 upscale_method=nvidia_rtx_vsr 不可用：需要 RTX VSR 放大模型
             -> 把权重放进 models/upscale_models/
  [全部不可用] SelfLift（分段自提升）      -> models/latent_upscale_models/
  [全部不可用] SemanticBridge（语义桥接）  -> models/semantic_bridge/
  [全部不可用] FaceRefine（人脸细化）      -> models/ultralytics/bbox/

结论：1/4 个高级接缝有可用选项。**被标记为不可用的选项不要接**。
     这不影响主流程：三个导演台的基础出片（t2v/i2v/fl2v/r2v）不需要这些权重。
```

---

## 三、我犯的两个判断错误（都被自己的验证抓出）

### 错误 1：一刀切整个节点

第一版实现是"这个节点所需的权重目录为空 → 整个节点不可用"。
于是 `MiniMaxH3DirectorRefine` 被标成"全部不可用"。

**但这是错的**：它的 `refine` 与 `upscale` 两个模式
**不需要任何额外权重**，只有 `latent_upscale` 模式才需要 3D latent 放大模型。

后果：用户会放弃本来能用的二采功能。

**修法**：`ADVANCED_MODES` 改成
`free_options`（不需要权重）+ `gated_options`（每项 -> 所需目录与原因），
报告按**选项**给出可用性。

守卫：`test_refine_free_options_available_without_weights`
（在"什么权重都没有"的假目录下断言 `refine`/`upscale` 仍可用）。

### 错误 2：把权重目录查错了

人脸检测权重我写的是 `models/detection/`，而节点 tooltip 原文是：

> 人脸检测权重，放到 **models/ultralytics/bbox/**（如 face_yolov8m.pt）

`models/detection/` 里只有一个 `put_detection_models_here` 占位文件，
于是判定"不可用"。**查错目录会得出相反的结论。**

守卫：`test_face_refine_checks_ultralytics_bbox` —— 显式断言查的是
`ultralytics/bbox` 且**不是** `detection`。

> 这两个错误的共同点：**把"看起来差不多"当成"就是这样"**。
> 前者是"节点级 vs 选项级"的粒度搞错，后者是目录名想当然。
> 都靠"去查真实数据"而不是"照猜"才纠正过来。

---

## 四、新增测试（10 条，`tests/test_advanced_seams.py`）

| 测试 | 覆盖 |
|---|---|
| `test_refine_free_options_available_without_weights` | **错误 1 的守卫** |
| `test_refine_latent_upscale_becomes_available_when_weight_present` | 反向：有权重时可用 |
| `test_face_refine_checks_ultralytics_bbox` | **错误 2 的守卫** |
| `test_face_refine_unavailable_without_weight` | 缺权重时如实报不可用 |
| `test_placeholder_files_do_not_count_as_weights` | `put_*` 不算权重 |
| `test_non_weight_extensions_do_not_count` | `.yaml`/`.txt` 不算权重 |
| `test_missing_comfy_base_reports_all_gated_unavailable` | 未配置路径时**不许猜成可用** |
| `test_all_four_seams_are_covered` | 四个接缝都在检测范围（漏一个 = 默默不检测） |
| `test_every_seam_names_a_real_node` | 每项都写明产出节点 |
| `test_report_mentions_correct_dirs` | 报告里的目录必须是**真正该放权重**的那个 |

---

## 五、验证汇总

| 项 | 结果 |
|---|---|
| 四个高级接缝被 ComfyUI 接受 | ✅ **4/4** |
| 能力检测按选项判断 | ✅ 1/4 接缝有可用选项（Refine 的 refine/upscale） |
| 新增测试 | 10 条 |
| 全量测试（系统 Python） | **EXIT=0** |
| 全量测试（ComfyUI venv） | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| 权重 | 59.38 GB / 6 文件，ComfyUI 全部识别 |

---

## 六、仍未验证

1. **真实 H3 视频出片** —— 已实测尝试并确证本机不可行：
   最小图也需 34.14 GB 权重同时驻留，本机物理内存 15.75 GB，
   进程进 CPU 回退路线时**完全死锁**（0 CPU / 0 磁盘 / 单线程）。
   换机需同时满足 ≥24 GB 显存 **与** ≥64 GB 内存。
2. 浏览器**渲染级**（DOM 级已过，本机无 headless 浏览器）。
3. 四个高级接缝的**实际执行效果** —— 接缝类型已验证可接，
   但 `latent_upscale`/`selflift`/`semantic_bridge`/`face_refine`
   在本机都缺权重，无法跑出结果。

---

_本轮所有 ✅ 均来自实际执行的命令输出；两个判断错误是我自己的，如实记录。_
