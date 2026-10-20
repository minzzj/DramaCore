# 第三轮进展记录（2026-10-17 凌晨）

> 目标轮次 1/60 内。本轮做了一件此前认为「做不到」的事：
> **在没有模型权重的情况下，真实验证了与 ComfyUI 的执行链路**，
> 并因此抓到一个会让**正常作业被误判为失败**的真实 bug。

---

## 一、关键突破：不用权重也能验证真实执行链路

此前我一直把「无法验证与 ComfyUI 的真实集成」归因于缺 60GB 权重。
**这个归因是错的。**

ComfyUI 有大量**纯 core 节点不需要任何权重**就能跑完一次执行。用一个三节点工作流：

```
EmptyImage(256x256) -> ImageScale(128x128) -> PreviewImage
```

实测结果（对运行中的 0.41.0）：

```
submitted  : 41f07ab3-20b5-43db-b673-842ab5ff579f
status     : success | completed: True
outputs    : ['3']
   image   : ComfyUI_temp_xsptv_00001_.png
```

**所以「HTTP 集成是否正确」是可验证的**，只是验证方式不同：
不验证「出片对不对」，而验证「提交/轮询/解析/幂等 是否真的通」。

这补齐了此前**只被 FakeTransport 覆盖**的部分。
FakeTransport 证明的是「我按我理解的协议写」；
这条测试证明「**ComfyUI 也这么理解**」—— 两者的差距正是 bug 藏身处。

---

## 二、因此抓到的真实 bug：提交后的竞态被误判为「作业丢失」

### 现象

紧接 `submit()` 调用 `poll()`，返回：

```
FAILED: ComfyUI history 与 queue 中都不存在作业 <prompt_id>（可能被重启清除）；按技术失败处理
```

**但该作业实际执行成功了**，产出 png 就在磁盘上。

### 根因

`/prompt` 返回 `prompt_id` 之后，作业要经过一小段
「已受理、尚未入队/开始执行」的过程。
在这个窗口里 `/history` 与 `/queue` **两头都查不到**。

原实现把「查不到」直接判为 `FAILED（作业丢失）`。
这是错的：**「还没出现」≠「已丢失」**。

### 危害

正常作业被当成失败 → 触发不必要的重试、消耗技术重试计数，
严重时按「技术失败」路径处理一个**其实成功**的作业（重复出片、版本混乱）。

### 修复

引入**提交后宽限期**（`submit_grace_s`，默认 10 秒）：

| 情况 | 处理 |
|---|---|
| 宽限期内查不到 | 返回 `UNKNOWN` + 明确说明「属正常现象，请稍后重试」 |
| 超过宽限期仍查不到 | 仍判 `FAILED`（作业确实丢失） |
| 查到 | 正常返回 QUEUED / RUNNING / SUCCEEDED / FAILED |

用 `time.monotonic()` 而不是 `time.time()` 计时（单调时钟不受系统时间调整影响）。

### 实测对比（同一竞态时刻）

```
grace= 0.0s  立即 poll -> FAILED    ComfyUI history 与 queue 中都不存在作业 29b7a97f...
             稍后 poll -> SUCCEEDED
grace=10.0s  立即 poll -> UNKNOWN   作业 b2efba77... 尚未出现在 history/queue 中（宽限期内，属正常）
             稍后 poll -> SUCCEEDED
```

**注意 `grace=0.0` 那行仍然复现旧行为** —— 说明这条测试真的覆盖了该竞态，
不是碰巧通过。

---

## 三、本轮新增测试（30 条）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_comfyui_live_integration.py` | 5 | 真实提交被接受 / 幂等键防重复 / 轮询到 SUCCEEDED 且有产出 / capabilities 真读实例 / **跑得通 ≠ 能力已验证** |
| `tests/test_director_nodes_live.py` | 4 | 三个导演台节点被真的识别 / `require_nodes()` 放行 / 版本现场读且与实例一致 / 节点可用 ≠ 能力已验证 |
| `tests/test_consistency.py` | 21 | 禁硬编码版本（AST 扫描）/ 版本数字比较 9 例 / 定长元组 / metadata mock 标记 4 条 + 反向断言 |

**两个文件都无服务时 skip 并标注「未验证」** —— 不假装跑过。

### 一个反复出现的纪律：区分「通了」与「验证了」

这两条断言是本轮刻意加的：

* `test_live_node_availability_does_not_mean_capability_verified`
* `test_no_weights_means_capabilities_stay_unverified`

本机现在**导演台节点齐全**且**纯 core 工作流能跑通**，
最容易犯的错就是顺手把 `verified` 改成 `True`。
那会变成「HTTP 通了 → 声称能出片」的假成功。
所以专门写了反向断言钉住它。

---

## 四、权重下载：已实测可下载，并修掉上游下载器的三个问题

### 实测：6 个文件全部可达，合计 59.38 GB

| 文件 | 大小 |
|---|---|
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 19.53 GB |
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 19.53 GB |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 14.61 GB |
| `minimax_h3_video_vae_fp16.safetensors` | 4.85 GB |
| `minimax_h3_audio_vae_fp32.safetensors` | 0.56 GB |
| `minimax_h3_fl2v_lightx2v_turbo_4step_..._bf16.safetensors` | 0.29 GB |

镜像 `hf-mirror.com` 全部可达。已实测下载 LoRA（0.29 GB）成功并通过 doctor 复核
（`权重文件数: 1`）。

### 重写下载器（`scripts/download_models.py`）

读上游 `ChainDirector/scripts/download_models.py` 发现三个实际问题：

1. **根目录定位不可靠** —— 用固定三级 `dirname` 跳转；脚本一挪位置就失效；
2. **没有断点续传** —— 60GB 断一次就从头再来；
3. **没有空间检查、没有大小校验** —— 下载前不查盘，下载后只看到「文件非空」，
   截断的 safetensors 要到加载时才报错。

新脚本逐条解决：显式/配置化根目录、HTTP Range 续传（服务器不支持 Range 时
自动回退为从头写）、下载前查可用空间、下载后按 `Content-Length` 校验、
已完成跳过、`--check` 只体检不下载、`--only` 分批。

**当前状态**：后台下载进行中（`pwsh-1104`）。
注意一个实测细节：首个 60 秒测到 291 MB/s（有突发缓存），
随后稳定在约 4 MB/s —— 按稳定速率 59GB 约需 4 小时。

---

## 五、目标机操作手册

新增 `docs/TARGET_MACHINE_HANDOFF.md`，把换机流程写成可照做的步骤：
前置条件（显存/内存/磁盘/CUDA torch 的实测数值）、下载、
装导演台包、配置、**跑融合体检（本机唯一做不到的那步）**、
拖入融合工作流、出片后必须检查 `is_mock=false`、
以及本机「能做什么/不能做什么」的如实清单。

---

## 五之二、下载开始后又抓到一个假阳性：`real_generation: True`

### 现象

下载了 LoRA + VAE + 文本编码器之后，`doctor` 报：

```
真实生成：环境看起来具备条件
```

**这是假的** —— 没有任何扩散模型，根本出不了片。

### 根因

`real_generation_ok` 的判据是 `model_files > 0`。
**「有文件」被当成了「能出片」。**

实测经过值得记下来：我先下载了最小的 LoRA（0.29 GB）验证下载链路，
`model_files` 从 0 变成 1；再下载 VAE 与文本编码器后变成 3–4。
判据只看非零，于是立刻翻成 True。

### 修复

改为检查**构成一次最小视频生成所必需的三个类别**：

| 类别 | 目录 |
|---|---|
| 扩散模型 | `diffusion_models` / `unet` / `checkpoints` |
| 文本编码器 | `text_encoders` / `clip` |
| VAE | `vae` |

三类齐全才报可用；否则**点名缺哪几类**：

```
真实生成：不可用 —— 虽然 models/ 有 2 个权重文件，但**缺少必需类别**：文本编码器、VAE。
  （有文件 ≠ 能出片：例如只有 LoRA 时无法生成任何内容）
```

`report` 里新增 `real_generation_missing_kinds` 供程序判断。

### 守卫

`tests/test_cli_doctor.py` 新增 F3 三条（并修正了一条按旧判据写的既有测试）：

* `test_only_lora_present_is_not_reported_as_available` —— 只有 LoRA + VAE + 文本编码器、**没有扩散模型**，必须判不可用；
* `test_all_required_kinds_present_reports_available` —— 三类齐全才报可用（**反向断言，防止上一条恒真**）；
* `test_lora_only_names_every_missing_kind` —— 只有 LoRA 时必须点名缺三类。

这是本轮**第二个**「有东西 / 通了 ≠ 能用」的假阳性
（第一个是「节点可用 ≠ 能力已验证」）。两者都已写成断言钉住。

---

## 六、当前状态

| 项 | 结果 |
|---|---|
| 全量测试 | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| 15 条验收清单 | 15/15 |
| 真实 ComfyUI 执行链路 | ✅ **已验证**（纯 core 工作流提交→执行→解析） |
| 导演台节点检测 | ✅ 三个指定包全部识别，`require_nodes()` 放行 |
| 权重 | 🔄 下载中（2.82 / 59.38 GB） |
| 真实 H3 出片 | ❌ 仍需 ≥24GB 显存（本机 4GB，档位 `minimal`） |

---

## 七、仍未验证（收窄了，但没有消失）

1. **真实 H3 视频出片** —— 需 ≥24GB 显存。本机 4GB，物理不可行。
   *这一条现在不再包含「HTTP 集成」——那部分已验证。*
2. **真导入 `pack_*` 做运行时字段比对** —— 需在 ComfyUI 运行时内执行（已写进目标机手册第 5 步）。
3. **网页面板在浏览器里的最终渲染**。
4. 参考音色链路与导演台高级模式接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出；跳过项均标注「未验证」。_
