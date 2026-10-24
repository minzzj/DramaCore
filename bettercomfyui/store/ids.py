"""标识符生成 —— docs/CONTRACTS.md 第 2 节的**唯一入口**。

硬约束
------
* **R6**：短码（``uuid8`` 段）生成后必须在真实索引里查重，重复就重新生成。
  本模块提供 :func:`short_code_conflict` / :func:`unique_short_code`，
  所有 ``new_*`` 函数都支持传入 ``existing``（索引快照或判定函数）自动重试。
* **R7**：ID 里不得包含可变状态（``待验证/已通过/已降级`` 一律是独立字段）。
  因此本模块只接受**稳定**输入：项目名、类型、序号、归一化名、时间戳。
* ``content_hash`` 只有 :func:`content_hash_of` 一个计算入口（sha256 流式读取），
  格式必须是 ``sha256:{64位小写hex}``，否则 :func:`parse_content_hash` 抛错。

本模块不得 import prompt/review/director/pipeline。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..models.entities import new_token
from ..models.enums import AssetType, CounterKind
from .atomic import canonical_json
from .errors import InvalidIdentifierError, StoreError, parse_enum

__all__ = [
    "CONTENT_HASH_RE",
    "PROJECT_ID_RE",
    "is_valid_project_id",
    "new_project_id",
    "new_finalization_token",
    "safe_token",
    "short_code_conflict",
    "unique_short_code",
    "parse_version",
    "format_version",
    "next_version",
    "version_sort_key",
    "content_hash_of",
    "parse_content_hash",
    "is_valid_content_hash",
    "lock_id",
    "new_asset_id",
    "new_suspect_id",
    "chapter_id",
    "parse_chapter_index",
    "segment_id",
    "attempt_id",
    "audit_id",
    "review_cycle_id",
    "mod_id",
    "new_cascade_id",
    "cnt_id",
    "arb_id",
    "abort_id",
    "idempotency_key",
]

PROJECT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^v(\d+)$")
_CHAPTER_RE = re.compile(r"^CH(\d{1,3})$", re.IGNORECASE)
_HASH_CHUNK = 1024 * 1024

#: 归一化名/项目名里允许保留的字符（其余一律转义为 ``_``）：
#: 字母、数字、CJK 等 ``str.isalnum()`` 为真的字符，以及 ``-`` ``_``。
_UNSAFE_REPLACEMENTS = "_"
_MAX_TOKEN_LEN = 64


# ---------------------------------------------------------------------------
# 基础校验
# ---------------------------------------------------------------------------


def is_valid_project_id(value: Any) -> bool:
    """``project_id`` 必须是 32 位小写 UUID4 hex（无连字符）。"""
    return isinstance(value, str) and bool(PROJECT_ID_RE.match(value))


def new_project_id() -> str:
    """``project_id`` = 小写 UUID4 hex（32 字符，无连字符）。"""
    return uuid.uuid4().hex


def new_finalization_token() -> str:
    """``finalization_token`` = 小写 UUID4 hex（用于审核结论的条件更新）。"""
    return new_token()


def safe_token(
    value: Any,
    *,
    kind: str,
    fallback: str | None = None,
    max_length: int = _MAX_TOKEN_LEN,
) -> str:
    """把任意文本转成可安全放进 ID / 文件名的一段 token。

    保留 ``isalnum()`` 的字符（含 CJK），其余（空白、标点、路径分隔符、控制字符）
    统一转成 ``_`` 并压缩连续 ``_``。为空时使用 ``fallback``，``fallback`` 也为空则抛错。
    """
    if value is None:
        text = ""
    else:
        text = unicodedata.normalize("NFC", str(value)).strip()
    if not text:
        if fallback:
            return fallback
        raise InvalidIdentifierError(f"{kind} 不能为空", kind=kind)

    out: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append(_UNSAFE_REPLACEMENTS)
    token = re.sub(r"_{2,}", "_", "".join(out)).strip("_-")
    if len(token) > max_length:
        token = token[:max_length].strip("_-")
    if not token:
        if fallback:
            return fallback
        raise InvalidIdentifierError(
            f"{kind} 归一后为空（全部是非法字符）", kind=kind, value=str(value)
        )
    return token


def _require_non_empty(value: Any, *, kind: str) -> str:
    if value is None:
        raise InvalidIdentifierError(f"{kind} 不能为空", kind=kind)
    text = str(value).strip()
    if not text:
        raise InvalidIdentifierError(f"{kind} 不能为空", kind=kind)
    return text


# ---------------------------------------------------------------------------
# R6：短码冲突检测
# ---------------------------------------------------------------------------


def short_code_conflict(candidate: str, existing: Any) -> bool:
    """判断短码是否与既有集合冲突。

    ``existing`` 可以是：``None``（不检查）、可调用对象（判定函数）、
    ``dict``/``set``/``list`` 等容器（成员检测）。
    """
    if existing is None:
        return False
    if callable(existing):
        return bool(existing(candidate))
    if isinstance(existing, str):
        raise InvalidIdentifierError(
            "existing 不能是字符串（会被误当作字符集合）；请传容器或判定函数",
            kind="existing",
        )
    return candidate in existing


def unique_short_code(
    factory: Callable[[], str],
    existing: Any = None,
    *,
    max_attempts: int = 64,
) -> str:
    """R6 落地：反复调用 ``factory`` 直到得到一个不冲突的短码。

    重试耗尽仍冲突则抛错（绝不返回可能重复的 ID —— 重复 ID 会导致素材串用）。
    """
    if max_attempts < 1:
        raise InvalidIdentifierError("max_attempts 必须 >= 1", max_attempts=max_attempts)
    for attempt in range(1, max_attempts + 1):
        candidate = factory()
        if not short_code_conflict(candidate, existing):
            return candidate
    raise StoreError(
        "短码冲突检测重试耗尽（R6）：索引里已有同名短码，请检查索引或扩大尝试次数",
        attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def parse_version(value: Any) -> int:
    """解析版本号：接受 ``"v1"`` / ``1`` / ``"1"``，返回 int。非法一律抛错。"""
    if isinstance(value, bool):
        raise InvalidIdentifierError("version 不能是 bool", version=value)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        match = _VERSION_RE.match(text)
        if match:
            number = int(match.group(1))
        elif text.isdigit():
            number = int(text)
        else:
            raise InvalidIdentifierError(
                "version 格式非法（要求 v{int}，如 v1）", version=value
            )
    else:
        raise InvalidIdentifierError("version 类型非法（要求 str 或 int）", version=value)
    if number < 1:
        raise InvalidIdentifierError("version 必须 >= 1", version=value)
    return number


def format_version(value: Any) -> str:
    """返回规范形式 ``v{int}``。"""
    return f"v{parse_version(value)}"


def next_version(value: Any) -> str:
    """``v{int}`` -> ``v{int+1}``。"""
    return f"v{parse_version(value) + 1}"


def version_sort_key(value: Any) -> tuple[int, str]:
    """稳定排序键（版本号数字大小）。"""
    return (parse_version(value), str(value))


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def content_hash_of(path: Path | str) -> str:
    """计算素材文件内容指纹：``sha256:{64位小写hex}``（流式读取，不占内存）。"""
    target = Path(path)
    if not target.is_file():
        raise StoreError("content_hash_of 的目标不是普通文件", path=str(target))
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def is_valid_content_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(CONTENT_HASH_RE.match(value))


def parse_content_hash(value: Any) -> str:
    """校验并返回 content_hash；格式非法直接抛错（绝不静默补全）。"""
    if not is_valid_content_hash(value):
        raise InvalidIdentifierError(
            "content_hash 格式非法（要求 sha256:{64位小写hex}）",
            content_hash=value if isinstance(value, str) else repr(value),
        )
    return str(value)


# ---------------------------------------------------------------------------
# LOCK / 审核 / 段 相关 ID
# ---------------------------------------------------------------------------


def lock_id(asset_id: Any, version: Any) -> str:
    """``LOCK_{asset_id}_{version}``。"""
    return f"LOCK_{_require_non_empty(asset_id, kind='asset_id')}_{format_version(version)}"


def new_asset_id(
    project_name: Any,
    asset_type: Any,
    seq: Any,
    *,
    existing: Any = None,
    max_attempts: int = 64,
) -> str:
    """``{项目名}_{类型}_{序号}_{uuid8}``。

    * ``project_name`` / ``asset_type`` 会被转义成文件系统安全 token；
    * 类型取枚举小写值（目录名与 ID 一致，避免大小写两套命名）；
    * ``existing`` 传入索引（asset_id 集合）时自动做 R6 查重重试。
    """
    name_token = safe_token(project_name, kind="project_name", fallback="project", max_length=24)
    if isinstance(asset_type, AssetType):
        type_token = asset_type.value.lower()
    else:
        type_token = parse_enum(AssetType, asset_type, kind="asset_type").value.lower()
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise InvalidIdentifierError("seq 必须是 >= 1 的整数", seq=seq)
    prefix = f"{name_token}_{type_token}_{seq}"
    return unique_short_code(
        lambda: f"{prefix}_{_uuid8()}", existing, max_attempts=max_attempts
    )


def new_suspect_id(
    normalized_name: Any,
    *,
    timestamp: str | None = None,
    existing: Any = None,
    max_attempts: int = 64,
) -> str:
    """``SUSPECT_{归一化名}_{时间戳}_{uuid8}``。

    ``timestamp`` 缺省为本地时间 ``%Y%m%d%H%M%S``；显式传入便于测试确定性。
    """
    name_token = safe_token(
        normalized_name, kind="normalized_name", fallback="unnamed", max_length=40
    )
    stamp = timestamp or datetime.now().strftime("%Y%m%d%H%M%S")
    stamp = safe_token(stamp, kind="timestamp", max_length=32)
    return unique_short_code(
        lambda: f"SUSPECT_{name_token}_{stamp}_{_uuid8()}",
        existing,
        max_attempts=max_attempts,
    )


def chapter_id(seq: Any) -> str:
    """``CH{3位序号}``，如 ``CH001``。"""
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise InvalidIdentifierError("chapter 序号必须是整数", seq=seq)
    if not 1 <= seq <= 999:
        raise InvalidIdentifierError("chapter 序号必须在 1..999", seq=seq)
    return f"CH{seq:03d}"


def parse_chapter_index(value: Any) -> int:
    """解析章节序号：接受 ``1`` / ``"1"`` / ``"CH001"``。"""
    if isinstance(value, bool):
        raise InvalidIdentifierError("chapter 不能是 bool", chapter=value)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        match = _CHAPTER_RE.match(text)
        if match:
            number = int(match.group(1))
        elif text.isdigit():
            number = int(text)
        else:
            raise InvalidIdentifierError(
                'chapter 格式非法（要求整数或 "CH{序号}"，如 CH001）', chapter=value
            )
    else:
        raise InvalidIdentifierError("chapter 类型非法（要求 str 或 int）", chapter=value)
    if not 1 <= number <= 999:
        raise InvalidIdentifierError("chapter 序号必须在 1..999", chapter=value)
    return number


def segment_id(project_id: Any, chapter: Any, seg: Any) -> str:
    """``{project_id}_CH{n}_SEG{m}``（逻辑段 ID，重生成不变）。

    ``n``/``m`` 使用**十进制数字本体**（不补零），与契约第 2 节的 ``{n}``/``{m}`` 一致；
    ``chapter`` 可以传 ``1`` / ``"CH001"`` 两种形式。
    """
    if not is_valid_project_id(project_id):
        raise InvalidIdentifierError(
            "project_id 必须是 32 位小写 UUID4 hex", project_id=project_id
        )
    chapter_number = parse_chapter_index(chapter)
    if isinstance(seg, bool) or not isinstance(seg, int) or seg < 1:
        raise InvalidIdentifierError("seg 必须是 >= 1 的整数", seg=seg)
    return f"{project_id}_CH{chapter_number}_SEG{seg}"


def attempt_id(segment_id_value: Any, version: Any, seq: Any) -> str:
    """``ATTEMPT_{segment_id}_{version}_{序号}``。"""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise InvalidIdentifierError("attempt 序号必须是 >= 1 的整数", seq=seq)
    return (
        f"ATTEMPT_{_require_non_empty(segment_id_value, kind='segment_id')}_"
        f"{format_version(version)}_{seq}"
    )


def audit_id(segment_id_value: Any, version: Any) -> str:
    """``AUDIT_{segment_id}_{version}``。"""
    return (
        f"AUDIT_{_require_non_empty(segment_id_value, kind='segment_id')}_"
        f"{format_version(version)}"
    )


def review_cycle_id(object_type: Any, object_id: Any, version: Any) -> str:
    """``{对象类型}:{对象ID}:{version}``（如 ``asset:{asset_id}:v1``）。"""
    kind_token = safe_token(object_type, kind="object_type", max_length=32)
    obj_token = _require_non_empty(object_id, kind="object_id")
    if ":" in obj_token:
        raise InvalidIdentifierError("object_id 不能包含 ':'", object_id=obj_token)
    return f"{kind_token}:{obj_token}:{format_version(version)}"


def mod_id(object_type: Any, object_id: Any, *, uuid8: str | None = None, existing: Any = None) -> str:
    """``MOD_{对象类型}_{对象ID}_{uuid8}``。"""
    kind_token = safe_token(object_type, kind="object_type", max_length=32)
    obj_token = _require_non_empty(object_id, kind="object_id")
    if uuid8 is None:
        return unique_short_code(lambda: f"MOD_{kind_token}_{obj_token}_{_uuid8()}", existing)
    _require_uuid8(uuid8)
    return f"MOD_{kind_token}_{obj_token}_{uuid8}"


def new_cascade_id(segment_id_value: Any, *, existing: Any = None) -> str:
    """``CASCADE_{segment_id}_{uuid8}``。"""
    seg_token = _require_non_empty(segment_id_value, kind="segment_id")
    return unique_short_code(lambda: f"CASCADE_{seg_token}_{_uuid8()}", existing)


def cnt_id(object_id: Any, kind: Any) -> str:
    """``CNT_{对象ID}_{计数类型}``；计数类型必须是 CNT1/CNT2/CNT3。"""
    obj_token = _require_non_empty(object_id, kind="object_id")
    counter = parse_enum(CounterKind, kind, kind="counter_kind")
    return f"CNT_{obj_token}_{counter.value}"


def arb_id(human_review_id: Any, model_review_id: Any) -> str:
    """``ARB_{人工审核ID}_{模型审核ID}``。"""
    human = _require_non_empty(human_review_id, kind="human_review_id")
    model = _require_non_empty(model_review_id, kind="model_review_id")
    for value in (human, model):
        if any(ch.isspace() for ch in value):
            raise InvalidIdentifierError("审核 ID 不能包含空白字符", value=value)
    return f"ARB_{human}_{model}"


def abort_id(review_record_id: Any) -> str:
    """``ABORT_{审核记录ID}``。"""
    return f"ABORT_{_require_non_empty(review_record_id, kind='review_record_id')}"


def idempotency_key(namespace: Any, payload: Any) -> str:
    """``{namespace}:{sha256(canonical_json)}``。

    ``canonical_json`` 保证同一逻辑请求（键序/空白/浮点写法不同）得到同一 key，
    因此可以安全地用作「重复提交检测」。
    """
    ns_token = safe_token(namespace, kind="namespace", max_length=32)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{ns_token}:{digest}"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _uuid8() -> str:
    """8 位小写 hex 短码（测试可 monkeypatch 本函数以构造冲突）。"""
    return secrets.token_hex(4)


def _require_uuid8(value: Any) -> str:
    text = _require_non_empty(value, kind="uuid8")
    if not re.fullmatch(r"[0-9a-f]{8}", text):
        raise InvalidIdentifierError("uuid8 必须是 8 位小写 hex", uuid8=text)
    return text
