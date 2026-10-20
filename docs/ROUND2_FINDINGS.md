# 第二轮进展记录（2026-10-17 凌晨）

> 目标轮次 1/60。本轮按目标要求**重新研读三份文档**，
> 聚焦《AI Agent 开发任务书》第 9 节 15 条验收清单与第 10 节交付要求，
> 逐条核对实现与证据，找出并修掉不一致。

---

## 一、本轮发现并修掉的 5 个问题

全部属于「**不报错但会误导人**」这一类 —— 比崩溃更危险。

### V-1 成片 metadata 无法区分 mock 与真实（验收 15 / 任务书第 9 节末句）

**问题**：`metadata.json` 里没有 `used_mock` / `unverified` 字段。
成片 mp4 在磁盘上**无法**区分「模型生成」与「占位媒体」；来源只打印在控制台，
下一个人拿到 mp4 + metadata 就会误当真实产出。

**修复**（`pipeline/core.py::write_output`）：

1. `generation_provenance` 为空 → **拒绝写出 metadata**；
2. 缺 `is_mock` → 拒绝写出；
3. `is_mock=True` 时自动加人类可读 `warning`；
4. 顶层加 `is_mock_output` 布尔。

**实测产物**：

```json
"is_mock_output": true,
"generation_provenance": {
  "is_mock": true,
  "executor": "placeholder-ffmpeg",
  "reason": "本机 ComfyUI 0.41.0；models/ 无任何模型权重，真实生成不可用；...",
  "unverified": ["真实视频模型生成（占位媒体由 ffmpeg 合成，非模型输出）", ...],
  "warning": "⚠️ 本成片由占位（mock）路径产出，**不是**真实模型生成结果。..."
}
```

### V-2 硬编码 ComfyUI 版本 → 同一台机器两个答案

`executors/comfyui_http.py` 写死 `LOCAL_COMFYUI_VERSION = "0.22.3"`，
`demo.py` 的 `real_generation_reason` 也写死同一字符串。
而开发环境早已是 **0.41.0**（还装了三个导演台包）。

**后果**：`doctor` 报 0.41.0、`demo` 报 0.22.3 —— 用户不知道该信哪个。
更糟的是 demo 的理由说「版本低于 0.30」而事实并非如此，会把人引向错误的排查方向。

**修复**：删常量，改 `read_comfyui_version(base_path)` 现场读 `comfyui_version.py`；
读不到返回 `None` 并如实说"未知"。

### V-3 版本比较用字符串 → `"0.5" >= "0.30"` 判 True

`cli.py` 原本写 `(comfy["version"] or "0.0") >= "0.30"`。
字符串语义下 `'5' > '3'`，所以 0.5 被错判为满足 0.30 下限。

**第二层坑**（修好数字比较后才暴露）：`(0, 30) >= (0, 30, 0)` 在 Python 里是 **False**
（前缀相等时短元组更小）→ `meets_min_comfyui("0.30")` 返回 False，
**一个版本不满足它自己的下限**。

**修复**：`_version_tuple(text, width=4)` 定长补齐；`None`/空串一律 False。

### V-4 配置指向错误实例（0.22.3 而非 0.41.0）

`config/user.yaml` 的 `comfyui_base_path` 指向一个 **0.22.3** 的旧安装，
而本机实际用于开发的是我装好的 **0.41.0**（含三个导演台包）。
`comfyui_endpoint` 也指向 `:8000` 而非实际的 `:8188`。

**修复**：改为 `.comfyui`（0.41.0）与 `http://127.0.0.1:8188`。
现在 `doctor` 正确报告版本 0.41.0、四个导演台包、1024 个节点类型。

### V-5 验收 14 的正向路径此前**没有**被验证

`tests/verify/test_acceptance_14_third_party_nodes.py` 用 `http://127.0.0.1:1`
（必然拒绝连接）证明「连不上时明确报错」—— 这**很重要但不够**。

它证明不了「节点装了之后真的能被认出来」。而且那条用例里 `nodes_available=False`
**不能**推出「节点没装」，只能推出「那个 dead URL 上没有节点」。

**补测**（`tests/test_director_nodes_live.py`，直连 127.0.0.1:8188）：

| 断言 | 实测 |
|---|---|
| `nodes_available is True` | ✅ |
| 三个指定导演台的候选节点都能找到 | ✅ |
| `require_nodes()` 放行 | ✅ |
| 版本从实例现场读到并与 `/system_stats` 一致 | ✅ 0.41.0 |
| **节点可用 ≠ 能力已验证**：`verified` 仍为 False | ✅ |

最后一条是关键区分：本机节点齐全但无权重，`verified` 必须是 False。
把「节点在」当成「能出片」就是假成功。

---

## 二、一个附带发现：`DIRECTOR_NODE_MAP` 有第 4 组

`executors/director_nodes.py` 的映射里有 **4** 组：

```
chain_director        -> MiniMaxH3ChainDirector / EN
aimixer_director      -> MiniMaxH3Director / ComfyMiniMaxH3Director
smart_director        -> MiniMaxH3SmartDirector / AllInOne / SmartPlanInspector
h3_chinese_director   -> H3PromptSidecar / H3ChineseDirector     ← 第 4 个包
```

第 4 组指向 **`H3-Chinese-Director`**（181 节点 + 8 个外部依赖的完整生产线），
**不在用户指定的三个包之内**，本机未安装。

已在 `tests/test_director_nodes_live.py` 里把前三组标为**必需**、
第 4 组标为**可选并报告实际安装状态**（当前 `False`），
避免出现「以为装了其实没装」。

---

## 三、本轮新增的守卫

| 文件 | 覆盖 |
|---|---|
| `tests/test_consistency.py` | 21 条：禁止硬编码版本（AST 扫描）、版本数字比较 9 例（含两个字符串比较会错判的用例）、定长元组、metadata mock 标记 4 条 + 反向断言 |
| `tests/test_director_nodes_live.py` | 4 条：对运行中 ComfyUI 的正向路径（无服务时 skip 并标未验证） |

**`test_consistency.py` 用 AST 而不是正则扫文本**：第一版用正则，
结果把 docstring 里提到的历史版本号也当成了硬编码（假阳性）。
用 AST 只看真实赋值语句，既不漏也不误报。

---

## 四、当前状态

| 项 | 结果 |
|---|---|
| 全量测试 | **EXIT=0** |
| 独立验收 | 92 通过 / 0 失败 / **0 未验证** |
| 15 条验收清单 | 15/15（14 号现同时有反向+正向证据） |
| 第一阶段覆盖盘点 | 73 条：DONE 64 / DELEGATED 13 / TODO 0 |
| 实测 ComfyUI | **0.41.0**，四个导演台包，1024 节点类型，`MiniMaxH3*` 20 个 |
| 我的节点 | 14 个 |
| 真实生成 | ❌ 无权重（`models/` 0 个权重文件） |

---

## 五、仍未验证（如实列出，未变）

1. **端到端真实出片** —— 需约 60GB 权重 + ≥24GB 显存（实测峰值 22,016 MiB）。
   本机 GTX 1050 Ti 4GB，档位 `minimal`。
2. 真导入 `pack_*` 的运行时字段比对（需完整 ComfyUI 运行时内执行）。
3. 网页面板在浏览器里的最终渲染（已验证 JS 可解析 + 路由 200 + 节点注册）。
4. 参考音色链路与导演台高级模式（二采/Guide/SelfLift）接入编排层。

---

_本轮所有 ✅ 均来自实际执行的命令输出。_
