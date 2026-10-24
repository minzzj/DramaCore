"""验收 6：新版本不继承旧版 LOCK/审核结论。

原文（任务书第 9 节第 6 条，第 188 行）：
    「新版本不继承旧版 LOCK/审核结论。」

需要模块：bettercomfyui.store.assets（set_lock / resolve_lock / clear_lock / get_version）、
          bettercomfyui.store.ids（lock_id）
依据：CONTRACTS.md 第 4 节；R3「LOCK 绑定 asset_id + version + content_hash」；
     方案第七部分一 第 8 条、第七部分十 第 1 条。

判定通过：
    P1 set_lock(v1) 后 resolve_lock(v1) 非空，且 lock_source 正确；
    P2 追加 v2 后 resolve_lock(v2) 必须为 None（未审核的新版本不可用于生成）；
    P3 content_hash 与磁盘不一致时 set_lock 必须抛错（方案第七部分七：内容指纹校验）；
    P4 clear_lock(v1) 后 resolve_lock(v1) 为 None（LOCK 可撤销但不影响 v2 记录）。

判定假通过（必须能变红）：
    F1 LOCK 记在「素材条目」而不是「版本」上（新版本自动继承）—— P2 会变红；
    F2 set_lock 不校验 content_hash —— P3 会变红；
    F3 resolve_lock 恒返回非空 —— P2/P4 会变红；
    F4 resolve_lock 恒返回 None —— P1 会变红。
"""

from __future__ import annotations

import pytest

from _api import asset_id_of, create_asset, create_project, make_asset_file, set_lock
from _harness import contract_attr, load, make_config


def _setup(tmp_path):
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "锁定项目", cfg)
    ea = create_asset(ha, name="角色A", asset_type="CHARACTER")
    aid = asset_id_of(ea)
    f1 = make_asset_file(tmp_path, "v1.png", b"V1-BYTES")
    f2 = make_asset_file(tmp_path, "v2.png", b"V2-BYTES-DIFFERENT")
    v1 = ha.assets.add_version(aid, source="AI", file_path=f1, status="PENDING_REVIEW")
    return ha, aid, v1, f1, f2


def test_lock_binds_to_version_not_asset(tmp_path):
    """P1/P2：LOCK 绑定版本，新版本绝不继承。"""
    ha, aid, v1, _f1, f2 = _setup(tmp_path)

    before = ha.assets.resolve_lock(aid, "v1")
    assert before is None, "未写入 LOCK 前 resolve_lock(v1) 必须为 None"

    locked = set_lock(ha.assets, aid, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"])
    assert locked is not None and locked.get("lock_source") == "HUMAN_PASS", (
        f"LOCK 必须记录来源，实际 {locked!r}"
    )
    got = ha.assets.resolve_lock(aid, "v1")
    assert got is not None, "set_lock 后 resolve_lock(v1) 必须返回记录"

    # R3 核心：追加新版本
    v2 = ha.assets.add_version(aid, source="AI", file_path=f2, status="PENDING_REVIEW")
    assert str(v2["version"]) == "v2"
    assert ha.assets.resolve_lock(aid, "v2") is None, (
        "R3 违背：v2 继承了旧 LOCK/审核结论，未审核素材将进入生成"
    )
    # 旧版本的 LOCK 仍然有效且未被改动
    again = ha.assets.resolve_lock(aid, "v1")
    assert again is not None and again.get("lock_source") == "HUMAN_PASS", (
        "新版本不应影响旧版本自身的 LOCK 记录"
    )


def test_lock_id_format_matches_contract(tmp_path):
    """P1 加强：lock_id 必须形如 LOCK_{asset_id}_{version}。

    实测形状：`set_lock` 返回**版本记录**（lock_id 在 `extra.lock_id`），
    `resolve_lock` 返回**扁平的 LOCK 记录**（lock_id 在顶层）。两者都接受。
    """
    ha, aid, v1, _f1, _f2 = _setup(tmp_path)
    ids = load("bettercomfyui.store.ids")
    fn = contract_attr(ids, "lock_id")
    expected = f"LOCK_{aid}_v1"
    assert fn(aid, "v1") == expected, (
        f"lock_id 必须是 LOCK_{{asset_id}}_{{version}}，期望 {expected}，实际 {fn(aid, 'v1')}"
    )
    locked = set_lock(ha.assets, aid, "v1", lock_source="MODEL_PASS", content_hash=v1["content_hash"])
    resolved = ha.assets.resolve_lock(aid, "v1")
    assert resolved is not None, "set_lock 后 resolve_lock 必须返回记录"
    got = str(resolved.get("lock_id") or (locked.get("extra") or {}).get("lock_id") or "")
    assert got == expected, (
        f"LOCK 记录里的 lock_id 必须是 {expected}，实际 {got!r}"
        f"（set_lock 返回键={sorted(locked)}，resolve_lock 返回键={sorted(resolved)}）"
    )


def test_set_lock_rejects_hash_mismatch(tmp_path):
    """P3：内容指纹与磁盘不一致 -> 必须抛错（不得静默写入 LOCK）。"""
    ha, aid, _v1, _f1, _f2 = _setup(tmp_path)
    bogus = "sha256:" + "0" * 64
    with pytest.raises(Exception) as ei:  # noqa: BLE001 - 契约只要求「必须校验」，异常类型不限
        set_lock(ha.assets, aid, "v1", lock_source="MODEL_PASS", content_hash=bogus)
    assert ha.assets.resolve_lock(aid, "v1") is None, (
        "校验失败后不得留下任何 LOCK（否则等于用坏指纹锁定了素材）"
    )
    msg = str(ei.value)
    assert msg, "抛错必须带可读信息"


def test_clear_lock_unlocks_only_that_version(tmp_path):
    """P4：clear_lock 只影响指定版本。"""
    ha, aid, v1, _f1, f2 = _setup(tmp_path)
    set_lock(ha.assets, aid, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"])
    v2 = ha.assets.add_version(aid, source="AI", file_path=f2, status="PENDING_REVIEW")
    set_lock(ha.assets, aid, "v2", lock_source="MODEL_PASS", content_hash=v2["content_hash"])

    ha.assets.clear_lock(aid, "v1")
    assert ha.assets.resolve_lock(aid, "v1") is None, "clear_lock(v1) 后应不可用于生成"
    assert ha.assets.resolve_lock(aid, "v2") is not None, "clear_lock(v1) 不得影响 v2"


def test_unlocked_version_cannot_be_used_for_generation(tmp_path):
    """R3 门禁：resolve_lock 是生成侧的唯一判据，未锁定版本必须为 None。"""
    ha, aid, _v1, _f1, f2 = _setup(tmp_path)
    v2 = ha.assets.add_version(aid, source="AI", file_path=f2, status="DRAFT")
    assert ha.assets.resolve_lock(aid, "v2") is None
    assert v2.get("lock_status", "UNLOCKED") == "UNLOCKED", (
        f"新版本 lock_status 必须显式为 UNLOCKED，实际 {v2.get('lock_status')!r}"
    )
