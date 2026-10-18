"""H_arb 仲裁（方案第七部分·一 第 9/10 条 + 参数表三「仲裁规则」）。

规则（逐字落地，**平局按方案：等于时模型胜出**）：
- 人工结果时间戳 **<**  模型审核完成时间戳 → **人工胜出** + 发 `ABORT_SIGNAL`
- 人工结果时间戳 **>=** 模型审核完成时间戳 → **模型胜出**，人工结果**仅写日志**
- 平局（相等）→ **模型胜出**

时间戳口径：只使用 `ReviewAttempt.backend_ts`（统一后端时间戳，+08:00 ISO8601）。
`client_ts` 一律不参与仲裁——缺 `backend_ts` 直接抛 `ArbitrationError`，绝不猜。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models.entities import LOCAL_TZ
from ._bridge import arb_id as _arb_id
from .errors import ArbitrationError

#: 仲裁只依赖这两个字段（CONTRACTS §6 / models.entities.ReviewRecord）
HUMAN_KEY = "human_attempt"
MODEL_KEY = "model_attempt"


def _require_attempt(cycle: dict, key: str) -> dict:
    node = cycle.get(key)
    if not isinstance(node, dict):
        raise ArbitrationError(
            f"仲裁需要 {key}（ReviewAttempt 字典），实际：{type(node).__name__}；"
            "人工与模型结果必须都已产生才能进入仲裁"
        )
    return node


def parse_backend_ts(value: Any, label: str) -> datetime:
    """解析统一后端时间戳；缺失/非法一律抛错（禁止回退到客户端时钟）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ArbitrationError(
            f"{label}.backend_ts 缺失：仲裁只认统一后端时间戳（models.entities.now_iso()，+08:00）；"
            "客户端时钟一律不参与仲裁"
        )
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ArbitrationError(f"{label}.backend_ts 不是合法 ISO8601：{value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _require_bool(attempt: dict, key: str, label: str) -> bool:
    value = attempt.get(key)
    if not isinstance(value, bool):
        raise ArbitrationError(
            f"{label}.{key} 必须是 bool（实际 {type(value).__name__}）——"
            "绝不用真值判断代替明确的通过/拒绝结论"
        )
    return value


def arbitrate(cycle: dict) -> dict:
    """返回 `{"winner","abort_signal","arb_id"}` 及判定明细。纯函数，不做任何 I/O。"""
    if not isinstance(cycle, dict):
        raise ArbitrationError(f"arbitrate 需要 dict（ReviewRecord.to_dict()），实际 {type(cycle).__name__}")

    human = _require_attempt(cycle, HUMAN_KEY)
    model = _require_attempt(cycle, MODEL_KEY)

    cycle_h = human.get("review_cycle_id")
    cycle_m = model.get("review_cycle_id")
    if cycle_h and cycle_m and str(cycle_h) != str(cycle_m):
        raise ArbitrationError(
            f"禁止跨审核周期仲裁：human_attempt.review_cycle_id={cycle_h!r} "
            f"!= model_attempt.review_cycle_id={cycle_m!r}"
        )

    human_id = human.get("attempt_id")
    model_id = model.get("attempt_id")
    if not human_id or not model_id:
        raise ArbitrationError(
            "仲裁需要 attempt_id 作为审核记录 ID（ARB_{人工审核ID}_{模型审核ID}）"
        )

    human_ts = parse_backend_ts(human.get("backend_ts"), "human_attempt")
    model_ts = parse_backend_ts(model.get("backend_ts"), "model_attempt")
    human_passed = _require_bool(human, "passed", "human_attempt")
    model_passed = _require_bool(model, "passed", "model_attempt")

    tie = human_ts == model_ts
    if human_ts < model_ts:
        winner = "HUMAN"
        abort_signal = True
        rule = "human_ts < model_ts → 人工胜出 + ABORT_SIGNAL"
    else:
        winner = "MODEL"
        abort_signal = False
        rule = "human_ts >= model_ts → 模型胜出（含平局），人工结果仅写日志"

    return {
        "winner": winner,
        "abort_signal": abort_signal,
        "arb_id": _arb_id(str(human_id), str(model_id)),
        "human_ts": human_ts.isoformat(timespec="seconds"),
        "model_ts": model_ts.isoformat(timespec="seconds"),
        "tie": tie,
        "human_passed": human_passed,
        "model_passed": model_passed,
        "human_logged_only": winner == "MODEL",
        "human_attempt_id": str(human_id),
        "model_attempt_id": str(model_id),
        "rule": rule,
    }


__all__ = ["arbitrate", "parse_backend_ts", "HUMAN_KEY", "MODEL_KEY"]
