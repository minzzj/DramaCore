# ROUND58 · H3 提示词工程精简版（V1.0 / 2026-10）集成

> 目标：把用户提供的《MiniMax H3 提示词工程精简版.docx》（12,890 字）**全文**加入插件，
> 并作为**默认系统提示词**；ROUND56 的《H3 提示词工程手册》完整版（41,966 字）与旧核心章节
> **全部保留、仍可在节点上/面板里选择**。旧版不删、默认改新。

---

## 1. 交付清单（改动文件）

| 类型 | 文件 | 说明 |
| --- | --- | --- |
| 新增（数据） | `bettercomfyui/prompt/data/h3_concise_v1.txt` | 精简版**全文**（12,912 字，UTF-8 无 BOM、LF），含「系统提示词 1：H3 视频生成提示词编译器（Master）」「系统提示词 2：素材图提示词生成器」「提示词片段库」 |
| 修改 | `bettercomfyui/prompt/h3_handbook.py` | 新增 `PRESET_CONCISE="h3_concise_v1"`、`DEFAULT_PRESET`、`PRESETS`、`PRESET_FILES`、`PRESET_LABELS`、`PRESET_ALIASES`、`get_concise_handbook()`、`canonical_preset()`、`is_handbook_preset()`、`preset_label()`、`preset_file()`、`is_concise_preset()`；`resolve_preset()` 支持新预设与别名。**旧 `PRESET_FULL`/`get_full_handbook()`/`PRESET_CORE`/`get_core_handbook()` 原样保留** |
| 修改 | `bettercomfyui_comfy/everything_node.py` | `SYSTEM_PROMPT_CHOICES` 首位新增「H3 提示词精简版（2026-10，推荐）」→ `h3_concise_v1`；原完整版改名「H3 手册（完整，历史版）」仍可选；新增 `SYSTEM_PROMPT_DEFAULT_KEY`、`default_system_prompt_label()`、`system_prompt_key()`、`system_prompt_label()`、`is_system_prompt_preset()`、`resolve_template_choice()`；跟随面板为空时默认精简版 |
| 修改 | `bettercomfyui_comfy/one_stop_node.py` | 「系统提示词选择」默认仍是「跟随面板配置（推荐）」，但面板为空时落到精简版；tooltip 文案写明默认 12,890 字精简版与旧历史版可选；面板值判定改用 `is_system_prompt_preset()`（旧标签不再被当成"自定义全文"）；跟随分支审计写清"面板未保存 → 默认精简版" |
| 修改 | `bettercomfyui_comfy/web/js/dramacore_director_ui.js` | 仅「⑭ 提示词」面板：默认加载精简版全文；新增「加载 H3 精简版（推荐）/加载历史完整版/加载核心章节」三个按钮；`loadPromptPanel()` 读对 `defaults.system_prompt` 层级；保存预置时存**预设键**（避开 8000 字符上限）；来源/字数/数据文件状态行；文案全部更新 |
| 新增（测试） | `tests/test_round58_concise_prompt.py` | 12 条单测：数据文件全文/预设解析/旧版保留/别名/选项顺序/节点默认/跟随面板默认/UI 契约/UI 别名与服务端一致性 |
| 新增（文档） | `docs/ROUND58_H3提示词精简版集成.md` | 本文 |

> 未改动（按分工属于其它专项）：`bettercomfyui/prompt/script_build.py`、`validate.py`、`inject.py`、
> `gateway.py`、`config/default.yaml`、`bettercomfyui/hardware.py`、`bettercomfyui_comfy/routes.py`、`install/*`。

---

## 2. 数据来源与提取

* 源文件：仓库根 `MiniMax H3 提示词工程精简版.docx`（Word 统计 12,890 字）。
* 提取方式：解析 `word/document.xml`，按文档顺序取段落（含标题/代码块/列表）+ 表格行（单元格用 ` | ` 连接），
  空行折叠为最多一个空行；不用 python-docx 之外的黑盒转换，保证不丢内容。
* 结果：**12,912 字**（含目录与标题），`> 12,000` 达标；无 BOM、LF 换行，`git diff` 稳定。
* 校验关键字：「H3 视频生成提示词编译器」「系统提示词 2」「提示词片段库」「输出前自检清单」
  「integrated_multimodal_description」「non_diegetic_music」「negative_prompt」「口播专用补丁」「短剧钩子」。

---

## 3. 预设 API（`bettercomfyui/prompt/h3_handbook.py`）

| 键 | 含义 | 数据文件 | 大约字数（含前后缀） |
| --- | --- | --- | --- |
| `h3_concise_v1` | **默认**：精简版 V1.0（2026-10）全文 | `data/h3_concise_v1.txt` | 13,494 |
| `h3_handbook` | 历史完整版（ROUND56 默认，仍可选） | `data/h3_handbook_full.txt` | 42,263 |
| `h3_core` | 旧核心章节（语法/5.2/6.10/11） | `data/h3_handbook_full.txt` | 6,664 |

```python
from bettercomfyui.prompt import h3_handbook as h

h.PRESET_CONCISE            # "h3_concise_v1"
h.DEFAULT_PRESET            # "h3_concise_v1"
h.PRESETS                   # ("h3_concise_v1", "h3_handbook", "h3_core")
h.canonical_preset("concise")          # "h3_concise_v1"（别名收口）
h.resolve_preset("h3_concise_v1")      # 全文（12,912 字正文一字不少 + 前后缀约束）
h.preset_chars("h3_concise_v1")        # 13,494
h.preset_label("h3_concise_v1")        # "H3 提示词精简版（2026-10，V1.0，默认）"
h.preset_file("h3_concise_v1")         # "data/h3_concise_v1.txt"
h.resolve_preset("unknown")            # None（调用方走其它预设）
```

* 精简版只在正文外套两段**约束前言/落地约束**（严格 JSON、英文正文、可见文字双引号、
  不生成字幕/水印、素材图必须带 negative_prompt）；正文本身不做任何删改。
* 数据文件缺失时 `get_concise_handbook()` 回退历史完整版（绝不返回空提示词）；
  调用方仍可用 `len(text)` + `preset_file()` 判断数据是否真的加载。

---

## 4. 默认提示词选择行为（完整矩阵）

`everything_node` 侧（`resolve_template_choice(label, custom, panel_value)`，可单测）：

| 节点「系统提示词选择」 | 自定义系统提示词 | 面板 `prompt.system_prompt_template` | 实际使用 |
| --- | --- | --- | --- |
| 跟随面板配置（推荐） | 空 | 空 | **`h3_concise_v1`（精简版全文）** |
| 跟随面板配置（推荐） | 空 | `h3_concise_v1` / `concise` | `h3_concise_v1` |
| 跟随面板配置（推荐） | 空 | `h3_handbook` / `H3 手册（完整，推荐）`（旧标签） | `h3_handbook`（历史完整版） |
| 跟随面板配置（推荐） | 空 | 一整段自定义提示词 | 原样使用该自定义全文 |
| 跟随面板配置（推荐） | 非空 | 任意 | 自定义全文优先 |
| H3 提示词精简版（2026-10，推荐） | 空 | 任意 | `h3_concise_v1` |
| H3 手册（完整，历史版） | 空 | 任意 | `h3_handbook` |
| H3 手册（精简） | 空 | 任意 | `h3_core` |
| 其它旧模板（严格 JSON…） | 空 | 任意 | 对应 `SYSTEM_PROMPT_PRESETS` 键 |

* `SYSTEM_PROMPT_CHOICES` 顺序（首项 = 默认）：精简版 → 完整历史版 → 旧精简 → 默认/严格 JSON/中文影视剧/按镜头/本地简化。
* ROUND56 及更早保存的工作流里存的是**旧标签**（`H3 手册（完整，推荐）`），
  `LEGACY_SYSTEM_PROMPT_LABELS` + `system_prompt_key()` 保证仍解析成 `h3_handbook`，
  不会静默掉到 `default`。
* 面板值若是旧标签，`resolve_template_choice()` 会先收口成预设键，避免被当成"自定义全文"。

---

## 5. 面板（网页 UI）行为

* 打开「⑭ 提示词」：面板没保存过默认提示词 → **自动加载精简版全文（12,912 字）**；
  下方状态行显示 `来源：H3 提示词精简版 V1.0（2026-10，12,890 字） · N 字 · key=h3_concise_v1 · 文件 …`。
* 按钮：
  * 「加载 H3 精简版（推荐）」→ `mode=concise`
  * 「加载历史完整版」→ `mode=full`（41,966 字旧手册）
  * 「加载核心章节」→ `mode=core`（旧精简）
* 「保存为面板默认」：
  * 载入的是预置且未手改 → 保存**预设键**（如 `h3_concise_v1`）：文档更新自动跟随，且避开
    `POST /bettercomfyui/panel/config` 的 **8000 字符**上限（12,890 字全文存不进去，这是既有服务端限制）；
  * 手动改过 → 保存自定义全文（>8000 字符时前端先拦下并提示）。
* 面板值本身是预设键/旧标签时，UI 也会把它解析成全文展示（`_presetModeOf`）。
* mode 别名按顺序探测：`concise → h3_concise_v1 → concise_v1 → 精简版`，以服务端返回的 `key`
  判定是否命中；未命中时**不冒充**精简版，状态行如实写"实际取到 h3_handbook，需服务端路由把
  mode=concise 映射到 h3_concise_v1"。

---

## 6. 验收证据

```
python -B -m pytest tests/test_round56_features.py tests/test_round58_concise_prompt.py -q
..................                                                       [100%]      # 18 passed

python -B -m pytest tests/test_round55_workflow_alignment.py tests/test_round53_fixes.py \
    tests/test_prompt_store_integration.py tests/test_round55_prompts.py -q
....................................                                       [100%]      # 36 passed

python -B -m pytest tests/verify/test_acceptance_31_one_node.py tests/test_audit_r2_fixes.py -q
...............................                                          [100%]      # 31 passed
[verify] 动态评估：通过=18 失败=0 未验证=0

python -B -m py_compile bettercomfyui/prompt/h3_handbook.py \
    bettercomfyui_comfy/everything_node.py bettercomfyui_comfy/one_stop_node.py   # OK
node --check bettercomfyui_comfy/web/js/dramacore_director_ui.js                  # OK
```

数据核对：`h3_concise_v1.txt` = **12,912 字**（>12,000）、21,082 字节、无 BOM、无 CRLF；
`resolve_preset("h3_concise_v1")` = 13,494 字（正文全文 + 前言/落地约束）。

全量回归（本机，`pytest.ini` 自带 `-q`，故用 `-o addopts=` 取统计）：

```
python -B -m pytest tests --ignore=tests/verify -q -o addopts="--strict-markers --tb=no"
1 failed, 1357 passed, 17 skipped in 75.43s

python -B -m pytest tests/verify -q -o addopts="--strict-markers --tb=line"
2 failed, 296 passed, 1 skipped in 106.06s
```

三个失败**均与本轮改动无关**（已逐条定位）：

| 失败用例 | 原因 |
| --- | --- |
| `tests/test_foundation.py::test_dumped_default_config_is_reproducible` | 其它专项正在改 `config/default.yaml`（文件 mtime 19:51:30，本轮未写该文件）：yaml 里新增了注释，和 `dump_config(Config())` 序列化结果不再逐字一致 |
| `tests/verify/...test_acceptance_28_install_and_workflows.py::test_p1_plugin_installed_with_identical_hashes` | 运行态 `.comfyui/custom_nodes/*` 是 ROUND56 快照，与开发态本就漂移（本轮新增 3 个文件差异）；需 Lead 在合并后跑 `scripts/sync_to_comfyui.py` |
| `tests/verify/...test_acceptance_35_staged_vram.py::test_p4_staged_run_dry_run_cli` | `scripts/staged_run.py` 子进程输出在 Windows 控制台的编码乱码（`拆分` 被解成 `\ufffd`），环境编码问题，与本轮无关 |


---

## 7. 已知依赖与未验证点

1. **`routes.py` 的 mode 映射（其它专项）**：`h_prompt_handbook` 目前是
   `key = PRESET_CORE if mode in ("core","h3_core","精简") else PRESET_FULL`，
   即 `mode=concise` 会返回**旧完整版**。需要 routes 归属专项补一支（本轮未改 routes.py）：
   ```python
   elif mode in ("concise", "h3_concise_v1", "concise_v1", "精简版", "lean"):
       key = h3_handbook.PRESET_CONCISE
   ```
   UI 已按上述别名探测，routes 补上后**无需再改前端**；未补之前，面板会退到历史完整版并在状态行如实提示。
2. **部署态仍是旧代码（本轮未部署、未重启）**：共享服务（127.0.0.1:8188）跑的是
   `.comfyui/custom_nodes/bettercomfyui{,_comfy}`（ROUND56 快照，`prompt/data/` 里只有
   `h3_handbook_full.txt`）。本轮按约束**没有**跑 `scripts/sync_to_comfyui.py`、**没有**重启服务——
   部署与重启属于 Lead/其它专项。实测 `GET /bettercomfyui/prompt/handbook?mode=concise`
   仍返回 `{"key":"h3_handbook","chars":42263}`（旧进程 + 旧代码），
   因此「面板默认加载 12,890 字精简版」**只做到代码与单测级验证，未在真实 UI 里点开验证**。
   部署注意：先同步文件，再重启 ComfyUI（Python 模块有 `sys.modules` 缓存，热改 `h3_handbook.py`
   不会生效；前端 JS 只需浏览器刷新）。
3. **`POST /panel/config` 的 8000 字符上限**（routes.py，既有行为）：12,890 字全文无法整段存为用户默认，
   因此面板保存走"预设键"；如需保存整段长文需 routes 侧放宽上限（不在本轮范围）。
4. **未做真实出片验证**：本轮未调用真实模型，未验证 12.9k 精简版作为系统提示词的实际出片效果
   （建议下一轮用同一剧本 A/B：`h3_concise_v1` vs `h3_handbook`，比较字段完整率与失败率）。
5. **ComfyUI 工作流兼容**：旧工作流里保存的旧标签已做兼容映射，但**未在真实 ComfyUI 前端里加载旧工作流实测**。

---

## 8. 回滚方式

* 只想换默认：把 `h3_handbook.DEFAULT_PRESET` 与 `everything_node.SYSTEM_PROMPT_DEFAULT_KEY`
  改回 `"h3_handbook"`，并把 `SYSTEM_PROMPT_CHOICES` 里旧完整版调回首位（一条改动即可）。
* 想彻底移除精简版：删 `data/h3_concise_v1.txt` + 本模块新增函数即可；`get_concise_handbook()`
  在文件缺失时会自动回退完整版，不会出现空提示词。
