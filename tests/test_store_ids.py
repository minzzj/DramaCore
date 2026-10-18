"""``store/ids.py``：契约第 2 节逐条格式比对 + R6 短码冲突重试。"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bettercomfyui.models.enums import AssetType, CounterKind  # noqa: E402
from bettercomfyui.store import ids  # noqa: E402
from bettercomfyui.store.errors import InvalidIdentifierError, StoreError  # noqa: E402

PROJECT_ID = "0123456789abcdef0123456789abcdef"
ASSET_ID = "甲项目_character_1_1234abcd"


class TestProjectAndTokens:
    def test_new_project_id_is_32_lower_hex_and_unique(self):
        seen = {ids.new_project_id() for _ in range(500)}
        assert len(seen) == 500
        for value in seen:
            assert re.fullmatch(r"[0-9a-f]{32}", value)
            assert ids.is_valid_project_id(value)

    @pytest.mark.parametrize(
        "bad",
        ["", "ABC", "0123456789ABCDEF0123456789ABCDEF", "0123456789abcdef0123456789abcde", "0" * 33, None, 123],
    )
    def test_invalid_project_id(self, bad):
        assert ids.is_valid_project_id(bad) is False

    def test_finalization_token(self):
        token = ids.new_finalization_token()
        assert re.fullmatch(r"[0-9a-f]{32}", token)
        assert token != ids.new_finalization_token()


class TestVersion:
    def test_next_version_accepts_int_and_str(self):
        assert ids.next_version(1) == "v2"
        assert ids.next_version("v1") == "v2"
        assert ids.next_version("3") == "v4"

    def test_parse_version(self):
        assert ids.parse_version("v7") == 7
        assert ids.parse_version(7) == 7
        assert ids.parse_version("7") == 7

    @pytest.mark.parametrize("bad", ["v0", "0", 0, -1, "1.5", "v", "", None, True, [], "1e3"])
    def test_bad_version_rejected(self, bad):
        with pytest.raises(InvalidIdentifierError):
            ids.parse_version(bad)

    def test_version_sort_key_numeric(self):
        items = ["v10", "v2", "v1"]
        assert sorted(items, key=ids.version_sort_key) == ["v1", "v2", "v10"]


class TestContentHash:
    def test_matches_hashlib_and_streams_large_file(self, tmp_path):
        payload = b"x" * (3 * 1024 * 1024 + 17)  # 跨多个 1MB 分块
        target = tmp_path / "big.bin"
        target.write_bytes(payload)
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert ids.content_hash_of(target) == expected

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(StoreError):
            ids.content_hash_of(tmp_path / "nope.bin")

    def test_parse_content_hash_strict(self):
        good = "sha256:" + "a" * 64
        assert ids.parse_content_hash(good) == good
        assert ids.is_valid_content_hash(good)

    @pytest.mark.parametrize(
        "bad",
        [
            "sha256:" + "A" * 64,          # 大写 hex 不合规
            "sha256:" + "a" * 63,
            "md5:" + "a" * 32,
            "a" * 64,
            "",
            None,
        ],
    )
    def test_parse_content_hash_rejections(self, bad):
        with pytest.raises(InvalidIdentifierError):
            ids.parse_content_hash(bad)
        assert ids.is_valid_content_hash(bad) is False


class TestComposedIds:
    def test_lock_id(self):
        assert ids.lock_id(ASSET_ID, "v2") == f"LOCK_{ASSET_ID}_v2"
        assert ids.lock_id(ASSET_ID, 2) == f"LOCK_{ASSET_ID}_v2"

    def test_new_asset_id_format(self):
        value = ids.new_asset_id("甲项目", AssetType.CHARACTER, 1)
        assert re.fullmatch(r"甲项目_character_1_[0-9a-f]{8}", value), value
        also = ids.new_asset_id("甲项目", "SCENE", 12)
        assert re.fullmatch(r"甲项目_scene_12_[0-9a-f]{8}", also), also

    def test_new_asset_id_unsafe_chars_escaped(self):
        value = ids.new_asset_id("a/b:c*d", "CHARACTER", 1)
        assert "/" not in value and ":" not in value and "*" not in value

    def test_new_asset_id_rejects_bad_input(self):
        with pytest.raises(StoreError):
            ids.new_asset_id("p", "NOT_A_TYPE", 1)
        for bad_seq in (0, -1, "1", True, None):
            with pytest.raises(InvalidIdentifierError):
                ids.new_asset_id("p", "CHARACTER", bad_seq)

    def test_asset_id_r6_retries_on_collision(self, monkeypatch):
        real = ids._uuid8
        monkeypatch.setattr(ids, "_uuid8", lambda: "deadbeef")
        colliding = ids.new_asset_id("甲", "CHARACTER", 1)  # 不加 existing -> 先拿到会冲突的那个
        assert colliding.endswith("deadbeef")

        calls = {"n": 0}

        def sequence():
            calls["n"] += 1
            return "deadbeef" if calls["n"] == 1 else real()

        monkeypatch.setattr(ids, "_uuid8", sequence)
        fresh = ids.new_asset_id("甲", "CHARACTER", 1, existing={colliding})
        assert fresh != colliding, "R6 必须在真实索引里查重并重新生成"

    def test_unique_short_code_exhaustion_raises(self):
        with pytest.raises(StoreError):
            ids.unique_short_code(lambda: "same", {"same"}, max_attempts=3)

    def test_short_code_conflict_forms(self):
        assert ids.short_code_conflict("a", {"a", "b"}) is True
        assert ids.short_code_conflict("c", {"a", "b"}) is False
        assert ids.short_code_conflict("a", lambda code: code == "a") is True
        assert ids.short_code_conflict("a", None) is False
        with pytest.raises(InvalidIdentifierError):
            ids.short_code_conflict("a", "abc")  # 字符串会被误当字符集合

    def test_new_suspect_id_format(self):
        value = ids.new_suspect_id("张三", timestamp="20260101120000")
        assert re.fullmatch(r"SUSPECT_张三_20260101120000_[0-9a-f]{8}", value), value

    def test_chapter_id(self):
        assert ids.chapter_id(1) == "CH001"
        assert ids.chapter_id(999) == "CH999"
        for bad in (0, 1000, "1", None):
            with pytest.raises(InvalidIdentifierError):
                ids.chapter_id(bad)

    def test_parse_chapter_index_accepts_three_forms(self):
        assert ids.parse_chapter_index(1) == 1
        assert ids.parse_chapter_index("1") == 1
        assert ids.parse_chapter_index("CH001") == 1
        assert ids.parse_chapter_index("ch7") == 7
        for bad in ("CH000", "abc", 0, 1000, None):
            with pytest.raises(InvalidIdentifierError):
                ids.parse_chapter_index(bad)

    def test_segment_id_same_for_int_and_chapter_string(self):
        first = ids.segment_id(PROJECT_ID, 1, 2)
        second = ids.segment_id(PROJECT_ID, "CH001", 2)
        assert first == second == f"{PROJECT_ID}_CH1_SEG2"

    def test_segment_id_rejects_bad_input(self):
        with pytest.raises(InvalidIdentifierError):
            ids.segment_id("not-a-project-id", 1, 1)
        with pytest.raises(InvalidIdentifierError):
            ids.segment_id(PROJECT_ID, 1, 0)

    def test_attempt_and_audit_id(self):
        segment = ids.segment_id(PROJECT_ID, 1, 2)
        assert ids.attempt_id(segment, "v3", 4) == f"ATTEMPT_{segment}_v3_4"
        assert ids.audit_id(segment, 3) == f"AUDIT_{segment}_v3"
        with pytest.raises(InvalidIdentifierError):
            ids.attempt_id(segment, "v3", 0)

    def test_review_cycle_id(self):
        assert ids.review_cycle_id("asset", ASSET_ID, "v1") == f"asset:{ASSET_ID}:v1"
        with pytest.raises(InvalidIdentifierError):
            ids.review_cycle_id("asset", "bad:id", "v1")

    def test_mod_and_cascade_id(self):
        assert ids.mod_id("asset", ASSET_ID, uuid8="1234abcd") == f"MOD_asset_{ASSET_ID}_1234abcd"
        assert re.fullmatch(r"MOD_asset_1_[0-9a-f]{8}", ids.mod_id("asset", "1"))
        with pytest.raises(InvalidIdentifierError):
            ids.mod_id("asset", "1", uuid8="XYZ")
        cascade = ids.new_cascade_id("seg1")
        assert re.fullmatch(r"CASCADE_seg1_[0-9a-f]{8}", cascade)

    def test_cnt_id(self):
        assert ids.cnt_id("任务甲", CounterKind.CNT1) == "CNT_任务甲_CNT1"
        assert ids.cnt_id("任务甲", "CNT3") == "CNT_任务甲_CNT3"
        with pytest.raises(StoreError):
            ids.cnt_id("任务甲", "CNT9")

    def test_arb_and_abort_id(self):
        assert ids.arb_id("H1", "M1") == "ARB_H1_M1"
        assert ids.abort_id("H1") == "ABORT_H1"
        with pytest.raises(InvalidIdentifierError):
            ids.arb_id("H 1", "M1")

    def test_idempotency_key_deterministic(self):
        first = ids.idempotency_key("prompt", {"b": 1, "a": [1, 2]})
        second = ids.idempotency_key("prompt", {"a": [1, 2], "b": 1})
        assert first == second
        assert first.startswith("prompt:")
        assert re.fullmatch(r"prompt:[0-9a-f]{64}", first)
        assert ids.idempotency_key("prompt", {"a": [1, 3]}) != first


class TestSafeToken:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a/b:c*d", "a_b_c_d"),
            ("  空格 名 ", "空格_名"),
            ("已有_横线", "已有_横线"),
            ("张三", "张三"),
        ],
    )
    def test_safe_token(self, raw, expected):
        assert ids.safe_token(raw, kind="test") == expected

    def test_safe_token_fallback_and_error(self):
        assert ids.safe_token(None, kind="test", fallback="anon") == "anon"
        with pytest.raises(InvalidIdentifierError):
            ids.safe_token("   ", kind="test")

    def test_generated_ids_are_filesystem_safe(self):
        values = [
            ids.new_asset_id("p", "CHARACTER", 1),
            ids.new_suspect_id("张三", timestamp="20260101120000"),
            ids.new_cascade_id("seg"),
            ids.mod_id("asset", "1"),
        ]
        for value in values:
            assert not any(ch in value for ch in '<>:"/\\|?*')
