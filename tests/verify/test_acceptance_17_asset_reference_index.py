"""验收 17：**素材 → 段**反向索引与「中程失败」的爆炸半径。

## 对应流程图

图一 SG8：

    ERR1 -.->|"中程失败仅重跑依赖该素材且未通过的段，已通过段用缓存"| GEN1

图四：

    F2A["某角色参考图不合格"] --> F2B["仅重跑依赖该角色且未通过的段"]

愿景原话：

> 算力成本和质量之间取中间值。不追求极致质量烧掉所有算力，
> 也不为了省算力牺牲成片可看性。**只重新生成不合格的那一段**。

## 为什么要新增这一条

`AssetStore.record_reference()` / `references_of()` 早就实现了，但**产品代码零调用**。
实测跑完整个 `run_demo`，所有素材的 `references` 都是空的（ROUND17 发现）。
后果是上面两条流程图**算不出来**，只能整条链重跑。

## 判定通过

    P1 跑完 `run_demo` 后，每个被引用的素材都有非空 `references`；
    P2 引用记录里含 `segment_id`，且能反查出**具体是哪些段**；
    P3 `reference_count` 按**不同段**计数（同一段重复引用不虚高）；
    P4 `segments_depending_on_asset` 默认只返回**未通过**的段；
    P5 段通过之后，再查就返回空（已通过的用缓存保留 —— 不重跑）；
    P6 `include_passed=True` 时能看到全部；
    P7 不在段列表里的引用记录被忽略（不返回查不到状态的幽灵段）。

## 判定假通过（必须能变红）

    F1 把 `plan_segments` 里的 `record_segment_asset_references` 调用删掉 -> P1 变红；
    F2 让 `segments_depending_on_asset` 忽略"是否通过" -> P4/P5 变红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _run_demo(tmp: Path) -> tuple[dict, Path]:
    """跑一次 demo，返回 (report, projects_root)。

    ⚠️ 必须把 projects_root 一起返回：_stores() 若自己重新 load_config()，
    会读回**默认** project_root（真实 projects/），于是找不到临时项目 ——
    第一版就这么写的，结果素材数=0、段数=0，测试全红。
    """
    from bettercomfyui.config import load_config
    from bettercomfyui.demo import run_demo

    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    projects_root = work / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.project_root = str(projects_root)
    rep = run_demo(repo_root=REPO_ROOT, config=cfg, project=None,
                   executor="mock", workdir=work)
    return rep, projects_root


def _stores(rep: dict, projects_root: Path):
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.segments import SegmentStore

    paths = ProjectPaths(root=projects_root, project_id=rep["project_id"])
    return AssetStore(paths), SegmentStore(paths), paths


# ---------------------------------------------------------------------------
# P1 / P2 反向索引被写起来了
# ---------------------------------------------------------------------------


def test_p1_assets_have_references_after_demo(tmp_path):
    """跑完 demo，被引用的素材必须有 `references`（修前全是空的）。"""
    rep, root = _run_demo(tmp_path)
    store, _seg, _paths = _stores(rep, root)
    assets = store.list_assets()
    assert assets, "没有任何素材"
    with_refs = [a for a in assets if store.references_of(a["asset_id"])]
    assert with_refs, (
        "所有素材的 references 都是空的 —— 素材→段反向索引没接线"
    )


def test_p2_references_point_at_real_segments(tmp_path):
    """引用记录必须含 `segment_id`，且能在段存储里找到对应段。"""
    rep, root = _run_demo(tmp_path)
    store, seg_store, _paths = _stores(rep, root)
    seg_ids = {s.segment_id for s in seg_store.list_segments()}
    assert seg_ids, "段存储为空"

    checked = 0
    for a in store.list_assets():
        for ref in store.references_of(a["asset_id"]):
            sid = ref.get("segment_id")
            assert sid, f"引用记录缺 segment_id：{ref!r}"
            assert sid in seg_ids, f"引用记录指向不存在的段：{sid}"
            checked += 1
    assert checked > 0, "一条引用记录都没有"


def test_p3_reference_count_counts_distinct_segments(tmp_path):
    """`reference_count` 按不同段计：同一段重复引用同一素材只算一次。"""
    rep, root = _run_demo(tmp_path)
    store, _seg, _paths = _stores(rep, root)
    for a in store.list_assets():
        aid = a["asset_id"]
        refs = store.referencing_segments(aid)
        distinct = {str(r.get("segment_id")) for r in refs if r.get("segment_id")}
        assert store.reference_count(aid) == len(distinct), (
            f"{a['name']} 的引用次数应等于不同段数 {len(distinct)}"
        )


# ---------------------------------------------------------------------------
# P4 / P5 / P6 / P7 中程失败的爆炸半径
# ---------------------------------------------------------------------------


def _pick_asset_with_segments(store, seg_store):
    """挑一个"被至少一个段引用"的素材，返回 (asset_id, [依赖它的段])。"""
    from bettercomfyui.director.cascade import segments_depending_on_asset

    for a in store.list_assets():
        aid = a["asset_id"]
        segs = seg_store.list_segments()
        hit = segments_depending_on_asset(store, segs, aid, include_passed=True)
        if hit:
            return aid, hit
    pytest.skip("没有素材被任何段引用，跳过")


def test_p4_only_unpassed_segments_are_returned(tmp_path):
    """默认只返回**未通过**的段 —— 已通过的用缓存保留，不重跑。"""
    from bettercomfyui.director.cascade import segments_depending_on_asset
    from bettercomfyui.models.enums import SegmentStatus

    rep, root = _run_demo(tmp_path)
    store, seg_store, _paths = _stores(rep, root)
    aid, all_hits = _pick_asset_with_segments(store, seg_store)
    assert all_hits, "没有依赖段"

    # 先把所有依赖段标成"已通过"
    segs = seg_store.list_segments()
    for hit in all_hits:
        seg = hit["segment"]
        seg.status = SegmentStatus.PASSED
        seg_store.update(seg)

    fresh = {s.segment_id: s for s in seg_store.list_segments()}
    unpassed = segments_depending_on_asset(store, list(fresh.values()), aid)
    assert unpassed == [], f"已通过的段仍被返回：{[h['segment'].segment_id for h in unpassed]}"


def test_p5_failed_segment_is_returned_after_being_unpassed(tmp_path):
    """把某段标成未通过后，它必须出现在结果里（这正是"中程失败"要找的）。"""
    from bettercomfyui.director.cascade import segments_depending_on_asset
    from bettercomfyui.models.enums import SegmentStatus

    rep, root = _run_demo(tmp_path)
    store, seg_store, _paths = _stores(rep, root)
    aid, all_hits = _pick_asset_with_segments(store, seg_store)

    segs = seg_store.list_segments()
    for hit in all_hits:
        seg = hit["segment"]
        seg.status = SegmentStatus.PASSED
        seg_store.update(seg)
    # 只把第一个标成失败
    target = all_hits[0]["segment"].segment_id
    for s in seg_store.list_segments():
        if s.segment_id == target:
            obj = seg_store.load_or_raise(target)
            obj.status = SegmentStatus.FAILED
            seg_store.update(obj)
            break

    hits = segments_depending_on_asset(store, seg_store.list_segments(), aid)
    ids = {h["segment"].segment_id for h in hits}
    assert target in ids, f"失败的段 {target} 没被返回（中程失败算不出来）"
    assert len(ids) == 1, f"只该返回失败的那一段，实际 {ids}"


def test_p6_include_passed_shows_all(tmp_path):
    """`include_passed=True` 时能看到全部依赖段。"""
    from bettercomfyui.director.cascade import segments_depending_on_asset

    rep, root = _run_demo(tmp_path)
    store, seg_store, _paths = _stores(rep, root)
    aid, all_hits = _pick_asset_with_segments(store, seg_store)
    every = segments_depending_on_asset(
        store, seg_store.list_segments(), aid, include_passed=True)
    assert len(every) >= len(all_hits), "include_passed 没有返回更多"


def test_p7_ghost_references_are_ignored(tmp_path):
    """引用记录指向不在段列表里的段时，必须忽略（不返回查不到状态的幽灵段）。"""
    from bettercomfyui.director.cascade import segments_depending_on_asset

    rep, root = _run_demo(tmp_path)
    store, seg_store, _paths = _stores(rep, root)
    aid, _hits = _pick_asset_with_segments(store, seg_store)
    # 传空段列表：所有引用都成了"幽灵"
    assert segments_depending_on_asset(store, [], aid, include_passed=True) == []
