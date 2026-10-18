"""``store/assets.py``：只追加（R2）、LOCK 绑定版本（R3）、项目隔离（R5）。

每条红线都有对应的「破坏实现就会红」的断言，例如：

* R2：加 v2/v3 后 v1 的文件字节、大小、mtime 必须完全不变；
  版本目录已存在时必须抛 :class:`ImmutableAssetError`；
* R3：v1 上锁后 v2 的 ``resolve_lock`` 必须是 ``None``；磁盘被篡改时
  ``set_lock`` 必须抛 :class:`ContentHashMismatchError`；
* R5：两个项目同名素材互不可见；meta.json 的 project_id 不符必须抛错。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_store_support import add_bytes_version, make_asset_store, make_project  # noqa: E402

from bettercomfyui.models.entities import AssetEntry, AssetVersion  # noqa: E402
from bettercomfyui.store import AssetStore  # noqa: E402
from bettercomfyui.store.errors import (  # noqa: E402
    AssetNotFoundError,
    ContentHashMismatchError,
    CorruptFileError,
    ImmutableAssetError,
    LockAlreadyExistsError,
    StoreError,
    VersionNotFoundError,
)


def snapshot(path: Path) -> tuple:
    stat = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns)


def make_asset(store: AssetStore, *, name: str = "张三", asset_type: str = "CHARACTER"):
    return store.create_asset(
        asset_type=asset_type, name=name, source="AI", library_scope="PROJECT"
    )


class TestCreateAsset:
    def test_layout_and_index(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset = make_asset(store, name="张三")

        asset_dir = paths.asset_dir("CHARACTER", asset["asset_id"])
        assert asset_dir.is_dir()
        assert (asset_dir / "asset.json").is_file()
        assert (asset_dir / "versions").is_dir()

        index = store.load_index()
        assert index["project_id"] == paths.project_id
        assert index["schema_version"] == 1
        assert len(index["assets"]) == 1
        assert index["assets"][0]["normalized_name"] == "张三"

        # 落盘的条目必须能被 models 层的 AssetEntry 反序列化（同一套 schema）
        entry = AssetEntry.from_dict(index["assets"][0])
        assert entry.asset_id == asset["asset_id"]
        assert entry.versions == []

    def test_asset_id_contains_seq_and_type(self, tmp_path):
        _, store = make_asset_store(tmp_path, "甲项目")
        first = make_asset(store, name="甲")
        second = make_asset(store, name="乙")
        assert "_1_" in first["asset_id"] and first["asset_id"].startswith("甲项目_character_1_")
        assert "_2_" in second["asset_id"]

    def test_invalid_type_rejected(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        with pytest.raises(StoreError):
            store.create_asset(
                asset_type="NOT_A_TYPE", name="x", source="AI", library_scope="PROJECT"
            )
        with pytest.raises(StoreError):
            store.create_asset(
                asset_type="CHARACTER", name="  ", source="AI", library_scope="PROJECT"
            )

    def test_asset_json_matches_index_entry(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset = make_asset(store)
        on_disk = json.loads(
            paths.asset_json("CHARACTER", asset["asset_id"]).read_text(encoding="utf-8")
        )
        assert on_disk == asset


class TestAddVersionAppendOnly:
    def test_three_versions_and_v1_untouched(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset = make_asset(store)
        asset_id = asset["asset_id"]

        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        v1_file = paths.absolute_from_relative(v1["file_path"])
        before = snapshot(v1_file)

        v2 = add_bytes_version(store, asset_id, b"v2-bytes-different")
        v3 = add_bytes_version(store, asset_id, b"v3-bytes-different-again")

        assert (v1["version"], v2["version"], v3["version"]) == ("v1", "v2", "v3")
        assert v1_file.is_file(), "R2：v1 文件必须仍然存在"
        assert snapshot(v1_file) == before, "R2：v1 文件字节/大小/mtime 不得被改动"

        index = store.load_index()
        versions = index["assets"][0]["versions"]
        assert [item["version"] for item in versions] == ["v1", "v2", "v3"]
        assert versions[0]["content_hash"] == v1["content_hash"], "R2：v1 指纹不得变化"
        assert index["assets"][0]["current_version"] == "v3"
        # 旧版本只记录「被谁取代」，状态与文件保留
        assert versions[0]["superseded_by"] == "v2"
        assert versions[1]["superseded_by"] == "v3"
        assert store.get_version(asset_id, "v1")["status"] == "DRAFT"

    def test_each_version_file_hash_matches_disk(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        payloads = [b"one", b"two-two", b"three-three-three"]
        for payload in payloads:
            add_bytes_version(store, asset_id, payload)

        files = store.version_files(asset_id)
        assert len(files) == 3
        for version_file, payload in zip(files, payloads):
            assert version_file.read_bytes() == payload
        index = store.load_index()
        for item in index["assets"][0]["versions"]:
            real = "sha256:" + hashlib.sha256(
                paths.absolute_from_relative(item["file_path"]).read_bytes()
            ).hexdigest()
            assert item["content_hash"] == real

    def test_file_level_guard_refuses_overwrite_even_if_dir_check_bypassed(self, tmp_path):
        """R2 的物理保证：文件级 ``O_EXCL`` 必须独立成立。

        上层（版本目录存在性）检查可能被绕过或将来被重构掉，因此这一层必须单独验证：
        目标文件已存在时拷贝必须失败，且**原内容一个字节都不能变**。
        """
        target = tmp_path / "file.png"
        target.write_bytes(b"original-material")
        source = tmp_path / "source.png"
        source.write_bytes(b"brand-new-material")

        with pytest.raises(ImmutableAssetError):
            AssetStore._copy_new_file(source, target)
        assert target.read_bytes() == b"original-material"

    def test_version_dir_conflict_raises_immutable(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        add_bytes_version(store, asset_id, b"v1")
        # 手工造出 v2 目录（模拟「已存在」），必须拒绝覆盖
        (paths.versions_dir("CHARACTER", asset_id) / "v2").mkdir()
        with pytest.raises(ImmutableAssetError):
            add_bytes_version(store, asset_id, b"v2")
        assert store.get_version(asset_id, "v2") is None

    def test_missing_source_file_rejected(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        with pytest.raises(StoreError):
            store.add_version(
                asset_id, source="AI", file_path=paths.output_dir / "nope.png", status="DRAFT"
            )

    def test_unknown_asset_rejected(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        src = Path(tmp_path) / "src.bin"
        src.write_bytes(b"x")
        with pytest.raises(AssetNotFoundError):
            store.add_version("不存在的素材", source="AI", file_path=src, status="DRAFT")

    def test_invalid_status_or_source_rejected(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        src = Path(tmp_path) / "src.bin"
        src.write_bytes(b"x")
        with pytest.raises(StoreError):
            store.add_version(asset_id, source="AI", file_path=src, status="NOT_A_STATUS")
        with pytest.raises(StoreError):
            store.add_version(asset_id, source="NOT_A_SOURCE", file_path=src, status="DRAFT")


class TestLock:
    def test_lock_lifecycle_and_non_inheritance(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        cycle = f"asset:{asset_id}:v1"

        assert store.resolve_lock(asset_id, "v1") is None, "未上锁不得可用于生成"

        lock = store.set_lock(
            asset_id, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"],
            review_cycle_id=cycle,
        )
        assert lock["lock_status"] == "LOCKED"
        assert lock["status"] == "ALLOWED"
        assert lock["lock_source"] == "HUMAN_PASS"
        assert lock["review_cycle_id"] == cycle
        assert lock["extra"]["lock_id"] == f"LOCK_{asset_id}_v1"

        resolved = store.resolve_lock(asset_id, "v1")
        assert resolved is not None
        assert resolved["content_hash"] == v1["content_hash"]
        assert resolved["lock_source"] == "HUMAN_PASS"
        assert Path(resolved["absolute_path"]).is_file()

        # R3：新版本绝不继承旧 LOCK
        v2 = add_bytes_version(store, asset_id, b"v2-bytes")
        assert v2["lock_status"] == "UNLOCKED" and v2["lock_source"] is None
        assert store.resolve_lock(asset_id, "v2") is None
        assert store.resolve_lock(asset_id, "v1") is not None
        assert store.locked_versions(asset_id) == ["v1"]

        cleared = store.clear_lock(asset_id, "v1", reason="重新审核")
        assert cleared["lock_status"] == "UNLOCKED"
        assert store.resolve_lock(asset_id, "v1") is None

    def test_set_lock_is_idempotent_and_does_not_rewrite(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        cycle = f"asset:{asset_id}:v1"
        kwargs = dict(lock_source="MODEL_PASS", content_hash=v1["content_hash"], review_cycle_id=cycle)

        first = store.set_lock(asset_id, "v1", **kwargs)
        before = paths.meta_json.read_bytes()
        second = store.set_lock(asset_id, "v1", **kwargs)

        assert first["extra"]["locked_at"] == second["extra"]["locked_at"]
        assert paths.meta_json.read_bytes() == before, "重复写同一 LOCK 不得产生第二次写入"

    def test_relock_with_different_conclusion_rejected(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        store.set_lock(
            asset_id, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"],
            review_cycle_id=f"asset:{asset_id}:v1",
        )
        before = paths.meta_json.read_bytes()
        with pytest.raises(LockAlreadyExistsError):
            store.set_lock(
                asset_id, "v1", lock_source="MODEL_PASS", content_hash=v1["content_hash"],
                review_cycle_id="model:late:result",
            )
        assert paths.meta_json.read_bytes() == before, "拒绝时不得留下任何写入"

    def test_set_lock_requires_review_cycle_id(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        with pytest.raises(StoreError) as excinfo:
            store.set_lock(
                asset_id, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"]
            )
        assert "review_cycle_id" in str(excinfo.value)

    def test_set_lock_rejects_hash_mismatch_with_disk(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"original-bytes")
        version_file = paths.absolute_from_relative(v1["file_path"])

        # 篡改磁盘上的素材文件（模拟人工改动 / 外部覆盖）
        version_file.write_bytes(b"tampered-bytes")

        with pytest.raises(ContentHashMismatchError):
            store.set_lock(
                asset_id, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"],
                review_cycle_id=f"asset:{asset_id}:v1",
            )
        assert store.resolve_lock(asset_id, "v1") is None

        with pytest.raises(ContentHashMismatchError):
            store.set_lock(
                asset_id, "v1", lock_source="HUMAN_PASS", content_hash="sha256:" + "0" * 64,
                review_cycle_id=f"asset:{asset_id}:v1",
            )
        with pytest.raises(StoreError):
            store.set_lock(
                asset_id, "v1", lock_source="HUMAN_PASS", content_hash="md5:abc",
                review_cycle_id=f"asset:{asset_id}:v1",
            )

    def test_resolve_lock_none_when_file_missing(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        store.set_lock(
            asset_id, "v1", lock_source="HUMAN_PASS", content_hash=v1["content_hash"],
            review_cycle_id=f"asset:{asset_id}:v1",
        )
        assert store.resolve_lock(asset_id, "v1") is not None
        paths.absolute_from_relative(v1["file_path"]).unlink()
        assert store.resolve_lock(asset_id, "v1") is None, "文件丢了不得usable"

    def test_lock_fallback_source_sets_fallback_status(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1-bytes")
        lock = store.set_lock(
            asset_id, "v1", lock_source="FALLBACK_PASS", content_hash=v1["content_hash"],
            review_cycle_id=f"asset:{asset_id}:v1",
        )
        assert lock["status"] == "FALLBACK"
        assert store.resolve_lock(asset_id, "v1")["lock_source"] == "FALLBACK_PASS"


class TestVersionStatusAndReferences:
    def test_set_version_status_blocks_structural_fields(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        add_bytes_version(store, asset_id, b"v1-bytes")
        original_hash = store.get_version(asset_id, "v1")["content_hash"]
        # version / asset_id 是位置参数，物理上无法被 **fields 覆盖；
        # 其余可经关键字传入的追溯字段必须显式拒绝（否则指纹可被篡改）
        for field in ("content_hash", "file_path"):
            with pytest.raises(StoreError):
                store.set_version_status(asset_id, "v1", "ALLOWED", **{field: "hacked"})
        assert store.get_version(asset_id, "v1")["content_hash"] == original_hash

    def test_set_version_status_keeps_unknown_fields_in_extra(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        add_bytes_version(store, asset_id, b"v1-bytes")
        updated = store.set_version_status(
            asset_id, "v1", "PENDING_REVIEW", score=88.5, reviewer="张三"
        )
        assert updated["status"] == "PENDING_REVIEW"
        assert updated["extra"]["score"] == 88.5
        assert updated["extra"]["reviewer"] == "张三"
        assert updated["content_hash"] == store.get_version(asset_id, "v1")["content_hash"]

    def test_record_reference_is_idempotent(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        add_bytes_version(store, asset_id, b"v1-bytes")
        ref = {"segment_id": "seg-1", "purpose": "first_frame"}
        store.record_reference(asset_id, "v1", ref)
        store.record_reference(asset_id, "v1", ref)
        refs = store.references_of(asset_id)
        assert len(refs) == 1
        assert refs[0]["segment_id"] == "seg-1"
        assert refs[0]["version"] == "v1"
        assert refs[0]["asset_id"] == asset_id

    def test_record_reference_rejects_bad_ref(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        add_bytes_version(store, asset_id, b"v1-bytes")
        with pytest.raises(StoreError):
            store.record_reference(asset_id, "v1", "not-a-dict")
        with pytest.raises(VersionNotFoundError):
            store.record_reference(asset_id, "v9", {"segment_id": "s"})


class TestFindAndList:
    def test_find_by_normalized_name_is_tolerant(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset = make_asset(store, name="张三")
        for query in ("张三", " 张三 ", "　张三　", "A B C"):
            found = store.find_by_normalized_name(query)
            if query == "A B C":
                assert found == []
            else:
                assert [item["asset_id"] for item in found] == [asset["asset_id"]]

    def test_fullwidth_and_case_insensitive_match(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset = make_asset(store, name="ＡＢＣ")
        assert store.find_by_normalized_name("abc")[0]["asset_id"] == asset["asset_id"]
        assert store.find_by_normalized_name("ＡＢＣ")[0]["asset_id"] == asset["asset_id"]

    def test_multiple_matches_returned_for_suspect_decision(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        first = make_asset(store, name="张三")
        second = make_asset(store, name="张 三")
        found = store.find_by_normalized_name("张三")
        assert {item["asset_id"] for item in found} == {first["asset_id"], second["asset_id"]}, (
            "R1：歧义必须返回多个候选，交给程序判 SUSPECT，绝不自动覆盖"
        )

    def test_empty_query_rejected(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        with pytest.raises(StoreError):
            store.find_by_normalized_name("   ")

    def test_list_assets_filters(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        make_asset(store, name="甲", asset_type="CHARACTER")
        make_asset(store, name="乙", asset_type="SCENE")
        store.create_asset(
            asset_type="PROP", name="公共道具", source="MANUAL", library_scope="PUBLIC"
        )
        assert len(store.list_assets()) == 3
        assert len(store.list_assets(asset_type="SCENE")) == 1
        assert len(store.list_assets(scope="PUBLIC")) == 1
        assert len(store.list_assets(asset_type="CHARACTER", scope="PROJECT")) == 1
        with pytest.raises(StoreError):
            store.list_assets(asset_type="NOPE")


class TestProjectIsolation:
    def test_two_projects_same_asset_name_are_invisible_to_each_other(self, tmp_path):
        first_paths, first_store = make_asset_store(tmp_path / "one", "甲项目")
        second_paths, second_store = make_asset_store(tmp_path / "two", "乙项目")
        assert first_paths.project_id != second_paths.project_id

        first_asset = make_asset(first_store, name="张三")
        second_asset = make_asset(second_store, name="张三")

        assert first_asset["asset_id"] != second_asset["asset_id"]
        assert [item["asset_id"] for item in first_store.list_assets()] == [first_asset["asset_id"]]
        assert [item["asset_id"] for item in second_store.list_assets()] == [
            second_asset["asset_id"]
        ]
        assert first_store.get_asset(second_asset["asset_id"]) is None
        assert second_store.get_asset(first_asset["asset_id"]) is None
        assert first_store.find_by_normalized_name("张三")[0]["asset_id"] == first_asset["asset_id"]

        # 每个项目的文件都在自己的 project_id 目录下（R5）
        for paths in (first_paths, second_paths):
            for item in sorted(paths.project_dir.rglob("*")):
                if item.is_file():
                    assert paths.project_id in str(item)

    def test_index_with_foreign_project_id_is_rejected(self, tmp_path):
        first_paths, first_store = make_asset_store(tmp_path / "one", "甲项目")
        second_paths, _ = make_asset_store(tmp_path / "two", "乙项目")
        make_asset(first_store)

        # 人为把别的项目的 meta.json 放到本项目目录 -> 必须报错，绝不串用
        first_paths.meta_json.write_bytes(second_paths.meta_json.read_bytes())
        with pytest.raises(CorruptFileError):
            first_store.load_index()

    def test_corrupt_index_is_not_silently_treated_as_empty(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        make_asset(store)
        paths.meta_json.write_bytes(b"{}")
        with pytest.raises(CorruptFileError):
            store.load_index()

    def test_asset_store_accepts_project_dir_path(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        store = AssetStore(paths.project_dir)
        assert store.project_id == paths.project_id
        assert store.list_assets() == []


class TestSuspectQueue:
    def test_suspects_are_stored_outside_meta_json(self, tmp_path):
        paths, store = make_asset_store(tmp_path)
        asset = make_asset(store, name="张三")
        add_bytes_version(store, asset["asset_id"], b"v1")

        meta_before = paths.meta_json.read_bytes()
        record = store.record_suspect(
            {
                "name": "张 三",
                "normalized_name": "张三",
                "asset_type": "CHARACTER",
                "library_scope": "PROJECT",
                "candidates": [{"asset_id": asset["asset_id"], "version": "v1"}],
                "reason": "仅空格差异",
            }
        )
        assert paths.meta_json.read_bytes() == meta_before, (
            "SUSPECT 记录不得改动 meta.json 的字节（prompt 层据此断言不覆盖）"
        )
        assert paths.suspects_json.is_file()
        assert record["suspect_id"].startswith("SUSPECT_")
        assert record["candidate_details"][0]["asset_id"] == asset["asset_id"]
        assert record["candidates"] == [asset["asset_id"]]

        pending = store.list_suspects(resolved=False)
        assert [item["suspect_id"] for item in pending] == [record["suspect_id"]]
        resolved = store.resolve_suspect(record["suspect_id"], resolution="reuse")
        assert resolved["resolved"] is True
        assert store.list_suspects(resolved=False) == []
        assert len(store.list_suspects(resolved=True)) == 1

    def test_record_suspect_keyword_form_and_auto_id(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        record = store.record_suspect(
            name="张三",
            normalized_name="张三",
            asset_type="CHARACTER",
            candidates=[],
            reason="首次出现即疑似",
        )
        assert record["suspect_id"].startswith("SUSPECT_张三_")
        assert store.suspect_ids() == {record["suspect_id"]}

    def test_record_suspect_requires_type(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        with pytest.raises(StoreError):
            store.record_suspect(normalized_name="张三", name="张三")

    def test_resolve_unknown_suspect_rejected(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        with pytest.raises(StoreError):
            store.resolve_suspect("SUSPECT_none", resolution="reuse")
        with pytest.raises(StoreError):
            store.resolve_suspect("SUSPECT_none", resolution="whatever")


class TestIndexSchemaParity:
    def test_version_entry_round_trips_through_models(self, tmp_path):
        _, store = make_asset_store(tmp_path)
        asset_id = make_asset(store)["asset_id"]
        v1 = add_bytes_version(store, asset_id, b"v1")
        entry = store.load_index()["assets"][0]
        parsed = AssetEntry.from_dict(entry)
        version = parsed.get_version("v1")
        assert isinstance(version, AssetVersion)
        assert version.content_hash == v1["content_hash"]
        assert version.file_path == v1["file_path"]
        assert version.lock_status.value == "UNLOCKED"
        assert parsed.usable_version() is None, "未审核素材不得 usable（R3 前置）"
