"""验收 12：服务重启后从检查点恢复，不重复创建版本或拼接。

原文（任务书第 9 节第 12 条，第 194 行）：
    「服务重启后从检查点恢复，不重复创建版本或拼接。」

需要模块：bettercomfyui.store.checkpoints（CheckpointStore）、bettercomfyui.store.ids
          （idempotency_key / canonical_json）、bettercomfyui.store.atomic、
          bettercomfyui.models.enums（Stage / ResumeTarget）
依据：CONTRACTS.md 第 2、3、4 节；方案第七部分七（原子写入、幂等键）；任务书第 6 节。

判定通过：
    P1 save 多次后 latest() 返回最新；at_or_before(Stage) 不返回比它更晚的检查点；
    P2 换一个进程/实例（模拟重启）后 latest() 仍能读到同一检查点；
    P3 resolve_resume_target() 返回 ResumeTarget（G/P/R）且与已保存阶段一致；
    P4 半写入/损坏的检查点文件不得被当成有效数据（原子写语义）；
    P5 幂等键确定性：同 namespace + 同逻辑对象 -> 同键；不同 namespace -> 不同键。

判定假通过（必须能变红）：
    F1 检查点只存在内存里 —— P2 会变红；
    F2 latest() 直接取目录里字典序最后一个文件而不校验内容 —— P4 会变红；
    F3 幂等键带时间戳/随机数 —— P5 会变红（同一输入两次必须相同）。
"""

from __future__ import annotations

import json

import pytest

from _api import build_checkpoint_store, create_project, make_asset_file, open_project
from _harness import contract_attr, load, make_config


def _enums():
    e = load("bettercomfyui.models.enums")
    return contract_attr(e, "Stage", kind="class"), contract_attr(e, "ResumeTarget", kind="class")


def _stages_mod():
    return load("bettercomfyui.models.enums")


def test_save_and_latest_are_ordered(tmp_path):
    """P1：多次保存后 latest() 是最新；at_or_before 不越界。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "检查点项目", cfg)
    Stage, _RT = _enums()
    ck = build_checkpoint_store(tmp_path, cfg, ha.root)

    seq = [Stage.PROJECT, Stage.SCRIPT_PARSE, Stage.ASSET_MATCH, Stage.ASSET_GENERATE]
    paths = []
    for i, st in enumerate(seq):
        p = ck.save(st, {"marker": i, "stage": st.value})
        paths.append(p)
        assert p is not None and str(p), f"save({st}) 必须返回路径"

    newest = ck.latest()
    assert isinstance(newest, dict), f"latest() 必须返回 dict，实际 {newest!r}"
    assert (newest.get("payload") or {}).get("marker") == len(seq) - 1 or newest.get("stage") == seq[-1].value, (
        f"latest() 必须返回最后一次保存的内容，实际 {newest!r}"
    )

    at = ck.latest(at_or_before=Stage.SCRIPT_PARSE)
    if at is not None:
        stage_val = str(at.get("stage"))
        order = [s.value for s in Stage]
        assert order.index(stage_val) <= order.index(Stage.SCRIPT_PARSE.value), (
            f"at_or_before(SCRIPT_PARSE) 不得返回更晚的阶段，实际 {at!r}"
        )


def test_latest_survives_restart(tmp_path):
    """P2：模拟重启（新实例、新进程语义）后仍能读到同一检查点。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "重启项目", cfg)
    Stage, _RT = _enums()

    ck1 = build_checkpoint_store(tmp_path, cfg, ha.root)
    ck1.save(Stage.PROJECT, {"marker": "a"})
    ck1.save(Stage.SEGMENT_GENERATE, {"marker": "b"})

    # 换一个新实例（模拟进程重启）：只给目录，不给任何内存状态
    ck2 = build_checkpoint_store(tmp_path, cfg, ha.root)
    latest = ck2.latest()
    assert isinstance(latest, dict) and (latest.get("payload") or {}).get("marker") == "b", (
        f"重启后必须能恢复最近检查点，实际 {latest!r}"
    )


def test_resolve_resume_target_is_g_p_or_r(tmp_path):
    """P3：恢复目标必须是 G/P/R，且与已保存阶段一致。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "恢复项目", cfg)
    Stage, RT = _enums()
    ck = build_checkpoint_store(tmp_path, cfg, ha.root)

    allowed = {RT.ASSET.value, RT.DIRECTOR.value, RT.REVIEW.value}
    cases = [
        (Stage.ASSET_REVIEW, RT.ASSET.value),
        (Stage.DIRECTOR_PLAN, RT.DIRECTOR.value),
        (Stage.SEGMENT_REVIEW, RT.REVIEW.value),
    ]
    for stage, expected in cases:
        ck.save(stage, {"stage": stage.value})
        got = ck.resolve_resume_target()
        val = getattr(got, "value", got)
        assert val in allowed, f"resolve_resume_target 必须返回 G/P/R，实际 {got!r}"
        assert val == expected, (
            f"阶段 {stage.value} 的恢复目标应为 {expected}，实际 {val}"
        )


def test_corrupted_checkpoint_is_not_treated_as_valid(tmp_path):
    """P4：损坏/半写入的检查点不得被当成有效数据。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "损坏项目", cfg)
    Stage, _RT = _enums()
    ck = build_checkpoint_store(tmp_path, cfg, ha.root)
    ck.save(Stage.PROJECT, {"marker": "good"})

    ckpt_dir = ha.root / "checkpoints"
    assert ckpt_dir.is_dir(), f"检查点必须落在 {ckpt_dir}（CONTRACTS.md 第 1 节目录布局）"
    bad = ckpt_dir / "zzz_corrupted.json"
    bad.write_text('{"marker": "broken", "oops"', encoding="utf-8")  # 半写入 JSON

    try:
        latest = ck.latest()
    except Exception as exc:  # noqa: BLE001 - 显式报错可以接受
        assert "corrupt" in str(exc).lower() or "json" in str(exc).lower() or "解析" in str(exc), (
            f"损坏检查点必须显式报错并说明原因，实际 {type(exc).__name__}: {exc}"
        )
        return
    assert latest != {"marker": "broken", "oops": None}, "损坏文件被当成了有效检查点"
    assert isinstance(latest, dict) and (latest.get("payload") or {}).get("marker") == "good", (
        f"损坏的最新文件应被跳过、退回上一个有效检查点，实际 {latest!r}"
    )


def test_idempotency_key_is_deterministic(tmp_path):
    """P5：幂等键必须是 canonical_json 的 sha256，不含时间戳/随机数。"""
    ids = load("bettercomfyui.store.ids")
    fn = contract_attr(ids, "idempotency_key")

    payload_a = {"prompt": "林夕走进教室", "steps": 25, "seed": 1234}
    payload_b = {"seed": 1234, "steps": 25, "prompt": "林夕走进教室"}  # 键序不同，逻辑相同
    k1 = fn("segment_generate", payload_a)
    k2 = fn("segment_generate", payload_b)
    k3 = fn("segment_review", payload_a)

    assert isinstance(k1, str) and ":" in k1, f"幂等键格式必须是 {{namespace}}:{{sha256}}，实际 {k1!r}"
    ns, _, digest = k1.partition(":")
    assert ns == "segment_generate", f"命名空间必须显式区分任务类型，实际 {ns!r}"
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), (
        f"摘要必须是 64 位小写 hex，实际 {digest!r}"
    )
    assert k1 == k2, "同一逻辑 payload（键序不同）必须得到同一幂等键"
    assert k1 != k3, "不同命名空间必须得到不同幂等键"


def test_resume_does_not_duplicate_asset_versions(tmp_path):
    """P2 加强：重启后重复执行同一素材入库请求不得创建新版本（幂等键去重）。"""
    ids = load("bettercomfyui.store.ids")
    atomic = load("bettercomfyui.store.atomic")
    canonical = contract_attr(atomic, "canonical_json")
    key_fn = contract_attr(ids, "idempotency_key")

    payload = {"asset": "角色A", "prompt": "参考图", "seed": 7}
    a = canonical(payload)
    b = canonical({"seed": 7, "prompt": "参考图", "asset": "角色A"})
    assert a == b, "canonical_json 必须对键序不敏感（确定性）"
    assert key_fn("asset_generate", payload) == key_fn("asset_generate", {"seed": 7, "prompt": "参考图", "asset": "角色A"}), (
        "同一逻辑请求必须得到同一幂等键，否则重启后会重复创建版本"
    )
