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

from typing import Any, Callable, Mapping

from ._conf import cfg_bool, cfg_get, cfg_str
from .errors import PromptDependencyError

__all__ = [
    "SYSTEM_PROMPT_PRESETS",
    "DEFAULT_PRESET",
    "build_asset_manifest",
    "build_system_prompt",
    "build_user_prompt",
    "build_prompt_messages",
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

_OUTPUT_RULES_JSON = """【输出格式】
只输出一个合法 JSON 对象（不要 Markdown 说明文字）。结构必须严格如下：
{schema}
字段要求：
- assets[].id 在本批输出内唯一；assets[].name 不得重复（匹配按名称归一化后进行）。
- assets[].asset_type 只能是：CHARACTER / SCENE / PROP / SHOT。
- chapters[].segments[].prompt 非空，且分镜的 asset_refs 只能引用本批 assets 的 id/name
  或上面清单里的既有素材名，不得引用不存在的素材。"""

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
    """组装系统提示词（模板 + 既有素材注入 + 输出格式约束）。"""
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
        elif fmt == "json":
            parts.append("【输出格式】只输出一个合法 JSON 对象。\n{schema}".replace("{schema}", _SCHEMA_SKELETON))
    else:
        if fmt == "delimited":
            parts.append(_OUTPUT_RULES_DELIMITED)
        else:
            parts.append(_OUTPUT_RULES_JSON.replace("{schema}", _SCHEMA_SKELETON))
        parts.append(
            "【素材类别】必须覆盖剧本中出现的：人物/角色（CHARACTER）、场景（SCENE）、"
            "物品/道具（PROP）、分镜/镜头（SHOT）。"
        )

    language = cfg_str(cfg, "prompt.output_language", "中文")
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

    granularity = cfg_str(cfg, "prompt.story_granularity", "按镜头")
    language = cfg_str(cfg, "prompt.output_language", "中文")
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
