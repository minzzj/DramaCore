"""追加式事件日志 —— docs/CONTRACTS.md 第 10 节（``logs/events.jsonl``）。

每行一个 JSON 对象，字段固定：

.. code-block:: json

    {"ts":"ISO8601+08:00","namespace":"BETTERCOMFYUI|REVIEW|CASCADE","event":"...",
     "project_id":"...","stage":"...","object_id":"...","version":null,
     "mode":null,"detail":{},"severity":"INFO"}

硬要求
------
* **只追加**：``O_APPEND`` + 单次 ``write`` 写完整行，不做 read-modify-write，
  因此多进程并发追加不会互相覆盖。
* **不得阻断主流程**：日志文件写失败只记 ``logging.warning``，绝不抛异常给调用方
  （返回值里带 ``_write_error`` 以便上层自证「日志没写成功」，而不是静默假装成功）。
* 输入校验失败（未知 namespace / severity、非法字段）**仍然抛错**——
  那是编码错误，越早暴露越好。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from ..models.entities import now_iso
from ..models.enums import ReviewMode, Stage
from .errors import CorruptFileError, StoreError, parse_enum
from .paths import ProjectPaths, coerce_project_paths

__all__ = [
    "EVENT_NAMESPACES",
    "EVENT_SEVERITIES",
    "make_event",
    "append_event",
    "read_events",
]

logger = logging.getLogger(__name__)

#: 事件来源命名空间。新增取值必须先改 docs/CONTRACTS.md 第 10 节。
EVENT_NAMESPACES: tuple[str, ...] = ("BETTERCOMFYUI", "REVIEW", "CASCADE")
EVENT_SEVERITIES: tuple[str, ...] = ("DEBUG", "INFO", "WARN", "ERROR")


def _validate_token(value: Any, *, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{kind} 必须是非空字符串", kind=kind, value=repr(value))
    text = value.strip()
    if any(ch.isspace() for ch in text):
        raise StoreError(f"{kind} 不能包含空白字符", kind=kind, value=text)
    return text


def make_event(
    *,
    event: str,
    namespace: str = "BETTERCOMFYUI",
    project_id: str | None = None,
    stage: Stage | str | None = None,
    object_id: str | None = None,
    version: str | int | None = None,
    mode: ReviewMode | str | None = None,
    detail: dict | None = None,
    severity: str = "INFO",
    ts: str | None = None,
) -> dict:
    """构造一条规范化事件记录（纯函数，不写盘；便于测试与复用）。"""
    event_name = _validate_token(event, kind="event")
    if namespace not in EVENT_NAMESPACES:
        raise StoreError(
            "未知的事件 namespace（新增取值必须先改 CONTRACTS 第 10 节）",
            namespace=namespace,
            allowed=list(EVENT_NAMESPACES),
        )
    if severity not in EVENT_SEVERITIES:
        raise StoreError(
            "未知的 severity", severity=severity, allowed=list(EVENT_SEVERITIES)
        )
    stage_value: str | None = None
    if stage is not None:
        stage_value = parse_enum(Stage, stage, kind="stage").value
    mode_value: str | None = None
    if mode is not None:
        mode_value = parse_enum(ReviewMode, mode, kind="mode").value
    if detail is None:
        detail_dict: dict = {}
    elif isinstance(detail, dict):
        detail_dict = detail
    else:
        raise StoreError("detail 必须是 dict", actual=type(detail).__name__)
    if version is not None and not isinstance(version, (str, int)):
        raise StoreError("version 必须是 str/int/None", actual=type(version).__name__)

    return {
        "ts": ts or now_iso(),
        "namespace": namespace,
        "event": event_name,
        "project_id": project_id,
        "stage": stage_value,
        "object_id": object_id,
        "version": None if version is None else str(version),
        "mode": mode_value,
        "detail": detail_dict,
        "severity": severity,
    }


def append_event(
    project: ProjectPaths | Path | str,
    *,
    event: str,
    namespace: str = "BETTERCOMFYUI",
    stage: Stage | str | None = None,
    object_id: str | None = None,
    version: str | int | None = None,
    mode: ReviewMode | str | None = None,
    detail: dict | None = None,
    severity: str = "INFO",
    fsync: bool = True,
) -> dict:
    """追加一条事件到 ``logs/events.jsonl``，返回写入的事件记录。

    写盘失败（磁盘满、权限、路径被占）只记 WARN 日志并返回带 ``_write_error``
    的记录，**不抛异常**——通知/日志绝不阻断主流程（契约第 10 节）。
    """
    paths = coerce_project_paths(project)
    record = make_event(
        event=event,
        namespace=namespace,
        project_id=paths.project_id,
        stage=stage,
        object_id=object_id,
        version=version,
        mode=mode,
        detail=detail,
        severity=severity,
    )
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    target = paths.events_jsonl
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(os.fspath(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning(
            "事件日志写入失败（不阻断主流程）：%s (%s)", target, exc
        )
        result = dict(record)
        result["_write_error"] = str(exc)
        return result
    return record


def read_events(
    project: ProjectPaths | Path | str,
    *,
    namespace: str | None = None,
    limit: int | None = None,
    strict: bool = False,
) -> list[dict]:
    """读取事件日志。

    ``strict=False``（默认）跳过损坏行并记 WARN —— 崩溃可能留下半行；
    ``strict=True`` 遇到损坏行抛 :class:`CorruptFileError`（用于验证时取证）。
    """
    paths = coerce_project_paths(project)
    target = paths.events_jsonl
    if not target.exists():
        return []
    if namespace is not None and namespace not in EVENT_NAMESPACES:
        raise StoreError(
            "未知的事件 namespace", namespace=namespace, allowed=list(EVENT_NAMESPACES)
        )

    records: list[dict] = []
    with target.open("r", encoding="utf-8", errors="strict") as handle:
        for lineno, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                if strict:
                    raise CorruptFileError(
                        f"事件日志第 {lineno} 行不是合法 JSON: {exc}",
                        path=str(target),
                        line=lineno,
                    ) from exc
                logger.warning("事件日志第 %s 行损坏，已跳过：%s", lineno, exc)
                continue
            if not isinstance(item, dict):
                if strict:
                    raise CorruptFileError(
                        f"事件日志第 {lineno} 行不是 JSON 对象",
                        path=str(target),
                        line=lineno,
                    )
                logger.warning("事件日志第 %s 行不是对象，已跳过", lineno)
                continue
            if namespace is not None and item.get("namespace") != namespace:
                continue
            records.append(item)
    if limit is not None:
        if limit < 0:
            raise StoreError("limit 不能为负", limit=limit)
        records = records[-limit:]
    return records
