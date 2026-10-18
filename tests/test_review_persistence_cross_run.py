"""回归：素材审核器跨 ComfyUI 节点执行的状态必须**落盘可恢复**。

## 为什么要有这个文件（审计 R1 发现）

`BetterComfyUIAssetReview` 每次执行都新建 `AssetReviewer`。而审核器的
候选历史、人工等待窗口、周期状态此前**只存在内存**：

* 模型三轮未过时，「从三轮候选中取最高分」跨运行只剩当前一轮候选
  （等于取当前分，不是最高分 —— 每轮对应一个**新版本**，这是方案 K→L 流程）；
* 人工等待/超时切换需要跨运行保留 AWAITING_REVIEW 周期，否则
  H1B→H3→H4→H2 整条链在产品里根本不可达；
* 人工超时切到模型模式后，周期停在 SCANNING；旧实现再次 `review_version`
  只返回 reused、**不执行模型轮**，H4→H2 断链。

修复后：审核状态写入 `<项目>/review/reviewer_state.json`，每次节点执行先 restore、
再推进超时；`review_version` 对 SCANNING 周期在模型模式下真正执行一轮模型审核。
本文件锁住这些契约。

    P1 三轮候选（v1/v2/v3）跨运行累积：FALLBACK 选中**最高分 v2**，不是当前轮 v3；
    P2 人工等待跨运行保留；超时后自动切模型，CNT1 重置；
    P3 切换后再审必须真正执行模型轮（模型评分参与，状态离开 SCANNING）；
    P4 ComfyUI 审核节点跨两次执行：等待态落盘、强制切换后模型轮真的跑了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.config import load_config  # noqa: E402
from bettercomfyui.models.enums import (  # noqa: E402
    AssetSource, AssetStatus, AssetType, LibraryScope, ReviewMode,
)
from bettercomfyui.review.asset_review import AssetReviewer  # noqa: E402
from bettercomfyui.review.counters import CounterStore  # noqa: E402
from bettercomfyui.store.assets import AssetStore  # noqa: E402
from bettercomfyui.store.paths import ProjectPaths  # noqa: E402
from bettercomfyui.store.projects import ProjectStore  # noqa: E402


def _make_project(tmp_path: Path):
    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    info = ProjectStore(root, cfg).create("审核跨运行回归")
    pid = str(info["project_id"])
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    assets = AssetStore(paths)
    img = tmp_path / "candidate.png"
    from PIL import Image

    Image.fromarray(np.full((64, 64, 3), 120, dtype=np.uint8)).save(img)
    entry = assets.create_asset(
        asset_type=AssetType.CHARACTER, name="跨运行角色",
        source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    rec = assets.add_version(
        entry["asset_id"], source=AssetSource.AI,
        file_path=img, status=AssetStatus.PENDING_REVIEW)
    material = _material(assets, entry["asset_id"], rec)
    return cfg, pid, paths, assets, material, img


def _material(assets, asset_id: str, rec: dict) -> dict:
    return {
        "asset_id": asset_id,
        "version": rec["version"],
        "file_path": rec.get("file_path"),
        "content_hash": rec["content_hash"],
    }


def _reviewer(cfg, pid, paths, assets, *, model_review, clock=None):
    return AssetReviewer(
        assets, config=cfg, counters=CounterStore(), project_id=pid,
        project=paths, model_review=model_review, clock=clock)


# ---------------------------------------------------------------------------
# P1：三轮候选（三个版本）跨运行累积，FALLBACK 选最高分
# ---------------------------------------------------------------------------


def test_fallback_picks_highest_across_separate_runs(tmp_path):
    cfg, pid, paths, assets, material, img = _make_project(tmp_path)
    threshold = float(cfg.asset_review.score_threshold)
    # 三轮对应三个**新版本**（方案 K→L：失败 -> 生成新版本 -> 重审）
    rounds = [
        (40.0, assets.add_version(material["asset_id"], source=AssetSource.AI,
                                  file_path=img, status=AssetStatus.PENDING_REVIEW)),
        (60.0, assets.add_version(material["asset_id"], source=AssetSource.AI,
                                  file_path=img, status=AssetStatus.PENDING_REVIEW)),
        (55.0, assets.add_version(material["asset_id"], source=AssetSource.AI,
                                  file_path=img, status=AssetStatus.PENDING_REVIEW)),
    ]
    scores = iter([score for score, _ in rounds])

    def model_review(_material):
        score = next(scores)
        return {"score": score, "passed": score >= threshold}

    snapshot = None
    for _score, rec in rounds:
        reviewer = _reviewer(cfg, pid, paths, assets, model_review=model_review)
        if snapshot is not None:
            reviewer.restore(snapshot)
        reviewer.review_version(material["asset_id"], rec["version"],
                                mode=ReviewMode.MODEL, material=_material(assets, material["asset_id"], rec))
        snapshot = reviewer.snapshot()

    candidates = snapshot["candidates"][material["asset_id"]]
    assert len(candidates) == 3, (
        f"跨运行候选历史没有累积：{candidates}（旧实现只剩当前轮）")
    assert [c["version"] for c in candidates] == ["v2", "v3", "v4"], candidates
    best = max(candidates, key=lambda c: (float(c["score"]), -int(c["round"])))
    assert best["score"] == 60.0, f"最高分候选应为 v3(60)，实际 {candidates}"
    assert best["version"] == "v3"
    # 兜底 LOCK 必须落在被选中的最高分版本上（v3），而不是当前轮 v4
    lock = assets.resolve_lock(material["asset_id"], "v3")
    assert lock is not None, "兜底必须给最高分版本写 LOCK"
    assert lock["lock_source"] == "FALLBACK_PASS", lock
    # LOCK 记录里不含分数：最高分判定以上面的 candidates 快照为准；
    # 再确认 review 结论的理由里写明了选中 v3（可追溯，不是黑箱）。
    review_reason = ""
    for record in snapshot.get("records") or []:
        if record.get("version") == "v3" and record.get("lock_source") == "FALLBACK_PASS":
            review_reason = str(record.get("reason") or "")
    assert "v3" in review_reason and "60" in review_reason, (
        f"兜底结论理由必须写明候选与选中版本/分数，实际 {review_reason!r}")
    assert assets.resolve_lock(material["asset_id"], "v4") is None, (
        "兜底不得给非最高分版本写 LOCK")


# ---------------------------------------------------------------------------
# P2 / P3：人工等待跨运行保留；超时切模型后模型轮真的执行
# ---------------------------------------------------------------------------


def test_human_timeout_and_model_round_across_runs(tmp_path):
    cfg, pid, paths, assets, material, _img = _make_project(tmp_path)
    clock1 = lambda: "2026-10-18T00:00:00+08:00"  # noqa: E731
    reviewer1 = _reviewer(
        cfg, pid, paths, assets, clock=clock1,
        model_review=lambda m: {"score": 85.0, "passed": True})
    opener = reviewer1.review_version(
        material["asset_id"], material["version"],
        mode=ReviewMode.HUMAN, material=material)
    assert str(opener.get("state")) in ("AWAITING_REVIEW", "ReviewState.AWAITING_REVIEW"), opener
    snapshot = reviewer1.snapshot()

    clock2 = lambda: "2026-10-18T00:11:00+08:00"  # noqa: E731 - 11 分钟 > 默认 10 分钟
    reviewer2 = _reviewer(
        cfg, pid, paths, assets, clock=clock2,
        model_review=lambda m: {"score": 85.0, "passed": True})
    reviewer2.restore(snapshot)
    switched = reviewer2.poll_timeouts()
    assert switched and switched[0].get("timeout") is True, (
        f"闲置 11 分钟（>默认 10 分钟）必须自动切换模型模式，实际 {switched}")
    assert switched[0]["mode"] == ReviewMode.AUTO_SWITCHED
    assert switched[0]["cnt1"] == 0, "切换后 CNT1 必须从 0 开始（此前人工次数不计入）"

    # P3：切换后再审必须真正执行模型轮（旧实现永远返回 SCANNING/reused）
    out = reviewer2.review_version(
        material["asset_id"], material["version"],
        mode=ReviewMode.AUTO_SWITCHED, material=material)
    assert str(out.get("state")) in ("ALLOWED", "ReviewState.ALLOWED"), (
        f"切换后的模型轮没有执行（评分 85 应通过），实际 state={out.get('state')!r}")
    assert out.get("cnt1") == 1, f"切换后第一轮模型审核 CNT1 应为 1，实际 {out.get('cnt1')!r}"
    lock = assets.resolve_lock(material["asset_id"], material["version"])
    assert lock is not None and lock["lock_source"] == "MODEL_PASS", lock


# ---------------------------------------------------------------------------
# P4：ComfyUI 审核节点跨两次执行 —— 等待态落盘、强制切换后模型轮真的跑了
# ---------------------------------------------------------------------------


def test_asset_review_node_persists_waiting_state_and_runs_model_round(tmp_path, monkeypatch):
    from bettercomfyui_comfy import nodes as N

    cfg, pid, paths, assets, material, _img = _make_project(tmp_path)
    # 把节点用的配置指向临时项目根（与既有审计脚本同一手法）
    monkeypatch.setattr(N, "_config", lambda: cfg)

    node = N.BetterComfyUIAssetReview()
    wait_out = node.run(
        project_id=pid, action="发起人工审核（等待）",
        asset_id=material["asset_id"], version=material["version"],
        operator="tester", reason="")
    wait_state = wait_out["result"][0]
    assert wait_state == "AWAITING_REVIEW", f"应停在人工等待态，实际 {wait_state}"

    state_path = Path(paths.project_dir) / "review" / "reviewer_state.json"
    assert state_path.is_file(), "审核器状态没有落盘（跨运行语义会丢失）"
    snapshot = json.loads(state_path.read_text(encoding="utf-8"))
    assert snapshot.get("cycles"), f"落盘状态里没有审核周期：{snapshot.keys()}"

    switch_out = node.run(
        project_id=pid, action="人工超时→切模型（强制）",
        asset_id=material["asset_id"], version=material["version"],
        operator="tester", reason="")
    state = switch_out["result"][0]
    score_payload = json.loads(switch_out["result"][1])
    assert state != "AWAITING_REVIEW", "强制切换后不得仍停在人工等待态"
    assert score_payload.get("mode") == "AUTO_SWITCHED", score_payload
    assert int(score_payload.get("cnt1") or 0) >= 1, (
        f"切换后必须真的执行模型轮（CNT1>=1），实际 {score_payload.get('cnt1')!r}")
    # 本地启发式对纯灰图低于阈值 -> BLOCKED；无论如何不得再是等待态
    assert state in ("BLOCKED", "ALLOWED", "FALLBACK"), state
