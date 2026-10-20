# 第十轮进展记录（2026-10-17 凌晨）

> 目标轮次 8/60。本轮把融合工作流从 **2 个补到 3 个**，
> 其中新增的 C 是**最能直接用**的一个（SmartDirector → Director 全链路，
> 含模型加载器与融合接缝）。过程中校验器抓到 2 个真 bug。

---

## 一、为什么还需要一个工作流 C

原有两个工作流都有明显短板：

| 工作流 | 短板 |
|---|---|
| A（项目 → 融合组 → Director） | Director 的 5 个必需输入（model/vae×2/clip）**没接**，用户要自己找 |
| B（时间轴 → ChainDirector → 回灌） | 不需要我的编排层，体现不出融合价值 |

缺的是**「拉进来就能跑，且体现融合」**的那个：SmartDirector 出 AI 分镜 →
`MMX_DIR_GROUP` → Director 执行引擎。这正是 goal 第 1 项描述的接缝。

### C 的结构（9 节点）

```
UNETLoader(ref2va) ──▶ LoraLoaderModelOnly(turbo 0.75) ──┐
CLIPLoader(qwen3vl, type=minimax) ───────────────────────┤
VAELoader(video vae) ────────────────────────────────────┤
VAELoader(audio vae) ────────────────────────────────────┤
                                                          ▼
MiniMaxH3SmartDirector ──MMX_DIR_GROUP──▶ MiniMaxH3Director ──▶ CreateVideo ──▶ SaveVideo
      （AI 分镜 + 导演组）                    （真实执行引擎）
```

**链路 6 就是融合接缝**：`SmartDirector[0](MMX_DIR_GROUP) -> Director.r2v_groups(MMX_DIR_GROUP)`。

模型文件名按 `scripts/download_models.py` 的清单填好，用户只需换成本机实际文件名。

---

## 二、校验器抓到 2 个真 bug（都出自我手写的工作流 JSON）

### B-1 目标槽位越界（工作流 B）

我照抄了别人的目标槽编号 `7 / 2 / 3`，而那两个节点实际只声明了 1~2 个输入：

```
[FAIL] link 1 目标槽 7 越界（节点 11 只声明了 1 个输入）
[FAIL] link 2 目标槽 2 越界（节点 12 只声明了 2 个输入）
[FAIL] link 3 目标槽 3 越界（节点 12 只声明了 2 个输入）
```

**为什么会漏到现在**：原校验器只查「源槽越界」和「控件数」，
**没查目标槽**。工作流 B 一直是"PASS"，其实连不上。

### B-2 源节点的 `outputs[].links` 没记录（前端画不出线）

ComfyUI 的连线是**双向记录**的：`wf.links` 记一份，
源节点 `outputs[i].links` 也要记一份。我只写了前者。

这条是新加的离线测试抓到的：

```
[FAIL] link 2 未记录在节点 11 的 outputs[0].links 里（实际 []）
       —— 连线会显示不出来
```

### 校验器因此加强了三处

1. **目标槽越界**检查；
2. **类型匹配**检查 —— 把 `IMAGE` 接到 `MMX_DIR_GROUP` 口上、
   槽位序号都在范围内也会被拒绝执行。现在会比对源输出类型与目标输入类型；
3. **目标输入名必须真实存在**（对控件口尤其重要）。

加强后的输出：

```
link 1: 2[0](MMX_DIR_GROUP) -> 3.r2v_groups(MMX_DIR_GROUP)
link 6: 20[0](MMX_DIR_GROUP) -> 26.r2v_groups(MMX_DIR_GROUP)
link 8: 26[1](AUDIO) -> 27.audio(AUDIO)
link 10: 27[0](VIDEO) -> 28.video(VIDEO)
总结论：全部 PASS
```

**融合的关键接缝现在被逐类型验证过了**，不再是"槽位序号看着对"。

---

## 三、离线测试补了 4 条（不需要 ComfyUI）

`tests/test_fusion_workflow.py` 新增：

| 测试 | 覆盖 |
|---|---|
| `test_link_target_slot_is_within_declared_inputs` | 目标槽必须 < 该节点 `inputs` 长度（B-1 的守卫） |
| `test_link_source_slot_is_within_declared_outputs` | 源槽必须在范围内 |
| `test_each_link_has_five_elements_and_a_type` | link 必须带类型（否则无法做类型匹配） |
| `test_output_slot_links_are_recorded` | 双向记录（B-2 的守卫） |

这四条**离线可查**，所以进 pytest；
需要真实 schema 的类型匹配检查留在 `validate_fusion_workflow.py`。

---

## 四、验证汇总

| 项 | 结果 |
|---|---|
| 工作流校验（对运行中 ComfyUI 的真实 schema） | **3/3 PASS**：A 4 OK、B 6 OK、**C 19 OK**，0 FAIL |
| 工作流结构测试（离线） | 24 条全过 |
| 全量测试（系统 Python） | **EXIT=0** |
| 全量测试（ComfyUI venv） | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| 权重下载 | 🔄 扩散模型 2/2、文本编码器 1/1、**VAE 仍缺** |

> `probe_model_kinds` 显示 `missing: ['VAE']` —— 两个 VAE 文件（4.85 GB + 0.56 GB）
> 仍在下载中。三个类别里只差最后一个。

---

## 五、仍未验证

1. **真实 H3 视频出片** —— 需 ≥24GB 显存（本机 4GB）。权重快齐了，仍缺 GPU。
2. 浏览器**渲染级**（DOM 级已过，本机无 headless 浏览器）。
3. 导演台高级模式（二采 / Guide / SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出；2 个 bug 都是我自己写的工作流 JSON 里的，如实记录。_
