"""确定性 JSON、原子写、加锁读改写（docs/CONTRACTS.md 第 3 节）。

本模块是整个 store 层「不丢数据」的地基，任何绕过它的 ``meta.json`` 写入
都是缺陷（契约第 3 节明确要求）。

不变量
------
1. :func:`canonical_json` 对同一逻辑对象两次调用**逐字节相同**
   （``sort_keys=True`` + 固定分隔符 + ``ensure_ascii=False`` + 拒绝 NaN/Inf）。
2. :func:`atomic_write_json` 要么完整写出新内容，要么保持目标文件**旧内容不变**：
   先写同目录临时文件 -> ``flush`` -> ``fsync`` -> ``os.replace``（同文件系统内原子）。
3. :func:`locked_update_json` 在**整个 read-modify-write 周期**持锁，
   因此并发写入不会丢失更新。

本模块不得 import prompt/review/director/pipeline。
"""

from __future__ import annotations

import copy
import json
import math
import os
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, time as _dt_time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from ..models.entities import now_iso  # 统一后端时间戳的唯一实现（models 层）
from .errors import (
    CorruptFileError,
    SerializationError,
    StoreError,
)
from .lockfile import FileLock, file_lock, lock_path_for

__all__ = [
    "canonical_json",
    "atomic_write_json",
    "locked_update_json",
    "file_lock",
    "FileLock",
    "lock_path_for",
    "read_json",
    "now_iso",
]

_MISSING = object()

#: ``os.replace`` 遇到 Windows 共享冲突时的有界重试窗口（秒）
_REPLACE_RETRY_S = 5.0

#: 读文件遇到 Windows 共享冲突时的有界重试窗口（秒）
_READ_RETRY_S = 2.0


# ---------------------------------------------------------------------------
# 确定性 JSON
# ---------------------------------------------------------------------------


def _canonicalize(obj: Any, _stack: frozenset[int] = frozenset()) -> Any:
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise SerializationError("canonical_json 拒绝 NaN/Infinity（不可确定性序列化）")
        if obj == 0.0:
            return 0.0  # -0.0 归一为 0.0
        return obj
    if isinstance(obj, Enum):
        return _canonicalize(obj.value, _stack)
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (datetime, date, _dt_time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    if isinstance(obj, Mapping):
        marker = id(obj)
        if marker in _stack:
            raise SerializationError("canonical_json 检测到循环引用，无法序列化")
        _stack = _stack | {marker}
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, Enum):
                str_key = str(key.value)
            elif isinstance(key, str):
                str_key = key
            elif isinstance(key, (int, float, bool)) or key is None:
                str_key = str(key)
            else:
                raise SerializationError(
                    "canonical_json 的键必须是 str/int/float/bool/None/Enum",
                    key_type=type(key).__name__,
                )
            if str_key in result:
                raise SerializationError(
                    "canonical_json 检测到归一化后的键冲突（会静默丢数据）",
                    key=str_key,
                )
            result[str_key] = _canonicalize(value, _stack)
        return result
    if isinstance(obj, (list, tuple)):
        marker = id(obj)
        if marker in _stack:
            raise SerializationError("canonical_json 检测到循环引用，无法序列化")
        _stack = _stack | {marker}
        return [_canonicalize(item, _stack) for item in obj]
    if isinstance(obj, (set, frozenset)):
        items = [_canonicalize(item, _stack) for item in obj]
        # 集合无序 -> 用元素的规范 JSON 排序，保证跨进程确定性
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    raise SerializationError(
        "canonical_json 不支持该类型（请显式转换成 JSON 原生类型）",
        type=type(obj).__name__,
    )


def canonical_json(obj: Any) -> str:
    """确定性 JSON 文本。

    ``sort_keys=True, separators=(',',':'), ensure_ascii=False``，float 归一化
    （拒绝 NaN/Inf，``-0.0`` -> ``0.0``）。同一逻辑对象两次调用字节相同。
    """
    normalized = _canonicalize(obj)
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - 兜底
        raise SerializationError(f"canonical_json 序列化失败: {exc}") from exc


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _read_bytes_with_retry(target: Path, *, retry_s: float = _READ_RETRY_S) -> bytes | None:
    """读文件字节；返回 ``None`` 表示文件不存在。

    Windows 上 ``os.replace`` 换名的一瞬间，其他进程打开该名字会短暂报
    ``PermissionError [WinError 32]``（共享冲突）。读方必须有界重试，
    否则「并发写 + 有人读」会偶发地把正常读取变成硬失败。
    """
    deadline = time.monotonic() + retry_s
    delay = 0.002
    while True:
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(0.05, delay * 2)


def read_json(
    path: Path | str,
    default: Any = _MISSING,
    *,
    require_dict: bool = False,
) -> Any:
    """读取 JSON 文件。

    * 文件不存在 / 为空：返回 ``default``（未提供 ``default`` 则抛错）；
    * 解析失败或顶层类型不符：抛 :class:`CorruptFileError`，**绝不当作空内容继续**；
    * 容忍 UTF-8 BOM（用 ``utf-8-sig`` 解码）；
    * 容忍 Windows 换名瞬间的共享冲突（有界重试）。
    """
    target = Path(path)
    try:
        raw = _read_bytes_with_retry(target)
    except PermissionError as exc:
        raise StoreError(
            "读取 JSON 失败：文件被其他进程占用（重试后仍失败）", path=str(target)
        ) from exc
    except OSError as exc:
        raise StoreError(f"读取 JSON 失败: {exc}", path=str(target)) from exc

    if raw is None:
        if default is _MISSING:
            raise StoreError("JSON 文件不存在", path=str(target)) from None
        return copy.deepcopy(default)

    if not raw.strip():
        if default is _MISSING:
            raise CorruptFileError("JSON 文件为空", path=str(target))
        return copy.deepcopy(default)

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise CorruptFileError(f"JSON 文件不是合法 UTF-8: {exc}", path=str(target)) from exc
    except json.JSONDecodeError as exc:
        raise CorruptFileError(
            f"JSON 解析失败: {exc}", path=str(target), position=getattr(exc, "pos", None)
        ) from exc

    if require_dict and not isinstance(data, dict):
        raise CorruptFileError(
            "JSON 顶层必须是对象", path=str(target), actual=type(data).__name__
        )
    return data


# ---------------------------------------------------------------------------
# 写（原子）
# ---------------------------------------------------------------------------


def _tmp_candidates(target: Path) -> list[Path]:
    """临时文件候选名。

    契约要求「同目录 ``{name}.tmp``」，因此**首选**固定名以保证逐字一致；
    两个进程同时写同一目标时，固定名会被对方占用（``O_EXCL`` 失败，说明
    对方正在写），此时回落到带 ``pid + 随机后缀`` 的同目录临时名，
    避免两个写者交错写同一个 tmp 而互相破坏内容。
    """
    return [
        target.with_name(target.name + ".tmp"),
        target.with_name(f"{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"),
    ]


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # Windows 不支持对目录 fsync
        return
    try:  # pragma: no cover - POSIX 分支
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace_with_retry(tmp: Path, target: Path) -> None:
    """``os.replace`` + Windows 共享冲突重试。

    Windows 上 ``os.replace``（MoveFileEx）要求对目标有 delete 权限：只要**另一个
    进程正打开着目标文件读**（Python 的 ``open`` 默认不共享 delete），替换就会
    报 ``PermissionError [WinError 32]``。meta.json 被读的机会很多，因此这里必须
    做有界重试；重试期限内仍失败则抛错（绝不退化成「直接覆盖写」）。
    """
    deadline = time.monotonic() + _REPLACE_RETRY_S
    delay = 0.002
    while True:
        try:
            os.replace(os.fspath(tmp), os.fspath(target))
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(0.05, delay * 2)


def atomic_write_json(path: Path | str, obj: Any, *, fsync: bool = True) -> None:
    """原子写 JSON：同目录 tmp -> flush -> fsync -> ``os.replace``。

    失败时目标文件保持旧内容（读方永远看不到半截文件）。序列化在创建任何
    文件之前完成，因此序列化错误不会留下垃圾文件、也不会动目标文件。
    """
    target = Path(path)
    text = canonical_json(obj)  # 先序列化：失败则完全不触碰磁盘
    data = text.encode("utf-8")

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp: Path | None = None
    created = False
    for candidate in _tmp_candidates(target):
        try:
            fd = os.open(os.fspath(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        tmp = candidate
        created = True
        break
    if tmp is None:  # pragma: no cover - 极端情况
        raise StoreError("无法创建原子写临时文件", path=str(target))

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        _replace_with_retry(tmp, target)
        tmp = None
    except BaseException:
        if created and tmp is not None:
            try:
                os.unlink(os.fspath(tmp))
            except OSError:
                pass
        raise
    if fsync:
        _fsync_dir(parent)


# ---------------------------------------------------------------------------
# 加锁读改写
# ---------------------------------------------------------------------------


def locked_update_json(
    path: Path | str,
    mutator: Callable[[dict], dict],
    *,
    timeout_s: float = 30.0,
) -> dict:
    """在**整个 read-modify-write 周期**持锁地更新 JSON 对象。

    * ``mutator`` 收到当前内容的**深拷贝**，必须返回新的 dict；
      返回 ``None`` 视为缺陷并抛错（否则会静默丢写）。
    * ``mutator`` 抛异常时目标文件**保持不变**。
    * 锁文件为同目录 ``.{文件名}.lock``；超时抛
      :class:`~bettercomfyui.store.errors.LockTimeoutError`（``StoreError`` 子类）。
    * 文件不存在或为空时按 ``{}`` 处理（更新语义下这是合理的初始状态）。
    """
    if not callable(mutator):
        raise StoreError("mutator 必须可调用", mutator=type(mutator).__name__)

    target = Path(path)
    with file_lock(lock_path_for(target), timeout_s=timeout_s):
        current = read_json(target, default={}, require_dict=True)
        working = copy.deepcopy(current)
        updated = mutator(working)
        if updated is None:
            raise StoreError(
                "mutator 必须返回新的 dict；返回 None 会造成静默丢写",
                path=str(target),
            )
        if not isinstance(updated, dict):
            raise StoreError(
                "mutator 的返回值必须是 dict",
                path=str(target),
                actual=type(updated).__name__,
            )
        atomic_write_json(target, updated)
        return updated
