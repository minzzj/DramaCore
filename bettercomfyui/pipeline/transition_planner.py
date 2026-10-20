"""SG6 转场位置决策（ROUND55）：纯规则引擎 —— 决定「哪里转场、哪里不转场」。

设计口径（离线、可解释、不依赖 GPU/ffmpeg/网络）：
- 只读段信息字典，输出逐接缝计划，可直接传给
  `bettercomfyui_comfy.director_edit.splice_segments(transition_plan=...)`；
- 规则引擎不做"猜测式 AI 幻觉"：每条决策都带 `reason` 与 `decision_source`（命中哪条规则），
  无信息时明确走默认规则，不假装是模型判断；
- pin（批内原生连续）段内部强制「无/最轻」，绝不为了花哨转场打断同一次拍摄的连续镜头；
- 场景切换 / 时间跳跃 / 章节边界 → 叠化 / 黑场；边界相似度高 → 硬切保持连贯；
- 用户可固定转场（关闭自动）：`user_policy={"mode": "fixed", ...}` 或直接传 `fixed_transition`。

输入段信息（dict，字段全部可选，缺省按"未知"处理，不编数据）::

    prompt / 提示词             该段画面描述（检测场景切换关键词）
    score / final_score        段评分 0-100
    boundary_similarity        与下一段边界的相似度 0-1（也可放在下一段
                               的 boundary_similarity 表示与上一段）
    pin_continuous / pin / native_continuation / same_batch_pin
                               该段是否与上一段属于批内原生连续
    scene_change / 场景切换     显式标记该段开头是场景切换
    time_jump / 时间跳跃        显式标记该段开头时间跳跃
    chapter_boundary / is_chapter_start
                               该段开头是新章节
    explicit_transition        用户对该接缝的固定转场（覆盖自动规则）

输出：``{"auto", "rules_version", "cut_count", "cuts", "audit", "warnings"}``；
``cuts[i] = {"cut_index": i, "transition": "叠化", "duration": 0.5,
             "reason": "...", "decision_source": "scene_change",
             "boundary": {...}}``（cut_index 从 0 开始，指第 i 与 i+1 段之间）。
"""

from __future__ import annotations

from typing import Any

RULES_VERSION = "round55-1"

# 转场标签（与 director_edit 的中文标签一致；planner 不依赖 ffmpeg，不校验本机 tag）
T_NONE = "无"
T_FADE = "淡入淡出"
T_DISSOLVE = "叠化"
T_BLACK = "黑场"
T_WHITE = "白场"
T_SLIDE_LEFT = "左滑"
T_SLIDE_RIGHT = "右滑"
T_SLIDE_UP = "上滑"
T_SLIDE_DOWN = "下滑"
KNOWN_TRANSITIONS: tuple[str, ...] = (
    T_NONE, T_FADE, T_DISSOLVE, T_BLACK, T_WHITE,
    T_SLIDE_LEFT, T_SLIDE_RIGHT, T_SLIDE_UP, T_SLIDE_DOWN,
)

# 场景切换关键词（中文为主，英文补全）
SCENE_CHANGE_KEYWORDS: tuple[str, ...] = (
    "场景切换", "换场", "切到", "转到", "与此同时", "另一处", "另一边", "另一个场景",
    "画面切换到", "镜头切换到", "蒙太奇", "闪回", "回忆", "倒叙",
    "scene change", "cut to", "next scene", "meanwhile", "montage", "flashback",
)
# 时间跳跃关键词（比场景切换更强：黑场更适合表现"过了一段时间"）
TIME_JUMP_KEYWORDS: tuple[str, ...] = (
    "次日", "第二天", "翌日", "多年后", "第二天清晨", "一周后", "一个月后", "几小时后",
    "时间跳跃", "时间跳", "多年以后", "数年后", "十年后", "三年后",
    "the next day", "years later", "time jump",
)
# 章节边界关键词（片头/章节名，黑场分隔）
CHAPTER_KEYWORDS: tuple[str, ...] = (
    "第一章", "第二章", "第三章", "第四章", "第五章", "第六章",
    "篇章", "章节", "序幕", "尾声", "终章", "chapter",
)

_PIN_KEYS = ("pin_continuous", "pin", "native_continuation", "same_batch_pin",
             "batch_pin", "pin_continuous_from_prev")
_CHAPTER_KEYS = ("chapter_boundary", "is_chapter_start", "new_chapter", "章节边界")
_SCENE_KEYS = ("scene_change", "场景切换", "scene_switch")
_TIME_KEYS = ("time_jump", "时间跳跃", "time_skip")
_EXPLICIT_KEYS = ("explicit_transition", "transition", "fixed_transition")

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "auto",
    "default_transition": T_DISSOLVE,
    "default_duration": 0.4,
    "pin_transition": T_NONE,
    "pin_duration": 0.0,
    "scene_change_transition": T_DISSOLVE,
    "scene_change_duration": 0.5,
    "time_jump_transition": T_BLACK,
    "time_jump_duration": 0.6,
    "chapter_transition": T_BLACK,
    "chapter_duration": 0.6,
    "similar_transition": T_NONE,
    "similar_duration": 0.0,
    "similarity_threshold": 0.62,
    "different_transition": T_DISSOLVE,
    "different_duration": 0.5,
    "dissimilarity_threshold": 0.35,
    "low_score_threshold": 70.0,
    "low_score_transition": T_DISSOLVE,
    "low_score_duration": 0.4,
    "min_transition_seconds": 0.1,
    "max_transition_seconds": 2.0,
    "scene_keywords": list(SCENE_CHANGE_KEYWORDS),
    "time_jump_keywords": list(TIME_JUMP_KEYWORDS),
    "chapter_keywords": list(CHAPTER_KEYWORDS),
    "cut_overrides": {},
}


# ---------------------------------------------------------------------------
# 小工具：容错读字段
# ---------------------------------------------------------------------------
def _first(seg: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in seg and seg[key] is not None:
            return seg[key]
    return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "是", "有", "on", "pin")
    if isinstance(value, dict):
        for key in ("value", "continuous", "enabled", "on"):
            if key in value:
                return _as_bool(value[key])
    return False


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prompt_of(seg: dict) -> str:
    return str(_first(seg, ("prompt", "提示词", "description", "描述"), "") or "")


def _score_of(seg: dict) -> float | None:
    for key in ("final_score", "score", "quality_score", "review_score", "r1b_score"):
        if key in seg:
            value = _as_float(seg[key])
            if value is not None:
                return value
    scores = seg.get("scores")
    if isinstance(scores, dict) and scores:
        values = [v for v in (_as_float(x) for x in scores.values()) if v is not None]
        if values:
            return sum(values) / len(values)
    return None


def _similarity_at(segments: list[dict], index: int) -> tuple[float | None, str]:
    """取第 index 与 index+1 段边界的相似度（两段任一给出都认）。"""
    nxt = segments[index + 1]
    cur = segments[index]
    for key in ("boundary_similarity", "boundary_similarity_prev", "similarity_prev",
                "prev_similarity"):
        value = _as_float(nxt.get(key))
        if value is not None:
            return value, f"第{index + 2}段.{key}"
    for key in ("boundary_similarity", "similarity_to_next", "next_similarity"):
        value = _as_float(cur.get(key))
        if value is not None:
            return value, f"第{index + 1}段.{key}"
    return None, ""


def _pin_continuous(seg_a: dict, seg_b: dict) -> bool:
    if any(_as_bool(seg_b.get(key)) for key in _PIN_KEYS):
        return True
    for key in ("pin_continuous_to_next", "native_continuation_to_next",
                "same_batch_pin_to_next"):
        if _as_bool(seg_a.get(key)):
            return True
    return False


def _match(text: str, keywords: list[str]) -> str:
    haystack = (text or "").lower()
    for keyword in keywords:
        if keyword and keyword.lower() in haystack:
            return keyword
    return ""


def _clamp_duration(value: Any, policy: dict, *, warnings: list[str],
                    where: str) -> float:
    number = _as_float(value)
    if number is None:
        number = float(policy["default_duration"])
    low = float(policy["min_transition_seconds"])
    high = float(policy["max_transition_seconds"])
    if number < low:
        warnings.append(f"⚠️ {where} 转场时长 {number:.2f}s < 最小 {low:.2f}s → 钳制")
        number = low
    if number > high:
        warnings.append(f"⚠️ {where} 转场时长 {number:.2f}s > 最大 {high:.2f}s → 钳制")
        number = high
    return round(number, 3)


def _effective_policy(user_policy: dict | None) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    for key, value in (user_policy or {}).items():
        if key == "scene_keywords" and isinstance(value, (list, tuple)):
            policy["scene_keywords"] = list(SCENE_CHANGE_KEYWORDS) + [str(x) for x in value]
        elif key == "cut_overrides" and isinstance(value, dict):
            policy["cut_overrides"] = dict(value)
        elif value is not None:
            policy[key] = value
    mode = str(policy.get("mode") or "auto").strip().lower()
    if mode in ("fixed", "固定", "manual", "手动"):
        policy["mode"] = "fixed"
    elif mode in ("off", "关闭", "none"):
        policy["mode"] = "off"
    else:
        policy["mode"] = "auto"
    return policy


def _make_cut(cut_index: int, transition: str, duration: Any, reason: str,
              source: str, policy: dict, warnings: list[str],
              boundary: dict | None = None) -> dict:
    transition = str(transition or T_NONE).strip()
    if transition in ("无", "", "none", "None", "硬切"):
        transition = T_NONE
    if transition == T_NONE:
        duration_value = 0.0
    else:
        duration_value = _clamp_duration(duration, policy, warnings=warnings,
                                         where=f"接缝[{cut_index + 1}]")
    return {
        "cut_index": cut_index,
        "transition": transition,
        "duration": duration_value,
        "reason": reason,
        "decision_source": source,
        "boundary": dict(boundary or {}),
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def decide_transitions(
    segments: list[dict],
    *,
    user_policy: dict | None = None,
    fixed_transition: str | None = None,
    fixed_duration: float | None = None,
    auto: bool | None = None,
) -> dict:
    """决定每处接缝的转场（纯规则，离线可测）。

    参数：
      * ``segments``：段信息列表（见模块 docstring 字段表；允许空/单段）。
      * ``user_policy``：用户策略 dict（mode/fixed_transition/... 见 DEFAULT_POLICY）。
      * ``fixed_transition``：给定则等价于用户固定转场（关闭自动决策）。
      * ``fixed_duration``：固定转场时长（可选）。
      * ``auto``：False 时关闭自动决策（有 fixed_transition 用固定值，否则全部硬切）。

    返回：``{"auto", "rules_version", "mode", "cut_count", "cuts", "audit",
             "warnings", "policy"}``；``cuts`` 可直接作为
    ``director_edit.splice_segments(transition_plan=cuts)`` 的输入。
    """
    segments = list(segments or [])
    warnings: list[str] = []
    policy = _effective_policy(user_policy)
    if fixed_transition is not None:
        policy["mode"] = "fixed"
        policy["fixed_transition"] = fixed_transition
        if fixed_duration is not None:
            policy["fixed_duration"] = fixed_duration
    if auto is False and policy["mode"] == "auto":
        policy["mode"] = "fixed" if policy.get("fixed_transition") else "off"
    auto_enabled = policy["mode"] == "auto"

    counts: dict[str, int] = {}

    def _bump(source: str) -> None:
        counts[source] = counts.get(source, 0) + 1

    lines: list[str] = []
    cuts: list[dict] = []
    n = len(segments)
    cut_count = max(0, n - 1)

    if n == 0:
        warnings.append("⚠️ 段列表为空：不产生接缝决策")
    elif n == 1:
        lines.append("只有 1 段：无接缝，不涉及转场")
    else:
        for i in range(cut_count):
            seg_a, seg_b = segments[i], segments[i + 1]
            boundary = {
                "index": i,
                "from_segment": i + 1,
                "to_segment": i + 2,
                "score_prev": _score_of(seg_a),
                "score_next": _score_of(seg_b),
                "pin_continuous": _pin_continuous(seg_a, seg_b),
            }
            similarity, similarity_src = _similarity_at(segments, i)
            boundary["similarity"] = similarity
            boundary["similarity_src"] = similarity_src

            # 0) 用户固定转场 / 关闭自动
            if not auto_enabled:
                if policy["mode"] == "off":
                    cut = _make_cut(i, T_NONE, 0.0, "用户关闭自动转场 → 全部硬切",
                                    "user_off", policy, warnings, boundary)
                else:
                    fixed_label = str(policy.get("fixed_transition") or T_DISSOLVE)
                    fixed_sec = policy.get("fixed_duration",
                                           policy["default_duration"])
                    cut = _make_cut(i, fixed_label, fixed_sec,
                                    f"用户固定转场「{fixed_label}」→ 关闭自动决策",
                                    "user_fixed", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            # 1) 逐接缝用户覆盖（cut_overrides / 段上的 explicit_transition）
            override = (policy.get("cut_overrides") or {}).get(i)
            if override is None:
                explicit = _first(seg_b, _EXPLICIT_KEYS)
                if explicit is not None:
                    override = {"transition": explicit}
            if override is not None:
                if isinstance(override, str):
                    override = {"transition": override}
                label = str(override.get("transition") or T_NONE)
                seconds = override.get("duration", policy["default_duration"])
                cut = _make_cut(i, label, seconds,
                                str(override.get("reason") or "用户逐接缝指定"),
                                "user_override", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            # 2) pin（批内原生连续）→ 无/最轻，绝不打断
            if boundary["pin_continuous"]:
                cut = _make_cut(i, policy["pin_transition"], policy["pin_duration"],
                                "批内原生连续（pin）：同一次拍摄的连续镜头 → 不打断",
                                "pin_continuous", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            prompt_b = _prompt_of(seg_b)

            # 3) 章节边界 → 黑场
            chapter_kw = _match(prompt_b, list(policy["chapter_keywords"]))
            if _as_bool(_first(seg_b, _CHAPTER_KEYS)) or chapter_kw:
                reason = (f"章节边界（{chapter_kw}）→ 黑场分隔章节"
                          if chapter_kw else "章节边界 → 黑场分隔章节")
                cut = _make_cut(i, policy["chapter_transition"],
                                policy["chapter_duration"], reason,
                                "chapter_boundary", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            # 4) 时间跳跃 → 黑场（比叠化更能表达"过了一段时间"）
            time_kw = _match(prompt_b, list(policy["time_jump_keywords"]))
            if _as_bool(_first(seg_b, _TIME_KEYS)) or time_kw:
                reason = (f"时间跳跃（{time_kw}）→ 黑场表现时间流逝"
                          if time_kw else "时间跳跃 → 黑场表现时间流逝")
                cut = _make_cut(i, policy["time_jump_transition"],
                                policy["time_jump_duration"], reason,
                                "time_jump", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            # 5) 场景切换（显式标记或提示词关键词）→ 叠化
            scene_kw = _match(prompt_b, list(policy["scene_keywords"]))
            explicit_scene = _as_bool(_first(seg_b, _SCENE_KEYS)) or \
                _as_bool(_first(seg_a, ("scene_change_to_next", "next_scene_change")))
            if explicit_scene or scene_kw:
                reason = (f"场景切换（{scene_kw}）→ 叠化过渡"
                          if scene_kw else "场景切换 → 叠化过渡")
                cut = _make_cut(i, policy["scene_change_transition"],
                                policy["scene_change_duration"], reason,
                                "scene_change", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            # 6) 边界相似度：同场景→硬切保持连贯；差异大→叠化
            if similarity is not None:
                if similarity >= float(policy["similarity_threshold"]):
                    cut = _make_cut(i, policy["similar_transition"],
                                    policy["similar_duration"],
                                    f"边界相似度 {similarity:.2f} ≥ "
                                    f"{float(policy['similarity_threshold']):.2f}："
                                    "同场景延续 → 硬切保持连贯",
                                    "boundary_similar", policy, warnings, boundary)
                    cuts.append(cut)
                    _bump(cut["decision_source"])
                    lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                                 f"{cut['duration']:.2f}s ← {cut['reason']}")
                    continue
                if similarity <= float(policy["dissimilarity_threshold"]):
                    cut = _make_cut(i, policy["different_transition"],
                                    policy["different_duration"],
                                    f"边界相似度 {similarity:.2f} ≤ "
                                    f"{float(policy['dissimilarity_threshold']):.2f}："
                                    "差异大 → 叠化过渡",
                                    "boundary_dissimilar", policy, warnings, boundary)
                    cuts.append(cut)
                    _bump(cut["decision_source"])
                    lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                                 f"{cut['duration']:.2f}s ← {cut['reason']}")
                    continue

            # 7) 相邻段评分偏低 → 叠化缓冲
            scores = [s for s in (boundary["score_prev"], boundary["score_next"])
                      if s is not None]
            if scores and min(scores) < float(policy["low_score_threshold"]):
                cut = _make_cut(i, policy["low_score_transition"],
                                policy["low_score_duration"],
                                f"相邻段评分偏低（最低 {min(scores):.1f} < "
                                f"{float(policy['low_score_threshold']):.1f}）→ 叠化缓冲",
                                "low_score", policy, warnings, boundary)
                cuts.append(cut)
                _bump(cut["decision_source"])
                lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                             f"{cut['duration']:.2f}s ← {cut['reason']}")
                continue

            # 8) 默认
            cut = _make_cut(i, policy["default_transition"],
                            policy["default_duration"],
                            "常规接缝：无场景切换证据 → 默认叠化（可被用户策略覆盖）",
                            "default", policy, warnings, boundary)
            cuts.append(cut)
            _bump(cut["decision_source"])
            lines.append(f"接缝[{i + 1}→{i + 2}] {cut['transition']} "
                         f"{cut['duration']:.2f}s ← {cut['reason']}")

    return {
        "auto": auto_enabled,
        "rules_version": RULES_VERSION,
        "mode": policy["mode"],
        "cut_count": cut_count,
        "cuts": cuts,
        "warnings": warnings,
        "policy": {k: v for k, v in policy.items()
                   if k not in ("scene_keywords", "time_jump_keywords",
                                "chapter_keywords")},
        "audit": {
            "rules_version": RULES_VERSION,
            "auto": auto_enabled,
            "mode": policy["mode"],
            "segments": n,
            "cut_count": cut_count,
            "counts": counts,
            "lines": lines,
            "note": "纯规则引擎（非语义模型）：依据 pin/章节/时间跳跃/场景切换词/"
                    "边界相似度/段评分决策；无信息走默认规则，不编数据。",
        },
    }


def decide_transitions_off(
    segments: list[dict],
    *,
    transition: str = T_NONE,
    duration: float = 0.0,
) -> dict:
    """关闭自动转场（全部接缝用固定值；默认全硬切）。返回结构与 decide_transitions 一致。"""
    segments = list(segments or [])
    n = len(segments)
    label = str(transition or T_NONE)
    cut_duration = 0.0 if label in ("无", "", "none") else float(duration)
    cuts = [
        {
            "cut_index": i,
            "transition": label,
            "duration": cut_duration,
            "reason": "用户关闭自动转场（固定转场/硬切）",
            "decision_source": "user_off",
            "boundary": {"index": i, "from_segment": i + 1, "to_segment": i + 2},
        }
        for i in range(max(0, n - 1))
    ]
    lines = [f"接缝[{c['cut_index'] + 1}→{c['cut_index'] + 2}] {c['transition']} "
             f"{c['duration']:.2f}s ← 用户关闭自动转场" for c in cuts]
    return {
        "auto": False,
        "rules_version": RULES_VERSION,
        "mode": "off",
        "cut_count": max(0, n - 1),
        "cuts": cuts,
        "warnings": [] if n >= 2 else ["⚠️ 段数不足 2：无接缝可决策"],
        "policy": {"mode": "off"},
        "audit": {
            "rules_version": RULES_VERSION,
            "auto": False,
            "mode": "off",
            "segments": n,
            "cut_count": max(0, n - 1),
            "counts": {"user_off": max(0, n - 1)},
            "lines": lines,
            "note": "用户关闭自动转场：全部接缝使用固定值（默认硬切）。",
        },
    }


def plan_from_decision(decision: dict) -> list[dict]:
    """把决策结果转成 director_edit.splice_segments 可直接吃的 transition_plan。"""
    plan: list[dict] = []
    for cut in (decision or {}).get("cuts") or []:
        plan.append({
            "cut_index": int(cut["cut_index"]),
            "transition": str(cut["transition"]),
            "duration": float(cut.get("duration") or 0.0),
            "reason": str(cut.get("reason") or ""),
        })
    return plan


def audit_lines(decision: dict) -> list[str]:
    """决策审计行（给节点 audit/report 直接追加）。"""
    audit = (decision or {}).get("audit") or {}
    header = (f"转场决策（{audit.get('rules_version', RULES_VERSION)}，"
              f"auto={'开' if audit.get('auto') else '关'}，"
              f"接缝 {audit.get('cut_count', 0)} 处）")
    lines = [header] + [str(x) for x in (audit.get("lines") or [])]
    for warning in (decision or {}).get("warnings") or []:
        lines.append(str(warning))
    return lines


def summary(decision: dict) -> dict:
    """简短统计（供节点 UI 汇报）：每类决策数量 + 转场合计时长。"""
    cuts = list((decision or {}).get("cuts") or [])
    total = sum(float(c.get("duration") or 0.0) for c in cuts)
    counts: dict[str, int] = {}
    for cut in cuts:
        label = str(cut.get("transition") or T_NONE)
        counts[label] = counts.get(label, 0) + 1
    return {"cut_count": len(cuts), "transition_counts": counts,
            "total_transition_seconds": round(total, 3),
            "auto": bool((decision or {}).get("auto"))}
