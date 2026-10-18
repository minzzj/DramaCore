"""审核事件日志适配层（namespace = `REVIEW`）。

**本模块不自己实现日志文件。** CONTRACTS §10 规定事件日志由 `bettercomfyui/store/events.py`
统一提供（`append_event(project, *, event, namespace=..., ...)`，落盘位置由 store 决定）。
这里只做两件事：

1. 构造符合 §10 的行记录（字段顺序固定：`ts, namespace, event, project_id, stage,
   object_id, version, mode, detail, severity`）并在内存中留一份镜像，便于断言；
2. 若 `bettercomfyui.store.events` 可用 → 转交给它落盘（namespace="REVIEW"）；
   若不可用 → 只保留内存镜像、`mode == "memory"`、`persistence_available is False`，
   并打印一次性 WARN。

为什么不「缺依赖也照常成功」：审核事件（状态变化、模式切换、仲裁结论、兜底、通知投递）
是方案 Z4/Z5 的强制记录项。把内存镜像当成「已记录」就是假成功，因此本类显式暴露落盘能力：
`mode` / `persistence_available` / `require_store_events()`，验证员可直接断言。

为什么事件写失败不抛异常：CONTRACTS §10 明确「写日志失败只告警，绝不中断主流程」。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..models.entities import now_iso
from .errors import StoreBridgeError

_LOG = logging.getLogger(__name__)

#: review 层事件命名空间（CONTRACTS §10）
NAMESPACE = "REVIEW"

#: 事件行的固定字段顺序（CONTRACTS §10 / v1.1 含 namespace）
EVENT_FIELDS = (
    "ts",
    "namespace",
    "event",
    "project_id",
    "stage",
    "object_id",
    "version",
    "mode",
    "detail",
    "severity",
)

_SEVERITIES = ("DEBUG", "INFO", "WARN", "ERROR")
_SEVERITY_ALIASES = {
    "WARNING": "WARN",
    "ERR": "ERROR",
    "CRITICAL": "ERROR",
    "FATAL": "ERROR",
    "NOTICE": "INFO",
    "TRACE": "DEBUG",
}

_STORE_EVENTS_MODULE = "bettercomfyui.store.events"
_store_events_cache: dict[str, Any] = {}
_store_events_lock = threading.RLock()


def reset_store_events_cache() -> None:
    """测试/热接入用：让下一次解析重新尝试导入 store.events。"""
    with _store_events_lock:
        _store_events_cache.clear()


def _store_events_module():
    with _store_events_lock:
        if "mod" in _store_events_cache:
            return _store_events_cache["mod"]
    try:
        mod = importlib.import_module(_STORE_EVENTS_MODULE)
    except Exception as exc:  # store.events 未就绪
        _LOG.debug("%s 不可用：%s", _STORE_EVENTS_MODULE, exc)
        mod = None
    with _store_events_lock:
        _store_events_cache["mod"] = mod
    return mod


def store_events_available() -> bool:
    """store.events 是否可导入。"""
    return _store_events_module() is not None


def require_store_events() -> None:
    """验收/生产校验用：store.events 不可用时**必须抛异常**（拒绝静默成功）。

    错误信息里写明「事件未持久化」，让验证员无法把「没落盘但流程继续了」误判成通过。
    """
    if not store_events_available():
        raise StoreBridgeError(
            f"{_STORE_EVENTS_MODULE} 尚未就绪：审核事件未持久化。review 层不自己另建日志文件"
            "（CONTRACTS §10），请先落地 store/events.py 后重跑本次验收。"
        )


def normalize_severity(severity: Any) -> str:
    text = str(severity or "INFO").strip().upper()
    text = _SEVERITY_ALIASES.get(text, text)
    if text not in _SEVERITIES:
        raise ValueError(f"severity 非法：{severity!r}；允许值：{', '.join(_SEVERITIES)}")
    return text


def _coerce(value: Any) -> Any:
    """把枚举/路径/集合等转换成可 JSON 序列化的原生值（递归）。"""
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_coerce(v) for v in value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _coerce(value.to_dict())
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _accepted(fn: Callable, names: tuple[str, ...]) -> set[str] | None:
    """返回 fn 能接受的关键字参数集合；**无法自省签名**时返回 None（表示「签名未知」）。

    只有 ``inspect.signature`` 自身的两种**已知不可自省**情形才返回 None，且必留 WARN：

    * ``ValueError``：C 实现的内建函数/方法没有 ``__text_signature__``；
    * ``TypeError``：对象带有非法的 ``__signature__``，或根本不是可调用对象。

    **其它任何异常一律抛出** —— 不许「一律返回 None」。原因：None 会被上层当作
    「签名未知但大概能用」，从而以缺失参数的调用蒙混过关（事件没落盘却看起来已记录）。
    调用方必须把 None 当成「不可安全桥接」，见 `_build_store_writer`。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _LOG.warning(
            "无法自省 %r 的签名（%s: %s）：按「签名为未知」处理，调用方不得据此盲目调用。",
            getattr(fn, "__qualname__", fn),
            type(exc).__name__,
            exc,
        )
        return None
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return set(names)
    return {n for n in names if n in params}


def _build_store_writer(project: Any, fsync: bool) -> tuple[Callable | None, str | None]:
    """把 store.events 包装成 `writer(record: dict)`；不可用时返回 (None, None)。"""
    mod = _store_events_module()
    if mod is None or project is None:
        return None, None

    payload_names = EVENT_FIELDS + ("fsync",)

    # store-project 的正式入口（已确认签名）：append_event(project, *, event, namespace=...)
    append = getattr(mod, "append_event", None)
    if callable(append):
        accepted = _accepted(append, payload_names)
        if accepted is None or "event" not in accepted:
            # 签名未知 / 不接受 event -> 绝不盲目调用（那会「事件没落盘却以为已记录」）。
            # 显式降级：writer 保持 None，ReviewEventLog 会以 mode="memory" + WARN 暴露。
            _LOG.warning(
                "%s.append_event 的签名无法确定是否接受 event 关键字（accepted=%r）："
                "拒绝创建事件写入器，改按「未落盘」显式暴露（绝不静默丢弃事件）。",
                _STORE_EVENTS_MODULE,
                accepted,
            )
        else:
            def writer(record: dict, _fn=append, _accepted=accepted) -> None:
                kwargs = {k: record[k] for k in _accepted if k in record}
                if "fsync" in _accepted:
                    kwargs["fsync"] = fsync
                if "namespace" not in kwargs and "namespace" in _accepted:
                    kwargs["namespace"] = NAMESPACE
                result = _fn(project, **kwargs)
                if isinstance(result, dict) and result.get("_write_error"):
                    raise StoreBridgeError(f"store 事件写入失败：{result['_write_error']}")

            return writer, f"{_STORE_EVENTS_MODULE}.append_event"

    for name in ("emit_event", "write_event", "log_event"):
        fn = getattr(mod, name, None)
        if not callable(fn):
            continue
        accepted = _accepted(fn, payload_names)
        if accepted is None or "event" not in accepted:
            continue

        def writer(record: dict, _fn=fn, _accepted=accepted) -> None:
            kwargs = {k: record[k] for k in _accepted if k in record}
            kwargs.setdefault("namespace", NAMESPACE)
            result = _fn(**kwargs)
            if isinstance(result, dict) and result.get("_write_error"):
                raise StoreBridgeError(f"store 事件写入失败：{result['_write_error']}")

        return writer, f"{_STORE_EVENTS_MODULE}.{name}"

    return None, None


class ReviewEventLog:
    """审核事件记录器：固定 §10 schema + 内存镜像 + 可选 store 落盘。"""

    def __init__(
        self,
        project: Any = None,
        *,
        project_id: str | None = None,
        writer: Callable[[dict], None] | None = None,
        clock: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
        fsync: bool = True,
    ) -> None:
        self.project = project
        self.project_id = project_id
        self._clock = clock or now_iso
        self._log = logger or _LOG
        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self.write_failures = 0
        self.writer_name: str | None = None
        if writer is not None:
            self._writer: Callable | None = writer
            self.writer_name = "injected"
        else:
            self._writer, self.writer_name = _build_store_writer(project, fsync)
        if self._writer is None:
            self._log.warning(
                "审核事件未落盘：%s 不可用或未提供 project（namespace=%s）。"
                "当前仅保留内存镜像——这不算「已记录」，上线前必须提供 store/events.py 与项目路径。",
                _STORE_EVENTS_MODULE,
                NAMESPACE,
            )

    # -- 能力自述（供验证员断言，绝不假成功） --------------------------------
    @property
    def mode(self) -> str:
        return "store" if self._writer is not None else "memory"

    @property
    def persistence_available(self) -> bool:
        return self._writer is not None

    # -- 记录 ---------------------------------------------------------------
    def emit(
        self,
        event: str,
        *,
        stage: Any = None,
        object_id: Any = None,
        version: Any = None,
        mode: Any = None,
        detail: dict | None = None,
        severity: str = "INFO",
    ) -> dict:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event 必须是非空字符串")
        record = {
            "ts": self._clock(),
            "namespace": NAMESPACE,
            "event": event.strip(),
            "project_id": self.project_id,
            "stage": _coerce(stage),
            "object_id": _coerce(object_id),
            "version": _coerce(version),
            "mode": _coerce(mode),
            "detail": _coerce(detail or {}),
            "severity": normalize_severity(severity),
        }
        with self._lock:
            self._entries.append(record)
            if self._writer is not None:
                try:
                    self._writer(dict(record))
                except Exception as exc:  # §10：写日志失败只告警，不中断主流程
                    self.write_failures += 1
                    self._log.warning(
                        "事件日志写入失败（不阻断主流程）：%s | event=%s", exc, record["event"]
                    )
        return record

    # -- 读取（测试/调试） --------------------------------------------------
    def entries(self, *, event: str | None = None, severity: str | None = None) -> list[dict]:
        with self._lock:
            items = [dict(e) for e in self._entries]
        if event is not None:
            items = [e for e in items if e["event"] == event]
        if severity is not None:
            sev = normalize_severity(severity)
            items = [e for e in items if e["severity"] == sev]
        return items

    def events(self) -> list[str]:
        with self._lock:
            return [e["event"] for e in self._entries]

    def has_event(self, event: str) -> bool:
        return any(e["event"] == event for e in self._entries)

    def tail(self, n: int = 10) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._entries[-int(n):]]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = [
    "NAMESPACE",
    "EVENT_FIELDS",
    "ReviewEventLog",
    "normalize_severity",
    "store_events_available",
    "require_store_events",
    "reset_store_events_cache",
]
