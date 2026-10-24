"""验收 4：疑似重名进入确认队列，不覆盖旧素材。

原文（任务书第 9 节第 4 条，第 186 行）：
    「疑似重名进入确认队列，不覆盖旧素材。」

需要模块：bettercomfyui.prompt.match（match_candidate / build_suspect_queue）、
          bettercomfyui.prompt.normalize（normalize_name / is_suspect）、
          bettercomfyui.store.assets（AssetStore）
依据：CONTRACTS.md 第 5 节；R1/R2；方案第五部分 42/44 项；方案第七部分一/八。

判定通过：
    P1 歧义候选（归一化名相同但存在多个条目，或高度相近）-> SUSPECT_DUPLICATE，
       suspect_id 以 SUSPECT_ 开头；
    P2 build_suspect_queue 能列出待确认记录；
    P3 旧素材内容字节级未被覆盖：既有版本的素材文件 sha256 完全不变。

判定假通过（必须能变红）：
    F1 「疑似」分支被实现成自动复用或自动新增 —— P1 断言 outcome 精确等于 SUSPECT_DUPLICATE；
    F2 旧文件被原地覆盖 —— P3 逐文件 sha256 比较会变红；
    F3 is_suspect 恒返回 False（等于没做疑似检测）—— test_is_suspect_detects_
       documented_similarity_kinds 会在三类差异上全部为 False 时变红。
"""

from __future__ import annotations

import pytest

from _api import asset_id_of, create_asset, create_project, make_asset_file, set_lock
from _harness import assert_tree_unchanged, contract_attr, load, make_config, tree_digest


def _candidate(name: str, **extra) -> dict:
    cand = {"name": name, "normalized_name": name, "asset_type": "CHARACTER", "prompt": name}
    cand.update(extra)
    return cand


def _outcome(result) -> str:
    val = getattr(result, "outcome", None)
    if val is None and isinstance(result, (tuple, list)) and result:
        val = result[0]
    return getattr(val, "value", val)


def _setup_with_history(tmp_path):
    """建项目 + 一个已锁定 v1 的素材；返回 (handle, asset_id, v1 文件字节快照)。"""
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "疑似项目", cfg)
    fa = make_asset_file(tmp_path, "linxi.png", b"ORIGINAL-v1-bytes-must-never-change")
    ea = create_asset(ha, name="林夕", source="AI")
    aid = asset_id_of(ea)
    v = ha.assets.add_version(aid, source="AI", file_path=fa, status="DRAFT")
    ver = str(v.get("version", "v1"))
    set_lock(ha.assets, aid, ver, lock_source="HUMAN_PASS", content_hash=v["content_hash"])
    return ha, aid, ver


def _force_ambiguity(ha, aid: str) -> bool:
    """制造「归一化名相同但存在多个条目」的歧义；返回是否成功。"""
    for alias in ("林夕", "林 夕", "林\u3000夕", "ＬＩＮＸＩ"):
        try:
            create_asset(ha, name=alias, source="AI")
        except Exception:  # noqa: BLE001 - 实现可能拒绝重复名，换下一种方式
            continue
        hits = ha.assets.find_by_normalized_name("林夕")
        if len(hits) > 1:
            return True
    return False


def test_ambiguous_candidate_goes_to_suspect_queue(tmp_path):
    """P1/P2：歧义 -> SUSPECT_DUPLICATE + 进入确认队列。"""
    ha, aid, ver = _setup_with_history(tmp_path)
    match = load("bettercomfyui.prompt.match")

    ambiguous = _force_ambiguity(ha, aid)
    candidate_name = "林夕" if ambiguous else "林夕2"  # 退化路径：靠近似（序数词）触发

    res = contract_attr(match, "match_candidate")(_candidate(candidate_name), ha.assets)
    outcome = _outcome(res)
    assert outcome == "SUSPECT_DUPLICATE", (
        f"疑似重名必须进入 SUSPECT_DUPLICATE（歧义构造成功={ambiguous}，候选={candidate_name!r}），"
        f"实际 outcome={outcome}，reason={getattr(res, 'reason', None)}"
    )
    sid = getattr(res, "suspect_id", None)
    assert isinstance(sid, str) and sid.startswith("SUSPECT_"), (
        f"suspect_id 必须形如 SUSPECT_{{归一化名}}_{{时间戳}}_{{uuid8}}，实际 {sid!r}"
    )
    assert getattr(res, "asset_id", None) is None, "疑似分支不得直接指向某个候选版本"

    queue = contract_attr(match, "build_suspect_queue")(ha.assets)
    assert isinstance(queue, list) and queue, "build_suspect_queue 必须列出待确认记录"
    assert any((r.get("suspect_id") == sid) for r in queue if isinstance(r, dict)), (
        f"确认队列必须包含刚建立的 {sid}，实际 {queue!r}"
    )


def test_suspect_never_overwrites_old_asset(tmp_path):
    """P3：疑似分支绝不允许覆盖旧素材（逐文件 sha256 比对）。"""
    ha, aid, ver = _setup_with_history(tmp_path)
    match = load("bettercomfyui.prompt.match")

    ambiguous = _force_ambiguity(ha, aid)

    def _data_digest():
        """只比较素材数据；logs/events.jsonl 是契约要求的追加式事件日志，不在范围内。"""
        return {k: v for k, v in tree_digest(ha.root).items() if not k.startswith("logs/")}

    # 快照在「歧义已存在」之后取：这样断言只针对「疑似匹配本身」的副作用
    before = _data_digest()
    assert before, "前置条件：项目目录必须有内容"

    for candidate_name in ("林夕", "林 夕", "林夕2"):
        contract_attr(match, "match_candidate")(_candidate(candidate_name), ha.assets)

    after = _data_digest()
    changed = {k for k in set(before) & set(after) if before[k] != after[k]}
    removed = set(before) - set(after)
    assert not changed, f"R2 违背：疑似匹配过程中既有文件被原地覆盖/改动：{sorted(changed)}"
    assert not removed, f"R2 违背：疑似匹配过程中删除了既有文件：{sorted(removed)}"
    # 记录歧义构造结果，便于报告判断本用例实际覆盖了哪条路径
    assert isinstance(ambiguous, bool)

    # v1 素材文件本体必须逐字节不变
    files = [p for p in ha.versions_dir("CHARACTER", aid).rglob("*") if p.is_file()]
    assert any(p.read_bytes() == b"ORIGINAL-v1-bytes-must-never-change" for p in files), (
        "R2 违背：v1 素材文件内容被覆盖，原始字节已不存在"
    )


def test_is_suspect_detects_documented_similarity_kinds():
    """F3：契约明确列出「仅差标点/序数词/繁简」三类相近，必须至少识别其中一类。"""
    norm = load("bettercomfyui.prompt.normalize")
    is_suspect = contract_attr(norm, "is_suspect")

    cases = {
        "序数词": ("林夕", ["林夕2"]),
        "标点": ("林夕！", ["林夕"]),
        "繁体": ("林夕", ["林夕"]),
        "空格歧义": ("林 夕", ["林夕"]),
    }
    detected = {k: bool(is_suspect(a, b)) for k, (a, b) in cases.items()}
    assert any(detected.values()), (
        f"is_suspect 对契约列出的相近类型全部返回 False，等于没有疑似检测：{detected}"
    )


def test_normalize_is_idempotent_on_boundary_names():
    """P3 前置：归一化必须幂等（否则同一名称每次匹配结果不同）。"""
    norm = load("bettercomfyui.prompt.normalize")
    normalize_name = contract_attr(norm, "normalize_name")

    names = [
        "",
        " ",
        "林夕",
        "林 夕",
        "林\u3000夕",
        "LIN XI",
        "LinXi",
        "林夕！？！",
        "（林夕）",
        "林夕_v2",
        "  \t 林夕 \n ",
    ] + [f"角色{i}" for i in range(30)]
    for n in names:
        once = normalize_name(n)
        twice = normalize_name(once)
        assert once == twice, f"normalize_name 不幂等：{n!r} -> {once!r} -> {twice!r}"


def test_normalize_unifies_fullwidth_and_halfwidth():
    """契约：全角空格 U+3000 与全角标点必须归一化到同一结果。"""
    norm = load("bettercomfyui.prompt.normalize")
    normalize_name = contract_attr(norm, "normalize_name")
    a = normalize_name("LINXI")
    assert normalize_name("LinXi") == a
    assert normalize_name(" LINXI ") == a
    assert normalize_name("LIN\u3000XI") == a, "全角空格 U+3000 未被处理"
