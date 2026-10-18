"""``prompt/match.py`` 测试：三分支确定性匹配 + R1/R5 红线。

覆盖：
* 已存在 -> EXISTS_REUSE（不生成）；不存在 -> NEW_GENERATE；歧义/相近 -> SUSPECT_DUPLICATE
* 歧义时**绝不覆盖**：断言 ``meta.json`` 字节完全未变
* 疑似记录不静默丢弃：store 缺少落盘钩子必须报错
* 纯程序逻辑：match 模块不得 import 任何模型网关

说明：本文件用 ``tests/_fake_store.py``（契约替身）。替身与真实 store 的
一致性由 ``tests/test_prompt_store_integration.py`` 用真实 store 断言。
"""

from __future__ import annotations

import ast
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pytest

from _fake_store import make_store, make_two_projects
from bettercomfyui.models.enums import MatchOutcome
from bettercomfyui.prompt.errors import PromptDependencyError, SuspectPersistenceError
from bettercomfyui.prompt.match import (
    MatchResult,
    build_suspect_queue,
    match_candidate,
    select_reusable_version,
)

MATCH_SRC = (_ROOT / "bettercomfyui" / "prompt" / "match.py").read_text(encoding="utf-8")


def test_match_result_fields_are_frozen_by_contract():
    assert MatchResult._fields == ("outcome", "asset_id", "version", "suspect_id", "reason")


# ---------------------------------------------------------------------------
# 分支一：精确匹配 -> 复用
# ---------------------------------------------------------------------------


def test_exists_reuse_when_unique_match(tmp_path):
    store = make_store(tmp_path)
    asset = store.seed_locked_asset("林澈")
    before = store.meta_bytes()

    result = match_candidate({"name": "林澈", "asset_type": "CHARACTER"}, store)

    assert result.outcome is MatchOutcome.EXISTS_REUSE
    assert result.asset_id == asset["asset_id"]
    assert result.version == "v1"
    assert result.suspect_id is None
    assert "exact_match:project" in result.reason
    assert "locked" in result.reason
    assert store.meta_bytes() == before, "复用分支不得改动素材库"


@pytest.mark.parametrize(
    "variant",
    ["ironman", "IRON  MAN", "Iron\u3000Man", " Iron Man ", "Iron-Man", "iron man。"],
)
def test_exists_reuse_is_normalization_based(tmp_path, variant):
    store = make_store(tmp_path)
    store.seed_locked_asset("Iron Man")
    result = match_candidate({"name": variant}, store)
    assert result.outcome is MatchOutcome.EXISTS_REUSE, variant


def test_exists_reuse_prefers_locked_version(tmp_path):
    store = make_store(tmp_path)
    asset = store.seed_locked_asset("林澈", versions=1, lock=True)
    v2 = store.add_version(asset["asset_id"])  # v2 未锁定

    result = match_candidate({"name": "林澈"}, store)

    assert result.outcome is MatchOutcome.EXISTS_REUSE
    assert result.version == "v1", "应优先选择 resolve_lock 通过的版本（R3）"
    assert store.get_version(asset["asset_id"], v2["version"]) is not None
    assert store.resolve_lock(asset["asset_id"], "v1") is not None
    assert store.resolve_lock(asset["asset_id"], "v2") is None


def test_exists_reuse_marks_unlocked(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("林澈", lock=False)

    result = match_candidate({"name": "林澈"}, store)

    assert result.outcome is MatchOutcome.EXISTS_REUSE
    assert result.version == "v1"
    assert "unlocked" in result.reason, "未锁定必须显式标注，不能伪装成已锁定"


def test_public_library_reuse_is_explicitly_marked(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("公共角色", scope="PUBLIC")

    result = match_candidate({"name": "公共角色", "library_scope": "PROJECT"}, store)

    assert result.outcome is MatchOutcome.EXISTS_REUSE
    assert "scope=PUBLIC" in result.reason
    assert "public" in result.reason


def test_project_isolation_no_cross_project_reuse(tmp_path):
    a, b = make_two_projects(tmp_path)
    a.seed_locked_asset("林澈")

    result_b = match_candidate({"name": "林澈"}, b)

    assert result_b.outcome is MatchOutcome.NEW_GENERATE, "R5：项目之间不得串用素材"
    assert b.list_assets() == []


# ---------------------------------------------------------------------------
# 分支二：确定不存在 -> 生成
# ---------------------------------------------------------------------------


def test_new_generate_on_empty_library(tmp_path):
    store = make_store(tmp_path)
    before = store.meta_bytes()

    result = match_candidate({"name": "苏晚", "asset_type": "CHARACTER"}, store)

    assert result.outcome is MatchOutcome.NEW_GENERATE
    assert (result.asset_id, result.version, result.suspect_id) == (None, None, None)
    assert store.meta_bytes() == before
    assert build_suspect_queue(store) == []


def test_new_generate_when_name_unrelated_to_existing(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("林澈")
    store.seed_locked_asset("雪夜长街", asset_type="SCENE")

    result = match_candidate({"name": "苏晚"}, store)

    assert result.outcome is MatchOutcome.NEW_GENERATE
    assert store.list_suspects() == []


# ---------------------------------------------------------------------------
# 分支三：疑似重复 -> 待确认队列（绝不覆盖、绝不丢弃）
# ---------------------------------------------------------------------------


def test_ambiguous_multiple_assets_becomes_suspect(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("林澈")
    store.seed_locked_asset("林 澈")  # 索引键相同的第二个条目 -> 歧义
    before = store.meta_bytes()

    result = match_candidate({"name": "林澈", "asset_type": "CHARACTER"}, store)

    assert result.outcome is MatchOutcome.SUSPECT_DUPLICATE
    assert result.version is None, "疑似重复绝不给出可复用版本"
    assert result.suspect_id and result.suspect_id.startswith("SUSPECT_")
    assert "ambiguous_multiple_assets:2" in result.reason
    assert store.meta_bytes() == before, "歧义时绝不覆盖：meta.json 字节必须完全未变"

    queue = build_suspect_queue(store)
    assert len(queue) == 1
    assert queue[0]["suspect_id"] == result.suspect_id
    assert len(queue[0]["candidates"]) == 2


def test_punctuation_only_variant_is_exact_match_not_suspect(tmp_path):
    """store 侧索引会忽略标点：只差标点的名称属于精确匹配（复用，不新建不覆盖）。"""
    store = make_store(tmp_path)
    asset = store.seed_locked_asset("林澈")
    before = store.meta_bytes()

    result = match_candidate({"name": "林澈。"}, store)

    assert result.outcome is MatchOutcome.EXISTS_REUSE
    assert result.asset_id == asset["asset_id"]
    assert store.list_suspects() == []
    assert store.meta_bytes() == before


def test_traditional_variant_becomes_suspect(tmp_path):
    """繁简差异：store 索引不折叠，程序判定为高度相近 -> 待人工确认。"""
    store = make_store(tmp_path)
    asset = store.seed_locked_asset("钢铁侠", asset_type="CHARACTER")
    before = store.meta_bytes()

    result = match_candidate({"name": "鋼鐵俠"}, store)

    assert result.outcome is MatchOutcome.SUSPECT_DUPLICATE
    assert "suspect_similar_name" in result.reason
    assert result.asset_id == asset["asset_id"], "唯一相近候选应记录在案便于人工确认"
    assert result.version is None
    assert store.meta_bytes() == before


def test_asset_type_is_derived_from_conflicting_candidate(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("钢铁侠", asset_type="PROP")

    result = match_candidate({"name": "鋼鐵俠"}, store)  # 未提供 asset_type

    record = build_suspect_queue(store)[0]
    assert record["candidate_type"] == "PROP"
    assert record["suspect_id"] == result.suspect_id


def test_suffix_variant_becomes_suspect(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("铁血战士", asset_type="CHARACTER")

    result = match_candidate({"name": "铁血战士2"}, store)

    assert result.outcome is MatchOutcome.SUSPECT_DUPLICATE


def test_suspect_requires_asset_type_and_never_drops_silently(tmp_path):
    """冲突候选没有 asset_type 时：显式报错，绝不静默丢弃疑似记录。"""
    store = make_store(tmp_path)
    store.seed_locked_asset("钢铁侠", asset_type="CHARACTER")

    class NoTypeStore:
        """模拟 list_assets 不返回 asset_type 的退化实现。"""

        def __init__(self, inner):
            self._inner = inner

        def list_assets(self, *, asset_type=None, scope=None):
            return [
                {k: v for k, v in item.items() if k != "asset_type"}
                for item in self._inner.list_assets(asset_type=asset_type, scope=scope)
            ]

        def __getattr__(self, item):
            return getattr(self._inner, item)

    with pytest.raises(PromptDependencyError) as excinfo:
        match_candidate({"name": "鋼鐵俠"}, NoTypeStore(store))
    assert "asset_type" in str(excinfo.value)
    assert build_suspect_queue(store) == []


def test_suspect_record_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("钢铁侠")

    first = match_candidate({"name": "鋼鐵俠"}, store)
    second = match_candidate({"name": "鋼鐵俠 "}, store)  # 归一化后同一候选

    # 复用既有疑似记录时，结论必须仍然是 SUSPECT_DUPLICATE 且不给版本
    assert second.outcome is MatchOutcome.SUSPECT_DUPLICATE
    assert second.version is None
    assert first.suspect_id == second.suspect_id
    assert "reused_existing_suspect" in second.reason
    assert len(build_suspect_queue(store)) == 1, "重跑不得产生重复队列（Lead 硬要求）"


def test_suspect_queue_excludes_resolved(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("钢铁侠")
    result = match_candidate({"name": "鋼鐵俠"}, store)
    store.resolve_suspect(result.suspect_id, resolution="reuse")

    assert build_suspect_queue(store) == []
    assert len(build_suspect_queue(store, resolved=None)) == 1
    assert build_suspect_queue(store, resolved=True)[0]["resolution"] == "reuse"


def test_missing_record_suspect_hook_raises_instead_of_dropping(tmp_path):
    store = make_store(tmp_path)
    store.seed_locked_asset("钢铁侠")

    class NoRecorder:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            if item == "record_suspect":
                raise AttributeError(item)
            return getattr(self._inner, item)

    with pytest.raises(SuspectPersistenceError) as excinfo:
        match_candidate({"name": "鋼鐵俠"}, NoRecorder(store))
    assert "record_suspect" in str(excinfo.value)
    assert build_suspect_queue(store) == [], "未落盘即视为失败，绝不静默丢弃"


def test_suspect_with_injected_id_factory_and_clock(tmp_path):
    from datetime import datetime, timezone

    store = make_store(tmp_path)
    store.seed_locked_asset("钢铁侠")
    fixed = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    result = match_candidate(
        {"name": "鋼鐵俠", "asset_type": "CHARACTER"},
        store,
        id_factory=lambda normalized_name: f"SUSPECT_{normalized_name}_fixed",
        clock=lambda: fixed,
    )

    assert result.suspect_id == "SUSPECT_鋼鐵俠_fixed"
    assert build_suspect_queue(store)[0]["created_at"] == fixed.isoformat()


def test_invalid_candidate_is_rejected(tmp_path):
    store = make_store(tmp_path)
    for bad in ({}, {"name": ""}, {"name": "   "}, {"name": 123}, "林澈"):
        with pytest.raises(Exception):
            match_candidate(bad, store)  # type: ignore[arg-type]


def test_invalid_library_scope_is_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(Exception):
        match_candidate({"name": "林澈", "library_scope": "GLOBAL"}, store)


# ---------------------------------------------------------------------------
# 纯程序逻辑（R1）：不得引入 AI/网络
# ---------------------------------------------------------------------------


def test_match_module_never_imports_ai_gateway_or_network():
    tree = ast.parse(MATCH_SRC)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"socket", "urllib.request", "requests", "httpx", "openai"}
    assert not (imported & forbidden), imported & forbidden
    assert not any("gateway" in name for name in imported), "R1：匹配不得调用模型网关"


def test_match_candidate_never_uses_the_network(tmp_path, monkeypatch):
    import urllib.request

    def _boom(*args, **kwargs):  # pragma: no cover - 只有出错才会走到
        raise AssertionError("match_candidate 不得发起网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    store = make_store(tmp_path)
    store.seed_locked_asset("林澈")
    assert match_candidate({"name": "林澈"}, store).outcome is MatchOutcome.EXISTS_REUSE
    assert match_candidate({"name": "苏晚"}, store).outcome is MatchOutcome.NEW_GENERATE


def test_select_reusable_version_reports_no_version(tmp_path):
    store = make_store(tmp_path)
    asset = store.create_asset(asset_type="PROP", name="空素材", source="AI", library_scope="PROJECT")
    version, note = select_reusable_version(store, asset)
    assert version is None and note == "no_version"
