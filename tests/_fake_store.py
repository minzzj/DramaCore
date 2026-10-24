"""``AssetStore`` 契约测试替身（CONTRACTS §4 + 疑似重复钩子）。

用途：让 prompt/review/director 的单元测试不依赖真实 store 的完整装配
（真实 store 需要项目目录、锁、事件日志等）。

重要：**它是替身，不是被验证对象**。测试里必须至少有一条走真实
``bettercomfyui.store`` 的集成用例（见 ``tests/test_prompt_store_integration.py``），
否则只能证明替身自洽。替身与真实 store 的一致性由该集成测试断言
（``index_key`` 语义、字段名、SUSPECT 独立文件）。

与真实 store 对齐的关键点（读 ``store/assets.py`` 后确认）：

* 索引匹配用 ``index_key``（NFKC + casefold + 去空白 + **忽略标点**），
  因此「仅差标点」在真实 store 上是**精确匹配**，不是疑似重复。
* 疑似记录写到**独立** ``suspects.json``（``meta.json`` 字节不变）；
  记录字段名与真实 store 一致：``candidate_name`` / ``candidate_type`` /
  ``candidates``（asset_id 字符串列表）/ ``candidate_details``。
* ``record_suspect`` 幂等：同一 (normalized_name, candidate_name, candidates) 不重复追加。
* ``resolve_suspect`` 的 ``resolution`` 只接受 reuse / create_new / discarded。
* ``set_lock`` 校验 content_hash 与磁盘一致；``resolve_lock`` 只认 LOCKED（R3）。

维护者：teammate ``prompt-assets``。其他任务请通过子类扩展，不要直接覆写本文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "FakeProjectPaths",
    "FakeAssetStore",
    "make_store",
    "make_two_projects",
    "canonical_bytes",
    "sha256_of",
    "index_key",
]

# --- 与 store/assets.py 对齐的标点表 -------------------------------------
_PUNCT_MAP = {
    "。": ".",
    "、": ",",
    "，": ",",
    "；": ";",
    "：": ":",
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
_IGNORED_CHARS = set(".,:;!?'\"()[]{}<>·-—_~`@#$%^&*+=|\\/")

_SUSPECT_RESOLUTIONS = frozenset({"reuse", "create_new", "discarded"})


def index_key(text: Any) -> str:
    """store 侧保守归一化（与 ``store.assets.index_key`` 语义一致）。"""
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


def canonical_bytes(obj: Any) -> bytes:
    """确定性 JSON 字节（与 store/atomic.canonical_json 语义一致）。"""
    text = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": "), allow_nan=False
    )
    return text.encode("utf-8")


def sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text)


class FakeProjectPaths:
    """``store.projects.ProjectPaths`` 的最小替身（真实目录布局）。"""

    def __init__(self, root: Path, project_id: str) -> None:
        self.root = Path(root)          # projects/ 根目录（与真实 ProjectPaths 一致）
        self.project_id = project_id
        self.project_dir = self.root / project_id
        self.meta_json = self.project_dir / "meta.json"
        self.suspects_json = self.project_dir / "suspects.json"
        self.project_json = self.project_dir / "project.json"
        self.config_yaml = self.project_dir / "config.yaml"
        self.assets_dir = self.project_dir / "assets"
        self.chapters_dir = self.project_dir / "chapters"
        self.segments_dir = self.project_dir / "segments"
        self.output_dir = self.project_dir / "output"
        self.checkpoints_dir = self.project_dir / "checkpoints"
        self.logs_dir = self.project_dir / "logs"
        self.events_jsonl = self.logs_dir / "events.jsonl"

    def ensure(self) -> "FakeProjectPaths":
        for path in (
            self.project_dir,
            self.assets_dir,
            self.chapters_dir,
            self.segments_dir,
            self.output_dir,
            self.checkpoints_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


class FakeAssetStore:
    """契约替身：真实文件、真实字节、无缓存。"""

    def __init__(self, paths: FakeProjectPaths) -> None:
        self.paths = paths
        self.paths.ensure()
        if not self.paths.meta_json.exists():
            self._write_index({"project_id": paths.project_id, "revision": 0, "assets": []})

    # -- 内部 IO --------------------------------------------------------
    @staticmethod
    def _atomic_write(path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(canonical_bytes(obj))
        os.replace(tmp, path)

    def _write_index(self, index: dict) -> None:
        self._atomic_write(self.paths.meta_json, index)

    def _read_suspects(self) -> dict:
        if not self.paths.suspects_json.exists():
            return {"suspects": []}
        return json.loads(self.paths.suspects_json.read_text(encoding="utf-8"))

    def _write_suspects(self, data: dict) -> None:
        self._atomic_write(self.paths.suspects_json, data)

    # -- 索引读取 -------------------------------------------------------
    def load_index(self) -> dict:
        return json.loads(self.paths.meta_json.read_text(encoding="utf-8"))

    def _find_asset(self, index: dict, asset_id: str) -> dict | None:
        for asset in index.get("assets", []):
            if asset.get("asset_id") == asset_id:
                return asset
        return None

    def list_assets(self, *, asset_type: Any = None, scope: Any = None) -> list[dict]:
        assets = list(self.load_index().get("assets", []))
        if asset_type is not None:
            wanted = str(getattr(asset_type, "value", asset_type))
            assets = [a for a in assets if str(a.get("asset_type")) == wanted]
        if scope is not None:
            wanted_scope = str(getattr(scope, "value", scope))
            assets = [a for a in assets if str(a.get("library_scope")) == wanted_scope]
        return assets

    def find_by_normalized_name(self, normalized: Any, *, scope: Any = None) -> list[dict]:
        key = index_key(normalized)
        if not key:
            raise ValueError("find_by_normalized_name 的查询名不能为空")
        out: list[dict] = []
        for asset in self.list_assets(scope=scope):
            if key in (index_key(asset.get("normalized_name")), index_key(asset.get("name"))):
                out.append(asset)
        return out

    # -- 写入 -----------------------------------------------------------
    def create_asset(
        self,
        *,
        asset_type: Any,
        name: str,
        source: Any = "AI",
        library_scope: Any = "PROJECT",
        normalized_name: str | None = None,
        note: str = "",
    ) -> dict:
        index = self.load_index()
        type_value = str(getattr(asset_type, "value", asset_type))
        asset_id = (
            f"{self.paths.project_id[:8]}_{type_value.lower()}_"
            f"{len(index['assets']) + 1:03d}_{uuid.uuid4().hex[:8]}"
        )
        asset = {
            "asset_id": asset_id,
            "asset_type": type_value,
            "name": name.strip(),
            "normalized_name": (normalized_name or index_key(name)).strip(),
            "library_scope": str(getattr(library_scope, "value", library_scope)),
            "source": str(getattr(source, "value", source)),
            "status": "DRAFT",
            "current_version": None,
            "versions": [],
            "created_at": _now(),
            "note": note,
        }
        index["assets"].append(asset)
        index["revision"] = int(index.get("revision", 0)) + 1
        self._write_index(index)
        return asset

    def add_version(
        self,
        asset_id: str,
        *,
        source: Any = None,
        file_path: Any = None,
        status: Any = "DRAFT",
        extra: Mapping[str, Any] | None = None,
        file_bytes: bytes | None = None,
    ) -> dict:
        index = self.load_index()
        asset = self._find_asset(index, asset_id)
        if asset is None:
            raise KeyError(f"asset_id 不存在：{asset_id}")
        version = f"v{len(asset['versions']) + 1}"
        target_dir = (
            self.paths.assets_dir / str(asset["asset_type"]).lower() / asset_id / "versions" / version
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "file.png"
        if file_bytes is not None:
            target.write_bytes(file_bytes)
        elif file_path is not None:
            target.write_bytes(Path(file_path).read_bytes())
        else:
            target.write_bytes(f"{asset_id}:{version}".encode("utf-8"))
        record = {
            "version": version,
            "file_path": str(target),
            "content_hash": sha256_of(target),
            "status": str(getattr(status, "value", status)),
            "source": str(getattr(source, "value", source)) if source else asset["source"],
            "lock_status": "UNLOCKED",
            "lock_source": None,
            "lock_id": None,
            "review_cycle_id": None,
            "created_at": _now(),
        }
        if extra:
            record.update(dict(extra))
        asset["versions"].append(record)
        asset["current_version"] = version
        index["revision"] = int(index.get("revision", 0)) + 1
        self._write_index(index)
        return record

    def set_version_status(self, asset_id: str, version: str, status: Any, **fields: Any) -> dict:
        index = self.load_index()
        asset = self._find_asset(index, asset_id)
        record = self._require_version(asset, version)
        record["status"] = str(getattr(status, "value", status))
        record.update(fields)
        index["revision"] = int(index.get("revision", 0)) + 1
        self._write_index(index)
        return record

    @staticmethod
    def _require_version(asset: dict | None, version: str) -> dict:
        if asset is None:
            raise KeyError("素材不存在")
        for record in asset["versions"]:
            if record["version"] == version:
                return record
        raise KeyError(f"版本不存在：{asset['asset_id']} {version}")

    def set_lock(
        self,
        asset_id: str,
        version: str,
        *,
        lock_source: Any,
        content_hash: str,
        review_cycle_id: str | None = None,
    ) -> dict:
        index = self.load_index()
        asset = self._find_asset(index, asset_id)
        record = self._require_version(asset, version)
        actual = sha256_of(Path(record["file_path"]))
        if actual != content_hash or record["content_hash"] != content_hash:
            raise ValueError(
                f"content_hash 与磁盘不一致：声明 {content_hash}，实际 {actual}（拒绝写 LOCK）"
            )
        record["lock_status"] = "LOCKED"
        record["lock_source"] = str(getattr(lock_source, "value", lock_source))
        record["lock_id"] = f"LOCK_{asset_id}_{version}"
        record["review_cycle_id"] = review_cycle_id
        index["revision"] = int(index.get("revision", 0)) + 1
        self._write_index(index)
        return record

    def clear_lock(self, asset_id: str, version: str, *, reason: str | None = None) -> dict:
        index = self.load_index()
        asset = self._find_asset(index, asset_id)
        record = self._require_version(asset, version)
        record["lock_status"] = "UNLOCKED"
        record["lock_source"] = None
        record["lock_id"] = None
        index["revision"] = int(index.get("revision", 0)) + 1
        self._write_index(index)
        return record

    def get_version(self, asset_id: str, version: str) -> dict | None:
        asset = self._find_asset(self.load_index(), asset_id)
        if asset is None:
            return None
        for record in asset["versions"]:
            if record["version"] == version:
                return record
        return None

    def resolve_lock(self, asset_id: str, version: str) -> dict | None:
        record = self.get_version(asset_id, version)
        if not record or record.get("lock_status") != "LOCKED":
            return None
        return record

    def references_of(self, asset_id: str) -> list[dict]:
        asset = self._find_asset(self.load_index(), asset_id)
        return list((asset or {}).get("references", []))

    def record_reference(self, asset_id: str, version: str, ref: Any) -> None:
        index = self.load_index()
        asset = self._find_asset(index, asset_id)
        if asset is None:
            raise KeyError(f"asset_id 不存在：{asset_id}")
        asset.setdefault("references", []).append(
            {"version": version, "ref": dict(ref) if isinstance(ref, Mapping) else ref}
        )
        index["revision"] = int(index.get("revision", 0)) + 1
        self._write_index(index)

    # -- 疑似重复（附加钩子，字段名与真实 store 一致） --------------------
    def new_suspect_id(self, normalized_name: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"SUSPECT_{_safe(normalized_name)}_{stamp}_{uuid.uuid4().hex[:8]}"

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
        """幂等：同一 (normalized_name, candidate_name, candidates) 不重复追加。"""
        merged: dict[str, Any] = dict(record or {})
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

        display_name = merged.get("name") or merged.get("candidate_name") or ""
        norm_value = merged.get("normalized_name") or index_key(display_name)
        if not norm_value:
            raise ValueError("record_suspect 需要 normalized_name（或可归一化的 name）")
        type_value = merged.get("asset_type") or merged.get("candidate_type")
        if type_value is None:
            raise ValueError("record_suspect 需要 asset_type（或 candidate_type）")

        candidate_ids: list[str] = []
        candidate_details: list[dict] = []
        for item in list(merged.get("candidates") or []):
            if isinstance(item, Mapping):
                candidate_details.append(dict(item))
                if item.get("asset_id"):
                    candidate_ids.append(str(item["asset_id"]))
            elif isinstance(item, str):
                candidate_ids.append(item)
            else:
                raise ValueError("candidates 元素必须是 asset_id 字符串或 asset dict")

        data = self._read_suspects()
        signature = (
            str(norm_value),
            index_key(display_name),
            json.dumps(candidate_ids, ensure_ascii=False),
        )
        for existing in data["suspects"]:
            other = (
                str(existing.get("normalized_name")),
                index_key(existing.get("candidate_name") or existing.get("name")),
                json.dumps(existing.get("candidates") or [], ensure_ascii=False),
            )
            if other == signature:
                return existing

        payload = {
            "suspect_id": str(merged.get("suspect_id") or self.new_suspect_id(str(norm_value))),
            "normalized_name": str(norm_value),
            "candidate_name": str(display_name),
            "candidate_type": str(getattr(type_value, "value", type_value)),
            "candidates": candidate_ids,
            "reason": str(merged.get("reason") or ""),
            "resolved": bool(merged.get("resolved", False)),
            "resolution": merged.get("resolution"),
            "library_scope": (
                str(getattr(merged["library_scope"], "value", merged["library_scope"]))
                if merged.get("library_scope") is not None
                else None
            ),
            "created_at": merged.get("created_at") or _now(),
        }
        if candidate_details:
            payload["candidate_details"] = candidate_details
        extras = {
            k: v
            for k, v in merged.items()
            if k
            not in {
                "suspect_id", "normalized_name", "name", "candidate_name", "asset_type",
                "candidate_type", "candidates", "reason", "resolved", "resolution",
                "created_at", "library_scope",
            }
        }
        if extras:
            payload["extra"] = extras
        data["suspects"].append(payload)
        self._write_suspects(data)
        return payload

    def list_suspects(self, *, resolved: bool | None = None) -> list[dict]:
        records = list(self._read_suspects()["suspects"])
        if resolved is None:
            return records
        return [r for r in records if bool(r.get("resolved", False)) == bool(resolved)]

    def suspect_ids(self) -> set[str]:
        return {str(r.get("suspect_id")) for r in self.list_suspects()}

    def resolve_suspect(
        self, suspect_id: str, *, resolution: str, resolved_by: str | None = None
    ) -> dict:
        if resolution not in _SUSPECT_RESOLUTIONS:
            raise ValueError("resolution 必须是 reuse / create_new / discarded")
        data = self._read_suspects()
        for record in data["suspects"]:
            if record.get("suspect_id") == suspect_id:
                record["resolved"] = True
                record["resolution"] = resolution
                record["resolved_by"] = resolved_by
                record["resolved_at"] = _now()
                self._write_suspects(data)
                return record
        raise KeyError(f"suspect_id 不存在：{suspect_id}")

    # -- 测试辅助 -------------------------------------------------------
    def meta_bytes(self) -> bytes:
        return self.paths.meta_json.read_bytes()

    def seed_locked_asset(
        self,
        name: str,
        *,
        asset_type: str = "CHARACTER",
        scope: str = "PROJECT",
        lock: bool = True,
        versions: int = 1,
        file_bytes: bytes | None = None,
    ) -> dict:
        """建素材 + 追加 ``versions`` 个版本（最后一个按需 LOCK），返回落盘后的条目。"""
        asset = self.create_asset(
            asset_type=asset_type, name=name, source="AI", library_scope=scope
        )
        record = None
        for i in range(max(1, versions)):
            record = self.add_version(
                asset["asset_id"], file_bytes=file_bytes or f"{name}-v{i + 1}".encode("utf-8")
            )
        if lock and record is not None:
            self.set_lock(
                asset["asset_id"],
                record["version"],
                lock_source="MODEL_PASS",
                content_hash=record["content_hash"],
                review_cycle_id="REVIEW:test:v1",
            )
        for item in self.load_index()["assets"]:
            if item["asset_id"] == asset["asset_id"]:
                return item
        raise AssertionError("素材未落盘")


def make_store(root: Path, project_id: str = "0123456789abcdef0123456789abcdef") -> FakeAssetStore:
    return FakeAssetStore(FakeProjectPaths(Path(root), project_id))


def make_two_projects(root: Path) -> tuple[FakeAssetStore, FakeAssetStore]:
    """两个互相隔离的项目（R5 用）。"""
    return (
        make_store(root, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        make_store(root, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    )
