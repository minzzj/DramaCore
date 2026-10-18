"""director/plan.py：段计划 + R3 引用解析 + 不返回部分结果。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bettercomfyui.director.errors import LockGateError, SegmentPlanError  # noqa: E402
from bettercomfyui.director.plan import (  # noqa: E402
    encode_dependency_ref,
    facet_value,
    locked_asset_lookup,
    parse_dependency_ref,
    plan_segments,
)
from bettercomfyui.models.entities import Segment  # noqa: E402
from bettercomfyui.models.enums import SegmentStatus  # noqa: E402

from test_director_fakes import (  # noqa: E402
    FakeAssetStore,
    FakeIds,
    locked_assets_for,
    make_attempt,
    make_config,
    make_segment,
)

PID = "0123456789abcdef0123456789abcdef"


def script_two_chapters():
    return {
        "title": "第一章 雪夜",
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {
                        "segment_index": 1,
                        "prompt": "雪夜街景，主角走入画面",
                        "asset_refs": ["A1", "A2"],
                        "duration_s": 12,
                        "generation_params": {"resolution": "720P"},
                    },
                    {
                        "segment_index": 2,
                        "prompt": "主角回头",
                        "asset_refs": ["A1"],
                        "dependency_refs": [{"segment_id": None}],  # 占位，测试内替换
                    },
                ],
            },
            {
                "chapter_id": "CH002",
                "segments": [{"segment_index": 1, "prompt": "黎明", "asset_refs": []}],
            },
        ],
    }


def test_plan_builds_segments_with_versions_and_params():
    script = script_two_chapters()
    seg1_id = f"{PID}_CH1_SEG1"
    script["chapters"][0]["segments"][1]["dependency_refs"] = [
        {"segment_id": seg1_id, "kind": "last_frame"}
    ]
    plan = plan_segments(
        script,
        make_config(),
        locked_assets=locked_assets_for(("A1", "v3"), ("A2", "v1")),
        ids=FakeIds(),
        project_id=PID,
    )
    assert len(plan) == 3
    assert all(isinstance(s, Segment) for s in plan)
    first, second, third = plan
    assert first.segment_id == f"{PID}_CH1_SEG1"
    assert first.chapter_id == "CH001"
    assert first.segment_index == 1
    assert third.segment_id == f"{PID}_CH2_SEG1"
    assert third.segment_index == 3  # 跨章节全局序号
    assert first.status is SegmentStatus.PLANNED
    assert first.current_version == "v1"

    # R3：引用必须落到具体版本
    assert first.asset_refs == [
        {"asset_id": "A1", "version": "v3", "ref": "A1"},
        {"asset_id": "A2", "version": "v1", "ref": "A2"},
    ]
    assert all("version" in r for r in first.asset_refs)

    # 全局参数注入 + 单段覆盖必须显式记录
    assert first.generation_params["segment_seconds"] == 12
    assert first.generation_params["resolution"] == "720P"  # 单段覆盖
    assert first.generation_params["params_source"] == "segment_override"
    assert first.generation_params["overrides"] == {"resolution": "720P"}
    assert second.generation_params["resolution"] == "1080P"
    assert second.generation_params["params_source"] == "global"

    # 依赖引用编码
    assert second.dependency_refs == [encode_dependency_ref(seg1_id, "last_frame", None, "v1")]
    assert parse_dependency_ref(second.dependency_refs[0])["kind"] == "last_frame"


def test_plan_rejects_unlocked_asset_reference():
    script = {
        "chapters": [
            {"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p", "asset_refs": ["A9"]}]}
        ]
    }
    with pytest.raises(LockGateError) as err:
        plan_segments(script, make_config(), locked_assets=locked_assets_for(("A1", "v1")), ids=FakeIds(), project_id=PID)
    assert "A9" in str(err.value)


def test_plan_rejects_reference_without_version():
    script = {
        "chapters": [
            {"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p", "asset_refs": ["A1"]}]}
        ]
    }
    with pytest.raises(LockGateError):
        plan_segments(
            script,
            make_config(),
            locked_assets={"A1": {"asset_id": "A1"}},  # 缺 version
            ids=FakeIds(),
            project_id=PID,
        )


def test_plan_rejects_ambiguous_reference():
    script = {
        "chapters": [
            {"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p", "asset_refs": ["A1"]}]}
        ]
    }
    with pytest.raises(LockGateError) as err:
        plan_segments(
            script,
            make_config(),
            locked_assets={"A1": [{"asset_id": "A1", "version": "v1"}, {"asset_id": "A1", "version": "v2"}]},
            ids=FakeIds(),
            project_id=PID,
        )
    assert "歧义" in str(err.value) or "多个候选版本" in str(err.value)


def test_plan_double_gate_with_store():
    """locked_assets 说可用但 store.resolve_lock 拒绝 -> 仍然拒绝（不放过）。"""
    script = {
        "chapters": [
            {"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p", "asset_refs": ["A1"]}]}
        ]
    }
    store = FakeAssetStore()  # 没有任何 LOCK
    with pytest.raises(LockGateError):
        plan_segments(
            script,
            make_config(),
            locked_assets=locked_assets_for(("A1", "v1")),
            ids=FakeIds(),
            project_id=PID,
            store=store,
        )
    assert store.resolve_calls == [("A1", "v1")]


def test_plan_missing_prompt_is_rejected():
    script = {"chapters": [{"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "   "}]}]}
    with pytest.raises(SegmentPlanError) as err:
        plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)
    assert "prompt" in str(err.value)


def test_plan_missing_project_id_is_rejected():
    script = {"chapters": [{"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p"}]}]}
    with pytest.raises(SegmentPlanError) as err:
        plan_segments(script, make_config(), locked_assets={}, ids=FakeIds())
    assert "project_id" in str(err.value)


def test_plan_forward_dependency_is_rejected():
    script = {
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {"segment_index": 1, "prompt": "p1", "dependency_refs": [f"{PID}_CH1_SEG2"]},
                    {"segment_index": 2, "prompt": "p2"},
                ],
            }
        ]
    }
    with pytest.raises(SegmentPlanError):
        plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)


def test_plan_unknown_dependency_kind_is_rejected():
    script = {
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {"segment_index": 1, "prompt": "p1"},
                    {
                        "segment_index": 2,
                        "prompt": "p2",
                        "dependency_refs": [{"segment_id": f"{PID}_CH1_SEG1", "kind": "magic"}],
                    },
                ],
            }
        ]
    }
    with pytest.raises(SegmentPlanError):
        plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)


def test_plan_never_returns_partial_result():
    """第二章非法 -> 整个计划抛错，绝不返回「半份计划」。"""
    script = {
        "chapters": [
            {"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "ok"}]},
            {"chapter_id": "CH002", "segments": [{"segment_index": 1, "prompt": ""}]},
        ]
    }
    with pytest.raises(SegmentPlanError):
        plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)


def test_plan_rejects_empty_and_non_mapping_input():
    with pytest.raises(SegmentPlanError):
        plan_segments({}, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)
    with pytest.raises(SegmentPlanError):
        plan_segments({"chapters": []}, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)
    with pytest.raises(SegmentPlanError):
        plan_segments([1, 2], make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)


def test_plan_as_dicts_serializable():
    script = {"chapters": [{"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p"}]}]}
    plan = plan_segments(
        script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID, as_dicts=True
    )
    assert isinstance(plan[0], dict)
    assert plan[0]["segment_id"] == f"{PID}_CH1_SEG1"
    assert plan[0]["status"] == "PLANNED"


def test_locked_asset_lookup_shapes():
    assert locked_asset_lookup({"A1": {"asset_id": "A1", "version": "v2"}})("A1")["version"] == "v2"
    assert locked_asset_lookup({"A1": "v5"})("A1") == {"asset_id": "A1", "version": "v5"}
    assert locked_asset_lookup({("A1", "v7"): {"asset_id": "A1"}})("A1")["version"] == "v7"
    assert locked_asset_lookup({"A1": {"v3": {"asset_id": "A1"}}})("A1")["version"] == "v3"
    assert locked_asset_lookup(lambda ref: {"asset_id": ref, "version": "v1"})("Z")["version"] == "v1"
    assert locked_asset_lookup({})("Z") is None


def test_facet_value_uses_attempt_fingerprints():
    attempt = make_attempt(
        "S1", "v2", 2, content_hash="sha256:" + "b" * 64, last_frame_path="tail_v2.png"
    )
    seg = make_segment("S1", 1, current_version="v2", attempts=[attempt])
    assert facet_value(seg, "last_frame") == "tail_v2.png"
    assert facet_value(seg, "reference_frame") == "sha256:" + "b" * 64
    assert facet_value(seg, "prompt") == "测试分镜提示词"
    assert facet_value(make_segment("S2"), "last_frame") is None


# ---------------------------------------------------------------------------
# O1：同章节相邻段自动衔接（级联失效判定的依赖证据来源）
# ---------------------------------------------------------------------------


def test_auto_chain_links_consecutive_segments():
    script = {
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {"segment_index": 1, "prompt": "p1"},
                    {"segment_index": 2, "prompt": "p2"},
                    {"segment_index": 3, "prompt": "p3"},
                ],
            }
        ]
    }
    plan = plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)
    ids = [s.segment_id for s in plan]
    assert plan[0].dependency_refs == []
    assert plan[1].dependency_refs == [encode_dependency_ref(ids[0], "last_frame", None, "v1")]
    assert plan[2].dependency_refs == [encode_dependency_ref(ids[1], "last_frame", None, "v1")]
    # 记录 ref_version 才能作为级联失效证据
    assert parse_dependency_ref(plan[1].dependency_refs[0])["ref_version"] == "v1"


def test_auto_chain_can_be_disabled():
    script = {
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [{"segment_index": 1, "prompt": "p1"}, {"segment_index": 2, "prompt": "p2"}],
            }
        ]
    }
    plan = plan_segments(
        script,
        make_config(**{"director.auto_chain": False}),
        locked_assets={},
        ids=FakeIds(),
        project_id=PID,
    )
    assert plan[1].dependency_refs == []


def test_explicit_dependency_wins_over_auto_chain():
    seg1 = f"{PID}_CH1_SEG1"
    script = {
        "chapters": [
            {
                "chapter_id": "CH001",
                "segments": [
                    {"segment_index": 1, "prompt": "p1"},
                    {
                        "segment_index": 2,
                        "prompt": "p2",
                        "dependency_refs": [{"segment_id": seg1, "kind": "motion"}],
                    },
                ],
            }
        ]
    }
    plan = plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)
    assert plan[1].dependency_refs == [encode_dependency_ref(seg1, "motion", None, "v1")]


def test_auto_chain_does_not_cross_chapters():
    script = {
        "chapters": [
            {"chapter_id": "CH001", "segments": [{"segment_index": 1, "prompt": "p1"}]},
            {"chapter_id": "CH002", "segments": [{"segment_index": 1, "prompt": "p2"}]},
        ]
    }
    plan = plan_segments(script, make_config(), locked_assets={}, ids=FakeIds(), project_id=PID)
    assert plan[1].dependency_refs == []
