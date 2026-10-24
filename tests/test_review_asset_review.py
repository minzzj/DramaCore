"""AssetReviewer 流程测试（本任务验收核心）。

覆盖：状态机路径、CNT1 三条语义、超时暂停口径、统一后端时间戳、
仲裁三分支 + 平局、FALLBACK 兜底与 LOCK 归属、迟到结果、技术失败不消耗 CNT。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_review_fakes import (  # noqa: E402
    FakeAssetStore,
    FakeClock,
    ScriptedModel,
    make_material,
    make_reviewer,
    recording_channel,
    seed_version,
)

from bettercomfyui.models.entities import ReviewAttempt  # noqa: E402
from bettercomfyui.models.enums import CounterKind, ErrorClass, ReviewMode, ReviewState  # noqa: E402
from bettercomfyui.review.errors import (  # noqa: E402
    CandidateLimitReached,
    IllegalTransition,
    ModelRoundAlreadyOpen,
    ReviewConfigError,
    ReviewError,
    ReviewTechnicalError,
)
from bettercomfyui.review.notify import NotificationService  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_memory_ledger():
    from bettercomfyui.review.commit import reset_memory_ledger

    reset_memory_ledger()
    yield
    reset_memory_ledger()


def material_for(version: dict, **extra) -> dict:
    data = {"file_path": version.get("file_path", "x/file.bin"),
            "content_hash": version["content_hash"]}
    data.update(extra)
    return data


def model_cfg(**overrides) -> dict:
    cfg = {"model_candidate_limit": 3, "score_threshold": 70.0}
    cfg.update(overrides)
    return cfg


def add_version(store: FakeAssetStore, asset_id: str, tag: str) -> dict:
    return store.add_version(asset_id, payload=f"{asset_id}-{tag}".encode())


# ---------------------------------------------------------------------------
# 人工模式
# ---------------------------------------------------------------------------


def test_human_cycle_starts_at_awaiting_review_and_passes():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)

    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    assert opened["state"] is ReviewState.AWAITING_REVIEW
    assert opened["lock_source"] is None
    assert opened["cnt1"] == 0
    cycle_id = opened["review_cycle_id"]
    assert cycle_id == f"ASSET:{asset_id}:{version['version']}"

    done = reviewer.submit_human_result(cycle_id, passed=True, operator="alice")
    assert done["state"] is ReviewState.ALLOWED
    assert done["lock_source"] == "HUMAN_PASS"
    assert done["lock_id"] == f"LOCK_{asset_id}_{version['version']}"
    assert done["committed"] is True
    assert done["cnt1"] == 0, "人工审核次数绝不进 CNT1"
    meta = store.read_meta(asset_id, version["version"])
    assert meta["lock_status"] == "LOCKED"
    assert meta["lock_source"] == "HUMAN_PASS"
    assert meta["status"] == "ALLOWED"
    assert store.call_count("set_lock") == 1
    events = reviewer.events.events()
    assert "review_state_changed" in events
    assert "human_review_result" in events


def test_human_reject_blocks_without_lock():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                    material=material_for(version))
    done = reviewer.submit_human_result(opened["review_cycle_id"], passed=False, operator="bob")
    assert done["state"] is ReviewState.BLOCKED
    assert done["lock_source"] is None
    assert done["lock_id"] is None
    assert done["committed"] is True
    assert store.call_count("set_lock") == 0
    assert store.read_meta(asset_id, version["version"])["status"] == "BLOCKED"


def test_five_human_reviews_never_enter_cnt1():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    current = version
    for index in range(5):
        if index:
            current = add_version(store, asset_id, f"human-{index}")
        opened = reviewer.review_version(asset_id, current["version"], mode="HUMAN",
                                         material=material_for(current))
        done = reviewer.submit_human_result(opened["review_cycle_id"], passed=False, operator="bob")
        assert done["state"] is ReviewState.BLOCKED
        assert done["cnt1"] == 0, f"第 {index + 1} 次人工审核后 CNT1 仍必须为 0"
    assert reviewer.counters.get(asset_id, CounterKind.CNT1) == 0
    assert reviewer.counters.get_tech_retry(asset_id) == 0


def test_same_version_review_is_idempotent_and_single_cycle():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    first = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                    material=material_for(version))
    second = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    assert second["review_cycle_id"] == first["review_cycle_id"]
    assert second["reused"] is True
    assert second["state"] is ReviewState.AWAITING_REVIEW
    assert len(reviewer.records()) == 1, "同一版本只允许一个正式审核周期"

    removed = reviewer.submit_human_result(first["review_cycle_id"], passed=True, operator="a")
    assert removed["committed"] is True
    third = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                    material=material_for(version))
    assert third["already_finalized"] is True
    assert store.call_count("set_lock") == 1


def test_human_validation_failure_goes_failed_without_consuming_cnt():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    result = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material={"file_path": "x", "content_hash": "not-a-hash"})
    assert result["state"] is ReviewState.FAILED
    assert result["cnt1"] == 0
    assert reviewer.counters.get_tech_retry(asset_id) == 1
    assert store.call_count("set_lock") == 0


def test_model_mode_never_enters_awaiting_review():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, model_review=ScriptedModel({"score": 90}))
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert result["state"] is ReviewState.ALLOWED
    states = [entry["to"] for entry in reviewer.get_cycle(result["review_cycle_id"]).history]
    assert states == ["VALIDATING", "SCANNING", "CLASSIFYING", "ALLOWED"]
    assert "AWAITING_REVIEW" not in states


# ---------------------------------------------------------------------------
# 模型模式与 CNT1
# ---------------------------------------------------------------------------


def test_model_pass_locks_with_model_pass_source_and_counts_one_round():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, model_review=ScriptedModel({"score": 88.5, "passed": True}))
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert result["state"] is ReviewState.ALLOWED
    assert result["lock_source"] == "MODEL_PASS"
    assert result["cnt1"] == 1
    assert result["score"] == 88.5
    meta = store.read_meta(asset_id, version["version"])
    assert meta["lock_source"] == "MODEL_PASS"


def test_model_retry_then_fallback_picks_highest_candidate_version():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    versions = [version, add_version(store, asset_id, "v2"), add_version(store, asset_id, "v3")]
    scores = [40.0, 65.0, 60.0]
    model = ScriptedModel(*[{"score": s} for s in scores])
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(), model_review=model)

    results = [
        reviewer.review_version(asset_id, item["version"], mode="MODEL", material=material_for(item))
        for item in versions
    ]
    assert [r["state"] for r in results[:2]] == [ReviewState.BLOCKED, ReviewState.BLOCKED]
    assert [r["cnt1"] for r in results] == [1, 2, 3], "每次实际执行模型审核前计数一次"
    fallback = results[2]
    assert fallback["state"] is ReviewState.FALLBACK
    assert fallback["lock_source"] == "FALLBACK_PASS"
    assert fallback["fallback_version"] == versions[1]["version"]
    assert fallback["lock_id"] == f"LOCK_{asset_id}_{versions[1]['version']}"
    assert fallback["score"] == 65.0

    # LOCK 只写给被选中的候选版本，且 content_hash 必须是该版本的真实指纹
    assert store.call_count("set_lock") == 1
    locked = store.set_lock_calls[-1]
    assert locked["version"] == versions[1]["version"]
    assert locked["content_hash"] == versions[1]["content_hash"]
    assert locked["lock_source"] == "FALLBACK_PASS"
    assert store.resolve_lock(asset_id, versions[1]["version"]) is not None
    for loser in (versions[0], versions[2]):
        assert store.read_meta(asset_id, loser["version"])["lock_status"] == "UNLOCKED"

    # 被选中版本的周期记录承载兜底结论（Lead 裁决：结论提交到被选中版本的 cycle）
    accepted_cycle = f"ASSET:{asset_id}:{versions[1]['version']}"
    record = reviewer.get_record(accepted_cycle)
    assert record.review_state is ReviewState.FALLBACK
    assert record.lock_source is not None and record.lock_source.value == "FALLBACK_PASS"
    assert record.content_hash is None  # 由 commit_final 从 store 读取真实指纹后加锁
    assert record.finalization_token
    # 决策周期（当前落选版本）记录兜底决策，但不写 LOCK
    decision = reviewer.get_record(results[2]["review_cycle_id"])
    assert decision.review_state is ReviewState.FALLBACK
    assert decision.lock_source is None
    assert "65.0" in decision.reason
    assert any(e["event"] == "fallback_selected" for e in reviewer.events.entries())
    assert any(e["event"] == "fallback_acceptance_recorded" for e in reviewer.events.entries())


def test_fallback_same_version_locks_that_version():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    versions = [version, add_version(store, asset_id, "v2"), add_version(store, asset_id, "v3")]
    scores = [30.0, 50.0, 69.0]  # 全部低于阈值 70，且第 3 轮最高 -> 同版本兜底
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(),
                             model_review=ScriptedModel(*[{"score": s} for s in scores]))
    results = [reviewer.review_version(asset_id, i["version"], mode="MODEL", material=material_for(i))
               for i in versions]
    fallback = results[2]
    assert fallback["fallback_version"] == versions[2]["version"]
    accepted = reviewer.get_record(fallback["review_cycle_id"])
    assert accepted.version == versions[2]["version"]
    assert accepted.lock_source.value == "FALLBACK_PASS"
    assert store.set_lock_calls[-1]["version"] == versions[2]["version"]


def test_cnt1_never_reset_by_version_bump():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(model_candidate_limit=10),
                             model_review=ScriptedModel({"score": 10}))
    current = version
    for index in range(5):
        if index:
            current = add_version(store, asset_id, f"v{index + 1}")
        result = reviewer.review_version(asset_id, current["version"], mode="MODEL",
                                         material=material_for(current))
        assert result["state"] is ReviewState.BLOCKED
        assert result["cnt1"] == index + 1, "版本号递增绝不清零 CNT1"
    assert reviewer.counters.get(asset_id, CounterKind.CNT1) == 5


def test_candidate_limit_reached_refuses_new_candidate():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    versions = [version, add_version(store, asset_id, "v2"), add_version(store, asset_id, "v3")]
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(),
                             model_review=ScriptedModel({"score": 20}))
    for item in versions:
        reviewer.review_version(asset_id, item["version"], mode="MODEL", material=material_for(item))
    fourth = add_version(store, asset_id, "v4")
    with pytest.raises(CandidateLimitReached) as excinfo:
        reviewer.review_version(asset_id, fourth["version"], mode="MODEL",
                                material=material_for(fourth))
    assert excinfo.value.best_candidate is not None


def test_model_round_cannot_be_opened_twice():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    opened = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    # 没有 model_review -> 技术失败 -> FAILED；恢复后进入 SCANNING 才可以开新轮次
    assert opened["state"] is ReviewState.FAILED


def test_begin_model_round_requires_scanning_state():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(),
                             model_review=ScriptedModel({"score": 90}))
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    with pytest.raises(IllegalTransition):
        reviewer.begin_model_round(opened["review_cycle_id"])


def test_unknown_cycle_id_is_rejected():
    reviewer = make_reviewer()
    with pytest.raises(ReviewError):
        reviewer.submit_human_result("ASSET:nope:v1", passed=True, operator="a")


# ---------------------------------------------------------------------------
# 技术失败不消耗 CNT
# ---------------------------------------------------------------------------


def test_technical_failure_consumes_no_cnt_and_recovers():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    model = ScriptedModel(ReviewTechnicalError("网关超时", error_class=ErrorClass.MODEL_API),
                          {"score": 91, "passed": True})
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(), model_review=model)

    failed = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert failed["state"] is ReviewState.FAILED
    assert failed["cnt1"] == 0, "技术失败绝不消耗 CNT1"
    assert reviewer.counters.get_tech_retry(asset_id) == 1
    assert store.call_count("set_lock") == 0

    recovered = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                        material=material_for(version))
    assert recovered["state"] is ReviewState.ALLOWED
    assert recovered["cnt1"] == 1
    assert recovered["lock_id"] == f"LOCK_{asset_id}_{version['version']}"


def test_error_class_in_payload_is_technical_failure():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(
        store, clock=clock, config=model_cfg(),
        model_review=ScriptedModel({"error_class": "COMFY_WORKFLOW", "error": "节点异常"}),
    )
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert result["state"] is ReviewState.FAILED
    assert result["cnt1"] == 0
    assert "COMFY_WORKFLOW" in result["reason"]


def test_quality_review_error_class_is_not_technical_failure():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(
        store, clock=clock, config=model_cfg(),
        model_review=ScriptedModel({"error_class": "QUALITY_REVIEW", "score": 20, "passed": False}),
    )
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert result["state"] is ReviewState.BLOCKED
    assert result["cnt1"] == 1, "质量不合格是正常的候选轮次，必须计数"


def test_missing_model_result_is_technical_failure_not_pass():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert result["state"] is ReviewState.FAILED
    assert result["cnt1"] == 0
    assert "拒绝把「没有结果」当成审核通过" in result["reason"]


def test_submit_model_failure_rolls_back_open_round():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    # 手工把周期推进到 SCANNING：用 AUTO_SWITCHED 路径（人工超时切换）
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    reviewer.on_human_timeout(cycle_id)
    assert reviewer.begin_model_round(cycle_id) == 1
    result = reviewer.submit_model_failure(cycle_id, error_class=ErrorClass.COMFY_WORKFLOW,
                                           reason="执行器崩溃")
    assert result["state"] is ReviewState.FAILED
    assert result["cnt1"] == 0
    assert result["tech_retry"] == 1


# ---------------------------------------------------------------------------
# 超时口径
# ---------------------------------------------------------------------------


def test_ui_open_does_not_pause_timeout_but_valid_op_does():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    assert opened["record"]["review_state"] == "AWAITING_REVIEW"

    idle = reviewer.record_human_activity(cycle_id, kind="page_open")
    assert idle["paused"] is False
    heartbeat = reviewer.record_human_activity(cycle_id, kind="ui_heartbeat")
    assert heartbeat["paused"] is False

    clock.advance(600)  # 界面一直开着但持续闲置
    assert reviewer.is_timed_out(cycle_id) is True, "界面开着但闲置必须仍能超时"
    result = reviewer.on_human_timeout(cycle_id)
    assert result["timeout"] is True


def test_valid_activity_extends_deadline():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(500)
    activity = reviewer.record_human_activity(cycle_id, kind="score_input")
    assert activity["paused"] is True
    clock.advance(200)
    assert reviewer.is_timed_out(cycle_id) is False, "持续有效操作期间暂停超时"
    clock.advance(401)
    assert reviewer.is_timed_out(cycle_id) is True


def test_timeout_pause_can_be_disabled_by_config():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock,
                             config=model_cfg(timeout_pause_on_active_op=False))
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(500)
    assert reviewer.record_human_activity(cycle_id, kind="score_input")["paused"] is False
    clock.advance(101)
    assert reviewer.is_timed_out(cycle_id) is True


def test_unknown_activity_kind_never_pauses_timeout():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    result = reviewer.record_human_activity(cycle_id, kind="something_unknown")
    assert result["paused"] is False
    warn = [e for e in reviewer.events.entries(event="human_review_activity")
            if e["severity"] == "WARN"]
    assert warn, "未知操作类型必须留下 WARN（不能静默当成有效操作）"


def test_timeout_not_reached_returns_reason_without_state_change():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    result = reviewer.on_human_timeout(cycle_id)
    assert result["timeout"] is False
    assert result["reason"] == "timeout_not_reached"
    assert result["remaining_s"] == pytest.approx(600.0, abs=1)
    assert reviewer.get_cycle(cycle_id).state is ReviewState.AWAITING_REVIEW


def test_timeout_switch_writes_mode_cnt1_and_events():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    result = reviewer.on_human_timeout(cycle_id)
    assert result["timeout"] is True
    assert result["state"] is ReviewState.SCANNING
    assert result["mode"] == ReviewMode.AUTO_SWITCHED
    assert result["cnt1"] == 0
    record = reviewer.get_record(cycle_id)
    assert record.mode is ReviewMode.AUTO_SWITCHED
    events = reviewer.events.events()
    assert "human_review_timeout" in events
    assert "review_mode_switched" in events
    switched = reviewer.events.entries(event="review_mode_switched")[-1]
    assert switched["detail"]["cnt1_after"] == 0
    assert switched["detail"]["human_attempts_not_counted_into_cnt1"] is True
    # 切换后事件日志记录超时+切换两条，且 timeout_reached/auto_switch 都在状态机历史里
    history = [h["event"] for h in reviewer.get_cycle(cycle_id).history]
    assert history[-2:] == ["timeout_reached", "auto_switch_model_started"]


def test_timeout_switch_resets_cnt1_that_was_nonzero():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(model_candidate_limit=10),
                             model_review=ScriptedModel({"score": 10}))
    current = version
    for index in range(2):
        if index:
            current = add_version(store, asset_id, f"model-{index}")
        reviewer.review_version(asset_id, current["version"], mode="MODEL",
                                material=material_for(current))
    assert reviewer.counters.get(asset_id, CounterKind.CNT1) == 2

    human_version = add_version(store, asset_id, "human")
    opened = reviewer.review_version(asset_id, human_version["version"], mode="HUMAN",
                                     material=material_for(human_version))
    clock.advance(601)
    result = reviewer.on_human_timeout(opened["review_cycle_id"])
    assert result["cnt1_before"] == 2
    assert result["cnt1"] == 0, "人工超时切换到模型模式时 CNT1 从 0 开始"

    # 切换后模型轮次从 1 重新累计
    reviewer.submit_model_result(opened["review_cycle_id"], score=10, passed=False)
    assert reviewer.counters.get(asset_id, CounterKind.CNT1) == 1


def test_poll_timeouts_only_switches_expired_cycles():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    first = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                    material=material_for(version))
    second_version = add_version(store, asset_id, "later")
    second = reviewer.review_version(asset_id, second_version["version"], mode="HUMAN",
                                     material=material_for(second_version))
    clock.advance(601)
    third_version = add_version(store, asset_id, "fresh")
    third = reviewer.review_version(asset_id, third_version["version"], mode="HUMAN",
                                    material=material_for(third_version))
    switched = reviewer.poll_timeouts()
    assert {r["review_cycle_id"] for r in switched} == {first["review_cycle_id"],
                                                        second["review_cycle_id"]}
    assert reviewer.get_cycle(third["review_cycle_id"]).state is ReviewState.AWAITING_REVIEW


def test_timeout_notification_failure_does_not_block():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    notifier = NotificationService([recording_channel("BAD", fail=True)], max_retries=1)
    reviewer = make_reviewer(store, clock=clock, notifier=notifier)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    clock.advance(601)
    result = reviewer.on_human_timeout(opened["review_cycle_id"])
    assert result["timeout"] is True
    assert result["notification"][0]["ok"] is False
    assert result["notification"][0]["attempts"] == 2
    assert reviewer.get_cycle(opened["review_cycle_id"]).state is ReviewState.SCANNING


# ---------------------------------------------------------------------------
# 统一后端时间戳
# ---------------------------------------------------------------------------


def test_client_ts_is_ignored_everywhere():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    result = reviewer.submit_human_result(cycle_id, passed=True, operator="alice",
                                          client_ts=0.0)
    record = reviewer.get_record(cycle_id)
    assert record.human_attempt.backend_ts == clock()
    assert record.human_attempt.backend_ts != "1970-01-01T08:00:00+08:00"
    assert result["client_ts_ignored"] is True
    event = reviewer.events.entries(event="human_review_result")[-1]
    assert event["detail"]["client_ts_used"] is False
    assert event["detail"]["client_ts"] == 0.0


def test_model_attempt_backend_ts_uses_server_clock():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, model_review=ScriptedModel({"score": 80}))
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    record = reviewer.get_record(result["review_cycle_id"])
    assert record.model_attempt.backend_ts == clock()


# ---------------------------------------------------------------------------
# 仲裁（含平局）
# ---------------------------------------------------------------------------


def _arbitration_setup(*, human_passed=True, model_ts=None):
    """把周期推进到 ARBITRATING（真实边），并注入指定时间的模型结果。"""
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    reviewer.on_human_timeout(cycle_id)          # TIMEOUT -> SCANNING
    clock.advance(20)
    late = reviewer.submit_human_result(cycle_id, passed=human_passed, operator="carol")
    assert late["state"] is ReviewState.ARBITRATING, "SCANNING + 迟到人工结果必须进入仲裁"
    assert late["committed"] is None, "仲裁未完成前不得提交最终结论"
    record = reviewer.get_record(cycle_id)
    record.model_attempt = ReviewAttempt(
        attempt_id="ATTEMPT_MODEL_1",
        review_cycle_id=cycle_id,
        mode=ReviewMode.MODEL,
        score=55.0,
        passed=False,
        backend_ts=model_ts or clock(),
    )
    return reviewer, store, cycle_id, record


def test_arbitration_human_earlier_wins_with_abort_signal():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    reviewer.on_human_timeout(cycle_id)
    clock.advance(10)
    reviewer.submit_human_result(cycle_id, passed=True, operator="carol")   # 人工 ts 较早
    clock.advance(10)
    result = reviewer.submit_model_result(cycle_id, score=55, passed=False)  # 模型更晚
    assert result["state"] is ReviewState.ALLOWED
    assert result["lock_source"] == "HUMAN_PASS"
    assert result["arb_winner"] == "HUMAN"
    assert result["abort_id"] == f"ABORT_{reviewer.get_record(cycle_id).mod_id}"
    assert result["committed"] is True
    events = reviewer.events.events()
    assert "abort_signal_issued" in events
    assert "arbitration_resolved" in events
    assert "human_result_logged_only" not in events


def test_arbitration_model_earlier_wins_and_human_only_logged():
    reviewer, store, cycle_id, record = _arbitration_setup(
        human_passed=True, model_ts="2026-10-16T10:00:05+08:00"
    )
    out = reviewer.arbitrate_cycle(cycle_id)
    assert out["arbitration"]["winner"] == "MODEL"
    assert out["arbitration"]["abort_signal"] is False
    assert out["state"] is ReviewState.BLOCKED, "模型胜出且未通过 -> 进入优化/新候选"
    assert record.arb_winner == "MODEL"
    assert any(e["event"] == "human_result_logged_only" for e in reviewer.events.entries())
    assert "abort_signal_issued" not in reviewer.events.events()
    assert store.call_count("set_lock") == 0


def test_arbitration_tie_goes_to_model():
    reviewer, store, cycle_id, record = _arbitration_setup(human_passed=True)
    record.model_attempt.backend_ts = reviewer.get_record(cycle_id).human_attempt.backend_ts
    out = reviewer.arbitrate_cycle(cycle_id)
    assert out["arbitration"]["tie"] is True
    assert out["arbitration"]["winner"] == "MODEL", "平局必须模型胜出"


def test_arbitration_model_earlier_passed_locks_model_pass():
    reviewer, store, cycle_id, record = _arbitration_setup(
        human_passed=False, model_ts="2026-10-16T10:00:05+08:00"
    )
    record.model_attempt.passed = True
    out = reviewer.arbitrate_cycle(cycle_id)
    assert out["arbitration"]["winner"] == "MODEL"
    assert out["state"] is ReviewState.ALLOWED
    assert out["lock_source"] == "MODEL_PASS"
    assert store.call_count("set_lock") == 1


def test_arbitration_human_earlier_rejected_blocks_with_abort():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg())
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    reviewer.on_human_timeout(cycle_id)
    clock.advance(5)
    reviewer.submit_human_result(cycle_id, passed=False, operator="carol")
    clock.advance(5)
    result = reviewer.submit_model_result(cycle_id, score=99, passed=True)
    assert result["state"] is ReviewState.BLOCKED
    assert result["arb_winner"] == "HUMAN"
    assert result["abort_id"]
    assert store.call_count("set_lock") == 0


def test_arbitrate_cycle_requires_arbitrating_state():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    with pytest.raises(ReviewError):
        reviewer.arbitrate_cycle(opened["review_cycle_id"])


# ---------------------------------------------------------------------------
# 迟到结果
# ---------------------------------------------------------------------------


def test_late_human_result_after_model_commit_changes_nothing():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(),
                             model_review=ScriptedModel({"score": 95, "passed": True}))
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    reviewer.on_human_timeout(cycle_id)
    clock.advance(5)
    committed = reviewer.submit_model_result(cycle_id, score=95, passed=True)
    assert committed["state"] is ReviewState.ALLOWED
    assert committed["committed"] is True
    before = store.read_meta_bytes(asset_id, version["version"])

    late = reviewer.submit_human_result(cycle_id, passed=False, operator="late-bob")
    assert late["accepted"] is False
    assert late["archived"] is True
    assert late["archive_state"] == "ABORTED"
    assert late["wrote_version_state"] is False
    assert late["state"] is ReviewState.ALLOWED, "迟到结果绝不改变当前版本状态"
    assert store.read_meta_bytes(asset_id, version["version"]) == before
    assert store.call_count("set_lock") == 1
    assert reviewer.late_results(cycle_id)[0]["archive"]["archive_state"] == "ABORTED"
    record = reviewer.get_record(cycle_id)
    assert record.review_state is ReviewState.ALLOWED
    assert record.human_attempt is None
    assert record.attempts[-1].superseded is True


def test_late_human_result_on_blocked_cycle_is_archived():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    reviewer.submit_human_result(cycle_id, passed=False, operator="bob")
    before = store.read_meta_bytes(asset_id, version["version"])
    late = reviewer.submit_human_result(cycle_id, passed=True, operator="late-bob")
    assert late["state"] is ReviewState.BLOCKED
    assert late["archive_state"] == "ABORTED"
    assert store.read_meta_bytes(asset_id, version["version"]) == before


def test_late_model_result_after_human_reject_is_archived():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock)
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN",
                                     material=material_for(version))
    cycle_id = opened["review_cycle_id"]
    reviewer.submit_human_result(cycle_id, passed=False, operator="bob")
    before = store.read_meta_bytes(asset_id, version["version"])
    late = reviewer.submit_model_result(cycle_id, score=99, passed=True)
    assert late["accepted"] is False
    assert late["archive_state"] == "ABORTED"
    assert store.read_meta_bytes(asset_id, version["version"]) == before
    assert store.call_count("set_lock") == 0


def test_human_result_in_wrong_state_raises_not_silently_accepted():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, model_review=ScriptedModel({"score": 95}))
    opened = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    assert opened["state"] is ReviewState.ALLOWED
    # 已终态：进入迟到归档（不改变版本状态）
    result = reviewer.submit_human_result(opened["review_cycle_id"], passed=True, operator="x")
    assert result["accepted"] is False
    assert result["state"] is ReviewState.ALLOWED


# ---------------------------------------------------------------------------
# 配置与快照
# ---------------------------------------------------------------------------


def test_unsupported_timeout_policy_is_refused():
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(on_timeout="suspend"))
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(fallback="whatever"))
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(new_version_requires_review=False))
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(human_timeout_minutes=0))
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(model_candidate_limit=99))
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(default_mode="AUTO_SWITCHED"))
    with pytest.raises(ReviewConfigError):
        make_reviewer(config=model_cfg(unknown_key=1))


def test_snapshot_restore_keeps_state_and_cnt1():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, config=model_cfg(model_candidate_limit=10),
                             model_review=ScriptedModel({"score": 10}))
    result = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                     material=material_for(version))
    snapshot = reviewer.snapshot()

    fresh = make_reviewer(store, clock=FakeClock())
    fresh.restore(snapshot)
    assert fresh.counters.get(asset_id, CounterKind.CNT1) == 1
    cycle = fresh.get_cycle(result["review_cycle_id"])
    assert cycle.state is ReviewState.BLOCKED
    assert fresh.get_record(result["review_cycle_id"]).reason == result["reason"]
