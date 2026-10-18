"""通知投递（弹窗/声音/QQ/飞书/钉钉，可多选）。

硬要求（CONTRACTS §10 + 方案参数表三）：
- **通知失败只写 `severity=WARN` 日志，绝不阻断主流程**；多个渠道之间互不影响
  （一个渠道失败不能跳过其余渠道）。
- **绝不假成功**：未配置 URL/传输层的 Webhook、未接入的弹窗/声音通道一律记为
  `ok=False` 并给出可操作信息，不会返回"已通知"。

真实投递方式由 UI/集成层通过 `register_channel()` 注入；默认的 popup/sound 在无界面进程里
**没有实现**，因此默认就是失败并告警 —— 这是刻意的，避免"以为通知发了"。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..models.entities import AssetReviewParams
from .errors import NotificationError

_LOG = logging.getLogger(__name__)

#: 规范渠道名
CANONICAL_CHANNELS = ("POPUP", "SOUND", "QQ", "FEISHU", "DINGTALK", "WEBHOOK", "LOG")

#: 配置/中文名 -> 规范名
CHANNEL_ALIASES = {
    "popup": "POPUP",
    "弹窗": "POPUP",
    "window": "POPUP",
    "sound": "SOUND",
    "声音": "SOUND",
    "audio": "SOUND",
    "qq": "QQ",
    "feishu": "FEISHU",
    "飞书": "FEISHU",
    "lark": "FEISHU",
    "dingtalk": "DINGTALK",
    "钉钉": "DINGTALK",
    "webhook": "WEBHOOK",
    "log": "LOG",
}

_MAX_TEXT = 300


def resolve_channel_name(name: Any) -> str:
    text = str(name).strip()
    return CHANNEL_ALIASES.get(text.lower(), text.upper())


def _clip(text: Any, limit: int = _MAX_TEXT) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[:limit] + "…"


@dataclass
class CallableChannel:
    """把任意 `send(*, title, message, severity, detail)` 包装成渠道。"""

    name: str
    send_fn: Callable[..., None]

    def send(self, *, title: str, message: str, severity: str = "INFO", detail: dict | None = None) -> None:
        self.send_fn(title=title, message=message, severity=severity, detail=detail)


@dataclass
class UnimplementedChannel:
    """未接入的渠道：**明确失败**，不假装成功。"""

    name: str
    reason: str

    def send(self, *, title: str, message: str, severity: str = "INFO", detail: dict | None = None) -> None:
        raise NotificationError(
            f"通知渠道 {self.name} 未接入：{self.reason}；"
            "请由 UI/集成层调用 NotificationService.register_channel() 注册真实实现"
            "（当前不会假装已发送）"
        )


class WebhookChannel:
    """QQ / 飞书 / 钉钉 / 通用 Webhook（HTTP 传输层必须显式注入，便于离线测试）。"""

    def __init__(
        self,
        name: str,
        *,
        url: str | None = None,
        transport: Callable[..., Any] | None = None,
        headers: dict | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.name = resolve_channel_name(name)
        self.url = url
        self.transport = transport
        self.headers = dict(headers or {})
        self.timeout_s = float(timeout_s)

    def send(self, *, title: str, message: str, severity: str = "INFO", detail: dict | None = None) -> None:
        if not self.url:
            raise NotificationError(
                f"通知渠道 {self.name} 未配置 url；请设置对应 Webhook 地址后再启用"
                "（未配置时绝不静默跳过）"
            )
        if self.transport is None:
            raise NotificationError(
                f"通知渠道 {self.name} 未注入 HTTP 传输层（transport）；"
                "为可离线测试与可控投递，本实现不内置网络调用"
            )
        payload = {"title": _clip(title), "message": _clip(message), "severity": severity,
                   "detail": detail or {}}
        try:
            self.transport(url=self.url, payload=payload, headers=self.headers, timeout_s=self.timeout_s)
        except Exception as exc:
            raise NotificationError(f"通知渠道 {self.name} 投递失败：{exc}") from exc


class LogChannel:
    """只写本地日志的渠道（永远可用，用于兜底可见性）。"""

    name = "LOG"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or _LOG

    def send(self, *, title: str, message: str, severity: str = "INFO", detail: dict | None = None) -> None:
        self._log.info("[通知] %s | %s | detail=%s", _clip(title), _clip(message), detail or {})


class NotificationService:
    """多渠道通知；任何失败都只降级为 WARN，绝不抛出到主流程。"""

    def __init__(
        self,
        channels: Any = None,
        *,
        params: AssetReviewParams | None = None,
        event_log: Any = None,
        logger: logging.Logger | None = None,
        max_retries: int = 0,
        project_id: str | None = None,
    ) -> None:
        self._log = logger or _LOG
        self._events = event_log
        self._lock = threading.RLock()
        self._channels: dict[str, Any] = {}
        self.project_id = project_id
        self.max_retries = max(0, int(max_retries))
        if channels is None and params is not None:
            channels = list(params.notify_channels or [])
        for channel in self._iter_channels(channels):
            self.register_channel(channel)

    # -- 注册 ---------------------------------------------------------------
    @staticmethod
    def _iter_channels(channels: Any) -> Iterable[Any]:
        if channels is None:
            return []
        if isinstance(channels, dict):
            return [CallableChannel(name=resolve_channel_name(k), send_fn=v) for k, v in channels.items()]
        if isinstance(channels, (str, bytes)) or hasattr(channels, "send"):
            return [channels]
        return list(channels)

    def _default_channel(self, name: str):
        canonical = resolve_channel_name(name)
        if canonical == "LOG":
            return LogChannel(self._log)
        if canonical in ("QQ", "FEISHU", "DINGTALK", "WEBHOOK"):
            return WebhookChannel(canonical)
        return UnimplementedChannel(
            canonical,
            "本进程未注册该渠道（无界面/未接入的 popup、sound 等一律视为未接入）",
        )

    def register_channel(self, channel: Any) -> str:
        """注册渠道：可以是 `send()` 对象、callable，或渠道名（用默认实现）。"""
        if isinstance(channel, str):
            obj = self._default_channel(channel)
            name = resolve_channel_name(channel)
        elif callable(channel) and not hasattr(channel, "send"):
            name = "CALLABLE"
            obj = CallableChannel(name=name, send_fn=channel)
        elif hasattr(channel, "send"):
            obj = channel
            name = resolve_channel_name(getattr(channel, "name", type(channel).__name__))
        else:
            raise NotificationError(f"无法注册通知渠道：{channel!r}（需要渠道名或带 send() 的对象）")
        with self._lock:
            self._channels[name] = obj
        return name

    def registered_channels(self) -> list[str]:
        with self._lock:
            return sorted(self._channels)

    # -- 投递 ---------------------------------------------------------------
    def notify(
        self,
        *,
        title: str,
        message: str,
        channels: Any = None,
        severity: str = "INFO",
        detail: dict | None = None,
    ) -> list[dict]:
        """投递通知。返回每个渠道的结果；**任何失败都不会抛出**。"""
        targets: list[Any]
        if channels is None:
            with self._lock:
                targets = list(self._channels.values())
        else:
            targets = []
            seen: set[str] = set()
            for item in self._iter_channels(channels):
                if isinstance(item, str):
                    name = resolve_channel_name(item)
                    with self._lock:
                        registered = self._channels.get(name)
                    # 显式点名时优先用已注册的实现，未注册才退回默认（默认多为「未接入」）
                    obj = registered if registered is not None else self._default_channel(item)
                else:
                    obj = item
                    name = resolve_channel_name(getattr(obj, "name", type(obj).__name__))
                if name in seen:
                    continue
                seen.add(name)
                targets.append(obj)

        if not targets:
            self._emit("notification_skipped", "WARN",
                       {"reason": "没有可用的通知渠道", "title": _clip(title)})
            return []

        results: list[dict] = []
        for channel in targets:
            name = resolve_channel_name(getattr(channel, "name", type(channel).__name__))
            attempts = 0
            error: str | None = None
            ok = False
            while attempts <= self.max_retries:
                attempts += 1
                try:
                    channel.send(title=title, message=message, severity=severity, detail=detail)
                    ok = True
                    error = None
                    break
                except Exception as exc:  # 任何渠道异常都不允许阻断主流程
                    error = f"{type(exc).__name__}: {exc}"
            if ok:
                self._emit("notification_delivery", "INFO",
                           {"channel": name, "ok": True, "attempts": attempts, "title": _clip(title)})
            else:
                self._log.warning(
                    "通知投递失败（不阻断主流程）：channel=%s error=%s title=%s", name, error, _clip(title)
                )
                self._emit("notification_delivery", "WARN",
                           {"channel": name, "ok": False, "attempts": attempts, "error": error,
                            "title": _clip(title)})
            results.append({"channel": name, "ok": ok, "attempts": attempts, "error": error})
        return results

    def _emit(self, event: str, severity: str, detail: dict) -> None:
        if self._events is None:
            return
        try:
            self._events.emit(event, severity=severity, detail=detail)
        except Exception as exc:  # pragma: no cover - 事件日志绝不阻断
            self._log.warning("通知事件写入失败（不阻断主流程）：%s", exc)


__all__ = [
    "NotificationService",
    "NotifyChannel",
    "CallableChannel",
    "UnimplementedChannel",
    "WebhookChannel",
    "LogChannel",
    "CHANNEL_ALIASES",
    "CANONICAL_CHANNELS",
    "resolve_channel_name",
]


class NotifyChannel:  # pragma: no cover - 仅作文档化协议
    """渠道协议：`name: str` + `send(*, title, message, severity, detail)`。"""

    name: str = ""

    def send(self, *, title: str, message: str, severity: str = "INFO", detail: dict | None = None) -> None:
        raise NotImplementedError
