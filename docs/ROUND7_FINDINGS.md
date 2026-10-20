# 第七轮进展记录（2026-10-17 凌晨）

> 目标轮次 5/60。本轮把**我此前反复声明"本机做不到"的最后一项验证做成了**：
> 在真实 ComfyUI 运行时内，用真实 `pack_*` 与**我构造的组**逐字段比对。
> 并且在这个过程中发现原来的检查是个**假守卫**。

---

## 一、我之前的结论是错的

从第一轮起我就在文档里写：

> **真导入 `pack_*` 的运行时比对未在本机完成** —— Director 包内部 `import comfy`，
> 必须在 ComfyUI 运行时内才能导入；本机只做了 AST 静态比对。

前半句对（确实需要 `comfy`），**后半句的推论错了** ——
我一直在 ComfyUI **外面**尝试导入，所以永远失败。
而**运行中的 ComfyUI 服务器里这个包是已加载的**（它的 20 个节点都在注册表里）。
只要把检查放进那个进程，就能做。

### 做法

1. 写一个**临时节点** `__TempRuntimeContractCheck`（用完即删，不是产品功能）；
2. 放进 `custom_nodes`，重启 ComfyUI；
3. 用 API 提交一个只含该节点的图；
4. 从 `/history` 取回报告。

### 实测结果

```
[契约自检] 包=ComfyUI_MiniMaxH3_Director
           （sys.modules 里有 2 个候选键，已优先取干净包名；
             路径式键是 ComfyUI 对目录型节点的注册方式）
           external_groups 实际文件=...\.comfyui\custom_nodes\
                                    ComfyUI_MiniMaxH3_Director\director\external_groups.py
           用真实 pack_* 与**我构造的组**逐字段比对：
  [OK] i2v 字段完全一致（11 个键，取值也相符）
  [OK] r2v 字段完全一致（12 个键，取值也相符）
  [OK] 空 r2v 组保护一致（双方都拒绝）

结论：运行时契约比对**全部通过**
```

**"字段名一致 + 关键取值也相符 + 空组保护一致"三项全过。**
goal 第 4 项从"静态比对"升级为"运行时比对"，`FUSION_REPORT` 里那条未验证项可以划掉了。

---

## 二、过程中发现原来的自检是个**假守卫**

第一版 `_contract_selfcheck` 长这样：

```python
want_i2v = {"version", "family", "kind", ...}      # 手抄的字段清单
real_i2v = set(pack_i2v_group(prompt="x").keys())
assert real_i2v == want_i2v
```

它比对的是「**手抄清单** vs 真实包」。

**问题**：如果我把 `fusion.make_i2v_group()` 改坏了（比如漏了 `first_frame`），
这条检查**依然是绿的** —— 因为手抄清单没变。
它只证明了我"抄得对"，没有证明我"产出对"。

这正是本项目一直在防的那类东西：看起来在验证，实际什么都没验证。

### 修法

改为与**我实际构造的组**比：

```python
real = pack_i2v_group(prompt="x")
mine = fusion.make_i2v_group(prompt="x", duration_sec=5.0)
# 比字段集合，再比 family/kind/first_frame 有没有 等关键取值
```

并补了 `tests/test_runtime_contract_check.py` 里两条**互相制衡**的测试：

* `test_selfcheck_compares_my_builder_not_a_hardcoded_list` ——
  注入一个"字段故意不同"的假包，**必须报 [不一致]**（如果实现是拿手抄清单比，这条会红）；
* `test_selfcheck_passes_with_a_faithful_fake` ——
  注入一个"与我的 builder 完全一致"的假包，**必须报通过**（防止上面那条恒红）。

---

## 三、顺带查清了一个可疑现象

报告里出现路径 `...\Better comfyUI\_x_comfyui\custom_nodes\...`，
而**`_x_comfyui` 这个目录并不存在**。

查证结果：这是 ComfyUI 对**目录型** custom node 的注册方式 ——
把完整路径里的 `.` 替换成 `_x_` 作为 `sys.modules` 键。
所以 `Better comfyUI\.comfyui` 变成 `Better comfyUI_x_comfyui`，
看起来就像有个 `_x_comfyui` 目录。

**佐证**：ChainDirector 自己的 `_autopatch.py` 注释里写了这件事
（它同时处理干净包名和路径式键两种注册方式）。

于是本机 `sys.modules` 里同时存在两个键，都指向同一份源码。

### 但我的查找逻辑当时是非确定性的

原来是「遍历 `sys.modules`，命中就返回」—— **取到哪个键取决于字典顺序**，
报告里时而打印这个、时而那个。

修法：把全部候选都收起来，**优先干净包名**（长度更短、不含 `_x_` 标记），
并在报告里写出候选个数与 `external_groups` 的**实际文件路径**，便于追查加载了哪一份。

---

## 四、验证

| 项 | 结果 |
|---|---|
| **运行时契约比对**（真实 ComfyUI 进程内） | ✅ i2v 11 键 / r2v 12 键 / 空组保护 **全部一致** |
| 新增测试 | 7 条（含互相制衡的假包测试） |
| 全量测试（系统 Python） | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| ComfyUI 实测 | 临时节点已删除（`__TempRuntimeContractCheck` 不在注册表里） |
| 权重下载 | 🔄 **24.27 / 59.38 GB** |

### 附带产出

* `scripts/runtime_contract_check.py` —— 目标机上可直接跑的独立脚本
  （本机因导演台包 `__init__` 需要 CUDA 而无法独立导入，所以脚本会如实报「未验证」；
  在装了 CUDA 的目标机上应当能直接跑通并输出同样结论）。
* 两个反复出现的操作性教训（我自己的）：
  **不要在同一条命令里"停服务 + 起服务"** —— 停的那一步会把自己刚起的进程杀掉。
  本轮又踩了一次。停与起必须分成两次调用。

---

## 五、仍未验证（收窄了一项）

1. **真实 H3 视频出片** —— 需 ≥24GB 显存（本机 4GB，档位 `minimal`）。
   *这一条仍是唯一的硬约束。*
2. ~~真导入 `pack_*` 的运行时字段比对~~ —— ✅ **本轮已完成**。
3. 网页面板在浏览器里的最终渲染。
4. 参考音色链路与导演台高级模式（二采/Guide/SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出；那条"假守卫"是我自己写的，如实记录。_
