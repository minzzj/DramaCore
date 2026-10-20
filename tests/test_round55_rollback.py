# -*- coding: utf-8 -*-
"""ROUND55-7 素材版本回滚模块测试（成功 / 哈希拒绝 / 目标不存在 / 日志 / list / CLI）。

只使用临时目录构造 fake 项目 + 素材库；不联网、不用 GPU。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_store_support import add_bytes_version, make_project  # noqa: E402

from bettercomfyui.store import AssetStore  # noqa: E402
from bettercomfyui.store.errors import (  # noqa: E402
    AssetNotFoundError,
    ContentHashMismatchError,
    VersionNotFoundError,
)
from bettercomfyui.store.rollback import (  # noqa: E402
    RollbackError,
    list_asset_versions,
    main,
    rollback_asset,
)


def _fixture(tmp_path: Path, *, name: str = "张三"):
    """返回 (paths, store, asset, v1, v2)；v1/v2 未上锁。"""
    proj = make_project(tmp_path, "回滚测试项目")
    paths = proj["paths"]
    store = AssetStore(paths)
    asset = store.create_asset(
        asset_type="CHARACTER", name=name, source="AI", library_scope="PROJECT"
    )
    aid = asset["asset_id"]
    v1 = add_bytes_version(store, aid, b"v1-bytes", extra={"score": 88.5})
    v2 = add_bytes_version(store, aid, b"v2-bytes-different", extra={"score": 91.0})
    return paths, store, asset, v1, v2


def _lock(store: AssetStore, asset_id: str, version: dict, source: str, cycle: str) -> None:
    store.set_lock(
        asset_id,
        version["version"],
        lock_source=source,
        content_hash=version["content_hash"],
        review_cycle_id=cycle,
    )


def _log_lines(paths) -> list[dict]:
    log = paths.project_dir / "_work" / "rollback_log.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestListAssetVersions:
    def test_lists_all_fields_and_lock_flags(self, tmp_path):
        paths, store, asset, v1, v2 = _fixture(tmp_path)
        _lock(store, asset["asset_id"], v2, "MODEL_PASS", "cycle-model-2")

        rows = list_asset_versions(paths.project_id, asset["asset_id"], root=paths.root)

        assert [r["version"] for r in rows] == ["v1", "v2"]
        by_version = {r["version"]: r for r in rows}
        assert by_version["v1"]["lock_status"] == "UNLOCKED"
        assert by_version["v1"]["locked"] is False
        assert by_version["v2"]["locked"] is True
        assert by_version["v2"]["is_current_lock"] is True
        assert by_version["v2"]["lock_source"] == "MODEL_PASS"
        assert by_version["v2"]["score"] == 91.0
        for row in rows:
            assert row["file_exists"] is True
            assert row["hash_ok"] is True
            assert row["hash_actual"] == row["hash_declared"]
            assert row["asset_type"] == "CHARACTER"
            assert row["name"] == "张三"
        # 只读：list 不得写索引 revision
        rev = store.load_index()["revision"]
        list_asset_versions(paths.project_id, None, root=paths.root)
        assert store.load_index()["revision"] == rev

    def test_list_whole_project_and_by_name(self, tmp_path):
        paths, store, asset, _v1, _v2 = _fixture(tmp_path)
        rows_all = list_asset_versions(paths.project_id, None, root=paths.root)
        assert len(rows_all) == 2
        rows_by_name = list_asset_versions(paths.project_id, "张三", root=paths.root)
        assert len(rows_by_name) == 2
        with pytest.raises(AssetNotFoundError):
            list_asset_versions(paths.project_id, "不存在的人", root=paths.root)


class TestRollbackSuccess:
    def test_rollback_to_older_locked_version(self, tmp_path):
        paths, store, asset, v1, v2 = _fixture(tmp_path)
        aid = asset["asset_id"]
        _lock(store, aid, v1, "HUMAN_PASS", "cycle-human-1")
        _lock(store, aid, v2, "MODEL_PASS", "cycle-model-2")
        assert store.latest_locked_version(aid) == "v2"
        versions_before = len(store.get_asset(aid)["versions"])
        v1_file = paths.absolute_from_relative(v1["file_path"])
        v2_file = paths.absolute_from_relative(v2["file_path"])

        result = rollback_asset(
            paths.project_id, aid, "v1", operator="tester", reason="旧版更稳", root=paths.root
        )

        assert result["ok"] is True and result["code"] == "OK"
        assert result["from_version"] == "v2" and result["target_version"] == "v1"
        assert result["locks_after"] == ["v1"]
        assert result["cleared_locks"] == ["v2"]
        # LOCK 状态：v1 保持 LOCK，v2 被 clear（留痕、不删除）
        assert store.get_version(aid, "v1")["lock_status"] == "LOCKED"
        assert store.get_version(aid, "v2")["lock_status"] == "UNLOCKED"
        assert store.get_version(aid, "v2")["extra"]["unlock_reason"].startswith("rollback->v1")
        assert store.latest_locked_version(aid) == "v1"
        # 不删除历史版本/文件
        assert len(store.get_asset(aid)["versions"]) == versions_before
        assert v1_file.is_file() and v2_file.is_file()
        assert result["audit"]["historical_versions_deleted"] == 0
        # 旧结论失效审计
        audit = result["audit"]
        assert audit["old_conclusions_invalidated"] is True
        assert audit["requires_regeneration"] is True
        assert audit["requires_reference_remount"] is True
        assert audit["next_actions"] and isinstance(audit["next_actions"], list)
        # 日志
        lines = _log_lines(paths)
        assert len(lines) == 1
        rec = lines[0]
        assert rec["status"] == "ok"
        assert rec["from_version"] == "v2" and rec["to_version"] == "v1"
        assert rec["operator"] == "tester" and rec["reason"] == "旧版更稳"
        assert rec["content_hash"] == v1["content_hash"]
        assert rec["cleared_locks"] == ["v2"]

    def test_rollback_is_idempotent_when_target_already_current(self, tmp_path):
        paths, store, asset, v1, _v2 = _fixture(tmp_path)
        aid = asset["asset_id"]
        _lock(store, aid, v1, "HUMAN_PASS", "cycle-human-1")
        result = rollback_asset(paths.project_id, aid, "v1", root=paths.root)
        assert result["ok"] is True
        assert result["changed"] is False
        assert store.latest_locked_version(aid) == "v1"


class TestRollbackRejections:
    def test_hash_mismatch_rejected_and_logged(self, tmp_path):
        paths, store, asset, v1, v2 = _fixture(tmp_path)
        aid = asset["asset_id"]
        _lock(store, aid, v1, "HUMAN_PASS", "cycle-human-1")
        _lock(store, aid, v2, "MODEL_PASS", "cycle-model-2")
        # 篡改 v1 文件：LOCK 声明的字节已不存在
        paths.absolute_from_relative(v1["file_path"]).write_bytes(b"tampered-v1")

        with pytest.raises(ContentHashMismatchError):
            rollback_asset(paths.project_id, aid, "v1", root=paths.root)

        # 拒绝不得改变 LOCK 状态（v2 仍是当前 LOCK）
        assert store.latest_locked_version(aid) == "v2"
        assert store.get_version(aid, "v1")["lock_status"] == "LOCKED"
        lines = _log_lines(paths)
        assert len(lines) == 1 and lines[0]["status"] == "rejected"
        assert lines[0]["error_code"] == "CONTENT_HASH_MISMATCH"
        assert lines[0]["declared_hash"] == v1["content_hash"]

    def test_target_version_missing(self, tmp_path):
        paths, store, asset, _v1, _v2 = _fixture(tmp_path)
        with pytest.raises(VersionNotFoundError):
            rollback_asset(paths.project_id, asset["asset_id"], "v9", root=paths.root)
        lines = _log_lines(paths)
        assert lines and lines[-1]["status"] == "rejected"
        assert lines[-1]["error_code"] == "TARGET_VERSION_NOT_FOUND"

    def test_target_exists_but_file_missing(self, tmp_path):
        paths, store, asset, v1, _v2 = _fixture(tmp_path)
        paths.absolute_from_relative(v1["file_path"]).unlink()
        with pytest.raises(RollbackError) as excinfo:
            rollback_asset(paths.project_id, asset["asset_id"], "v1", root=paths.root)
        assert excinfo.value.rollback_code == "TARGET_FILE_MISSING"

    def test_unlocked_target_requires_explicit_revalidate(self, tmp_path):
        paths, store, asset, v1, _v2 = _fixture(tmp_path)
        aid = asset["asset_id"]
        with pytest.raises(RollbackError) as excinfo:
            rollback_asset(
                paths.project_id, aid, "v1", revalidate_lock=False, root=paths.root
            )
        assert excinfo.value.rollback_code == "TARGET_VERSION_NOT_LOCKED"
        assert _log_lines(paths)[-1]["error_code"] == "TARGET_VERSION_NOT_LOCKED"

        # 显式 revalidate_lock=True 才会重新 LOCK，并留审计
        result = rollback_asset(
            paths.project_id, aid, "v1", operator="admin", reason="重新授权", root=paths.root
        )
        assert result["relocked"] is True
        row = store.get_version(aid, "v1")
        assert row["lock_status"] == "LOCKED"
        assert row["lock_source"] == "HUMAN_PASS"
        assert row["review_cycle_id"].startswith("rollback:admin:")
        assert store.latest_locked_version(aid) == "v1"


class TestLogAppend:
    def test_two_rollbacks_append_two_lines(self, tmp_path):
        paths, store, asset, v1, v2 = _fixture(tmp_path)
        aid = asset["asset_id"]
        _lock(store, aid, v1, "HUMAN_PASS", "cycle-human-1")
        _lock(store, aid, v2, "MODEL_PASS", "cycle-model-2")
        rollback_asset(paths.project_id, aid, "v1", operator="a", root=paths.root)
        # 重新 LOCK v2（模拟新审核周期），再回滚到 v2
        store.set_lock(aid, "v2", lock_source="MODEL_PASS",
                       content_hash=v2["content_hash"], review_cycle_id="cycle-model-3")
        rollback_asset(paths.project_id, aid, "v2", operator="b", root=paths.root)
        lines = _log_lines(paths)
        assert [x["status"] for x in lines] == ["ok", "ok"]
        assert [x["operator"] for x in lines] == ["a", "b"]
        assert lines[0]["to_version"] == "v1" and lines[1]["to_version"] == "v2"


class TestCli:
    def test_cli_list_and_apply(self, tmp_path, capsys):
        paths, store, asset, v1, v2 = _fixture(tmp_path)
        aid = asset["asset_id"]
        _lock(store, aid, v1, "HUMAN_PASS", "cycle-human-1")
        _lock(store, aid, v2, "MODEL_PASS", "cycle-model-2")

        rc = main(["--project-id", paths.project_id, "--list", "--root", str(paths.root)])
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out)
        assert payload["ok"] is True and len(payload["versions"]) == 2

        rc = main([
            "--project-id", paths.project_id, "--asset", aid, "--target-version", "v1",
            "--operator", "cli-user", "--reason", "cli 回滚", "--root", str(paths.root),
        ])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is True and result["target_version"] == "v1"
        assert result["audit"]["old_conclusions_invalidated"] is True

    def test_cli_rejected_returns_2(self, tmp_path, capsys):
        paths, store, asset, _v1, _v2 = _fixture(tmp_path)
        rc = main([
            "--project-id", paths.project_id, "--asset", asset["asset_id"],
            "--target-version", "v9", "--root", str(paths.root),
        ])
        assert rc == 2
        err = json.loads(capsys.readouterr().err)
        assert err["ok"] is False and err["code"] == "VERSION_NOT_FOUND"
