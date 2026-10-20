# 第十一轮进展记录（2026-10-17 凌晨）

> 目标轮次 9/60。本轮做了一件此前没做过的事：**把生成的工作流真的提交给 ComfyUI**，
> 让 ComfyUI 自己判断图结构对不对 —— 于是暴露出 4 个真问题，
> 其中 2 个是「参数静默错位」这类最难查的类型。

---

## 一、为什么还要多这一层校验

`validate_fusion_workflow.py` 已经查了控件数、源/目标槽范围、类型匹配、
目标输入名存在性。但它终究是**我在检查我自己的理解**。

新增 `submit_check.py`：把 UI 格式的工作流转成 **API 格式**
（`/prompt` 实际吃的形状）**提交进去**，由 ComfyUI 回答：

* 图结构是否合法；
* 输入名与类型是否被真实节点接受；
* 必需输入有没有漏。

即使因为缺权重导致执行失败，只要 `/prompt` 不返回 `node_errors`，
就说明**图本身是好的** —— 「输入名写错 / 类型不对 / 必需输入漏了」这一类全部排除。

---

## 二、ComfyUI 抓到的 4 个真问题

### 问题 1（最严重）：控件位置整体错位

```
节点 20 (MiniMaxH3SmartDirector):
  Value 0.0 smaller than min of 0.2   (输入: 默认时长)
节点 26 (MiniMaxH3Director):
  Required input is missing: bd_grp_sample
```

**根因**：我手维护了一个 `link_types` 清单，其中**误把 `BDGROUP` 当成连线类型**。
而它真实声明是：

```json
"bd_grp_sample": ["BDGROUP", {"default": "采样设置"}]
```

—— 这是**取值列表**，即 **COMBO 控件**。

被跳过之后，该节点**之后的所有控件位置整体前移一格**，`widgets_values` 全部错位。
`默认时长` 拿到 0.0（低于 min 0.2），`bd_grp_sample` 则彻底没被赋值。

> 这就是「参数静默错位」：不报错、不崩溃，值悄悄填到别的参数上。

### 问题 2：`len(t) > 3` 这个长度启发式

改成结构化判断后仍失败，查下去发现我又用了个脆弱的启发式：

```python
elif isinstance(t, str) and t.isupper() and len(t) > 3:
```

`"VAE"` 长度**正好是 3**，条件不成立 → `video_vae` / `audio_vae`
被当成控件塞进 `widgets_values` → 又一次错位。

**改成结构性证据**：看选项里**有没有 UI 专属键**
（`default` / `min` / `max` / `step` / `multiline`）。
纯连线口不会有这些键。这个判据对 `VAE`（3 字符）与 `MMX_DIR_GROUP`（13 字符）
一视同仁。

### 问题 3：`opts` 被写成空字典，default 全部丢失

```python
opts = {}          # ❌ 第一版
```

于是 `default_for()` 永远拿不到 `default`，回落到
`FLOAT -> 0.0` / `BOOLEAN -> False`，正是问题 1 里
`Value 0.0 smaller than min of 0.2` 的直接来源。

### 问题 4：工作流 B 的必需输入根本没接

ChainDirector 有 5 个**必需连线输入**（`model_r2v` / `model_i2v` /
`video_vae` / `audio_vae` / `clip`），`image_0` 也是必需的
（它直接 `kw["image_0"]`，缺了会 KeyError）。

工作流 B 原来只接了一根时间轴 —— ComfyUI 报
`required_input_missing: clip / model_i2v / ...`。
已补上 2 个 UNETLoader + CLIPLoader + 2 个 VAELoader + LoadImage（共 9 节点）。

---

## 三、校验器与生成器必须**同规则**

问题 1、2 的根源都是「分类规则」。
所以我把**同一条规则**写进了两个地方，并加了一条测试直接比对：

```python
for s in [一批真实形状的 schema]:
    assert len(widget_slots(s)) == V.widget_count(s)
```

两边各算一套的话，校验只会给出**假安心** —— 生成错了、校验也按同样错法点头。

---

## 四、区分「图的问题」与「环境的问题」

修完前三个问题后，剩下的报错只有：

```
节点 24 (VAELoader) vae_name: 'minimax_h3_video_vae_fp16.safetensors' not in ['pixel_space']
```

这是**环境问题**：VAE 权重还在下载，ComfyUI 的清单里只有 `pixel_space`。

如果把这类报错和真正的图结构错误混在一起报，就会得出
"工作流有问题"的**错误结论**。所以 `submit_check.py` 里加了 `classify()`：

| 分类 | 判据 | 含义 |
|---|---|---|
| `[FAIL]` | 非文件名类错误 | 图/输入不合法，**必须修** |
| `[ENV]` | `value_not_in_list` 且收到的是**带扩展名的文件名** | 权重未就位，**不是工作流的错** |

最终输出：

```
汇总：图结构通过 1 个；真问题 0 个；仅环境问题（权重未就位）2 个
总结论：所有工作流的图结构都被 ComfyUI 接受
```

---

## 五、验证汇总

| 项 | 结果 |
|---|---|
| **图结构被 ComfyUI 接受** | ✅ 3/3（真问题 0 个） |
| 工作流校验（对真实 schema） | **3/3 PASS**：A 4 OK、B 18 OK、C 19 OK，0 FAIL |
| 控件分类测试（新增） | 18 条 |
| 全量测试（系统 Python） | **EXIT=0** |
| 全量测试（ComfyUI venv） | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / 0 未验证 |
| 权重下载 | 🔄 扩散模型 2/2、文本编码器 1/1、**VAE 仍缺** |

### 新增的测试（`tests/test_workflow_widget_rules.py`，18 条）

* **事故 1 守卫**：`["BDGROUP", {"default": ...}]` 是控件而非连线口；
* **事故 2 守卫**：`"VAE"` 等短类型名是连线口（参数化 7 种类型，跨 3~13 字符）；
* 连线口夹在控件之间时**控件位置不被挤偏**（两次事故的实质）；
* `default` 必须从 opts 取到，不能回落成 0；
* **生成器与校验器同规则**（逐 schema 比对两边计数）。

> 写最后那条时又踩了一个小坑：`inspect.getsource` 拿到的文本里
> docstring 正好写着"早期用了 `len(t) > 3`"，直接 grep 会**误报**。
> 已改为先剥离 docstring 再扫（用 AST 取 `end_lineno`）。

---

## 六、仍未验证

1. **真实 H3 视频出片** —— 需 ≥24GB 显存（本机 4GB）。权重只差 VAE。
2. 浏览器**渲染级**（DOM 级已过，本机无 headless 浏览器）。
3. 导演台高级模式（二采 / Guide / SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出；4 个问题都是我自己写的工作流生成器里的，如实记录。_
