"""事件记录（契约 §10 v1.1：统一 schema，含必填 `namespace`）。

约定：
- **不自建日志文件**（契约要求统一走 `store/events.py`，由 store-project 独占）。
- 本模块只做「内存记录 + 转发给注入的 sink」：
  - 记录本身始终可用（测试与回报据此断言事件确实产生）；
  - 若注入了 `sink`，逐条转发；转发失败只追加 `severity=WARN` 记录，**绝不阻断主流程**。

行 schema（契约 §10）：
`{"ts","namespace","event","project_id","stage","object_id","version","mode","detail","severity"}`
"""

from __future__ import annotations

from typing import Any

from bettercomfyui.models.entities import now_iso

__all__ = ["DEFAULT_NAMESPACE", "EVENT_FIELDS", "EventRecorder", "SEVERITIES"]

DEFAULT_NAMESPACE = "CASCADE"
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
SEVERITIES = ("DEBUG", "INFO", "WARN", "ERROR")


class EventRecorder:
    """最小事件记录器：内存追加 + 可选 sink 转发。"""

    def __init__(
        self,
        sink: Any | None = None,
        *,
        project_id: str = "",
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        if not namespace:
            raise ValueError("namespace 是契约 §10 的必填字段，不得为空")
        self.sink = sink
        self.project_id = str(project_id or "")
        self.namespace = str(namespace)
        self.records: list[dict] = []

    def emit(
        self,
        event: str,
        *,
        stage: str | None = None,
        object_id: str | None = None,
        version: str | None = None,
        mode: str | None = None,
        detail: dict | None = None,
        severity: str = "INFO",
    ) -> dict:
        sev = str(severity).upper()
        if sev not in SEVERITIES:
            raise ValueError(f"severity 必须是 {SEVERITIES} 之一，收到 {severity!r}")
        record = {
            "ts": now_iso(),
            "namespace": self.namespace,
            "event": str(event),
            "project_id": self.project_id,
            "stage": stage,
            "object_id": object_id,
            "version": version,
            "mode": mode,
            "detail": dict(detail or {}),
            "severity": sev,
        }
        self.records.append(record)
        self._forward(record)
        return record

    def _forward(self, record: dict) -> None:
        if self.sink is None:
            return
        for name in ("emit", "log", "write", "log_event"):
            fn = getattr(self.sink, name, None)
            if callable(fn):
                try:
                    fn(record)
                    return
                except Exception as exc:  # noqa: BLE001 - 日志/通知失败不得阻断主流程
                    self.records.append(
                        {
                            "ts": now_iso(),
                            "namespace": self.namespace,
                            "event": "event_sink_failed",
                            "project_id": self.project_id,
                            "stage": record.get("stage"),
                            "object_id": record.get("object_id"),
                            "version": record.get("version"),
                            "mode": None,
                            "detail": {
                                "sink": type(self.sink).__name__,
                                "method": name,
                                "error": f"{type(exc).__name__}: {exc}",
                                "failed_event": record.get("event"),
                            },
                            "severity": "WARN",
                        }
                    )
                    return

    def events(self, name: str | None = None) -> list[dict]:
        if name is None:
            return list(self.records)
        return [r for r in self.records if r.get("event") == name]
