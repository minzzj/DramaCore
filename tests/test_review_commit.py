"""唯一提交点测试：条件更新、32 线程并发、迟到结果零副作用、失败语义。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_review_fakes import (  # noqa: E402
    FailingSetLockStore,
    FailingStatusAfterLockStore,
    FakeAssetStore,
    seed_version,
)

from bettercomfyui.review.commit import (  # noqa: E402
    FinalizationLedger,
    archive_late_result,
    commit_final,
    reset_memory_ledger,
)


@pytest.fixture(autouse=True)
def _isolate_memory_ledger():
    """进程内 ledger 是进程级共享的（这正是同一进程内条件更新的语义），测试间必须重置。"""
    reset_memory_ledger()
    yield
    reset_memory_ledger()


def make_cycle(store, asset_id: str, version: dict, *, state: str = "ALLOWED",
               lock_source: str | None = "MODEL_PASS", ledger=None, **extra) -> dict:
    data = {
        "review_cycle_id": f"ASSET:{asset_id}:{version['version']}",
        "review_state": state,
        "target_kind": "asset",
        "target_id": asset_id,
        "version": version["version"],
        "content_hash": version["content_hash"],
        "lock_source": lock_source,
        "mode": "MODEL",
        "reason": "unit-test",
    }
    if ledger is not None:
        data["ledger"] = ledger
    data.update(extra)
    return data


def test_single_commit_writes_lock_once():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)

    result = commit_final(cycle, store=store)

    assert result["committed"] is True
    assert result["reason"] is None
    assert result["claim"] == "won"
    assert result["lock_id"] == f"LOCK_{asset_id}_{version['version']}"
    assert result["finalized_at"]
    assert result["durable"] is False  # 内存 ledger 如实上报
    assert result["side_effects"] == [
        f"set_lock:{result['lock_id']}",
        "set_version_status:ALLOWED",
    ]
    assert store.call_count("set_lock") == 1
    assert store.call_count("set_version_status") == 1
    meta = store.read_meta(asset_id, version["version"])
    assert meta["lock_status"] == "LOCKED"
    assert meta["lock_source"] == "MODEL_PASS"
    assert meta["review_cycle_id"] == cycle["review_cycle_id"]
    assert meta["status"] == "ALLOWED"


def test_concurrent_32_threads_exactly_one_commit():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    downstream_calls: list[dict] = []
    cycle = make_cycle(store, asset_id, version, ledger=ledger,
                       downstream=lambda data: downstream_calls.append(data))

    results: list[dict] = []
    guard = threading.Lock()
    barrier = threading.Barrier(32)

    def worker():
        payload = dict(cycle)
        barrier.wait()
        outcome = commit_final(payload, store=store)
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    committed = [r for r in results if r["committed"]]
    losers = [r for r in results if not r["committed"]]
    assert len(results) == 32
    assert len(committed) == 1, f"committed=True 必须恰好 1 次，实际 {len(committed)}"
    assert len(losers) == 31
    assert {r["reason"] for r in losers} == {"already_finalized"}
    assert all(r["side_effects"] == [] for r in losers), "落败提交不得产生任何副作用"
    assert store.call_count("set_lock") == 1, "LOCK 必须只写入 1 次"
    assert store.call_count("set_version_status") == 1
    assert len(downstream_calls) == 1, "只有首次提交者可以触发下游"
    assert store.read_meta(asset_id, version["version"])["lock_status"] == "LOCKED"


def test_second_commit_is_already_finalized_and_bytes_unchanged():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)

    first = commit_final(dict(cycle), store=store)
    assert first["committed"] is True
    before = store.read_meta_bytes(asset_id, version["version"])

    second = commit_final(dict(cycle), store=store)
    assert second["committed"] is False
    assert second["reason"] == "already_finalized"
    assert second["claim"] == "already_locked_idempotent"
    assert second["side_effects"] == []
    assert store.read_meta_bytes(asset_id, version["version"]) == before, "迟到提交不得改变版本状态"
    assert store.call_count("set_lock") == 1


def test_failed_state_is_not_conclusive_and_does_not_consume_token():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    failed = make_cycle(store, asset_id, version, state="FAILED", lock_source=None, ledger=ledger)

    result = commit_final(failed, store=store)
    assert result["committed"] is False
    assert result["reason"].startswith("state_not_conclusive:FAILED")
    assert result["claim"] == "not_attempted"
    assert ledger.get(failed["review_cycle_id"]) is None, "技术失败不得占用唯一提交权"
    assert store.call_count("set_lock") == 0

    # 恢复后仍可正常提交（token 未被消耗）
    allowed = make_cycle(store, asset_id, version, ledger=ledger)
    assert commit_final(allowed, store=store)["committed"] is True


def test_blocked_commits_without_lock():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, state="BLOCKED", lock_source=None, ledger=ledger)

    result = commit_final(cycle, store=store)
    assert result["committed"] is True
    assert result["lock_id"] is None
    assert store.call_count("set_lock") == 0
    assert store.read_meta(asset_id, version["version"])["status"] == "BLOCKED"
    assert commit_final(dict(cycle), store=store)["reason"] == "already_finalized"


def test_aborted_commit_touches_no_version_state():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    before = store.read_meta_bytes(asset_id, version["version"])
    cycle = make_cycle(store, asset_id, version, state="ABORTED", lock_source=None, ledger=ledger)

    result = commit_final(cycle, store=store)
    assert result["committed"] is True
    assert result["side_effects"] == ["archive_only_no_version_write"]
    assert store.call_count("set_lock") == 0
    assert store.call_count("set_version_status") == 0
    assert store.read_meta_bytes(asset_id, version["version"]) == before


def test_missing_content_hash_releases_claim():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)
    cycle["content_hash"] = None
    cycle["version"] = "v-unknown"  # 让 store 也查不到指纹
    cycle["lock_version"] = "v-unknown"

    result = commit_final(cycle, store=store)
    assert result["committed"] is False
    assert result["reason"] == "missing_content_hash"
    assert result["claim"] == "released"
    assert store.call_count("set_lock") == 0


def test_missing_lock_source_is_refused():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    cycle = make_cycle(store, asset_id, version, lock_source=None, ledger=FinalizationLedger(None))
    result = commit_final(cycle, store=store)
    assert result["committed"] is False
    assert result["reason"] == "missing_lock_source"
    assert store.call_count("set_lock") == 0


def test_store_not_configured_does_not_claim():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)
    result = commit_final(cycle, store=None)
    assert result["committed"] is False
    assert result["reason"] == "store_not_configured"
    assert ledger.get(cycle["review_cycle_id"]) is None


def test_set_lock_failure_releases_claim_and_retry_succeeds():
    store = FailingSetLockStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)

    first = commit_final(dict(cycle), store=store)
    assert first["committed"] is False
    assert first["reason"] == "store_write_failed"
    assert first["claim"] == "released"
    assert ledger.get(cycle["review_cycle_id"]) is None, "LOCK 未写入时必须释放提交权"

    store.set_lock = FakeAssetStore.set_lock.__get__(store, FakeAssetStore)  # 修复后重试
    second = commit_final(dict(cycle), store=store)
    assert second["committed"] is True
    assert second["lock_id"] is not None


def test_lock_written_then_status_failure_keeps_claim_and_never_relocks():
    store = FailingStatusAfterLockStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)

    first = commit_final(dict(cycle), store=store)
    assert first["committed"] is True, "LOCK 已写入 -> 结论已成立，不能回滚"
    assert first["warnings"], "必须如实上报后置步骤失败"
    assert store.call_count("set_lock") == 1

    second = commit_final(dict(cycle), store=store)
    assert second["committed"] is False
    assert second["reason"] == "already_finalized"
    assert store.call_count("set_lock") == 1, "绝不能写第二次 LOCK"


def test_existing_different_lock_is_refused():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    first = commit_final(make_cycle(store, asset_id, version, ledger=ledger), store=store)
    assert first["committed"] is True

    other = make_cycle(store, asset_id, version, lock_source="FALLBACK_PASS", ledger=ledger)
    other["review_cycle_id"] = f"ASSET:{asset_id}:v9"
    result = commit_final(other, store=store)
    assert result["committed"] is False
    assert result["reason"] == "already_locked_in_store"
    assert store.call_count("set_lock") == 1


def test_file_ledger_is_durable_and_concurrency_safe(tmp_path):
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    path = tmp_path / "review" / "finalizations.json"
    ledger = FinalizationLedger(path)
    assert ledger.durable is True
    cycle = make_cycle(store, asset_id, version, ledger=ledger)

    results: list[dict] = []
    guard = threading.Lock()
    barrier = threading.Barrier(32)

    def worker():
        payload = dict(cycle)
        barrier.wait()
        outcome = commit_final(payload, store=store)
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([r for r in results if r["committed"]]) == 1
    assert store.call_count("set_lock") == 1
    assert path.exists(), "落盘 ledger 必须真的写文件"
    ledger_data = path.read_text(encoding="utf-8")
    assert cycle["review_cycle_id"] in ledger_data


def test_ledger_claim_release_and_token_check():
    ledger = FinalizationLedger(None)
    won, entry = ledger.claim("CYCLE:1", "token-a", detail={"state": "ALLOWED"})
    assert won is True and entry["token"] == "token-a"
    won_again, existing = ledger.claim("CYCLE:1", "token-b")
    assert won_again is False and existing["token"] == "token-a"
    assert ledger.release("CYCLE:1", "token-wrong") is False
    assert ledger.release("CYCLE:1", "token-a", reason="unit") is True
    assert ledger.get("CYCLE:1") is None
    won_third, _ = ledger.claim("CYCLE:1", "token-c")
    assert won_third is True


def test_archive_late_result_never_touches_store():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)
    commit_final(dict(cycle), store=store)
    before = store.read_meta_bytes(asset_id, version["version"])
    calls_before = store.snapshot_calls()

    outcome = archive_late_result(
        cycle, store=store, ledger=ledger, reason="迟到人工结果",
        attempt={"attempt_id": "ATTEMPT_X", "superseded": True},
    )
    assert outcome["archived"] is True
    assert outcome["archive_state"] == "ABORTED"
    assert outcome["wrote_version_state"] is False
    assert outcome["store_calls"] == []
    assert store.snapshot_calls() == calls_before
    assert store.read_meta_bytes(asset_id, version["version"]) == before
    late = ledger.get(cycle["review_cycle_id"])["late_results"]
    assert late[0]["archive_state"] == "ABORTED"


def test_archive_for_unclaimed_cycle_does_not_block_future_commit():
    store = FakeAssetStore()
    asset_id, version = seed_version(store)
    ledger = FinalizationLedger(None)
    cycle = make_cycle(store, asset_id, version, ledger=ledger)

    archive_late_result(cycle, ledger=ledger, reason="早到的迟到结果", attempt=None)
    assert ledger.get(cycle["review_cycle_id"]) is None, "归档迟到结果不得创建提交权"

    assert commit_final(dict(cycle), store=store)["committed"] is True


def test_non_asset_target_only_claims(tmp_path):
    ledger = FinalizationLedger(None)
    cycle = {
        "review_cycle_id": "SEGMENT:SEG1:v1",
        "review_state": "ALLOWED",
        "target_kind": "segment",
        "target_id": "SEG1",
        "version": "v1",
        "lock_source": "MODEL_PASS",
        "ledger": ledger,
    }
    result = commit_final(cycle, store=None)
    assert result["committed"] is True
    assert result["lock_id"] is None
    assert result["side_effects"] == ["claim_only_non_asset_target"]


def test_commit_requires_valid_input():
    store = FakeAssetStore()
    from bettercomfyui.review.errors import FinalizationError

    with pytest.raises(FinalizationError):
        commit_final({"review_state": "ALLOWED"}, store=store)
    with pytest.raises(FinalizationError):
        commit_final({"review_cycle_id": "X", "review_state": "NOPE"}, store=store)
    with pytest.raises(FinalizationError):
        commit_final("not-a-dict", store=store)  # type: ignore[arg-type]
