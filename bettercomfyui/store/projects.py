"""项目生命周期（docs/CONTRACTS.md 第 4 节）。

职责边界
--------
* 建/列/开/关/删/切项目；建目录布局；写 ``project.json`` 与项目级 ``config.yaml``；
* 配置分层解析复用 ``bettercomfyui.config.load_config``（项目 > 用户 > 默认），
  本模块**不重新实现配置合并**；
* 素材库、检查点、事件日志分别委托给 ``store/assets.py`` / ``store/checkpoints.py``
  / ``store/events.py``（同一 store 层，各自负责自己的文件）。

安全要点
--------
* ``project_id`` 一律 32 位小写 UUID4 hex，由 :class:`ProjectPaths` 二次校验；
* 删除项目前校验「解析后的绝对路径 == projects_root/project_id」，且必须
  ``confirm_token == project_id``，项目被打开或有运行任务时拒绝删除；
* 配置快照写盘前**清空 api_key**（与 ``config.dump_config`` 的策略一致，
  绝不把真实密钥写进仓库）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import yaml

from ..config import Config, ConfigError, load_config
from ..models.entities import Chapter, now_iso
from ..models.enums import Stage
from .assets import AssetStore, empty_index
from .atomic import atomic_write_json, locked_update_json, read_json
from .checkpoints import CheckpointStore
from .errors import (
    ConfirmTokenMismatchError,
    CorruptFileError,
    PathIsolationError,
    ProjectBusyError,
    ProjectExistsError,
    ProjectNotFoundError,
    StoreError,
)
from .events import append_event
from .ids import is_valid_project_id, new_project_id
from .paths import ProjectPaths, assert_within

__all__ = ["ProjectStore", "PROJECT_SCHEMA_VERSION"]

logger = logging.getLogger(__name__)

PROJECT_SCHEMA_VERSION = 1
_CONFIG_YAML_HEADER = (
    "# 项目级配置覆盖（优先级最高：项目 config.yaml > 用户 user.yaml > 默认 default.yaml）\n"
    "# 默认留空，表示完全继承上层配置。未知键会在加载期报错。\n"
)


class ProjectStore:
    """项目仓库。``root`` 是仓库根目录（或直接传 ``projects/`` 目录）。"""

    def __init__(self, root: Path | str, config: Config | None = None) -> None:
        self.root = Path(root)
        self.config = config if config is not None else Config()
        project_root = getattr(getattr(self.config, "global_", None), "project_root", None)
        project_root = str(project_root or "projects")
        configured = Path(project_root)

        if configured.is_absolute():
            self.projects_root = configured
            self.repo_root = self.root
        elif self.root.name == configured.name and self.root.is_dir():
            # 允许直接把 <repo>/projects 传进来
            self.projects_root = self.root
            self.repo_root = self.root.parent
        else:
            self.projects_root = self.root / configured
            self.repo_root = self.root
        self._open: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 路径 / 存在性
    # ------------------------------------------------------------------

    def paths(self, project_id: str) -> ProjectPaths:
        """返回项目路径对象（不要求项目已存在，create 也要用）。"""
        return ProjectPaths(root=self.projects_root, project_id=project_id)

    def exists(self, project_id: str) -> bool:
        if not is_valid_project_id(project_id):
            return False
        return self.paths(project_id).project_json.is_file()

    def require(self, project_id: str) -> ProjectPaths:
        if not is_valid_project_id(project_id):
            raise ProjectNotFoundError(
                "project_id 必须是 32 位小写 UUID4 hex", project_id=repr(project_id)
            )
        paths = self.paths(project_id)
        if not paths.project_json.is_file():
            raise ProjectNotFoundError(
                "项目不存在（缺少 project.json）",
                project_id=project_id,
                project_dir=str(paths.project_dir),
            )
        return paths

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def _config_snapshot(self) -> dict:
        snapshot = self.config.to_dict()
        prompt_section = snapshot.get("prompt")
        if isinstance(prompt_section, dict) and prompt_section.get("api_key"):
            # 与 config.dump_config 一致：绝不把真实密钥写入仓库
            prompt_section["api_key"] = None
        return snapshot

    def _write_project_config(self, paths: ProjectPaths) -> None:
        body = yaml.safe_dump({}, allow_unicode=True, sort_keys=False)
        paths.config_yaml.write_text(_CONFIG_YAML_HEADER + body, encoding="utf-8")

    def create(self, display_name: str, *, project_id: str | None = None) -> dict:
        """新建项目：``project_id`` + 目录布局 + 空素材库 + 默认配置 + PROJECT 检查点。"""
        if not isinstance(display_name, str) or not display_name.strip():
            raise StoreError("display_name 不能为空", display_name=repr(display_name))
        name = display_name.strip()

        attempts = 0
        while True:
            # 显式传入的 project_id 必须原样校验（空串不能被静默当成「自动生成」）
            candidate = project_id if project_id is not None else new_project_id()
            if not is_valid_project_id(candidate):
                raise PathIsolationError(
                    "project_id 必须是 32 位小写 UUID4 hex", project_id=repr(candidate)
                )
            paths = self.paths(candidate)
            if not paths.project_dir.exists():
                break
            if project_id is not None:
                raise ProjectExistsError(
                    "项目目录已存在", project_id=project_id, path=str(paths.project_dir)
                )
            attempts += 1
            if attempts >= 8:  # pragma: no cover - UUID4 碰撞概率可忽略
                raise StoreError("生成唯一 project_id 失败", attempts=attempts)

        paths.ensure_layout()
        created = now_iso()
        project = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": paths.project_id,
            "display_name": name,
            "created_at": created,
            "updated_at": created,
            "script_id": None,
            "config_snapshot": self._config_snapshot(),
            "status": "idle",
            "running_tasks": [],
            "progress": {},
            "closed_at": None,
        }
        atomic_write_json(paths.project_json, project)
        self._write_project_config(paths)
        atomic_write_json(paths.meta_json, empty_index(paths.project_id))
        append_event(
            paths,
            event="PROJECT_CREATED",
            detail={"display_name": name},
        )
        CheckpointStore(paths).save(
            Stage.PROJECT,
            {"project_id": paths.project_id, "display_name": name, "created_at": created},
        )
        # 注意：create 不把项目标记为「已打开」——只有 open() 才代表有会话状态，
        # 否则「新建后立刻删除」会被误判为「有未保存状态」而拒绝。
        result = dict(project)
        result["paths"] = paths
        return result

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list(self) -> list[dict]:
        """扫描 ``projects/`` 下带 ``project.json`` 的目录。损坏项目跳过并 WARN。"""
        out: list[dict] = []
        if not self.projects_root.is_dir():
            return out
        for entry in sorted(self.projects_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            project_json = entry / "project.json"
            if not project_json.is_file():
                continue
            try:
                data = read_json(project_json, require_dict=True)
            except StoreError as exc:
                logger.warning("跳过无法读取的项目 %s: %s", entry.name, exc)
                continue
            out.append(data)
        out.sort(key=lambda item: (item.get("created_at") or "", item.get("project_id") or ""))
        return out

    # ------------------------------------------------------------------
    # 打开 / 关闭 / 切换
    # ------------------------------------------------------------------

    def _load_project_config(self, paths: ProjectPaths) -> Config:
        """按 项目 > 用户 > 默认 的优先级加载配置（复用 config.load_config）。"""
        return load_config(repo_root=self.repo_root, project_dir=paths.project_dir)

    def _load_chapters(self, paths: ProjectPaths) -> list[dict]:
        if not paths.chapters_dir.is_dir():
            return []
        chapters: list[dict] = []
        for item in sorted(paths.chapters_dir.glob("*.json"), key=lambda p: p.name):
            raw = read_json(item, default=None)
            if raw is None:
                continue
            try:
                chapter = Chapter.from_dict(raw)
            except (ValueError, TypeError) as exc:
                raise CorruptFileError(
                    f"章节文件不符合 models.entities.Chapter 约束：{exc}",
                    path=str(item),
                ) from exc
            chapters.append(chapter.to_dict())
        chapters.sort(key=lambda c: (c.get("index", 0), c.get("chapter_id") or ""))
        return chapters

    def open(self, project_id: str) -> dict:
        """加载项目：配置 / 章节 / 素材库 / 进度 / 最近检查点。

        只读操作，不修改 ``project.json``（避免「打开一下」就产生磁盘写入）。
        项目 ``config.yaml`` 非法时抛 :class:`~bettercomfyui.config.ConfigError`。
        """
        paths = self.require(project_id)
        project = read_json(paths.project_json, require_dict=True)
        config = self._load_project_config(paths)
        chapters = self._load_chapters(paths)
        assets = AssetStore(paths).load_index()
        checkpoints = CheckpointStore(paths)

        result = dict(project)
        result["paths"] = paths
        result["config"] = config
        result["chapters"] = chapters
        result["assets"] = assets
        result["progress"] = dict(project.get("progress") or {})
        result["latest_checkpoint"] = checkpoints.latest()
        result["resume_target"] = checkpoints.resolve_resume_target()
        self._open[project_id] = {"paths": paths, "snapshot": result}
        append_event(
            paths,
            event="PROJECT_OPENED",
            detail={"resume_target": result["resume_target"].value},
        )
        return result

    def resolve_reference(self, value: str) -> tuple[dict, str]:
        """把工作流/节点里的「项目引用」解析成真实项目。

        ## 为什么需要它（审计 R2 发现）

        发布的工作流里曾把**开发机的 32 位项目 id** 硬编码进 widget。
        换一台机器（或新用户首次使用）该 id 不存在：
        `Everything` 节点会静默拿不到素材库、`AssetGen/AssetReview` 等节点直接报
        `ProjectNotFound` —— 「工作流能打开但跑不通」。

        解析规则（把可移植性放进数据层，避免每台机器手改 id）：

        * 合法 32 位项目 id（UUID4 hex）→ 必须是**已存在**项目；
          不存在抛 :class:`~bettercomfyui.store.errors.ProjectNotFoundError`
          （id 写错时静默新建空项目会掩盖错误，所以这里**绝不**隐式创建）；
        * 其他非空字符串 → 视为**项目显示名**：已有同名项目则复用；
          没有则新建一个同名项目（首次使用工作流即可继续跑，不必先手填 id）。

        返回 ``(info, action)``，``action`` ∈
        ``{"EXISTING_ID", "EXISTING_NAME", "CREATED_NAME"}``；
        调用方应据此在摘要/审计里如实说明「复用了哪个项目 / 自动新建了哪个项目」。
        """
        text = (value or "").strip()
        if not text:
            raise ValueError(
                "项目引用为空：请填写 project_id，或填写项目显示名（如「我的短剧」）"
            )
        if is_valid_project_id(text):
            return self.open(text), "EXISTING_ID"
        for row in self.list():
            if str(row.get("display_name") or "").strip() == text:
                return self.open(str(row["project_id"])), "EXISTING_NAME"
        return self.create(text), "CREATED_NAME"

    def close(self, project_id: str) -> dict:
        """保存状态后关闭项目（写 ``progress`` / ``status`` / ``closed_at``）。"""
        if project_id not in self._open:
            return {
                "project_id": project_id,
                "closed": False,
                "reason": "not_open",
                "saved": None,
            }
        state = self._open.pop(project_id)
        paths = state["paths"]

        def mutator(data: dict) -> dict:
            data = dict(data)
            data["status"] = "closed"
            data["closed_at"] = now_iso()
            data["updated_at"] = now_iso()
            data.setdefault("progress", {})
            return data

        saved = locked_update_json(paths.project_json, mutator)
        append_event(paths, event="PROJECT_CLOSED", detail={"status": "closed"})
        return {
            "project_id": project_id,
            "closed": True,
            "reason": "saved",
            "saved": saved,
        }

    def switch(self, from_id: str | None, to_id: str) -> dict:
        """先保存 ``from_id`` 再打开 ``to_id``。"""
        closed = self.close(from_id) if from_id else None
        opened = self.open(to_id)
        opened["switched_from"] = from_id
        opened["closed_previous"] = closed
        return opened

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete(self, project_id: str, *, confirm_token: str) -> None:
        """删除项目（不可恢复）。需要 ``confirm_token == project_id`` 二次确认。

        拒绝条件：token 不符、项目不存在、项目处于打开状态（可能有未保存状态）、
        ``running_tasks`` 非空、或存在 ``.running`` 运行标记文件。
        """
        if confirm_token != project_id:
            raise ConfirmTokenMismatchError(
                "删除项目需要二次确认：confirm_token 必须等于 project_id",
                project_id=project_id,
            )
        paths = self.require(project_id)
        if project_id in self._open:
            raise ProjectBusyError(
                "项目仍处于打开状态（可能有未保存状态），请先 close 再删除",
                project_id=project_id,
            )
        project = read_json(paths.project_json, require_dict=True)
        running = project.get("running_tasks") or []
        if running:
            raise ProjectBusyError(
                "项目有运行中的任务，拒绝删除", project_id=project_id, running_tasks=running
            )
        if (paths.project_dir / ".running").exists():
            raise ProjectBusyError(
                "项目存在 .running 运行标记，拒绝删除", project_id=project_id
            )

        # 防御性二次校验：解析后的绝对路径必须正好是 projects_root/<project_id>
        target = paths.project_dir.resolve()
        assert_within(self.projects_root.resolve(), target)
        if target.name != project_id or target.parent != self.projects_root.resolve():
            raise PathIsolationError(
                "拒绝删除：目标路径不是 projects_root/<project_id>",
                target=str(target),
                projects_root=str(self.projects_root),
            )
        shutil.rmtree(target)
        logger.info("项目已删除：%s", project_id)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def opened_projects(self) -> list[str]:
        """当前进程内已打开的项目 id（删除前检查「未保存状态」用）。"""
        return sorted(self._open)
