"""提交模式测试：`auto_commit=True`（默认，审核器内部提交）与 `False`（调用方提交）。

背景（验收 9 的两条路径都必须在）：
- Lead 的 `demo.py` 依赖 `review_version(mode=MODEL)` 通过即写 LOCK → 默认 auto_commit=True；
- 独立验证员的验收 9 用例假设「review_version 只到达结论态，由调用方 commit_final 提交」
  → `auto_commit=False` 时必须成立，且 `review_version` 的返回值本身要能直接喂给 commit_final
  （契约缺口 G-4：cycle dict 字段未定义）。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_review_fakes import (  # noqa: E402
    FakeAssetStore,
    FakeClock,
    ScriptedModel,
    make_material,
    make_reviewer,
    seed_version,
)

from bettercomfyui.models.enums import ReviewState  # noqa: E402
from bettercomfyui.review.commit import commit_final, reset_memory_ledger  # noqa: E402


def mat(version: dict) -> dict:
    """材料的 content_hash 必须是该版本真实指纹（否则 store 会拒绝加锁，R3）。"""
    return {"file_path": version.get("file_path", "x/file.bin"),
            "content_hash": version["content_hash"]}


@pytest.fixture(autouse=True)
def _isolate_memory_ledger():
    reset_memory_ledger()
    yield
    reset_memory_ledger()


def test_auto_commit_false_returns_commit_ready_cycle():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, auto_commit=False,
                             model_review=ScriptedModel({"score": 91.0, "passed": True}))

    res = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                  material=mat(version))
    assert res["state"] is ReviewState.ALLOWED
    assert res["committed"] is False
    assert res["finalization"]["reason"] == "auto_commit_disabled"
    assert res["lock_id"] is None
    assert store.call_count("set_lock") == 0, "auto_commit=False 时审核器不得自己写 LOCK"

    # G-4：返回值本身必须是合法的 commit_final 输入
    for key in ("review_state", "target_kind", "target_id", "content_hash",
                "finalization_token", "version", "lock_source", "review_cycle_id"):
        assert key in res, f"review_version 返回值缺少 commit_final 需要的字段 {key}"
    outcome = commit_final(dict(res), store=store)
    assert outcome["committed"] is True, outcome
    assert outcome["lock_id"] == f"LOCK_{asset_id}_{version['version']}"
    assert store.call_count("set_lock") == 1
    assert store.resolve_lock(asset_id, version["version"])["lock_source"] == "MODEL_PASS"


def test_auto_commit_false_concurrent_commit_exactly_one_and_one_lock():
    """验收 9 的 P1/P2 形状：结论已到达但**尚未提交**的周期，32 线程并发提交。"""
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, auto_commit=False,
                             model_review=ScriptedModel({"score": 95.0, "passed": True}))
    res = reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                  material=mat(version))
    assert res["state"] is ReviewState.ALLOWED
    assert store.call_count("set_lock") == 0
    cycle = dict(res)

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

    winners = [r for r in results if r.get("committed") is True]
    losers = [r for r in results if r.get("committed") is not True]
    assert len(winners) == 1, f"必须恰好 1 个 committed=True，实际 {len(winners)}"
    assert {r["reason"] for r in losers} == {"already_finalized"}
    assert all(r["side_effects"] == [] for r in losers)
    assert store.call_count("set_lock") == 1, "LOCK 必须恰好写入 1 次"


def test_auto_commit_false_sequential_second_commit_rejected():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, auto_commit=False,
                             model_review=ScriptedModel({"score": 95.0, "passed": True}))
    cycle = dict(reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                        material=mat(version)))
    first = commit_final(dict(cycle), store=store)
    second = commit_final(dict(cycle), store=store)
    assert first["committed"] is True
    assert second["committed"] is False
    assert second["reason"] == "already_finalized"
    assert store.call_count("set_lock") == 1


def test_auto_commit_false_late_result_still_archived():
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    reviewer = make_reviewer(store, clock=clock, auto_commit=False,
                             model_review=ScriptedModel({"score": 95.0, "passed": True}))
    cycle = dict(reviewer.review_version(asset_id, version["version"], mode="MODEL",
                                        material=mat(version)))
    assert commit_final(dict(cycle), store=store)["committed"] is True
    before = store.read_meta_bytes(asset_id, version["version"])

    late = reviewer.submit_human_result(cycle["review_cycle_id"], passed=False, operator="late")
    assert late["accepted"] is False
    assert late["archive_state"] == "ABORTED"
    assert store.read_meta_bytes(asset_id, version["version"]) == before
    assert store.call_count("set_lock") == 1
