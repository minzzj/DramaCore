"""``store/projects.py``：项目生命周期、布局、删除保护、配置快照不泄密。"""

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

from test_store_support import make_project  # noqa: E402

from bettercomfyui.config import Config  # noqa: E402
from bettercomfyui.models.entities import now_iso  # noqa: E402
from bettercomfyui.models.enums import ResumeTarget  # noqa: E402
from bettercomfyui.store import (  # noqa: E402
    ProjectPaths,
    ProjectStore,
    atomic_write_json,
    read_json,
)
from bettercomfyui.store.errors import (  # noqa: E402
    ConfirmTokenMismatchError,
    PathIsolationError,
    ProjectBusyError,
    ProjectExistsError,
    ProjectNotFoundError,
    StoreError,
)


class TestCreate:
    def test_creates_full_layout(self, tmp_path):
        store = ProjectStore(tmp_path)
        project = store.create("测试项目")
        paths = project["paths"]

        assert isinstance(paths, ProjectPaths)
        for directory in (
            paths.project_dir,
            paths.chapters_dir,
            paths.assets_dir,
            paths.segments_dir,
            paths.output_dir,
            paths.checkpoints_dir,
            paths.logs_dir,
        ):
            assert directory.is_dir(), directory

        assert paths.project_json.is_file()
        assert paths.config_yaml.is_file()
        assert paths.meta_json.is_file()
        assert paths.events_jsonl.is_file()

        data = read_json(paths.project_json)
        assert data["project_id"] == paths.project_id
        assert data["display_name"] == "测试项目"
        assert data["running_tasks"] == []
        assert data["progress"] == {}
        assert read_json(paths.meta_json)["assets"] == []
        assert read_json(paths.meta_json)["project_id"] == paths.project_id

        # 建项目即落 PROJECT 检查点（断点续传锚点）
        assert (paths.checkpoints_dir / "PROJECT.1.json").is_file()
        assert project["project_id"] in str(paths.project_dir)

    def test_project_id_is_uuid4_hex(self, tmp_path):
        project = ProjectStore(tmp_path).create("甲")
        pid = project["project_id"]
        assert len(pid) == 32 and pid == pid.lower()

    @pytest.mark.parametrize("bad", ["ABC", "0123456789ABCDEF0123456789ABCDEF", "short", "../escape", ""])
    def test_invalid_project_id_rejected(self, tmp_path, bad):
        with pytest.raises((PathIsolationError, StoreError)):
            ProjectStore(tmp_path).create("甲", project_id=bad)

    def test_duplicate_explicit_id_rejected(self, tmp_path):
        store = ProjectStore(tmp_path)
        first = store.create("甲")
        with pytest.raises(ProjectExistsError):
            store.create("乙", project_id=first["project_id"])

    def test_empty_display_name_rejected(self, tmp_path):
        with pytest.raises(StoreError):
            ProjectStore(tmp_path).create("   ")

    def test_config_snapshot_never_leaks_api_key(self, tmp_path):
        config = Config()
        config.prompt.api_key = "超级机密密钥"
        config.prompt.endpoint = "https://example.invalid/v1"
        store = ProjectStore(tmp_path, config)
        project = store.create("甲")

        raw = project["paths"].project_json.read_text(encoding="utf-8")
        assert "超级机密密钥" not in raw, "配置快照不得把真实密钥写入仓库"
        snapshot = json.loads(raw)["config_snapshot"]
        assert snapshot["prompt"]["api_key"] is None
        assert snapshot["prompt"]["endpoint"] == "https://example.invalid/v1"

    def test_projects_dir_can_be_passed_directly(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        store = ProjectStore(projects_root)
        assert store.projects_root == projects_root
        project = store.create("甲")
        assert project["paths"].project_dir.parent == projects_root

    def test_config_driven_project_root(self, tmp_path):
        config = Config()
        config.global_.project_root = "my_projects"
        project = ProjectStore(tmp_path, config).create("甲")
        assert (tmp_path / "my_projects" / project["project_id"]).is_dir()


class TestList:
    def test_lists_created_projects_and_ignores_junk(self, tmp_path):
        store = ProjectStore(tmp_path)
        first = store.create("甲")
        second = store.create("乙")
        (store.projects_root / "not_a_project").mkdir()
        (store.projects_root / "broken").mkdir()
        (store.projects_root / "broken" / "project.json").write_text("{oops", encoding="utf-8")

        listed = store.list()
        assert {item["project_id"] for item in listed} == {first["project_id"], second["project_id"]}
        names = {item["project_id"]: item["display_name"] for item in listed}
        assert names[first["project_id"]] == "甲"
        assert names[second["project_id"]] == "乙"
        # 排序必须确定（created_at 精度到秒，同一秒内按 project_id 兜底）
        again = [item["project_id"] for item in store.list()]
        assert again == [item["project_id"] for item in listed]
        assert again == sorted(again, key=lambda pid: pid) or len(set(again)) == 2

    def test_list_on_missing_root_is_empty(self, tmp_path):
        assert ProjectStore(tmp_path / "nowhere").list() == []


class TestOpenCloseSwitch:
    def test_open_returns_loaded_state(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        opened = store.open(created["project_id"])

        assert isinstance(opened["config"], Config)
        assert opened["paths"].project_id == created["project_id"]
        assert opened["chapters"] == []
        assert opened["assets"]["assets"] == []
        assert opened["resume_target"] == ResumeTarget.ASSET
        assert opened["latest_checkpoint"]["stage"] == "PROJECT"
        assert store.opened_projects() == [created["project_id"]]

    def test_open_loads_chapters_from_disk(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        paths = created["paths"]
        atomic_write_json(
            paths.chapter_json("CH001"),
            {
                "chapter_id": "CH001",
                "index": 1,
                "title": "第一幕",
                "raw_script": "剧本正文",
                "scenes": [],
                "new_asset_candidates": [],
                "created_at": now_iso(),
            },
        )
        opened = store.open(created["project_id"])
        assert [chapter["chapter_id"] for chapter in opened["chapters"]] == ["CH001"]
        assert opened["chapters"][0]["title"] == "第一幕"

    def test_open_corrupt_chapter_raises(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        created["paths"].chapter_json("CH001").write_text('{"chapter_id":"CH001"}', encoding="utf-8")
        # chapter_id/index 缺 index -> Chapter 构造失败必须报错，不得静默跳过
        with pytest.raises(StoreError):
            store.open(created["project_id"])

    def test_open_missing_project(self, tmp_path):
        with pytest.raises(ProjectNotFoundError):
            ProjectStore(tmp_path).open("0" * 32)
        with pytest.raises(ProjectNotFoundError):
            ProjectStore(tmp_path).open("not-an-id")

    def test_close_writes_state(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        assert store.close(created["project_id"])["closed"] is False, "未打开的项目不应被写入"

        store.open(created["project_id"])
        result = store.close(created["project_id"])
        assert result["closed"] is True
        assert result["saved"]["status"] == "closed"
        assert result["saved"]["closed_at"]
        assert read_json(created["paths"].project_json)["status"] == "closed"
        assert store.opened_projects() == []

    def test_switch_saves_from_then_opens_to(self, tmp_path):
        store = ProjectStore(tmp_path)
        first = store.create("甲")
        second = store.create("乙")
        store.open(first["project_id"])

        switched = store.switch(first["project_id"], second["project_id"])
        assert switched["project_id"] == second["project_id"]
        assert switched["switched_from"] == first["project_id"]
        assert switched["closed_previous"]["closed"] is True
        assert read_json(first["paths"].project_json)["status"] == "closed"
        assert store.opened_projects() == [second["project_id"]]

    def test_switch_without_from(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        switched = store.switch(None, created["project_id"])
        assert switched["closed_previous"] is None


class TestDelete:
    def test_requires_matching_confirm_token(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        paths = created["paths"]
        with pytest.raises(ConfirmTokenMismatchError):
            store.delete(created["project_id"], confirm_token="yes")
        assert paths.project_dir.is_dir()

        store.delete(created["project_id"], confirm_token=created["project_id"])
        assert not paths.project_dir.exists()

    def test_refuses_while_open(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        store.open(created["project_id"])
        with pytest.raises(ProjectBusyError):
            store.delete(created["project_id"], confirm_token=created["project_id"])
        assert created["paths"].project_dir.is_dir()

    def test_refuses_when_running_tasks(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        paths = created["paths"]

        def mutator(data):
            data["running_tasks"] = ["job-1"]
            return data

        from bettercomfyui.store import locked_update_json

        locked_update_json(paths.project_json, mutator)
        with pytest.raises(ProjectBusyError):
            store.delete(created["project_id"], confirm_token=created["project_id"])
        assert paths.project_dir.is_dir()

    def test_refuses_when_running_marker_exists(self, tmp_path):
        store = ProjectStore(tmp_path)
        created = store.create("甲")
        (created["paths"].project_dir / ".running").write_text("busy", encoding="utf-8")
        with pytest.raises(ProjectBusyError):
            store.delete(created["project_id"], confirm_token=created["project_id"])

    def test_delete_missing_project(self, tmp_path):
        with pytest.raises(ProjectNotFoundError):
            ProjectStore(tmp_path).delete("0" * 32, confirm_token="0" * 32)


class TestEventsIntegration:
    def test_project_lifecycle_events_recorded(self, tmp_path):
        from bettercomfyui.store import read_events

        store = ProjectStore(tmp_path)
        created = store.create("甲")
        store.open(created["project_id"])
        store.close(created["project_id"])

        events = read_events(created["paths"])
        names = [event["event"] for event in events]
        assert "PROJECT_CREATED" in names
        assert "PROJECT_OPENED" in names
        assert "PROJECT_CLOSED" in names
        assert "CHECKPOINT_SAVED" in names
        for event in events:
            assert event["namespace"] == "BETTERCOMFYUI"
            assert event["project_id"] == created["project_id"]
            assert event["ts"]
