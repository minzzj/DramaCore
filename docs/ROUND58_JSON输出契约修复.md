# ROUND58 JSON 输出契约修复（DeepSeek 系统提示词 ↔ 解析器）

- 日期：2026-10-20
- 专项：提示词 / JSON 配置与 DeepSeek 输出识别（上下文隔离子智能体）
- 写作用域：`bettercomfyui/prompt/{script_build,validate,inject,normalize,gateway}.py`、
  `config/default.yaml`、`tests/`、本报告
- 未触碰：`h3_handbook.py`、`bettercomfyui/prompt/data/*`、`everything_node.py`、
  `one_stop_node.py`、UI JS、`routes.py`、`hardware*` / `install*`、`config/user.yaml`
- 未调用真实外部 API，未发布 GitHub，未重启共享 ComfyUI

---

## 0. 结论（先看这 5 行）

1. **根因是两套 JSON 契约打架**：H3 手册（完整版 5.2 与新增的精简版 docx）要求
   `task_type / video_prompt / material_image_prompts / character_sheet`，而解析器只认
   `title / chapters / assets`；`everything_node` 默认又把 H3 手册当系统提示词，
   所以 DeepSeek「按提示词正确输出」后解析器必然报 `$.chapters missing_field`，
   `material_image_prompts` 里的四类素材图提示词与 `video_prompt` 全部丢失。
2. 现在系统提示词末尾追加**带优先级的【输出契约】**，明确「本段优先于上文任何 JSON 格式要求」，
   并禁止顶层使用手册结构；手册正文仍保留（语法/对白/自检），不再抢输出格式。
3. 解析器增加**形状适配层** `adapt_script_output()`：H3 手册结构、外层封装
   （`data/result/script`）、中文键、双重编码、旧 `type` 形状都能显式转换，
   **转换说明写进 warnings → 结果 meta["notes"]**，不是静默吞错。
4. `custom` provider 的 JSON 配置（`custom_request_template` / `custom_response_path`）
   已验证能被 `build_gateway` 正确使用；收尾时 `models/entities.py` 与
   `config/default.yaml` 也已同步为推荐默认值（并行集成落盘，本专项最终交付未包含对这两个文件的改动），
   `test_dumped_default_config_is_reproducible` 与新增契约测试均通过。
5. 新增 23 条契约测试，三类真实输出（纯 JSON / json 代码围栏 / 前后说明文字）全部解析成功；
   相关回归子集 **306 tests / 0 failed / 3 skipped**；`py_compile` 通过。
   全量测试结果见第 4 节（含一条与本专项无关的并行安装漂移说明）。

---

## 1. 根因（按影响排序）

### 1.1 P0：系统提示词要求的 JSON 与解析器不一致

证据链（实际运行）：

- `bettercomfyui/prompt/h3_handbook.py` 把
  `docs/ROUND55_H3提示词工程手册_纯文本.txt` 全文作为系统提示词；
  手册 **5.2 节【输出格式】** 明确要求：

  ```json
  {
    "task_type": "T2VA | I2VA | FL2VA | L2VA | Ref2VA",
    "video_prompt": "最终 H3 提示词全文",
    "material_image_prompts": [{"type": "人物图/场景图/道具图/分镜图", "prompt": "...", "negative_prompt": "..."}],
    "character_sheet": [{"speaker_id": "S1", "name": "角色名", "appearance": "...", "costume": "...", "voice": "..."}],
    "notes": "..."
  }
  ```

- 新精简版 `MiniMax H3 提示词工程精简版.docx` 的【输出格式】是**同一套结构**
  （另一专项正在提取，本专项未修改其文件）。
- `bettercomfyui_comfy/everything_node.py` 在「跟随面板配置」且面板为空时，
  会把 `cfg.prompt.system_prompt_template` 设为 `"h3_handbook"`（约 541/551-553 行），
  于是 DeepSeek 看到的是手册输出格式。
- `validate_script()` 只接受 `title / chapters / assets`，实测报错：

  ```
  $.chapters [missing_field] 缺少必填字段 chapters
  ```

  手册结构里的 `video_prompt`、`material_image_prompts`、`character_sheet`
  全部不在规范结构里 → 即使解析成功也拿不到素材与视频段。

**这不是模型不听话，而是我们给了模型两套互相冲突的契约。**

### 1.2 P1：输出规则本身不完整

旧 `_OUTPUT_RULES_JSON` 只有三行字段说明：

- 没有声明「覆盖上文的 JSON 格式」；
- 没有禁止 H3 手册顶层结构；
- 没有输出前自检（`json.loads`、数组类型、必填字段）；
- 本地模型分支只在 `output_format == "json"` 时追加骨架，**`auto` 时什么都不加**
  （真实节点调用的是 `auto`，见 `everything_node.py` 第 575/581 行附近）。

### 1.3 P1：配置短代码直接进了提示词

`config/default.yaml` 里是规范短代码：

```yaml
output_language: zh
story_granularity: shot
```

`inject.py` 原样拼接，DeepSeek 实际收到：

```
【输出语言】zh。
- 输出语言：zh
- 分镜粒度：shot
```

模型只能猜「zh/shot」的含义，输出语言与分镜粒度都可能漂移。
（UI 节点传的是「中文 / 按镜头」，所以只在非节点调用路径暴露。）

### 1.4 P1：custom provider 的 JSON 配置根本不可用

`config/default.yaml` 原来是：

```yaml
custom_request_template: {}
custom_response_path: null
custom_response_template: null
```

- `build_gateway()` 对 `custom` 要求 `prompt.custom_response_path`；
  `null` → `GatewayError`，custom 模式无法启动；
- 模板为空时 gateway 才回退到内置 OpenAI 兼容模板，用户无法真正配置自己的接口；
- 若把 `custom_request_template` 写成**一整段 JSON 字符串**，
  `string.Formatter` 会把 JSON 自己的 `{` 当占位符 → `未知占位符` 报错；
- 若 `custom_response_path` 指向 `{"content": ...}` 这样的对象，
  旧代码 `str(value)` 会得到 Python repr（单引号），下游 JSON 解析必然失败。

### 1.5 P1：提示词允许引用清单名，解析器却把引用丢掉

`inject._EXISTENCE_RULES` 第 3 条明确写：

> 需要引用清单中的既有素材时，请在分镜的 asset_refs 里**逐字照抄**清单中的原始名称。

但 `script_build.generate_script_prompts()` 调 `validate_script()` 时**没有传**
`existing_assets`，于是这类引用被判定为 `invalid_reference` 并进入
`dropped_invalid_refs`（首帧/尾帧引用直接丢失）。
系统提示词说「可以引用」，解析器说「不存在」，属于同一种契约不一致。

### 1.6 P2：其他真实输出变体识别不了

- OpenAI 兼容网关把 `content` 再序列化成 JSON 字符串（双重编码）；
- 业务 JSON 包在 `{"data": {...}}` / `{"result": "..."}` 外层；
- 中文键：`素材 / 章节 / 类型 / 角色 / 分镜 / 素材引用`；
- 顶层直接给 `segments`（没有 `chapters`）；
- 分镜把 H3 三字段拆开写（`integrated_multimodal_description` 等），没有 `prompt`；
- 模型漏写 `asset_refs`（本可按空数组处理，旧实现整份输出失败）。

---

## 2. 统一后的输出契约

### 2.1 规范结构（DeepSeek 必须输出这个）

```json
{
  "title": "章节标题（可选）",
  "output_language": "中文（可选）",
  "chapters": [
    {
      "chapter_id": "CH001（可省略，程序补）",
      "title": "小节标题（可选）",
      "summary": "剧情摘要（可选）",
      "segments": [
        {
          "segment_index": 1,
          "prompt": "必填：最终视频提示词全文；H3 三字段按行合并进这一个字符串",
          "asset_refs": ["A1", "苏晚"],
          "duration_s": 10,
          "first_frame_ref": "A2",
          "last_frame_ref": "A2",
          "generation_params": {}
        }
      ]
    }
  ],
  "assets": [
    {
      "id": "A1",
      "asset_type": "CHARACTER | SCENE | PROP | SHOT",
      "name": "林澈",
      "prompt": "素材图提示词（必填）",
      "negative_prompt": "可选",
      "description": "可选",
      "source": "AI",
      "library_scope": "PROJECT",
      "tags": []
    }
  ]
}
```

必填：`chapters`（非空数组）、`assets`（数组，可为空）、
`assets[].id/asset_type/name/prompt`、`segments[].prompt/asset_refs`。
`asset_refs` 可以引用本批 assets 的 id/name，或既有素材清单里的原始名称。

### 2.2 系统提示词里的契约优先级

`prompt/inject.py` 现在在模板之后追加：

```
【输出契约 bettercomfyui.script-json/v1（最高优先级）】
只输出一个 JSON 对象……可以放在 ```json 围栏里，但围栏外不得有其它内容。
**本段优先于上文任何 JSON 格式要求**（包括 H3 提示词手册的【输出格式】）：
- 视频提示词必须写进 chapters[].segments[].prompt……
- 素材图提示词必须写进 assets[]……
- 禁止使用 H3 手册的顶层结构 task_type / video_prompt / material_image_prompts /
  character_sheet……
……
- 输出前自检：整个响应必须能被 json.loads 解析；assets 与 chapters 必须是数组；
  每个 asset 都有 id/asset_type/name/prompt；每个 segment 都有 prompt 与 asset_refs；
  被截断时先缩短描述再重发完整 JSON，禁止输出半个对象。
```

- 契约版本号：`bettercomfyui.script-json/v1`（`OUTPUT_CONTRACT_VERSION`）。
- 契约**永远追加在模板之后**：即使用户把 4 万字 H3 手册 / 精简版设成系统提示词，
  最后出现的格式约束仍是本项目契约。
- 本地模型 + `auto` 也会拿到完整契约（旧代码此分支为空）。

### 2.3 解析器兼容的输入形状

`validate.adapt_script_output(parsed) -> (script, warnings, shape)`：

| shape | 触发条件 | 处理方式 |
|---|---|---|
| `script` | 规范 `assets/chapters` | 字段别名/类型轻微归一 |
| `legacy` | 素材用 `type` 键，或 `chapters[].assets` | `type`→`asset_type`；章节级 assets 合并到顶层 |
| `handbook` | `video_prompt` / `material_image_prompts` / `character_sheet` / `task_type` | 显式转换（见 2.4），并写 warnings |
| 外层封装 | `data/result/script/output/payload/response/剧本/结果/内容/数据` | 解开后重新判断形状 |
| 中文键 | `素材/资产/章节/类型/角色/场景/道具/分镜/素材引用/提示词…` | 按 `_FIELD_ALIASES` 映射为规范字段 |
| 双重编码 | `"{\"chapters\": ...}"` | 最多递归 3 层解开，`ParseReport.extracted_from="double_encoded"` |

**兼容但不吞错**：

- 手册结构缺 `video_prompt` → `ScriptValidationError` 错误码
  `handbook_missing_video_prompt`（消息写明要补什么）；
- 完全未知的顶层结构 → 错误码 `unrecognized_output_shape`，
  消息列出实际顶层键与期望结构；
- 字段级错误仍由 `validate_script()` 报精确 `path` / `code`
  （例如 `$.chapters[0].segments[0].prompt [empty_field]`）；
- 所有自动改名/合成名称/补空数组都进 warnings，最终写进
  `ScriptPromptResult.meta["notes"]`，可在节点审计文本里看到（需 Lead 接一下，见第 6 节）。

### 2.4 H3 手册结构 → 标准结构的映射

| 手册字段 | 转换结果 |
|---|---|
| `video_prompt` | `chapters[0].segments[0].prompt`（只生成 1 个视频段） |
| `material_image_prompts[].type` | `人物图→CHARACTER`、`场景图→SCENE`、`道具图→PROP`、`分镜图→SHOT`（也接受英文枚举） |
| `material_image_prompts[].prompt` | `assets[].prompt` |
| `material_image_prompts[].negative_prompt` | `assets[].negative_prompt` |
| `material_image_prompts[].name` | `assets[].name`；缺失时按类别合成（`人物图1` 等，记 warning） |
| `character_sheet[]` | 仅当**没有** CHARACTER 素材图时，用 `appearance + costume` 生成 CHARACTER 素材；已有人物图时只保留 `character_sheet` 原文并提示 |
| `task_type` / `notes` | 原样保留在顶层（不丢），并写转换 warning |
| `asset_refs` | 置空数组（手册未给出显式引用；首尾帧由导演台指定，warning 明说） |

> 设计取舍：手册结构本身不包含「哪个素材对应哪一段」的引用关系，
> 强行猜 `asset_refs` 会造成错误首帧。适配层选择**不猜**，并把「需要人工/导演台指定首尾帧」写进 warning。

---

## 3. 改了哪些文件

### 3.1 `bettercomfyui/prompt/validate.py`

- 新增 `adapt_script_output()`（`__all__` 同步）与 `ADAPTED_SHAPES`；
- 新增形状适配常量与函数：顶层别名、章节内分镜段别名、外层封装、H3 手册标记、
  中英文素材类别映射、H3 三字段合并；
- 重写 `canonicalize_script()` 为「别名归一 + 章节级 assets 合并」两步，
  保持旧的深拷贝/去重/顺序语义（`tests/verify/test_acceptance_16_seam_shapes.py` 全过）；
- `_try_parse_json()` 支持双重编码（最多 3 层）；
- 字段归一：`asset_refs` 逗号串→数组、`segment_index/duration_s` 数字串→数字、
  `prompt` 缺失但 `description` 存在时回退并 warning、缺失 `asset_refs` 补空数组并 warning；
- 兼容 `chapters` 单对象、顶层 `segments` 数组、`segments`/`分镜`/`shots` 等别名；
- 模块 docstring 增加 ROUND58 形状兼容说明。

> `normalize.py` **未改**：适配层的 `_alias_key()` 复用 `normalize_name()` 做键名归一
> （去空格/下划线、统一大小写与标点），素材名的匹配/疑似判定行为完全不变。

### 3.2 `bettercomfyui/prompt/inject.py`

- 新增 `OUTPUT_CONTRACT_VERSION = "bettercomfyui.script-json/v1"`；
- 重写 `_OUTPUT_RULES_JSON`：优先级声明 + 禁止手册顶层结构 + 精确 schema + 字段规则 + 输出前自检；
- 新增 `_ASSET_CATEGORY_RULE`、`_LANGUAGE_LABELS`、`_GRANULARITY_LABELS`、`_localized()`：
  `zh→中文`、`en→English`、`shot→按镜头`、`scene→按场景`；
- `build_system_prompt()`：本地模型 + `auto` 也追加完整契约；
- `build_user_prompt()`：输出语言/分镜粒度使用中文标签；
- 新增 `parse_manifest_asset_names()`：从 `store.inject` 的制表符清单（含旧的多空格格式）
  解析既有素材名，供 script_build 传给 `validate_script(existing_assets=...)`；
- `__all__` 同步。

### 3.3 `bettercomfyui/prompt/script_build.py`

- 解析后改用 `adapt_script_output()`，把返回的 warnings 写进 `meta["notes"]`；
- `meta` 新增 `output_shape`（`script/legacy/handbook`）与 `existing_asset_names`；
- 用 `parse_manifest_asset_names(messages["manifest"])` 得到的名称作为
  `validate_script(existing_assets=...)`，系统提示词允许的既有素材引用不再被丢掉；
- 占位模式仍用 `canonicalize_script()`（行为不变）。

### 3.4 `bettercomfyui/prompt/gateway.py`

- `_content_to_text()` 支持对象：`{"content": ...}` / `{"text": ...}` / `{"message": ...}`；
- 新增 `_coerce_template_node()`：`custom_request_template` 写成 JSON 字符串时先 `json.loads`，
  避免 JSON 的 `{` 被当格式占位符；
- `CustomGateway.build_payload()` 增加 `{api_key}`、`{max_tokens}` 占位符（未配置时为空串）；
- `CustomGateway.extract_text()`：路径取不到或不是文本时给出可操作 `GatewayError`；
  `response_template` 生成对象时用 `json.dumps` 而不是 Python repr；
- 保持「绝不静默回退 mock」「缺失配置列出键名」的既有红线。

### 3.5 `config/default.yaml` / `PromptParams`：custom provider 默认值（当前已落盘）

本专项的直接写域只包含 `config/default.yaml`（不含 `models/entities.py`）。
收尾核对时，仓库中的 `PromptParams` 默认值与 `config/default.yaml`
**已由并行集成同步为下面的推荐值**（`entities.py` 与 YAML 时间戳均为此轮集成），
本专项的最终交付**不包含**对这两个文件的改动（早期试改因一致性守卫已恢复）。
仓库有一条强守卫要求二者一致：

```python
# tests/test_foundation.py::test_dumped_default_config_is_reproducible
dump_config(Config(), tmp)
assert tmp.read_text() == (repo_root / "config" / "default.yaml").read_text()
```

它要求 `default.yaml` 与 `PromptParams` 的 dataclass 默认值**逐字节一致**
（不能有注释、不能有偏差）。因此只改 YAML 会让全量测试变红；必须同时改实体默认值。
**当前实际值**（新增契约测试 `test_default_config_carries_the_recommended_custom_provider_values`
会防止它再漂移）：

```yaml
prompt:
  max_tokens: 8192
  custom_request_template:
    model: '{model}'
    messages:
    - role: system
      content: '{system}'
    - role: user
      content: '{user}'
    stream: false
  custom_response_path: choices.0.message.content
  custom_response_template: null
```

对应的 `bettercomfyui/models/entities.py::PromptParams` 当前内容：

```python
    max_tokens: int | None = 8192
    custom_request_template: dict = field(default_factory=lambda: {
        "model": "{model}",
        "messages": [
            {"role": "system", "content": "{system}"},
            {"role": "user", "content": "{user}"},
        ],
        "stream": False,
    })
    custom_response_path: str | None = "choices.0.message.content"
    custom_response_template: str | None = None
```

核对/重建步骤：

1. 确认 `PromptParams` 的 `max_tokens`、`custom_request_template`、
   `custom_response_path` 与上面一致；
2. `python -B -c "from bettercomfyui.config import Config, dump_config; dump_config(Config(), 'config/default.yaml')"`
   重新生成默认 YAML（生成结果无注释）；
3. 跑 `tests/test_foundation.py::test_dumped_default_config_is_reproducible`
   与 `tests/test_round58_deepseek_json_contract.py`。

其余默认值（`output_language: zh`、`output_format: json`、`story_granularity: shot`）
**未改**，`tests/test_vision_defaults_conformance.py` 明确锁定它们；
短代码在 `inject.py` 边界翻译成中文标签，不把代码写进提示词。
`max_tokens: 8192` 按 DeepSeek-chat 输出上限设置；若换成本地小上下文模型，
请按该模型的上限覆盖（过大可能被服务端以 400 拒绝）。

> 备注：`config.py::_cross_validate` 目前只允许 `output_format` 为 `json/delimited`，
> 所以 `default.yaml` 不能写 `auto`。节点在加载后把 `cfg.prompt.output_format = "auto"`
> 并显式传 `output_format="auto"` 给解析器，当前不受影响。
> 若 Lead 想让配置层也支持 `auto`，需要改 `bettercomfyui/config.py`（不在本专项写域）。

### 3.6 测试与 fixtures（新增）

- `tests/fixtures/r58_pure.json` —— 纯 JSON；
- `tests/fixtures/r58_fenced.txt` —— json 代码围栏 + 前后说明；
- `tests/fixtures/r58_prose.txt` —— JSON 前后带自然语言说明；
- `tests/fixtures/r58_handbook.json` —— H3 手册结构（含中文素材类别、character_sheet）；
- `tests/test_round58_deepseek_json_contract.py` —— 23 条契约测试。

---

## 4. 测试结果

环境：Windows / Python 3.11.9 / `python -B`（无第三方网络调用）。

```text
python -B -m pytest tests/test_round58_deepseek_json_contract.py -q
23 passed（tests=23, failures=0, errors=0, skipped=0）

# 相关回归子集（prompt 全套 + 配置默认值 + 节点/一体化 + fusion/e2e）
python -B -m pytest \
  tests/test_prompt_validate.py tests/test_prompt_inject.py \
  tests/test_prompt_gateway.py tests/test_prompt_normalize.py \
  tests/test_prompt_redlines.py tests/test_prompt_store_integration.py \
  tests/test_prompt_match.py tests/test_round58_deepseek_json_contract.py \
  tests/test_vision_defaults_conformance.py tests/test_config_wiring_ratchet.py \
  tests/test_end_to_end_demo.py tests/test_comfy_node_controls.py \
  tests/test_one_stop_round46.py tests/test_one_stop_round49.py \
  tests/test_round55_prompts.py tests/test_round56_features.py \
  tests/test_fusion_contract.py -q
306 passed, 3 skipped, 0 failed

# 全量回归（含 tests/verify 验收集）
python -B -m pytest -q
# 20:20（并行集成改 __init__.py / routes.py 之前）：1697 tests, 0 failed, 18 skipped
# 20:32：1698 tests, 1 failed, 18 skipped
```

**关于 20:32 全量的唯一失败（与本专项无关）**：

```text
FAILED tests/verify/test_acceptance_28_install_and_workflows.py::
  test_p1_plugin_installed_with_identical_hashes
文件内容不一致（装的是旧版）：__init__.py / api_settings.py /
  everything_node.py / routes.py / web/js/dramacore_director_ui.js
```

这是**并行集成漂移**：`bettercomfyui_comfy/` 下的源码（本轮其他专项在 20:24—20:40
继续修改）比 `.comfyui/custom_nodes/bettercomfyui_comfy/` 里的安装副本新，
与 ROUND58 的提示词/JSON 契约改动无关。**需 Lead 重新同步插件后复跑该验收项**：

```powershell
python -B scripts/sync_to_comfyui.py          # 同步到 .comfyui/custom_nodes
python -B scripts/sync_to_comfyui.py --check  # 确认哈希一致
```

本专项未修改这些文件。子集 306 条与本专项相关的测试全绿。

3 条子集 skip 与全量 18 条 skip 都是环境/未验证项（`.comfyenv` 内 torch / 真实 pack
不可导入、`/free` 后未观测到显存下降等）。

```text
python -B -m py_compile bettercomfyui/prompt/*.py bettercomfyui/config.py \
  tests/test_round58_deepseek_json_contract.py
exit code 0
```

新增测试覆盖：

- 系统提示词包含可机读契约、优先级声明、禁止手册顶层结构、自检要求；
- `zh/shot` → `中文/按镜头`；
- 推荐/默认 custom provider 配置（第 3.5 节）能被 `build_gateway` 直接使用
  （请求体 messages、URL、响应路径、JSON 字符串模板、对象 content），
  并断言 `Config()`/`default.yaml` 与推荐值一致（防漂移）；
- 纯 JSON / 围栏 / 前后说明文字：`parse_with_report` + `adapt_script_output` +
  `validate_script` 三层全过，并各跑一次 `generate_script_prompts` E2E；
- 手册结构转换（警告可见、素材类别映射、character_sheet 不重复造人物图）；
- 手册缺 `video_prompt`、未知形状的错误码与可读消息；
- 外层封装 + 中文键 + `asset_refs` 逗号串；
- 双重编码 JSON；
- 旧 `type` / 章节级 assets 仍可用；
- H3 三字段合并进 `segments[].prompt`；
- 缺失 `asset_refs` 补空数组并 warning；
- 清单里的既有素材名不再被当作无效引用丢弃；
- 适配层不放过真正的字段错误（精确 path 仍然报出）。

---

## 5. 手工复现（不调用外部 API）

```python
from bettercomfyui.config import load_config
from bettercomfyui.prompt.inject import build_prompt_messages
from bettercomfyui.prompt.validate import parse_with_report, adapt_script_output, validate_script

cfg = load_config(repo_root=r"<repo>")
msgs = build_prompt_messages(script_text="林澈独行雪夜长街。", cfg=cfg, manifest="")
print(msgs["system"])              # 末尾应出现【输出契约 bettercomfyui.script-json/v1】

raw = open("tests/fixtures/r58_handbook.json", encoding="utf-8").read()
parsed, report = parse_with_report(raw, output_format="json")
script, warnings, shape = adapt_script_output(parsed)
print(shape, warnings)             # handbook + 转换说明
print(validate_script(script))     # assets: 人物图/场景图；segments: video_prompt
```

---

## 6. 仍需 Lead 落盘的补丁（本专项写域外）

### 6.1 `everything_node.py`：把解析/转换 warnings 暴露给用户

本专项按约定**没有修改** `everything_node.py`。当前功能已闭环（解析结果正确），
但适配层生成的 warnings 会被节点丢弃，用户看不到「模型按 H3 手册结构输出、已自动转换」
这类关键信息。建议在 `bettercomfyui_comfy/everything_node.py`
的 `generate_meta = {k: v for k, v in result.meta.items() if k != "notes"}`
（当前约第 692 行）之后插入：

```python
            # ROUND58：把解析/形状适配的 warnings 暴露给用户，避免“静默转换”。
            for _note in (result.meta.get("notes") or []):
                warnings.append(f"提示词解析：{_note}")
            if str(result.meta.get("output_shape") or "") == "handbook":
                warnings.append(
                    "模型按 H3 手册结构（video_prompt/material_image_prompts）输出，"
                    "已自动转换为标准剧本结构；建议系统提示词选择含【输出契约】的模板，"
                    "或让模型按 chapters/assets 输出。"
                )
```

### 6.2 `models/entities.py` + `config/default.yaml`：custom provider 默认值（已落盘，供核对/回滚）

**当前状态**：这两处已由并行集成同步为下面的推荐值（见 3.5），
`test_dumped_default_config_is_reproducible` 与新增的
`test_default_config_carries_the_recommended_custom_provider_values` 均通过。
本小节保留为**核对/回滚补丁**——若之后有人只改 YAML 或只改实体默认值，
全量测试会立刻变红，按下面两处一起改即可。

```python
# bettercomfyui/models/entities.py::PromptParams
    max_tokens: int | None = 8192
    custom_request_template: dict = field(default_factory=lambda: {
        "model": "{model}",
        "messages": [
            {"role": "system", "content": "{system}"},
            {"role": "user", "content": "{user}"},
        ],
        "stream": False,
    })
    custom_response_path: str | None = "choices.0.message.content"
```

然后重新生成默认 YAML 并跑守卫测试：

```powershell
python -B -c "from bettercomfyui.config import Config, dump_config; dump_config(Config(), 'config/default.yaml')"
python -B -m pytest tests/test_foundation.py::test_dumped_default_config_is_reproducible `
  tests/test_round58_deepseek_json_contract.py -q
```

> 这两步不需要再改 `prompt/*` 任何代码；新测试
> `test_recommended_custom_provider_config_is_gateway_compatible` 与
> `test_default_config_carries_the_recommended_custom_provider_values`
> 已把同一份配置固化成常量，并防止 YAML/实体默认值再次漂移。

### 6.3 其他可选（非阻塞）

1. **`config.py` 允许 `output_format: auto`**：若希望 `default.yaml` 直接写 `auto`，
   需要把 `config.py::_cross_validate` 的 `("json", "delimited")` 扩成
   `("json", "delimited", "auto")`。当前节点加载后自行赋值为 `auto`，不加也能跑。
2. **审计文本显示形状**：`everything_node.py` 末尾审计行目前只写
   `输出格式解析：{output_format_detected}`，可追加
   `形状：{generate_meta.get("output_shape", "")}`
   （`generate_meta` 已含该键），方便用户确认是否走了 handbook 转换。
3. **新精简版提示词接入**：另一专项若把精简版落成新的系统提示词预设名
   （例如 `h3_simplified`），建议在 `inject._resolve_template()` 里复用
   `h3_handbook`/`h3_core` 的解析分支，确保【输出契约】仍追加在模板之后；
   本专项未改 `h3_handbook.py` / `prompt/data/*`。

---

## 7. 兼容性与回滚

- **旧形状仍然可用**：`type` 键、章节级 `assets`、分隔符文本模式全部保留；
  `tests/verify/test_acceptance_16_seam_shapes.py` 通过。
- **默认配置行为**：`output_language`/`story_granularity` 的配置值未改，
  只在提示词边界翻译，`test_vision_defaults_conformance` 通过。
- **`config/default.yaml` 与 `Config()` 一致**：当前口径经并行集成同步为推荐
  custom 默认值，`test_dumped_default_config_is_reproducible` 通过；
  不要只改其中一处（见 3.5/6.2）。
- **回滚方式**：本专项修改集中在 4 个 prompt 文件 + 新增测试/fixtures/文档；
  另有并行集成的 `models/entities.py` / `config/default.yaml` 默认值（本专项未直接编辑）。
  回滚时按 6.2 同时恢复这两处，未改任何运行期数据文件或用户配置。
- **安全边界**：没有新增网络调用；测试全部使用 `_FakeGateway` / `_FakeTransport`；
  没有把真实密钥写入仓库。
