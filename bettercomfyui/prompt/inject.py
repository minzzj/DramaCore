"""提示词组装：系统提示词 + 用户提示词（A1 -> A2 -> A3 -> A4，CONTRACTS §5）。

关键职责（R1 落地的一半）：

* 把 ``store.inject.build_existing_asset_manifest`` 生成的**既有素材清单原文**
  注入系统提示词，并明确告诉模型「这些已经有了，不要再生成，只输出新的」。
* 明确禁止模型输出「已存在/不存在」的判断结论——存在性判断由
  :mod:`bettercomfyui.prompt.match` 在生成后做确定性匹配。
* 素材库为空时走「空」分支（方案 A1B）：跳过注入块，直接让模型输出新素材。
* 提示词模板可覆盖：``cfg.prompt.system_prompt_template`` 既可以是预置模板名
  （见 :data:`SYSTEM_PROMPT_PRESETS`），也可以是一整段自定义文本。
* 本地模型（千问）走简化约束指令（方案「本地模型适配：千问必开」）。

不写事件日志：事件统一由 ``bettercomfyui.store.events`` 负责（CONTRACTS §10），
本模块不自建日志文件。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from ._conf import cfg_bool, cfg_get, cfg_str
from .errors import PromptDependencyError
from .normalize import normalize_name

__all__ = [
    "SYSTEM_PROMPT_PRESETS",
    "DEFAULT_PRESET",
    "OUTPUT_CONTRACT_VERSION",
    "build_asset_manifest",
    "build_system_prompt",
    "build_user_prompt",
    "build_prompt_messages",
    "parse_manifest_asset_names",
]


# ---------------------------------------------------------------------------
# 预置系统提示词（方案：预置 5 套，用户可覆盖）
# ---------------------------------------------------------------------------

_ROLE_BLOCK = (
    "你是「Better ComfyUI」视频生成流水线中的剧本拆解与素材提示词模型。\n"
    "你的唯一任务：读取用户给出的剧本章节，输出\n"
    "（1）本批次**新增**素材的生成提示词；（2）分镜段提示词与素材引用。\n"
    "你不负责判断素材是否已经存在，也不负责生成图片或视频。"
)

DEFAULT_PRESET = "default"

#: 系统提示词里写明的输出契约版本（与 ``prompt/validate.py`` 的适配逻辑对应）。
#: 出现解析异常时，先核对系统提示词里这个版本与解析器版本是否一致。
OUTPUT_CONTRACT_VERSION = "bettercomfyui.script-json/v1"

SYSTEM_PROMPT_PRESETS: dict[str, str] = {
    "default": _ROLE_BLOCK,
    "strict_json": (
        _ROLE_BLOCK
        + "\n本次要求：只输出一个合法 JSON 对象，不得输出解释、前后缀文字或 Markdown 代码围栏。"
    ),
    "local_qwen": (
        "你是剧本拆解助手。请严格按用户要求的 JSON 结构输出，不要输出多余文字。"
        "只输出新增素材，已给出的素材不要重复输出。"
    ),
    "chinese_drama": (
        _ROLE_BLOCK
        + "\n风格要求：中文影视剧叙事，人物形象描述包含年龄段、发型、服装、气质；"
        "场景描述包含时间、天气、光线、环境细节；保持同一角色跨章节形象一致（逐字复用清单中的原名）。"
    ),
    "shot_by_shot": (
        _ROLE_BLOCK
        + "\n分镜粒度：按镜头拆分，每个镜头单独一段，包含镜头运动、景别、主体动作与情绪。"
    ),
}

#: 输出结构骨架（与 prompt/validate.py 的校验规则严格对应）。
_SCHEMA_SKELETON = """{
  "title": "章节标题",
  "chapters": [
    {
      "chapter_id": "CH001",
      "title": "小节标题",
      "summary": "本段剧情摘要",
      "segments": [
        {
          "segment_index": 1,
          "prompt": "本段分镜视频提示词（含镜头运动、景别、动作、情绪）",
          "asset_refs": ["A1"],
          "duration_s": 10,
          "first_frame_ref": "A2",
          "last_frame_ref": "A2"
        }
      ]
    }
  ],
  "assets": [
    {
      "id": "A1",
      "asset_type": "CHARACTER",
      "name": "林澈",
      "prompt": "素材生成提示词（画面主体、风格、光线、构图、画质）",
      "source": "AI",
      "library_scope": "PROJECT"
    }
  ]
}"""

_EXISTENCE_RULES = """【素材存在性规则（必须遵守）】
1. 下面列出的素材**已经存在于素材库**，禁止再次输出，禁止以「同义名/别名/简写」重复输出：
{manifest}
2. 你**只能**输出本次新增的素材；**禁止**输出「已存在 / 不存在 / 需要复用」之类的判断结论，
   存在性判断由程序在生成之后做确定性匹配完成。
3. 需要引用清单中的既有素材时，请在分镜的 asset_refs 里**逐字照抄**清单中的原始名称。
4. 不确定是否重复时，选择复用手写清单中的名称，而不是新造一个名字。"""

_EMPTY_LIBRARY_RULE = """【素材存在性规则（必须遵守）】
1. 当前项目素材库为空，本次可以自由输出新素材。
2. 你**只能**输出本次新增的素材；**禁止**输出「已存在 / 不存在」之类的判断结论，
   存在性判断由程序完成。"""

_OUTPUT_RULES_JSON = """【输出契约 __CONTRACT_VERSION__（最高优先级）】
只输出一个 JSON 对象，不要输出解释文字；可以放在 ```json 围栏里，但围栏外不得有其它内容。
**本段优先于上文任何 JSON 格式要求**（包括 H3 提示词手册的【输出格式】）：
- 视频提示词必须写进 chapters[].segments[].prompt。H3 三字段请按
  `integrated_multimodal_description: …`、`overall_soundscape: …`、
  `non_diegetic_music: …` 三行合并进同一个 prompt 字符串（也可以只写一段完整提示词）。
- 素材图提示词必须写进 assets[]（asset_type 只能是 CHARACTER / SCENE / PROP / SHOT）。
- 禁止使用 H3 手册的顶层结构 task_type / video_prompt / material_image_prompts /
  character_sheet：程序按下面的结构取素材与分镜，顶层换成手册结构会导致素材/分镜识别失败。
{schema}
字段规则：
- 顶层必须有 assets（数组，可为空）与 chapters（非空数组）；title 可选。
- assets[]：id 唯一（建议 A1、A2…）、asset_type 必填、name 必填且不得重复、prompt 必填；
  可选 negative_prompt / description / source / library_scope。
- chapters[]：chapter_id（CH001 形式，可省略）、title/summary 可选、segments 非空。
- segments[]：prompt 必填；asset_refs 必填（数组，可为空）；
  可选 segment_index、duration_s、first_frame_ref、last_frame_ref。
- asset_refs 只能引用本批 assets 的 id/name 或【素材存在性规则】清单里的原始名称。
- 输出前自检：整个响应必须能被 json.loads 解析；assets 与 chapters 必须是数组；
  每个 asset 都有 id/asset_type/name/prompt；每个 segment 都有 prompt 与 asset_refs；
  被截断时先缩短描述再重发完整 JSON，禁止输出半个对象。"""

_ASSET_CATEGORY_RULE = (
    "【素材类别】必须覆盖剧本中出现的：人物/角色（CHARACTER）、场景（SCENE）、"
    "物品/道具（PROP）、分镜/镜头（SHOT）。"
)

#: 输出语言/分镜粒度在配置里是短代码（``zh`` / ``shot``），但提示词里要写人话，
#: 否则 DeepSeek 会看到「输出语言：zh」「分镜粒度：shot」这种半截指令。
_LANGUAGE_LABELS: dict[str, str] = {
    "zh": "中文",
    "zh-cn": "中文",
    "zh_cn": "中文",
    "cn": "中文",
    "chinese": "中文",
    "中文": "中文",
    "en": "English",
    "en-us": "English",
    "en_us": "English",
    "english": "English",
    "英文": "英文",
}
_GRANULARITY_LABELS: dict[str, str] = {
    "shot": "按镜头",
    "shots": "按镜头",
    "按镜头": "按镜头",
    "scene": "按场景",
    "按场景": "按场景",
    "segment": "按分镜段",
    "segments": "按分镜段",
    "按分镜段": "按分镜段",
}


def _localized(value: Any, mapping: Mapping[str, str], default: str) -> str:
    """把配置里的短代码翻译成提示词里可读的中文/英文标签。"""
    text = str(value or "").strip()
    if not text:
        return default
    return mapping.get(text.lower(), text)


_OUTPUT_RULES_DELIMITED = """【输出格式】
使用分隔符文本输出，程序会自动分段。格式：
=== ASSET ===
id: A1
type: CHARACTER
name: 林澈
prompt: 素材生成提示词（可多行）
=== ASSET ===
id: A2
type: SCENE
name: 雪夜街道
prompt: ……
=== CHAPTER ===
chapter_id: CH001
title: 小节标题
=== SEGMENT ===
segment_index: 1
prompt: 分镜视频提示词
asset_refs: A1, A2
字段要求与 JSON 输出一致（type 只能是 CHARACTER / SCENE / PROP / SHOT）。"""

_LOCAL_SIMPLIFIED = """【简化要求（本地模型）】
1. 只输出 JSON，不要解释。
2. 只输出新增素材，清单里已有的不要输出。
3. 不要输出「已存在 / 不存在」的判断。
4. name 必须逐字照抄清单里的写法。"""


def _json_contract() -> str:
    """把契约版本与结构骨架填进输出契约文本。"""
    return (
        _OUTPUT_RULES_JSON.replace("__CONTRACT_VERSION__", OUTPUT_CONTRACT_VERSION)
        .replace("{schema}", _SCHEMA_SKELETON)
    )


# ---------------------------------------------------------------------------
# 素材清单
# ---------------------------------------------------------------------------


def build_asset_manifest(
    store: Any,
    *,
    library_scope: Any = None,
    manifest_builder: Callable[..., str] | None = None,
) -> str:
    """读取既有素材清单文本（复用 ``store.inject``，R1 的程序侧入口）。

    未注入 ``manifest_builder`` 且 ``bettercomfyui.store.inject`` 不可用时抛
    :class:`PromptDependencyError`——**不回退到自建列表**，避免出现第二份真源。
    """
    if manifest_builder is None:
        try:
            from ..store.inject import build_existing_asset_manifest
        except ImportError as exc:  # pragma: no cover - store 未就绪时
            raise PromptDependencyError(
                "需要 bettercomfyui.store.inject.build_existing_asset_manifest "
                "生成既有素材清单（CONTRACTS §4）；store 未就绪，"
                "请显式传入 manifest_builder（测试替身）"
            ) from exc
        manifest_builder = build_existing_asset_manifest

    if library_scope is None:
        text = manifest_builder(store)
    else:
        try:
            text = manifest_builder(store, library_scope=library_scope)
        except TypeError as exc:
            if "library_scope" not in str(exc):
                raise
            text = manifest_builder(store)
    return text or ""


#: 既有素材清单的行格式（``store.inject`` 是制表符分隔：类型 / 序号 / 名称 / 锁定状态）。
_MANIFEST_TAB_MIN_COLUMNS = 3
_MANIFEST_ROW_RE = re.compile(
    r"^(CHARACTER|SCENE|PROP|SHOT|AUDIO|VIDEO)\s+#?\s*(\d+)\s+(.+)$",
    re.IGNORECASE,
)


def parse_manifest_asset_names(manifest: str) -> list[str]:
    """从既有素材清单文本里提取素材名（去重、保持出现顺序）。

    ## 为什么需要它（ROUND58）

    系统提示词的【素材存在性规则】明确允许分镜的 ``asset_refs`` **逐字照抄清单里的
    既有素材名**；但 ``generate_script_prompts`` 之前没有把清单名传给
    ``validate_script(existing_assets=...)``，于是这类引用全部被判为
    ``invalid_reference`` 并被丢弃 —— 系统提示词说「可以引用」，解析器却说「不存在」，
    这是提示词与解析器的契约不一致。

    本函数只解析**程序自己生成**的清单文本（``store.inject`` 的格式），
    不猜测素材是否存在于库中：允许引用 ≠ 认定存在，最终存在性仍由
    :mod:`bettercomfyui.prompt.match` 判定（R1）。
    """
    names: list[str] = []
    for raw_line in str(manifest or "").splitlines():
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith("["):
            continue
        name = ""
        columns = [column.strip() for column in line.split("\t")]
        if (
            len(columns) >= _MANIFEST_TAB_MIN_COLUMNS
            and columns[1].isdigit()
        ):
            name = columns[2]
        else:
            match = _MANIFEST_ROW_RE.match(line)
            if match:
                # 尾部可能还有「LOCKED v1 MODEL_PASS」：按 2+ 空格切掉。
                tail = re.split(r"\s{2,}|\t", match.group(3).strip())[0].strip()
                name = tail
        if name:
            names.append(name)

    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        key = normalize_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


# ---------------------------------------------------------------------------
# 提示词组装
# ---------------------------------------------------------------------------


def _resolve_template(cfg: Any, template: str | None) -> str:
    """解析系统提示词模板。

    * 未配置 / 空白串 / 空 dict -> 默认预置（**不能用空串替换掉角色块**：
      ``config/default.yaml`` 里 ``system_prompt_template: ''`` 就是「未覆盖」的意思）；
    * 命中预置名 -> 用该预置；否则按整段自定义文本使用。
    """
    raw = template if template is not None else cfg_get(cfg, "prompt.system_prompt_template", None)
    if raw is None:
        return SYSTEM_PROMPT_PRESETS[DEFAULT_PRESET]
    if isinstance(raw, Mapping):
        # 允许 {"role": "...", "extra": "..."} 形式
        parts = [str(v) for v in raw.values() if isinstance(v, str) and v.strip()]
        return "\n".join(parts) if parts else SYSTEM_PROMPT_PRESETS[DEFAULT_PRESET]
    text = str(raw)
    key = text.strip().lower()
    if not key:
        return SYSTEM_PROMPT_PRESETS[DEFAULT_PRESET]
    if key in ("h3_handbook", "h3_core"):
        try:
            from .h3_handbook import resolve_preset  # noqa: PLC0415

            handbook = resolve_preset(key)
            if handbook:
                return handbook
        except Exception:  # noqa: BLE001 - 手册加载失败时退回默认预置，不阻断
            pass
    if key in SYSTEM_PROMPT_PRESETS:
        return SYSTEM_PROMPT_PRESETS[key]
    return text


def _is_local_model(cfg: Any, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    provider = cfg_str(cfg, "prompt.model_provider", "") or ""
    return provider.strip().lower().replace("-", "_") in ("local_qwen", "qwen", "local")


def _output_format(cfg: Any) -> str:
    raw = cfg_str(cfg, "prompt.output_format", "json") or "json"
    key = raw.strip().lower()
    if key in ("delimited", "delimiter", "text", "plain", "分隔符", "纯文本"):
        return "delimited"
    if key in ("auto", "json_or_delimited"):
        return "auto"
    return "json"


def build_system_prompt(
    *,
    cfg: Any = None,
    manifest: str = "",
    template: str | None = None,
    local_model: bool | None = None,
) -> str:
    """组装系统提示词（模板 + 既有素材注入 + 输出契约）。

    ROUND58：输出契约永远追加在模板之后，并明确写出「本段优先于上文任何 JSON
    格式要求」—— 这样即使用户把 H3 提示词手册（448 字精简版或 4 万字全文）
    设成系统提示词，DeepSeek 也不会把 ``video_prompt/material_image_prompts``
    的顶层结构当成最终输出，解析器就能稳定拿到 ``chapters/assets``。
    """
    parts: list[str] = [_resolve_template(cfg, template).strip()]

    if manifest.strip():
        parts.append(_EXISTENCE_RULES.replace("{manifest}", manifest.strip()))
    else:
        parts.append(_EMPTY_LIBRARY_RULE)

    simplified = _is_local_model(cfg, local_model)
    fmt = _output_format(cfg)
    if simplified:
        parts.append(_LOCAL_SIMPLIFIED)
        if fmt == "delimited":
            parts.append(_OUTPUT_RULES_DELIMITED)
        else:
            # 本地模型 + 默认 auto 也必须拿到完整契约（旧代码 auto 时什么都不加）。
            parts.append(_json_contract())
    else:
        if fmt == "delimited":
            parts.append(_OUTPUT_RULES_DELIMITED)
        else:
            parts.append(_json_contract())
        parts.append(_ASSET_CATEGORY_RULE)

    language = _localized(
        cfg_str(cfg, "prompt.output_language", "中文"), _LANGUAGE_LABELS, "中文"
    )
    if language:
        parts.append(f"【输出语言】{language}。")
    return "\n\n".join(p for p in parts if p)


def build_user_prompt(
    *,
    script_text: str,
    cfg: Any = None,
    manifest_injected: bool = False,
    chapter_hint: str | None = None,
) -> str:
    """组装用户提示词（剧本正文 + 参数 + 尾部 R1 提醒）。"""
    if not isinstance(script_text, str) or not script_text.strip():
        raise PromptDependencyError("script_text 必须是非空字符串（剧本正文）")

    granularity = _localized(
        cfg_str(cfg, "prompt.story_granularity", "按镜头"),
        _GRANULARITY_LABELS,
        "按镜头",
    )
    language = _localized(
        cfg_str(cfg, "prompt.output_language", "中文"), _LANGUAGE_LABELS, "中文"
    )
    fmt = _output_format(cfg)

    header_lines = [
        "请把下面的剧本拆解为素材提示词与分镜提示词。",
        f"- 输出语言：{language}",
        f"- 分镜粒度：{granularity}",
        f"- 输出格式：{'JSON' if fmt != 'delimited' else '分隔符文本'}",
    ]
    if chapter_hint:
        header_lines.append(f"- 章节标识：{chapter_hint}")
    header_lines.append(
        "- 只输出新增素材" + ("；既有素材清单已在系统提示词中给出，禁止重复输出。"
                            if manifest_injected else "。")
    )

    tail = (
        "【再次强调】不要输出「已存在 / 不存在」的判断结论；"
        "只输出新增素材，已给出的素材不要重复输出。"
    )
    return (
        "\n".join(header_lines)
        + "\n\n【剧本正文】\n"
        + script_text.strip()
        + "\n\n"
        + tail
    )


def build_prompt_messages(
    *,
    script_text: str,
    cfg: Any = None,
    store: Any = None,
    manifest: str | None = None,
    manifest_builder: Callable[..., str] | None = None,
    library_scope: Any = None,
    chapter_hint: str | None = None,
    template: str | None = None,
    local_model: bool | None = None,
) -> dict:
    """一次性组装 ``{"system","user"}``（供 ``gateway.complete`` 直接使用）。

    额外返回两个诊断字段（不参与模型调用）：
    ``manifest``（实际注入的清单原文）与 ``manifest_injected``。
    """
    inject_enabled = cfg_bool(cfg, "prompt.inject_existing_assets", True)
    if manifest is None:
        manifest = ""
        if inject_enabled and store is not None:
            manifest = build_asset_manifest(
                store, library_scope=library_scope, manifest_builder=manifest_builder
            )
    manifest = manifest or ""

    system = build_system_prompt(
        cfg=cfg, manifest=manifest, template=template, local_model=local_model
    )
    user = build_user_prompt(
        script_text=script_text,
        cfg=cfg,
        manifest_injected=bool(manifest.strip()),
        chapter_hint=chapter_hint,
    )
    return {
        "system": system,
        "user": user,
        "manifest": manifest,
        "manifest_injected": bool(manifest.strip()),
    }
