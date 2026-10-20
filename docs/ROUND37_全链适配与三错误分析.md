# ROUND37 全链适配与三错误分析（8188 远端 4090 · ComfyUI 0.33.0）

> 时间：2026-10-18 09:45–11:05（Asia/Shanghai）｜执行：fix-agent（只读排查 + 本地准备，**未**在远端重启/安装/提交队列）
> 目标机：`ssh -p PORT user@your-gpu-host`（外网 ComfyUI `http://your-gpu-host:8188`，内网 8188）
> 交付：适配工作流 1 份、巨人安装脚本 1 份、三错误根因 + 模型映射 + 风险/回滚/测试清单（本文）

---

## 0. 结论摘要

| # | 用户说法 | 实测根因 | 证据 | 修复 |
|---|---|---|---|---|
| ① | 一个节点错误 | 工作流里的 **`MiniMaxH3Director`（导演台巨人节点，id=18）远端未安装**，前端 graphToPrompt 得到「无 class_type」的节点，服务端直接判 `missing_node_type` | 远端日志 `/tmp/bcu_comfyui2.log`：`invalid prompt: {'type': 'missing_node_type', ... "Node 'ID #18' has no class_type..."}`；远端 `/object_info` 1545 类节点里 6 个巨人包 **全部 MISSING** | `scripts/远程安装巨人.sh`（装 6 个巨人包 + 重启 + 校验 + 失败回滚） |
| ② | 两个演示/输入错误 | **两处独立**：<br>(a) **widget 值整体错位 1 位**——远端前端 1.48.7 会多存一个 `control_after_generate` widget，本地 0.41 已不存，于是 `KSampler` 的 `cfg/sampler/scheduler` 全部错位；<br>(b) **输入值非法/为空**——`CheckpointLoaderSimple.ckpt_name=""`（Value not in list）、`CreateVideo.bit_depth="auto"`（应为 INT）、演示节点「成片剪辑/视频审核」路径为空 | 远端日志实证：`KSampler 6: Failed to convert an input value to a FLOAT value: cfg, euler` / `Value not in list: sampler_name: 'simple'` / `Value not in list: scheduler: 1`；而 `S3_素材生成入库审核（GPU）.json` 的 KSampler **id=6** 且值恰为 `[0,20,8.0,"euler","simple",1.0]`（与日志完全对应） | 适配工作流按远端 schema **重排 widget 数组**（补 `control_after_generate`、类型/下拉值非法回落默认）；`ckpt→RealVisXL`；成片剪辑开「自动发现最新视频」 |
| ③ | 一个模型缺失 | **LoRA 文件名远端不存在**（本地 `…lightx2v_turbo_4step_v0.1_comfy_resized_avg_rank_21_bf16` 远端没有）；另 `CheckpointLoaderSimple` 为空、CLIP 用的是 **NVFP4**（4090 消费卡无 NVFP4 硬件）；UNET/两个 VAE 名字其实能对上 | 远端 `/object_info` 各 loader 下拉清单逐一核对（见 §2③） | 适配工作流全部换成远端实测文件名（LoRA→`v1.1_768p_comfyui_bf16`、CLIP→`int8_convrot`、ckpt→`RealVisXL_V5.0_fp16`） |

**一句话**：用户看到的三个错误 =「缺 1 个巨人节点」+「前端版本差异导致控件值错位/非法」+「模型文件名不一致」。
三件事**互相独立**，全部修好才能跑通；修复入口 = **安装脚本（一次）** + **更换为适配版工作流（每次导入）**。

---

## 1. 远端实测环境（只读核对）

| 项 | 实测值 | 来源 |
|---|---|---|
| ComfyUI | **0.33.0**，`/model/ComfyUI`，进程 PID 4683（PPID=1，**无 supervisor 守护**） | `/object_info`、`/model/ComfyUI/comfyui_version.py`、`ps -ef` |
| 前端 | **1.48.7** | `/model/ComfyUI/user/default/comfy.settings.json` → `Comfy.InstalledVersion` |
| 启动命令 | `.venv/bin/python main.py --listen 0.0.0.0 --port 8188 --disable-auto-launch --database-url sqlite:////tmp/comfyui.db`，cwd=`/model/ComfyUI`，env `BETTERCOMFYUI_HOME=/data/BetterComfyUI` | `/proc/4683/cmdline`、`/proc/4683/environ` |
| 节点类型总数 | **1545**；其中我们的 `BetterComfyUI*` **24 个全部已注册**（与本地仓库代码同版本，关键文件字节数一致） | `/object_info` |
| 巨人节点 | **6 个包全部未安装**：`MiniMaxH3Director / MiniMaxH3SmartDirector / MiniMaxH3ChainDirector / DNNodes / StoryboardNode / DeepSeekPromptConnector` 均 MISSING | `/object_info` |
| 解压工具 | **没有 `unzip`/`zip`/`rsync`**；有 `python3 3.10.12`（zipfile 可用）、`tar`、`ffmpeg` | `command -v` |
| 巨人包依赖 | **已装齐**：opencv-python-headless your-gpu-host / scenedetect 0.7.1 / ultralytics 8.4.138 / requests / imageio-ffmpeg / torch 2.6.0+cu124 | ComfyUI venv `pip list` |
| 磁盘 | `/` 40G（用 2%）、`/data` 500G 空、`/model` 7T（剩 1.6T） | `df -h` |
| 历史队列 | `/history` 最近 9 条全部 success（失败的 prompt 被服务端拒绝，**不进 history、只在日志**） | `/history`、`/queue`（空） |

### 巨人源码包（Lead 已上传）
`/root/giants_source.zip`：27,491,072 字节，md5 `77e7a8ccaee4a9aef403751b5ab3ccae`，427 个条目 / 解压后 30MB，6 个顶层目录。
⚠️ **该 zip 是 Windows 打的，条目分隔符是反斜杠**（如 `ComfyUI-DNNodes\__init__.py`），Linux 上 `unzip` 会解出一个带反斜杠的文件名；且远端没有 unzip。安装脚本已用 `python3 zipfile` 手动解压并统一 `\`→`/`。

---

## 2. 三错误逐条分析（含证据与修复方式）

### 错误① 节点错误 = `MiniMaxH3Director` 未注册（缺巨人包）

- **现象**：导入《00_【总流程】剧本→成片（GPU）》时会弹 **Missing Node Types**（红框节点「MiniMaxH3Director」）；点队列则被服务端拒绝。
- **实证**（`/tmp/bcu_comfyui2.log` 末尾）：
  ```
  invalid prompt: {'type': 'missing_node_type',
    'message': "Node 'ID #18' has no class_type. The workflow may be corrupted or a custom node is missing.",
    'details': "Node ID '#18'", 'extra_info': {'node_id': '18', 'class_type': None, 'node_title': None}}
  ```
  该工作流 28 个节点里 **id=18 正是 `MiniMaxH3Director`**（其余 27 个类型远端全部存在）。
  日志更早处还有同一根因的旧工作流记录（节点标题 `🎬 MiniMaxH3Director（导演台，真实出片）`，id #46/#7）。
- **根因**：远端装了我们 24 个 `BetterComfyUI*` 节点，但 **6 个巨人包没装**；导演台节点在巨人包里。
- **修复**：执行 `scripts/远程安装巨人.sh`（见 §4）。

### 错误② 两个「演示/输入」错误 —— 两种场景都覆盖

用户没给截图，所以按**加载期**与**点队列期**两种场景分别定位，两种都已修：

**②-a（若发生在点队列）widget 值错位 1 位（前端版本差异，实证）**

远端前端 1.48.7 会在带 `control_after_generate` 的 INT 控件（如 `KSampler.seed`、`MiniMaxH3Director.seed`）后面**多保存一个 control widget 值**（`"fixed"/"randomize"`）；本地 0.41 前端已不再保存。于是本地生成的 JSON 到远端加载后，**该节点之后的 widget 值整体前移 1 位**：

| 远端日志原文（`/tmp/bcu_comfyui2.log`） | 错位解释 |
|---|---|
| `Failed to convert an input value to a FLOAT value: cfg, euler, could not convert string to float: 'euler'` | 远端把第 4 个值 `"euler"` 当成 `cfg` |
| `Value not in list: sampler_name: 'simple' not in (list of length 44)` | 第 5 个值 `"simple"` 落到 `sampler_name` |
| `Value not in list: scheduler: 1 not in ['simple', …]` | 第 6 个值 `1.0` 落到 `scheduler` |

**这些日志正是 `KSampler 6` 报的**，而本项目 `bettercomfyui_comfy/workflows/S3_素材生成入库审核（GPU）.json` 里 KSampler **id=6**、值恰为 `[0, 20, 8.0, "euler", "simple", 1.0]` —— 说明用户还把 **S3 演示工作流**（或同类展示用工作流）放到了远端运行。
不修的话，**装了巨人之后《00 GPU 全流程》的 KSampler id=9 会报同样的错**。适配版已把每个节点的 widget 数组重排为远端格式：
`KSampler → [0, "fixed", 20, 8.0, "euler", "simple", 1.0]`（7 个值）。

**②-b（若发生在加载/首次运行）输入值非法或为空**

| 输入问题 | 加载/运行表现 | 修复（适配版已改） |
|---|---|---|
| `CheckpointLoaderSimple.ckpt_name = ""` | 前端 `Value not in list: ckpt_name: ''`（节点红框，无法排队） | 改为 `RealVisXL_V5.0_fp16.safetensors`（远端已有） |
| `CreateVideo` widget 串位：本地 4 个值 `[30.0,"auto","sRGB","none"]`、远端 schema 只有 `[fps,bit_depth]` → `bit_depth="auto"` | `Failed to convert an input value to a INT value: bit_depth, auto` | 重排为 `[24.0, 8]`（fps 实际由导演台连线提供） |
| `SaveVideo` 本地只有 1 个值、远端需要 3 个（`filename_prefix, format, codec`） | 缺 `format/codec` 会在 API 提交时报 `Required input is missing: format` | 补齐 `["BetterComfyUI/一体化", "auto", "auto"]` |
| 「成片剪辑」`段视频路径列表` 为空（演示节点必报） | 运行期明确报错：「段视频路径列表为空：每行填一个段视频绝对路径…」 | **开启「自动发现最新视频」+ 时间窗 720 分钟**（节点原生能力，审计会列出实际用到的文件） |
| 「视频审核」`视频文件路径` 为空 | 运行期报错要求填路径 | 该输入已由「成片剪辑.成片路径」自动连线喂入，无需手填 |

### 错误③ 模型缺失 = 模型文件名与远端不一致（Missing Models 弹窗）

远端下拉清单逐项核对结果：

| 节点 | 本地工作流原值 | 远端是否存在 | 适配后取值 | 说明 |
|---|---|---|---|---|
| UNETLoader | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | ✅ 存在（同名） | **保持** | 远端还有 `MiniMax_H3_Ref2VA_pruned_int8_convrot` 等 23 个可选 |
| CLIPLoader | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | ⚠️ 文件在，但 **4090 无 NVFP4 硬件** | **`qwen3vl_32b_minimax_h3_int8_convrot.safetensors`** | 改 int8，避免跑不动 |
| VAELoader（视频） | `minimax_h3_video_vae_fp16.safetensors` | ✅ 存在 | **保持** | |
| VAELoader（音频） | `minimax_h3_audio_vae_fp32.safetensors` | ✅ 存在 | **保持** | |
| LoraLoaderModelOnly | `minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy_resized_avg_rank_21_bf16.safetensors` | ❌ **不存在** | **`minimax_h3_fl2v_turbo_4step_v1.1_768p_comfyui_bf16.safetensors`** | 远端 Turbo LoRA 系列共 8 个，选 v1.1 768p |
| CheckpointLoaderSimple | `""`（空） | —（空值非法） | **`RealVisXL_V5.0_fp16.safetensors`** | 素材生成用的 SDXL 图像模型 |

> 备选（若 r2v 出片质量异常）：LoRA 可换 `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`（远端已有，语义更贴合 **ref2va UNET + r2v 模式**）。
> 调参提示：Turbo LoRA 是 4-step 蒸馏，导演台 `steps=25` 偏多，建议 4~8、`cfg=1.0`。

---

## 3. 节点缺失清单（远端 → 安装后应出现）

| 巨人包（要装） | 关键节点（安装后 /object_info 应出现） |
|---|---|
| `ComfyUI_MiniMaxH3_Director` | **MiniMaxH3Director**（本工作流用）、MiniMaxH3DirectorGroupsCombine、MiniMaxH3DirectorGroupReferenceToVideo、MiniMaxH3DirectorRefine/… |
| `ComfyUI_MiniMaxH3_Smart_Director` | MiniMaxH3SmartDirector、MiniMaxH3SmartDirectorAllInOne |
| `ComfyUI-MiniMaxH3-ChainDirector` | MiniMaxH3ChainDirector、MiniMaxH3ChainDirectorEN |
| `ComfyUI-DNNodes` | DNNodes、DNGroupDirector、DNMediaToDirectorGroup |
| `ComfyUI-Storyboard-LLM` | StoryboardNode（DeepSeek 拆剧本） |
| `ComfyUI-DeepSeek-API-connector` | DeepSeekPromptConnector |

本全流程文件实际只用到 **1 个巨人节点**：`MiniMaxH3Director`（id=18）。其余 5 包为「剧本拆解/智能导演/链式导演」等其它工作流所需，一次装全可避免以后逐个踩坑。

---

## 4. 巨人安装脚本（`scripts/远程安装巨人.sh`，**未执行**，需用户/Lead 手动在远端跑）

```bash
# 1) 上传（本地 → 远端）
python -B "[USER-HOME]\AppData\Local\Temp\bcu_remote.py" --put "scripts\远程安装巨人.sh" "/root/远程安装巨人.sh"
# 2) 建议先干跑（只解压/查依赖/查备份计划，不动 custom_nodes、不重启）
DRY_RUN=1 bash /root/远程安装巨人.sh
# 3) 正式安装（会重启 ComfyUI，约 3~5 分钟；请确认没有人在跑队列）
bash /root/远程安装巨人.sh
```

脚本内部步骤（每步都有真实结果打印，失败自动回滚）：
1. 前置检查（root / zip 存在 / `custom_nodes` 存在 / 当前 PID / 安装前节点数）；
2. `python3 zipfile` 解压到 `/root/giants_src`（**兼容 Windows 反斜杠条目，不依赖 unzip**，跳过 `.git/__pycache__`）；
3. 检查巨人依赖（opencv/scenedetect/ultralytics/requests/imageio-ffmpeg），缺则用 ComfyUI venv 的 pip 安装（远端实测**已齐**，此步应直接通过）；
4. 备份同名旧目录到 `/root/giants_backup_<时间戳>`，复制 6 包到 `/model/ComfyUI/custom_nodes/`，清 `__pycache__`；
5. **按当前进程原命令行重启**（保留 `--listen 0.0.0.0 --port 8188 --database-url sqlite:////tmp/comfyui.db`、`BETTERCOMFYUI_HOME=/data/BetterComfyUI`、cwd），日志追加 `/model/ComfyUI/comfyui-8188.log`，等 `/object_info` 就绪（最多 180s）；
6. 校验 8 个关键节点是否注册；失败 → 删新目录、恢复备份、重启、恢复节点数、打印日志尾部。

已对脚本做 `bash -n` 语法检查：**通过**（在远端 `/tmp` 上传临时副本校验后立即删除，未改动任何远端配置、未执行安装逻辑）。

---

## 5. 适配工作流（`docs/远程适配工作流/00_剧核总流程_4090适配版.json`）

- 生成脚本：`scripts/build_4090_workflow.py`（可离线用 `/object_info` 快照复跑）
- 结构：**28 节点 / 31 连线**，与源工作流一致（仅数据适配，不删节点、不改连线拓扑）
- 项目默认：**剧核试用**（`BetterComfyUIEverything / AssetGen / AssetIngest / AssetReview / Finalize / VideoReview` 6 处 + 使用说明文本）
- 改动清单（由脚本自动打印，共 20+ 条）：
  - 模型 6 处替换（见 §2③ 表）
  - `KSampler` 6→7 个 widget（补 `control_after_generate="fixed"`）
  - `MiniMaxH3Director` 25→26 个 widget（补 control + 确认 3 个 JS 分组控件占位：采样设置/高级采样/性能），`task_type` 由 `"r2v"` 修正为远端合法完整标签 `"r2v — 参考主体生视频(Reference to Video)"`
  - `CreateVideo` 4→2 个 widget（`[24.0, 8]`）、`SaveVideo` 1→3 个（`[prefix, "auto", "auto"]`）
  - `BetterComfyUIEverything` 24→21 个 widget（丢掉旧版残留的 3 个空值）
  - 「成片剪辑」开自动发现（`720` 分钟窗口）

### 校验结果（两套校验器都过）

**A. 本仓库新增的远端 schema 校验器**（`scripts/build_4090_workflow.py`，校验节点存在性/下拉值/widget 数量与类型/连线端口与类型）：
```
校验 1/2：直连远端 0.33（未装巨人）
  [预期缺失] 节点不存在：MiniMaxH3Director（id=18）
  → 预期缺失 1 条（巨人节点），其它问题 0 条
校验 2/2：复合 schema（远端 0.33 + 本地巨人定义，模拟装好巨人）
  [OK] 0 个问题
结论：PASS（除预期的巨人节点缺失外全部通过）
```

**B. 项目自带 `scripts/build_master_workflow.py::validate()`**（叠加本地巨人定义后）：**0 条问题，PASS**。
> 顺带发现一个健壮性小缺陷（未改项目代码）：`validate()` 在节点类型缺失时会先记一条「节点不存在」，随后遍历连线时 `KeyError` 崩溃；建议以后加 `if src_node["type"] not in info: continue`。

**C. 与源工作流结构对比**：节点数 28 vs 28、连线 31 vs 31、节点 id/类型完全一致（仅字段数据不同）。

---

## 6. 修复后用户应该看到什么（验收口径）

1. **导入《00_剧核总流程_4090适配版.json》**：
   - 安装巨人前：仍会有一个红色 `MiniMaxH3Director`（预期，需先跑安装脚本）；
   - **安装巨人后重新导入：没有任何 Missing Node Types 弹窗、没有 Missing Models 弹窗、所有节点无红框。**
2. **点队列前只剩 2 件用户输入**：① 右侧「Better ComfyUI」面板 → API 配置 → 填 DeepSeek 密钥；② 在「一体化总控台」粘贴剧本（项目已默认「剧核试用」，首次运行自动建项目）。
3. **点队列后**：不再出现 `missing_node_type / Value not in list / Failed to convert`；`KSampler`、`CreateVideo`、`SaveVideo` 校验通过；若显存不足（H3 峰值 45.58GB，ROUND36 实测）会 OOM —— 按工作流说明**分两段跑**（先静音③④组跑素材，再静音①②组跑出片）。
4. **若仍有「模型不存在」提示**：说明远端模型文件名又变了 —— 在该节点下拉里重选一次即可（对照 §2③ 表），或改 `scripts/build_4090_workflow.py` 里 `MODEL_MAP` 重跑生成。

---

## 7. 风险 / 回滚 / 替代方案

### 风险（按影响排序）
1. **巨人节点与 ComfyUI 0.33 的兼容性未知**（源码是在本地 0.41 环境开发/验证的）：最坏情况是巨人包 import 失败（节点不注册）或前端 JS 面板 API 不兼容（不影响后端出片）。安装脚本会在校验失败时**自动回滚**，ComfyUI 不会一直起不来。
2. **重启会打断队列**：脚本会 `kill -TERM` 当前 ComfyUI 再按原命令行拉起；有任务在跑时不要执行。
3. **显存**：4090 48G，H3 单模型实测峰值 45.58GB；本工作流「SDXL 素材生成 + H3 出片」同时驻留会 OOM → 必须分阶段（工作流说明里已写；也可用项目 `scripts/staged_run.py` 思路）。
4. **LoRA 语义**：远端的 `v1.1_768p` 是 fl2v Turbo LoRA，而本工作流 UNET 是 ref2va、导演台跑 r2v 模式；若画面/音画异常，换 §2③ 的 ref2v 备选 LoRA，并把导演台 `steps` 降到 4~8。
5. **成片剪辑自动发现的竞态**：同一队列里若「成片剪辑」比 `SaveVideo` 先执行，可能扫不到刚生成的 mp4（时间窗 720 分钟已覆盖历史文件；真遇到就重跑一次或手填路径）。
6. **timeline_data 为空**：巨人代码会自动补默认 timeline（`director_common.py:245`），风险低；若导演台报 timeline 相关错，在面板里点一次「生成时间线」再跑。

### 回滚方案
- **脚本自带**：任一步失败自动恢复备份目录 + 重启 + 校验节点数恢复安装前值（1545）。
- **手动回滚**：
  ```bash
  rm -rf /model/ComfyUI/custom_nodes/{ComfyUI_MiniMaxH3_Director,ComfyUI_MiniMaxH3_Smart_Director,ComfyUI-MiniMaxH3-ChainDirector,ComfyUI-DNNodes,ComfyUI-Storyboard-LLM,ComfyUI-DeepSeek-API-connector}
  # 如有旧版本：从 /root/giants_backup_<时间戳>/ 移回
  # 然后按安装脚本同样的命令行重启
  ```
- **一键还原到「没有巨人」状态**：删掉上面 6 个目录 + 重启，远端回到本次排查前的 1545 节点状态（我们的 24 个节点不受影响）。

### 替代方案（巨人兼容失败时，仍能出片）
- **出片用镜像自带生产工作流**：`/model/ComfyUI/user/default/workflows/minimax_h3_t2v_turbo.json`（ROUND36 实测：864×480/5.17s/含音频，冷 112.83s、热 82.45s、峰值 45.58GB），出片后用我们的《00_【总流程】段视频→剪好成片（本机可跑）》做**拼接+封装+自检**，再挂「视频审核」；
- 或只用 **S3 素材生成入库审核（GPU）**（本机可跑型演示工作流，注意用适配后的 widget 数组）+ 我们的剪辑/审核链路，不依赖任何巨人节点。

---

## 8. 下一步测试清单（本次未执行的部分）

| # | 动作 | 预期 | 只读/写入 |
|---|---|---|---|
| 1 | `bash -n /root/远程安装巨人.sh` | 语法 OK（本地已过一次） | 远端临时文件 |
| 2 | `DRY_RUN=1 bash /root/远程安装巨人.sh` | 6 目录解压齐、依赖已就绪、备份计划打印，不动服务 | 只写 /root/giants_src |
| 3 | `bash /root/远程安装巨人.sh`（空闲时） | 自动重启 + `MiniMaxH3Director` 等 8 节点注册 | 写 custom_nodes + 重启 |
| 4 | 远端 Web UI 导入适配版工作流 | 无 Missing 弹窗、无红框 | 只读 |
| 5 | 面板填 DeepSeek Key，只跑①②组（③④静音） | 素材生成→入库→审核通过 | 会占 GPU |
| 6 | 只跑③④组（①②静音） | 导演台出片 + SaveVideo | 会占 GPU |
| 7 | 跑⑤ + 成片剪辑（自动发现） | 成片路径自动接视频审核，输出结论 | CPU |
| 8 | 若巨人兼容失败 | 走 §7 替代方案，反馈日志给 Lead | — |

---

## 9. 交付物与证据

| 交付 | 路径 |
|---|---|
| 三错误分析 + 模型映射 + 风险/回滚/测试清单 | `docs/ROUND37_全链适配与三错误分析.md`（本文） |
| 4090 适配工作流（28 节点） | `docs/远程适配工作流/00_剧核总流程_4090适配版.json` |
| 工作流生成 + 校验脚本 | `scripts/build_4090_workflow.py` |
| 巨人安装脚本（含校验/回滚） | `scripts/远程安装巨人.sh` |
| 证据 | 远端 `/tmp/bcu_comfyui2.log`（missing_node_type #18、KSampler 6 错位）、远端 `/object_info` 快照、`/root/giants_source.zip` md5、`/model/ComfyUI/user/default/comfy.settings.json`（前端 1.48.7） |

**未做/未验证（不写成已验证）**：
- 未在远端安装巨人、未重启、未提交任何队列；
- 未在远端真实加载适配版工作流（需先装巨人，属于下一步 #3~#4）；
- 巨人节点在 0.33 上的真实注册/出片结果未验证（兼容性风险见 §7）；
- 本报告里的「修复后无弹窗」是**基于 schema/模型清单的静态核对结论**，最终以远端实测 #4 为准。


---

## 附录 A：Lead 真机验证结果（2026-10-18 10:25–10:40，实际执行）

### A1. 巨人节点安装（已执行，成功）
- 用 `/root/giants_source.zip`（26MB 源码，Windows 反斜杠 zip 已用 python zipfile 修正）安装 5 个包：
  `ComfyUI_MiniMaxH3_Director / Smart_Director / ChainDirector / Storyboard-LLM / DeepSeek-API-connector`
- 重启 ComfyUI（保留 `--database-url` + `BETTERCOMFYUI_HOME=/data/BetterComfyUI`），10s 就绪。
- **注册校验：8/8 节点 OK**：MiniMaxH3Director、MiniMaxH3DirectorGroupsCombine、
  GroupReferenceToVideo、GroupImageToVideo、MiniMaxH3SmartDirector、MiniMaxH3ChainDirector、
  StoryboardNode、DeepSeekPromptConnector；我们 24 个 BetterComfyUI 节点仍全部在。
- **DNNodes 包未装**：其 `__init__.py` 依赖 `comfyui-SelfLift`（远端未安装），导入失败；
  本工作流**不使用** DNNodes，故跳过（日志 `IMPORT FAILED: .../bettercomfyui` 是裸包提示，无害）。

### A2. 适配版全链工作流（已上传到远端 UI 工作流目录）
`/model/h3-persistent/user/default/workflows/00_剧核总流程_4090适配版.json`
- 前端官方方式加载：**28 节点 / 28 prompt 节点 / badNodes=[]**，`graphToPrompt()` 成功；
- 节点类型对照远端 `/object_info`：**21 种类型，0 缺失**；
- 模型下拉核对：UNET / CLIP(int8) / 两个 VAE / LoRA(v1.1) / ckpt(RealVisXL) 全部在远端可选项内。

### A3. S3 素材链路真机验证（真实 SDXL 出图）
- 适配版 `S3_素材生成_4090适配版.json`：**12 节点 / 15 连线**（新增「入库素材ID → 审核 asset_id」依赖边，
  修复原来“审核可能先于入库执行”的缺陷）；
- 真机结果：SDXL 生成 **12.36s** → 入库 **v1 + sha256:9959bad5… + PENDING_REVIEW** →
  素材审核（本地启发式）**60.205 < 70 → BLOCKED、CNT1=1、LOCK 未取得**（完全符合 R3：未过审核不得用于生成；
  旧版本结论不因重跑而失效）；审核状态/计数器已跨运行落盘。
- 结论：**素材生成→入库→版本指纹→素材审核（BLOCKED/CNT1/无 LOCK）在真机 4090 上跑通**；
  LOCK 成功需要更高分素材（调模型/提示词/阈值），链路本身已验证。

### A4. 真机验证后的使用状态
- 用户在远端 Web UI（`http://your-gpu-host:8188`）刷新后应看到：
  `00_剧核总流程_4090适配版`、`S3_素材生成_4090适配版`、`1_剧核试用_剧本到全部内容`、`2_剧核试用_段视频到成片与审核`；
- 打开适配版：**无 Missing Node Types / Missing Models 弹窗、无红框**；
  点队列前只剩：右侧面板填 DeepSeek Key、总控台粘剧本（项目默认「剧核试用」）；
- 显存提醒：SDXL 素材 + H3 出片同图一次跑在 48G 上会 OOM（实测 H3 全链峰值 45.58GB），
  请按工作流说明**分两段**（先素材审核，再出片+剪辑+审核）。
