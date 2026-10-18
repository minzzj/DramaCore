"""状态机测试：每条合法边 + 非法组合 + 终态 + 取消 + 并发。

规格事实断言（Lead 明确要求）：`REVIEW_TRANSITIONS` 中**不存在** `target=AWAITING_REVIEW`
的边 → 若将来有人加一条假边，本文件立即变红。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bettercomfyui.models.enums import (  # noqa: E402
    REVIEW_ABORTABLE_STATES,
    REVIEW_TRANSITIONS,
    ReviewState,
)
from bettercomfyui.review.errors import IllegalTransition  # noqa: E402
from bettercomfyui.review.events import ReviewEventLog  # noqa: E402
from bettercomfyui.review.state import (  # noqa: E402
    USER_CANCEL_EVENT,
    ReviewCycle,
    events_for_state,
)

ALL_EVENTS = sorted({event for (_, event) in REVIEW_TRANSITIONS} | {USER_CANCEL_EVENT})

EDGE_CASES = [
    (src, event, target)
    for (src, event), target in sorted(REVIEW_TRANSITIONS.items(), key=lambda kv: (kv[0][0].value, kv[0][1]))
]

ILLEGAL_CASES = [
    (ReviewState.RECEIVED, "human_pass"),
    (ReviewState.RECEIVED, "scan_completed"),
    (ReviewState.VALIDATING, "scan_completed"),
    (ReviewState.VALIDATING, "human_pass"),
    (ReviewState.SCANNING, "human_pass"),
    (ReviewState.SCANNING, "score_above_threshold"),
    (ReviewState.CLASSIFYING, "human_pass"),
    (ReviewState.AWAITING_REVIEW, "scan_completed"),
    (ReviewState.AWAITING_REVIEW, "score_above_threshold"),
    (ReviewState.TIMEOUT, "human_pass"),
    (ReviewState.ALLOWED, "human_pass"),
    (ReviewState.ALLOWED, "score_above_threshold"),
    (ReviewState.FAILED, "human_pass"),
    (ReviewState.ABORTED, "start_review"),
    (ReviewState.BLOCKED, "score_above_threshold"),
    (ReviewState.BLOCKED, "human_pass"),
    (ReviewState.ARBITRATING, "start_review"),
    (ReviewState.FALLBACK, "human_pass"),
]


@pytest.mark.parametrize("src,event,target", EDGE_CASES, ids=[f"{s.value}-{e}" for s, e, _ in EDGE_CASES])
def test_every_legal_edge(src, event, target):
    cycle = ReviewCycle(f"CYCLE:{src.value}", state=src)
    assert cycle.can_fire(event) is True
    assert cycle.fire(event) is target
    assert cycle.state is target


@pytest.mark.parametrize("src,event", ILLEGAL_CASES, ids=[f"{s.value}-{e}" for s, e in ILLEGAL_CASES])
def test_illegal_combination_raises(src, event):
    cycle = ReviewCycle(f"CYCLE:{src.value}", state=src)
    with pytest.raises(IllegalTransition) as excinfo:
        cycle.fire(event)
    error = excinfo.value
    assert error.from_state is src
    assert error.event == event
    assert cycle.state is src, "非法转移绝不能改变状态（更不能静默忽略）"
    assert "非法状态转移" in str(error)


def test_no_incoming_edge_to_awaiting_review():
    """规格事实：人工等待态没有任何入边（只能作为初始状态进入）。"""
    targets = set(REVIEW_TRANSITIONS.values())
    assert ReviewState.AWAITING_REVIEW not in targets, (
        "REVIEW_TRANSITIONS 出现了 target=AWAITING_REVIEW 的边；"
        "方案第七部分·六 的表格里没有这条边，人工周期必须以初始状态进入"
    )
    cycle = ReviewCycle("C:human", state=ReviewState.AWAITING_REVIEW)
    assert cycle.state is ReviewState.AWAITING_REVIEW
    assert ReviewState.AWAITING_REVIEW.is_terminal is False


def test_terminal_states_reject_everything_not_explicitly_allowed():
    for state in ReviewState:
        if not state.is_terminal:
            continue
        for event in ALL_EVENTS:
            cycle = ReviewCycle(f"CYCLE:{state.value}", state=state)
            if (state, event) in REVIEW_TRANSITIONS:
                assert cycle.fire(event) is REVIEW_TRANSITIONS[(state, event)]
            else:
                with pytest.raises(IllegalTransition):
                    cycle.fire(event)


def test_user_cancel_only_from_abortable_states():
    for state in ReviewState:
        cycle = ReviewCycle(f"CYCLE:{state.value}", state=state)
        if state in REVIEW_ABORTABLE_STATES:
            assert cycle.fire("cancel") is ReviewState.ABORTED
            assert cycle.last_event == USER_CANCEL_EVENT
        else:
            with pytest.raises(IllegalTransition):
                cycle.fire(USER_CANCEL_EVENT)


def test_blocked_is_not_terminal_and_can_only_go_to_validating():
    cycle = ReviewCycle("C:blocked", state=ReviewState.BLOCKED)
    assert cycle.state.is_terminal is False
    assert cycle.allowed_events() == ("optimized_new_version_created", "user_cancel")
    with pytest.raises(IllegalTransition):
        cycle.fire("human_pass")
    assert cycle.fire("optimized_new_version_created") is ReviewState.VALIDATING


def test_unknown_event_and_bad_input():
    cycle = ReviewCycle("C:x")
    with pytest.raises(IllegalTransition) as excinfo:
        cycle.fire("no_such_event")
    assert "start_review" in str(excinfo.value), "错误信息必须列出当前状态允许的事件"
    with pytest.raises(ValueError):
        cycle.fire("")
    with pytest.raises(ValueError):
        cycle.fire(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReviewCycle("")
    with pytest.raises(ValueError):
        ReviewCycle("C:bad", state="NOT_A_STATE")


def test_history_and_events_recorded():
    log = ReviewEventLog(project=None)
    cycle = ReviewCycle("C:hist", object_type="ASSET", object_id="A1", version="v1", event_log=log)
    cycle.fire("start_review", reason="unit")
    cycle.fire("validation_passed")
    assert [h["event"] for h in cycle.history] == ["start_review", "validation_passed"]
    assert cycle.history[0]["from"] == "RECEIVED"
    assert cycle.history[0]["to"] == "VALIDATING"
    events = log.entries(event="review_state_changed")
    assert len(events) == 2
    assert events[0]["detail"]["to"] == "VALIDATING"
    assert events[0]["namespace"] == "REVIEW"


def test_snapshot_restore_round_trip():
    cycle = ReviewCycle("C:snap", object_type="ASSET", object_id="A1", version="v1")
    cycle.fire("start_review")
    cycle.fire("validation_passed")
    data = cycle.snapshot()
    restored = ReviewCycle.restore(data)
    assert restored.state is ReviewState.SCANNING
    assert restored.review_cycle_id == "C:snap"
    assert [h["event"] for h in restored.history] == ["start_review", "validation_passed"]


def test_concurrent_fire_is_serialized():
    cycle = ReviewCycle("C:race")
    successes: list[ReviewState] = []
    failures: list[Exception] = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        try:
            successes.append(cycle.fire("start_review"))
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1, "并发 fire 必须恰好一次成功"
    assert len(failures) == 15
    assert all(isinstance(f, IllegalTransition) for f in failures)
    assert cycle.state is ReviewState.VALIDATING
    assert len(cycle.history) == 1


def test_events_for_state_matches_table():
    for state in ReviewState:
        expected = {event for (src, event) in REVIEW_TRANSITIONS if src is state}
        if state in REVIEW_ABORTABLE_STATES:
            expected.add(USER_CANCEL_EVENT)
        assert set(events_for_state(state)) == expected
