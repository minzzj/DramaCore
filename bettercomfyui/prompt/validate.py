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
    "validate_script",
    "SUPPORTED_OUTPUT_FORMATS",
    "SCRIPT_STRUCTURE_VERSION",
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


def _try_parse_json(raw: str) -> tuple[dict, str] | None:
    """多策略 JSON 提取，返回 ``(dict, 来源说明)`` 或 ``None``（表示全部策略不适用）。

    ``None`` 的语义是「本输入不是合法 JSON」，由调用方抛 ``invalid_json``
    —— 这里不吞错：失败信息会在异常里体现，不会退化成「解析出半个结果」。
    """
    for source, text in _json_dict_candidates(raw):
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue  # 预期控制流：本策略不适用，继续尝试下一策略（最终失败->invalid_json）
        if isinstance(value, dict):
            return value, source
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

    ## 规整规则

    1. **位置**：章节级 ``chapters[].assets`` 合并到顶层 ``assets``
       （顶层优先；按 ``id`` 去重，保持出现顺序）；
    2. **键名**：``type`` -> ``asset_type``（已经叫 ``asset_type`` 的不动）；
    3. 不改动其它任何字段，也不做校验 —— 校验交给 ``validate_script``
       （这样「无效引用/重复 ID」仍然会被拦，不会因为规整而放过）。

    返回**深拷贝**，不改入参。
    """
    if not isinstance(parsed, Mapping):
        raise ScriptValidationError(
            [_fail("$", "invalid_type",
                   f"剧本必须是 JSON 对象，收到 {type(parsed).__name__}")]
        )

    script = copy.deepcopy(dict(parsed))
    chapters = script.get("chapters")
    if not isinstance(chapters, list):
        chapters = []

    def _normalize_asset(item: Any) -> Any:
        if not isinstance(item, Mapping):
            return item
        rec = dict(item)
        if "asset_type" not in rec and "type" in rec:
            rec["asset_type"] = rec.pop("type")
        return rec

    merged: list[dict] = []
    seen_ids: set[str] = set()

    def _absorb(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            rec = _normalize_asset(item)
            if not isinstance(rec, Mapping):
                continue
            key = str(rec.get("id") or rec.get("name") or "")
            if key and key in seen_ids:
                continue          # 同一素材在两处都写了：保留先出现的
            if key:
                seen_ids.add(key)
            merged.append(dict(rec))

    # 顶层优先，然后按章节顺序吸收章节级的
    _absorb(script.get("assets"))
    for ch in chapters:
        if isinstance(ch, Mapping):
            _absorb(ch.get("assets"))
            # 吸收后**移除章节级副本**：留着会让人（和下游）以为还有第二份，
            # 也可能被别的代码再读一次导致重复处理。规整后只有顶层一处。
            if "assets" in ch:
                ch.pop("assets", None)

    script["assets"] = merged
    return script


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
