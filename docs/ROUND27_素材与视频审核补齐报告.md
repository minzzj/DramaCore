# ROUND27 报告：补齐「素材生成 / 素材审核 / 视频审核」

> **用户要求（本轮原话）**：
> 「这个东西在逻辑图里面可不仅仅只是由视频生成的环节，还有图片的生成，就是那些素材的生成。
> 还有图片，就是素材的审核以及视频的审核……或者是你可以做两个单独节点用于审核。
> 总之，逻辑要清晰，要完整。如果现在已经有了的话，那么请教我怎么使用它？」
>
> 完成：2026-10-17 23:45。用法见 **`docs/使用指南_素材生成与审核.md`**。

---

## 一、先回答"现在有没有"

| 逻辑图环节 | 本轮之前 | 本轮之后 |
|---|---|---|
| **SG2 素材生成 GEN1–GEN4** | ❌ 界面没有入口（只有手动添加） | ✅ `BetterComfyUIAssetGen`（取任务）+ 任意图像工作流 + `BetterComfyUIAssetIngest`（入库） |
| **SG2 素材库 G/G7（只追加+指纹）** | 仅手动素材/演示脚本 | ✅ 入库节点：新版本 + SHA-256 + `PENDING_REVIEW`，绝不覆盖 |
| **SG3 素材审核 H/H1/H2/CNT1/LOCK/FALLBACK** | ⚠️ **简化版**：直接 `set_lock`，绕过审核状态机（无 CNT1/无超时/无仲裁/FALLBACK） | ✅ 升级为走 `review.AssetReviewer`：人工/模型、CNT1、超时切模型、唯一提交、版本级 LOCK、FALLBACK 来源 |
| **SG5 视频段审核 R_seg/CNT2/CNT3/N1_EDGE** | ⚠️ 藏在 `FusionIngest` 里，且评分是**常量 80 分（假通过）** | ✅ 评分改为真实帧统计（`local_scorer.frame_scores`）；另可在 `BetterComfyUIVideoReview` 里按视频文件做段级审核 |
| **SG6 整片审核 R_full/R1b/FULL_POLICY** | ❌ 界面没有（库里有，但要求注入评分器） | ✅ `BetterComfyUIVideoReview`：R_full 只执行一次 + 阻止/降级显式策略 |
| 拼接/封装 SPLICE/ENC | ⚠️ 库里有（`pipeline.splice/encode`），`BetterComfyUIRun`/CLI 走 demo 路径；界面无独立节点 | 本轮未新增（用户未点名）；列为下一步 |

**一句话**：素材生成、素材审核、视频段审核、整片审核现在都有清晰的界面入口了；
其中「素材审核」和「视频审核」就是你说的**两个单独节点**。

## 二、新增/升级了什么（文件级）

### 新增
| 文件 | 作用 |
|---|---|
| `bettercomfyui/review/local_scorer.py` | **本地评分器**（真实像素/帧统计，非常量）：图像 `画质/构图`；视频帧 R1a 四维；整片 R1b 四维；抽帧/切段工具。语义维度（一致性/合规）**如实 skipped**，不编分 |
| `bettercomfyui_comfy/review_nodes.py` | 3 个界面节点：`BetterComfyUIAssetGen`（素材生成·取任务）、`BetterComfyUIAssetIngest`（素材入库）、`BetterComfyUIVideoReview`（视频审核 R_seg+边界+R_full） |
| `scripts/build_review_workflows.py` | 构建 S3/S4 两套工作流（真实 `/object_info` 校验后落盘） |
| `tests/verify/test_acceptance_32_review_pipeline.py` | 验收 32（9 条）：评分真实性、生成/入库/审核/LOCK、视频审核段级+整片+策略、假评分回归 |
| `docs/使用指南_素材生成与审核.md` | **手把手使用说明**（含参数、注意项、GPS 红线） |

### 修改
| 文件 | 改动 |
|---|---|
| `bettercomfyui_comfy/nodes.py` | `BetterComfyUIAssetReview` 重写为完整审核状态机（走库）；`_orch_repo_root()` 修复（详见三·2） |
| `bettercomfyui_comfy/fusion_nodes.py` | **修掉常量 80 分假评分**：回灌段级审核改用真实帧统计；评不出来就明确失败（绝不假装通过） |
| `bettercomfyui_comfy/review_nodes.py` | 视频审核的「整片阈值覆盖」真正写进 R_full 配置（否则覆盖是摆设） |
| `bettercomfyui_comfy/__init__.py` | 注册 review 节点（节点数 20 → **23**） |
| `tests/test_config_wiring_ratchet.py` | `assets.sampler` / `assets.batch_count` 已接线，从 KNOWN_UNWIRED 移除（棘轮反向守卫） |
| `scripts/verify_install.py` | 期望节点 23、工作流加 S3/S4 |
| `tests/verify/_harness.py` | 新增验收 32 |

### 工作流（源码 + 界面目录双份，真实 schema 校验通过）
| 工作流 | 节点 | 说明 |
|---|---|---|
| `S3_素材生成入库审核（GPU）` | 12 | 取任务 → Checkpoint/CLIP/KSampler（**换成你的图像模型**）→ 入库 → 素材审核 → 3 预览 |
| `S4_视频审核（本机可跑）` | 5 | 视频路径 → 段级 R_seg + 边界 + 整片 R_full → 4 预览；**本机有 ffmpeg 即可跑** |

## 三、顺带修掉的 4 个真 Bug

1. **视频段审核是假通过**：`FusionIngest._review_and_count` 里 `{d: 80.0 for d in dims}` 是常量分 ——
   任何段都恒过 70 阈值。已改为 `local_scorer.frame_scores(真实帧)`；拿不到帧就明确失败。
2. **ComfyUI 内仓库根解析错**：`_orch_repo_root()` 在插件安装形态下返回
   `<repo>/.comfyui/custom_nodes`，导致节点在 ComfyUI 里把项目根当成了
   `custom_nodes/projects`（这就是之前那个杂散目录 + `演示项目` 副本的由来，S4 首次 E2E 直接报"项目不存在"）。
   已改为强标记向上查找并显式拒绝 `custom_nodes`；杂散目录已**非破坏性隔离**到
   `_stray_custom_nodes_projects/`（数据保留）。
3. **整片阈值覆盖失效**：视频审核节点把覆盖值只用于显示，没写进 R_full 的配置 →
   实测「阈值 100 + 阻止输出」仍然通过。已修复并纳入验收 32 P8。
4. **契约/棘轮红灯**：`review/local_scorer.py` 反向 import `pipeline`（违反 C5 依赖方向），
   改为自带 ffmpeg 定位；`assets.sampler/batch_count` 已由新节点消费，棘轮清单同步移除。
   现在 `check_contracts.py`：**BLOCKER = 0**。

## 四、证据（全部实测）

```
系统 python -B -m pytest tests/verify -q → EXIT=0，通过=272 失败=0 未验证=1
.comfyenv  -B -m pytest tests/verify -q → EXIT=0，通过=261 失败=0 未验证=3
全量 pytest tests/                       → EXIT=0
契约检查 check_contracts.py              → BLOCKER=0（C5 修复）
verify_install.py                        → 23 节点可见（总节点 1035）/ 工作流在 / 巨人包在 / 哈希一致

节点级冒烟（临时项目，结束自动清理）：23/23 断言 PASS
  - 清晰图 vs 平坦图评分显著不同；图像/帧/整片评分均非恒定
  - 素材生成取到待生成素材 + 真实生成参数（分辨率/步数/采样器/CFG/种子/批量）
  - 入库 v1/v2 只追加 + sha256 + PENDING_REVIEW
  - 模型审核 → ALLOWED + MODEL_PASS LOCK + CNT1；人工通过 → HUMAN_PASS
  - 视频审核：2 段 R_seg + R_full 执行一次；阈值100+阻止输出 → 明确 block；CNT2 累加
  - 回灌节点不再有常量 80 分假评分

真 ComfyUI 端到端（S4，临时项目 + 合成视频）：
  提交 5 节点 → status=success
  4 个预览可见：结论 / 段审核JSON（R1a 四维）/ 整片审核JSON（R1b）/ 审计（含 R_seg、R_full）
  counters.json 与 review/video_reviews.json 落盘 → 7/7 断言 PASS

真浏览器（系统 Edge）：S3、S4、W0、W2、P1、W0L 六套全部打开 —
  节点错误无、未连线输入无、pageerror 无、我方 console.error 无
```

## 五、诚实边界（不粉饰）

1. **素材生成需要你的图像模型**：本机没有图像权重，S3 只验证了结构/连线/浏览器打开，
   **没有产出真实素材图**；到 GPU 机器在图里选自己的 Checkpoint 即可跑。
2. **本地评分是启发式，不是语义模型**：
   - 图像只算「画质/构图」，「一致性/合规」明确 skipped；
   - 视频四维来自真实帧统计（连贯/闪烁/直方图相似/帧差离散度），能抓机械问题，
     判断不了"剧情是否合理"。
   - 要语义审核请替换评分模型（配置 `asset_review` / `video_review` 的阈值仍在用；
     评分器接口后续可注入，本轮不假装已有）。
3. **视频重生成仍需 GPU**：视频审核节点只做判定 + CNT2/CNT3 记账 + 输出"需重生成段"，
   不假装重生成。
4. **拼接/封装界面节点**：库里已有 `pipeline.splice/encode`（且 `BetterComfyUIRun`/CLI 在跑），
   但界面暂未单独暴露；这是下一步，不是"已完成"。
