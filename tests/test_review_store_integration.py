"""真实 store 集成测试（task-2 已就绪 → 切换到真实实现重跑）。

不再是替身：真实 `AssetStore` + 真实 `ProjectPaths` + 真实 `store.events` 落盘。
若 store 尚未就绪，本文件整体 skip（**skip 不算通过**，见回报说明）。
"""

from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("bettercomfyui.store.assets")
pytest.importorskip("bettercomfyui.store.events")

from bettercomfyui.models.enums import (  # noqa: E402
    AssetSource,
    AssetStatus,
    AssetType,
    CounterKind,
    LibraryScope,
)
from bettercomfyui.review.asset_review import AssetReviewer  # noqa: E402
from bettercomfyui.review.commit import (  # noqa: E402
    FinalizationLedger,
    commit_final,
    reset_memory_ledger,
)
from bettercomfyui.review.counters import CounterStore  # noqa: E402
from bettercomfyui.review.events import ReviewEventLog, require_store_events  # noqa: E402
from bettercomfyui.store.assets import AssetStore  # noqa: E402
from bettercomfyui.store.events import read_events  # noqa: E402
from bettercomfyui.store.paths import ProjectPaths  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_fakes import FakeClock, ScriptedModel  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_memory_ledger():
    reset_memory_ledger()
    yield
    reset_memory_ledger()


@pytest.fixture()
def project(tmp_path):
    paths = ProjectPaths(tmp_path / "projects", uuid.uuid4().hex)
    paths.ensure_layout()
    return paths, AssetStore(paths)


def seed(store: AssetStore, paths: ProjectPaths, name: str = "hero", payload: bytes = b"hero-real"):
    asset = store.create_asset(asset_type=AssetType.CHARACTER, name=name,
                               source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    blob = paths.project_dir / f"{name}.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(payload)
    version = store.add_version(asset["asset_id"], source=AssetSource.AI,
                                file_path=blob, status=AssetStatus.DRAFT)
    material = {"file_path": version["file_path"], "content_hash": version["content_hash"]}
    return asset["asset_id"], version, material


def counting(store: AssetStore, method: str):
    """在不修改 store 代码的前提下统计真实方法调用次数（测试侧包装）。"""
    original = getattr(store, method)
    counter = {"n": 0}
    guard = threading.Lock()

    def wrapper(*args, **kwargs):
        with guard:
            counter["n"] += 1
        return original(*args, **kwargs)

    setattr(store, method, wrapper)
    return counter


def test_events_are_persisted_through_real_store_events(project):
    paths, store = project
    require_store_events()  # 未就绪必须抛；这里不抛 = 真的可用
    clock = FakeClock()
    log = ReviewEventLog(project=paths, clock=clock, project_id=paths.project_id)
    assert log.mode == "store"
    assert log.persistence_available is True
    log.emit("unit_event", detail={"k": "v"}, severity="WARN")
    assert log.write_failures == 0
    persisted = read_events(paths, namespace="REVIEW")
    assert persisted, "审核事件必须真的落到 logs/events.jsonl"
    assert persisted[-1]["event"] == "unit_event"
    assert persisted[-1]["severity"] == "WARN"
    assert persisted[-1]["namespace"] == "REVIEW"


def test_human_pass_on_real_store_writes_real_lock(project):
    paths, store = project
    asset_id, version, material = seed(store, paths)
    clock = FakeClock()
    reviewer = AssetReviewer(
        store, clock=clock, config={"model_candidate_limit": 3, "score_threshold": 70.0},
        event_log=ReviewEventLog(project=paths, clock=clock, project_id=paths.project_id),
    )
    locks = counting(store, "set_lock")

    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN", material=material)
    result = reviewer.submit_human_result(opened["review_cycle_id"], passed=True, operator="alice")

    assert result["state"] == "ALLOWED"
    assert locks["n"] == 1
    lock = store.resolve_lock(asset_id, version["version"])
    assert lock is not None, "真实 store 必须能解析出 LOCK"
    assert lock["lock_source"] == "HUMAN_PASS"
    assert lock["review_cycle_id"] == opened["review_cycle_id"]
    assert lock["content_hash"] == version["content_hash"]
    assert store.get_version(asset_id, version["version"])["status"] == "ALLOWED"

    names = [e["event"] for e in read_events(paths, namespace="REVIEW")]
    for expected in ("review_cycle_opened", "human_review_result", "finalization_committed"):
        assert expected in names, f"{expected} 必须落盘（Z4/Z5）"


def test_cross_version_fallback_locks_selected_version_cycle(project):
    paths, store = project
    asset_id, first, material_v1 = seed(store, paths, name="hero")
    versions = [first]
    materials = [material_v1]
    for index in range(2):
        blob = paths.project_dir / f"hero-v{index + 2}.bin"
        blob.write_bytes(f"hero-v{index + 2}".encode())
        item = store.add_version(asset_id, source=AssetSource.AI, file_path=blob,
                                 status=AssetStatus.DRAFT)
        versions.append(item)
        materials.append({"file_path": item["file_path"], "content_hash": item["content_hash"]})

    clock = FakeClock()
    reviewer = AssetReviewer(
        store, clock=clock, config={"model_candidate_limit": 3, "score_threshold": 70.0},
        model_review=ScriptedModel({"score": 30}, {"score": 60}, {"score": 40}),
        event_log=ReviewEventLog(project=paths, clock=clock, project_id=paths.project_id),
    )
    results = [reviewer.review_version(asset_id, item["version"], mode="MODEL", material=materials[i])
               for i, item in enumerate(versions)]
    fallback = results[2]
    selected = versions[1]
    assert fallback["state"] == "FALLBACK"
    assert fallback["fallback_version"] == selected["version"]
    assert fallback["lock_id"] == f"LOCK_{asset_id}_{selected['version']}"

    lock = store.resolve_lock(asset_id, selected["version"])
    assert lock is not None
    assert lock["lock_source"] == "FALLBACK_PASS"
    assert lock["content_hash"] == selected["content_hash"]
    # Lead 裁决：结论提交到「被选中版本的那个 cycle」
    assert lock["review_cycle_id"] == f"ASSET:{asset_id}:{selected['version']}"
    for loser in (versions[0], versions[2]):
        assert store.resolve_lock(asset_id, loser["version"]) is None


def test_concurrent_commit_on_real_store_writes_one_lock(project):
    paths, store = project
    asset_id, version, _ = seed(store, paths)
    derived = AssetReviewer._derive_ledger_path(store)
    assert derived is not None
    assert str(paths.project_dir) in str(derived), "ledger 必须落在项目目录内（R5 项目隔离）"

    ledger = FinalizationLedger(derived)
    locks = counting(store, "set_lock")
    cycle = {
        "review_cycle_id": f"ASSET:{asset_id}:{version['version']}",
        "review_state": "ALLOWED",
        "target_kind": "asset",
        "target_id": asset_id,
        "version": version["version"],
        "content_hash": version["content_hash"],
        "lock_source": "MODEL_PASS",
        "mode": "MODEL",
        "ledger": ledger,
    }
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
    assert locks["n"] == 1, "真实 store 上 LOCK 也只允许写入一次"
    assert {r["reason"] for r in results if not r["committed"]} == {"already_finalized"}
    assert all(r["durable"] is True for r in results)
    assert derived.exists()
    assert store.resolve_lock(asset_id, version["version"]) is not None


def test_late_human_result_keeps_real_meta_bytes(project):
    paths, store = project
    asset_id, version, material = seed(store, paths)
    clock = FakeClock()
    reviewer = AssetReviewer(
        store, clock=clock, config={"model_candidate_limit": 3, "score_threshold": 70.0},
        event_log=ReviewEventLog(project=paths, clock=clock, project_id=paths.project_id),
    )
    opened = reviewer.review_version(asset_id, version["version"], mode="HUMAN", material=material)
    cycle_id = opened["review_cycle_id"]
    clock.advance(601)
    reviewer.on_human_timeout(cycle_id)
    committed = reviewer.submit_model_result(cycle_id, score=88, passed=True)
    assert committed["state"] == "ALLOWED"

    meta_path = paths.version_meta(AssetType.CHARACTER, asset_id, version["version"])
    before = meta_path.read_bytes()
    late = reviewer.submit_human_result(cycle_id, passed=False, operator="late")
    assert late["accepted"] is False
    assert late["archive_state"] == "ABORTED"
    assert meta_path.read_bytes() == before, "迟到结果绝不改变真实 meta.json 字节"


def test_counter_scope_works_with_real_ids():
    """回归（director-exec 报出）：scope()/snapshot() 曾因 cnt_id("TECH") 抛 StoreError。"""
    counters = CounterStore()
    counters.increment("A1", CounterKind.CNT2)
    counters.increment_tech_retry("A1")
    assert counters.scope("A1") == {"CNT1": 0, "CNT2": 1, "CNT3": 0, "TECH_RETRY": 1}
    assert counters.snapshot()["tech_retry"] == {"CNT_A1_TECH": 1}
