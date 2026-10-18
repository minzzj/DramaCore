"""验收 7：人工审核超时后按配置切换模型，且记录事件。

原文（任务书第 9 节第 7 条，第 189 行）：
    「人工审核超时后按配置切换模型，且记录事件。」

需要模块：bettercomfyui.review.asset_review（AssetReviewer）、review.counters、
          review.notify（NotificationService）
依据：CONTRACTS.md 第 6、10 节；方案第七部分一 第 2/3 条；R4。

实测要点（供报告引用）：
    * `review_version` 的 `material` 必须能定位被审核版本（含 `file_path` 或 `content_hash`），
      否则进入 FAILED（这是确定性校验，不是缺陷）；
    * `AssetReviewer(clock=...)` 是可注入的字符串时钟 —— 用它做确定性超时测试；
    * 不传 `project=` 时事件只留内存镜像并打印 WARN（「这不算「已记录」」），
      因此本用例显式传 `project=` 以验证事件真的落到 `logs/events.jsonl`；
    * 通知渠道未注册时投递失败**只写 WARN、不阻断主流程**（实测符合契约第 10 节）。

判定通过：
    P1 review_version(mode=HUMAN) 返回 review_cycle_id，状态为 AWAITING_REVIEW；
    P2 闲置超过 human_timeout_minutes 后 on_human_timeout 生效，mode 变为 AUTO_SWITCHED；
    P3 模式切换事件写入 logs/events.jsonl；
    P4 通知失败不阻断主流程。

判定假通过（必须能变红）：
    F1 超时计时永不触发 —— P2 变红（假时钟推进 11 分钟）；
    F2 只改内存不写事件 —— P3 变红；
    F3 通知抛错直接冒泡 —— P4 变红。
    F4 计数器不可读导致「CNT1 归零」无法验证 —— 单列为未验证（缺口 G-7）。
"""

from __future__ import annotations

import json

import pytest

from _api import FakeClock, asset_id_of, build_asset_reviewer, create_asset, create_project, make_asset_file
from _harness import contract_attr, load, make_config, read_jsonl


def _setup(tmp_path, cfg=None):
    cfg = cfg or make_config(asset_review={"default_mode": "HUMAN", "human_timeout_minutes": 10})
    ha, _store, _root = create_project(tmp_path, "超时项目", cfg)
    ea = create_asset(ha, name="角色A", asset_type="CHARACTER")
    aid = asset_id_of(ea)
    f1 = make_asset_file(tmp_path, "v1.png", b"TIMEOUT-V1")
    v1 = ha.assets.add_version(aid, source="AI", file_path=f1, status="PENDING_REVIEW")
    return cfg, ha, aid, f1, v1


def _reviewer(ha, cfg, clock):
    return build_asset_reviewer(ha.assets, cfg, project=ha.paths, clock=clock)


def test_human_review_awaits_and_times_out_with_event(tmp_path):
    """P1-P4：闲置超时 -> 切模式 + 写事件 + 不抛错。"""
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    enums = load("bettercomfyui.models.enums")
    ReviewMode = contract_attr(enums, "ReviewMode", kind="class")
    clock = FakeClock()
    reviewer = _reviewer(ha, cfg, clock)

    material = {"file_path": str(f1), "content_hash": v1["content_hash"], "prompt": "角色A 参考图"}
    res = reviewer.review_version(aid, "v1", mode=ReviewMode.HUMAN, material=material)
    assert isinstance(res, dict), f"review_version 必须返回 dict，实际 {type(res)!r}"
    cycle_id = res.get("review_cycle_id")
    assert isinstance(cycle_id, str) and cycle_id, f"必须返回 review_cycle_id，实际 {res!r}"
    assert res.get("state") == "AWAITING_REVIEW", (
        f"人工模式启动后必须处于 AWAITING_REVIEW，实际 {res.get('state')}（reason={res.get('reason')}）"
    )

    # 界面打开但持续闲置：推进 11 分钟必须能超时（不得因「界面开着」永不超时）
    clock.advance(minutes=11)
    out = reviewer.on_human_timeout(cycle_id)
    assert isinstance(out, dict), f"on_human_timeout 必须返回 dict，实际 {type(out)!r}"
    assert out.get("timeout") is True, f"11 分钟闲置必须判定超时，实际 {out!r}"
    assert out.get("mode") == "AUTO_SWITCHED" or "AUTO_SWITCHED" in json.dumps(out, ensure_ascii=False), (
        f"超时切换必须把模式置为 AUTO_SWITCHED（方案第七部分一 第 3 条），实际 {out!r}"
    )

    # P3：事件必须落盘
    events = read_jsonl(ha.events_path())
    assert events, f"必须写 {ha.events_path()}（契约第 10 节）"
    hit = [e for e in events if "AUTO_SWITCHED" in json.dumps(e, ensure_ascii=False)]
    assert hit, (
        f"模式切换事件未写入事件日志（方案 Z4/Z5：模式切换必须记录）。已有事件：{[e.get('event') for e in events]}"
    )


def test_review_version_requires_locatable_material(tmp_path):
    """P1 加强：material 无法定位被审核版本时必须显式 FAILED（不得静默当通过）。"""
    cfg, ha, aid, _f1, _v1 = _setup(tmp_path)
    enums = load("bettercomfyui.models.enums")
    ReviewMode = contract_attr(enums, "ReviewMode", kind="class")
    reviewer = _reviewer(ha, cfg, FakeClock())

    res = reviewer.review_version(aid, "v1", mode=ReviewMode.HUMAN, material={"prompt": "只有提示词"})
    assert res.get("state") == "FAILED", (
        f"material 缺 file_path/content_hash 时必须显式 FAILED（确定性校验），实际 {res.get('state')}"
    )
    assert res.get("lock_source") is None, "校验失败绝不能产生 LOCK"
    assert ha.assets.resolve_lock(aid, "v1") is None, "FAILED 路径不得写出任何 LOCK"


def test_notify_failure_does_not_block_timeout_switch(tmp_path):
    """P4：通知失败绝不能阻断主流程（契约第 10 节；实现打印 WARN 后继续）。"""
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    enums = load("bettercomfyui.models.enums")
    ReviewMode = contract_attr(enums, "ReviewMode", kind="class")
    clock = FakeClock()
    reviewer = _reviewer(ha, cfg, clock)

    res = reviewer.review_version(
        aid, "v1", mode=ReviewMode.HUMAN,
        material={"file_path": str(f1), "content_hash": v1["content_hash"]},
    )
    clock.advance(minutes=11)
    out = reviewer.on_human_timeout(res["review_cycle_id"])  # 不得抛错
    assert isinstance(out, dict) and out.get("timeout") is True, (
        f"通知渠道未注册时仍必须完成超时切换，实际 {out!r}"
    )
    notif = out.get("notification")
    if isinstance(notif, list):
        assert all(isinstance(n, dict) for n in notif), "通知投递结果必须逐渠道如实记录"
        assert not any(n.get("ok") is not True and not n.get("error") for n in notif), (
            "投递失败必须带 error 说明（不得假装已发送）"
        )


def test_cnt1_not_consumed_by_human_and_reset_on_switch(tmp_path):
    """R4：人工次数不计入 CNT1；超时切换后 CNT1 从 0 开始。"""
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    enums = load("bettercomfyui.models.enums")
    ReviewMode = contract_attr(enums, "ReviewMode", kind="class")
    CounterKind = contract_attr(enums, "CounterKind", kind="class")
    from _api import build_counter_store

    counters = build_counter_store(cfg)
    clock = FakeClock()
    reviewer = build_asset_reviewer(
        ha.assets, cfg, project=ha.paths, clock=clock, counters=counters
    )
    material = {"file_path": str(f1), "content_hash": v1["content_hash"]}

    for _ in range(3):
        res = reviewer.review_version(aid, "v1", mode=ReviewMode.HUMAN, material=material)
    assert counters.get(aid, CounterKind.CNT1) == 0, (
        f"人工审核次数不得计入 CNT1（方案第七部分一 第 2 条），实际 {counters.get(aid, CounterKind.CNT1)}"
    )

    clock.advance(minutes=11)
    out = reviewer.on_human_timeout(res["review_cycle_id"])
    assert out.get("cnt1") == 0, f"超时切换后 CNT1 必须从 0 开始，实际 {out.get('cnt1')}"
    assert counters.get(aid, CounterKind.CNT1) == 0, "切换后 CNT1 必须为 0"


def test_active_operation_pauses_timeout_needs_activity_api(tmp_path):
    """H1B/H1C：只有「持续有效审核操作」才暂停计时。

    契约缺口 G-3：CONTRACTS.md 未定义「有效审核操作」上报接口。实测 AssetReviewer
    公开方法清单里也没有 note_activity/touch 一类入口，因此本用例记为**未验证**。
    """
    cfg, ha, aid, f1, v1 = _setup(tmp_path)
    enums = load("bettercomfyui.models.enums")
    ReviewMode = contract_attr(enums, "ReviewMode", kind="class")
    clock = FakeClock()
    reviewer = _reviewer(ha, cfg, clock)

    activity = [
        n for n in dir(reviewer)
        if any(k in n.lower() for k in ("activity", "touch", "heartbeat", "keep_alive", "keepalive"))
        and not n.startswith("__")
        and callable(getattr(reviewer, n, None))  # 实测存在同名 dict 字段 _activity，必须过滤
    ]
    if not activity:
        pytest.skip(
            "未验证：AssetReviewer 未暴露任何「有效审核操作」上报接口"
            f"（现有公开方法：{sorted(n for n in dir(reviewer) if not n.startswith('_'))}）。"
            "缺少前置条件：契约需定义例如 note_activity(cycle_id)，并在其调用后暂停超时计时，"
            "才能可复现地验证「持续操作不超时 / 界面闲置仍超时」。"
        )

    res = reviewer.review_version(
        aid, "v1", mode=ReviewMode.HUMAN,
        material={"file_path": str(f1), "content_hash": v1["content_hash"]},
    )
    cycle_id = res["review_cycle_id"]
    touch = getattr(reviewer, activity[0])
    for _ in range(12):
        clock.advance(minutes=1)
        try:
            touch(cycle_id, kind="inspect")  # kind 必须属于 VALID_HUMAN_OP_KINDS（实测集合），否则计为 ignored_ops
        except TypeError:
            touch(cycle_id)
    out = reviewer.on_human_timeout(cycle_id)
    assert out.get("timeout") is not True, "持续有效操作期间不得超时切换（H1B/H1C）"
