"""store 桥接层：优先使用 `bettercomfyui.store` 的实现，store 未就绪时使用契约等价回退。

为什么存在这一层
----------------
CONTRACTS §2/§3 规定 ID 生成与原子写只能由 `store/` 提供，review 层不得自带第二套真源。
但 review 与 store 是并行开发的：store 未落地时 review 必须仍可独立运行与测试。
因此本模块按以下规则工作：

1. 若 `bettercomfyui.store.ids` / `bettercomfyui.store.atomic` **可导入** → 直接调用 store，
   错误原样抛出（不吞异常、不静默换实现对）。
2. 若模块**不存在** → 使用契约等价的本地回退实现，并记录一次 WARN 日志。
3. `ids_source()` / `atomics_source()` 返回 `"store"` 或 `"fallback"`，
   验收脚本与测试可直接断言当前走的是哪条路径——回退绝不静默。

回退实现仅覆盖 review 层用到的部分，语义与 CONTRACTS §2/§3 完全一致。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import StoreBridgeError

_LOG = logging.getLogger(__name__)

_STORE_MODULES = {"ids": "bettercomfyui.store.ids", "atomic": "bettercomfyui.store.atomic"}

_cache: dict[str, Any] = {}
_cache_lock = threading.RLock()
_warned = {"ids": False, "atomic": False}


def reset_store_cache() -> None:
    """测试/热接入用：清空缓存，下一次调用重新尝试导入 store。"""
    with _cache_lock:
        _cache.clear()
        _warned["ids"] = False
        _warned["atomic"] = False


def _store_module(which: str):
    key = f"mod:{which}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        mod = importlib.import_module(_STORE_MODULES[which])
    except Exception as exc:  # 模块不存在 / 导入期错误 -> store 未就绪
        _LOG.debug("store 模块 %s 不可用：%s", _STORE_MODULES[which], exc)
        mod = None
    with _cache_lock:
        _cache[key] = mod
    return mod


def _store_fn(which: str, func: str):
    mod = _store_module(which)
    if mod is None:
        return None
    return getattr(mod, func, None)


def _warn_fallback(which: str) -> None:
    with _cache_lock:
        if _warned[which]:
            return
        _warned[which] = True
    _LOG.warning(
        "bettercomfyui.store.%s 尚未就绪：review 层使用契约等价回退实现（非第二真源，"
        "store 落地后自动切换到 store）。",
        which,
    )


def ids_source() -> str:
    """返回当前 ID 生成来源：`"store"` 或 `"fallback"`。"""
    return "store" if _store_module("ids") is not None else "fallback"


def atomics_source() -> str:
    """返回当前原子写/文件锁来源：`"store"` 或 `"fallback"`。"""
    return "store" if _store_module("atomic") is not None else "fallback"


# ---------------------------------------------------------------------------
# canonical_json（CONTRACTS §3）
# ---------------------------------------------------------------------------


def _default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, tuple):
        return list(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"canonical_json 不支持的类型：{type(obj).__name__}")


def _fallback_canonical_json(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


def canonical_json(obj: Any) -> str:
    """确定性 JSON：同一逻辑对象两次调用必须得到完全相同的字符串。"""
    fn = _store_fn("atomic", "canonical_json")
    if fn is not None:
        return fn(obj)
    _warn_fallback("atomic")
    return _fallback_canonical_json(obj)


# ---------------------------------------------------------------------------
# 文件锁（CONTRACTS §3）：跨进程 + 同进程多线程安全 + 同线程可重入
# ---------------------------------------------------------------------------

_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_nesting = threading.local()


def _process_lock(key: str) -> threading.RLock:
    with _cache_lock:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _lock_key(lock_path: Path) -> str:
    resolved = str(lock_path.resolve())
    return resolved.lower() if os.name == "nt" else resolved


def _os_lock(fd: int, lock_path: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + float(timeout_s)
    if os.name == "nt":  # pragma: no cover - 平台分支
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreBridgeError(f"获取文件锁超时：{lock_path}")
                time.sleep(0.005)
    else:  # pragma: no cover - 平台分支
        import fcntl

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreBridgeError(f"获取文件锁超时：{lock_path}")
                time.sleep(0.005)


def _os_unlock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - 平台分支
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - 平台分支
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _fallback_file_lock(lock_path: Path, *, timeout_s: float = 30.0) -> Iterator[None]:
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = _lock_key(lock_path)
    held: dict[str, int] = getattr(_nesting, "held", None)
    if held is None:
        held = {}
        _nesting.held = held
    if key in held:  # 同一线程重入：不重复获取 OS 锁（Windows 下同进程不同句柄会冲突）
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
            if held[key] <= 0:
                held.pop(key, None)
        return

    plock = _process_lock(key)
    if not plock.acquire(timeout=float(timeout_s)):
        raise StoreBridgeError(f"获取进程内文件锁超时：{lock_path}")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            _os_lock(fd, lock_path, timeout_s)
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
                _os_unlock(fd)
        finally:
            os.close(fd)
    finally:
        plock.release()


@contextlib.contextmanager
def file_lock(lock_path: Path, *, timeout_s: float = 30.0) -> Iterator[None]:
    """跨平台文件锁上下文管理器（同一进程内可重入）。"""
    fn = _store_fn("atomic", "file_lock")
    if fn is not None:
        with fn(Path(lock_path), timeout_s=timeout_s):
            yield
        return
    _warn_fallback("atomic")
    with _fallback_file_lock(Path(lock_path), timeout_s=timeout_s):
        yield


# ---------------------------------------------------------------------------
# 原子写（CONTRACTS §3）
# ---------------------------------------------------------------------------


def _fallback_atomic_write_json(path: Path, obj: Any, *, fsync: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    data = _fallback_canonical_json(obj)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
        raise


def atomic_write_json(path: Path, obj: Any, *, fsync: bool = True) -> None:
    """先写同目录 `{name}.tmp` -> flush -> fsync -> os.replace()。"""
    fn = _store_fn("atomic", "atomic_write_json")
    if fn is not None:
        return fn(Path(path), obj, fsync=fsync)
    _warn_fallback("atomic")
    return _fallback_atomic_write_json(Path(path), obj, fsync=fsync)


def read_json_strict(path: Path) -> dict:
    """严格读取 JSON 对象。文件不存在或为空 -> {}；损坏 -> 抛错（绝不静默当作空）。"""
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreBridgeError(f"JSON 元数据损坏，拒绝当作空对象处理：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise StoreBridgeError(f"JSON 元数据顶层必须是对象：{path}")
    return data


def _fallback_locked_update_json(
    path: Path, mutator: Callable[[dict], dict], *, timeout_s: float = 30.0
) -> dict:
    path = Path(path)
    lock_path = path.parent / f".{path.name}.lock"
    with _fallback_file_lock(lock_path, timeout_s=timeout_s):
        current = read_json_strict(path)
        updated = mutator(current)
        if not isinstance(updated, dict):
            raise StoreBridgeError("locked_update_json 的 mutator 必须返回 dict")
        _fallback_atomic_write_json(path, updated)
        return updated


def locked_update_json(
    path: Path, mutator: Callable[[dict], dict], *, timeout_s: float = 30.0
) -> dict:
    """整个 read-modify-write 周期持锁（CONTRACTS §3）。"""
    fn = _store_fn("atomic", "locked_update_json")
    if fn is not None:
        return fn(Path(path), mutator, timeout_s=timeout_s)
    _warn_fallback("atomic")
    return _fallback_locked_update_json(Path(path), mutator, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# ID 生成（CONTRACTS §2）
# ---------------------------------------------------------------------------


def _need_str(value: Any, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} 不能为 None")
    text = value.value if isinstance(value, Enum) else value
    text = str(text)
    if not text.strip():
        raise ValueError(f"{name} 不能为空字符串")
    return text


def new_finalization_token() -> str:
    fn = _store_fn("ids", "new_finalization_token")
    if fn is not None:
        return fn()
    _warn_fallback("ids")
    return uuid.uuid4().hex


def _uuid8() -> str:
    return uuid.uuid4().hex[:8]


def review_cycle_id(object_type: str, object_id: str, version: str) -> str:
    """`{对象类型}:{对象ID}:{version}`"""
    object_type = _need_str(object_type, "object_type")
    object_id = _need_str(object_id, "object_id")
    version = _need_str(version, "version")
    fn = _store_fn("ids", "review_cycle_id")
    if fn is not None:
        return fn(object_type, object_id, version)
    _warn_fallback("ids")
    return f"{object_type}:{object_id}:{version}"


def mod_id(object_type: str, object_id: str, suffix: str | None = None) -> str:
    """`MOD_{对象类型}_{对象ID}_{uuid8}`（store.ids.mod_id 的 uuid8 是仅关键字参数）"""
    object_type = _need_str(object_type, "object_type")
    object_id = _need_str(object_id, "object_id")
    fn = _store_fn("ids", "mod_id")
    if fn is not None:
        if suffix is None:
            return fn(object_type, object_id)
        return fn(object_type, object_id, uuid8=suffix)
    _warn_fallback("ids")
    return f"MOD_{object_type}_{object_id}_{suffix or _uuid8()}"


def attempt_id(scope_id: str, version: str, seq: int) -> str:
    """`ATTEMPT_{对象ID}_{version}_{序号}`（序号从 1 开始，不补零）"""
    scope_id = _need_str(scope_id, "scope_id")
    version = _need_str(version, "version")
    try:
        seq_int = int(seq)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"attempt_id 的 seq 必须是 >=1 的整数，实际：{seq!r}") from exc
    if seq_int < 1:
        raise ValueError(f"attempt_id 的 seq 必须 >=1，实际：{seq_int}")
    fn = _store_fn("ids", "attempt_id")
    if fn is not None:
        return fn(scope_id, version, seq_int)
    _warn_fallback("ids")
    return f"ATTEMPT_{scope_id}_{version}_{seq_int}"


def arb_id(human_review_id: str, model_review_id: str) -> str:
    """`ARB_{人工审核ID}_{模型审核ID}`"""
    human_review_id = _need_str(human_review_id, "human_review_id")
    model_review_id = _need_str(model_review_id, "model_review_id")
    fn = _store_fn("ids", "arb_id")
    if fn is not None:
        return fn(human_review_id, model_review_id)
    _warn_fallback("ids")
    return f"ARB_{human_review_id}_{model_review_id}"


def abort_id(review_record_id: str) -> str:
    """`ABORT_{审核记录ID}`"""
    review_record_id = _need_str(review_record_id, "review_record_id")
    fn = _store_fn("ids", "abort_id")
    if fn is not None:
        return fn(review_record_id)
    _warn_fallback("ids")
    return f"ABORT_{review_record_id}"


def lock_id(asset_id: str, version: str) -> str:
    """`LOCK_{asset_id}_{version}`"""
    asset_id = _need_str(asset_id, "asset_id")
    version = _need_str(version, "version")
    fn = _store_fn("ids", "lock_id")
    if fn is not None:
        return fn(asset_id, version)
    _warn_fallback("ids")
    return f"LOCK_{asset_id}_{version}"


def cnt_id(object_id: str, kind: Any) -> str:
    """`CNT_{对象ID}_{计数类型}`"""
    object_id = _need_str(object_id, "object_id")
    kind_text = _need_str(kind, "kind")
    fn = _store_fn("ids", "cnt_id")
    if fn is not None:
        return fn(object_id, kind)
    _warn_fallback("ids")
    return f"CNT_{object_id}_{kind_text}"
