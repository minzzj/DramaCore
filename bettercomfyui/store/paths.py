"""项目目录布局与路径隔离 —— docs/CONTRACTS.md 第 1 节 + R5。

R5（项目隔离）的落地方式
------------------------
1. :class:`ProjectPaths` 的**每一个**路径都由 ``project_dir`` 派生，
   而 ``project_dir = root / project_id``，因此「任何路径都含 project_id」是
   **构造性保证**，不是靠调用方自觉。
2. 所有外部传入的路径片段（``asset_id`` / ``version`` / ``chapter_id`` /
   ``segment_id`` / ``stage``）都经过 :func:`safe_component` 校验：
   拒绝空串、``.``/``..``、路径分隔符、Windows 非法字符、保留设备名。
3. :meth:`ProjectPaths.within` 用于验证「一个已有路径确实属于本项目」，
   跨项目访问会抛 :class:`~bettercomfyui.store.errors.PathIsolationError`。

布局（唯一合法布局）::

    <repo>/projects/<project_id>/
      project.json  config.yaml  meta.json  suspects.json
      chapters/<chapter_id>.json
      assets/<type>/{asset.json, versions/v1/{file.<ext>, meta.json}}
      segments/<segment_id>.json
      output/  checkpoints/<stage>.<seq>.json  logs/events.jsonl
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..models.enums import AssetType, Stage
from .errors import PathIsolationError, parse_enum
from .ids import format_version, is_valid_project_id

__all__ = [
    "ProjectPaths",
    "coerce_project_paths",
    "safe_component",
    "assert_within",
    "ASSET_TYPE_DIRS",
]

#: 素材类型 -> 目录名（小写，与 asset_id 里的类型段一致）
ASSET_TYPE_DIRS: dict[str, str] = {t.value: t.value.lower() for t in AssetType}

_FORBIDDEN_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_COMPONENT_LEN = 120


def safe_component(value: object, *, kind: str = "component", max_length: int = _MAX_COMPONENT_LEN) -> str:
    """校验单个路径片段（目录名/文件名主干）。

    非法输入**抛错**而不是「清洗后继续」：静默改写标识符会让落盘路径与元数据
    里的 ID 不一致，进而造成「找不到素材」的假象。
    """
    if not isinstance(value, str):
        raise PathIsolationError(f"{kind} 必须是字符串", kind=kind, value=repr(value))
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        raise PathIsolationError(f"{kind} 不能为空", kind=kind)
    if text in {".", ".."}:
        raise PathIsolationError(f"{kind} 不能是 '.' 或 '..'", kind=kind, value=text)
    if len(text) > max_length:
        raise PathIsolationError(
            f"{kind} 过长（>{max_length}）", kind=kind, length=len(text)
        )
    for ch in text:
        if ch in _FORBIDDEN_CHARS or ord(ch) < 32 or ch in {"\x7f"}:
            raise PathIsolationError(
                f"{kind} 含非法字符 {ch!r}", kind=kind, value=text
            )
    if "/" in text or "\\" in text or os.sep in text or (os.altsep and os.altsep in text):
        raise PathIsolationError(f"{kind} 不能包含路径分隔符", kind=kind, value=text)
    if Path(text).name != text:
        raise PathIsolationError(f"{kind} 含路径成分", kind=kind, value=text)
    if text[-1] in {" ", "."}:
        raise PathIsolationError(
            f"{kind} 不能以空格或点结尾（Windows 会静默去掉）", kind=kind, value=text
        )
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        raise PathIsolationError(
            f"{kind} 是 Windows 保留设备名", kind=kind, value=text
        )
    return text


def _validated_asset_type(asset_type: object) -> AssetType:
    return parse_enum(AssetType, asset_type, kind="asset_type")


def assert_within(base: Path | str, path: Path | str) -> Path:
    """断言 ``path`` 位于 ``base`` 之内（含 base 自身），否则抛 PathIsolationError。"""
    base_path = Path(base)
    target = Path(path)
    try:
        base_real = base_path.resolve()
        target_real = target.resolve()
    except OSError as exc:  # pragma: no cover - 环境异常
        raise PathIsolationError(f"无法解析路径: {exc}", base=str(base_path)) from exc
    if target_real != base_real and base_real not in target_real.parents:
        raise PathIsolationError(
            "路径越界：目标不在允许的根目录之内（R5 项目隔离）",
            base=str(base_real),
            target=str(target_real),
        )
    return target


@dataclass(frozen=True)
class ProjectPaths:
    """一个项目的全部路径。``root`` 是 ``projects/`` 根目录，不是仓库根。"""

    root: Path
    project_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if not is_valid_project_id(self.project_id):
            raise PathIsolationError(
                "project_id 必须是 32 位小写 UUID4 hex（无连字符）",
                project_id=repr(self.project_id),
            )

    # -- 根 -----------------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        return self.root / self.project_id

    @property
    def project_json(self) -> Path:
        return self.project_dir / "project.json"

    @property
    def config_yaml(self) -> Path:
        return self.project_dir / "config.yaml"

    @property
    def meta_json(self) -> Path:
        """素材索引（所有更新必须经过 locked_update_json）。"""
        return self.project_dir / "meta.json"

    @property
    def suspects_json(self) -> Path:
        """疑似重复队列（独立文件，避免污染 meta.json 的字节）。"""
        return self.project_dir / "suspects.json"

    # -- 章节 ---------------------------------------------------------------

    @property
    def chapters_dir(self) -> Path:
        return self.project_dir / "chapters"

    def chapter_json(self, chapter_id: object) -> Path:
        return self.chapters_dir / f"{safe_component(str(chapter_id), kind='chapter_id')}.json"

    # -- 素材 ---------------------------------------------------------------

    @property
    def assets_dir(self) -> Path:
        return self.project_dir / "assets"

    def asset_type_dir(self, asset_type: object) -> Path:
        enum_value = _validated_asset_type(asset_type)
        return self.assets_dir / ASSET_TYPE_DIRS[enum_value.value]

    def asset_dir(self, asset_type: object, asset_id: object) -> Path:
        return self.asset_type_dir(asset_type) / safe_component(
            str(asset_id), kind="asset_id"
        )

    def asset_json(self, asset_type: object, asset_id: object) -> Path:
        return self.asset_dir(asset_type, asset_id) / "asset.json"

    def versions_dir(self, asset_type: object, asset_id: object) -> Path:
        return self.asset_dir(asset_type, asset_id) / "versions"

    def version_dir(self, asset_type: object, asset_id: object, version: object) -> Path:
        return self.versions_dir(asset_type, asset_id) / safe_component(
            format_version(version), kind="version"
        )

    def version_meta(self, asset_type: object, asset_id: object, version: object) -> Path:
        return self.version_dir(asset_type, asset_id, version) / "meta.json"

    def version_file(
        self, asset_type: object, asset_id: object, version: object, filename: object
    ) -> Path:
        return self.version_dir(asset_type, asset_id, version) / safe_component(
            str(filename), kind="version_filename"
        )

    # -- 视频段 -------------------------------------------------------------

    @property
    def segments_dir(self) -> Path:
        return self.project_dir / "segments"

    def segment_json(self, segment_id: object) -> Path:
        return self.segments_dir / f"{safe_component(str(segment_id), kind='segment_id')}.json"

    # -- 输出 / 检查点 / 日志 ------------------------------------------------

    @property
    def output_dir(self) -> Path:
        return self.project_dir / "output"

    @property
    def checkpoints_dir(self) -> Path:
        return self.project_dir / "checkpoints"

    def checkpoint_json(self, stage: object, seq: object) -> Path:
        stage_enum = parse_enum(Stage, stage, kind="stage")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise PathIsolationError("checkpoint seq 必须是 >= 1 的整数", seq=repr(seq))
        return self.checkpoints_dir / f"{stage_enum.value}.{seq}.json"

    @property
    def logs_dir(self) -> Path:
        return self.project_dir / "logs"

    @property
    def events_jsonl(self) -> Path:
        return self.logs_dir / "events.jsonl"

    # -- 工具 ---------------------------------------------------------------

    def ensure_layout(self) -> "ProjectPaths":
        """创建布局所需的全部目录（幂等）。"""
        for directory in (
            self.project_dir,
            self.chapters_dir,
            self.assets_dir,
            self.segments_dir,
            self.output_dir,
            self.checkpoints_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def within(self, path: Path | str) -> Path:
        """校验 ``path`` 属于本项目目录，返回解析后的路径。"""
        return assert_within(self.project_dir, path)

    def relative(self, path: Path | str) -> str:
        """返回相对项目目录的 posix 风格路径（用于写进元数据，绝不写绝对路径）。"""
        self.within(path)  # R5 越界校验
        try:
            relative = Path(path).resolve().relative_to(self.project_dir.resolve())
        except (ValueError, OSError) as exc:  # pragma: no cover - 防御性
            raise PathIsolationError(
                "路径不在项目目录内，无法计算相对路径",
                path=str(path),
                project_dir=str(self.project_dir),
            ) from exc
        return relative.as_posix()

    def absolute_from_relative(self, rel: str) -> Path:
        """把元数据里的相对路径还原成绝对路径，并校验未越界。"""
        if not isinstance(rel, str) or not rel.strip():
            raise PathIsolationError("相对路径不能为空", value=repr(rel))
        candidate = Path(rel)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:", rel):
            raise PathIsolationError("元数据里不得出现绝对路径", value=rel)
        return self.within(self.project_dir / candidate)


def coerce_project_paths(value: object) -> ProjectPaths:
    """接受 :class:`ProjectPaths`、项目目录 ``Path``/``str``。

    传目录时按 ``<projects_root>/<project_id>`` 反推：目录名必须是合法
    ``project_id``，否则抛错（防止把任意目录当成项目目录）。
    """
    if isinstance(value, ProjectPaths):
        return value
    if isinstance(value, (str, Path)):
        candidate = Path(value)
        project_id = candidate.name
        if not is_valid_project_id(project_id):
            raise PathIsolationError(
                "无法从路径推断 project_id（目录名必须是 32 位小写 UUID4 hex）",
                path=str(candidate),
            )
        return ProjectPaths(root=candidate.parent, project_id=project_id)
    raise PathIsolationError(
        "AssetStore/CheckpointStore 需要 ProjectPaths 或项目目录 Path",
        actual=type(value).__name__,
    )
