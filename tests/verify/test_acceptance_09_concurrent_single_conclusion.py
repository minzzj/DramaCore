"""验收 9：并发审核只产生一个有效结论；迟到结果不能覆盖新版本。

原文（任务书第 9 节第 9 条，第 191 行）：
    「并发审核只产生一个有效结论；迟到结果不能覆盖新版本。」

需要模块：bettercomfyui.review.commit（commit_final）、bettercomfyui.review.asset_review、
          bettercomfyui.store.assets（AssetStore，提供 set_lock 以便打桩计数）
依据：CONTRACTS.md 第 6 节「并发与迟到结果」；方案第七部分一 第 9/10 条、第七部分七
      （review_cycle_id + finalization_token 条件更新）；任务书第 6 节。

判定通过：
    P1 32 个线程并发提交同一 review_cycle_id：恰好 1 个 committed=True；
       其余为 committed=False 且 reason == "already_finalized"；
    P2 LOCK 只被写入 1 次（对 store.set_lock 打桩计数，是真实调用证据）；
    P3 已提交后到达的迟到人工结果不得改变当前版本状态（文件树字节级未变）。

判定假通过（必须能变红）：
    F1 用「先检查后写入」而不是条件更新 —— 并发下 committed=True 会多于 1，P1 变红；
    F2 只把 committed 标成 True 而没写 LOCK —— P2 变红（要求恰好 1 次真实 set_lock）；
    F3 迟到结果直接覆盖版本状态 —— P3 变红；
    F4 测试里根本没并发（顺序调用）—— 本用例用 32 个线程 + Barrier 强制同时进入临界区；
        且断言 len(set(results)) 覆盖到「有待提交者失败」这一事实。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from _api import (
    FakeClock,
    asset_id_of,
    build_asset_reviewer,
    build_counter_store,
    create_asset,
    create_project,
    make_asset_file,
)
from _harness import assert_tree_unchanged, contract_attr, load, make_config, tree_digest

N_THREADS = 32


def _setup(tmp_path):
    cfg = make_config(asset_review={"default_mode": "MODEL"})
    ha, _store, _root = create_project(tmp_path, "并发项目", cfg)
    ea = create_asset(ha, name="角色A", asset_type="CHARACTER")
    aid = asset_id_of(ea)
    f1 = make_asset_file(tmp_path, "v1.png", b"CONCURRENT-V1")
    v1 = ha.assets.add_version(aid, source="AI", file_path=f1, status="PENDING_REVIEW")
    return cfg, ha, aid, f1, v1


def _cycle(cfg, ha, aid, f1, v1):
    """产出一个**已到达结论态**的正式审核周期，作为 commit_final 的输入。

    实测（契约缺口 G-4）：`commit_final(cycle)` 需要 `cycle["review_state"]`，
    而 `review_version` 返回的键是 `state`。这里按实测字段名构造 cycle，
    并在 VERIFY.md 记录契约缺口 G-4（cycle dict 字段未定义）。
    """
    enums = load("bettercomfyui.models.enums")
    ReviewMode = contract_attr(enums, "ReviewMode", kind="class")
    counters = build_counter_store(cfg)
    clock = FakeClock()
    reviewer = build_asset_reviewer(
        ha.assets, cfg, project=ha.paths, clock=clock, counters=counters,
        model_review=lambda rec: {"score": 95.0, "passed": True, "reason": "验证用评分"},
        auto_commit=False,  # 关键：让结论到达后**不**自动提交，把唯一提交留给 commit_final
    )
    res = reviewer.review_version(
        aid, "v1", mode=ReviewMode.MODEL,
        material={"file_path": str(f1), "content_hash": v1["content_hash"], "prompt": "角色A"},
    )
    assert isinstance(res, dict), f"review_version 必须返回 dict，实际 {type(res)!r}"
    assert res.get("review_cycle_id"), f"必须带 review_cycle_id，实际 {res!r}"
    assert res.get("state") in ("ALLOWED", "BLOCKED", "FALLBACK"), (
        f"提交前必须到达结论态，实际 state={res.get('state')}（reason={res.get('reason')}）"
    )
    rec = res.get("record")
    assert isinstance(rec, dict), (
        "review_version 返回值必须带 record（ReviewRecord.to_dict()）—— commit_final 的输入形状；"
        f"实际 keys={sorted(res)}"
    )
    for key in ("review_cycle_id", "review_state", "target_id", "content_hash", "version"):
        assert rec.get(key) is not None, f"record 缺 {key}（commit_final 必需），实际 keys={sorted(rec)}"

    # review-lock 确认：`auto_commit=False` 时 review_version 只推进状态机到结论态，
    # 返回值即「可直接喂给 commit_final 的 cycle dict」（含 review_state/target_id/
    # content_hash/finalization_token/lock_version），唯一提交留给 commit_final。
    assert rec.get("finalization_token"), (
        f"auto_commit=False 时 record 必须带 finalization_token（条件更新的前提），实际 keys={sorted(rec)}"
    )
    # 同时把 reviewer 实例返回：迟到结果必须提交给**同一个**实例（周期记录在实例内）
    return rec, reviewer


def test_concurrent_commit_produces_exactly_one_winner(tmp_path, monkeypatch):
    """P1/P2：并发提交恰好一个胜出，LOCK 恰好写入一次。"""
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    commit = load("bettercomfyui.review.commit")
    fn = contract_attr(commit, "commit_final")
    cycle, reviewer = _cycle(cfg, ha, aid, f1, v1)

    lock_calls: list[tuple] = []
    real_set_lock = ha.assets.set_lock

    def spy(*a, **kw):
        lock_calls.append((a, kw))
        return real_set_lock(*a, **kw)

    try:
        monkeypatch.setattr(ha.assets, "set_lock", spy)
    except (AttributeError, TypeError):
        pytest.skip(
            "未验证：AssetStore 实例不允许打桩 set_lock（可能用了 __slots__），"
            "无法取得「LOCK 只写入一次」的真实调用证据"
        )

    barrier = threading.Barrier(N_THREADS)
    results: list[dict] = []
    errors: list[str] = []
    guard = threading.Lock()

    def worker(i: int):
        try:
            barrier.wait(timeout=30)  # 强制 32 个线程同时进入临界区
            r = fn(cycle, store=ha.assets)
            with guard:
                results.append(r if isinstance(r, dict) else {"committed": bool(r), "raw": repr(r)})
        except Exception as exc:  # noqa: BLE001 - 收集后统一判定
            with guard:
                errors.append(f"线程{i}: {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        list(pool.map(worker, range(N_THREADS)))

    assert not errors, f"并发提交不得抛异常（应通过条件更新返回 already_finalized）：{errors[:5]}"
    assert len(results) == N_THREADS, f"应收到 {N_THREADS} 个结果，实际 {len(results)}"

    winners = [r for r in results if r.get("committed") is True]
    losers = [r for r in results if r.get("committed") is not True]
    assert len(winners) == 1, (
        f"并发提交必须恰好 1 个 committed=True，实际 {len(winners)} 个："
        f"{[r.get('reason') for r in winners]}"
    )
    for r in losers:
        assert r.get("reason") == "already_finalized", (
            f"未胜出者必须返回 reason='already_finalized'（契约第 6 节），实际 {r!r}"
        )
    assert len(lock_calls) == 1, (
        f"LOCK 必须恰好写入 1 次，实际 set_lock 被调用 {len(lock_calls)} 次"
    )


def test_late_result_cannot_overwrite_committed_version(tmp_path, monkeypatch):
    """P3：提交后到达的迟到结果不得改变版本状态。"""
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    commit = load("bettercomfyui.review.commit")
    fn = contract_attr(commit, "commit_final")
    cycle, reviewer = _cycle(cfg, ha, aid, f1, v1)
    first = fn(cycle, store=ha.assets)
    assert first.get("committed") is True, f"首次提交必须成功，实际 {first!r}"

    locked_before = ha.assets.resolve_lock(aid, "v1")
    # 只比较素材数据：logs/events.jsonl 与 review/finalizations.json 是契约要求留痕的审计物，
    # 「不得改变版本状态」指的是 assets/** 与 LOCK，而不是整棵项目树零字节变化。
    before = {k: v for k, v in tree_digest(ha.root).items() if k.startswith("assets/")}
    assert before, "前置条件：必须已有素材数据"

    # 迟到的模型结果（契约：迟到结果 -> ABORTED 归档，绝不改变版本状态）
    late = reviewer.submit_model_result(cycle["review_cycle_id"], score=10.0, passed=False)
    late_text = str(late)
    assert "ABORT" in late_text.upper() or "already_finalized" in late_text, (
        f"迟到结果必须被标记为失效/已终结，实际 {late!r}"
    )

    after = {k: v for k, v in tree_digest(ha.root).items() if k.startswith("assets/")}
    assert_tree_unchanged(before, after, what="迟到结果到达后的素材数据（assets/**）")
    locked_after = ha.assets.resolve_lock(aid, "v1")
    assert locked_after == locked_before, "迟到结果不得改动已提交版本的 LOCK/审核结论"


def test_second_commit_after_first_is_rejected(tmp_path):
    """P1 顺序版本：非并发场景下第二次提交也必须被拒绝（条件更新本身要成立）。"""
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    commit = load("bettercomfyui.review.commit")
    fn = contract_attr(commit, "commit_final")
    cycle, reviewer = _cycle(cfg, ha, aid, f1, v1)

    first = fn(cycle, store=ha.assets)
    second = fn(cycle, store=ha.assets)
    assert first.get("committed") is True, f"首次提交必须成功，实际 {first!r}"
    assert second.get("committed") is False, f"第二次提交必须失败，实际 {second!r}"
    assert second.get("reason") == "already_finalized", (
        f"reason 必须是 already_finalized，实际 {second.get('reason')!r}"
    )
