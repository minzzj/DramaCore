"""H_arb 仲裁测试：三分支 + 平局（**平局模型胜出**）+ 时间戳口径。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bettercomfyui.models.entities import ReviewAttempt  # noqa: E402
from bettercomfyui.models.enums import ReviewMode  # noqa: E402
from bettercomfyui.review.arbiter import arbitrate  # noqa: E402
from bettercomfyui.review.errors import ArbitrationError  # noqa: E402

CYCLE = "ASSET:A1:v1"


def attempt(attempt_id: str, ts: str, passed: bool, mode=ReviewMode.HUMAN) -> dict:
    return ReviewAttempt(
        attempt_id=attempt_id,
        review_cycle_id=CYCLE,
        mode=mode,
        passed=passed,
        backend_ts=ts,
    ).to_dict()


def cycle_dict(human: dict | None, model: dict | None) -> dict:
    return {"review_cycle_id": CYCLE, "human_attempt": human, "model_attempt": model}


def test_human_earlier_wins_and_aborts():
    human = attempt("ATTEMPT_A1_v1_1", "2026-10-16T10:00:00+08:00", True)
    model = attempt("ATTEMPT_A1_v1_2", "2026-10-16T10:00:01+08:00", False, ReviewMode.MODEL)
    result = arbitrate(cycle_dict(human, model))
    assert result["winner"] == "HUMAN"
    assert result["abort_signal"] is True
    assert result["human_logged_only"] is False
    assert result["tie"] is False
    assert result["arb_id"] == "ARB_ATTEMPT_A1_v1_1_ATTEMPT_A1_v1_2"


def test_human_earlier_rejected_still_wins():
    human = attempt("H1", "2026-10-16T10:00:00+08:00", False)
    model = attempt("M1", "2026-10-16T10:00:05+08:00", True, ReviewMode.MODEL)
    result = arbitrate(cycle_dict(human, model))
    assert result["winner"] == "HUMAN"
    assert result["abort_signal"] is True
    assert result["human_passed"] is False


def test_model_earlier_wins_and_human_only_logged():
    human = attempt("H1", "2026-10-16T10:00:05+08:00", True)
    model = attempt("M1", "2026-10-16T10:00:00+08:00", False, ReviewMode.MODEL)
    result = arbitrate(cycle_dict(human, model))
    assert result["winner"] == "MODEL"
    assert result["abort_signal"] is False
    assert result["human_logged_only"] is True
    assert result["tie"] is False


def test_tie_goes_to_model():
    """方案参数表三：等于时模型胜出（极易写反，单独断言）。"""
    ts = "2026-10-16T10:00:00+08:00"
    result = arbitrate(cycle_dict(attempt("H1", ts, True), attempt("M1", ts, False, ReviewMode.MODEL)))
    assert result["winner"] == "MODEL"
    assert result["abort_signal"] is False
    assert result["tie"] is True
    assert "平局" in result["rule"]


def test_missing_attempts_raise():
    with pytest.raises(ArbitrationError):
        arbitrate(cycle_dict(None, attempt("M1", "2026-10-16T10:00:00+08:00", True, ReviewMode.MODEL)))
    with pytest.raises(ArbitrationError):
        arbitrate(cycle_dict(attempt("H1", "2026-10-16T10:00:00+08:00", True), None))
    with pytest.raises(ArbitrationError):
        arbitrate({"review_cycle_id": CYCLE})


def test_missing_or_client_only_timestamp_raises():
    human = {"attempt_id": "H1", "passed": True, "review_cycle_id": CYCLE,
             "client_ts": 1.0}  # 只有客户端时钟
    model = attempt("M1", "2026-10-16T10:00:00+08:00", True, ReviewMode.MODEL)
    with pytest.raises(ArbitrationError) as excinfo:
        arbitrate(cycle_dict(human, model))
    assert "后端时间戳" in str(excinfo.value)

    human_bad = {"attempt_id": "H1", "passed": True, "backend_ts": "not-a-timestamp",
                 "review_cycle_id": CYCLE}
    with pytest.raises(ArbitrationError):
        arbitrate(cycle_dict(human_bad, model))


def test_passed_must_be_bool():
    human = {"attempt_id": "H1", "passed": "yes", "backend_ts": "2026-10-16T10:00:00+08:00",
             "review_cycle_id": CYCLE}
    model = attempt("M1", "2026-10-16T10:00:00+08:00", True, ReviewMode.MODEL)
    with pytest.raises(ArbitrationError):
        arbitrate(cycle_dict(human, model))


def test_cross_cycle_arbitration_forbidden():
    human = attempt("H1", "2026-10-16T10:00:00+08:00", True)
    model = attempt("M1", "2026-10-16T10:00:00+08:00", True, ReviewMode.MODEL)
    model["review_cycle_id"] = "ASSET:A1:v2"
    with pytest.raises(ArbitrationError):
        arbitrate(cycle_dict(human, model))


def test_attempt_id_required_for_arb_id():
    human = attempt("H1", "2026-10-16T10:00:00+08:00", True)
    human.pop("attempt_id")
    model = attempt("M1", "2026-10-16T10:00:00+08:00", True, ReviewMode.MODEL)
    with pytest.raises(ArbitrationError):
        arbitrate(cycle_dict(human, model))


def test_arbitrate_is_pure():
    human = attempt("H1", "2026-10-16T10:00:00+08:00", True)
    model = attempt("M1", "2026-10-16T10:00:00+08:00", True, ReviewMode.MODEL)
    payload = cycle_dict(human, model)
    snapshot = repr(payload)
    arbitrate(payload)
    assert repr(payload) == snapshot, "arbitrate 不得修改入参（纯函数）"
