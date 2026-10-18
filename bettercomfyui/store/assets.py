"""素材库 —— 只追加、LOCK 绑定版本、项目隔离（docs/CONTRACTS.md 第 4 节）。

三条硬规则在本模块的落地方式
----------------------------
**R2（只追加不覆盖）**
  * 新内容一律写入**新版本目录** ``versions/v{n}/``；
  * 版本目录用 ``mkdir(exist_ok=False)`` 创建、素材文件用
    ``os.open(..., O_CREAT | O_EXCL)`` 写入并逐字节复制 ——
    目标已存在就失败，物理上不可能原地覆盖；
  * 任何更新 ``meta.json`` 的路径都经过 :func:`~bettercomfyui.store.atomic.locked_update_json`。

**R3（LOCK 绑定 asset_id + version + content_hash）**
  * 写 LOCK 前**重新计算磁盘文件指纹**，与入参和版本元数据三者比对，
    任一不一致抛 :class:`~bettercomfyui.store.errors.ContentHashMismatchError`；
  * LOCK 只写在自己那个版本上，新版本绝不继承（``add_version`` 新建的版本
    一律 ``lock_status=UNLOCKED``）；
  * :meth:`AssetStore.resolve_lock` 是生成侧唯一门禁：**没有可授权的 LOCK 记录**
    （未 LOCK / 版本不存在 / 三要素不全 / 文件不在磁盘上）返回 ``None``，绝不「大概能过」；
    而**读取/解析失败**（索引损坏、指纹无法解析、路径越界）一律抛
    :class:`~bettercomfyui.store.errors.StoreError` 并标注 ``ErrorClass.FILE_ENCODING``
    —— 技术故障绝不能被降级成「未审核（正常拒绝）」，否则调用方无法区分二者，
    正是任务书要防的「技术失败被当成正常结果」。

**R5（项目隔离）**
  * 所有路径由 :class:`~bettercomfyui.store.paths.ProjectPaths` 派生；
  * ``meta.json`` 里的 ``project_id`` 与当前项目不一致时抛
    :class:`~bettercomfyui.store.errors.CorruptFileError`（绝不串项目读写）。

持久化结构严格等于 ``models/entities.py`` 的 ``AssetEntry`` / ``AssetVersion``
（构造 dataclass -> 校验不变量 -> ``to_dict()`` 落盘），因此不存在「第二套 schema」。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import unicodedata
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any, Callable, Iterable

from ..models.entities import AssetEntry, AssetVersion, SuspectRecord, now_iso
from ..models.enums import (
    AssetSource,
    AssetStatus,
    AssetType,
    ErrorClass,
    LibraryScope,
    LockSource,
    LockStatus,
)
from .atomic import atomic_write_json, locked_update_json, lock_path_for, read_json
from .events import append_event
from .errors import (
    AssetNotFoundError,
    ContentHashMismatchError,
    CorruptFileError,
    ImmutableAssetError,
    InvalidIdentifierError,
    LockAlreadyExistsError,
    StoreError,
    VersionNotFoundError,
    parse_enum,
)
from .ids import (
    content_hash_of,
    format_version,
    lock_id,
    new_asset_id,
    new_suspect_id,
    parse_content_hash,
    parse_version,
    version_sort_key,
)
from .lockfile import file_lock
from .paths import ProjectPaths, coerce_project_paths

__all__ = ["AssetStore", "INDEX_SCHEMA_VERSION", "empty_index", "index_key",
           "verify_locked_file_content"]

logger = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION = 1
SUSPECT_SCHEMA_VERSION = 1

#: 不允许通过 ``set_version_status(**fields)`` 改写的追溯字段（改了就无法复核）
STRUCTURAL_VERSION_FIELDS = frozenset({"version", "content_hash", "file_path", "asset_id"})

_DEFAULT_LOCK_TIMEOUT_S = 30.0

# 中文/全角标点 -> 半角（NFKC 不处理这些 CJK 标点）
_PUNCT_MAP = {
    "。": ".",
    "，": ",",
    "、": ",",
    "：": ":",
    "；": ";",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "《": "<",
    "》": ">",
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "・": "·",
    "—": "-",
    "–": "-",
    "～": "~",
}
#: 比较时直接忽略的标点（归一化的最终目的：只比较「实质字符」）
_IGNORED_CHARS = set(".,:;!?'\"()[]{}<>·-—_~`@#$%^&*+=|\\/")


def index_key(text: Any) -> str:
    """store 侧**保守**归一化：NFKC + casefold + 去空白/标点。

    作用：让「全角/半角、大小写、空格、中英标点」差异不影响索引命中；同时
    :meth:`AssetStore.find_by_normalized_name` 会对查询串再次归一化，因此即使
    调用方（``prompt/normalize.py``）的归一化细节不同也不会漏匹配。
    本函数是纯函数且幂等（``index_key(index_key(x)) == index_key(x)``）。
    """
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    out: list[str] = []
    for ch in normalized:
        mapped = _PUNCT_MAP.get(ch, ch)
        if mapped.isspace() or mapped in _IGNORED_CHARS:
            continue
        out.append(mapped)
    return "".join(out)


def empty_index(project_id: str) -> dict:
    """空素材索引（新建项目或 meta.json 尚不存在时的合法初值）。"""
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": 0,
        "updated_at": now_iso(),
        "assets": [],
    }


def verify_locked_file_content(locked: dict, file_path: Path | str) -> str:
    """校验「磁盘文件实际字节」与 LOCK 记录声明的内容指纹一致（R3 生成前置门禁）。

    ## 为什么需要它（审计 R2 发现）

    `resolve_lock` 里那条「content_hash 与版本元数据不一致」判断，实际上是
    **记录与自身比较**（`parse_content_hash(entry["content_hash"]) != entry["content_hash"]`
    只校验格式），而生成侧（导演台参考图 / 素材节点）只检查文件存在。
    后果：LOCK 之后文件被外部替换/篡改，消费方仍按**旧 LOCK 授权的指纹**加载
    **不同内容**，没有任何报错 —— 版本/指纹/LOCK 语义静默失效。

    这里重算磁盘 SHA-256 并比对；不一致抛 :class:`ContentHashMismatchError`
    （内容被替换不是「正常未授权」，不能降级成 None 混过去）。返回实际指纹。

    调用位置：所有「把 LOCK 授权素材读进生成链路」的入口（素材节点 / 融合参考图 /
    参考音视频），见 `bettercomfyui_comfy/asset_nodes.py` 与 `fusion.py`。
    """
    declared = parse_content_hash((locked or {}).get("content_hash"))
    actual = content_hash_of(file_path)
    if actual != declared:
        raise ContentHashMismatchError(
            "LOCK 授权的内容指纹与实际文件内容不一致："
            f"LOCK 声明 {declared}，磁盘实际 {actual}；文件 {file_path}。"
            "拒绝把未授权/被替换的内容送入生成（R3）。"
            "如确需替换内容，请创建新版本并重新审核取得新 LOCK。",
            asset_id=(locked or {}).get("asset_id"),
            version=(locked or {}).get("version"),
            lock_id=(locked or {}).get("lock_id"),
            declared_hash=declared,
            actual_hash=actual,
            file_path=str(file_path),
        )
    return actual


def _build_version(raw: dict) -> AssetVersion:
    known = {f.name for f in dc_fields(AssetVersion)}
    return AssetVersion(**{k: v for k, v in raw.items() if k in known})


def _build_entry(raw: dict) -> AssetEntry:
    known = {f.name for f in dc_fields(AssetEntry)}
    payload = {k: v for k, v in raw.items() if k in known}
    payload["versions"] = [_build_version(v) for v in payload.get("versions", [])]
    return AssetEntry(**payload)


class AssetStore:
    """项目素材库。所有写操作都是「加锁 + 原子写 + 只追加」。"""

    def __init__(self, project: ProjectPaths | Path | str, *, timeout_s: float = _DEFAULT_LOCK_TIMEOUT_S) -> None:
        self.paths = coerce_project_paths(project)
        self.timeout_s = float(timeout_s)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.paths.meta_json

    @property
    def project_id(self) -> str:
        return self.paths.project_id

    def _emit(self, event: str, **kwargs: Any) -> None:
        """写事件日志。任何异常都不得阻断素材库写入（契约第 10 节）。"""
        try:
            append_event(self.paths, event=event, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 日志绝不阻断主流程
            logger.warning("写入事件日志失败（已忽略）：%s (%s)", event, exc)

    def load_index(self) -> dict:
        """持锁读取素材索引；文件**确实不存在**时返回空索引。

        索引里的 ``project_id`` 必须与当前项目一致（R5）；条目结构用
        ``AssetEntry`` 反序列化校验（非法枚举值 / 非法 content_hash 一律抛错）。

        ⚠️ 「文件不存在」与「文件存在但损坏」必须严格区分：

        * 文件不存在 -> 空索引（首次写入前的正常状态）；
        * **文件存在但为 0 字节** -> 抛 ``CorruptFileError``。

        为什么 0 字节要单独处理：``read_json`` 把「空内容」统一映射为
        ``default=None``，于是 0 字节的 meta.json 会伪装成「文件不存在」，
        最终让 ``resolve_lock`` 静默返回 ``None`` —— 也就是把
        **「索引损坏」误报成「该版本未审核」**。调用方据此拒绝生成，表面无害，
        但真实索引已损坏却无人察觉，且下一次写入会把它永久覆盖掉。
        """
        exists = self.index_path.exists()
        with file_lock(lock_path_for(self.index_path), timeout_s=self.timeout_s):
            raw = read_json(self.index_path, default=None)
        if raw is None and exists:
            size = self.index_path.stat().st_size if self.index_path.exists() else -1
            raise CorruptFileError(
                "meta.json 存在但内容为空/无法解析（可能被截断或写入中断），"
                "拒绝按空素材库继续：这会让『索引损坏』被误报成『所有版本都未审核』",
                path=str(self.index_path),
                size_bytes=size,
            )
        return self._validate_index(raw)

    def _validate_index(self, raw: dict | None) -> dict:
        if raw is None:
            return empty_index(self.paths.project_id)
        if not isinstance(raw, dict):
            raise CorruptFileError("meta.json 顶层必须是对象", path=str(self.index_path))
        if not raw and not self.index_path.exists():
            # 文件确实不存在（首次写入），按空索引处理；
            # 但「文件存在却是空对象」属于损坏，下面会报错，绝不当成空库继续。
            return empty_index(self.paths.project_id)
        if not raw:
            raise CorruptFileError(
                "meta.json 是空对象（可能被截断/损坏），拒绝按空素材库继续",
                path=str(self.index_path),
            )
        stored_pid = raw.get("project_id")
        if stored_pid != self.paths.project_id:
            raise CorruptFileError(
                "meta.json 的 project_id 与当前项目不一致（R5 项目隔离，拒绝串用）",
                path=str(self.index_path),
                expected=self.paths.project_id,
                actual=stored_pid,
            )
        assets = raw.get("assets")
        if not isinstance(assets, list):
            raise CorruptFileError(
                "meta.json 的 assets 必须是数组", path=str(self.index_path)
            )
        for item in assets:
            if not isinstance(item, dict):
                raise CorruptFileError("素材条目必须是对象", path=str(self.index_path))
            try:
                _build_entry(item)
            except (ValueError, TypeError) as exc:
                raise CorruptFileError(
                    f"素材条目不符合 models.entities.AssetEntry 约束：{exc}",
                    path=str(self.index_path),
                    asset_id=item.get("asset_id"),
                ) from exc
        return raw

    def _mutate_index(self, mutator: Callable[[dict], dict]) -> dict:
        """唯一允许的索引写入路径：加锁 + 深拷贝 + 原子写 + 版本号自增。"""

        def _wrapper(index: dict) -> dict:
            index = self._validate_index(index)
            result = mutator(index)
            if result is None:
                raise StoreError("索引 mutator 必须返回 dict")
            result["schema_version"] = INDEX_SCHEMA_VERSION
            result["project_id"] = self.paths.project_id
            result["revision"] = int(result.get("revision", 0)) + 1
            result["updated_at"] = now_iso()
            return result

        return locked_update_json(self.index_path, _wrapper, timeout_s=self.timeout_s)

    def locked_update_index(self, mutator: Callable[[dict], dict]) -> dict:
        """给同层模块提供的受锁索引更新入口（仍然是同一条写入路径）。

        调用方拿到的 dict 是当前索引的深拷贝，必须返回新的 dict。
        """
        return self._mutate_index(mutator)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @staticmethod
    def _find(index: dict, asset_id: str) -> dict | None:
        for item in index.get("assets", []):
            if item.get("asset_id") == asset_id:
                return item
        return None

    def _require(self, index: dict, asset_id: str) -> dict:
        asset = self._find(index, asset_id)
        if asset is None:
            raise AssetNotFoundError(
                "素材不存在", asset_id=asset_id, project_id=self.paths.project_id
            )
        return asset

    @staticmethod
    def _require_version(asset: dict, version: Any) -> dict:
        wanted = format_version(version)
        for item in asset.get("versions", []):
            if item.get("version") == wanted:
                return item
        raise VersionNotFoundError(
            "素材版本不存在", asset_id=asset.get("asset_id"), version=wanted
        )

    def _scope_filter(self, scope: Any) -> str | None:
        if scope is None:
            return None
        return parse_enum(LibraryScope, scope, kind="library_scope").value

    def list_assets(self, *, asset_type: Any = None, scope: Any = None) -> list[dict]:
        """列出素材条目（确定性排序：创建时间 -> asset_id）。"""
        type_value = (
            None if asset_type is None else parse_enum(AssetType, asset_type, kind="asset_type").value
        )
        scope_value = self._scope_filter(scope)
        index = self.load_index()
        out = [
            item
            for item in index["assets"]
            if (type_value is None or item.get("asset_type") == type_value)
            and (scope_value is None or item.get("library_scope") == scope_value)
        ]
        out.sort(key=lambda item: (item.get("created_at") or "", item.get("asset_id") or ""))
        return out

    def get_asset(self, asset_id: str) -> dict | None:
        index = self.load_index()
        found = self._find(index, asset_id)
        return None if found is None else found

    def find_by_normalized_name(self, normalized: Any, *, scope: Any = None) -> list[dict]:
        """按归一化名查找素材，**可能返回 0 / 1 / N 个**。

        返回多个 -> 调用方据此判 ``SUSPECT_DUPLICATE``（R1：存在性判断只由程序做）。
        查询串与库内 ``normalized_name``/``name`` 都会再过一遍 :func:`index_key`，
        因此全角/半角、大小写、空格、中英标点差异都能命中。
        """
        key = index_key(normalized)
        if not key:
            raise StoreError("find_by_normalized_name 的查询名不能为空", value=repr(normalized))
        scope_value = self._scope_filter(scope)
        index = self.load_index()
        matches = []
        for item in index["assets"]:
            if scope_value is not None and item.get("library_scope") != scope_value:
                continue
            if key in (index_key(item.get("normalized_name")), index_key(item.get("name"))):
                matches.append(item)
        matches.sort(key=lambda item: (item.get("created_at") or "", item.get("asset_id") or ""))
        return matches

    def get_version(self, asset_id: str, version: Any) -> dict | None:
        index = self.load_index()
        asset = self._find(index, asset_id)
        if asset is None:
            return None
        wanted = format_version(version)
        for item in asset.get("versions", []):
            if item.get("version") == wanted:
                return item
        return None

    def locked_versions(self, asset_id: str) -> list[str]:
        index = self.load_index()
        asset = self._find(index, asset_id)
        if asset is None:
            raise AssetNotFoundError("素材不存在", asset_id=asset_id)
        return [
            v["version"]
            for v in asset.get("versions", [])
            if v.get("lock_status") == LockStatus.LOCKED.value
        ]

    def latest_locked_version(self, asset_id: str) -> str | None:
        locked = self.locked_versions(asset_id)
        if not locked:
            return None
        return max(locked, key=version_sort_key)

    # ------------------------------------------------------------------
    # 命名规则（图一 SG2 G2「素材类型_序号_名称」）
    # ------------------------------------------------------------------
    #: 素材类型 -> 中文前缀（用于人可读命名，不改磁盘 ID）。
    _TYPE_CN = {
        "CHARACTER": "角色", "SCENE": "场景", "PROP": "物品",
        "SHOT": "分镜", "AUDIO": "音频", "VIDEO": "视频",
    }

    def human_label(self, asset_id: str) -> str:
        """返回人可读命名 `类型_序号_名称`（图一 SG2 G2）。

        ## 为什么不改 asset_id 本身

        sset_id 是**磁盘目录名 + 引用键**（`assets/<type>/<asset_id>/`）。
        改它的格式会让**已有项目全部失效**（目录找不到、引用对不上、LOCK 失效）——
        那是破坏性迁移，代价远大于收益。

        所以：**磁盘 ID 保持稳定，展示层用可读命名**。
        界面上看到的是 `角色_1_林秀`，实际引用仍是 `xxx_character_1_ab12cd34`。
        两者由本方法确定性地互相对应。
        """
        index = self.load_index()
        asset = self._require(index, asset_id)
        type_value = str(asset.get("asset_type") or "")
        prefix = self._TYPE_CN.get(type_value, type_value or "素材")
        # 从 asset_id 里解析序号：{project}_{type}_{seq}_{uuid}
        seq = ""
        for token in str(asset_id).split("_"):
            if token.isdigit():
                seq = token
                break
        name = str(asset.get("name") or "")
        parts = [prefix] + ([seq] if seq else []) + ([name] if name else [])
        return "_".join(parts)

    def human_labels(self) -> dict[str, str]:
        """一次取全部素材的 sset_id -> 可读命名（列表/界面用）。"""
        return {
            str(item.get("asset_id")): self.human_label(str(item.get("asset_id")))
            for item in self.list_assets()
        }

    def references_of(self, asset_id: str) -> list[dict]:
        index = self.load_index()
        asset = self._require(index, asset_id)
        return list(asset.get("references", []))

    def referencing_segments(
        self, asset_id: str, *, version: Any = None, only_failed: Any = None
    ) -> list[dict]:
        """反查「**哪些段引用了这个素材**」。返回引用记录（含 `segment_id`）。

        这是图四 `F2B`「仅重跑依赖该角色且未通过的段」与图一 SG8
        「中程失败：仅重跑依赖该素材且未通过的段」所依赖的**反向索引**。

        * `version=None` -> 该素材的所有版本；
        * `version` 给定 -> 只查这个版本；
        * `only_failed`：可选的判定函数 `(reference_record) -> bool`。
          **注意**：段是否通过存在**段**上（`Segment.review_status`），
          不在素材引用记录里。所以这里只做「素材 → 段」这一维；
          过滤「未通过的段」由调用方拿 `segment_id` 去段存储里查 ——
          两边职责分开，避免这里偷偷持有段状态。
        """
        index = self.load_index()
        asset = self._require(index, asset_id)
        wanted = format_version(version) if version is not None else None
        out: list[dict] = []
        for ref in asset.get("references", []) or []:
            if not isinstance(ref, dict):
                continue
            if wanted is not None and str(ref.get("version")) != wanted:
                continue
            if only_failed is not None and not only_failed(ref):
                continue
            out.append(dict(ref))
        return out

    def reference_count(self, asset_id: str, *, distinct_segments: bool = True) -> int:
        """「引用次数」（图七 `GF` 契约字段）。

        `distinct_segments=True`（默认）按**不同段**计，同一段重复引用同一素材只算一次
        —— 否则一个段写两次引用就会让"引用次数"虚高。
        """
        refs = self.referencing_segments(asset_id)
        if not distinct_segments:
            return len(refs)
        return len({str(r.get("segment_id")) for r in refs if r.get("segment_id")})

    # ------------------------------------------------------------------
    # 写入：素材条目
    # ------------------------------------------------------------------

    def _project_display_name(self) -> str:
        try:
            data = read_json(self.paths.project_json, default={}, require_dict=True)
        except StoreError:
            return "project"
        name = data.get("display_name") or data.get("project_name")
        return str(name) if name else "project"

    def create_asset(
        self,
        *,
        asset_type: Any,
        name: str,
        source: Any,
        library_scope: Any,
        normalized_name: str | None = None,
        note: str = "",
        per_class_limit: int | None = None,
    ) -> dict:
        """新建素材条目（无版本）。``asset_id`` 由 :func:`store.ids.new_asset_id` 生成。

        ``per_class_limit``：**每类素材数量上限**（图一 SG2 `G4`
        「数量上限每类默认最多9张可调」）。

        * 传 ``None``（默认）-> 不限制；
        * 传正整数 -> 该类已有数量达到上限时抛 :class:`StoreError`。

        ## 为什么由调用方传而不是自己去读配置

        `AssetStore` 只认项目路径，**不持有全局配置**（避免把配置语义漏进存储层）。
        调用方（`demo.py` / 插件节点）从 `cfg.assets.per_class_limit` 取值传进来。

        此前这个配置项**配了但没有任何代码读它** —— 实测连续造出 12 个同类素材
        无一被拦（ROUND18）。所以上限校验放在**唯一创建入口**上。
        """
        type_enum = parse_enum(AssetType, asset_type, kind="asset_type")
        source_enum = parse_enum(AssetSource, source, kind="source")
        scope_enum = parse_enum(LibraryScope, library_scope, kind="library_scope")
        if per_class_limit is not None:
            if not isinstance(per_class_limit, int) or isinstance(per_class_limit, bool):
                raise StoreError(
                    "per_class_limit 必须是正整数或 None",
                    per_class_limit=repr(per_class_limit),
                )
            if per_class_limit < 1:
                raise StoreError("per_class_limit 必须 >= 1", per_class_limit=per_class_limit)
        if not isinstance(name, str) or not name.strip():
            raise StoreError("素材名称不能为空", name=repr(name))
        name_text = name.strip()
        if len(name_text) > 200:
            raise StoreError("素材名称过长（>200）", length=len(name_text))
        norm = str(normalized_name).strip() if normalized_name else index_key(name_text)
        if not norm:
            raise StoreError("归一化名不能为空", name=name_text)
        if not isinstance(note, str):
            raise StoreError("note 必须是字符串")

        holder: dict[str, Any] = {}

        def mutator(index: dict) -> dict:
            same_type = [
                item for item in index["assets"] if item.get("asset_type") == type_enum.value
            ]
            # 图一 SG2 G4：每类数量上限。放在 mutator 里（持索引锁）判断，
            # 避免并发下两个创建者都读到 n-1 然后一起越过上限。
            if per_class_limit is not None and len(same_type) >= per_class_limit:
                raise StoreError(
                    f"每类素材数量已达上限 {per_class_limit}（当前 {type_enum.value} "
                    f"已有 {len(same_type)} 个）：拒绝新建 {name_text!r}。\n"
                    "上限来自配置 assets.per_class_limit（图一 SG2 G4）。"
                    "要加素材请先删除/归档同类素材，或调大该配置。",
                    asset_type=type_enum.value,
                    per_class_limit=per_class_limit,
                    existing=len(same_type),
                )
            seq = len(same_type) + 1
            existing_ids = {item.get("asset_id") for item in index["assets"]}
            asset_id = new_asset_id(
                self._project_display_name(), type_enum, seq, existing=existing_ids
            )
            entry = AssetEntry(
                asset_id=asset_id,
                asset_type=type_enum,
                name=name_text,
                normalized_name=norm,
                library_scope=scope_enum,
                source=source_enum,
                current_version=None,
                versions=[],
                references=[],
                created_at=now_iso(),
                note=note,
            )
            payload = entry.to_dict()
            asset_dir = self.paths.asset_dir(type_enum, asset_id)
            (asset_dir / "versions").mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.paths.asset_json(type_enum, asset_id), payload)
            index["assets"].append(payload)
            holder["asset_id"] = asset_id
            holder["entry"] = payload
            return index

        index = self._mutate_index(mutator)
        stored = self._require(index, holder["asset_id"])
        self._emit(
            "ASSET_CREATED",
            namespace="BETTERCOMFYUI",
            object_id=stored["asset_id"],
            detail={
                "asset_type": type_enum.value,
                "name": name_text,
                "normalized_name": norm,
                "library_scope": scope_enum.value,
                "source": source_enum.value,
            },
        )
        return stored

    # ------------------------------------------------------------------
    # 写入：版本（只追加）
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_extension(source: Path) -> str:
        suffix = source.suffix.lower()
        if len(suffix) > 1 and suffix[1:].isalnum() and len(suffix) <= 11:
            return suffix
        return ".bin"

    @staticmethod
    def _unified_image(source: Path, longest_edge: int) -> Path | None:
        """把图片按「最长边 = ``longest_edge``」**等比**缩放，返回临时文件路径。

        返回 ``None`` 表示"不需要处理"：

        * 源文件**不是图片**（音频/视频等）—— 不报错，原样入库；
        * 源文件尺寸**已经符合** —— 不必重编码，避免无谓的画质损失。

        对应图一 SG2 `G3`「格式与尺寸：默认统一分辨率，可选自动缩放」。

        ## 为什么要等比缩放而不是强制 1024x1024

        强拉成正方形会把角色/场景拉伸变形，反而破坏"参考图是身份一致性核心"这件事。
        统一的是**最长边**，宽高比保持原样。

        依赖 Pillow（项目里 `fusion.py` / 各脚本已在用）。缺 Pillow 时
        **只在真的要缩放时**报错 —— 不用这个功能的调用方完全不受影响。
        """
        if not isinstance(longest_edge, int) or isinstance(longest_edge, bool) or longest_edge < 32:
            raise StoreError(
                "unify_resolution 必须是 >= 32 的整数（最长边像素）",
                unify_resolution=repr(longest_edge),
            )
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise StoreError(
                "统一素材分辨率需要 Pillow（PIL）。请安装 Pillow，或不要传 unify_resolution。"
            ) from exc

        try:
            with Image.open(source) as im:
                im.load()
                width, height = im.size
        except Exception:  # noqa: BLE001 - 非图片一律按"不处理"放行
            return None

        longest = max(int(width), int(height))
        if longest <= 0:
            return None
        if longest == longest_edge:
            return None

        scale = longest_edge / longest
        new_size = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        suffix = source.suffix or ".png"
        fd, tmp_name = tempfile.mkstemp(suffix=suffix, prefix="bcui_unify_")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with Image.open(source) as im:
                resized = im.convert("RGB") if im.mode in ("P", "CMYK") else im
                resized = resized.resize(new_size, Image.LANCZOS)
                resized.save(tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    @staticmethod
    def _copy_new_file(source: Path, target: Path) -> str:
        """把源文件复制到**尚不存在**的目标路径，返回 content_hash。

        ``O_CREAT|O_EXCL``：目标已存在直接失败 —— 这是 R2「不覆盖」的物理保证。
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(os.fspath(target), flags, 0o600)
        except FileExistsError as exc:
            raise ImmutableAssetError(
                "目标素材文件已存在，拒绝覆盖（R2：素材库只追加）",
                path=str(target),
            ) from exc
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            try:
                os.unlink(os.fspath(target))
            except OSError:
                pass
            raise
        return content_hash_of(target)

    def add_version(
        self,
        asset_id: str,
        *,
        source: Any,
        file_path: Path | str,
        status: Any,
        extra: dict | None = None,
        unify_resolution: int | None = None,
    ) -> dict:
        """追加新版本：``version+1`` + 复制文件 + 计算 content_hash。**绝不覆盖旧版本**。

        ``unify_resolution``：**素材统一分辨率**（图一 SG2 `G3`）。
        给定时把图片按「最长边 = 该值」等比缩放后再入库；非图片/尺寸已符合不处理。
        实际缩放后会写进版本的 ``extra["resolution_unified"]``，可追溯。

        不给（默认 ``None``）时行为与以前完全一致 —— 旧调用方不受影响。
        """
        source_enum = parse_enum(AssetSource, source, kind="source")
        status_enum = parse_enum(AssetStatus, status, kind="status")
        if extra is not None and not isinstance(extra, dict):
            raise StoreError("extra 必须是 dict", actual=type(extra).__name__)
        src = Path(file_path)
        if not src.is_file():
            raise StoreError("素材源文件不存在或不是普通文件", file_path=str(src))

        # 统一分辨率：先算出"实际要入库的文件"，原始文件永不被改（R2）。
        copy_src = src
        unified_path: Path | None = None
        effective_extra = dict(extra or {})
        if unify_resolution is not None:
            unified_path = self._unified_image(src, unify_resolution)
            if unified_path is not None:
                copy_src = unified_path
                effective_extra["resolution_unified"] = int(unify_resolution)
                effective_extra["original_size"] = None  # 下面用 PIL 结果填（拿不到就留 None）
                try:
                    from PIL import Image  # noqa: PLC0415

                    with Image.open(src) as im:
                        effective_extra["original_size"] = list(im.size)
                    with Image.open(copy_src) as im2:
                        effective_extra["unified_size"] = list(im2.size)
                except Exception:  # noqa: BLE001 - 尺寸信息只是附加说明
                    pass

        holder: dict[str, Any] = {}

        def mutator(index: dict) -> dict:
            asset = self._require(index, asset_id)
            type_enum = AssetType.parse(asset["asset_type"])
            versions = list(asset.get("versions", []))
            next_number = max((parse_version(v["version"]) for v in versions), default=0) + 1
            version = format_version(next_number)

            version_dir = self.paths.version_dir(type_enum, asset_id, version)
            if version_dir.exists():
                raise ImmutableAssetError(
                    "版本目录已存在，拒绝覆盖（R2：素材库只追加）",
                    path=str(version_dir),
                    version=version,
                )
            try:
                version_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:  # pragma: no cover - 兜底竞态
                raise ImmutableAssetError(
                    "版本目录已存在（并发创建），拒绝覆盖", path=str(version_dir)
                ) from exc

            target_file = self.paths.version_file(
                type_enum, asset_id, version, f"file{self._safe_extension(copy_src)}"
            )
            if copy_src.resolve() == target_file.resolve():
                raise StoreError(
                    "源文件就是目标版本文件，禁止原地写入（R2）", file_path=str(src)
                )
            digest = self._copy_new_file(copy_src, target_file)

            entry = AssetVersion(
                version=version,
                content_hash=digest,
                file_path=self.paths.relative(target_file),
                status=status_enum,
                lock_status=LockStatus.UNLOCKED,
                lock_source=None,
                review_cycle_id=None,
                review_history=[],
                source=source_enum,
                created_at=now_iso(),
                superseded_by=None,
                extra=dict(effective_extra),
            )
            # 旧版本只记录「被谁取代」，不改状态、不删文件（R2：版本可追溯）
            previous = asset.get("current_version")
            if previous:
                for item in versions:
                    if item.get("version") == previous:
                        item["superseded_by"] = version

            payload = entry.to_dict()
            versions.append(payload)
            asset["versions"] = versions
            asset["current_version"] = version
            atomic_write_json(
                self.paths.version_meta(type_enum, asset_id, version), payload
            )
            atomic_write_json(self.paths.asset_json(type_enum, asset_id), asset)
            holder["version"] = payload
            return index

        try:
            self._mutate_index(mutator)
        finally:
            # 统一分辨率的临时文件用完就删 —— 不留在 temp 里堆积。
            if unified_path is not None:
                unified_path.unlink(missing_ok=True)
        stored = holder["version"]
        self._emit(
            "ASSET_VERSION_ADDED",
            object_id=asset_id,
            version=stored["version"],
            detail={
                "content_hash": stored["content_hash"],
                "status": stored["status"],
                "source": stored["source"],
                "file_path": stored["file_path"],
            },
        )
        return stored

    def _replace_version(self, index: dict, asset: dict, version: str, payload: dict) -> None:
        versions = list(asset.get("versions", []))
        for position, item in enumerate(versions):
            if item.get("version") == version:
                versions[position] = payload
                break
        else:  # pragma: no cover - 调用前已 require
            raise VersionNotFoundError("素材版本不存在", asset_id=asset.get("asset_id"), version=version)
        asset["versions"] = versions

    def _write_version_meta(self, asset: dict, payload: dict) -> None:
        type_enum = AssetType.parse(asset["asset_type"])
        atomic_write_json(
            self.paths.version_meta(type_enum, asset["asset_id"], payload["version"]), payload
        )
        atomic_write_json(self.paths.asset_json(type_enum, asset["asset_id"]), asset)

    def set_version_status(self, asset_id: str, version: Any, status: Any, **fields: Any) -> dict:
        """更新版本状态与附加字段（如 ``review_cycle_id`` / ``score``）。

        追溯字段（``version``/``content_hash``/``file_path``/``asset_id``）**不可改写**；
        未知字段进入 ``AssetVersion.extra``（保证不丢信息）。
        """
        status_enum = parse_enum(AssetStatus, status, kind="status")
        illegal = sorted(set(fields) & STRUCTURAL_VERSION_FIELDS)
        if illegal:
            raise StoreError(
                "禁止改写追溯字段（R2/R3：版本、指纹、文件路径不可变）", fields=illegal
            )
        holder: dict[str, Any] = {}

        def mutator(index: dict) -> dict:
            asset = self._require(index, asset_id)
            current = self._require_version(asset, version)
            known = {f.name for f in dc_fields(AssetVersion)}
            merged = dict(current)
            extra = dict(current.get("extra") or {})
            for key, value in fields.items():
                if key in known:
                    merged[key] = value
                else:
                    extra[key] = value
            merged["status"] = status_enum.value
            merged["extra"] = extra
            entry = _build_version(merged)  # 触发 models 层不变量校验
            payload = {**merged, **entry.to_dict()}
            self._replace_version(index, asset, entry.version, payload)
            self._write_version_meta(asset, payload)
            holder["payload"] = payload
            return index

        self._mutate_index(mutator)
        payload = holder["payload"]
        self._emit(
            "ASSET_VERSION_STATUS_CHANGED",
            object_id=asset_id,
            version=payload["version"],
            mode=None,
            detail={"status": payload["status"], "fields": sorted(fields)},
        )
        return payload

    # ------------------------------------------------------------------
    # LOCK（R3）
    # ------------------------------------------------------------------

    def set_lock(
        self,
        asset_id: str,
        version: Any,
        *,
        lock_source: Any,
        content_hash: str,
        review_cycle_id: str | None = None,
    ) -> dict:
        """给某个**具体版本**写 LOCK。

        * ``content_hash`` 必须同时等于：入参、版本元数据、**磁盘文件实时指纹**；
        * ``review_cycle_id`` 必填 —— ``AssetVersion`` 的不变量要求 LOCKED 版本能
          追溯到授权它的审核周期（否则就是「不知道谁批的」假通过）；
        * 完全相同的 LOCK 重复调用是幂等的：直接返回既有记录，不重写文件
          （下游据此断言「LOCK 只被写入一次」）；
        * 已有 LOCK 且结论不同 -> 抛
          :class:`~bettercomfyui.store.errors.LockAlreadyExistsError`，
          必须先 :meth:`clear_lock` 留痕（R3 第二道防线，绝不静默改写）。
        """
        if review_cycle_id is None or not str(review_cycle_id).strip():
            raise StoreError(
                "set_lock 必须提供 review_cycle_id（关键字参数）："
                "models.entities.AssetVersion 要求 LOCKED 版本必须能追溯到授权它的审核周期；"
                "请从 commit_final 的 cycle 传入 review_cycle_id",
                asset_id=asset_id,
                version=str(version),
            )
        cycle_id = str(review_cycle_id).strip()
        lock_source_enum = parse_enum(LockSource, lock_source, kind="lock_source")
        hash_text = parse_content_hash(content_hash)

        existing = self.get_version(asset_id, version)
        if existing is not None and self._lock_matches(existing, lock_source_enum, hash_text, cycle_id):
            return existing  # 幂等：不产生第二次 LOCK 写入
        if existing is not None and existing.get("lock_status") == LockStatus.LOCKED.value:
            raise self._relock_error(existing, lock_source_enum, cycle_id, asset_id)

        holder: dict[str, Any] = {}

        def mutator(index: dict) -> dict:
            asset = self._require(index, asset_id)
            current = self._require_version(asset, version)
            if current.get("lock_status") == LockStatus.LOCKED.value and not self._lock_matches(
                current, lock_source_enum, hash_text, cycle_id
            ):
                # 与上面同一条规则，但在锁内复核 —— 防止并发下第二个写者绕过
                raise self._relock_error(current, lock_source_enum, cycle_id, asset_id)
            if current.get("content_hash") != hash_text:
                raise ContentHashMismatchError(
                    "LOCK 被拒绝：入参 content_hash 与版本元数据不一致（R3）",
                    asset_id=asset_id,
                    version=current["version"],
                    stored=current.get("content_hash"),
                    given=hash_text,
                )
            disk_file = self.paths.absolute_from_relative(current["file_path"])
            if not disk_file.is_file():
                raise StoreError(
                    "LOCK 被拒绝：版本文件不在磁盘上",
                    asset_id=asset_id,
                    version=current["version"],
                    file_path=str(disk_file),
                )
            actual = content_hash_of(disk_file)
            if actual != hash_text:
                raise ContentHashMismatchError(
                    "LOCK 被拒绝：磁盘文件指纹与声明的 content_hash 不一致（R3，文件可能被篡改）",
                    asset_id=asset_id,
                    version=current["version"],
                    disk=actual,
                    given=hash_text,
                )

            status = (
                AssetStatus.FALLBACK
                if lock_source_enum is LockSource.FALLBACK_PASS
                else AssetStatus.ALLOWED
            )
            extra = dict(current.get("extra") or {})
            extra["lock_id"] = lock_id(asset_id, current["version"])
            extra["locked_at"] = now_iso()
            merged = {
                **current,
                "lock_status": LockStatus.LOCKED.value,
                "lock_source": lock_source_enum.value,
                "review_cycle_id": cycle_id,
                "status": status.value,
                "extra": extra,
            }
            if cycle_id not in (merged.get("review_history") or []):
                merged["review_history"] = [
                    *(merged.get("review_history") or []),
                    cycle_id,
                ]
            entry = _build_version(merged)
            payload = {**merged, **entry.to_dict()}
            self._replace_version(index, asset, entry.version, payload)
            self._write_version_meta(asset, payload)
            holder["payload"] = payload
            return index

        self._mutate_index(mutator)
        payload = holder["payload"]
        self._emit(
            "ASSET_LOCK_SET",
            object_id=asset_id,
            version=payload["version"],
            detail={
                "lock_id": payload.get("extra", {}).get("lock_id"),
                "lock_source": payload.get("lock_source"),
                "content_hash": payload.get("content_hash"),
                "review_cycle_id": cycle_id,
            },
        )
        return payload

    @staticmethod
    def _relock_error(
        version_entry: dict,
        lock_source: LockSource,
        cycle_id: str,
        asset_id: str | None = None,
    ) -> LockAlreadyExistsError:
        return LockAlreadyExistsError(
            "该版本已存在 LOCK，拒绝用不同的审核结论静默改写（R3 第二道防线）："
            "如需按新审核周期重新加锁，必须先 clear_lock(asset_id, version, reason=...) 留痕",
            asset_id=asset_id or version_entry.get("asset_id"),
            version=version_entry.get("version"),
            existing_lock_source=version_entry.get("lock_source"),
            existing_review_cycle_id=version_entry.get("review_cycle_id"),
            incoming_lock_source=lock_source.value,
            incoming_review_cycle_id=cycle_id,
        )

    @staticmethod
    def _lock_matches(
        version_entry: dict, lock_source: LockSource, content_hash: str, cycle_id: str
    ) -> bool:
        return (
            version_entry.get("lock_status") == LockStatus.LOCKED.value
            and version_entry.get("lock_source") == lock_source.value
            and version_entry.get("content_hash") == content_hash
            and version_entry.get("review_cycle_id") == cycle_id
        )

    def clear_lock(self, asset_id: str, version: Any, *, reason: str | None = None) -> dict:
        """撤销某版本的 LOCK。审核历史保留在 ``review_cycle_id`` / ``review_history``。"""
        holder: dict[str, Any] = {}

        def mutator(index: dict) -> dict:
            asset = self._require(index, asset_id)
            current = self._require_version(asset, version)
            extra = dict(current.get("extra") or {})
            extra["unlocked_at"] = now_iso()
            if reason:
                extra["unlock_reason"] = str(reason)
            merged = {
                **current,
                "lock_status": LockStatus.UNLOCKED.value,
                "lock_source": None,
                "extra": extra,
            }
            entry = _build_version(merged)
            payload = {**merged, **entry.to_dict()}
            self._replace_version(index, asset, entry.version, payload)
            self._write_version_meta(asset, payload)
            holder["payload"] = payload
            return index

        self._mutate_index(mutator)
        payload = holder["payload"]
        self._emit(
            "ASSET_LOCK_CLEARED",
            object_id=asset_id,
            version=payload["version"],
            severity="WARN",
            detail={"reason": reason},
        )
        return payload

    def resolve_lock(self, asset_id: str, version: Any) -> dict | None:
        """生成侧唯一门禁（R3）：返回 LOCK 记录，或 ``None`` 表示**不可用于生成**。

        **判据一：``None`` = 「没有可授权的 LOCK 记录」（正常拒绝，不是故障）**

        1. 索引里没有该素材 / 没有该版本（没有记录）；
        2. ``lock_status != LOCKED``（未审核、未上锁、已 ``clear_lock``）——最常见的门禁拒绝；
        3. ``lock_source`` 或 ``review_cycle_id`` 缺失（记录不构成授权）；
        4. ``content_hash`` 与版本元数据不一致；
        5. 版本文件不在磁盘上（文件丢失 -> 该版本不可用，既有语义见
           ``tests/test_store_assets.py::test_resolve_lock_none_when_file_missing``）。

        **判据二：「读不出来」绝不返回 ``None``，一律抛 :class:`StoreError`**

        索引读取/解析失败、``content_hash`` 无法解析、``file_path`` 越界（R5 违规 ->
        :class:`~bettercomfyui.store.errors.PathIsolationError`）都属于**技术故障**。
        把它们降级成「未审核」会让调用方无法区分
        「正常拒绝」与「读失败」—— 这正是任务书要防的「技术失败被当成正常结果」。
        抛出时保留原始异常类型与 ``code``，并在 ``context["error_class"]`` 标注
        :class:`~bettercomfyui.models.enums.ErrorClass` （``FILE_ENCODING``），
        同时写 WARN 日志说明是「读取失败」而非「未审核」。

        日志口径（写清是哪一种）：读失败 = WARN（异常）；未 LOCK = INFO（正常门禁决定）；
        索引里没有该素材/版本 = DEBUG（常见探测）。**故意不把正常拒绝记成 WARN**：
        该门禁在匹配/校验热路径上按版本逐个调用，正常拒绝记 WARN 会淹没真正的异常。
        """
        try:
            index = self.load_index()
        except StoreError as exc:
            self._annotate_read_failure(
                asset_id, version, "读取素材索引失败（索引损坏/被占用/项目不符）", exc
            )
            raise
        asset = self._find(index, asset_id)
        if asset is None:
            logger.debug(
                "resolve_lock：索引中没有该素材，按「不可用于生成」拒绝：asset_id=%s", asset_id
            )
            return None
        wanted = format_version(version)
        entry = None
        for item in asset.get("versions", []):
            if item.get("version") == wanted:
                entry = item
                break
        if entry is None:
            logger.debug(
                "resolve_lock：素材没有该版本，按「不可用于生成」拒绝：asset_id=%s version=%s",
                asset_id,
                wanted,
            )
            return None
        if entry.get("lock_status") != LockStatus.LOCKED.value:
            logger.info(
                "resolve_lock：该版本没有 LOCK 记录（未审核/未上锁），按「不可用于生成」拒绝"
                "（这是正常门禁，不是读取失败）：asset_id=%s version=%s lock_status=%s",
                asset_id,
                wanted,
                entry.get("lock_status"),
            )
            return None
        lock_source = entry.get("lock_source")
        cycle_id = entry.get("review_cycle_id")
        if not lock_source or not cycle_id:
            logger.warning(
                "resolve_lock：LOCK 记录不自洽（缺 lock_source/review_cycle_id），"
                "按「不可用于生成」拒绝（这是记录问题，不是读取失败）："
                "asset_id=%s version=%s",
                asset_id,
                wanted,
            )
            return None
        try:
            hash_text = parse_content_hash(entry.get("content_hash"))
        except StoreError as exc:
            self._annotate_read_failure(
                asset_id, version, "版本元数据的 content_hash 无法解析", exc
            )
            raise
        if hash_text != entry.get("content_hash"):
            return None
        try:
            disk_file = self.paths.absolute_from_relative(entry.get("file_path"))
        except StoreError as exc:
            self._annotate_read_failure(
                asset_id, version, "版本元数据的 file_path 无法解析（路径越界 R5）", exc
            )
            raise
        if not disk_file.is_file():
            logger.warning(
                "resolve_lock：版本文件不在磁盘上，按「不可用于生成」拒绝"
                "（文件丢失 = 该版本不可用，这不是读取失败）：asset_id=%s version=%s path=%s",
                asset_id,
                wanted,
                disk_file,
            )
            return None
        extra = entry.get("extra") or {}
        return {
            "lock_id": extra.get("lock_id") or lock_id(asset_id, wanted),
            "asset_id": asset_id,
            "version": wanted,
            "content_hash": hash_text,
            "lock_source": lock_source,
            "review_cycle_id": cycle_id,
            "status": entry.get("status"),
            "file_path": entry.get("file_path"),
            "absolute_path": str(disk_file),
            "locked_at": extra.get("locked_at"),
        }

    @staticmethod
    def _annotate_read_failure(
        asset_id: str, version: Any, what: str, exc: StoreError
    ) -> None:
        """给「读取失败」异常补可分类上下文并留 WARN（与 ``None`` = 未授权严格区分）。

        只补不覆盖：``setdefault`` 保证原始上下文优先；异常类型与 ``code`` 原样保留，
        因此调用方既能按 :class:`StoreError` 统一捕获，也能按 ``code``/``error_class``
        判定技术失败类别（任务书第 6 节：技术失败与质量不合格必须分开）。
        """
        exc.context.setdefault("error_class", ErrorClass.FILE_ENCODING)
        exc.context.setdefault("asset_id", asset_id)
        exc.context.setdefault("version", str(version))
        exc.context.setdefault("operation", "resolve_lock")
        logger.warning(
            "resolve_lock 读取失败（%s —— 这是技术故障，不是「未审核」）："
            "asset_id=%s version=%s -> %s: %s",
            what,
            asset_id,
            version,
            type(exc).__name__,
            exc,
        )

    # ------------------------------------------------------------------
    # 引用关系
    # ------------------------------------------------------------------

    def record_reference(self, asset_id: str, version: Any, ref: dict) -> None:
        """记录「哪个镜头/视频段引用了哪个素材版本」。同一引用重复记录是幂等的。"""
        if not isinstance(ref, dict) or not ref:
            raise StoreError("ref 必须是非空 dict", actual=type(ref).__name__)
        wanted = format_version(version)

        def mutator(index: dict) -> dict:
            asset = self._require(index, asset_id)
            self._require_version(asset, wanted)
            references = list(asset.get("references", []))
            payload = {
                **ref,
                "asset_id": asset_id,
                "version": wanted,
                "recorded_at": now_iso(),
            }
            for existing in references:
                same = all(existing.get(k) == payload.get(k) for k in ref)
                if same and existing.get("version") == wanted:
                    return index  # 幂等，不重复追加
            references.append(payload)
            asset["references"] = references
            type_enum = AssetType.parse(asset["asset_type"])
            atomic_write_json(self.paths.asset_json(type_enum, asset_id), asset)
            return index

        self._mutate_index(mutator)
        self._emit(
            "ASSET_REFERENCE_RECORDED",
            object_id=asset_id,
            version=wanted,
            detail={k: v for k, v in ref.items() if isinstance(v, (str, int, float, bool, type(None)))},
        )

    # ------------------------------------------------------------------
    # 疑似重复（独立文件 suspects.json，绝不触碰 meta.json）
    # ------------------------------------------------------------------

    @property
    def suspects_path(self) -> Path:
        return self.paths.suspects_json

    def _suspects_doc(self, raw: dict | None) -> dict:
        if raw is None:
            return self._fresh_suspects_doc()
        if not isinstance(raw, dict):
            raise CorruptFileError(
                "suspects.json 顶层必须是对象", path=str(self.suspects_path)
            )
        if not raw and not self.suspects_path.exists():
            # locked_update_json 对「文件不存在」传空 dict：这是首次写入的合法情况；
            # 但「文件存在却是空对象」属于损坏，下面会报错。
            return self._fresh_suspects_doc()
        if not isinstance(raw.get("suspects"), list):
            raise CorruptFileError(
                "suspects.json 结构非法（缺少 suspects 数组）", path=str(self.suspects_path)
            )
        if raw.get("project_id") != self.paths.project_id:
            raise CorruptFileError(
                "suspects.json 的 project_id 与当前项目不一致（R5）",
                path=str(self.suspects_path),
                expected=self.paths.project_id,
                actual=raw.get("project_id"),
            )
        return raw

    def _fresh_suspects_doc(self) -> dict:
        return {
            "schema_version": SUSPECT_SCHEMA_VERSION,
            "project_id": self.paths.project_id,
            "revision": 0,
            "updated_at": now_iso(),
            "suspects": [],
        }

    def list_suspects(self, *, resolved: bool | None = None) -> list[dict]:
        with file_lock(lock_path_for(self.suspects_path), timeout_s=self.timeout_s):
            raw = read_json(self.suspects_path, default=None)
        doc = self._suspects_doc(raw)
        items = doc["suspects"]
        if resolved is None:
            return list(items)
        return [item for item in items if bool(item.get("resolved")) is bool(resolved)]

    def suspect_ids(self) -> set[str]:
        return {str(item.get("suspect_id")) for item in self.list_suspects()}

    def record_suspect(
        self,
        record: dict | None = None,
        *,
        suspect_id: str | None = None,
        name: str | None = None,
        normalized_name: str | None = None,
        asset_type: Any = None,
        library_scope: Any = None,
        candidates: Iterable[Any] | None = None,
        reason: str | None = None,
        **fields: Any,
    ) -> dict:
        """落盘一条 SUSPECT_* 记录（**禁止静默丢弃**，R1 的 SUSPECT 分支）。

        兼容三种调用形态：``record_suspect(payload_dict)`` / 关键字 / 混合。
        ``candidates`` 可传 asset_id 字符串列表，也可传 asset dict 列表
        （会抽出 ``asset_id``，原始 dict 存进 ``candidate_details`` 以免丢信息）。
        """
        merged: dict[str, Any] = {}
        if record is not None:
            if not isinstance(record, dict):
                raise StoreError("record 必须是 dict", actual=type(record).__name__)
            merged.update(record)
        if name is not None:
            merged["name"] = name
        if normalized_name is not None:
            merged["normalized_name"] = normalized_name
        if asset_type is not None:
            merged["asset_type"] = asset_type
        if library_scope is not None:
            merged["library_scope"] = library_scope
        if candidates is not None:
            merged["candidates"] = list(candidates)
        if reason is not None:
            merged["reason"] = reason
        if suspect_id is not None:
            merged["suspect_id"] = suspect_id
        merged.update(fields)

        display_name = merged.get("name") or merged.get("candidate_name")
        norm_value = merged.get("normalized_name") or index_key(display_name)
        type_value = merged.get("asset_type") or merged.get("candidate_type")
        if not norm_value:
            raise StoreError("record_suspect 需要 normalized_name（或可归一化的 name）")
        if type_value is None:
            raise StoreError("record_suspect 需要 asset_type（或 candidate_type）")
        type_enum = parse_enum(AssetType, type_value, kind="asset_type")

        raw_candidates = list(merged.get("candidates") or [])
        candidate_ids: list[str] = []
        candidate_details: list[dict] = []
        for item in raw_candidates:
            if isinstance(item, dict):
                candidate_details.append(item)
                value = item.get("asset_id")
                if value:
                    candidate_ids.append(str(value))
            elif isinstance(item, str):
                candidate_ids.append(item)
            else:
                raise StoreError(
                    "candidates 元素必须是 asset_id 字符串或 asset dict",
                    actual=type(item).__name__,
                )

        scope_value = merged.get("library_scope")
        if scope_value is not None:
            scope_value = parse_enum(LibraryScope, scope_value, kind="library_scope").value

        holder: dict[str, Any] = {}

        def mutator(doc: dict) -> dict:
            doc = self._suspects_doc(doc)
            existing_ids = {str(item.get("suspect_id")) for item in doc["suspects"]}
            final_id = merged.get("suspect_id")
            if final_id is None:
                final_id = new_suspect_id(
                    norm_value, existing=existing_ids, max_attempts=64
                )
            base = SuspectRecord(
                suspect_id=str(final_id),
                normalized_name=str(norm_value),
                candidate_name=str(display_name or norm_value),
                candidate_type=type_enum,
                candidates=candidate_ids,
                reason=str(merged.get("reason") or ""),
                resolved=bool(merged.get("resolved", False)),
                resolution=merged.get("resolution"),
                created_at=merged.get("created_at") or now_iso(),
            )
            payload = base.to_dict()
            if scope_value is not None:
                payload["library_scope"] = scope_value
            if candidate_details:
                payload["candidate_details"] = candidate_details
            extras = {
                k: v
                for k, v in merged.items()
                if k
                not in {
                    "suspect_id",
                    "normalized_name",
                    "name",
                    "candidate_name",
                    "asset_type",
                    "candidate_type",
                    "candidates",
                    "reason",
                    "resolved",
                    "resolution",
                    "created_at",
                    "library_scope",
                }
            }
            if extras:
                payload["extra"] = extras
            payload["updated_at"] = now_iso()
            doc["suspects"].append(payload)
            doc["revision"] = int(doc.get("revision", 0)) + 1
            doc["updated_at"] = now_iso()
            doc["schema_version"] = SUSPECT_SCHEMA_VERSION
            doc["project_id"] = self.paths.project_id
            doc["suspects"] = doc["suspects"]
            holder["payload"] = payload
            return doc

        locked_update_json(self.suspects_path, mutator, timeout_s=self.timeout_s)
        payload = holder["payload"]
        self._emit(
            "SUSPECT_RECORDED",
            namespace="REVIEW",
            object_id=payload.get("suspect_id"),
            detail={
                "normalized_name": payload.get("normalized_name"),
                "candidate_type": payload.get("candidate_type"),
                "matches": candidate_ids,
                "reason": payload.get("reason"),
            },
        )
        return payload

    def resolve_suspect(
        self, suspect_id: str, *, resolution: str, resolved_by: str | None = None
    ) -> dict:
        """标记疑似记录已确认（``resolution``：reuse / create_new / discarded）。"""
        if resolution not in {"reuse", "create_new", "discarded"}:
            raise StoreError(
                "resolution 必须是 reuse / create_new / discarded", resolution=resolution
            )
        holder: dict[str, Any] = {}

        def mutator(doc: dict) -> dict:
            doc = self._suspects_doc(doc)
            for item in doc["suspects"]:
                if str(item.get("suspect_id")) == suspect_id:
                    item["resolved"] = True
                    item["resolution"] = resolution
                    item["resolved_at"] = now_iso()
                    if resolved_by:
                        item["resolved_by"] = resolved_by
                    holder["payload"] = dict(item)
                    break
            else:
                raise StoreError("疑似记录不存在", suspect_id=suspect_id)
            doc["revision"] = int(doc.get("revision", 0)) + 1
            doc["updated_at"] = now_iso()
            return doc

        locked_update_json(self.suspects_path, mutator, timeout_s=self.timeout_s)
        payload = holder["payload"]
        self._emit(
            "SUSPECT_RESOLVED",
            namespace="REVIEW",
            object_id=suspect_id,
            detail={"resolution": resolution, "resolved_by": resolved_by},
        )
        return payload

    # ------------------------------------------------------------------
    # 只读辅助（不是契约的一部分，供同层模块与测试使用）
    # ------------------------------------------------------------------

    def version_files(self, asset_id: str) -> list[Path]:
        """返回该素材所有版本的**实际文件路径**（按版本号升序，用于 R2 自检）。"""
        index = self.load_index()
        asset = self._require(index, asset_id)
        out: list[Path] = []
        for item in sorted(asset.get("versions", []), key=lambda v: version_sort_key(v["version"])):
            out.append(self.paths.absolute_from_relative(item["file_path"]))
        return out
