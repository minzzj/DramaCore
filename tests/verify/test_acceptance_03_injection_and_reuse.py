"""验收 3：已有素材正确注入；明确匹配时复用、不重复生成。

原文（任务书第 9 节第 3 条，第 185 行）：
    「已有素材正确注入；明确匹配时复用、不重复生成。」

需要模块：bettercomfyui.store.inject（build_existing_asset_manifest）、
          bettercomfyui.prompt.match（match_candidate / MatchResult）
依据：CONTRACTS.md 第 4、5 节；R1「存在性判断只能由程序做」；
     方案第七部分（A1->A2->A3->A4 增量注入）；方案第五部分 24 项（空素材库边界）。

判定通过：
    P1 空素材库 -> build_existing_asset_manifest 返回 ""（调用方据此走 A1B 的「空」分支）；
    P2 有素材 -> 清单文本包含「类型 + 序号 + 名称 + 锁定状态」四要素；
    P3 归一化后明确匹配 -> EXISTS_REUSE，且**不产生任何新版本**（文件树字节级不变）；
    P4 确实不存在 -> NEW_GENERATE。

判定假通过（必须能变红）：
    F1 清单由 AI/模型生成而不是程序读库 —— 静态检查见 check_contracts.py（prompt/match 不得依赖 gateway）；
        运行时判据：清单内容必须随素材库实际内容变化而变化（加一个素材必须出现在清单里）；
    F2 匹配走「总是 NEW_GENERATE」实现 —— P3 会变红；
    F3 匹配走「总是 EXISTS_REUSE」实现 —— P4 会变红；
    F4 复用路径偷偷写了新版本 —— tree_digest 断言会变红。
"""

from __future__ import annotations

import pytest

from _api import asset_id_of, create_asset, create_project, make_asset_file, open_project, set_lock
from _harness import assert_tree_unchanged, contract_attr, load, make_config, tree_digest


def _mods():
    inject = load("bettercomfyui.store.inject")
    match = load("bettercomfyui.prompt.match")
    return inject, match


def _candidate(name: str, **extra) -> dict:
    """契约未定义 candidate 的完整字段，这里给出最小必需字段 + 常见别名。"""
    cand = {
        "name": name,
        "normalized_name": name,
        "asset_type": "CHARACTER",
        "prompt": f"{name} 的参考图",
    }
    cand.update(extra)
    return cand


def _outcome(result) -> str:
    val = getattr(result, "outcome", None)
    if val is None and isinstance(result, (tuple, list)) and result:
        val = result[0]
    return getattr(val, "value", val)


def test_empty_library_manifest_is_empty_string(tmp_path):
    """P1：空素材库边界（方案第五部分 24 项，A1B 跳过注入）。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "空库项目", cfg)
    inject, _match = _mods()
    text = contract_attr(inject, "build_existing_asset_manifest")(ha.assets)
    assert text == "", f"空素材库必须返回空字符串（调用方据此跳过注入），实际 {text!r}"


def test_manifest_contains_type_name_and_lock_status(tmp_path):
    """P2：清单必须是程序读库得到的四要素文本，且随库内容变化。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "注入项目", cfg)
    inject, _match = _mods()
    fn = contract_attr(inject, "build_existing_asset_manifest")

    fa = make_asset_file(tmp_path, "a.png", b"inject-A")
    ea = create_asset(ha, name="林夕", source="AI")
    aid = asset_id_of(ea)
    v1 = ha.assets.add_version(aid, source="AI", file_path=fa, status="DRAFT")
    assert v1.get("content_hash"), "add_version 必须返回 content_hash（契约第 4 节）"
    set_lock(ha.assets, aid, str(v1.get("version", "v1")), lock_source="HUMAN_PASS",
             content_hash=v1["content_hash"])

    text = fn(ha.assets)
    assert isinstance(text, str) and text.strip(), "有素材时清单不得为空"
    assert "林夕" in text, f"清单必须包含素材名称，实际：{text!r}"
    assert ("CHARACTER" in text.upper()) or ("角色" in text), f"清单必须包含素材类型，实际：{text!r}"
    assert "v1" in text, f"清单必须包含版本信息，实际：{text!r}"
    assert ("LOCK" in text.upper()) or ("锁定" in text) or ("已通过" in text), (
        f"清单必须包含锁定状态（契约：类型+序号+名称+锁定状态），实际：{text!r}"
    )

    # F1 判据：清单必须反映库的真实内容
    fb = make_asset_file(tmp_path, "b.png", b"inject-B")
    eb = create_asset(ha, name="陈默", source="AI")
    ha.assets.add_version(asset_id_of(eb), source="AI", file_path=fb, status="DRAFT")
    text2 = fn(ha.assets)
    assert text2 != text and "陈默" in text2, (
        "新增素材后清单必须变化并包含新素材（否则清单不是程序读库得到的）"
    )


def test_explicit_match_reuses_without_new_version(tmp_path):
    """P3：明确匹配 -> EXISTS_REUSE，且不得产生新版本。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "匹配项目", cfg)
    _inject, match = _mods()

    fa = make_asset_file(tmp_path, "a.png", b"reuse-content")
    ea = create_asset(ha, name="林夕", source="AI")
    aid = asset_id_of(ea)
    ha.assets.add_version(aid, source="AI", file_path=fa, status="DRAFT")

    before = tree_digest(ha.root)
    res = contract_attr(match, "match_candidate")(_candidate("林夕"), ha.assets)
    assert _outcome(res) == "EXISTS_REUSE", f"明确匹配必须复用，实际 outcome={_outcome(res)}（reason={getattr(res, 'reason', None)}）"
    assert getattr(res, "asset_id", None) == aid, "复用必须指向既有 asset_id"
    assert getattr(res, "version", None) == "v1", "复用必须指向具体版本"

    after = tree_digest(ha.root)
    assert_tree_unchanged(before, after, what="EXISTS_REUSE 匹配过程（不得写入新版本/新文件）")


def test_absent_candidate_generates(tmp_path):
    """P4：确实不存在 -> NEW_GENERATE（防止「总是复用」的假实现）。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "匹配项目2", cfg)
    _inject, match = _mods()

    fa = make_asset_file(tmp_path, "a.png", b"reuse-content")
    ea = create_asset(ha, name="林夕", source="AI")
    ha.assets.add_version(asset_id_of(ea), source="AI", file_path=fa, status="DRAFT")

    res = contract_attr(match, "match_candidate")(_candidate("全新的角色XYZ"), ha.assets)
    assert _outcome(res) == "NEW_GENERATE", (
        f"不存在的素材必须进入生成队列，实际 outcome={_outcome(res)}"
    )


def test_normalized_match_survives_spacing_and_case(tmp_path):
    """P3 加强：空格/全角空格/大小写差异仍是「明确匹配」，不得误判为新增。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "归一化项目", cfg)
    _inject, match = _mods()

    fa = make_asset_file(tmp_path, "a.png", b"norm")
    ea = create_asset(ha, name="LinXi", source="AI")
    ha.assets.add_version(asset_id_of(ea), source="AI", file_path=fa, status="DRAFT")

    for variant in ("LinXi", " linxi ", "LINXI", "Lin\u3000Xi"):
        res = contract_attr(match, "match_candidate")(_candidate(variant), ha.assets)
        assert _outcome(res) == "EXISTS_REUSE", (
            f"归一化后同一名称 {variant!r} 必须复用，实际 {_outcome(res)}"
        )


def test_match_never_imports_ai_gateway():
    """R1 静态判据：match 模块不得依赖 gateway（存在性判断不能交给 AI）。"""
    match = load("bettercomfyui.prompt.match")
    src = open(match.__file__, encoding="utf-8").read()
    assert "gateway" not in src.lower().replace("promptgateway", ""), (
        "R1 违背：prompt/match.py 出现 gateway 依赖，存在性判断必须纯程序确定性"
    )
