"""``bettercomfyui.store`` 公开接口（docs/CONTRACTS.md 第 3、4 节）。

依赖方向单向：store 只依赖 ``models`` 与标准库，**绝不 import**
``prompt`` / ``review`` / ``director`` / ``pipeline``。

模块导航
--------
* :mod:`~bettercomfyui.store.atomic` —— 确定性 JSON / 原子写 / 加锁读改写
* :mod:`~bettercomfyui.store.lockfile` —— 跨平台文件锁（Windows + POSIX，可重入）
* :mod:`~bettercomfyui.store.ids` —— 全部标识符生成（第 2 节唯一入口）
* :mod:`~bettercomfyui.store.paths` —— 目录布局与 R5 项目隔离
* :mod:`~bettercomfyui.store.projects` —— 项目生命周期
* :mod:`~bettercomfyui.store.assets` —— 素材库（只追加 / LOCK / 疑似队列）
* :mod:`~bettercomfyui.store.checkpoints` —— 检查点与恢复目标
* :mod:`~bettercomfyui.store.inject` —— R1：已有素材清单注入文本
* :mod:`~bettercomfyui.store.events` —— 追加式事件日志（第 10 节）
* :mod:`~bettercomfyui.store.errors` —— 异常体系
"""

from __future__ import annotations

from . import ids
from .assets import INDEX_SCHEMA_VERSION, AssetStore, empty_index, index_key
from .atomic import (
    FileLock,
    atomic_write_json,
    canonical_json,
    file_lock,
    lock_path_for,
    locked_update_json,
    now_iso,
    read_json,
)
from .checkpoints import (
    CHECKPOINT_SCHEMA_VERSION,
    STAGE_ORDER,
    STAGE_TO_RESUME_TARGET,
    CheckpointStore,
)
from .errors import (
    AssetNotFoundError,
    ConfirmTokenMismatchError,
    ContentHashMismatchError,
    CorruptFileError,
    ImmutableAssetError,
    InvalidIdentifierError,
    LockAlreadyExistsError,
    LockTimeoutError,
    PathIsolationError,
    ProjectBusyError,
    ProjectExistsError,
    ProjectNotFoundError,
    SerializationError,
    StoreError,
    VersionNotFoundError,
)
from .events import EVENT_NAMESPACES, EVENT_SEVERITIES, append_event, make_event, read_events
from .inject import build_existing_asset_manifest
from .paths import ProjectPaths, assert_within, coerce_project_paths, safe_component
from .projects import PROJECT_SCHEMA_VERSION, ProjectStore

__all__ = [
    # 模块
    "ids",
    # 原子写 / 锁
    "canonical_json",
    "atomic_write_json",
    "locked_update_json",
    "file_lock",
    "FileLock",
    "lock_path_for",
    "read_json",
    "now_iso",
    # 路径
    "ProjectPaths",
    "coerce_project_paths",
    "safe_component",
    "assert_within",
    # 项目
    "ProjectStore",
    "PROJECT_SCHEMA_VERSION",
    # 素材
    "AssetStore",
    "INDEX_SCHEMA_VERSION",
    "empty_index",
    "index_key",
    # 检查点
    "CheckpointStore",
    "CHECKPOINT_SCHEMA_VERSION",
    "STAGE_ORDER",
    "STAGE_TO_RESUME_TARGET",
    # 注入
    "build_existing_asset_manifest",
    # 事件
    "append_event",
    "read_events",
    "make_event",
    "EVENT_NAMESPACES",
    "EVENT_SEVERITIES",
    # 异常
    "StoreError",
    "SerializationError",
    "CorruptFileError",
    "LockTimeoutError",
    "PathIsolationError",
    "ProjectNotFoundError",
    "ProjectExistsError",
    "ProjectBusyError",
    "ConfirmTokenMismatchError",
    "AssetNotFoundError",
    "VersionNotFoundError",
    "ContentHashMismatchError",
    "LockAlreadyExistsError",
    "ImmutableAssetError",
    "InvalidIdentifierError",
]
