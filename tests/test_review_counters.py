"""计数器测试：CNT1/CNT2/CNT3 语义隔离、绑定逻辑任务、版本递增不清零、唯一合法 reset。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bettercomfyui.models.enums import CounterKind  # noqa: E402
from bettercomfyui.review.counters import CounterStore  # noqa: E402
from bettercomfyui.review.errors import CounterError, IllegalCounterReset  # noqa: E402
from bettercomfyui.review.events import ReviewEventLog  # noqa: E402


def store() -> CounterStore:
    return CounterStore(event_log=ReviewEventLog(project=None))


def test_key_binds_logical_task_not_version():
    counters = store()
    key = counters.key("ASSET_A1", CounterKind.CNT1)
    assert key == "CNT_ASSET_A1_CNT1"
    assert "v1" not in key


def test_increment_get_and_scope():
    counters = store()
    assert counters.get("A1", CounterKind.CNT1) == 0
    assert counters.increment("A1", CounterKind.CNT1) == 1
    assert counters.increment("A1", CounterKind.CNT1) == 2
    assert counters.increment("A1", CounterKind.CNT2) == 1
    assert counters.increment("A1", CounterKind.CNT3) == 1
    assert counters.scope("A1") == {"CNT1": 2, "CNT2": 1, "CNT3": 1, "TECH_RETRY": 0}


def test_kinds_never_accumulate_into_each_other():
    counters = store()
    for _ in range(3):
        counters.increment("A1", CounterKind.CNT2)
    assert counters.get("A1", CounterKind.CNT1) == 0
    assert counters.get("A1", CounterKind.CNT3) == 0
    for _ in range(2):
        counters.increment("A1", CounterKind.CNT3)
    assert counters.get("A1", CounterKind.CNT2) == 3
    assert counters.get("A1", CounterKind.CNT1) == 0


def test_version_bump_never_resets_any_counter():
    """R4：版本号递增绝不清零。计数器绑定逻辑任务 ID，所以换版本仍取到同一个计数器。"""
    counters = store()
    obj_id = "ASSET_A1"  # 逻辑素材 ID，v1..v5 共用
    for version in ("v1", "v2", "v3", "v4", "v5"):
        counters.increment(obj_id, CounterKind.CNT1)
        assert counters.get(obj_id, CounterKind.CNT1) == int(version[1:])
    assert counters.get(obj_id, CounterKind.CNT1) == 5
    assert counters.get(obj_id, CounterKind.CNT2) == 0


def test_reset_only_allowed_for_cnt1():
    counters = store()
    counters.increment("A1", CounterKind.CNT1)
    counters.increment("A1", CounterKind.CNT1)
    counters.reset("A1", CounterKind.CNT1)
    assert counters.get("A1", CounterKind.CNT1) == 0
    for kind in (CounterKind.CNT2, CounterKind.CNT3):
        with pytest.raises(IllegalCounterReset):
            counters.reset("A1", kind)
        assert counters.get("A1", kind) == 0


def test_rollback_increment_is_bounded():
    counters = store()
    counters.increment("A1", CounterKind.CNT1)
    assert counters.rollback_increment("A1", CounterKind.CNT1) == 0
    with pytest.raises(CounterError):
        counters.rollback_increment("A1", CounterKind.CNT1)


def test_tech_retry_is_independent_of_cnts():
    counters = store()
    for _ in range(5):
        counters.increment_tech_retry("A1")
    assert counters.get_tech_retry("A1") == 5
    assert counters.scope("A1")["CNT1"] == 0
    assert counters.scope("A1")["CNT2"] == 0
    assert counters.scope("A1")["CNT3"] == 0


def test_tech_retry_key_does_not_go_through_counter_kind():
    """`TECH` 不是合法 CounterKind（store.ids.cnt_id 会强校验拒绝），必须直接拼 key。"""
    counters = store()
    counters.increment_tech_retry("A1")
    assert "CNT_A1_TECH" in counters.keys()
    for kind in CounterKind:
        assert kind.value != "TECH"
        assert counters.key("A1", kind) != "CNT_A1_TECH"
    assert counters.snapshot()["tech_retry"] == {"CNT_A1_TECH": 1}


def test_scope_and_snapshot_work_with_real_store_ids():
    """回归：director-exec 实测 `scope()` 曾因 cnt_id("TECH") 抛 StoreError。"""
    counters = store()
    counters.increment("A1", CounterKind.CNT2)
    counters.increment_tech_retry("A1")
    assert counters.scope("A1") == {"CNT1": 0, "CNT2": 1, "CNT3": 0, "TECH_RETRY": 1}
    assert counters.snapshot()["counts"] == {"CNT_A1_CNT2": 1}


def test_invalid_kind_and_obj_id():
    counters = store()
    with pytest.raises(CounterError):
        counters.increment("A1", "CNT9")
    with pytest.raises(CounterError):
        counters.increment("A1", None)
    with pytest.raises(CounterError):
        counters.increment("", CounterKind.CNT1)
    with pytest.raises(CounterError):
        counters.get(None, CounterKind.CNT1)  # type: ignore[arg-type]


def test_concurrent_increment_loses_nothing():
    counters = store()
    barrier = threading.Barrier(100)

    def worker():
        barrier.wait()
        counters.increment("A1", CounterKind.CNT1)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert counters.get("A1", CounterKind.CNT1) == 100


def test_snapshot_restore():
    counters = store()
    counters.increment("A1", CounterKind.CNT1)
    counters.increment("A1", CounterKind.CNT3)
    counters.increment_tech_retry("A1")
    snapshot = counters.snapshot()
    fresh = store()
    fresh.restore(snapshot)
    assert fresh.scope("A1") == {"CNT1": 1, "CNT2": 0, "CNT3": 1, "TECH_RETRY": 1}
    with pytest.raises(CounterError):
        fresh.restore("not-a-dict")  # type: ignore[arg-type]


def test_counter_events_are_logged():
    log = ReviewEventLog(project=None)
    counters = CounterStore(event_log=log)
    counters.increment("A1", CounterKind.CNT1)
    counters.reset("A1", CounterKind.CNT1)
    counters.increment_tech_retry("A1")
    assert [e["event"] for e in log.entries()] == [
        "counter_incremented",
        "counter_reset",
        "tech_retry_incremented",
    ]
    assert log.entries()[0]["detail"]["cnt_id"] == "CNT_A1_CNT1"
