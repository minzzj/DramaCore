# ROUND36 目标机实机验证报告（RTX 4090 48G）

> 时间：2026-10-18 08:56–10:20（Asia/Shanghai）
> 目标机：`ssh -p PORT user@your-gpu-host`（外网 ComfyUI：`http://your-gpu-host:8188`）
> 结论先说：**真实 H3 成片/剪辑/质检链路已在 4090 上跑通；发现并当场修复 3 个真实问题；
> 实测显存峰值 45.58GB → 32GB 生产机（5090）必须分阶段，否则必 OOM。**

---

## 一、机器与环境（实测）

| 项 | 实测值 |
|---|---|
| GPU | NVIDIA GeForce RTX 4090，**49140 MiB（≈48G）**，driver 550.107.02 |
| 内存 | 容器可见 ≈1.0TiB（宿主共享），96G 规格满足 |
| 磁盘 | `/` overlay 40G、**`/data` 500G 空**、`/model` 7T（剩 1.7T） |
| Python | `/opt/conda/bin/python` 3.11.11；ComfyUI 自带 `.venv` 为 Python 3.10.12 |
| PyTorch | **2.6.0+cu124**，`torch.cuda.is_available()=True`，capability **(8,9)**，显存 47.5GB |
| ComfyUI | **0.33.0**（原生产镜像，位于 `/model/ComfyUI`） |
| 预置模型 | H3 ref2va/fl2va int8、pruned 系列、视频/音频 VAE、qwen3vl_32b **int8**（27.1G）与 nvfp4（15.7G）、Turbo LoRA ×5、SDXL（RealVisXL/animagine）、Flux 等 |
| 预置节点 | H3 Turbo/Hybrid/Context/Motion、KJNodes、VideoHelperSuite 等（总节点类型 1545） |

## 二、部署（只传源码，其余远端下载/安装）

按用户要求「能从网上下载的都让远端下，只上传源码」：
- 本地上传：**源码部署包 8.04MB**（`dist/bettercomfyui_bundle_*.zip`：插件/编排/工作流/测试/文档）；
- 远端自行安装：`apt ffmpeg`、`apt python3 python3-dev`、`pip imageio-ffmpeg safetensors`；
- 部署位置：`/data/BetterComfyUI`；插件+编排包复制到 `/model/ComfyUI/custom_nodes`；
  启动加 `BETTERCOMFYUI_HOME=/data/BetterComfyUI`（修复旧版在 ComfyUI 内误判 custom_nodes 的问题）。
- **结果：我们的 24 个节点全部注册成功**（`/object_info` 自建节点数 = 24），面板路由已注册。

### 环境问题（已当场解决）
| 问题 | 现象 | 处理 |
|---|---|---|
| `.venv` 损坏 | `.venv/bin/python3 -> /usr/bin/python3` 不存在 | `apt install python3`（3.10.12）恢复，venv 复活 |
| Triton 编译失败 | 缺 `/usr/include/python3.10/Python.h` | `apt install python3.10-dev build-essential` |
| 无 ffmpeg | `which ffmpeg` 空 | `apt install ffmpeg`（4.4.2，含 libx264/aac） |
| 缺 Python 包 | `imageio_ffmpeg`/`safetensors` 缺失 | `pip install imageio-ffmpeg safetensors`（远端下载） |

## 三、实机验证结果（全部真跑）

### 3.1 现有真实 H3 视频：质检
机器上 `/model/h3-persistent/output` 有 **1433 个 mp4（2.9GB）** 生产产物。
- 抽检最大视频（WanAnimate 运动迁移 480x832、24.43s）：
  完整性/剪辑性/音画/跳变正常；音画同步 99.86；**叙事流畅 50→（修复后）100**；
  节奏合理 58.77（低于默认阈值 70）→ 按规则判"不通过"（如实，非造假）。
- **发现真实缺陷 #1：叙事流畅公式对单镜头成片天然误判**（采样 12/12 个真实 H3 片段恒为 50）。
  → 当场修复：片段级（≤90s）0~30 切/分钟视为正常，仅机械式快切扣分；整片（>90s）保留理想区间。
  修复后同一视频 叙事流畅 = 100。

### 3.2 真实 H3 片段 → 我们的「成片剪辑」节点（拼接+封装+自检）
取 3 个真实 E05 生产 take（各约 8.5s，1376x768@24fps）：
```
输入 3 段 → 成片 25.47s（Σ段 25.43s，容差内）→ 完整性 passed=True
剪辑性：场景切换 2（≈3 段-1，证明是拼接剪辑过的）｜黑帧 0｜冻帧 1 段｜静音 0
质量（启发式）：叙事 100 / 音画 99.35 / 节奏 73.60 / 无跳变 83.33
占位检测：未发现 → 质量审核 CLI 结论：通过
```
证据：`remote_reports/finalize_result.json`、`remote_reports/remote_stitched_quality.md`。

### 3.3 真实 H3 生成（用镜像自带生产工作流 `minimax_h3_t2v_turbo.json`，官方前端提交）
| 指标 | 冷启动（首次，含加载） | 热启动（模型已缓存，随机种子强制重算） |
|---|---|---|
| 输出 | 864×480、5.17s、24fps、含音频 | 同规格、新文件 |
| 生成耗时 | **112.83 s** | **82.45 s** |
| 显存峰值 | **40.93 GB** | **45.58 GB**（4090 48G 几乎吃满） |
| 状态 | success | success |
| 成片质检 | — | **通过**（叙事 100 / 音画 100 / 节奏 72.96 / 无跳变 100） |

> **关键结论（实测，非理论）**：H3 全链在 4090 上峰值 **45.58GB > 32GB** →
> **5090-32G 生产机绝不能全模型同时驻留**；必须分阶段（文本编码器 → 卸载 → UNET+VAE），
> 与本地理论方案（峰值 24.94GB）一致。

### 3.4 视频审核节点（真实生成片段）
- 旧实现：5.17s 片段按默认「每段 5s」切出 0.17s 尾段 → 抽帧失败 → **整次审核中断**。
- **发现真实缺陷 #2 → 当场修复**：尾段 <0.5s 记录 `too_short=True` 并继续（结论"需返工"，提示调整每段时长），不再中断。
- 修复后复验：`segments=2`（第 2 段 too_short），`R_full executed=True`，
  R1b = 叙事 100 / 音画 94.82 / 节奏 83.13 / 无跳变 100，passed=True，policy=proceed。
- 本地已加回归测试（验收 32 P11），本地验收 32 = 11 条全过。

### 3.5 测试机发现的第 3 个问题（工具层，非产品代码）
镜像自带生产工作流的自定义节点字段（如 `MiniMaxH3TurboLoRA.low_vram`、`SaveVideo.format`）
与我们的通用 JSON→API 转换器顺序不一致 → API 提交 400。
解决：改用 **ComfyUI 官方前端提交流程**（浏览器加载工作流 → graphToPrompt → /prompt），
提交成功；该转换差异不影响产品代码，已记录为“生产工作流适配指南”。

## 四、结论

1. **目标机实机验证通过**：插件 24 节点可用；真实 H3 成片的「剪辑/完整性/质量」检查跑通；
   真实生成（冷 112.83s / 热 82.45s）与显存峰值（40.93 / 45.58GB）已实测。
2. **测试机问题已当场修复并回归**：叙事流畅误判（12/12 真实片段）、视频审核尾段中断；
   另有环境问题（venv/ffmpeg/头文件）用远端下载安装解决。
3. **32GB 生产结论（实测支撑）**：4090 峰值 45.58GB → 5090-32G 必须分阶段；
   生产建议 720p + tiled VAE + batch=1 + `--reserve-vram 1.5`，并用 `scripts/staged_run.py`
   分两段（素材 → /free → 出片），全程 `nvidia-smi` 峰值 <30.5GB。
4. **尚未完成（需 5090 生产机）**：5090 实测效率/峰值；双卡实测对比表 5090 列；
   我方导演台节点（MiniMaxH3Director 等）在该镜像上的适配；素材生成（S3）用远端 SDXL 的实测。

## 五、证据文件

本地：`[USER-HOME]\AppData\Local\Temp\bcu_remote_evidence\`
（vram_gen.csv / vram_warm.csv / MiniMax_H3_00002_.mp4 / MiniMax_H3_00003_.mp4 /
remote_stitched_quality.md / video_review_generated.json / generated_warm_quality.json 等）
远端：`/data/BetterComfyUI/remote_reports/`。


---

## 六、补充：第 4 个当场修复的问题（全量回归发现）

本地全量 pytest 暴露了审核语义漏洞：**无音轨成片（音画同步=0）会被其它三维平均后判整片通过**
（100+0+97.28+100 / 4 = 74.32 > 阈值 70）。
修复：`pipeline.full_review` 增加硬约束——`音画同步 <= 0`（无音轨/无法测量）→ 整片**不通过**，
failure_reason 明确写"音画同步为 0…不允许平均抵消"。
回归：验收 32 增 P12（无音轨硬约束）；审计 R2 的原失败用例恢复通过；
**验收 32 = 12 条全过；全量 pytest EXIT=0**。

## 七、最终复跑（本地，2026-10-18 09:40）

| 检查 | 结果 |
|---|---|
| 系统验收 `pytest tests/verify` | 通过 298 / 失败 0 / 未验证 1（EXIT=0） |
| 全量 `pytest tests/` | EXIT=0 |
| venv 验收 | EXIT=0 |
| 远端插件 | 24 节点注册、面板路由注册 |
| 远端 ComfyUI | 8188 运行（外网 8188），可继续用 |


---

## 八、宣传片（全中文/1080p，用户要求）与 S3 素材链路真机结果

- **宣传片高清中文版**：`docs/宣传片_剧核_DramaCore_高清中文版.mp4`
  （1920×1080 / 24fps / 25.67s / 15.83MB）：5 个镜头由远端 4090 H3 Turbo 真实生成（全中文提示词，
  每段 864×480 原生、80.69~83.45s/段），再用远端 `H3_04_Safe_Video_Upscale`（RealESGRAN x2）超分到
  1728×960 后 lanczos 到 1080p（5/5 成功），最后由本项目「成片剪辑」节点拼接+自检（passed=True）。
  质检：叙事100/音画100/无跳变100，仅「节奏合理 65.57<70」偏低（多镜头硬切口径，已如实记录，未调阈值修饰）。
- **S3 素材链路真机**：SDXL 生成 12.36s → 入库 v1 + sha256 + PENDING_REVIEW → 本地启发式审核 60.2<70
  → BLOCKED、CNT1=1、不写 LOCK（符合 R3）。已修复「审核先于入库」的依赖边缺陷。


---

## 九、整片预览修复 + 高分辨率效率更正（ROUND39）

- **Bug 修复（用户反馈：ComfyUI 网页端看不到“一整个成片”）**：成片剪辑节点现在会
  ① 把最终成片复制到 ComfyUI `output/剧核成片/`；
  ② 返回 `ui.images + animated + gifs(format=video/h264-mp4)` 三种预览输出；
  ③ 本地真机队列验证 PASS（导出文件存在、ui 三键齐全、history 可回放）。
  用户在工作流里跑完剪辑后，节点上/预览区可直接播放**整片**。
- **高分辨率效率更正**：480p 口径作废；720p 实测 232.03s/段、峰值 41.09GB；
  生产建议 480p+超分 1080p（120~125s/段）。详见 `docs/ROUND39_高分辨率效率修正报告.md`。
