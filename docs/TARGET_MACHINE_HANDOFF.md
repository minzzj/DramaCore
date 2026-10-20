# 切到目标机（≥24GB 显存）的操作手册

> 本机是开发机：GTX 1050 Ti **4GB**、CPU 版 torch，**物理上跑不了 H3 采样**。
> 真实出片必须换机。这份手册把换机流程写成可照做的步骤，并标出**每一步怎么验证**。

---

## 0. 前置条件（必须全部满足）

| 项 | 要求 | 为什么 |
|---|---|---|
| 显存 | **≥24GB**（实测峰值 **22,016 MiB ≈ 21.5 GiB**） | H3 六段生产实测口径 |
| 系统内存 | **≥64GB（硬性）** | **实测**：最小图也需同时驻留 ref2va（19.53 GB）+ 文本编码器（14.61 GB）= **34.14 GB**。本机 15.75 GB，实测进 CPU 回退路线时**完全死锁**（0 CPU / 0 磁盘 / 单线程，见 ROUND12_FINDINGS.md） |
| 磁盘 | **≥100GB** 可用（权重 **59.38 GB**） | 已实测 6 个文件的真实大小 |
| GPU 驱动 | 支持 CUDA 的 NVIDIA 驱动 | — |
| PyTorch | **CUDA 版**（本机是 `2.14.1+cpu`，必须换） | CPU 版无法用 GPU |
| ComfyUI | **≥0.30**（本机已装 **0.41.0**，满足） | 导演台下限 |
| ffmpeg | 在 PATH 或 `imageio-ffmpeg` 可用 | 拼接/封装 |

**实测权重清单（6 个文件，59.38 GB）**：

| 文件 | 大小 | 目标目录 |
|---|---|---|
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 19.53 GB | `models/diffusion_models` |
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 19.53 GB | `models/diffusion_models` |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 14.61 GB | `models/text_encoders` |
| `minimax_h3_video_vae_fp16.safetensors` | 4.85 GB | `models/vae` |
| `minimax_h3_audio_vae_fp32.safetensors` | 0.56 GB | `models/vae` |
| `minimax_h3_fl2v_lightx2v_turbo_4step_..._bf16.safetensors` | 0.29 GB | `models/loras` |

来源：`Comfy-Org/MiniMax-H3`（前 5 个）与 `Kijai/MiniMax-H3_comfy`（LoRA）。
镜像 `hf-mirror.com` **已实测可访问**，6/6 全部可达。

---

## 1. 下载权重

```bash
# 从配置读 ComfyUI 路径
python scripts/download_models.py --comfy-root-from-config

# 或显式指定
python scripts/download_models.py --comfy-root "D:\ComfyUI"

# 只看清单/空间，不下载
python scripts/download_models.py --comfy-root-from-config --check

# 分批（例如先下小的验证链路）
python scripts/download_models.py --comfy-root-from-config --only minimax_h3_fl2v_lightx2v
```

**为什么用这个脚本而不是导演台自带的**：上游 `chain/scripts/download_models.py`
用固定三级 `dirname` 跳转定位根目录（实测本机布局下推断是错的），
且**没有断点续传**（60GB 断一次就白等）、**没有空间检查**、**没有大小校验**。
本脚本逐条解决：显式指定根目录、HTTP Range 续传、下载前查空间、
下载后按 `Content-Length` 校验、已完成跳过。断线直接重跑即可续传。

**验证**：
```bash
python -m bettercomfyui.cli doctor     # 应报告权重文件数 = 6，且版本 >= 0.30
```

---

## 2. 装 ComfyUI（若目标机还没有）

```bash
python scripts/setup_comfyui.py        # 建 venv + torch + ComfyUI + 依赖 + 同步插件
```

**注意**：`setup_comfyui.py` 装的是 **CPU 版 torch**（因为本机要能跑）。
目标机请在装好后手动换成 CUDA 版：

```bash
.comfyenv\Scripts\pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu124
```

然后确认：
```bash
.comfyenv\Scripts\python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

---

## 3. 装三个导演台包（本机已验证方式）

三个包在 `保留备用的节点/` 里，直接复制到 `custom_nodes`：

```
ComfyUI_MiniMaxH3_Director        -> custom_nodes/
ComfyUI_MiniMaxH3_Smart_Director  -> custom_nodes/
ComfyUI-MiniMaxH3-ChainDirector   -> custom_nodes/
ComfyUI-DNNodes                   -> custom_nodes/     （素材卡打包，融合节点用到）
```

**本机已实测**：这 4 个包与我的插件共存，ComfyUI 0.41.0 注册 **1024** 个节点类型，
其中 `MiniMaxH3*` **20 个**、我的 **14 个**。

ChainDirector 官方工作流还用到两个外部包（本机**未装**，需要时再装）：
- `ComfyUI-VideoHelperSuite`（`VHS_VideoCombine`）
- `ComfyUI-KJNodes`（`PathchSageAttentionKJ`）

> `git clone` 在本机被墙（`github.com:443` 超时），但 **HTTPS 下载 zip 正常** ——
> 用 codeload zip 解压安装即可。

---

## 4. 配好配置

`config/user.yaml`：

```yaml
global:
  comfyui_base_path: '目标机的 ComfyUI 根目录'   # 含 comfyui_version.py 与 models/
  comfyui_endpoint: 'http://127.0.0.1:8188'
```

**验证**：
```bash
python -m bettercomfyui.cli doctor
```
应看到：版本号正确、自定义节点含 4 个包、**权重文件数 = 6**、
「真实生成：环境看起来具备条件」。

> ⚠️ `doctor` 说的「具备条件」≠ 已验证出片。它只检查前置条件。

---

## 5. 启动并做**融合体检**（本机做不到的最后一步）

```bash
.comfyenv\Scripts\python main.py --listen 127.0.0.1 --port 8188
```

打开 `http://127.0.0.1:8188`，在节点搜索里找 **`Better ComfyUI 融合·体检`**，加一个到画布，点 Queue。

它会做两件事：
1. 逐个点名检查三个导演台的节点类型是否已注册；
2. **用真实 `pack_i2v_group` / `pack_r2v_group` 做运行时字段比对**。

第 2 项是本机**唯一没能完成**的验证 —— 因为 Director 包内部 `import comfy`，
必须在 ComfyUI 运行时内才能导入。本机只做了 **AST 静态比对**
（已证明能抓 bug：它当场抓到我漏了 `first_frame`/`last_frame`）。

**期望输出**：
```
[契约自检] 与真实包 pack_* 逐字段比对：
  [OK] i2v 字段完全一致
  [OK] r2v 字段完全一致
```

如果出现「[不一致]」，请把它贴给我 —— 那说明真实包的 schema 与我复刻的有差异。

---

## 6. 拖入融合工作流

`bettercomfyui_comfy/workflows/` 下两个工作流都已对真实 schema 校验通过（PASS）：

| 文件 | 拓扑 |
|---|---|
| `fusion_A_director_r2v.json` | 项目 → **融合·段计划→导演组** → `MiniMaxH3Director.r2v_groups` |
| `fusion_B_chain_director.json` | **融合·时间轴文本** → `MiniMaxH3ChainDirector` → **融合·回灌段版本** |

拖进 ComfyUI 后：
1. 工作流 A 里补上 `MiniMaxH3Director` 的 5 个必需连线（`model`/`video_vae`/`audio_vae`/`clip` + `image_0`）；
2. 或直接用 ChainDirector 路线（B），它自带模型加载链，接线更少。

---

## 7. 出片后必须做的检查

1. **`融合·回灌段版本` 节点**：把导演台的 `images`+`audio` 回灌成段版本
   —— 写入内容指纹与审核材料；
2. 打开成片的 `*.metadata.json`，确认：
   ```json
   "is_mock_output": false,
   "generation_provenance": { "is_mock": false, "executor": "comfyui" }
   ```
   **`is_mock` 必须是 `false`** —— 若为 `true` 说明走的是占位路径，不是真实生成。
3. 按项目策略做段级/整片审核。

---

## 8. 本机（开发机）能做什么 / 不能做什么

**能做**（已实测）：
- 编排逻辑全链路：项目隔离、剧本校验、素材匹配/LOCK、审核状态机、CNT1/2/3、
  级联返工、拼接封装、整片审核策略、检查点恢复 —— **909+ 测试全绿**。
- 拼接/封装/字幕/水印/封面：**真实 ffmpeg** 完成。
- 插件加载、14 个节点注册、5 条 HTTP 路由。
- 导演台节点**检测**（节点在不在、版本多少）。

**不能做**：
- ❌ 真实 H3 采样（4GB 显存 + CPU torch）
- ❌ 真导入 `pack_*` 做运行时字段比对（见 §5）
- ❌ 任何依赖模型权重的端到端出片

---

## 9. 一键自检清单

换机后按顺序跑，每步都应有明确输出：

```bash
# 1 环境
python -m bettercomfyui.cli doctor                 # 权重数应为 6

# 2 编排层全量测试
python -m pytest tests/ -q                          # 应 EXIT=0

# 3 融合工作流结构校验（需 ComfyUI 在跑）
python bettercomfyui_comfy/workflows/validate_fusion_workflow.py

# 4 端到端（仍可先用 mock 验证编排闭环）
python -m bettercomfyui.cli run --executor mock

# 5 真实出片（在 ComfyUI 界面里跑融合工作流）
```

---

_本手册由 Lead 于 2026-10-17 凌晨撰写。所有实测数字均来自本机实际执行。_
