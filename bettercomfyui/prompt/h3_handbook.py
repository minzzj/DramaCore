# -*- coding: utf-8 -*-
"""H3 提示词工程手册落地（ROUND56 → ROUND58）。

用户要求：**不要把手册只当"要点"追加**，而是把手册内容真正作为系统提示词写入。
本模块把两份文档打包进 ``bettercomfyui/prompt/data/``，并按预设键解析成系统提示词：

* ``h3_concise_v1``（**默认**，ROUND58）—— ``data/h3_concise_v1.txt``：
  《MiniMax H3 提示词工程精简版》V1.0 / 2026-10 全文（12,912 字），
  含「系统提示词 1：H3 视频生成提示词编译器（Master）」「系统提示词 2：素材图提示词生成器」
  「提示词片段库」；
* ``h3_handbook``（历史完整版，仍可选）—— ``data/h3_handbook_full.txt``：
  《MiniMax H3 提示词工程手册》全文（41,966 字，ROUND56 默认）；
* ``h3_core``（旧精简，仍可选）—— 完整手册的核心章节
  （3.x 语法 / 5.2 系统提示词 / 6.10 负面 / 11 排错）。

ROUND58 变更：默认预设由 ``h3_handbook`` 改为 ``h3_concise_v1``（12,890 字精简版更适合
一次调用出全量提示词的上下文预算），旧完整版/旧核心章节**全部保留**为可选项。
"""
from __future__ import annotations

from pathlib import Path

#: 预设键（写进 config / 节点的 system_prompt_template 就是这些字符串）。
PRESET_V3 = "h3_v3_contract"
PRESET_CONCISE = "h3_concise_v1"
PRESET_FULL = "h3_handbook"
PRESET_CORE = "h3_core"

#: ROUND74：默认预设 = V3.0 契约对齐（唯一系统提示词，用户上传版）。
DEFAULT_PRESET = PRESET_V3

#: 可选预设（顺序即界面推荐顺序）。
PRESETS = (PRESET_V3, PRESET_CONCISE, PRESET_FULL, PRESET_CORE)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_PATH = DATA_DIR / "h3_handbook_full.txt"
CONCISE_DATA_PATH = DATA_DIR / "h3_concise_v1.txt"
V3_DATA_PATH = DATA_DIR / "h3_v3_contract.txt"

#: 预设 → 数据文件（告警文案里直接告诉用户该查哪个文件）。
PRESET_FILES = {
    PRESET_V3: "data/h3_v3_contract.txt",
    PRESET_CONCISE: "data/h3_concise_v1.txt",
    PRESET_FULL: "data/h3_handbook_full.txt",
    PRESET_CORE: "data/h3_handbook_full.txt",
}

#: 预设 → 人话标签（界面/告警/审计共用，避免各处各写一套）。
PRESET_LABELS = {
    PRESET_V3: "h3_v3_contract（V3.0 契约对齐·唯一提示词）",
    PRESET_CONCISE: "H3 提示词精简版（2026-10，V1.0）",
    PRESET_FULL: "H3 手册完整版（历史版）",
    PRESET_CORE: "H3 手册核心章节（旧精简）",
}

#: 别名 → 规范键（路由/节点/面板传进来的各种叫法都收口到这里）。
PRESET_ALIASES = {
    PRESET_V3: PRESET_V3,
    "h3_v3": PRESET_V3,
    "v3": PRESET_V3,
    "v3_contract": PRESET_V3,
    "契约对齐": PRESET_V3,
    "唯一提示词": PRESET_V3,
    PRESET_CONCISE: PRESET_CONCISE,
    "h3_concise": PRESET_CONCISE,
    "concise": PRESET_CONCISE,
    "concise_v1": PRESET_CONCISE,
    "lean": PRESET_CONCISE,
    "精简版": PRESET_CONCISE,
    PRESET_FULL: PRESET_FULL,
    "full": PRESET_FULL,
    "完整版": PRESET_FULL,
    "历史完整版": PRESET_FULL,
    PRESET_CORE: PRESET_CORE,
    "core": PRESET_CORE,
    "精简": PRESET_CORE,
    "核心章节": PRESET_CORE,
}

PREFIX = (
    "【MiniMax H3 视频生成提示词工程手册 V1.0 全文】\n"
    "下面是本项目采纳的 H3 提示词工程手册全文。你的输出必须严格遵循本手册规则"
    "（三字段结构、任务模式判定、禁止抽象词、镜头时长与切点、可见文字双引号、音频三分层、"
    "素材图 MASTER RULE、负面词库、常见失败修复等）。\n\n"
)
SUFFIX = (
    "\n\n【返回前自检】\n"
    "1) 任务模式（T2VA/I2VA/FL2VA/L2VA）判定正确；2) 三字段齐全且字段名不改；"
    "3) 每个镜头有明确动作/景别/光线/声音，无抽象词；4) 对白口型与音频一致；"
    "5) 画面上出现的文字用英文双引号原样保留；6) 不生成字幕/UI水印/logo（除非剧本明确可见文字）；"
    "7) **视频段 duration_s 由剧情决定（4–15s），禁止每段固定 15s；输出 JSON 时 duration_s 必填且逐段不同/按内容**。"
)

#: ROUND58：精简版（12,912 字）作为系统提示词时的开场/收尾约束。
CONCISE_PREFIX = (
    "【MiniMax H3 提示词工程精简版 V1.0（2026-10）全文 · 本项目默认系统提示词】\n"
    "下面全文是本文档的三部分：系统提示词 1：H3 视频生成提示词编译器（Master）；"
    "系统提示词 2：素材图提示词生成器（文生图用）；提示词片段库（按需拼入）。\n"
    "你的输出必须严格遵循本文全部规则：五步工作流；T2VA/I2VA/FL2VA/L2VA/Ref2VA 模式判定与"
    "对齐指令行；Base 三字段与 Ref2VA 六段字段名一字不改；禁止抽象词；总时长 4—15 秒且约每 3 秒"
    "一个切镜；对白逐字、口型与发声者 (Sx) 绑定、画外音含嘴唇闭合说明；画面可见文字用英文双引号"
    "原样保留；素材图 MASTER RULE 与每类模板/负面词库；输出前自检清单逐项通过；严格输出 JSON。\n\n"
)
CONCISE_SUFFIX = (
    "\n\n【本项目落地约束（与上文同等强制）】\n"
    "1) 只输出【输出格式】规定的 JSON 对象，不写解释、不加 Markdown 代码块以外的前后缀；"
    "2) 视频提示词正文用英文，对白/歌词/画面可见文字保留原语言并放在英文双引号内；"
    "3) 不生成字幕/UI/水印/logo（剧本明确要求可见文字的除外，且必须是镜头里拍得到的实物文字）；"
    "4) 素材图提示词必须带 negative_prompt，画幅与目标视频一致；"
    "5) 交付前逐条跑【输出前自检清单】，任何一条不通过就改到通过；"
    "6) **每个视频段的 duration_s 由你按剧情决定（4–15 秒），不得每段固定 15 秒、也不得所有段同一时长**："
    "台词密集/多切镜/复杂动作约 10–15s；普通对白 6–10s；无台词动作/空镜/氛围 4–6s；"
    "台词按约每秒 2.5 个英文词预留呼吸；总时长=各段 duration_s 之和，不强制固定总长；"
    "H3 单次生成上限 15s，超过 15s 的剧情必须拆段。\n"
    "7) **物理真实感（通用，不限题材）**：所有场景、道具、材质按现实世界尺度、重量和反射规律表现；"
    "玻璃、水晶、金属等透明/反光材质必须有真实厚度、折射/反射和接触阴影；人物皮肤、手部、衣料要有真实纹理；"
    "禁止塑料/CGI/悬浮/透明人感；除非剧本明确奇幻，不要无依据地放大背景装饰、道具或参考图中的局部元素成画面主体。\n"
    "8) **单段固定时长/单镜头约束**：如果只生成一个固定时长段，只写一个连续镜头；不要写多个 [Shot]、"
    "不要写超过该段总时长的 at 时间码。多镜头必须拆成多个 segment，每个镜头时间必须落在该段时长内。"
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_full() -> str:
    return _read(DATA_PATH)


def _read_concise() -> str:
    return _read(CONCISE_DATA_PATH)


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
    """取一节内容：优先用第二次出现（第一次是目录），找不到就退回第一次。"""
    positions = []
    pos = 0
    while True:
        i = text.find(start_marker, pos)
        if i < 0:
            break
        positions.append(i)
        pos = i + 1
    if not positions:
        return ""
    start = positions[1] if len(positions) > 1 else positions[0]
    end = text.find(end_marker, start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def get_full_handbook() -> str:
    """完整手册全文（历史版系统提示词）。"""
    body = _read_full()
    if not body.strip():
        return ""
    return PREFIX + body.strip() + SUFFIX


def get_core_handbook() -> str:
    """核心章节（3.x 语法 + 5.2 系统提示词 + 6.10 负面 + 11 排错）；失败回退全文。"""
    body = _read_full()
    if not body.strip():
        return ""
    parts = [
        _slice_between(body, "3  H3 提示词核心语法", "6  "),
        _slice_between(body, "5.2  DeepSeek 系统提示词", "5.3  剧本输入"),
        _slice_between(body, "6.10  负面提示词库", "6.11  素材图与"),
        _slice_between(body, "11  常见失败与修复", "12  附录"),
    ]
    core = "\n\n".join(p for p in parts if p)
    if len(core) < 2000:
        return get_full_handbook()
    return PREFIX + core + SUFFIX


def get_concise_handbook() -> str:
    """精简版全文（ROUND58 默认系统提示词）。

    数据文件缺失时**退回历史完整版**（绝不返回空提示词让模型裸奔）；
    调用方可通过 ``len(...)`` + :data:`CONCISE_DATA_PATH` 判断是否真的加载到了精简版。
    """
    body = _read_concise()
    if not body.strip():
        return get_full_handbook()
    return CONCISE_PREFIX + body.strip() + CONCISE_SUFFIX


def get_v3_contract() -> str:
    """V3.0 契约对齐系统提示词全文（唯一提示词；不额外加前后缀）。"""
    body = _read(V3_DATA_PATH)
    return body.strip()


def canonical_preset(key: str) -> str:
    """把任意别名收口成规范预设键；未知键返回 ``""``（调用方走其它预设）。"""
    return PRESET_ALIASES.get(str(key or "").strip().lower(), "")


def is_handbook_preset(key: str) -> bool:
    """这个键是不是本模块管的 H3 手册预设。"""
    return bool(canonical_preset(key))


def preset_label(key: str, default: str = "") -> str:
    """预设键 → 人话标签（用于界面/审计/告警）。"""
    canonical = canonical_preset(key)
    return PRESET_LABELS.get(canonical, default)


def preset_file(key: str, default: str = "") -> str:
    """预设键 → 数据文件相对路径（用于"数据为空请检查…"的告警）。"""
    canonical = canonical_preset(key)
    return PRESET_FILES.get(canonical, default)


def is_concise_preset(key: str) -> bool:
    """是不是精简版（ROUND58 默认）。"""
    return canonical_preset(key) == PRESET_CONCISE


def resolve_preset(key: str) -> str | None:
    """把预设键解析成系统提示词文本；未知键返回 None（调用方走其它预设）。"""
    canonical = canonical_preset(key)
    if canonical == PRESET_V3:
        return get_v3_contract()
    if canonical == PRESET_CONCISE:
        return get_concise_handbook()
    if canonical == PRESET_FULL:
        return get_full_handbook()
    if canonical == PRESET_CORE:
        return get_core_handbook()
    return None


def preset_chars(key: str) -> int:
    text = resolve_preset(key) or ""
    return len(text)
