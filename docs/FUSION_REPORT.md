# 融合报告（FUSION REPORT）

> 2026-10-17 凌晨。本报告记录**编排层与三个真实导演台的融合**：
> 契约证据、融合点、实测结果、替代验证方法、以及**未验证项**。
> 架构说明见 [INTEGRATION.md](INTEGRATION.md)。

---

## 一、先说结论（含我此前犯的错）

**用户指出的问题成立**：我此前把 AI 视频生成做成了自己编排器里的 mock，
把用户给的导演台当成"候选组件"审计了一遍就放过 —— **把核心引擎降级成了可选依赖。**

**现在已纠正**：三个导演台包已装进 ComfyUI 并**实测注册成功**，我的 14 个节点与它们
在同一个 ComfyUI 里共存，融合接缝是**官方留好的** `MMX_DIR_GROUP` 输入。

---

## 二、实测结果（全部本次真实执行）

| 验证项 | 结果 |
|---|---|
| ComfyUI 运行中 | ✅ 0.41.0，http://127.0.0.1:8188 |
| 三个导演台包已安装 | ✅ `ComfyUI_MiniMaxH3_Director`、`ComfyUI_MiniMaxH3_Smart_Director`、`ComfyUI-MiniMaxH3-ChainDirector`（+`ComfyUI-DNNodes`） |
| 导演台节点真的注册 | ✅ `MiniMaxH3Director` / `MiniMaxH3SmartDirector` / `MiniMaxH3SmartDirectorAllInOne` / `MiniMaxH3ChainDirector` / `MiniMaxH3DirectorGroupImageToVideo` / `...GroupReferenceToVideo` / `...GroupsCombine` / `H3MediaLoader` / `DNMediaToDirectorGroup` **全部已注册** |
| 我的节点 | ✅ **14 个**（原 6 个 + 5 个融合 + 3 个素材适配） |
| ComfyUI 节点类型总数 | 1024 |
| 融合契约测试 | ✅ `tests/test_fusion_contract.py` **14 通过 / 1 skip**（skip = 需完整 ComfyUI 运行时的真导入比对，已标注未验证） |
| 契约守卫有效性 | ✅ **变异测试证明**：删掉 r2v 的 `first_frame`/`last_frame` → 测试变红；恢复 → 变绿 |

---

## 三、融合接缝（这是全部工作的关键发现）

`ComfyUI_MiniMaxH3_Director/nodes/director.py`：

```python
class MiniMaxH3Director:
    RETURN_TYPES = ('IMAGE','AUDIO','FLOAT','INT','IMAGE','STRING','IMAGE','IMAGE')
    RETURN_NAMES = ('images','audio','fps','frame_count',
                    'source_images','report','images_pre_refine','images_pre_face_refine')
    FUNCTION = 'execute'
    # required: model / video_vae / audio_vae / clip + director_timeline_required_inputs()
    # optional: i2v_groups: MMX_DIR_GROUP
    #           r2v_groups: MMX_DIR_GROUP
    #           tooltip: "When connected, overrides UI cards for execution (external priority)."
```

**它自己就是真实生成引擎**（输出 IMAGE 帧 + AUDIO），并且**官方留了"外部优先"的驱动口**。
所以融合不需要改它一行代码：**只要产出正确的 `MMX_DIR_GROUP`。**

### 三个包的角色（实测）

| 包 | 节点 | 角色 | 输出 |
|---|---|---|---|
| `ComfyUI_MiniMaxH3_Director` | `MiniMaxH3Director` | **执行引擎** | IMAGE + AUDIO |
| | `...GroupImageToVideo` / `...GroupReferenceToVideo` / `...GroupsCombine` | 组打包 | `MMX_DIR_GROUP` |
| `ComfyUI_MiniMaxH3_Smart_Director` | `MiniMaxH3SmartDirector` | AI 分镜 → 组 | `MMX_DIR_GROUP` + `SMART_PLAN` |
| | `MiniMaxH3SmartDirectorAllInOne` | 一体化（自己出片） | IMAGE + AUDIO |
| `ComfyUI-MiniMaxH3-ChainDirector` | `MiniMaxH3ChainDirector` | 自动分段+首帧硬锁定接力 | IMAGE + AUDIO |

**关键分工**：`SmartDirector` 只**产出**组，`Director` 只**消费**组。
我的编排层做同一件事（产出组），但**多带版本/LOCK/审核**。

---

## 四、`MMX_DIR_GROUP` 载荷契约（逐字段来自真实源码）

`director/external_groups.py`：

```python
def pack_i2v_group(*, prompt='', duration_sec=..., first_frame=None, last_frame=None):
    return {'version':1, 'family':'i2v', 'kind': infer_i2v_kind(first,last),
            'prompt':..., 'duration_sec':float(...),
            'first_frame': first[:1].clone() if first is not None else None,
            'last_frame':  last[:1].clone()  if last  is not None else None,
            'ref_images':{}, 'ref_videos':{}, 'ref_video_audios':{}, 'ref_audios':{}}

def pack_r2v_group(*, prompt='', duration_sec=..., ref_image_size='match',
                   ref_images=None, ref_videos=None, ref_video_audios=None, ref_audios=None):
    return {'version':1, 'family':'r2v', 'kind':'r2v', 'prompt':...,
            'duration_sec':..., 'ref_image_size':...,
            'first_frame': None, 'last_frame': None,      # ← 我第一版漏了这两个
            'ref_images':{idx:tensor}, 'ref_videos':{...},
            'ref_video_audios':{...}, 'ref_audios':{...}}
```

`infer_i2v_kind`：both→`fl2v`；其一→`i2v`；皆无→`t2v`。

### ❗ 实测抓到的我自己的 bug

我第一版 `make_r2v_group` **漏了 `first_frame` / `last_frame`**（读源码时被截断误导）。
若不修，r2v 组会缺字段，行为不可预期。

**抓住它的方法是硬证据而不是记忆**：`tests/test_fusion_contract.py` 用 **AST 解析真实源码**，
从 `pack_r2v_group` 的 `return` 字面量里把字典键抄出来，与我的输出做集合比对。
变异测试证明这条守卫有效（见第二节）。

---

## 五、我新增的融合能力

### 5.1 融合节点（5 个）

| 节点 | 作用 |
|---|---|
| `BetterComfyUI 融合·段计划→导演组` | 剧本 + **已 LOCK 素材** → `MMX_DIR_GROUP`，直连 `Director.i2v_groups`/`r2v_groups` |
| `BetterComfyUI 融合·单组打包` | 手工打包一个组（首尾帧/参考图/强制 kind） |
| `BetterComfyUI 融合·组合并` | 多组合并（对应 `GroupsCombine`） |
| `BetterComfyUI 融合·回灌段版本` | Director/ChainDirector 的 `images`+`audio` → 段版本 + 内容指纹 + 审核材料 |
| `BetterComfyUI 融合·体检` | 检查三个导演台是否装齐 + **用真实 `pack_*` 做字段比对** |

### 5.2 素材适配节点（3 个）

| 节点 | 作用 |
|---|---|
| `BetterComfyUI 素材·取已锁定图` | `asset_id`(+`version`) → **IMAGE**，直连 `ref_image_N` 或 `image_0..8` |
| `BetterComfyUI 素材·多槽输出(1-9)` | 按段 `asset_refs` 顺序出 9 个 IMAGE 槽 |
| `BetterComfyUI 融合·时间轴文本` | 生成 ChainDirector 的 `时间轴提示词`，并**规避它的真实陷阱** |

### 5.3 两条硬规则在这个接缝上落地

1. **R3 门禁**：只有 `resolve_lock()` 通过的版本才允许进组。未 LOCK **直接报错**，
   绝不静默跳过（跳过会让你以为素材用上了而实际没有）。有针对性测试。
2. **版本可追溯**：每个参考槽都记录 `asset_id / version / lock_id / lock_source / content_hash`，
   写进节点的"审计JSON"输出与回灌材料，成片能追到具体素材版本。

---

## 六、两个真实陷阱（读源码发现，已在我的节点里规避）

### 6.1 ChainDirector 的 `全局提示词` 会被丢弃 ★

`CD/chain.py:270-307` 构造的 timeline 里**没有 `commonEnabled`**，
导致依赖包 `DIR/director/external_groups.py:676` 走
`prompt = group_prompt or fallback_prompt` 的 else 分支：
**只要段文本非空，`全局提示词` 就被完全丢弃**，不进入正向提示词
（最终落点 `DIR/director/executor_core.py:795` `positive_prompt = seg.prompt`）。

→ 我的 `融合·时间轴文本` 节点把"全片设定"**逐段前置**到每行时间轴文本里，
保证设定句一定生效，而不是依赖一个会被丢掉的输入。

### 6.2 ChainDirector 中文节点的 `ref_video_fps` 参数失效

`CD/chain.py:501` 读 `kw.get("参考视频帧率", 0)`，而 INPUT_TYPES 里的键是 `ref_video_fps`
（`CD/chain.py:455`）→ **恒为 0** → 永远走等比切分，无法按帧率对齐参考视频。
英文版 `CD/chain_en.py:91` 写法正确。

→ 规避：需要精确按秒切参考视频时用英文节点 `MiniMaxH3ChainDirectorEN`。
**这是上游包的 bug，我没有改它的代码**（不在我的写入范围，且改了会与上游分叉）。

---

## 七、测试方法（含替代验证，你要求的部分）

| # | 方法 | 替代了什么 | 边界 |
|---|---|---|---|
| **F1** | **AST 解析真实源码提取字段**，与我的输出做集合比对 | 「我的记忆是对的」 | 只能验字段集合与 `kind` 分支，验不了运行时类型 |
| **F2** | **变异测试**（删字段→变红→恢复→变绿） | 「测试是不是白写的」 | 只对已覆盖项有效 |
| **F3** | **真实 store 的 R3 门禁测试**（建项目→建素材→不加锁→必须报错） | 手工验证审核门禁 | 未覆盖多版本/兜底策略组合 |
| **F4** | **ComfyUI 实测注册**（`/object_info` 查节点名） | 手工点界面 | 证明注册成功，**不证明节点连线后能出片** |
| **F5** | **静态 schema 自检**（`融合·体检` 节点内） | — | 装了真实包时用 `pack_*` 运行时比对 |

### 未验证项（如实列出）

1. **端到端出片未验证** —— 需要 MiniMax H3 模型权重（约 60GB）与本机没有的 GPU 能力。
   本机是 CPU 版 torch + 4GB 显存开发机，**不可能跑 H3 采样**。
2. **`segments`/`groups` 被 Director 实际消费未验证** —— 需要模型权重的完整运行。
3. **ChainDirector 参考视频切分的实测未验证**（依赖权重）。
4. ~~真导入 `pack_*` 的运行时比对未在本机完成~~ —— ✅ **已于 2026-10-17 完成**（见 §十一）。原说明：Director 包内部 `import comfy`，
   必须在 ComfyUI 运行时内才能导入；本机只做了 AST 静态比对（已证明能抓 bug）。
   装齐权重后在 ComfyUI 里跑一次 `BetterComfyUI 融合·体检` 即可完成这最后一步。
5. **参考音色/音频链路、导演台高级模式（二采/Guide/SelfLift）未接入我的编排层。**

---

## 八、还差什么才能真正出片

| 需要 | 说明 |
|---|---|
| **模型权重约 60GB** | 6 个文件：ref2va/fl2va 剪枝 int8、qwen3vl_32b 文本编码器、video/audio VAE、turbo LoRA。来源 `Comfy-Org/MiniMax-H3` 与 `Kijai/MiniMax-H3_comfy`，或跑 `ChainDirector/scripts/download_models.py` |
| **GPU** | 实测口径：峰值显存 **22,016 MiB**（约 21.5GB）；系统内存建议 ≥64GB。本机 4GB 不够 |
| **`ffmpeg` 在 PATH** | 导演台导出视频需要 |
| 可选 | `sageattention`、`ComfyUI-VideoHelperSuite`、`ComfyUI-KJNodes`（ChainDirector 官方工作流用到 `VHS_VideoCombine`、`PathchSageAttentionKJ`） |

> `git clone` 在本机被墙（`github.com:443`），但 **HTTPS 下载 zip 正常**，
> 所以外部包可以下载 zip 解压安装。

---

## 九、怎么用（融合工作流）

```
[Better ComfyUI 项目] ─project_id─┐
                                  ├─▶ [融合·段计划→导演组] ─MMX_DIR_GROUP─▶ MiniMaxH3Director.r2v_groups / i2v_groups
[剧本 JSON / 纯文本]──────────────┘         │
                                            └─审计JSON─▶ 显示
[Better ComfyUI 素材·取已锁定图] ─IMAGE─▶ MiniMaxH3DirectorGroupReferenceToVideo.ref_image_1..N
                                                    │
                                          MiniMaxH3Director(images/audio)
                                                    │
                    [融合·回灌段版本] ◀─────────────┘
                            │
                            └─▶ 段版本 + 内容指纹 + 审核材料（未审核不得拼接）
```

**替代：ChainDirector 路线**（它自己出片）

```
[融合·时间轴文本] ─时间轴提示词─▶ MiniMaxH3ChainDirector
[素材·多槽输出] ─IMAGE─▶ image_0..image_8
MiniMaxH3ChainDirector ─画面帧/音频─▶ [融合·回灌段版本] ─▶ 段版本 + 审核
```

**最简"先看效果"路线**（不需要我的编排层）：

```
[MiniMaxH3SmartDirector] ─导演组─▶ MiniMaxH3Director ─▶ CreateVideo ─▶ SaveVideo
```

---

_本报告由 Lead 于 2026-10-17 凌晨撰写。所有 ✅ 均来自本次实际执行的命令输出；未验证项已单独列出。_

---

## 十、goal 第 3 项的完成状态（2026-10-17 更新）

原文要求：「把 Director 的 images/audio 存成段版本**并触发段级审核与 CNT2/CNT3**」。

最初实现只做到前半句（存文件 + 写审核材料 JSON）。
后来发现两处不足并补齐：

| 子项 | 状态 | 证据 |
|---|---|---|
| 存成**真实段版本**（不是只写 JSON） | ✅ | `store/segments.py`；实测 `SEG0001@v1` 落盘、新实例可读回 |
| 重启不重复建版本（验收 12） | ✅ | 同版本同指纹**幂等**；不同指纹**报错**（有变异测试证明守卫有效） |
| **触发段级审核** | ✅ | 审核结论写回段存储 + 进审核材料；审核崩溃时明说"未完成"，绝不假装通过 |
| **CNT2 / CNT3 分开记账** | ✅ | 实测 `CNT2=3`、`CNT3=1` 各自独立落盘；首次生成不计 |
| 版本可复现 | ✅ | 种子随版本落盘（`SegmentAttempt.used_seed`） |

**顺带修掉的真 bug**：`add_version` 原本只推进 `candidate_version`，
不推进 `current_version`，导致审核器要求 `version == current_version` 时
**每个新版本都被判为 stale 而无法审核**。
修法是让新版本成为 current（而不是放宽守卫 ——
那条守卫正是「迟到结果不得覆盖新版本」的实现基础）。
并补反向断言测试，防止后来者放宽它。

---

## 十一、运行时契约比对：已完成（2026-10-17 更新）

### 之前为什么"做不到"，以及我错在哪

我此前的理由是「Director 包内部 `import comfy`，必须在 ComfyUI 运行时内才能导入」。
**前半句对，推论错了** —— 我一直在 ComfyUI **外面**尝试导入，当然永远失败。
而运行中的 ComfyUI 服务器里这个包**本来就是已加载的**。

### 做法

写一个临时节点（用完即删）放进 `custom_nodes`，重启 ComfyUI，
再用 API 提交一个只含该节点的图，从 `/history` 取回报告。

### 实测输出

```
[契约自检] 包=ComfyUI_MiniMaxH3_Director
           （sys.modules 里有 2 个候选键，已优先取干净包名；
             路径式键是 ComfyUI 对目录型节点的注册方式）
           external_groups 实际文件=...\custom_nodes\
                                    ComfyUI_MiniMaxH3_Director\director\external_groups.py
  [OK] i2v 字段完全一致（11 个键，取值也相符）
  [OK] r2v 字段完全一致（12 个键，取值也相符）
  [OK] 空 r2v 组保护一致（双方都拒绝）

结论：运行时契约比对**全部通过**
```

### 顺带修掉一个**假守卫**

原来的 `_contract_selfcheck` 拿一份**手抄的字段清单**去和真实包比 ——
也就是说，**我的 builder 改坏了它照样绿**。
已改为与 `fusion.make_i2v_group()` / `make_r2v_group()` 的**真实输出**比字段集合
与关键取值（`family`/`kind`/`first_frame` 有无），并加了互相制衡的测试：
注入"字段不同"的假包必须报不一致，注入"完全一致"的假包必须报通过。

### 复现方式

目标机（有 CUDA）上可直接跑：

```bash
cd .comfyui
python ..\scripts\runtime_contract_check.py
```

本机因导演台包 `__init__` 需要 CUDA 而无法独立导入，脚本会如实输出
「未验证」而不是假装通过。

---

## 十二、网页面板的验证边界（2026-10-17 更新）

### 从"完全未验证"推进到"DOM 级已验证"

原来关于面板只有三条检查，**都不能证明面板能跑**：
`node --check` 只证语法、路由 200 只证后端、节点注册与面板无关。

现在用**最小 DOM 替身 + Node ESM 真实执行**（`tests/js/panel_dom_test.mjs`）：

```
[OK] 模块可加载  [OK] 调用了 app.registerExtension  [OK] 扩展有 setup()
[OK] setup() 能跑完  [OK] body 里有节点  [OK] 挂载了带 id 的元素
[OK] 面板没有打印任何错误
结果：9/9 通过
```

并与线上服务器交叉确认：`/extensions/bettercomfy_comfy/js/bettercomfyui_panel.js`
返回 **14381 字节**，与被执行的文件大小一致 —— 是同一份。

### 边界（不夸大）

| 层级 | 状态 |
|---|---|
| 模块加载 / 扩展注册 / `setup()` 执行 / 事件绑定 / 挂载 | ✅ 已验证 |
| 服务器确实在提供这份文件 | ✅ 已验证（字节数一致） |
| **浏览器渲染级**（像素、CSS 生效、布局、事件冒泡） | ❌ **未验证**（本机无 headless 浏览器） |

DOM 级通过 **不等于** 浏览器渲染正确，两者在文档里分开写。

### 一个值得记的教训

替身最初不解析 `innerHTML`，导致 `querySelector` 恒为 null、面板在绑事件时崩溃 ——
**而这时我分不清"面板有 bug"和"替身不够真"**。
修好解析后 9/9，说明面板本身干净。

更关键的是：修好之后 8 项全绿，但 stderr 里仍有一行
`面板初始化失败: insertAdjacentHTML is not a function` ——
**根节点挂上了、断言全绿、后续功能全挂**。
于是补了一条断言「面板没有打印任何错误」。

**"部分成功"最容易被误判为"成功"。**


---

## 十四、三个融合工作流（2026-10-17 更新）

全部由 `build_fusion_workflow.py` **从运行中 ComfyUI 的真实 schema 生成**，
再由 `validate_fusion_workflow.py` 逐条校验。

| 文件 | 拓扑 | 校验 |
|---|---|---|
| `fusion_A_director_r2v.json` | 项目 → 融合组 → **MiniMaxH3Director** | **PASS** 4 OK / 0 FAIL |
| `fusion_B_chain_director.json` | 时间轴文本 → **MiniMaxH3ChainDirector** → 回灌 | **PASS** 6 OK / 0 FAIL |
| `fusion_C_smart_director_full.json` | SmartDirector（AI 分镜 → `MMX_DIR_GROUP`）→ **MiniMaxH3Director**（含模型加载器）→ CreateVideo → SaveVideo | **PASS** 19 OK / 0 FAIL |

### C 是最能直接用的一个

```
UNETLoader(ref2va) ─▶ LoraLoaderModelOnly(turbo 0.75) ─┐
CLIPLoader(qwen3vl, type=minimax) ─────────────────────┤
VAELoader(video vae) / VAELoader(audio vae) ───────────┘
                                                        ▼
MiniMaxH3SmartDirector ──MMX_DIR_GROUP──▶ MiniMaxH3Director ──▶ CreateVideo ──▶ SaveVideo
```

模型文件名按 `scripts/download_models.py` 的清单填好，用户只需改名即可。

### 校验器因此加强了三处（并抓到 2 个真 bug）

**B-1 目标槽位越界**：我照抄了别人的槽编号（7/2/3），节点实际只声明 1~2 个输入。
原校验器只查源槽，**不查目标槽** —— 工作流 B 一直显示 PASS，其实连不上。

**B-2 `outputs[].links` 没记录**：ComfyUI 连线是**双向记录**的
（`wf.links` 一份 + 源节点 `outputs[i].links` 一份）。只写前者，前端画不出线。

加强后的输出逐条带类型：

```
link 1: 2[0](MMX_DIR_GROUP) -> 3.r2v_groups(MMX_DIR_GROUP)
link 6: 20[0](MMX_DIR_GROUP) -> 26.r2v_groups(MMX_DIR_GROUP)
link 8: 26[1](AUDIO) -> 27.audio(AUDIO)
link 10: 27[0](VIDEO) -> 28.video(VIDEO)
总结论：全部 PASS
```

**融合的关键接缝（`MMX_DIR_GROUP`）现在被逐类型验证过**，
不再是"槽位序号看着对"。

离线守卫（`tests/test_fusion_workflow.py`）补了 4 条：
目标槽范围、源槽范围、link 必须带类型、`outputs[].links` 双向记录 ——
不需要 ComfyUI 就能跑。

---

## 十五、把工作流提交给 ComfyUI 验证（2026-10-17 更新）

alidate_fusion_workflow.py 是**我在检查我自己的理解**。
新增 submit_check.py 把 UI 格式转成 **API 格式**提交进 /prompt，
由 ComfyUI 回答图结构是否合法 —— 「输入名写错 / 类型不对 / 必需输入漏了」全被排除。

### 它抓到 4 个真问题

| # | 问题 | 表现 |
|---|---|---|
| 1 | 手维护 link_types 清单，**把 BDGROUP 当成连线类型** | 它真实声明是 ["BDGROUP", {"default": "采样设置"}] —— 是 COMBO 控件。被跳过后该节点**之后所有控件位置前移一格**，widgets_values 全部错位 |
| 2 | 用 len(t) > 3 判断类型名 | "VAE" 长度**正好 3**，条件不成立 → ideo_vae/udio_vae 被当成控件，又一次错位 |
| 3 | opts = {} 写成空字典 | default_for() 拿不到 default，回落成 0.0，报 Value 0.0 smaller than min of 0.2 |
| 4 | 工作流 B 的必需输入没接 | ChainDirector 有 5 个必需连线输入 + image_0（会 KeyError），原来只接了一根时间轴 |

问题 1/2 的实质是**参数静默错位**：不报错、不崩溃，值悄悄填到别的参数上。

### 修法：用结构性证据，不用启发式

分类规则改为：类型是取值列表 → COMBO；是 INT/FLOAT/STRING/BOOLEAN → 控件；
**裸类型名且选项里没有任何 UI 专属键**（default/min/max/step/multiline）→ 连线口。

这个判据对 VAE（3 字符）与 MMX_DIR_GROUP（13 字符）一视同仁。
并且**生成器与校验器共用同一条规则**（有测试逐 schema 比对两边计数）——
两边各算一套的话，校验只会给出假安心。

### 区分「图的问题」与「环境的问题」

修完后只剩 VAE 未下载导致的 alue_not_in_list。这类是**环境问题**，
混报会得出工作流有问题的错误结论。所以加了分类：

| 分类 | 判据 | 含义 |
|---|---|---|
| [FAIL] | 非文件名类错误 | 图/输入不合法，必须修 |
| [ENV] | alue_not_in_list 且收到**带扩展名的文件名** | 权重未就位，不是工作流的错 |

最终：

`
汇总：图结构通过 1 个；真问题 0 个；仅环境问题（权重未就位）2 个
总结论：所有工作流的图结构都被 ComfyUI 接受
`


---

## 十六、导演台高级接缝（2026-10-17 更新）

MiniMaxH3Director 除主接缝（i2v_groups/
2v_groups）外还有 **4 个高级可选输入**，
各由一个专门节点产出对应类型：

| 接缝 | 产出节点 | 输出类型 |
|---|---|---|
| 
efine | MiniMaxH3DirectorRefine | MMX_DIR_REFINE |
| selflift | MiniMaxH3DirectorSelfLift | MMX_DIR_SELFLIFT |
| semantic_bridge | MiniMaxH3DirectorSemanticBridge | MMX_DIR_SEMANTIC_BRIDGE |
| ace_refine | MiniMaxH3DirectorFaceRefine | MMX_DIR_FACE_REFINE |

### 已实测：4/4 接缝**真的能接上**

scripts/probe_advanced_seams.py 用真实节点产出对应类型、接到 Director 上提交：

`
[refine]          图被接受（已入队）
[face_refine]     图被接受（已入队）
[semantic_bridge] 图被接受（已入队）
[selflift]        图被接受（已入队）
`

比读 schema 强一层 —— schema 里有这个输入只说明声明了，
**提交被接受才说明类型真的兼容**。

### 能力检测：按**选项**判断，不能一刀切

任务书第 7 节要求「检测能力并说明不支持项」。这些 COMBO 在缺权重时会退化，
**其中最危险的是显示了一个不存在的名字**：

* latent_upscale_model: ["(将 3D 权重放入 models/latent_upscale_models)"]
* detector: [["face_yolov8m.pt"], ...] ← **磁盘上并没有这个文件**

所以检测必须**查磁盘**。实测本机：

| 接缝 | 状态 | 说明 |
|---|---|---|
| 二采 / 放大（Refine） | ✅ 部分可用 | 
efine/upscale **不需要额外权重**；latent_upscale 需要 3D 放大模型 |
| SelfLift | ❌ | 需要 models/latent_upscale_models/ |
| SemanticBridge | ❌ | 需要 models/semantic_bridge/ |
| FaceRefine | ❌ | 需要 models/ultralytics/bbox/（**不是** detection/） |

### 两个判断错误（自查纠正）

1. **一刀切整个节点**：第一版把 MiniMaxH3DirectorRefine 整节点标成不可用 ——
   但它的 
efine/upscale 两个模式不需要任何额外权重。
   会让用户放弃本来能用的功能。已改为 ree_options + gated_options 的按选项模型。
2. **查错目录**：人脸检测权重在 models/ultralytics/bbox/（节点 tooltip 原文），
   我写成了 models/detection/。**查错目录会得出相反的结论**。

两者的共同点：把看起来差不多当成了就是这样。都补了守卫测试。

### 对主流程无影响

基础出片（t2v/i2v/fl2v/r2v）**不需要**这些权重。
高级接缝是可选增强，缺权重时对应选项不可用，但主流程照常。
