"""通知与事件日志测试。

重点：
- **通知失败只写 WARN 日志，绝不阻断主流程**（含一个渠道失败不影响其它渠道）；
- 未配置/未接入的渠道**不假成功**；
- 事件行 schema 必须是 CONTRACTS §10（含 namespace=REVIEW），且写失败不抛。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_review_fakes import FakeClock, recording_channel  # noqa: E402

from bettercomfyui.models.entities import AssetReviewParams  # noqa: E402
from bettercomfyui.review import events as review_events  # noqa: E402
from bettercomfyui.review.errors import NotificationError  # noqa: E402
from bettercomfyui.review.events import EVENT_FIELDS, ReviewEventLog  # noqa: E402
from bettercomfyui.review.notify import (  # noqa: E402
    NotificationService,
    UnimplementedChannel,
    WebhookChannel,
)


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------


def test_notification_failure_does_not_block_and_other_channels_still_deliver(caplog):
    service = NotificationService([recording_channel("OK"), recording_channel("BAD", fail=True)])
    with caplog.at_level(logging.WARNING, logger="bettercomfyui.review.notify"):
        results = service.notify(title="t", message="m")
    assert {r["channel"] for r in results} == {"OK", "BAD"}
    ok = {r["channel"]: r["ok"] for r in results}
    assert ok == {"OK": True, "BAD": False}
    assert any("通知投递失败" in record.message for record in caplog.records)


def test_notification_emits_warn_event_on_failure():
    log = ReviewEventLog(project=None)
    service = NotificationService([recording_channel("BAD", fail=True)], event_log=log)
    results = service.notify(title="t", message="m")
    assert results[0]["ok"] is False
    events = log.entries(event="notification_delivery")
    assert events[-1]["severity"] == "WARN"
    assert events[-1]["detail"]["ok"] is False


def test_notification_success_event_is_info():
    log = ReviewEventLog(project=None)
    service = NotificationService([recording_channel("OK")], event_log=log)
    assert service.notify(title="t", message="m")[0]["ok"] is True
    assert log.entries(event="notification_delivery")[-1]["severity"] == "INFO"


def test_unknown_channel_name_is_reported_not_silently_skipped():
    service = NotificationService([])
    results = service.notify(title="t", message="m", channels=["NOPE"])
    assert results[0]["channel"] == "NOPE"
    assert results[0]["ok"] is False
    assert "未接入" in results[0]["error"]


def test_default_channels_are_not_faked():
    """默认配置 popup+sound 在无界面进程里必须显式失败（绝不假成功）。"""
    service = NotificationService(params=AssetReviewParams())
    assert service.registered_channels() == ["POPUP", "SOUND"]
    results = service.notify(title="t", message="m")
    assert len(results) == 2
    assert all(r["ok"] is False for r in results)
    assert all(isinstance(service._channels[r["channel"]], UnimplementedChannel) for r in results)


def test_webhook_requires_config_and_transport():
    channel = WebhookChannel("FEISHU")
    with pytest.raises(NotificationError):
        channel.send(title="t", message="m")
    channel = WebhookChannel("FEISHU", url="https://example.invalid/hook")
    with pytest.raises(NotificationError):
        channel.send(title="t", message="m")
    sent: list[dict] = []
    channel = WebhookChannel(
        "FEISHU", url="https://example.invalid/hook",
        transport=lambda **kwargs: sent.append(kwargs),
    )
    channel.send(title="t", message="m", severity="WARN", detail={"a": 1})
    assert sent[0]["url"] == "https://example.invalid/hook"
    assert sent[0]["payload"]["severity"] == "WARN"


def test_no_channels_configured_is_warned_not_silent():
    log = ReviewEventLog(project=None)
    service = NotificationService([], event_log=log)
    assert service.notify(title="t", message="m") == []
    assert log.entries(event="notification_skipped")[-1]["severity"] == "WARN"


def test_retries_are_counted():
    attempts = {"n": 0}

    def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("boom")

    service = NotificationService({"FLAKY": flaky}, max_retries=2)
    results = service.notify(title="t", message="m")
    assert results[0]["ok"] is True
    assert results[0]["attempts"] == 3
    assert attempts["n"] == 3


def test_register_channel_replaces_default():
    service = NotificationService(params=AssetReviewParams())
    service.register_channel(recording_channel("POPUP"))
    results = service.notify(title="t", message="m", channels=["popup"])
    assert results[0]["channel"] == "POPUP"
    assert results[0]["ok"] is True


# ---------------------------------------------------------------------------
# 事件日志
# ---------------------------------------------------------------------------


def test_event_record_schema_matches_contract():
    clock = FakeClock()
    log = ReviewEventLog(project=None, clock=clock, project_id="pid123")
    record = log.emit("unit_event", stage="ASSET_REVIEW", object_id="A1", version="v1",
                      mode="HUMAN", detail={"k": "v"}, severity="warn")
    assert tuple(record) == EVENT_FIELDS, "字段顺序必须与 CONTRACTS §10 一致"
    assert record["namespace"] == "REVIEW"
    assert record["severity"] == "WARN"
    assert record["ts"] == clock()
    assert record["project_id"] == "pid123"


def test_memory_mode_is_explicit_and_warns(caplog):
    review_events.reset_store_events_cache()
    with caplog.at_level(logging.WARNING, logger="bettercomfyui.review.events"):
        log = ReviewEventLog(project=None)
    assert log.mode == "memory"
    assert log.persistence_available is False
    assert any("未落盘" in record.message for record in caplog.records)


def test_invalid_severity_and_event_rejected():
    log = ReviewEventLog(project=None)
    with pytest.raises(ValueError):
        log.emit("x", severity="LOUD")
    with pytest.raises(ValueError):
        log.emit("")
    with pytest.raises(ValueError):
        log.emit(None)  # type: ignore[arg-type]


def test_writer_failure_only_warns(caplog):
    def broken(record):
        raise OSError("disk full")

    log = ReviewEventLog(project=None, writer=broken)
    with caplog.at_level(logging.WARNING, logger="bettercomfyui.review.events"):
        record = log.emit("unit_event")
    assert record["event"] == "unit_event"
    assert log.write_failures == 1
    assert log.entries()  # 内存镜像仍保留
    assert any("事件日志写入失败" in r.message for r in caplog.records)


def test_store_events_write_error_is_counted(monkeypatch):
    """store.append_event 返回 `_write_error` 时必须视为写失败（不静默假装成功）。"""

    def append_event(project, *, event, namespace="BETTERCOMYUI", **kwargs):
        return {"event": event, "namespace": namespace, "_write_error": "disk full"}

    fake = types.ModuleType("bettercomfyui.store.events")
    fake.append_event = append_event
    monkeypatch.setitem(sys.modules, "bettercomfyui.store.events", fake)
    review_events.reset_store_events_cache()
    try:
        log = ReviewEventLog(project=Path("."), clock=FakeClock())
        assert log.mode == "store"
        log.emit("unit_event")
        assert log.write_failures == 1
    finally:
        review_events.reset_store_events_cache()


def test_adapter_delegates_to_store_events_namespace_review(monkeypatch, tmp_path):
    calls: list[dict] = []

    def append_event(project, *, event, namespace="BETTERCOMYUI", stage=None, object_id=None,
                     version=None, mode=None, detail=None, severity="INFO", fsync=True):
        calls.append({"project": project, "event": event, "namespace": namespace,
                      "severity": severity, "detail": detail, "stage": stage})
        return {"ok": True}

    fake = types.ModuleType("bettercomfyui.store.events")
    fake.append_event = append_event
    monkeypatch.setitem(sys.modules, "bettercomfyui.store.events", fake)
    review_events.reset_store_events_cache()
    try:
        log = ReviewEventLog(project=tmp_path, clock=FakeClock())
        assert log.mode == "store"
        assert log.persistence_available is True
        log.emit("mode_switched", detail={"a": 1}, severity="WARN")
        assert calls == [{
            "project": tmp_path,
            "event": "mode_switched",
            "namespace": "REVIEW",
            "severity": "WARN",
            "detail": {"a": 1},
            "stage": None,
        }]
    finally:
        review_events.reset_store_events_cache()


def test_entries_filters_and_tail():
    log = ReviewEventLog(project=None)
    log.emit("a")
    log.emit("b", severity="WARN")
    assert [e["event"] for e in log.entries(severity="WARN")] == ["b"]
    assert log.has_event("a") is True
    assert [e["event"] for e in log.tail(1)] == ["b"]
    log.clear()
    assert log.entries() == []
