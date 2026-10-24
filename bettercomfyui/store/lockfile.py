"""跨平台文件锁（Windows + POSIX）：docs/CONTRACTS.md 第 3 节。

契约要求
--------
* 需在 Windows 与 POSIX 均可工作；
* **同一进程内可重入**（同一线程重复进入不会自锁死）；
* 超时抛 :class:`~bettercomfyui.store.errors.LockTimeoutError`（``StoreError`` 子类）。

实现要点（并发正确性是本模块的全部价值）
----------------------------------------
1. **进程内重入**用一把 ``threading.RLock`` 表达：同线程重入直接成功；
   其他线程必须等待，等待超时按超时处理，**绝不会**因为「同进程」就放行，
   否则同进程两个线程会同时进入 read-modify-write 而丢失更新。
2. **跨进程互斥**用操作系统字节锁：POSIX ``fcntl.flock`` / Windows
   ``msvcrt.locking``。锁文件本身不删除（删除锁文件会让竞争者拿到不同 inode
   的两把「同一把锁」，是经典的锁失效 bug）；进程崩溃时由内核释放锁。
3. 轮询采用短间隔 + 指数退避到 50ms，避免空转打满 CPU。
4. 锁文件路径约定：``.{文件名}.lock``（与目标同目录），见 :func:`lock_path_for`。
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from bettercomfyui.store.errors import LockTimeoutError, StoreError

__all__ = ["FileLock", "file_lock", "lock_path_for"]

_POLL_MIN_S = 0.002
_POLL_MAX_S = 0.050


def lock_path_for(path: Path | str) -> Path:
    """返回目标文件对应的锁文件路径：同目录 ``.{文件名}.lock``。"""
    target = Path(path)
    return target.with_name("." + target.name + ".lock")


@dataclass
class _Entry:
    """进程内某把锁的状态（只受 :data:`_ENTRIES_GUARD` 与 ``rlock`` 保护）。"""

    rlock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    fd: int | None = None


_ENTRIES: dict[str, _Entry] = {}
_ENTRIES_GUARD = threading.Lock()


def _lock_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _entry_for(key: str) -> _Entry:
    with _ENTRIES_GUARD:
        entry = _ENTRIES.get(key)
        if entry is None:
            entry = _Entry()
            _ENTRIES[key] = entry
        return entry


def _open_lock_file(path: Path) -> int:
    parent = path.parent
    if str(parent) and not parent.exists():
        os.makedirs(parent, exist_ok=True)
    fd = os.open(os.fspath(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:  # pragma: no cover - 环境异常
        os.close(fd)
        raise
    return fd


if os.name == "nt":  # pragma: no cover - 平台分支（本机 windows 走这条）

    def _try_os_lock(fd: int) -> bool:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release_os_lock(fd: int) -> None:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:  # pragma: no cover - 平台分支

    def _try_os_lock(fd: int) -> bool:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release_os_lock(fd: int) -> None:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _acquire_os_lock(fd: int, *, deadline: float, path: Path, timeout_s: float) -> None:
    delay = _POLL_MIN_S
    while True:
        if _try_os_lock(fd):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockTimeoutError(
                "等待文件锁超时（另一进程正在持锁）",
                lock=str(path),
                timeout_s=timeout_s,
            )
        time.sleep(min(delay, remaining))
        delay = min(_POLL_MAX_S, delay * 2)


class FileLock:
    """跨进程 / 跨线程的排他文件锁。

    用法::

        with FileLock(lock_path, timeout_s=5.0):
            ...

    同一个 ``FileLock`` 实例重复 ``acquire`` 会抛 :class:`StoreError`（防止忘记
    ``release``）；同一线程通过不同实例重入同一把锁是允许的（契约要求可重入）。
    """

    def __init__(self, lock_path: Path | str, *, timeout_s: float = 30.0) -> None:
        self.path = Path(lock_path)
        self.key = _lock_key(self.path)
        self.timeout_s = float(timeout_s)
        if self.timeout_s < 0:
            raise StoreError("timeout_s 不能为负数", timeout_s=timeout_s)
        self._held = False

    # -- 内部 ---------------------------------------------------------------

    def _assert_not_held(self) -> None:
        if self._held:
            raise StoreError(
                "同一个 FileLock 实例已被持有，禁止重复 acquire（请先 release）",
                lock=str(self.path),
            )

    # -- 公开接口 -----------------------------------------------------------

    def acquire(self, timeout_s: float | None = None) -> "FileLock":
        self._assert_not_held()
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + timeout
        entry = _entry_for(self.key)

        wait = max(0.0, deadline - time.monotonic())
        if not entry.rlock.acquire(timeout=wait):
            # 同进程内其他线程正持锁 -> 必须等待，不能因为「同进程」就放行
            raise LockTimeoutError(
                "等待文件锁超时（同进程其他线程正在持锁）",
                lock=str(self.path),
                timeout_s=timeout,
            )
        try:
            if entry.depth > 0:
                # 当前线程已持有（可重入）
                entry.depth += 1
            else:
                fd = _open_lock_file(self.path)
                try:
                    _acquire_os_lock(fd, deadline=deadline, path=self.path, timeout_s=timeout)
                except BaseException:
                    os.close(fd)
                    raise
                entry.fd = fd
                entry.depth = 1
        except BaseException:
            entry.rlock.release()
            raise
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        entry = _entry_for(self.key)
        try:
            entry.depth -= 1
            if entry.depth <= 0:
                entry.depth = 0
                fd, entry.fd = entry.fd, None
                if fd is not None:
                    try:
                        _release_os_lock(fd)
                    finally:
                        os.close(fd)
        finally:
            entry.rlock.release()

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def file_lock(lock_path: Path | str, *, timeout_s: float = 30.0) -> Iterator[FileLock]:
    """上下文管理器形式：``with file_lock(path, timeout_s=30): ...``"""
    lock = FileLock(lock_path, timeout_s=timeout_s)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
