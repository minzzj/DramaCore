"""``store/checkpoints.py``：只追加、最新检查点、G/P/R 恢复目标（自描述）。"""

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

from test_store_support import make_project, spawn_child  # noqa: E402

from bettercomfyui.models.enums import ResumeTarget, Stage  # noqa: E402
from bettercomfyui.store import (  # noqa: E402
    STAGE_ORDER,
    STAGE_TO_RESUME_TARGET,
    CheckpointStore,
    CorruptFileError,
    atomic_write_json,
    read_json,
    read_events,
)
from bettercomfyui.store.errors import StoreError  # noqa: E402


class TestSaveAppendOnly:
    def test_seq_increments_and_old_files_untouched(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        checkpoints = CheckpointStore(paths)

        first = checkpoints.save(Stage.ASSET_GENERATE, {"n": 1})
        first_bytes = first.read_bytes()
        second = checkpoints.save(Stage.ASSET_GENERATE, {"n": 2})
        third = checkpoints.save(Stage.ASSET_GENERATE, {"n": 3})

        assert [item.name for item in (first, second, third)] == [
            "ASSET_GENERATE.1.json",
            "ASSET_GENERATE.2.json",
            "ASSET_GENERATE.3.json",
        ]
        assert first.read_bytes() == first_bytes, "检查点只追加，历史文件不得被改写"

        data = read_json(third)
        assert data["seq"] == 3
        assert data["stage"] == "ASSET_GENERATE"
        assert data["payload"] == {"n": 3}
        assert data["stage_index"] == STAGE_ORDER.index("ASSET_GENERATE")
        assert data["resume_target"] == "G"  # 自描述：当时算出的恢复目标
        assert data["project_id"] == paths.project_id
        assert data["saved_at"]

    def test_payload_must_be_dict(self, tmp_path):
        checkpoints = CheckpointStore(make_project(tmp_path)["paths"])
        with pytest.raises(StoreError):
            checkpoints.save(Stage.SPLICE, ["not", "a", "dict"])

    def test_invalid_stage_rejected(self, tmp_path):
        checkpoints = CheckpointStore(make_project(tmp_path)["paths"])
        with pytest.raises(StoreError):
            checkpoints.save("NOT_A_STAGE", {})

    def test_checkpoint_event_recorded(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        CheckpointStore(paths).save(Stage.SPLICE, {"ok": True})
        events = [event for event in read_events(paths) if event["event"] == "CHECKPOINT_SAVED"]
        assert events and events[-1]["stage"] == "SPLICE"

    def test_lock_file_name_is_not_double_dotted(self, tmp_path):
        """锁文件必须恰好是 ``.checkpoints.lock``（曾误用 lock_path_for 生成 ``..x.lock.lock``）。"""
        paths = make_project(tmp_path)["paths"]
        CheckpointStore(paths).save(Stage.SPLICE, {})
        lock_files = sorted(item.name for item in paths.checkpoints_dir.glob("*lock*"))
        assert lock_files == [".checkpoints.lock"], lock_files
        assert not list(paths.checkpoints_dir.glob("..*")), "不得出现双点文件名"


class TestLatest:
    def test_latest_returns_newest_stage(self, tmp_path):
        checkpoints = CheckpointStore(make_project(tmp_path)["paths"])
        checkpoints.save(Stage.ASSET_REVIEW, {"stage": "asset"})
        checkpoints.save(Stage.DIRECTOR_PLAN, {"stage": "plan"})
        checkpoints.save(Stage.SEGMENT_GENERATE, {"stage": "seg"})

        latest = checkpoints.latest()
        assert latest["stage"] == "SEGMENT_GENERATE"
        assert latest["payload"] == {"stage": "seg"}

    def test_latest_at_or_before_filters_by_stage(self, tmp_path):
        checkpoints = CheckpointStore(make_project(tmp_path)["paths"])
        checkpoints.save(Stage.ASSET_REVIEW, {"stage": "asset"})
        checkpoints.save(Stage.SEGMENT_GENERATE, {"stage": "seg"})

        latest = checkpoints.latest(at_or_before=Stage.DIRECTOR_PLAN)
        assert latest["stage"] == "ASSET_REVIEW"

    def test_same_stage_higher_seq_wins(self, tmp_path):
        checkpoints = CheckpointStore(make_project(tmp_path)["paths"])
        checkpoints.save(Stage.CASCADE, {"n": 1})
        checkpoints.save(Stage.CASCADE, {"n": 2})
        assert checkpoints.latest()["payload"] == {"n": 2}

    def test_no_checkpoints_returns_none(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        for item in paths.checkpoints_dir.glob("*.json"):
            item.unlink()
        assert CheckpointStore(paths).latest() is None

    def test_corrupt_checkpoint_without_stage_raises(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        (paths.checkpoints_dir / "SPLICE.9.json").write_text('{"payload":{}}', encoding="utf-8")
        with pytest.raises(CorruptFileError):
            CheckpointStore(paths).latest()

    def test_list_and_load_payload(self, tmp_path):
        checkpoints = CheckpointStore(make_project(tmp_path)["paths"])
        checkpoints.save(Stage.SPLICE, {"a": 1})
        checkpoints.save(Stage.OUTPUT, {"b": 2})
        listed = checkpoints.list_checkpoints()
        assert [item["stage"] for item in listed] == ["PROJECT", "SPLICE", "OUTPUT"]
        assert checkpoints.load_payload(stage=Stage.SPLICE) == {"a": 1}


class TestResumeTarget:
    def test_mapping_covers_every_stage(self):
        assert set(STAGE_TO_RESUME_TARGET) == set(Stage)
        assert STAGE_TO_RESUME_TARGET[Stage.PROJECT] is ResumeTarget.ASSET
        assert STAGE_TO_RESUME_TARGET[Stage.ASSET_REVIEW] is ResumeTarget.ASSET
        assert STAGE_TO_RESUME_TARGET[Stage.DIRECTOR_PLAN] is ResumeTarget.DIRECTOR
        assert STAGE_TO_RESUME_TARGET[Stage.SEGMENT_GENERATE] is ResumeTarget.DIRECTOR
        assert STAGE_TO_RESUME_TARGET[Stage.SEGMENT_REVIEW] is ResumeTarget.REVIEW
        assert STAGE_TO_RESUME_TARGET[Stage.OUTPUT] is ResumeTarget.REVIEW

    def test_empty_project_resumes_from_asset_stage(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        for item in paths.checkpoints_dir.glob("*.json"):
            item.unlink()
        assert CheckpointStore(paths).resolve_resume_target() is ResumeTarget.ASSET

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            (Stage.ASSET_REVIEW, ResumeTarget.ASSET),
            (Stage.SEGMENT_GENERATE, ResumeTarget.DIRECTOR),
            (Stage.SEGMENT_REVIEW, ResumeTarget.REVIEW),
            (Stage.CASCADE, ResumeTarget.REVIEW),
            (Stage.OUTPUT, ResumeTarget.REVIEW),
        ],
    )
    def test_resume_target_follows_saved_stage(self, tmp_path, stage, expected):
        paths = make_project(tmp_path)["paths"]
        checkpoints = CheckpointStore(paths)
        checkpoints.save(stage, {"stage": stage.value})
        assert checkpoints.resolve_resume_target() is expected
        # 与落盘的自描述字段一致（以后改映射表也不会改变历史语义）
        assert checkpoints.latest()["resume_target"] == expected.value

    def test_stored_resume_target_wins_over_mapping_table(self, tmp_path):
        """自描述优先：历史检查点里存档的恢复目标不得被后来的映射表改动影响。"""
        paths = make_project(tmp_path)["paths"]
        target = paths.checkpoint_json(Stage.ASSET_REVIEW, 7)
        atomic_write_json(
            target,
            {
                "schema_version": 1,
                "project_id": paths.project_id,
                "stage": "ASSET_REVIEW",
                "stage_index": STAGE_ORDER.index("ASSET_REVIEW"),
                "seq": 7,
                "saved_at": "2026-10-16T22:00:00+08:00",
                "resume_target": "P",
                "payload": {},
            },
        )
        assert CheckpointStore(paths).resolve_resume_target() is ResumeTarget.DIRECTOR

    def test_illegal_stored_target_falls_back_to_table(self, tmp_path):
        paths = make_project(tmp_path)["paths"]
        atomic_write_json(
            paths.checkpoint_json(Stage.SEGMENT_REVIEW, 1),
            {
                "stage": "SEGMENT_REVIEW",
                "seq": 1,
                "saved_at": "2026-10-16T22:00:00+08:00",
                "resume_target": "Z",
                "payload": {},
            },
        )
        assert CheckpointStore(paths).resolve_resume_target() is ResumeTarget.REVIEW


_WORKER_SAVE = """
import sys
from pathlib import Path
from bettercomfyui.store import CheckpointStore
from bettercomfyui.models.enums import Stage

paths_dir = Path(sys.argv[1]); tag = sys.argv[2]; count = int(sys.argv[3])
store = CheckpointStore(paths_dir)
for index in range(count):
    store.save(Stage.CASCADE, {"tag": tag, "index": index})
"""


def test_two_processes_saving_checkpoints_never_collide(tmp_path):
    """并发保存同一阶段的检查点：seq 必须唯一，且每个文件都是完整的。"""
    paths = make_project(tmp_path)["paths"]
    procs = [spawn_child(_WORKER_SAVE, str(paths.project_dir), tag, "8") for tag in ("A", "B")]
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, f"{out}\n{err}"

    files = sorted(paths.checkpoints_dir.glob("CASCADE.*.json"))
    # PROJECT.1 之外，两个进程各写了 8 个 CASCADE
    assert len(files) == 16, [item.name for item in files]
    seqs = set()
    for item in files:
        data = json.loads(item.read_text(encoding="utf-8"))
        seqs.add(data["seq"])
        assert data["payload"]["tag"] in {"A", "B"}
    assert len(seqs) == 16, f"seq 冲突：{sorted(seqs)}"
