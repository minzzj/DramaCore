"""``store/inject.py``：R1 —— 程序生成「已有素材清单」注入文本。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_store_support import add_bytes_version, make_asset_store  # noqa: E402

from bettercomfyui.store import AssetStore, build_existing_asset_manifest  # noqa: E402
from bettercomfyui.store.errors import StoreError  # noqa: E402


def create(store: AssetStore, name: str, asset_type: str = "CHARACTER", scope: str = "PROJECT"):
    return store.create_asset(
        asset_type=asset_type, name=name, source="AI", library_scope=scope
    )


def test_empty_library_returns_empty_string(tmp_path):
    _, store = make_asset_store(tmp_path)
    assert build_existing_asset_manifest(store) == ""


def test_manifest_has_four_contract_columns(tmp_path):
    _, store = make_asset_store(tmp_path)
    asset = create(store, "张三")
    add_bytes_version(store, asset["asset_id"], b"v1")

    text = build_existing_asset_manifest(store)
    lines = text.splitlines()
    assert lines[0].startswith("[已有素材清单")
    assert "权威来源" in lines[0]

    columns = lines[1].split("\t")
    assert columns[0] == "CHARACTER"     # 类型
    assert columns[1] == "01"            # 序号
    assert columns[2] == "张三"          # 名称
    assert columns[3] == "UNLOCKED"      # 锁定状态


def test_locked_version_is_reported_with_source(tmp_path):
    paths, store = make_asset_store(tmp_path)
    asset = create(store, "张三")
    version = add_bytes_version(store, asset["asset_id"], b"v1")
    store.set_lock(
        asset["asset_id"],
        "v1",
        lock_source="HUMAN_PASS",
        content_hash=version["content_hash"],
        review_cycle_id=f"asset:{asset['asset_id']}:v1",
    )
    text = build_existing_asset_manifest(store)
    assert "LOCKED v1 HUMAN_PASS" in text


def test_sequence_number_is_per_type_and_stable(tmp_path):
    _, store = make_asset_store(tmp_path)
    create(store, "甲", "CHARACTER")
    create(store, "乙", "CHARACTER")
    create(store, "咖啡馆", "SCENE")

    rows = build_existing_asset_manifest(store).splitlines()[1:]
    seqs = {row.split("\t")[0]: [] for row in rows}
    for row in rows:
        assert row.split("\t")[1] in {"01", "02"}
        seqs[row.split("\t")[0]].append(row.split("\t")[1])
    assert sorted(seqs["CHARACTER"]) == ["01", "02"]
    assert seqs["SCENE"] == ["01"]


def test_public_library_is_explicitly_tagged(tmp_path):
    _, store = make_asset_store(tmp_path)
    create(store, "公共场景", "SCENE", "PUBLIC")
    create(store, "项目场景", "SCENE", "PROJECT")

    text = build_existing_asset_manifest(store)
    assert "[PUBLIC]" in text, "R5：公共库必须显式标注"
    lines = text.splitlines()
    public_line = next(line for line in lines if "公共场景" in line)
    project_line = next(line for line in lines if "项目场景" in line)
    assert public_line.endswith("[PUBLIC]")
    assert not project_line.endswith("[PUBLIC]")


def test_scope_filter_excludes_public(tmp_path):
    _, store = make_asset_store(tmp_path)
    create(store, "公共场景", "SCENE", "PUBLIC")
    create(store, "项目场景", "SCENE", "PROJECT")

    project_only = build_existing_asset_manifest(store, library_scope="PROJECT")
    assert "项目场景" in project_only
    assert "公共场景" not in project_only

    public_only = build_existing_asset_manifest(store, library_scope="PUBLIC")
    assert "公共场景" in public_only
    assert "项目场景" not in public_only


def test_manifest_is_deterministic(tmp_path):
    _, store = make_asset_store(tmp_path)
    for name in ("甲", "乙", "丙"):
        create(store, name)
    first = build_existing_asset_manifest(store)
    for _ in range(5):
        assert build_existing_asset_manifest(store) == first


def test_manifest_is_project_scoped(tmp_path):
    _, first_store = make_asset_store(tmp_path / "one", "甲项目")
    _, second_store = make_asset_store(tmp_path / "two", "乙项目")
    create(first_store, "甲项目角色")
    create(second_store, "乙项目角色")

    first_text = build_existing_asset_manifest(first_store)
    second_text = build_existing_asset_manifest(second_store)
    assert "甲项目角色" in first_text and "乙项目角色" not in first_text
    assert "乙项目角色" in second_text and "甲项目角色" not in second_text


def test_invalid_scope_rejected(tmp_path):
    _, store = make_asset_store(tmp_path)
    with pytest.raises(StoreError):
        build_existing_asset_manifest(store, library_scope="EVERYWHERE")
