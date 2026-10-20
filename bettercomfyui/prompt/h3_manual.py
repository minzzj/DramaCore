# -*- coding: utf-8 -*-
"""H3 提示词工程手册落地（ROUND55）。

来源：``docs/ROUND55_H3提示词工程手册_纯文本.txt``（MiniMax H3 视频生成提示词工程手册 V1.0，
含 3.x 三字段语法 / 3.7 可见文字 / 3.8 音频三分层 / 5.2 DeepSeek 系统提示词 / 6.10 负面词库 /
7—9 模板 / 11 常见失败）。

本模块只提供**纯函数**（无 IO、无网络、无 ComfyUI 依赖），供上层
``bettercomfyui/prompt/inject.py``、``BetterComfyUIEverything`` 与单节点集成时调用：

* :func:`build_system_prompt_appendix` —— H3 手册要点（≤4000 字），追加到剧本拆解系统提示词；
* :func:`build_negative_prompt` —— 素材图/视频防瑕疵负面词库（手册 6.10）；
* :func:`normalize_style` —— 风格文本归一化（空白/标点/长度，幂等）；
* :func:`inject_no_subtitle_instruction` —— 追加「不要生成字幕/UI 文字/水印/logo/文字标识」，
  按手册 3.7 保留剧本明确要求的可见文字（英文双引号内原样）；
* :data:`NO_SUBTITLE_SYSTEM_RULE` —— 可放进系统提示词的硬约束常量；
* :func:`build_prompt_bundle` —— 汇总以上产物 + 配置开关，便于一站式注入。

诚实边界：H3 **没有 negative prompt 字段**（手册 6.10 明确），所以"负面词库"用于素材图模型；
视频侧用一句显式排除句（:data:`VIDEO_NEGATIVE_SENTENCE`）或提示词句子表达。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "NO_SUBTITLE_SYSTEM_RULE",
    "NO_SUBTITLE_PROMPT_RULE",
    "NO_SUBTITLE_STRICT_RULE",
    "VIDEO_NEGATIVE_SENTENCE",
    "HUMAN_NEGATIVE",
    "SCENE_NEGATIVE",
    "VIDEO_NEGATIVE",
    "MAX_APPENDIX_CHARS",
    "H3_TASK_TYPES",
    "build_system_prompt_appendix",
    "build_negative_prompt",
    "normalize_style",
    "normalize_task_type",
    "extract_scripted_visible_text",
    "has_no_subtitle_instruction",
    "inject_no_subtitle_instruction",
    "build_prompt_bundle",
]

#: 手册要点附录长度上限（任务书要求 ≤4000 字）。
MAX_APPENDIX_CHARS = 4000

#: H3 官方任务模式（手册 3.1 / 5.2）。
H3_TASK_TYPES = ("T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA")


# ---------------------------------------------------------------------------
# 禁止字幕 / 画面文字（手册 3.7 可见文字 + 用户新需求）
# ---------------------------------------------------------------------------

NO_SUBTITLE_SYSTEM_RULE = (
    "【画面文字硬约束·禁止生成字幕】最终视频画面中不得出现任何字幕（subtitles / captions）、"
    "滚动字幕、标题、时间码、UI 文字、界面元素、水印、logo、台标、片头片尾文字或任何文字标识；"
    "台词与旁白只通过声音与口型表达，不得以文字形式烧录进画面。"
    "唯一例外（手册 3.7 可见文字）：当剧本明确要求画面上出现某段文字时，"
    "必须按官方写法用英文双引号原样保留在该句里"
    "（例如：A red neon sign reading \"营业中\" glows above the doorway.），"
    "语言/大小写/标点都不改、不翻译；除此之外一律不得生成文字。"
)

#: 追加到**单段视频提示词**末尾的禁字幕规则（保留可见文字例外）。
NO_SUBTITLE_PROMPT_RULE = (
    "文字排除：不要生成字幕（subtitles / captions）、屏幕 UI 文字、水印、logo、台标或任何文字标识；"
    "台词只以语音/口型呈现，不要烧录成画面文字。"
    "若上文用英文双引号写出了剧本要求的可见文字，该文字可以原样出现在画面里（手册 3.7），"
    "其余任何文字都不得出现。"
)

#: 严格模式：即使提示词里有引号文字也不许出现（用户显式关闭保留时使用）。
NO_SUBTITLE_STRICT_RULE = (
    "文字排除：画面中不得出现任何文字，包括字幕（subtitles / captions）、UI 文字、水印、logo、"
    "台标、招牌文字、对话框与文字标识；台词只以语音/口型呈现。"
    "即使剧本或提示词中出现引号文字，也不要把它画到画面上。"
)

#: H3 无 negative prompt 字段（手册 6.10）：视频侧用显式排除句表达防瑕疵。
VIDEO_NEGATIVE_SENTENCE = (
    "The video must be free of flickering, jittery frames, morphing face, unstable body, "
    "warping background, broken lip sync and static noise; no on-screen text, captions, "
    "subtitles, watermarks, logos or UI elements. Keep the camera lock stable and the "
    "lip sync continuous with the spoken dialogue."
)

#: 去重标记：提示词里已存在任一标记则不再重复追加（大小写不敏感）。
_SUBTITLE_MARKERS = (
    "不要生成字幕",
    "禁止生成字幕",
    "不生成字幕",
    "无字幕",
    "不要字幕",
    "no subtitle",
    "no subtitles",
    "without subtitles",
    "subtitles are not",
    "no on-screen text",
    "文字排除",
    "no_subtitle",
)

#: 手册 3.7：可见文字放在英文双引号内原样保留。
_VISIBLE_TEXT_PATTERNS = (
    re.compile(r'"([^"\n]{1,120})"'),
    re.compile(r"“([^”\n]{1,120})”"),
    re.compile(r"「([^」\n]{1,120})」"),
)


def has_no_subtitle_instruction(prompt: str | None) -> bool:
    """提示词里是否已经包含"不要字幕"类指令（用于去重，避免重复追加）。"""
    text = str(prompt or "").lower()
    if not text:
        return False
    return any(marker.lower() in text for marker in _SUBTITLE_MARKERS)


def extract_scripted_visible_text(prompt: str | None) -> list[str]:
    """提取提示词中按手册 3.7 用引号写出的"剧本可见文字"（去重、保序）。

    同时兼容英文双引号、中文弯引号与直角引号；只做提取，不做判定。
    """
    text = str(prompt or "")
    found: list[str] = []
    for pattern in _VISIBLE_TEXT_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if value and value not in found:
                found.append(value)
    return found


def inject_no_subtitle_instruction(
    prompt: str,
    *,
    keep_scripted_visible_text: bool = True,
) -> str:
    """给系统提示词或单段视频提示词追加"不要生成字幕/文字"约束。

    行为（按任务书与手册 3.7）：

    * 空输入安全：返回规则本身（不抛错），便于调用方直接把返回值当提示词；
    * **去重**：若提示词已包含"不要生成字幕 / no subtitles"等标记，原样返回不重复追加；
    * ``keep_scripted_visible_text=True``（默认）：允许剧本用英文双引号明确要求的可见文字，
      并把已提取到的引号文字回写进规则，提醒模型"这些可以原样出现"；
    * ``keep_scripted_visible_text=False``：严格模式，连引号文字也不许出现在画面。

    返回：原文 + 空行 + 规则（已含原文时返回原文）。
    """
    base = str(prompt or "").strip()
    if base and has_no_subtitle_instruction(base):
        return base
    if not base:
        return NO_SUBTITLE_PROMPT_RULE
    if keep_scripted_visible_text:
        rule = NO_SUBTITLE_PROMPT_RULE
        visible = extract_scripted_visible_text(base)
        if visible:
            quoted = "、".join(f'"{item}"' for item in visible)
            rule = (
                f"{rule}\n剧本明确要求的可见文字（英文双引号内原样出现，不翻译、不改成字幕）："
                f"{quoted}。"
            )
    else:
        rule = NO_SUBTITLE_STRICT_RULE
    return f"{base}\n\n{rule}"


# ---------------------------------------------------------------------------
# 负面提示词库（手册 6.10；用于素材图模型，不用于 H3——H3 没有 negative 字段）
# ---------------------------------------------------------------------------

#: 人物类通用负面词（手册 6.10「人物类：通用人物」）。
HUMAN_NEGATIVE = (
    "blurry, low quality, low resolution, distorted, deformed, disfigured, bad anatomy, "
    "bad hands, extra fingers, extra limbs, fused fingers, missing fingers, bad proportions, "
    "plastic skin, watermark, text, signature, logo, cropped, out of frame, worst quality, "
    "jpeg artifacts, duplicate, oversaturated, cartoonish"
)

#: 场景/道具类负面词（手册 6.10「场景道具类」）。
SCENE_NEGATIVE = (
    "blurry, low quality, watermark, text, logo, cluttered, oversaturated, "
    "fisheye distortion, people, extra objects"
)

#: 视频成片防瑕疵词（手册 6.10「视频类」，用于提示词句子或后续检查，不是 H3 字段）。
VIDEO_NEGATIVE = (
    "flickering, jittery frames, morphing face, unstable body, warping background, "
    "lip sync broken, static noise"
)


def _dedupe_terms(terms: list[str]) -> list[str]:
    """按大小写不敏感去重、保持首次出现顺序，并去掉空项。"""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        item = str(term or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_negative_prompt(extra: str = "") -> str:
    """返回素材图/视频防瑕疵负面提示词（逗号分隔，已去重）。

    内容 = 人物类 + 场景道具类 + 视频类（手册 6.10），再追加调用方 ``extra``
    （按逗号/分号/顿号拆分后去重）。保证包含 ``blurry`` / ``watermark`` / ``text``。
    """
    extra_terms = re.split(r"[,，;；、\n]+", str(extra or ""))
    terms = _dedupe_terms(
        [*HUMAN_NEGATIVE.split(","), *SCENE_NEGATIVE.split(","),
         *VIDEO_NEGATIVE.split(","), *extra_terms]
    )
    return ", ".join(terms)


# ---------------------------------------------------------------------------
# 风格文本归一化（纯函数、幂等）
# ---------------------------------------------------------------------------

_STYLE_TRANSLATE = str.maketrans(
    {
        "，": ", ",
        "；": "; ",
        "、": ", ",
        "：": ": ",
        "。": ". ",
        "（": " (",
        "）": ") ",
    }
)

_STYLE_MAX_CHARS = 800


def normalize_style(text: str | None) -> str:
    """归一化风格描述文本（幂等，供统一风格/风格标签拼接使用）。

    规则：
    1. None / 非字符串安全处理为 ``""``；
    2. 中文标点转半角英文标点（便于拼进英文提示词）；
    3. 连续空白折叠为单个空格；
    4. 去掉首尾标点与空白、折叠重复逗号/分号；
    5. 超长截断到 :data:`_STYLE_MAX_CHARS` 字符（避免提示词爆长）。

    示例::

        >>> normalize_style("  冷蓝电影光，， 高对比。 ")
        '冷蓝电影光, 高对比'
    """
    if text is None:
        return ""
    value = str(text).strip()
    if not value:
        return ""
    value = value.translate(_STYLE_TRANSLATE)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*([,;])\s*(?:[,;]\s*)+", r"\1 ", value)
    value = re.sub(r"\s+([,;])", r"\1", value)
    value = value.strip(" ,;.")
    if len(value) > _STYLE_MAX_CHARS:
        value = value[:_STYLE_MAX_CHARS].rstrip(" ,;.")
    return value


_TASK_ALIASES = {
    "t2va": "T2VA", "t2v": "T2VA", "文生视频": "T2VA", "text to video": "T2VA",
    "i2va": "I2VA", "i2v": "I2VA", "首帧": "I2VA", "image to video": "I2VA",
    "fl2va": "FL2VA", "fl2v": "FL2VA", "首尾帧": "FL2VA", "first-last frame": "FL2VA",
    "l2va": "L2VA", "l2v": "L2VA", "尾帧": "L2VA", "last frame": "L2VA",
    "ref2va": "Ref2VA", "r2v": "Ref2VA", "全参考": "Ref2VA", "reference": "Ref2VA",
}


def normalize_task_type(value: str | None) -> str:
    """把各种任务模式写法归一为 H3 官方 ``T2VA / I2VA / FL2VA / L2VA / Ref2VA``。

    未知输入返回 ``""``（调用方决定报错或回退），不做猜测。
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _TASK_ALIASES:
        return _TASK_ALIASES[raw]
    for key, label in _TASK_ALIASES.items():
        if key and key in raw:
            return label
    return ""


# ---------------------------------------------------------------------------
# 系统提示词附录（≤4000 字；手册要点精简版）
# ---------------------------------------------------------------------------

_SYSTEM_APPENDIX = """【H3 提示词编译规则（MiniMax H3 提示词工程手册 V1.0 精简）】
你是 MiniMax H3 视频生成提示词编译器：把剧本编译成 H3 可直接执行的结构化提示词，
并在需要时输出四类素材图（人物/场景/道具/分镜）的文生图提示词。
一、输出语言与篇幅
1. 提示词正文一律英文（H3 文本编码器对英文结构化文本响应最佳）；对白、歌词、画面可见文字保留原语言。
2. 生成类主字段目标 350—450 个英文词；对白密集优先保证完整台词时间线，不机械凑字数。
3. 禁止抽象词（cinematic / beautiful / 紧张 / 感人 / 史诗感等无画面含义的词）；每句只写某个时刻
   看得见、听得见的具体内容（构图、主体位置、可见动作、状态变化、运镜、同步声音）。
4. 先定总时长（4—15 秒）再排切点：约每 3 秒一个切镜，每镜至少 3 秒。
二、任务模式判定（必须先判定再写）
- 纯文本无参考图 → T2VA：直接三字段，无指令行。
- 有 1 张首帧 → I2VA：首行写官方对齐指令（Picture 1 对齐 0.00 秒），空一行后接三字段。
- 首帧+尾帧 → FL2VA：首行说明 Picture 1 对齐 0.00 秒、Picture 2 对齐 S.SS 秒；写两帧之间连续可见的变化路径。
- 只有尾帧 → L2VA：首行说明 Picture 1 对齐 S.SS 秒；推断合理前置状态逐步收敛到尾帧。
- 有参考图/视频/音频 → Ref2VA：输出六段（subject_definitions / summary / retention_analysis /
  detailed_description / overall_soundscape / non_diegetic_music），正文英文书写。
三、三字段语法（T2VA/I2VA/FL2VA/L2VA，字段名不可改名、不可省略）
- integrated_multimodal_description：以 [Shot 1] 开头写风格与初始构图，再写主体、动作、运镜、同步声音；
  后续镜头 [Shot N] At 00:SS.mmm, the camera cuts to ...（[Shot 1] 无时间戳，切点严格递增且在时长内；
  切镜必须引入新主体/空间/状态/视点/时间，仅角度距离变化用运镜不切镜）。
- overall_soundscape：1—4 句英文，氛围声/物理动作声/非语言人声；不重复对白与配乐；全程静音才写 N/A。
- non_diegetic_music：1—3 句英文，只写乐器、速度、节奏、力度变化；无配乐写 N/A；不要抽象情绪词。
- 镜头 = 运动类型 + 幅度 + 速度（Push/Pull/Truck/Pedestal/Pan/Tilt/Arc/Tracking/Static/Shake/POV/
  Roll），幅度速度只在影响结果时写，并自然写进句子而不是句尾标签堆叠。
四、对白、口型与可见文字
- 说话人按发声顺序编号 (S1)(S2)……跨镜头稳定；多人同声 (S1,S2)；不出声不给编号。
- 身份/语气/动作写在 <d> 标签外；<d> 内只放语言标签 + 逐字台词：<d>[English] 原词原句。</d>；
  台词逐字保留、不翻译不改写。画外音用 says in an off-screen voiceover 并说明画内人物嘴唇闭合。
- 对白预算约每秒 2.5 个英文词；超长台词拆镜头或缩短。
- 可见文字（手册 3.7）：画面上实际出现的招牌/标签/界面文字必须放英文双引号内原样保留
  （语言、大小写、标点都不改、不翻译），例如 A red neon sign reading \"营业中\" glows above the doorway.
五、音频三分层（不得混放）
场景同步声写进 integrated_multimodal_description 时间线；整体氛围/物理声写进 overall_soundscape；
观众专属配乐写进 non_diegetic_music。
六、素材图提示词（用户要求时输出）
四类：人物图（锁定身份与服装，脸部清晰、单一主体）、场景图（环境/光线/色调，无杂乱人物）、
道具图（材质/形状/颜色/文字精确、主体突出）、分镜图（故事板、景别与构图明确、运镜方向可读）。
结构顺序 MASTER RULE：Subject → Action → Environment → Style → Camera → Lighting → Mood → Details；
每条必须同时给 negative prompt（blurry, low quality, distorted, deformed, watermark, text,
extra fingers, bad anatomy, oversaturated 等，见负面提示词库）。
七、画面文字硬约束（用户新需求）
画面不得出现字幕、滚动字幕、标题、时间码、UI 文字、水印、logo、台标或任何文字标识；
台词只通过声音与口型表达，不得烧录成画面文字。唯一例外是第四节「可见文字」用英文双引号写出的
剧本明确要求文字（原样出现，不翻译）；除此之外不得生成任何文字。
八、输出前自检清单（全部通过才输出）
1. 总时长与所有切点匹配，[Shot 1] 无时间戳、切点递增且在时长内；
2. 每句都是可观察的画面或声音，没有剧情概括与抽象情绪词；
3. 说话人编号稳定，(Sx) 只在 detailed_description/集成描写的对白处出现；
4. 声音三层不混放；对白逐字、口型与发声者绑定、画外音嘴唇闭合说明齐全；
5. Ref2VA 六段齐全、标签语义一致、retention 标记正确；
6. 没有字幕/UI 文字/水印/logo；剧本要求的可见文字已用英文双引号原样保留。
"""


def build_system_prompt_appendix() -> str:
    """返回可追加到「剧本拆解系统提示词」的 H3 手册要点（≤4000 字）。

    内容与手册 V1.0 对齐：三字段语法 / 任务模式判定 / 禁抽象词 / 镜头与切点 / 对白口型 /
    可见文字(3.7) / 音频三分层(3.8) / 素材图 MASTER RULE 与负面词 / 禁止字幕硬约束 / 自检清单。
    """
    return _SYSTEM_APPENDIX


# ---------------------------------------------------------------------------
# 一站式汇总（供 Lead 注入）
# ---------------------------------------------------------------------------


def _cfg_switch(cfg: Any, name: str, default: bool) -> bool:
    """从 Config / SimpleNamespace / dict 读开关；缺失或异常回退默认值。"""
    if cfg is None:
        return bool(default)
    prompt = cfg.get("prompt") if isinstance(cfg, dict) else getattr(cfg, "prompt", None)
    raw: Any = None
    if isinstance(prompt, dict):
        raw = prompt.get(name)
    elif prompt is not None:
        raw = getattr(prompt, name, None)
    if raw is None:
        return bool(default)
    return bool(raw)


def build_prompt_bundle(
    *,
    prompt: str = "",
    cfg: Any = None,
    keep_scripted_visible_text: bool = True,
    include_appendix: bool = True,
) -> dict:
    """汇总 ROUND55 提示词产物，便于上层一次取用。

    参数：
        prompt: 系统提示词或单段视频提示词（空字符串安全）。
        cfg: Config 或 ``SimpleNamespace(prompt=...)``；读
             ``prompt.h3_manual_enabled``（默认 True）与 ``prompt.no_subtitle``（默认 True）。
        keep_scripted_visible_text: 是否保留手册 3.7 的剧本可见文字例外。
        include_appendix: 是否生成 H3 手册附录（h3_manual_enabled=True 且本参数为 True 才生成）。

    返回 dict 键：
        ``h3_manual_enabled`` / ``no_subtitle`` / ``system_appendix`` / ``appendix_chars`` /
        ``system_rule`` / ``negative_prompt`` / ``negative_for_video`` /
        ``injected_prompt`` / ``scripted_visible_text`` / ``note``。
    """
    h3_enabled = _cfg_switch(cfg, "h3_manual_enabled", True)
    no_subtitle = _cfg_switch(cfg, "no_subtitle", True)
    appendix = build_system_prompt_appendix() if (h3_enabled and include_appendix) else ""
    system_rule = NO_SUBTITLE_SYSTEM_RULE if no_subtitle else ""
    base = str(prompt or "")
    if no_subtitle:
        injected = inject_no_subtitle_instruction(
            base, keep_scripted_visible_text=keep_scripted_visible_text
        )
    else:
        injected = base
    return {
        "h3_manual_enabled": bool(h3_enabled),
        "no_subtitle": bool(no_subtitle),
        "system_appendix": appendix,
        "appendix_chars": len(appendix),
        "system_rule": system_rule,
        "negative_prompt": build_negative_prompt() if h3_enabled else "",
        "negative_for_video": VIDEO_NEGATIVE_SENTENCE if h3_enabled else "",
        "injected_prompt": injected,
        "scripted_visible_text": extract_scripted_visible_text(base),
        "note": (
            "H3 没有 negative prompt 字段（手册 6.10）：negative_prompt 用于素材图模型；"
            "视频侧用 negative_for_video 显式排除句。"
        ),
    }
