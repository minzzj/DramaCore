# ROUND55 · H3 提示词工程手册要点 + 禁止生成字幕（落地说明）

> 来源：`docs/ROUND55_H3提示词工程手册_纯文本.txt`（MiniMax H3 视频生成提示词工程手册 V1.0，713 行，已全文通读）。
> 落地代码：`bettercomfyui/prompt/h3_manual.py`（ROUND55 新增，纯函数、无 IO/无 ComfyUI 依赖）。
> 测试：`tests/test_round55_prompts.py`（18 条，`python -B -m pytest tests/test_round55_prompts.py -q` → EXIT=0）。
> 配置：`bettercomfyui/config.py` + `config/default.yaml` 新增 `prompt.h3_manual_enabled`（默认 True）、`prompt.no_subtitle`（默认 True）。
> 本轮**未改** `one_stop_node.py` / `everything_node.py`（由 Lead 集成）。

---

## 一、手册要点（压缩版，完整见手册原文）

### 1. 一句话定位
H3 是**视频生成模型不是文本模型**；本地部署没有官方 H3-Context-IR（把大白话改写成结构化提示词的模块未开源），
所以「我们/DeepSeek 就是 Context-IR」——提示词必须结构化，否则等于提交缺键 JSON（手册 1.1 / 2.4）。

### 2. 任务模式判定（先判定再写）
| 输入 | 模式 | 首行 |
|---|---|---|
| 纯文本 | T2VA | 无指令行，直接三字段 |
| 1 张首帧 | I2VA | `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.` |
| 首帧+尾帧 | FL2VA | 首行说明 Picture 1 对齐 0.00 秒、Picture 2 对齐 S.SS 秒；写两帧之间连续可见的变化路径 |
| 只有尾帧 | L2VA | 首行说明 `<Picture 1>` 对齐 S.SS 秒；推断前置状态收敛到尾帧 |
| 参考图/视频/音频 | Ref2VA | 六段格式（见 §4） |

### 3. 三字段（名称不可改、不可省略；手册 3.2 / 3.10）
- `integrated_multimodal_description`：`[Shot 1]` 开头（风格+初始构图）→ 主体/动作/运镜/同步声；
  后续 `[Shot N] At 00:SS.mmm, the camera cuts to ...`；`[Shot 1]` 无时间戳、切点严格递增且在时长内；
  **切镜必须引入新主体/空间/状态/视点/时间**，仅角度/距离变化用运镜不切镜。
- `overall_soundscape`：1—4 句英文，氛围声/物理动作声/非语言人声；不重复对白与配乐；全程静音才写 `N/A`。
- `non_diegetic_music`：1—3 句英文，只写乐器/速度/节奏/力度；无配乐写 `N/A`；**不用抽象情绪词**。
- 镜头表达 = 运动类型 + 幅度 + 速度（`Push/Pull/Truck/Pedestal/Pan/Tilt/Arc/Tracking/Static/Shake/POV/Roll`）；
  幅度/速度只在影响结果时写，自然写进句子而非句尾标签堆叠（3.4）。
- 节奏：约每 3 秒一个切镜，每镜至少 3 秒；先定总时长再排切点（3.5 / 3.9）。
- 篇幅：复杂多镜头 350—450 英文词；简单单镜头 150—250 词（3.9）。

### 4. 全参考 Ref2VA 六段（手册 4.x）
`subject_definitions` → `summary`（`[任务类型]` 前缀）→ `retention_analysis`（`fully_preserved` 等标记）→
`detailed_description`（风格写在 `[Shot 1]` **之前**，350—500 词）→ `overall_soundscape` → `non_diegetic_music`。
四种标签：`<Subject N>` 内容单元 / `<Picture N>` 帧锚点 / `<Video N>` 整段视频来源 / `<Audio N>` 音频信号。

### 5. 对白与口型（3.6）
- 说话人按发声顺序编号 `(S1)(S2)`，跨镜头稳定；多人同声 `(S1,S2)`；不出声不给编号。
- 身份/语气/动作写在 `<d>` 外；`<d>` 内只放语言标签+逐字台词：`<d>[English] 原词原句。</d>`；**台词逐字、不翻译不改写**。
- 画外音用 `says in an off-screen voiceover` 并紧跟「画内人物嘴唇保持闭合」；跨切镜用 `<scenetrans>`，被截断用 `<cutoff>`。
- 对白预算 ≈ 每秒 2.5 个英文词；10 秒留 20—25 词。

### 6. 可见文字（3.7，本次「禁字幕」的唯一例外）
画面上实际出现的招牌/标签/字幕/横幅/界面文字，**必须放在英文双引号内原样保留**（语言/大小写/标点不改、不翻译），
例如 `A red neon sign reading "营业中" glows above the doorway.`；听不清的源词标 `[unclear]`，不猜写。
→ 本项目的「禁止生成字幕」= 除这一段引号文字外，一律不生成任何字幕/UI/水印/logo/文字标识。

### 7. 音频三分层（3.8，不得混放）
场景同步声 → `integrated_multimodal_description` 时间线；整体声景 → `overall_soundscape`；配乐 → `non_diegetic_music`。
不要用「紧张的音乐」「烘托氛围」这类抽象词。

### 8. 素材图提示词（第 6 章）
四类：人物图（锁定身份/服装，脸清晰、单一主体）、场景图（环境/光线/色调，无杂乱人物）、道具图（材质/形状/颜色/文字精确）、
分镜图（故事板、景别/构图/运镜方向）。
MASTER RULE 顺序：`Subject → Action → Environment → Style → Camera → Lighting → Mood → Details`，每条附 negative prompt。
负面词库（6.10）：人物类（blurry/low quality/distorted/bad anatomy/extra fingers/watermark/text/logo…）、
场景道具类（watermark/text/logo/cluttered/people…）、视频类（flickering/morphing face/lip sync broken…）。
**H3 视频提示词本身没有 negative prompt 字段**（6.10 明确）→ 视频侧用句子显式排除。

### 9. 常见失败（第 11 章，Top 与修复）
| 现象 | 原因 | 修复 |
|---|---|---|
| 声音是编造的/不对 | 没指定音频 | 补音频指示：声音+环境声+排除项（no music） |
| 出现不想要的配乐 | 以为不写就无声 | 显式 `no music` 或 `non_diegetic_music: N/A` |
| 台词说完嘴还在动 | 没给收尾节拍 | 台词后加小动作/移开视线/微笑 |
| 口型对不上 | 视觉与音频时机不一致 | 一镜头一声音一主角；说话时稳住镜头 |
| 模型自己加运镜 | 没排除 | 写 `camera remains completely static throughout` |
| 时间戳超片长 | 先写内容后定时长 | 先定总时长再排切点 |
| 宽镜头口型不可读 | 人脸太小 | 说话角色用中景或更近 |
| 首尾帧被当两张静态图 | 没写中间变化路径 | 写连续可观察变化，直到收敛到 Picture 2 |
| 小构图变化也切镜 | 每次切镜重置主体状态 | 距离/角度变化用推拉摇移，只有新信息才切 |

---

## 二、本次落地实现（`bettercomfyui/prompt/h3_manual.py`）

### 常量
| 名称 | 用途 |
|---|---|
| `NO_SUBTITLE_SYSTEM_RULE` | 放系统提示词的硬约束：禁字幕/滚动字幕/标题/时间码/UI 文字/水印/logo/台标/文字标识；台词只以声音+口型；唯一例外=手册 3.7 引号可见文字 |
| `NO_SUBTITLE_PROMPT_RULE` | 追加到单段视频提示词的禁字幕规则（保留可见文字例外） |
| `NO_SUBTITLE_STRICT_RULE` | 严格模式（连引号文字也不画） |
| `VIDEO_NEGATIVE_SENTENCE` | H3 无 negative 字段 → 视频侧显式排除句（flickering/morphing/lip sync/no on-screen text…） |
| `HUMAN_NEGATIVE` / `SCENE_NEGATIVE` / `VIDEO_NEGATIVE` | 手册 6.10 负面词库（分三类） |
| `MAX_APPENDIX_CHARS = 4000`、`H3_TASK_TYPES` | 附录长度预算 / 任务模式枚举 |

### 函数
| 签名 | 说明 |
|---|---|
| `build_system_prompt_appendix() -> str` | 手册要点附录，实测 **2493 字**（≤4000），含「三字段/可见文字/负面/字幕/水印」等关键字 + 8 条自检清单 |
| `build_negative_prompt(extra: str = "") -> str` | 人物+场景道具+视频三类负面词去重合并；`extra` 按逗号/分号/顿号拆分去重；保证含 `blurry/watermark/text` |
| `normalize_style(text: str | None) -> str` | 中文标点→半角、空白折叠、去首尾/重复标点、800 字截断；**幂等** |
| `normalize_task_type(value: str | None) -> str` | 各种写法归一为 `T2VA/I2VA/FL2VA/L2VA/Ref2VA`，未知返回 `""` |
| `extract_scripted_visible_text(prompt) -> list[str]` | 提取英文双引号/中文弯引号/「」内的剧本可见文字（去重保序） |
| `has_no_subtitle_instruction(prompt) -> bool` | 去重检测（含"不要生成字幕/no subtitles/文字排除"等标记） |
| `inject_no_subtitle_instruction(prompt, *, keep_scripted_visible_text=True) -> str` | 追加禁字幕规则：空串安全（返回规则本身）、已含标记不重复追加、默认保留手册 3.7 可见文字并在规则里回写具体引号文字；`False`=严格模式 |
| `build_prompt_bundle(*, prompt="", cfg=None, keep_scripted_visible_text=True, include_appendix=True) -> dict` | 汇总返回 10 个键：`h3_manual_enabled/no_subtitle/system_appendix/appendix_chars/system_rule/negative_prompt/negative_for_video/injected_prompt/scripted_visible_text/note`；`cfg` 支持 Config / SimpleNamespace / dict，缺省 True |

### 配置（只加字段，不改实体）
- `bettercomfyui/config.py`：新增 `ROUND55_PROMPT_EXTRA_DEFAULTS = {"h3_manual_enabled": True, "no_subtitle": True}`（**默认值来源，不依赖 YAML**）；
  为配置段 `[prompt]` 增加 `_PromptParamsRound55(PromptParams)` 子类（**不改 `models/entities.py`**，旧 `Config()`/`PromptParams()` 兼容），
  未知键校验仍然生效（拼错会报 ConfigError），`_cross_validate` 增加布尔校验。
- `config/default.yaml` 加键：**本轮先不加**（原因：运行中的 ComfyUI 可能仍加载旧安装副本的 `config.py`，YAML 出现新键会让旧副本 `load_config` 报「未知键」，打断其他环节）。**Lead 同步 + 重启后**在 `prompt:` 段追加 `h3_manual_enabled: true` / `no_subtitle: true` 即可；`config.py` 已提供同值默认，不依赖 YAML，可被 `config/user.yaml` 与项目 `config.yaml` 覆盖。
- 读取方式：`build_prompt_bundle(cfg=cfg)` 自动读开关；`cfg.prompt.h3_manual_enabled` / `cfg.prompt.no_subtitle`（旧实例用 `getattr(..., True)` 兜底）。

---

## 三、Lead 集成建议（不改本轮文件，供 ROUND55 集成步骤）

### 1. 系统提示词（一次 API 的 system）
注入点：`bettercomfyui/prompt/inject.py::build_system_prompt()`（当前组装 系统提示词+素材清单+约束指令）。
建议：
```python
from bettercomfyui.prompt.h3_manual import build_prompt_bundle
bundle = build_prompt_bundle(cfg=cfg)          # 读 prompt.h3_manual_enabled / no_subtitle
system = build_system_prompt(...)              # 原逻辑
if cfg.prompt.h3_manual_enabled:
    system += "\n\n" + bundle["system_appendix"]     # H3 手册要点（2493 字）
if cfg.prompt.no_subtitle:
    system += "\n\n" + bundle["system_rule"]         # NO_SUBTITLE_SYSTEM_RULE
```

### 2. 逐段视频提示词
注入点：`everything_node.py` 产出 `video_prompts[]` 后 / `one_stop_node._run_single` 构造 `segments[].prompt` 前（任一处，建议统一在 Everything 出口）。
```python
video_prompts = [
    inject_no_subtitle_instruction(p, keep_scripted_visible_text=True)
    for p in video_prompts
]
```
注意与 ROUND55 的另一需求一致：**只追加一次**（函数自带去重，重复调用不会叠加）；保留剧本 3.7 引号可见文字。

### 3. negative / 素材图
- 素材图（SDXL/FLUX 等文生图）：把 `build_negative_prompt()` 接到素材生成的 negative 输入（当前单节点 `_NEGATIVE` 常量可替换/合并）。
- H3 视频：**不要**尝试 negative 字段；需要时在 `video_prompt` 末尾追加 `VIDEO_NEGATIVE_SENTENCE` 或按失败表现写排除句。

### 4. 审计/可观测
建议在审计中记录：`h3_manual_enabled / no_subtitle / appendix_chars / negative_terms数 / 被注入的段数`，
避免"配置改了但没生效"的假象（本仓库铁律：未知键报错、配置必须可追溯）。

### 5. UI（可选，后续）
面板「剧核配置」增加两个开关：`启用 H3 手册提示词`、`禁止生成字幕（保留剧本可见文字）`，
保存到 `config/user.yaml` 的 `prompt.h3_manual_enabled` / `prompt.no_subtitle`（面板已有写入通道）。

---

## 四、验证记录

```
python -B -m pytest tests/test_round55_prompts.py -q   → 18 passed, EXIT=0
python -B -m pytest tests/test_config_wiring_ratchet.py -q → 7 passed, EXIT=0（新增配置字段未破坏接线棘轮）
附录长度实测 2493 字（上限 4000）；build_prompt_bundle 默认返回 10 键
config roundtrip：to_dict → dump_config → load_config 后 h3_manual_enabled/no_subtitle 均保持 True
```

不联网、不用 GPU；未改 `one_stop_node.py` / `everything_node.py`。

---

_本文件由 docs-agent 生成（ROUND55 专项5）；代码/测试/配置改动均在上列允许写作用域内。_
