"""真实实现集成测试：store.ids + store.AssetStore + review.CounterStore + models.Config。

与 `test_director_*.py`（替身驱动）互补：本文件**不注入替身**，专门验证
「契约已冻结、真实模块到位后」director/executors 的行为是否仍然成立：
- `default_ids()` 真的能解析 `store.ids`，且 ID 格式与契约 §2 一致；
- R3 门禁走真实 `AssetStore.resolve_lock`（内容指纹一致才放行）；
- CNT2/CNT3 走真实 `review.CounterStore`，技术失败不动计数；
- 级联 CNT3 作用域 = `store.ids.cnt_id(segment_id, "CNT3")`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import bettercomfyui.store.ids as store_ids  # noqa: E402
from bettercomfyui.director.cascade import CascadeController  # noqa: E402
from bettercomfyui.director.errors import LockGateError  # noqa: E402
from bettercomfyui.director.generate import SegmentGenerator  # noqa: E402
from bettercomfyui.director.plan import encode_dependency_ref, plan_segments  # noqa: E402
from bettercomfyui.director.review import SegmentReviewer  # noqa: E402
from bettercomfyui.director.rework import ReworkController  # noqa: E402
from bettercomfyui.models.entities import Config  # noqa: E402
from bettercomfyui.models.enums import CounterKind, DegradeFlag, SegmentStatus  # noqa: E402
from bettercomfyui.review import CounterStore  # noqa: E402
from bettercomfyui.store.assets import AssetStore  # noqa: E402
from bettercomfyui.store.paths import ProjectPaths  # noqa: E402

from test_director_fakes import (  # noqa: E402
    FailingExecutor,
    ScriptedExecutor,
    fixed_scorer,
    make_attempt,
    make_segment,
)

PID = None  # 每个测试动态生成


def real_project(tmp_path: Path, *, display_name: str = "集成测试项目"):
    from bettercomfyui.executors.base import ExecutorError  # noqa: F401  (保持导入面清晰)

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    pid = store_ids.new_project_id()
    paths = ProjectPaths(root, pid).ensure_layout()
    paths.project_json.write_text(
        json.dumps({"project_id": pid, "display_name": display_name}, ensure_ascii=False),
        encoding="utf-8",
    )
    return pid, paths, AssetStore(paths)


def make_locked_asset(assets: AssetStore, source_file: Path, *, name: str = "林澈"):
    asset = assets.create_asset(
        asset_type="CHARACTER", name=name, source="AI", library_scope="PROJECT"
    )
    version = assets.add_version(
        asset["asset_id"], source="AI", file_path=source_file, status="PENDING_REVIEW"
    )
    locked = assets.set_lock(
        asset["asset_id"],
        version["version"],
        lock_source="HUMAN_PASS",
        content_hash=version["content_hash"],
        review_cycle_id=f"asset:{asset['asset_id']}:{version['version']}",
    )
    return asset, locked


def script_for(pid: str, asset_ref: str):
    return {
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {
                        "segment_index": 1,
                        "prompt": "雪夜街景，主角走入画面",
                        "asset_refs": [asset_ref],
                        "duration_s": 10,
                    },
                    {
                        "segment_index": 2,
                        "prompt": "主角回头",
                        "asset_refs": [asset_ref],
                    },
                ],
            }
        ]
    }


def real_config(**overrides):
    cfg = Config()
    for path, value in overrides.items():
        head, _, tail = path.partition(".")
        target = getattr(cfg, head) if tail else cfg
        setattr(target, tail or head, value)
    return cfg


def test_real_store_ids_drive_plan_generate_and_review(tmp_path):
    pid, paths, assets = real_project(tmp_path)
    src = tmp_path / "linche_v1.png"
    src.write_bytes(b"fake-character-png-v1")
    asset, locked = make_locked_asset(assets, src)
    cfg = real_config()

    plan = plan_segments(
        script_for(pid, asset["asset_id"]),
        cfg,
        locked_assets={asset["asset_id"]: {"asset_id": asset["asset_id"], "version": "v1"}},
        project_id=pid,
        store=assets,  # 真实 R3 双门禁
    )
    assert [s.segment_id for s in plan] == [
        store_ids.segment_id(pid, 1, 1),
        store_ids.segment_id(pid, 1, 2),
    ]
    assert plan[0].asset_refs == [
        {
            "asset_id": asset["asset_id"],
            "version": locked["version"],
            "content_hash": locked["content_hash"],
            "lock_source": "HUMAN_PASS",
            "ref": asset["asset_id"],
        }
    ]

    counters = CounterStore()
    generator = SegmentGenerator(assets, counters, cfg=cfg, project_id=pid)
    executor = ScriptedExecutor(tmp_path / "jobs")
    gen = generator.generate(plan[0], executor=executor)
    assert gen["technical_failure"] is False
    assert gen["version"] == "v1"
    assert gen["attempt_id"] == store_ids.attempt_id(plan[0].segment_id, "v1", 1)

    reviewer = SegmentReviewer(counters, cfg=cfg, scorer=fixed_scorer(80))
    review = reviewer.review(gen["segment"], gen["version"])
    assert review["audit_id"] == store_ids.audit_id(plan[0].segment_id, "v1")
    assert review["audit_id"] == f"AUDIT_{plan[0].segment_id}_v1"
    assert review["review_cycle_id"] == store_ids.review_cycle_id(
        "segment", plan[0].segment_id, "v1"
    )
    assert review["passed"] is True


def test_real_asset_store_blocks_unlocked_reference(tmp_path):
    pid, _, assets = real_project(tmp_path)
    src = tmp_path / "unlocked.png"
    src.write_bytes(b"no-lock-yet")
    asset = assets.create_asset(
        asset_type="CHARACTER", name="未审核角色", source="MANUAL", library_scope="PROJECT"
    )
    version = assets.add_version(
        asset["asset_id"], source="MANUAL", file_path=src, status="MANUAL_UNREVIEWED"
    )
    assert assets.resolve_lock(asset["asset_id"], version["version"]) is None

    # 1) 计划阶段就被 R3 拦下
    with pytest.raises(LockGateError):
        plan_segments(
            script_for(pid, asset["asset_id"]),
            real_config(),
            locked_assets={},
            project_id=pid,
            store=assets,
        )
    # 2) 生成阶段再次被拦下（双保险），且不产生任何提交
    counters = CounterStore()
    generator = SegmentGenerator(assets, counters, cfg=real_config(), project_id=pid)
    segment = make_segment(
        store_ids.segment_id(pid, 1, 1),
        1,
        asset_refs=[{"asset_id": asset["asset_id"], "version": version["version"]}],
    )
    executor = ScriptedExecutor(tmp_path / "jobs")
    with pytest.raises(LockGateError):
        generator.generate(segment, executor=executor)
    assert executor.submits == []
    assert counters.get(segment.segment_id, CounterKind.CNT2) == 0
    assert counters.get(segment.segment_id, CounterKind.CNT3) == 0


def test_real_counter_store_untouched_by_technical_failures(tmp_path):
    pid, _, assets = real_project(tmp_path)
    src = tmp_path / "a.png"
    src.write_bytes(b"locked")
    asset, locked = make_locked_asset(assets, src)
    counters = CounterStore()
    generator = SegmentGenerator(assets, counters, cfg=real_config(), project_id=pid)
    segment = make_segment(
        store_ids.segment_id(pid, 1, 1),
        1,
        asset_refs=[{"asset_id": asset["asset_id"], "version": locked["version"]}],
    )
    for _ in range(5):
        result = generator.generate(segment, executor=FailingExecutor())
        assert result["technical_failure"] is True
    assert generator.tech_retries.get(segment.segment_id) == 5
    # 真实 CounterStore：CNT2/CNT3 必须仍然是 0
    assert counters.get(segment.segment_id, CounterKind.CNT2) == 0
    assert counters.get(segment.segment_id, CounterKind.CNT3) == 0
    assert counters.get(segment.segment_id, CounterKind.CNT1) == 0


def test_real_counter_store_separates_cnt2_and_cnt3(tmp_path):
    pid, _, assets = real_project(tmp_path)
    src = tmp_path / "a.png"
    src.write_bytes(b"locked")
    asset, locked = make_locked_asset(assets, src)
    cfg = real_config(**{"video_review.cnt2_limit": 10, "video_review.cnt3_limit": 10})
    counters = CounterStore()
    generator = SegmentGenerator(assets, counters, cfg=cfg, project_id=pid)
    reviewer = SegmentReviewer(counters, cfg=cfg, scorer=fixed_scorer(80))
    rework = ReworkController(
        counters, cfg=cfg, generator=generator, reviewer=reviewer, project_id=pid
    )
    executor = ScriptedExecutor(tmp_path / "jobs")

    normal_id = store_ids.segment_id(pid, 1, 9)
    normal = make_segment(
        normal_id,
        9,
        asset_refs=[{"asset_id": asset["asset_id"], "version": locked["version"]}],
        current_version="v1",
        attempts=[make_attempt(normal_id, "v1", 1, score=40.0, passed=False)],
        status=SegmentStatus.REVIEWING,
    )
    for _ in range(3):
        normal = rework.handle_quality_failure(normal, {"score": 40}, executor=executor)["segment"]

    # 级联段（依赖上游）
    up_id = store_ids.segment_id(pid, 1, 1)
    up = make_segment(
        up_id,
        1,
        asset_refs=[{"asset_id": asset["asset_id"], "version": locked["version"]}],
        current_version="v2",
        attempts=[make_attempt(up_id, "v2", 2, content_hash="sha256:" + "a" * 64)],
    )
    down_id = store_ids.segment_id(pid, 1, 2)
    down = make_segment(
        down_id,
        2,
        asset_refs=[{"asset_id": asset["asset_id"], "version": locked["version"]}],
        current_version="v2",
        attempts=[make_attempt(down_id, "v2", 2)],
        dependency_refs=[encode_dependency_ref(up_id, "reference_frame", "stale-facet", "v1")],
    )
    cascade = CascadeController(
        counters,
        cfg=cfg,
        generator=generator,
        reviewer=SegmentReviewer(counters, cfg=cfg, scorer=fixed_scorer(80), edge_scorer=lambda a, b: {"score": 95}),
        project_id=pid,
    )
    cascade.set_segments([up, down])
    mark = cascade.mark_next(down, cascade_id="CASCADE_X")
    assert mark["cnt3_scope"] == store_ids.cnt_id(down_id, "CNT3")
    assert counters.get(down_id, CounterKind.CNT3) == 0  # 建作用域不计数
    run = cascade.run(down, executor=executor)
    assert run["outcome"] == "N1_CLEAN"

    assert counters.get(normal_id, CounterKind.CNT2) == 3
    assert counters.get(normal_id, CounterKind.CNT3) == 0
    assert counters.get(down_id, CounterKind.CNT3) == 1
    assert counters.get(down_id, CounterKind.CNT2) == 0
    # 版本号递增没有清零任何计数器
    assert normal.current_version == "v4"
    assert counters.get(normal_id, CounterKind.CNT2) == 3


def test_real_cascade_depth_limit_still_terminates(tmp_path):
    pid, _, assets = real_project(tmp_path)
    src = tmp_path / "a.png"
    src.write_bytes(b"locked")
    asset, locked = make_locked_asset(assets, src)
    cfg = real_config(**{"video_review.max_cascade_depth": 3})
    counters = CounterStore()
    generator = SegmentGenerator(assets, counters, cfg=cfg, project_id=pid)
    reviewer = SegmentReviewer(
        counters, cfg=cfg, scorer=fixed_scorer(80), edge_scorer=lambda a, b: {"score": 95}
    )
    cascade = CascadeController(
        counters, cfg=cfg, generator=generator, reviewer=reviewer, project_id=pid
    )
    segments = []
    for i in range(6):
        sid = store_ids.segment_id(pid, 1, i + 1)
        deps = (
            [encode_dependency_ref(store_ids.segment_id(pid, 1, i), "reference_frame", "stale", "v1")]
            if i
            else []
        )
        segments.append(
            make_segment(
                sid,
                i + 1,
                asset_refs=[{"asset_id": asset["asset_id"], "version": locked["version"]}],
                current_version="v2",
                attempts=[make_attempt(sid, "v2", 2, content_hash="sha256:" + f"{i:064d}")],
                dependency_refs=deps,
            )
        )
    cascade.set_segments(segments)
    result = cascade.propagate(segments[0], executor=ScriptedExecutor(tmp_path / "jobs"))
    assert result["stopped_reason"] == "max_cascade_depth"
    assert result["iterations"] == 4
    assert result["degrade_flag"] is DegradeFlag.CASCADE_DEGRADED
    assert reviewer.full_review_calls == 0
    assert result["full_review_calls"] == 0
    assert counters.get(segments[4].segment_id, CounterKind.CNT2) == 0
