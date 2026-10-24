"""边界用例：跨实例迟到结果归档、未知周期显式报错、中文渠道名与多选通知。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_review_fakes import (  # noqa: E402
    FakeAssetStore,
    FakeClock,
    make_material,
    make_reviewer,
    recording_channel,
    seed_version,
)

from bettercomfyui.review.commit import reset_memory_ledger  # noqa: E402
from bettercomfyui.review.errors import ReviewError  # noqa: E402
from bettercomfyui.review.notify import NotificationService, resolve_channel_name  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_memory_ledger():
    reset_memory_ledger()
    yield
    reset_memory_ledger()


def test_late_result_from_another_instance_is_archived_only():
    """另一个 AssetReviewer 实例（等价于跨进程/重启后的 worker）收到迟到结果。

    必须凭 finalization ledger 判定为「已终结 -> 归档 ABORTED」，
    且**不得触碰版本状态**（meta.json 字节不变）。
    """
    store, clock = FakeAssetStore(), FakeClock()
    asset_id, version = seed_version(store)
    material = {"file_path": version.get("file_path"), "content_hash": version["content_hash"]}

    first = make_reviewer(store, clock=clock)
    opened = first.review_version(asset_id, version["version"], mode="HUMAN", material=material)
    committed = first.submit_human_result(opened["review_cycle_id"], passed=True, operator="alice")
    assert committed["committed"] is True
    before = store.read_meta_bytes(asset_id, version["version"])

    # 另一个实例：注册表为空，但共享同一 finalization ledger（内存模式为进程级共享）
    second = make_reviewer(store, clock=clock)
    assert second.records() == []
    late = second.submit_human_result(opened["review_cycle_id"], passed=False, operator="late-bob")
    assert late["accepted"] is False
    assert late["orphan_instance"] is True
    assert late["archive_state"] == "ABORTED"
    assert late["wrote_version_state"] is False
    assert store.read_meta_bytes(asset_id, version["version"]) == before
    assert store.call_count("set_lock") == 1
    assert second.late_results(opened["review_cycle_id"])[0]["orphan_instance"] is True

    late_model = second.submit_model_result(opened["review_cycle_id"], score=10, passed=False)
    assert late_model["orphan_instance"] is True
    assert store.read_meta_bytes(asset_id, version["version"]) == before


def test_unknown_cycle_without_ledger_record_still_raises():
    """ledger 里也没有记录 = 真正未知：必须显式报错，绝不静默归档。"""
    store, clock = FakeAssetStore(), FakeClock()
    reviewer = make_reviewer(store, clock=clock)
    with pytest.raises(ReviewError) as excinfo:
        reviewer.submit_human_result("ASSET:nope:v1", passed=True, operator="x")
    assert "unknown" in str(excinfo.value).lower() or "未知" in str(excinfo.value)
    with pytest.raises(ReviewError):
        reviewer.submit_model_result("ASSET:nope:v1", score=1.0, passed=False)


def test_chinese_channel_names_resolve_to_canonical():
    assert resolve_channel_name("弹窗") == "POPUP"
    assert resolve_channel_name("声音") == "SOUND"
    assert resolve_channel_name("飞书") == "FEISHU"
    assert resolve_channel_name("钉钉") == "DINGTALK"
    assert resolve_channel_name("qq") == "QQ"
    assert resolve_channel_name("FeiShu") == "FEISHU"


def test_multi_channel_selection_delivers_to_chosen_channels_only():
    popup = recording_channel("POPUP")
    feishu = recording_channel("FEISHU")
    dingtalk = recording_channel("DINGTALK", fail=True)
    service = NotificationService([popup, feishu, dingtalk])
    assert service.registered_channels() == ["DINGTALK", "FEISHU", "POPUP"]

    results = service.notify(title="审核超时", message="已切换模型", channels=["弹窗", "飞书", "钉钉"])
    assert {r["channel"]: r["ok"] for r in results} == {"POPUP": True, "FEISHU": True, "DINGTALK": False}
    assert len(popup.send_fn.__self__.sent) == 1
    assert len(feishu.send_fn.__self__.sent) == 1
    assert "未接入" not in results[2]["error"]  # 已注册但投递失败：如实报错且不抛


def test_all_default_channels_failing_never_blocks():
    service = NotificationService(params=None, channels=["弹窗", "声音", "QQ", "飞书", "钉钉"])
    results = service.notify(title="t", message="m")
    assert len(results) == 5
    assert all(r["ok"] is False for r in results)
    assert all(r["error"] for r in results), "失败必须带 error，绝不假装已发送"
