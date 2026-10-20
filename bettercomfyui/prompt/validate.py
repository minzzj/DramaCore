"""剧本输出的解析与结构校验（CONTRACTS §5；任务书第 9 节验收 2）。

职责边界：

* ``parse_script_output`` —— 只负责「文本 -> dict」。结构化 JSON 优先，
  兼容《方案》要求的「特殊分隔符文本」。解析失败一律抛
  :class:`ScriptValidationError`。
* ``validate_script`` —— 只负责「dict -> 已校验的规范 dict」。缺字段、
  无效引用、重复 ID、格式错误一律抛 :class:`ScriptValidationError`。

**绝不返回部分结果**：两条路径要么返回完整结果，要么抛异常；不存在
「校验了一半先返回、错的部分后面再说」这种假成功路径。校验未通过前
不得进入素材生成（验收 2）。

剧本 JSON 结构（``validate_script`` 通过后的规范形态，下游 ``director``
可直接消费）::

    {
      "title": "第一章",              # 可选，默认 ""
      "output_language": "中文",      # 可选，原样保留
      "chapters": [                   # 必填，非空
        {
          "chapter_id": "CH001",      # 可选；缺省按顺序补 CH001/CH002...
          "title": "...", "summary": "...",
          "segments": [               # 必填，非空
            {
              "segment_index": 1,     # 可选；缺省按顺序补 1..n
              "prompt": "...",        # 必填，非空
              "asset_refs": ["A1"],   # 必填（可为空列表）；每项必须能解析
              "duration_s": 10,       # 可选
              "first_frame_ref": "...", "last_frame_ref": "...",   # 可选
              "generation_params": {} # 可选
            }
          ]
        }
      ],
      "assets": [                     # 必填（可为空列表）；只允许「新增素材」
        {
          "id": "A1",                 # 必填、本输出内唯一（分镜引用的本地主键）
          "asset_type": "CHARACTER",  # 必填，取值来自 models.enums.AssetType
          "name": "林澈",             # 必填；同一输出内归一化后必须唯一
          "normalized_name": "林澈",  # 由校验器补全（normalize_name 的结果）
          "prompt": "...",            # 必填，非空
          "source": "AI",             # 可选，默认 AI
          "library_scope": "PROJECT"  # 可选，默认 PROJECT
        }
      ]
    }

``asset_refs`` 的解析顺序：本输出内的 asset ``id`` -> 本输出内 asset
``name``（归一化比较）-> ``existing_assets``（已注入的既有素材名，归一化比较）。
引用可以带类型前缀（``"角色:林澈"``）。任何解析不到的引用都是错误。

## ROUND58：模型输出形状的兼容契约

DeepSeek 的系统提示词可能同时来自本项目的【输出契约】与 H3 提示词手册（后者要求
``task_type / video_prompt / material_image_prompts / character_sheet`` 结构）。
解析器因此在**边界**上兼容三种形状，但**兼容不等于吞错**：

* ``script`` —— 本项目标准结构（``title`` / ``chapters`` / ``assets``）；
* ``legacy`` —— 旧调用方形状（章节级 ``assets`` + ``type`` 键）；
* ``handbook`` —— H3 手册结构（``video_prompt`` + ``material_image_prompts`` +
  ``character_sheet``），由 :func:`adapt_script_output` 显式转换，**返回的 warnings
  会写进结果元数据**，不会悄悄丢掉 ``video_prompt`` 或素材图提示词。

:func:`adapt_script_output` 只做「形状/键名归一」，字段是否合法仍由
:func:`validate_script` 严格判定；缺失必填字段依旧报出具体 ``path`` 与 ``code``。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..models.enums import AssetSource, AssetType, LibraryScope
from .errors import PromptError
from .normalize import normalize_name

__all__ = [
    "ScriptValidationError",
    "ParseReport",
    "parse_with_report",
    "parse_script_output",
    "canonicalize_script",
    "adapt_script_output",
    "validate_script",
    "SUPPORTED_OUTPUT_FORMATS",
    "SCRIPT_STRUCTURE_VERSION",
    "ADAPTED_SHAPES",
]


SCRIPT_STRUCTURE_VERSION = "1.0"

#: ``output_format`` 的合法取值（含常见同义写法）。
SUPPORTED_OUTPUT_FORMATS = ("json", "delimited", "auto")

_JSON_FORMAT_ALIASES = frozenset(
    {"json", "structured", "structured_json", "结构化json", "结构化 json"}
)
_DELIMITED_FORMAT_ALIASES = frozenset(
    {"delimited", "delimiter", "text", "plain", "plain_text", "分隔符", "纯文本", "特殊分隔符"}
)
_AUTO_FORMAT_ALIASES = frozenset({"auto", "json_or_delimited", "结构化json+分隔符", "json+分隔符"})

_CHAR_LIMIT_NAME = 200
_CHAR_LIMIT_PROMPT = 20000
_CHAR_LIMIT_ID = 128
_CHAR_LIMIT_TEXT = 20000


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ScriptValidationError(PromptError):
    """剧本解析/校验错误，携带结构化错误列表 ``[{"path","code","message"}]``。"""

    def __init__(self, errors: list[dict]) -> None:
        if not errors:
            raise ValueError("ScriptValidationError 必须携带至少一条错误（绝不返回部分结果）")
        normalized: list[dict] = []
        for item in errors:
            if not isinstance(item, Mapping):
                normalized.append(
                    {"path": "$", "code": "unknown", "message": str(item)}
                )
                continue
            normalized.append(
                {
                    "path": str(item.get("path", "$")),
                    "code": str(item.get("code", "unknown")),
                    "message": str(item.get("message", "")),
                }
            )
        self.errors: list[dict] = normalized
        super().__init__(self._render())

    @property
    def codes(self) -> list[str]:
        return [e["code"] for e in self.errors]

    def _render(self) -> str:
        head = self.errors[0]
        extra = "" if len(self.errors) == 1 else f"（共 {len(self.errors)} 条错误）"
        return (
            f"剧本校验失败{extra}：{head['path']} [{head['code']}] {head['message']}"
        )


def _fail(path: str, code: str, message: str) -> dict:
    return {"path": path, "code": code, "message": message}


# ---------------------------------------------------------------------------
# 解析：结构化 JSON
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseReport:
    """解析过程报告（不进入业务数据，供调用方记录日志/诊断）。"""

    requested_format: str
    method: str                      # "json" | "delimited"
    extracted_from: str              # 说明 JSON 是从哪里抽出来的
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "requested_format": self.requested_format,
            "method": self.method,
            "extracted_from": self.extracted_from,
            "warnings": list(self.warnings),
        }


_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def _strip_bom(text: str) -> str:
    return text.lstrip("\ufeff")


def _match_end(text: str, start: int) -> int:
    """返回 ``text[start]`` 处 ``{``/``[`` 的匹配结束下标（含字符串字面量处理）。

    找不到匹配返回 -1。
    """
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    stack = [closer]
    in_string = False
    escape = False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or ch != stack[-1]:
                return -1
            stack.pop()
            if not stack:
                return i
    return -1


def _balanced_slices(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "{[":
            end = _match_end(text, i)
            if end != -1:
                out.append(text[i : end + 1])
                i = end + 1
                continue
        i += 1
    return out


def _json_dict_candidates(raw: str) -> list[tuple[str, str]]:
    """按优先级返回 ``(来源说明, 待解析文本)`` 列表。"""
    candidates: list[tuple[str, str]] = []
    text = _strip_bom(raw).strip()
    if not text:
        return candidates
    candidates.append(("raw", text))
    for m in _FENCE_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        body = m.group(2).strip()
        if not body:
            continue
        if lang in ("json", "json5", ""):
            candidates.append(("fence", body))
    # 围栏内容里可能还包着一层说明文字；再做平衡切片兜底。
    for m in _FENCE_RE.finditer(text):
        for slice_ in _balanced_slices(m.group(2)):
            candidates.append(("fence_balanced", slice_))
    for slice_ in sorted(_balanced_slices(text), key=len, reverse=True):
        candidates.append(("balanced", slice_))
    return candidates


def _try_parse_json(raw: str, *, depth: int = 0) -> tuple[dict, str] | None:
    """多策略 JSON 提取，返回 ``(dict, 来源说明)`` 或 ``None``（表示全部策略不适用）。

    ``None`` 的语义是「本输入不是合法 JSON」，由调用方抛 ``invalid_json``
    —— 这里不吞错：失败信息会在异常里体现，不会退化成「解析出半个结果」。

    ROUND58：兼容「双重编码」——有些 OpenAI 兼容网关会把 ``content`` 再序列化成
    一个 JSON 字符串（``"{\"chapters\": ...}"``），这里最多递归 3 层把它解开。
    """
    for source, text in _json_dict_candidates(raw):
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue  # 预期控制流：本策略不适用，继续尝试下一策略（最终失败->invalid_json）
        if isinstance(value, dict):
            return value, source
        if isinstance(value, str) and depth < 3 and value.lstrip().startswith(("{", "[")):
            nested = _try_parse_json(value, depth=depth + 1)
            if nested is not None:
                return nested[0], "double_encoded"
            continue
        # 顶层是数组：不是合法剧本结构，但要让调用方拿到明确错误而不是「解析失败」。
        if isinstance(value, list):
            raise ScriptValidationError(
                [
                    _fail(
                        "$",
                        "invalid_top_level_type",
                        "模型输出的顶层必须是 JSON 对象（{...}），收到数组；"
                        "剧本必须以 chapters/assets 字段组织",
                    )
                ]
            )
    return None


# ---------------------------------------------------------------------------
# 解析：特殊分隔符文本
# ---------------------------------------------------------------------------

_BARE_DELIM_RE = re.compile(r"^\s*([=\-#*~_]{3,})\s*$")
_HEADER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*[=\-#*~_]{3,}\s*(?P<label>[^=\-#*~_]{1,40}?)\s*[=\-#*~_]{3,}\s*$"
    ),
    re.compile(r"^\s*[【\[]\s*(?P<label>[^】\]]{1,40})\s*[】\]]\s*$"),
    re.compile(r"^\s*#{1,6}\s*(?P<label>[^\s#][^#]{0,39}?)\s*#*\s*$"),
    re.compile(
        r"^\s*<(?P<label>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)"
        r"(?P<attrs>\s+[^>]*)?>\s*$"
    ),
)

_KV_RE = re.compile(
    r"^\s*(?:[【\[]\s*(?P<br_key>[^】\]]{1,40})\s*[】\]]\s*"
    r"|(?P<key>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff .]{0,39}?)\s*(?P<sep>[:：=])\s*)"
    r"(?P<value>.*)$"
)
_ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"([^\"]*)\"")


def _alias_key(text: str) -> str:
    """别名查表键：归一化 + 去空格 + 去下划线（``asset_refs`` -> ``assetrefs``）。"""
    return normalize_name(text).replace(" ", "").replace("_", "")


#: 分节别名 -> (section, 默认 asset_type)
_SECTION_ALIASES: dict[str, tuple[str, str | None]] = {
    "asset": ("assets", None),
    "assets": ("assets", None),
    "assetcard": ("assets", None),
    "资产": ("assets", None),
    "資產": ("assets", None),
    "素材": ("assets", None),
    "素材卡": ("assets", None),
    "角色": ("assets", "CHARACTER"),
    "人物": ("assets", "CHARACTER"),
    "角色图": ("assets", "CHARACTER"),
    "character": ("assets", "CHARACTER"),
    "characters": ("assets", "CHARACTER"),
    "person": ("assets", "CHARACTER"),
    "场景": ("assets", "SCENE"),
    "场景图": ("assets", "SCENE"),
    "scene": ("assets", "SCENE"),
    "scenes": ("assets", "SCENE"),
    "物品": ("assets", "PROP"),
    "道具": ("assets", "PROP"),
    "物品图": ("assets", "PROP"),
    "prop": ("assets", "PROP"),
    "props": ("assets", "PROP"),
    "分镜": ("assets", "SHOT"),
    "分镜图": ("assets", "SHOT"),
    "镜头": ("assets", "SHOT"),
    "shot": ("assets", "SHOT"),
    "shots": ("assets", "SHOT"),
    "storyboard": ("assets", "SHOT"),
    "chapter": ("chapters", None),
    "chapters": ("chapters", None),
    "章节": ("chapters", None),
    "章": ("chapters", None),
    "segment": ("segments", None),
    "segments": ("segments", None),
    "段": ("segments", None),
    "段落": ("segments", None),
    "分镜段": ("segments", None),
}

#: 字段别名 -> 规范字段名
_FIELD_ALIASES: dict[str, str] = {
    "id": "id",
    "编号": "id",
    "标识": "id",
    "name": "name",
    "名称": "name",
    "名字": "name",
    "素材名": "name",
    "type": "asset_type",
    "assettype": "asset_type",
    "类型": "asset_type",
    "类别": "asset_type",
    "素材类型": "asset_type",
    "prompt": "prompt",
    "提示词": "prompt",
    "正向提示词": "prompt",
    "negativeprompt": "negative_prompt",
    "负面提示词": "negative_prompt",
    "反向提示词": "negative_prompt",
    "description": "description",
    "描述": "description",
    "说明": "description",
    "source": "source",
    "来源": "source",
    "libraryscope": "library_scope",
    "scope": "library_scope",
    "作用域": "library_scope",
    "tags": "tags",
    "标签": "tags",
    "chapterid": "chapter_id",
    "章节id": "chapter_id",
    "title": "title",
    "标题": "title",
    "summary": "summary",
    "摘要": "summary",
    "概要": "summary",
    "segmentindex": "segment_index",
    "段号": "segment_index",
    "序号": "segment_index",
    "assetrefs": "asset_refs",
    "参考素材": "asset_refs",
    "素材引用": "asset_refs",
    "durations": "duration_s",
    "时长": "duration_s",
    "firstframeref": "first_frame_ref",
    "首帧": "first_frame_ref",
    "lastframeref": "last_frame_ref",
    "尾帧": "last_frame_ref",
    "generationparams": "generation_params",
    "生成参数": "generation_params",
}

_LIST_FIELDS = frozenset({"tags", "asset_refs"})
_NUMERIC_FIELDS = frozenset({"segment_index", "duration_s"})

#: 「JSON 解析不适用」哨兵（避免用 None 与合法的 JSON null 混淆）。
_NOT_JSON = object()


def _match_header(line: str) -> tuple[str, dict] | None:
    if _BARE_DELIM_RE.match(line):
        return "", {}
    for pattern in _HEADER_RES:
        m = pattern.match(line)
        if not m:
            continue
        label = (m.groupdict().get("label") or "").strip()
        attrs: dict = {}
        raw_attrs = m.groupdict().get("attrs")
        if raw_attrs:
            for key, value in _ATTR_RE.findall(raw_attrs):
                attrs[key] = value
        if label:
            return label, attrs
    return None


def _maybe_json(text: str) -> Any:
    """形如 ``{...}`` / ``[...]`` 的值尝试按 JSON 解析。

    语义：解析成功返回值对象；解析失败**原样返回字符串**（不丢数据，也不静默
    改成空值）——字符串会被后续 ``validate_script`` 的类型校验拦下并报错。
    """
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return text  # 预期控制流：不是 JSON，保留原始文本交给校验器判定
    return text


def _coerce_value(field_name: str, value: str) -> Any:
    text = value.strip()
    if field_name in _NUMERIC_FIELDS:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    if field_name in _LIST_FIELDS:
        # 先试 JSON 数组；不是 JSON 就退回「按中英逗号/顿号切分」。
        # 两条路径都是有语义的解析策略，没有任何数据被丢弃。
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed = _NOT_JSON  # 预期控制流：本策略不适用，改用分隔符切分
            if parsed is not _NOT_JSON:
                return parsed
        parts = [p.strip() for p in re.split(r"[,，、;；]", text) if p.strip()]
        return parts
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text  # 预期控制流：不是 JSON，保留原文（由校验器报类型错误）
    lowered = text.lower()
    if lowered in ("true", "是"):
        return True
    if lowered in ("false", "否"):
        return False
    return text


def _parse_block_body(lines: Sequence[str]) -> tuple[dict, list[str]]:
    """把块体解析成 ``(字段 dict, 无法识别的行)``。"""
    fields: dict[str, Any] = {}
    leftovers: list[str] = []
    current_key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_key, buffer
        if current_key is not None:
            text = "\n".join(buffer).strip()
            if current_key in fields:
                prev = fields[current_key]
                if isinstance(prev, list):
                    fields[current_key] = prev + [text] if text else prev
                else:
                    fields[current_key] = f"{prev}\n{text}" if text else prev
            elif text:
                fields[current_key] = _maybe_json(text)
        current_key, buffer = None, []

    for line in lines:
        if not line.strip():
            if current_key is not None:
                buffer.append("")
            continue
        m = _KV_RE.match(line)
        if m:
            flush()
            raw_key = (m.group("br_key") or m.group("key") or "").strip()
            alias = _FIELD_ALIASES.get(_alias_key(raw_key))
            if alias is None:
                leftovers.append(line)
                current_key = None
                continue
            current_key = alias
            raw_value = m.group("value")
            buffer = [] if raw_value is None else [raw_value]
            continue
        if current_key is not None:
            buffer.append(line.rstrip())
        else:
            leftovers.append(line)
    flush()
    for key in list(fields):
        if key in _LIST_FIELDS:
            value = fields[key]
            if isinstance(value, str):
                fields[key] = _coerce_value(key, value)
        if key in _NUMERIC_FIELDS and isinstance(fields[key], str):
            fields[key] = _coerce_value(key, fields[key])
    return fields, leftovers


def _parse_delimited(raw: str) -> tuple[dict, tuple[str, ...]]:
    text = _strip_bom(raw).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    blocks: list[dict] = []
    for line in lines:
        header = _match_header(line)
        if header is not None:
            blocks.append({"label": header[0], "attrs": header[1], "body": []})
            continue
        if blocks:
            blocks[-1]["body"].append(line)
        elif line.strip():
            # 分隔符之前的内容：作为隐式素材块（兼容「只有 key: value」的输出）。
            blocks.append({"label": "", "attrs": {}, "body": [line]})

    if not blocks:
        raise ScriptValidationError(
            [
                _fail(
                    "$",
                    "no_recognizable_structure",
                    "分隔符文本中没有任何可识别的块或字段；期望形如 "
                    "'=== ASSET ===' / '【角色】' / 'name: xxx' 的结构",
                )
            ]
        )

    warnings: list[str] = []
    result: dict = {"chapters": [], "assets": []}
    current_chapter: dict | None = None
    implicit_chapter_created = False

    for index, block in enumerate(blocks):
        label = block["label"]
        attrs: dict = block["attrs"]
        fields, leftovers = _parse_block_body(block["body"])
        if attrs:
            merged = dict(attrs)
            merged.update({k: v for k, v in fields.items() if k not in attrs})
            fields = merged
        if leftovers:
            stray = " / ".join(s.strip() for s in leftovers[:3] if s.strip())
            if stray:
                warnings.append(f"块 #{index} 中未识别的行已忽略：{stray[:120]}")

        alias_key = _alias_key(label)
        section_type = _SECTION_ALIASES.get(alias_key)

        if section_type is None and label and ("prompt" in fields or "name" in fields):
            # 未知标签但块内有素材字段：按素材处理，名称取标签本身。
            section, default_type = "assets", None
            fields.setdefault("name", label)
        elif section_type is None:
            if label:
                warnings.append(f"未知分节标签已跳过：{label}")
            continue
        else:
            section, default_type = section_type

        if section == "assets":
            record = dict(fields)
            if default_type and not record.get("asset_type"):
                record["asset_type"] = default_type
            if not record.get("name") and label:
                record["name"] = label
            result["assets"].append(record)
        elif section == "chapters":
            chapter = dict(fields)
            chapter.setdefault("segments", [])
            result["chapters"].append(chapter)
            current_chapter = chapter
        elif section == "segments":
            if current_chapter is None:
                current_chapter = {"segments": []}
                result["chapters"].append(current_chapter)
                implicit_chapter_created = True
            current_chapter.setdefault("segments", []).append(dict(fields))

    if implicit_chapter_created:
        warnings.append("分隔符文本中没有章节标签，已为分镜段自动建立隐式章节")
    if not result["assets"] and not result["chapters"]:
        raise ScriptValidationError(
            [
                _fail(
                    "$",
                    "no_recognizable_structure",
                    "分隔符文本里没有识别出任何素材块或章节块（解析结果为空，"
                    "拒绝把空结果当成「剧本为空」继续）；期望形如 "
                    "'=== ASSET ===' / '【角色】' / 'name: xxx' 的结构",
                )
            ]
        )
    if result["assets"]:
        for i, asset in enumerate(result["assets"]):
            asset.setdefault("id", f"A{i + 1}")
    return result, tuple(warnings)


# ---------------------------------------------------------------------------
# 对外解析入口
# ---------------------------------------------------------------------------


def _normalize_output_format(output_format: str) -> str:
    if not isinstance(output_format, str):
        raise ScriptValidationError(
            [
                _fail(
                    "$",
                    "invalid_output_format",
                    f"output_format 必须是字符串，收到 {type(output_format).__name__}",
                )
            ]
        )
    key = normalize_name(output_format)
    if key in _JSON_FORMAT_ALIASES:
        return "json"
    if key in _DELIMITED_FORMAT_ALIASES:
        return "delimited"
    if key in _AUTO_FORMAT_ALIASES:
        return "auto"
    raise ScriptValidationError(
        [
            _fail(
                "$",
                "invalid_output_format",
                f"未知 output_format={output_format!r}；允许值：json / delimited / auto",
            )
        ]
    )


def parse_with_report(raw: str, *, output_format: str = "json") -> tuple[dict, ParseReport]:
    """与 :func:`parse_script_output` 相同，但额外返回 :class:`ParseReport`。"""
    fmt = _normalize_output_format(output_format)
    if not isinstance(raw, str):
        raise ScriptValidationError(
            [
                _fail(
                    "$",
                    "invalid_type",
                    f"模型输出必须是字符串，收到 {type(raw).__name__}",
                )
            ]
        )
    if not raw.strip():
        raise ScriptValidationError([_fail("$", "empty_output", "模型输出为空")])

    if fmt == "json":
        parsed = _try_parse_json(raw)
        if parsed is None:
            raise ScriptValidationError(
                [
                    _fail(
                        "$",
                        "invalid_json",
                        "输出不是合法 JSON（已尝试原文、```json 围栏与平衡切片）；"
                        "按配置 output_format=json，禁止回退到分隔符解析",
                    )
                ]
            )
        value, source = parsed
        return value, ParseReport("json", "json", source)

    if fmt == "delimited":
        value, warnings = _parse_delimited(raw)
        return value, ParseReport("delimited", "delimited", "delimiter", warnings)

    # auto：先 JSON，再分隔符。两条路径都显式记录在 report 里，不是静默回退。
    parsed = _try_parse_json(raw)
    if parsed is not None:
        value, source = parsed
        return value, ParseReport("auto", "json", source)
    value, warnings = _parse_delimited(raw)
    return value, ParseReport("auto", "delimited", "delimiter", warnings)


def parse_script_output(raw: str, *, output_format: str = "json") -> dict:
    """解析模型输出为 dict。解析失败抛 :class:`ScriptValidationError`，绝不返回部分结果。"""
    parsed, _report = parse_with_report(raw, output_format=output_format)
    return parsed


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------

_CH_ID_RE = re.compile(r"^CH\d{3}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_text(
    value: Any,
    path: str,
    errors: list[dict],
    *,
    field_name: str,
    required: bool,
    limit: int = _CHAR_LIMIT_TEXT,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        if required:
            errors.append(_fail(path, "missing_field", f"缺少必填字段 {field_name}"))
        return None
    if not _is_str(value):
        errors.append(
            _fail(path, "invalid_type", f"{field_name} 必须是字符串，收到 {type(value).__name__}")
        )
        return None
    text = value.strip()
    if not text and not allow_empty:
        errors.append(_fail(path, "empty_field", f"{field_name} 不得为空"))
        return None
    if len(text) > limit:
        errors.append(
            _fail(path, "too_long", f"{field_name} 长度 {len(text)} 超过上限 {limit}")
        )
        return None
    if _CTRL_RE.search(text):
        errors.append(
            _fail(path, "invalid_format", f"{field_name} 含控制字符（可能是输出被截断/损坏）")
        )
        return None
    return text


def _enum_value(
    raw: Any,
    enum_cls: Any,
    path: str,
    errors: list[dict],
    *,
    field_name: str,
    required: bool,
    default: Any = None,
) -> str | None:
    if raw is None:
        if required:
            errors.append(_fail(path, "missing_field", f"缺少必填字段 {field_name}"))
            return None
        return None if default is None else str(default.value if hasattr(default, "value") else default)
    if not _is_str(raw):
        errors.append(
            _fail(path, "invalid_type", f"{field_name} 必须是字符串，收到 {type(raw).__name__}")
        )
        return None
    candidate = raw.strip()
    for variant in (candidate, candidate.upper(), candidate.lower()):
        try:
            return str(enum_cls.parse(variant).value)
        except ValueError:
            continue  # 预期控制流：大小写变体尝试；全部失败后在下方记录 invalid_enum 错误
    allowed = ", ".join(m.value for m in enum_cls)
    errors.append(
        _fail(path, "invalid_enum", f"{field_name}={raw!r} 非法；允许值：{allowed}")
    )
    return None


def _ref_key(ref: Any) -> str | None:
    """把一条引用转成用于查表的字符串；无法识别返回 None。"""
    if _is_str(ref):
        text = ref.strip()
        if not text:
            return None
        # 允许 "角色:林澈" / "CHARACTER:林澈" 形式，取分隔符右侧
        for sep in (":", "："):
            if sep in text:
                head, tail = text.split(sep, 1)
                if tail.strip():
                    return tail.strip()
        return text
    if isinstance(ref, Mapping):
        for key in ("id", "name", "asset_id", "素材名"):
            value = ref.get(key)
            if _is_str(value) and value.strip():
                return value.strip()
    return None


def _existing_name_set(existing_assets: Any) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    names: set[str] = set()
    if existing_assets is None:
        return ids, names
    if _is_str(existing_assets):
        names.add(normalize_name(existing_assets))
        return ids, names
    if isinstance(existing_assets, Mapping):
        existing_assets = list(existing_assets.values())
    if not isinstance(existing_assets, Iterable):
        return ids, names
    for item in existing_assets:
        if _is_str(item):
            names.add(normalize_name(item))
        elif isinstance(item, Mapping):
            for key in ("asset_id", "id"):
                value = item.get(key)
                if _is_str(value) and value.strip():
                    ids.add(value.strip())
            value = item.get("name")
            if _is_str(value) and value.strip():
                names.add(normalize_name(value))
    names.discard("")
    return ids, names


# ---------------------------------------------------------------------------
# ROUND58：模型输出形状适配（JSON 契约兼容层）
# ---------------------------------------------------------------------------

#: 适配后的形状名（写进 ``ScriptPromptResult.meta["output_shape"]``）。
ADAPTED_SHAPES: tuple[str, ...] = ("script", "legacy", "handbook")

#: 顶层键别名 -> 规范键。键统一经 :func:`_alias_key` 归一化后查表。
_TOP_LEVEL_ALIASES: dict[str, str] = {
    "assets": "assets",
    "asset": "assets",
    "assetlist": "assets",
    "assetcards": "assets",
    "assetcatalog": "assets",
    "素材": "assets",
    "素材列表": "assets",
    "素材卡": "assets",
    "素材清单": "assets",
    "资产": "assets",
    "资产列表": "assets",
    "chapters": "chapters",
    "chapter": "chapters",
    "chapterlist": "chapters",
    "章节": "chapters",
    "章节列表": "chapters",
    "分章": "chapters",
    "title": "title",
    "标题": "title",
    "片名": "title",
    "outputlanguage": "output_language",
    "输出语言": "output_language",
    "语言": "output_language",
}

#: 章节内「分镜段数组」的候选键（H3 手册/第三方常写 shots / 分镜）。
_SEGMENT_LIST_KEYS: tuple[str, ...] = (
    "segments",
    "segment",
    "shotlist",
    "shots",
    "storyboard",
    "分镜",
    "分镜段",
    "镜头",
    "段落",
)
_SEGMENT_LIST_KEYS_NORMALIZED: frozenset[str] = frozenset(
    _alias_key(key) for key in _SEGMENT_LIST_KEYS
)

#: 外层封装键：OpenAI 兼容服务/自配接口常把业务 JSON 包在 data/result/script 里。
_ENVELOPE_KEYS: tuple[str, ...] = (
    "data",
    "result",
    "script",
    "output",
    "payload",
    "response",
    "剧本",
    "结果",
    "内容",
    "数据",
)
_ENVELOPE_KEYS_NORMALIZED: frozenset[str] = frozenset(
    _alias_key(key) for key in _ENVELOPE_KEYS
)

#: H3 提示词手册的输出标记（手册 5.2 与《精简版》的【输出格式】）。
_HANDBOOK_MARKERS: tuple[str, ...] = (
    "video_prompt",
    "material_image_prompts",
    "character_sheet",
    "task_type",
)

#: 素材类别别名 -> ``models.enums.AssetType`` 的值。
_ASSET_TYPE_ALIASES: dict[str, str] = {
    "character": "CHARACTER",
    "characters": "CHARACTER",
    "person": "CHARACTER",
    "role": "CHARACTER",
    "portrait": "CHARACTER",
    "人物": "CHARACTER",
    "人物图": "CHARACTER",
    "角色": "CHARACTER",
    "角色图": "CHARACTER",
    "主角": "CHARACTER",
    "配角": "CHARACTER",
    "scene": "SCENE",
    "scenes": "SCENE",
    "environment": "SCENE",
    "background": "SCENE",
    "场景": "SCENE",
    "场景图": "SCENE",
    "环境": "SCENE",
    "背景": "SCENE",
    "prop": "PROP",
    "props": "PROP",
    "object": "PROP",
    "item": "PROP",
    "物品": "PROP",
    "物品图": "PROP",
    "道具": "PROP",
    "道具图": "PROP",
    "物件": "PROP",
    "shot": "SHOT",
    "shots": "SHOT",
    "storyboard": "SHOT",
    "frame": "SHOT",
    "分镜": "SHOT",
    "分镜图": "SHOT",
    "镜头": "SHOT",
    "故事板": "SHOT",
    "audio": "AUDIO",
    "音频": "AUDIO",
    "音色": "AUDIO",
    "video": "VIDEO",
    "视频": "VIDEO",
    "参考视频": "VIDEO",
}

#: 合成素材名用的中文类别标签（仅当模型没给 name 时使用，并记 warning）。
_ASSET_TYPE_LABELS_CN: dict[str, str] = {
    "CHARACTER": "人物图",
    "SCENE": "场景图",
    "PROP": "道具图",
    "SHOT": "分镜图",
    "AUDIO": "音频参考",
    "VIDEO": "视频参考",
}

#: H3 三字段：模型若按字段拆开输出，合并进 ``segments[].prompt``（不丢字段）。
_H3_PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("integrated_multimodal_description", "integrated_multimodal_description"),
    ("overall_soundscape", "overall_soundscape"),
    ("non_diegetic_music", "non_diegetic_music"),
)


def _map_asset_type(raw: Any) -> str | None:
    """把中英文素材类别写法映射到 ``AssetType`` 值；未知返回 ``None``。"""
    if raw is None:
        return None
    key = _alias_key(str(raw))
    if not key:
        return None
    return _ASSET_TYPE_ALIASES.get(key)


def _as_text(value: Any) -> str:
    """把模型可能给出的多种「文本容器」抽成字符串（只拼接，不改写内容）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        chunks = [_as_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk)
    if isinstance(value, Mapping):
        for key in ("text", "content", "prompt", "value", "description"):
            if key in value:
                text = _as_text(value[key])
                if text:
                    return text
        # H3 三字段对象：按字段名逐行输出，字段不丢。
        h3_lines: list[str] = []
        for field, label in _H3_PROMPT_FIELDS:
            text = _as_text(value.get(field))
            if text:
                h3_lines.append(f"{label}: {text}")
        if h3_lines:
            return "\n".join(h3_lines)
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_list(value: Any) -> list:
    """把单对象/逗号串/JSON 数组统一成 list（保持输入顺序）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed = _NOT_JSON
            if isinstance(parsed, list):
                return parsed
        return [part.strip() for part in re.split(r"[,，、;；\n]+", text) if part.strip()]
    return [value]


def _looks_like_script(obj: Mapping) -> bool:
    """是否带本项目的剧本骨架（assets / chapters，含别名）。"""
    keys = {_alias_key(str(key)) for key in obj}
    return any(_TOP_LEVEL_ALIASES.get(key) in ("assets", "chapters") for key in keys)


def _looks_like_handbook(obj: Mapping) -> bool:
    """是否是 H3 提示词手册要求的输出结构。"""
    return any(key in obj for key in _HANDBOOK_MARKERS)


def _is_legacy_shape(obj: Mapping) -> bool:
    """旧形状：素材用 ``type`` 键，或 assets 挂在章节级。"""
    for key in obj:
        if _TOP_LEVEL_ALIASES.get(_alias_key(str(key))) != "assets":
            continue
        items = obj.get(key)
        if isinstance(items, list) and any(
            isinstance(item, Mapping) and "type" in item for item in items
        ):
            return True
    chapters = obj.get("chapters")
    if isinstance(chapters, list):
        return any(isinstance(ch, Mapping) and "assets" in ch for ch in chapters)
    return False


def _rename_record_fields(record: Mapping) -> tuple[dict, list[str]]:
    """按 :data:`_FIELD_ALIASES` 把字段别名改成规范名（未知键原样保留）。"""
    out: dict[str, Any] = {}
    duplicates: list[str] = []
    for key, value in record.items():
        canonical = _FIELD_ALIASES.get(_alias_key(str(key)))
        if canonical is None:
            out.setdefault(str(key), value)
            continue
        if canonical in out:
            duplicates.append(f"{key}->{canonical}")
            continue
        out[canonical] = value
    return out, duplicates


def _coerce_number(text: str) -> Any:
    """``"10"`` -> 10，``"10.5"`` -> 10.5，其它返回 ``None``。"""
    stripped = str(text).strip()
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        return float(stripped)
    return None


def _normalize_asset_record(record: Mapping, *, index: int, warnings: list[str]) -> dict:
    out, duplicates = _rename_record_fields(record)
    if duplicates:
        warnings.append(
            f"assets[{index}] 同时出现规范字段与别名（已保留先出现的）：{', '.join(duplicates)}"
        )
    if out.get("asset_type") is not None:
        mapped = _map_asset_type(out["asset_type"])
        if mapped is not None:
            if str(out["asset_type"]).strip().upper() != mapped:
                warnings.append(
                    f"assets[{index}].asset_type={out['asset_type']!r} 已归一为 {mapped}"
                )
            out["asset_type"] = mapped
    for key in ("id", "name", "source", "library_scope"):
        if out.get(key) is not None and not isinstance(out[key], str):
            out[key] = str(out[key])
    if out.get("prompt") is not None and not isinstance(out["prompt"], str):
        out["prompt"] = _as_text(out["prompt"])
    if not str(out.get("prompt") or "").strip():
        description = _as_text(out.get("description"))
        if description:
            out["prompt"] = description
            warnings.append(
                f"assets[{index}] 缺少 prompt，已用 description 作为素材提示词"
                "（请让模型按【输出契约】输出 prompt）"
            )
    if out.get("negative_prompt") is not None and not isinstance(
        out["negative_prompt"], str
    ):
        out["negative_prompt"] = _as_text(out["negative_prompt"])
    if "tags" in out:
        out["tags"] = _as_list(out["tags"])
    return out


def _normalize_segment_record(record: Mapping, *, path: str, warnings: list[str]) -> dict:
    out, duplicates = _rename_record_fields(record)
    if duplicates:
        warnings.append(
            f"{path} 同时出现规范字段与别名（已保留先出现的）：{', '.join(duplicates)}"
        )
    index_value = out.get("segment_index")
    if isinstance(index_value, str):
        number = _coerce_number(index_value)
        if number is not None:
            out["segment_index"] = number
    duration = out.get("duration_s")
    if isinstance(duration, str):
        number = _coerce_number(duration)
        if number is not None:
            out["duration_s"] = number
    prompt = out.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        out["prompt"] = _as_text(prompt)
    if not str(out.get("prompt") or "").strip():
        merged: list[str] = []
        for field, label in _H3_PROMPT_FIELDS:
            text = _as_text(out.get(field))
            if text:
                merged.append(f"{label}: {text}")
        if merged:
            out["prompt"] = "\n".join(merged)
            warnings.append(
                f"{path}.prompt 缺失，已把 H3 三字段（integrated_multimodal_description / "
                "overall_soundscape / non_diegetic_music）合并进 prompt"
            )
    if "asset_refs" not in out:
        out["asset_refs"] = []
        warnings.append(f"{path}.asset_refs 缺失，已按空数组处理（不阻塞素材生成）")
    else:
        normalized_refs: list[Any] = []
        for ref in _as_list(out["asset_refs"]):
            if isinstance(ref, Mapping):
                normalized_refs.append({str(k): v for k, v in ref.items()})
            elif isinstance(ref, str):
                text = ref.strip()
                if text:
                    normalized_refs.append(text)
            elif ref is not None:
                normalized_refs.append(str(ref))
        out["asset_refs"] = normalized_refs
    for key in ("first_frame_ref", "last_frame_ref"):
        if out.get(key) is not None and not isinstance(out[key], str):
            out[key] = _as_text(out[key])
    return out


def _normalize_chapter_record(record: Mapping, *, index: int, warnings: list[str]) -> dict:
    out, duplicates = _rename_record_fields(record)
    if duplicates:
        warnings.append(
            f"chapters[{index}] 同时出现规范字段与别名（已保留先出现的）：{', '.join(duplicates)}"
        )
    # 旧形状允许把 assets 挂在章节级：先归一字段名/取值，再交给 _merge_chapter_assets。
    if "assets" in out:
        chapter_assets: list[Any] = []
        for ai, item in enumerate(_as_list(out["assets"])):
            if isinstance(item, Mapping):
                chapter_assets.append(
                    _normalize_asset_record(
                        item,
                        index=ai,
                        warnings=warnings,
                    )
                )
            else:
                chapter_assets.append(item)
        out["assets"] = chapter_assets
    segments_key: str | None = None
    for key in out:
        if _alias_key(str(key)) in _SEGMENT_LIST_KEYS_NORMALIZED:
            segments_key = str(key)
            break
    if segments_key is not None and segments_key != "segments":
        out["segments"] = out.pop(segments_key)
        warnings.append(f"chapters[{index}].{segments_key} 已按别名映射为 segments")
    if "segments" in out:
        raw_segments = _as_list(out["segments"])
        segments: list[Any] = []
        for si, seg in enumerate(raw_segments):
            if isinstance(seg, Mapping):
                segments.append(
                    _normalize_segment_record(
                        seg,
                        path=f"$.chapters[{index}].segments[{si}]",
                        warnings=warnings,
                    )
                )
            else:
                segments.append(seg)
        out["segments"] = segments
    return out


def _normalize_top_level(script: Mapping, warnings: list[str]) -> dict:
    """顶层键别名归一 + 单章节/顶层分镜段的兜底包装。"""
    out: dict[str, Any] = {}
    for key, value in script.items():
        canonical = _TOP_LEVEL_ALIASES.get(_alias_key(str(key)))
        if canonical is None:
            out.setdefault(str(key), value)
            continue
        if canonical in out:
            warnings.append(f"顶层键 {key!r} 与 {canonical!r} 重复，已保留先出现的一份")
            continue
        out[canonical] = value
        if str(key) != canonical:
            warnings.append(f"顶层键 {key!r} 已按别名映射为 {canonical!r}")
    chapters = out.get("chapters")
    if isinstance(chapters, Mapping):
        out["chapters"] = [chapters]
        warnings.append("chapters 是单个对象，已包成单元素数组")
    elif chapters is not None and not isinstance(chapters, list):
        out["chapters"] = _as_list(chapters)
        warnings.append("chapters 不是数组，已按列表解析")
    if "chapters" not in out:
        for key in list(out):
            if _alias_key(str(key)) in _SEGMENT_LIST_KEYS_NORMALIZED:
                out["chapters"] = [{"segments": out.pop(key)}]
                warnings.append(f"顶层键 {key!r} 被当作分镜段数组，已包成隐式章节")
                break
    return out


def _normalize_script_records(script: Mapping, warnings: list[str]) -> dict:
    """逐条归一 assets / chapters / segments 的字段名与取值形态。"""
    out = dict(script)
    if "assets" in out:
        assets: list[Any] = []
        for i, item in enumerate(_as_list(out["assets"])):
            if isinstance(item, Mapping):
                assets.append(_normalize_asset_record(item, index=i, warnings=warnings))
            else:
                assets.append(item)
        out["assets"] = assets
    chapters = out.get("chapters")
    if isinstance(chapters, list):
        normalized: list[Any] = []
        for ci, chapter in enumerate(chapters):
            if isinstance(chapter, Mapping):
                normalized.append(
                    _normalize_chapter_record(chapter, index=ci, warnings=warnings)
                )
            else:
                normalized.append(chapter)
        out["chapters"] = normalized
    return out


def _merge_chapter_assets(script: dict, warnings: list[str]) -> dict:
    """把章节级 ``assets`` 合并到顶层（顶层优先；按 id/name 去重）。"""
    chapters = script.get("chapters")
    if not isinstance(chapters, list):
        chapters = []
    merged: list[dict] = []
    seen: set[str] = set()

    def _absorb(items: Any) -> None:
        for item in _as_list(items):
            if not isinstance(item, Mapping):
                continue
            rec = dict(item)
            key = str(rec.get("id") or rec.get("name") or "")
            if key and key in seen:
                warnings.append(f"素材 {key!r} 在多处重复出现，已保留先出现的一份")
                continue
            if key:
                seen.add(key)
            merged.append(rec)

    _absorb(script.get("assets"))
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, Mapping):
            continue
        chapter_assets = chapter.get("assets")
        if chapter_assets is None:
            continue
        before = len(merged)
        _absorb(chapter_assets)
        if len(merged) > before:
            warnings.append(f"chapters[{index}].assets 已合并到顶层 assets")
        # 吸收后移除章节级副本：留着会让人（和下游）以为还有第二份。
        chapter.pop("assets", None)
    script["assets"] = merged
    return script


def _unwrap_envelopes(script: Mapping, warnings: list[str], *, depth: int = 0) -> Mapping:
    """解开 ``{"data": {...}}`` / ``{"result": "{...}"}`` 之类外层封装。"""
    if depth >= 3 or _looks_like_script(script):
        return script
    for key, value in script.items():
        if _alias_key(str(key)) not in _ENVELOPE_KEYS_NORMALIZED:
            continue
        inner: Any = value
        if isinstance(inner, str):
            parsed = _try_parse_json(inner)
            inner = parsed[0] if parsed is not None else None
        if isinstance(inner, Mapping) and (
            _looks_like_script(inner) or _looks_like_handbook(inner)
        ):
            warnings.append(f"已解开外层封装键 {key!r}（业务 JSON 在其内部）")
            return _unwrap_envelopes(copy.deepcopy(dict(inner)), warnings, depth=depth + 1)
    return script


def _synthetic_asset_name(asset_type: str, counters: dict[str, int]) -> str:
    label = _ASSET_TYPE_LABELS_CN.get(asset_type, asset_type or "素材")
    counters[label] = counters.get(label, 0) + 1
    return f"{label}{counters[label]}"


def _convert_handbook(script: Mapping, warnings: list[str]) -> dict:
    """把 H3 手册结构转成本项目标准脚本结构（字段显式搬运，不丢内容）。"""
    errors: list[dict] = []
    video_prompt = _as_text(script.get("video_prompt"))
    if not video_prompt:
        errors.append(
            _fail(
                "$.video_prompt",
                "handbook_missing_video_prompt",
                "识别为 H3 手册结构（task_type / video_prompt / material_image_prompts / "
                "character_sheet），但 video_prompt 缺失或不是非空文本；本项目要求 "
                "chapters[].segments[].prompt 非空。请让模型按【输出契约】输出，或补上 video_prompt",
            )
        )

    assets: list[dict] = []
    used_names: set[str] = set()
    counters: dict[str, int] = {}

    material = _as_list(script.get("material_image_prompts"))
    for index, item in enumerate(material):
        path = f"$.material_image_prompts[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_fail(path, "invalid_type", "素材图提示词条目必须是对象"))
            continue
        type_raw = item.get("type") or item.get("asset_type") or item.get("类别")
        asset_type = _map_asset_type(type_raw)
        if asset_type is None:
            errors.append(
                _fail(
                    f"{path}.type",
                    "invalid_asset_type",
                    f"type={type_raw!r} 无法映射到素材类别；允许：人物图/场景图/道具图/分镜图"
                    "（或 CHARACTER/SCENE/PROP/SHOT/AUDIO/VIDEO）",
                )
            )
        prompt = _as_text(item.get("prompt"))
        if not prompt:
            errors.append(_fail(f"{path}.prompt", "missing_field", "素材图提示词缺少非空 prompt"))
            continue
        name = _as_text(item.get("name"))
        if not name:
            name = _synthetic_asset_name(asset_type or "SHOT", counters)
            warnings.append(f"{path} 未给 name，已按类别合成名称 {name!r}")
        norm = normalize_name(name)
        if norm in used_names:
            base = name
            suffix = 2
            while normalize_name(f"{base}{suffix}") in used_names:
                suffix += 1
            name = f"{base}{suffix}"
            norm = normalize_name(name)
            warnings.append(f"{path} 名称重复，已改为 {name!r}（避免素材名归一化冲突）")
        used_names.add(norm)
        record: dict[str, Any] = {
            "id": _as_text(item.get("id")) or f"A{len(assets) + 1}",
            "asset_type": asset_type or "SHOT",
            "name": name,
            "prompt": prompt,
        }
        negative = _as_text(item.get("negative_prompt"))
        if negative:
            record["negative_prompt"] = negative
        description = _as_text(item.get("description"))
        if description:
            record["description"] = description
        assets.append(record)

    character_sheet = _as_list(script.get("character_sheet"))
    has_character_asset = any(record.get("asset_type") == "CHARACTER" for record in assets)
    if character_sheet and has_character_asset:
        # 手册语义：character_sheet 是「角色登记表」，人物图提示词在 material_image_prompts 里。
        # 已经有人物图时不再重复造 CHARACTER 素材（否则会多生成一张重复人物图）。
        names = [
            _as_text(person.get("name")) or _as_text(person.get("speaker_id"))
            for person in character_sheet
            if isinstance(person, Mapping)
        ]
        listed = "、".join(name for name in names if name)
        warnings.append(
            "character_sheet 中的角色"
            + (f"（{listed}）" if listed else "")
            + "未转成额外素材：已有人物图提示词；建议让模型把角色名写进 assets[].name，"
            "以便与分镜 asset_refs 对齐"
        )
    else:
        for index, person in enumerate(character_sheet):
            path = f"$.character_sheet[{index}]"
            if not isinstance(person, Mapping):
                errors.append(_fail(path, "invalid_type", "角色登记表条目必须是对象"))
                continue
            name = _as_text(person.get("name")) or _as_text(person.get("speaker_id"))
            if not name:
                errors.append(_fail(path, "missing_field", "角色登记表条目缺少 name/speaker_id"))
                continue
            if normalize_name(name) in used_names:
                continue
            prompt = "，".join(
                part
                for part in (
                    _as_text(person.get("appearance")),
                    _as_text(person.get("costume")),
                )
                if part
            )
            if not prompt:
                warnings.append(
                    f"{path} 的 appearance/costume 都为空，无法生成人物图提示词，已跳过"
                )
                continue
            used_names.add(normalize_name(name))
            assets.append(
                {
                    "id": f"A{len(assets) + 1}",
                    "asset_type": "CHARACTER",
                    "name": name,
                    "prompt": prompt,
                }
            )
            warnings.append(
                f"{path} 已转换为 CHARACTER 素材（prompt 由 appearance/costume 拼接）"
            )

    if errors:
        raise ScriptValidationError(errors)
    if not assets:
        warnings.append("H3 手册结构未提供任何素材图提示词：assets 为空（仍会生成视频段）")

    chapter: dict[str, Any] = {
        "chapter_id": "CH001",
        "segments": [
            {"segment_index": 1, "prompt": video_prompt, "asset_refs": []}
        ],
    }
    title = _as_text(script.get("title")) or _as_text(script.get("task_type"))
    if title:
        chapter["title"] = title
    converted: dict[str, Any] = {
        "title": title or "H3 手册输出（已适配）",
        "chapters": [chapter],
        "assets": assets,
    }
    for key in ("task_type", "notes", "output_language", "character_sheet"):
        if key in script:
            converted[key] = script[key]
    warnings.append(
        "已把 H3 手册结构转换为标准剧本结构：video_prompt → chapters[0].segments[0].prompt；"
        "material_image_prompts → assets[]；asset_refs 为空（首尾帧请在导演台指定）"
    )
    return converted


def adapt_script_output(parsed: Mapping | dict) -> tuple[dict, tuple[str, ...], str]:
    """把模型输出适配为 :func:`validate_script` 认的规范形状。

    返回 ``(script, warnings, shape)``：

    * ``shape="script"`` —— 本来就是标准结构；
    * ``shape="legacy"`` —— 旧调用方形状（``type`` 键 / 章节级 assets）；
    * ``shape="handbook"`` —— H3 手册形状（``video_prompt`` + 素材图提示词）。

    ``warnings`` 必须由调用方写进结果元数据（例如
    ``ScriptPromptResult.meta["notes"]``），这样「字段被改名/合成」是**可见的**，
    不会被当成无损耗的解析。字段合法性仍由 :func:`validate_script` 严格判定。
    """
    if not isinstance(parsed, Mapping):
        raise ScriptValidationError(
            [_fail("$", "invalid_type", f"剧本必须是 JSON 对象，收到 {type(parsed).__name__}")]
        )

    original = copy.deepcopy(dict(parsed))
    warnings: list[str] = []
    script = _unwrap_envelopes(copy.deepcopy(original), warnings)
    script = _normalize_top_level(script, warnings)

    if _looks_like_script(script):
        shape = "legacy" if _is_legacy_shape(original) else "script"
        normalized = _normalize_script_records(script, warnings)
        normalized = _merge_chapter_assets(normalized, warnings)
        return normalized, tuple(warnings), shape

    if _looks_like_handbook(script):
        return _convert_handbook(script, warnings), tuple(warnings), "handbook"

    keys = ", ".join(sorted(str(key) for key in original)) or "（空对象）"
    raise ScriptValidationError(
        [
            _fail(
                "$",
                "unrecognized_output_shape",
                f"无法识别的输出结构：顶层键 {keys}。本项目契约要求顶层包含 assets 与 chapters；"
                "兼容旧形状（type 键 / 章节级 assets）与 H3 手册形状"
                "（video_prompt + material_image_prompts + character_sheet）",
            )
        ]
    )


def canonicalize_script(parsed: Mapping | dict) -> dict:
    """把**老形状**的剧本规整成 :func:`validate_script` 认的规范形状。

    ## 为什么需要它（这是接缝缺口的修复）

    实测发现两个形状在项目里并存，而且**接缝处对不上**：

    | 来源 | `assets` 放在哪 | 用什么键 |
    |---|---|---|
    | ``validate_script`` 的规范输出 | **顶层** | ``asset_type`` |
    | ``demo.py`` / 内置 ``DEMO_SCRIPT`` | **章节级** | ``type`` |

    后果是**假成功**：把经校验的规范剧本喂给 ``run_demo``，
    ``chapter_obj.get("assets", [])`` 一次都不进循环 ——
    素材的匹配/复用/疑似队列/生成/LOCK **整段静默跳过**，
    而 ``run_demo`` 仍返回 ``ok=True``。

    按 ``AGENTS.md``：「Normalize third-party or upstream return conventions
    at the integration boundary.」—— 就在边界上归一，两边都不用改形状。

    ## 规整规则（ROUND58 扩展后）

    1. **位置**：章节级 ``chapters[].assets`` 合并到顶层 ``assets``
       （顶层优先；按 ``id`` 去重，保持出现顺序）；
    2. **键名**：``type`` -> ``asset_type``，中文/别名字段 -> 规范字段
       （``素材`` -> ``assets``、``章节`` -> ``chapters``、``分镜`` -> ``segments`` 等）；
    3. **取值形态**：``asset_refs`` 逗号串 -> 数组、``segment_index``/``duration_s``
       数字串 -> 数字；缺失的 ``asset_refs`` 按空数组并记 warning；
    4. 不改动其它任何字段，也不做校验 —— 校验交给 ``validate_script``
       （这样「无效引用/重复 ID」仍然会被拦，不会因为规整而放过）。

    返回**深拷贝**，不改入参。需要 warnings 与形状名时用
    :func:`adapt_script_output`（本函数是它的无 warnings 版本）。
    """
    if not isinstance(parsed, Mapping):
        raise ScriptValidationError(
            [_fail("$", "invalid_type", f"剧本必须是 JSON 对象，收到 {type(parsed).__name__}")]
        )

    warnings: list[str] = []
    script = _normalize_top_level(copy.deepcopy(dict(parsed)), warnings)
    script = _normalize_script_records(script, warnings)
    return _merge_chapter_assets(script, warnings)


def validate_script(parsed: dict, *, existing_assets: Iterable | None = None,
                   allow_drop_invalid_refs: bool = False,
                   dropped_refs: list[str] | None = None) -> dict:
    """严格校验剧本结构，返回规范化的深拷贝。

    任何一处不合法都抛 :class:`ScriptValidationError`（携带**全部**错误），
    绝不返回部分结果、绝不「先跑再说」。
    """
    errors: list[dict] = []

    if not isinstance(parsed, Mapping):
        raise ScriptValidationError(
            [
                _fail(
                    "$",
                    "invalid_type",
                    f"剧本必须是 JSON 对象，收到 {type(parsed).__name__}",
                )
            ]
        )
    if not parsed:
        raise ScriptValidationError([_fail("$", "empty_script", "剧本为空对象")])

    script = copy.deepcopy(dict(parsed))

    # ---- 顶层可选字段 -------------------------------------------------
    title = script.get("title")
    if title is not None:
        checked = _check_text(title, "$.title", errors, field_name="title", required=False, allow_empty=True)
        script["title"] = checked if checked is not None else ""
    output_language = script.get("output_language")
    if output_language is not None:
        checked = _check_text(
            output_language, "$.output_language", errors,
            field_name="output_language", required=False, allow_empty=True, limit=64,
        )
        if checked is None:
            script.pop("output_language", None)
        else:
            script["output_language"] = checked

    # ---- assets -------------------------------------------------------
    assets_raw = script.get("assets")
    assets: list[dict] = []
    asset_ids: dict[str, int] = {}
    asset_names: dict[str, int] = {}
    if assets_raw is None:
        errors.append(_fail("$.assets", "missing_field", "缺少必填字段 assets（可以是空数组）"))
    elif not isinstance(assets_raw, list):
        errors.append(
            _fail("$.assets", "invalid_type", f"assets 必须是数组，收到 {type(assets_raw).__name__}")
        )
    else:
        for i, item in enumerate(assets_raw):
            base = f"$.assets[{i}]"
            if not isinstance(item, Mapping):
                errors.append(_fail(base, "invalid_type", "素材条目必须是对象"))
                continue
            record = copy.deepcopy(dict(item))
            asset_id = _check_text(
                record.get("id"), f"{base}.id", errors,
                field_name="id", required=True, limit=_CHAR_LIMIT_ID,
            )
            if asset_id is not None:
                if asset_id in asset_ids:
                    errors.append(
                        _fail(
                            f"{base}.id",
                            "duplicate_id",
                            f"素材 id 重复：{asset_id}（首次出现于 $.assets[{asset_ids[asset_id]}]）",
                        )
                    )
                else:
                    asset_ids[asset_id] = i
                record["id"] = asset_id
            asset_type = _enum_value(
                record.get("asset_type"), AssetType, f"{base}.asset_type", errors,
                field_name="asset_type", required=True,
            )
            if asset_type is not None:
                record["asset_type"] = asset_type
            name = _check_text(
                record.get("name"), f"{base}.name", errors,
                field_name="name", required=True, limit=_CHAR_LIMIT_NAME,
            )
            if name is not None:
                record["name"] = name
                norm = normalize_name(name)
                if not norm:
                    errors.append(
                        _fail(f"{base}.name", "invalid_format", f"name={name!r} 归一化后为空")
                    )
                elif norm in asset_names:
                    errors.append(
                        _fail(
                            f"{base}.name",
                            "duplicate_normalized_name",
                            f"素材名归一化后重复：{name!r}（归一化后 {norm!r}）与 "
                            f"$.assets[{asset_names[norm]}].name "
                            f"（匹配按归一化名进行，重复会导致程序无法确定性匹配）",
                        )
                    )
                else:
                    asset_names[norm] = i
                    record["normalized_name"] = norm
            prompt = _check_text(
                record.get("prompt"), f"{base}.prompt", errors,
                field_name="prompt", required=True, limit=_CHAR_LIMIT_PROMPT,
            )
            if prompt is not None:
                record["prompt"] = prompt
            source = _enum_value(
                record.get("source"), AssetSource, f"{base}.source", errors,
                field_name="source", required=False, default=AssetSource.AI,
            )
            record["source"] = source if source is not None else str(AssetSource.AI.value)
            scope = _enum_value(
                record.get("library_scope"), LibraryScope, f"{base}.library_scope", errors,
                field_name="library_scope", required=False, default=LibraryScope.PROJECT,
            )
            record["library_scope"] = (
                scope if scope is not None else str(LibraryScope.PROJECT.value)
            )
            for optional_text, limit in (
                ("negative_prompt", _CHAR_LIMIT_PROMPT),
                ("description", _CHAR_LIMIT_TEXT),
            ):
                if record.get(optional_text) is not None:
                    checked = _check_text(
                        record[optional_text], f"{base}.{optional_text}", errors,
                        field_name=optional_text, required=False, allow_empty=True, limit=limit,
                    )
                    if checked is None:
                        record.pop(optional_text, None)
                    else:
                        record[optional_text] = checked
            if record.get("tags") is not None:
                tags = record["tags"]
                if not isinstance(tags, list) or not all(_is_str(t) for t in tags):
                    errors.append(
                        _fail(
                            f"{base}.tags",
                            "invalid_type",
                            "tags 必须是字符串数组（分隔符解析器已负责把 text 转成数组）",
                        )
                    )
                else:
                    record["tags"] = [t.strip() for t in tags if t.strip()]
            assets.append(record)

    # ---- chapters / segments -----------------------------------------
    existing_ids, existing_names = _existing_name_set(existing_assets)
    ref_ids = set(asset_ids) | existing_ids
    ref_names = set(asset_names) | existing_names

    chapters_raw = script.get("chapters")
    chapters: list[dict] = []
    chapter_ids: dict[str, int] = {}
    if chapters_raw is None:
        errors.append(_fail("$.chapters", "missing_field", "缺少必填字段 chapters"))
    elif not isinstance(chapters_raw, list) or not chapters_raw:
        errors.append(
            _fail(
                "$.chapters",
                "invalid_type" if not isinstance(chapters_raw, list) else "empty_field",
                "chapters 必须是非空数组",
            )
        )
    else:
        for ci, chapter_raw in enumerate(chapters_raw):
            cpath = f"$.chapters[{ci}]"
            if not isinstance(chapter_raw, Mapping):
                errors.append(_fail(cpath, "invalid_type", "章节必须是对象"))
                continue
            chapter = copy.deepcopy(dict(chapter_raw))
            chapter_id = chapter.get("chapter_id")
            if chapter_id is None:
                chapter["chapter_id"] = f"CH{ci + 1:03d}"
            else:
                checked = _check_text(
                    chapter_id, f"{cpath}.chapter_id", errors,
                    field_name="chapter_id", required=True, limit=16,
                )
                if checked is not None:
                    if not _CH_ID_RE.match(checked):
                        errors.append(
                            _fail(
                                f"{cpath}.chapter_id",
                                "invalid_format",
                                f"chapter_id={checked!r} 不符合同 CH{{3位数字}}（如 CH001）",
                            )
                        )
                    elif checked in chapter_ids:
                        errors.append(
                            _fail(
                                f"{cpath}.chapter_id",
                                "duplicate_id",
                                f"chapter_id 重复：{checked}（首次出现于 $.chapters[{chapter_ids[checked]}]）",
                            )
                        )
                    else:
                        chapter_ids[checked] = ci
                    chapter["chapter_id"] = checked
            for field_name in ("title", "summary"):
                if chapter.get(field_name) is not None:
                    checked = _check_text(
                        chapter[field_name], f"{cpath}.{field_name}", errors,
                        field_name=field_name, required=False, allow_empty=True,
                    )
                    if checked is None:
                        chapter.pop(field_name, None)
                    else:
                        chapter[field_name] = checked

            segments_raw = chapter.get("segments")
            segments: list[dict] = []
            seg_indices: dict[int, int] = {}
            if segments_raw is None:
                errors.append(_fail(f"{cpath}.segments", "missing_field", "缺少必填字段 segments"))
            elif not isinstance(segments_raw, list) or not segments_raw:
                errors.append(
                    _fail(
                        f"{cpath}.segments",
                        "invalid_type" if not isinstance(segments_raw, list) else "empty_field",
                        "segments 必须是非空数组",
                    )
                )
            else:
                for si, seg_raw in enumerate(segments_raw):
                    spath = f"{cpath}.segments[{si}]"
                    if not isinstance(seg_raw, Mapping):
                        errors.append(_fail(spath, "invalid_type", "分镜段必须是对象"))
                        continue
                    segment = copy.deepcopy(dict(seg_raw))
                    index_value = segment.get("segment_index")
                    if index_value is None:
                        segment["segment_index"] = si + 1
                    elif not _is_int(index_value) or index_value < 1:
                        errors.append(
                            _fail(
                                f"{spath}.segment_index",
                                "invalid_format",
                                f"segment_index 必须是 >=1 的整数，收到 {index_value!r}",
                            )
                        )
                    else:
                        if index_value in seg_indices:
                            errors.append(
                                _fail(
                                    f"{spath}.segment_index",
                                    "duplicate_id",
                                    f"segment_index 重复：{index_value}"
                                    f"（首次出现于 ...segments[{seg_indices[index_value]}]）",
                                )
                            )
                        else:
                            seg_indices[index_value] = si
                        segment["segment_index"] = index_value

                    prompt = _check_text(
                        segment.get("prompt"), f"{spath}.prompt", errors,
                        field_name="prompt", required=True, limit=_CHAR_LIMIT_PROMPT,
                    )
                    if prompt is not None:
                        segment["prompt"] = prompt

                    refs_raw = segment.get("asset_refs")
                    refs_out: list[str] = []
                    if refs_raw is None:
                        errors.append(
                            _fail(f"{spath}.asset_refs", "missing_field", "缺少必填字段 asset_refs（可为空数组）")
                        )
                    elif not isinstance(refs_raw, list):
                        errors.append(
                            _fail(
                                f"{spath}.asset_refs",
                                "invalid_type",
                                f"asset_refs 必须是数组，收到 {type(refs_raw).__name__}",
                            )
                        )
                    else:
                        for ri, ref in enumerate(refs_raw):
                            rpath = f"{spath}.asset_refs[{ri}]"
                            key = _ref_key(ref)
                            if key is None:
                                if allow_drop_invalid_refs:
                                    if dropped_refs is not None:
                                        dropped_refs.append(f"{rpath}: 无法识别的引用 {ref!r}（已丢弃）")
                                    continue
                                errors.append(
                                    _fail(rpath, "invalid_reference", f"无法识别的引用：{ref!r}")
                                )
                                continue
                            if key in ref_ids or normalize_name(key) in ref_names:
                                refs_out.append(key)
                                continue
                            if allow_drop_invalid_refs:
                                if dropped_refs is not None:
                                    dropped_refs.append(
                                        f"{rpath}: 引用了不存在的素材 {key!r}（已丢弃，不参与生成）")
                                continue
                            errors.append(
                                _fail(
                                    rpath,
                                    "invalid_reference",
                                    f"引用了不存在的素材 {key!r}：既不在本次输出的 assets 中，"
                                    f"也不在已注入的既有素材清单中（R1：不得凭 AI 判断存在性）",
                                )
                            )
                    segment["asset_refs"] = refs_out

                    duration = segment.get("duration_s")
                    if duration is not None:
                        if not _is_number(duration) or duration <= 0:
                            errors.append(
                                _fail(
                                    f"{spath}.duration_s",
                                    "invalid_format",
                                    f"duration_s 必须是 >0 的数字，收到 {duration!r}",
                                )
                            )
                    for frame_field in ("first_frame_ref", "last_frame_ref"):
                        if segment.get(frame_field) is not None:
                            checked = _check_text(
                                segment[frame_field], f"{spath}.{frame_field}", errors,
                                field_name=frame_field, required=False, limit=_CHAR_LIMIT_ID,
                            )
                            if checked is None:
                                segment.pop(frame_field, None)
                                continue
                            key = _ref_key(checked)
                            if key is None or not (
                                key in ref_ids or normalize_name(key) in ref_names
                            ):
                                if allow_drop_invalid_refs:
                                    if dropped_refs is not None:
                                        dropped_refs.append(
                                            f"{spath}.{frame_field}: 引用了不存在的素材 "
                                            f"{checked!r}（已丢弃）")
                                    segment.pop(frame_field, None)
                                    continue
                                errors.append(
                                    _fail(
                                        f"{spath}.{frame_field}",
                                        "invalid_reference",
                                        f"{frame_field}={checked!r} 引用了不存在的素材",
                                    )
                                )
                            else:
                                segment[frame_field] = key
                    if segment.get("generation_params") is not None and not isinstance(
                        segment["generation_params"], Mapping
                    ):
                        errors.append(
                            _fail(
                                f"{spath}.generation_params",
                                "invalid_type",
                                "generation_params 必须是对象",
                            )
                        )
                    segments.append(segment)
            chapter["segments"] = segments
            chapters.append(chapter)

    if errors:
        raise ScriptValidationError(errors)

    script["chapters"] = chapters
    script["assets"] = assets
    script.setdefault("title", "")
    return script
