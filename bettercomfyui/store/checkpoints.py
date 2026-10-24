"""阶段检查点 —— docs/CONTRACTS.md 第 4 节 + ``ResumeTarget`` 恢复语义。

* ``save`` **只追加**：``checkpoints/<stage>.<seq>.json``，seq 单调递增，目标已存在
  一律拒绝（物理上不可能覆盖历史检查点）；
* seq 分配在文件锁内完成，多进程并发保存不会拿到同一个 seq；
* 每个检查点**自描述**：写入当时的 ``resume_target`` 与 ``stage_index``，
  ``resolve_resume_target()`` 优先读检查点里存档的值 —— 这样以后调整映射表
  **不会改变历史检查点的恢复语义**（Lead 裁决 task-2 #2）。

Stage -> ResumeTarget 映射（Lead 已批准）：

============ =====================================================
G（素材）     PROJECT, SCRIPT_PARSE, ASSET_MATCH, ASSET_GENERATE, ASSET_REVIEW
P（导演台）   DIRECTOR_PLAN, SEGMENT_GENERATE
R（审核）     SEGMENT_REVIEW, CASCADE, SPLICE, FULL_REVIEW, OUTPUT
============ =====================================================

无检查点 -> ``ResumeTarget.ASSET``（G，最保守：从头做素材阶段）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from ..models.entities import now_iso
from ..models.enums import ResumeTarget, Stage
from .atomic import atomic_write_json, read_json
from .errors import CorruptFileError, ImmutableAssetError, StoreError, parse_enum
from .events import append_event
from .lockfile import file_lock
from .paths import ProjectPaths, coerce_project_paths

__all__ = [
    "CheckpointStore",
    "STAGE_ORDER",
    "STAGE_TO_RESUME_TARGET",
    "CHECKPOINT_SCHEMA_VERSION",
]

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA_VERSION = 1

#: 阶段推进顺序（直接取 models.enums.Stage 的声明顺序）
STAGE_ORDER: tuple[str, ...] = tuple(stage.value for stage in Stage)

#: Stage -> 恢复时从哪个子流程重新进入（G/P/R）
STAGE_TO_RESUME_TARGET: dict[Stage, ResumeTarget] = {
    Stage.PROJECT: ResumeTarget.ASSET,
    Stage.SCRIPT_PARSE: ResumeTarget.ASSET,
    Stage.ASSET_MATCH: ResumeTarget.ASSET,
    Stage.ASSET_GENERATE: ResumeTarget.ASSET,
    Stage.ASSET_REVIEW: ResumeTarget.ASSET,
    Stage.DIRECTOR_PLAN: ResumeTarget.DIRECTOR,
    Stage.SEGMENT_GENERATE: ResumeTarget.DIRECTOR,
    Stage.SEGMENT_REVIEW: ResumeTarget.REVIEW,
    Stage.CASCADE: ResumeTarget.REVIEW,
    Stage.SPLICE: ResumeTarget.REVIEW,
    Stage.FULL_REVIEW: ResumeTarget.REVIEW,
    Stage.OUTPUT: ResumeTarget.REVIEW,
}

_DEFAULT_LOCK_TIMEOUT_S = 30.0


class CheckpointStore:
    """项目检查点。``project`` 可以是 ProjectPaths 或项目目录。"""

    def __init__(self, project: ProjectPaths | Path | str, *, timeout_s: float = _DEFAULT_LOCK_TIMEOUT_S) -> None:
        self.paths = coerce_project_paths(project)
        self.timeout_s = float(timeout_s)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @property
    def checkpoints_dir(self) -> Path:
        return self.paths.checkpoints_dir

    def _lock_path(self) -> Path:
        """检查点 seq 分配用的锁文件。

        路径**本身就是完整锁文件名**（直接交给 ``file_lock``），不要再经
        ``lock_path_for`` 包一层，否则会生成 ``..checkpoints.lock.lock`` 这种
        双点文件名（能用但极具误导性）。该文件以 ``.lock`` 结尾，
        不会被 ``<stage>.<seq>.json`` 的扫描命中。
        """
        return self.checkpoints_dir / ".checkpoints.lock"

    @staticmethod
    def _stage_index(stage: str) -> int:
        try:
            return STAGE_ORDER.index(stage)
        except ValueError as exc:
            raise CorruptFileError(f"检查点里的 stage 非法：{stage!r}") from exc

    def _existing(self) -> list[dict]:
        """列出全部检查点（按 (stage_index, seq) 升序）。"""
        directory = self.checkpoints_dir
        if not directory.is_dir():
            return []
        records: list[dict] = []
        for item in sorted(directory.glob("*.json"), key=lambda p: p.name):
            if item.name.startswith("."):
                continue
            raw = read_json(item, default=None)
            if raw is None:
                continue
            if not isinstance(raw, dict) or "stage" not in raw:
                raise CorruptFileError(
                    "检查点文件缺少 stage 字段（拒绝猜测）", path=str(item)
                )
            raw.setdefault("seq", self._seq_from_name(item.name))
            raw.setdefault("_path", str(item))
            records.append(raw)
        records.sort(key=lambda r: (self._stage_index(r["stage"]), int(r.get("seq") or 0)))
        return records

    @staticmethod
    def _seq_from_name(name: str) -> int:
        stem = Path(name).stem
        _, _, tail = stem.rpartition(".")
        return int(tail) if tail.isdigit() else 0

    def _next_seq(self, stage: Stage) -> int:
        highest = 0
        for item in self.checkpoints_dir.glob(f"{stage.value}.*.json"):
            seq = self._seq_from_name(item.name)
            highest = max(highest, seq)
        return highest + 1

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def save(self, stage: Stage, payload: dict) -> Path:
        """保存检查点（只追加）。返回写入的文件路径。"""
        stage_enum = parse_enum(Stage, stage, kind="stage")
        if not isinstance(payload, dict):
            raise StoreError("检查点 payload 必须是 dict", actual=type(payload).__name__)

        with file_lock(self._lock_path(), timeout_s=self.timeout_s):
            seq = self._next_seq(stage_enum)
            target = self.paths.checkpoint_json(stage_enum, seq)
            if target.exists():
                raise ImmutableAssetError(
                    "检查点目标已存在，拒绝覆盖（只追加）", path=str(target)
                )
            record = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "project_id": self.paths.project_id,
                "stage": stage_enum.value,
                "stage_index": STAGE_ORDER.index(stage_enum.value),
                "seq": seq,
                "saved_at": now_iso(),
                # 自描述：恢复语义随检查点一起固化
                "resume_target": STAGE_TO_RESUME_TARGET[stage_enum].value,
                "payload": payload,
            }
            atomic_write_json(target, record)

        append_event(
            self.paths,
            event="CHECKPOINT_SAVED",
            stage=stage_enum,
            detail={"seq": seq, "path": target.name},
        )
        return target

    def latest(self, *, at_or_before: Stage | None = None) -> dict | None:
        """最近一次检查点（按 stage 顺序 -> seq）；``at_or_before`` 限定不晚于某阶段。"""
        limit = None
        if at_or_before is not None:
            limit = self._stage_index(parse_enum(Stage, at_or_before, kind="stage").value)
        candidates = [
            record
            for record in self._existing()
            if limit is None or self._stage_index(record["stage"]) <= limit
        ]
        if not candidates:
            return None
        return max(
            candidates, key=lambda r: (self._stage_index(r["stage"]), int(r.get("seq") or 0))
        )

    def list_checkpoints(self) -> list[dict]:
        """全部检查点（按 (stage_index, seq) 升序）。"""
        return self._existing()

    def resolve_resume_target(self) -> ResumeTarget:
        """恢复目标 G / P / R。

        优先使用检查点里**存档的** ``resume_target``（自描述），缺失/非法时
        回退到 :data:`STAGE_TO_RESUME_TARGET` 映射表；无检查点 -> ``E.ASSET``。
        """
        record = self.latest()
        if record is None:
            return ResumeTarget.ASSET
        stored = record.get("resume_target")
        if stored is not None:
            try:
                return ResumeTarget.parse(stored)
            except ValueError:
                logger.warning(
                    "检查点 %s 的 resume_target 非法（%r），回退到映射表",
                    record.get("_path"),
                    stored,
                )
        return STAGE_TO_RESUME_TARGET[parse_enum(Stage, record["stage"], kind="stage")]

    def load(self, path: Path | str) -> dict:
        """读取单个检查点文件。"""
        return read_json(Path(path), default=None, require_dict=True) or {}

    def load_payload(self, *, stage: Stage | None = None) -> dict | None:
        """读取最近检查点的 payload（无检查点返回 None）。"""
        record = self.latest(at_or_before=stage)
        if record is None:
            return None
        payload = record.get("payload")
        return payload if isinstance(payload, dict) else None
