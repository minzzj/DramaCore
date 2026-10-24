"""review 层测试替身与工具（不是生产代码）。

`FakeAssetStore` 是 CONTRACTS §4 `AssetStore` 的**线程安全测试替身**：
- 真的把 `versions/<v>/meta.json` 写到磁盘，使「迟到结果不得改变版本状态」可以做**字节断言**；
- 记录 `set_lock` / `set_version_status` 调用次数，使「LOCK 恰好写入 1 次」「其余提交无副作用」
  可以被直接断言；
- `set_lock` 语义与 store-project 已确认的实现保持一致：相同
  `(lock_source, content_hash, review_cycle_id)` 幂等；结论不同则拒绝。

注意：本文件不实现 `record_suspect/list_suspects`（那是 prompt-assets 的替身职责）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bettercomfyui.models.entities import LOCAL_TZ
from bettercomfyui.review import AssetReviewer, ReviewEventLog
from bettercomfyui.review.notify import CallableChannel
from bettercomfyui.review.counters import CounterStore


class FakeStoreError(Exception):
    """替身自身的错误（例如 content_hash 不一致）。"""


def sha256_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeClock:
    """确定性后端时钟：返回 +08:00 的 ISO8601（与 models.entities.now_iso 同格式）。"""

    def __init__(self, start: str = "2026-10-16T10:00:00+08:00") -> None:
        self._t = datetime.fromisoformat(start)
        if self._t.tzinfo is None:  # pragma: no cover - 防御
            self._t = self._t.replace(tzinfo=LOCAL_TZ)

    def __call__(self) -> str:
        return self._t.isoformat(timespec="seconds")

    @property
    def now(self) -> str:
        return self()

    def advance(self, seconds: float) -> str:
        self._t = self._t + timedelta(seconds=float(seconds))
        return self()


class FakeAssetStore:
    """契约 §4 的最小可用、线程安全测试替身（真实落盘 meta.json）。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="bcui_fake_store_"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._asset_seq = 0
        #: 每个替身实例独立的随机后缀：避免不同测试/不同 store 生成相同 asset_id
        #: （asset_id 相同 → review_cycle_id 相同 → 进程内 ledger 会跨测试串味）
        self._uid = uuid.uuid4().hex[:8]
        self.calls: dict[str, int] = {}
        self.set_lock_calls: list[dict] = []
        self.set_status_calls: list[dict] = []

    # -- 路径 ---------------------------------------------------------------
    def asset_dir(self, asset_id: str) -> Path:
        return self.root / "assets" / asset_id

    def meta_path(self, asset_id: str, version: str) -> Path:
        return self.asset_dir(asset_id) / "versions" / version / "meta.json"

    def read_meta_bytes(self, asset_id: str, version: str) -> bytes:
        path = self.meta_path(asset_id, version)
        return path.read_bytes() if path.exists() else b""

    def read_meta(self, asset_id: str, version: str) -> dict | None:
        path = self.meta_path(asset_id, version)
        if not path.exists():
            return None
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def _write_meta(self, asset_id: str, version: str, data: dict) -> None:
        path = self.meta_path(asset_id, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # -- 素材 ---------------------------------------------------------------
    def create_asset(self, *, asset_type: str, name: str, source: str = "AI",
                     library_scope: str = "PROJECT") -> dict:
        with self._lock:
            self._asset_seq += 1
            asset_id = f"{name}_{asset_type}_{self._asset_seq}_{self._uid}"
        self.asset_dir(asset_id).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.calls["create_asset"] = self.calls.get("create_asset", 0) + 1
        return {"asset_id": asset_id, "asset_type": asset_type, "name": name,
                "source": source, "library_scope": library_scope, "current_version": None}

    def add_version(self, asset_id: str, *, source: str = "AI", file_path: str | Path | None = None,
                    payload: bytes | None = None, status: str = "DRAFT", extra: dict | None = None) -> dict:
        with self._lock:
            self.calls["add_version"] = self.calls.get("add_version", 0) + 1
            versions_dir = self.asset_dir(asset_id) / "versions"
            existing = [p.name for p in versions_dir.iterdir() if p.is_dir()] if versions_dir.exists() else []
            index = len(existing) + 1
            version = f"v{index}"
        if payload is None:
            payload = f"{asset_id}-{version}-payload".encode("utf-8")
        content_hash = sha256_of(payload)
        version_dir = self.asset_dir(asset_id) / "versions" / version
        version_dir.mkdir(parents=True, exist_ok=True)
        blob = version_dir / "file.bin"
        blob.write_bytes(payload)
        meta = {
            "asset_id": asset_id,
            "version": version,
            "content_hash": content_hash,
            "file_path": str(file_path) if file_path is not None else str(blob),
            "status": str(status),
            "lock_status": "UNLOCKED",
            "lock_source": None,
            "review_cycle_id": None,
            "source": source,
            "extra": dict(extra or {}),
        }
        if file_path is not None:
            meta["declared_file_path"] = str(file_path)
        with self._lock:
            self._write_meta(asset_id, version, meta)
        return meta

    def get_version(self, asset_id: str, version: str) -> dict | None:
        with self._lock:
            self.calls["get_version"] = self.calls.get("get_version", 0) + 1
        return self.read_meta(asset_id, version)

    def set_version_status(self, asset_id: str, version: str, status: str, **fields: Any) -> dict:
        with self._lock:
            self.calls["set_version_status"] = self.calls.get("set_version_status", 0) + 1
            for forbidden in ("version", "content_hash", "file_path"):
                if forbidden in fields:
                    raise FakeStoreError(f"set_version_status 禁止改写 {forbidden}")
            meta = self.read_meta(asset_id, version)
            if meta is None:
                raise FakeStoreError(f"未知版本：{asset_id} {version}")
            meta["status"] = str(status)
            meta.update({k: (v.value if hasattr(v, "value") else v) for k, v in fields.items()})
            self._write_meta(asset_id, version, meta)
            self.set_status_calls.append({"asset_id": asset_id, "version": version,
                                          "status": str(status), "fields": dict(fields)})
        return meta

    def set_lock(self, asset_id: str, version: str, *, lock_source: Any, content_hash: str,
                 review_cycle_id: str | None = None, allow_unverified_disk: bool = False) -> dict:
        source = lock_source.value if hasattr(lock_source, "value") else str(lock_source)
        with self._lock:
            self.calls["set_lock"] = self.calls.get("set_lock", 0) + 1
            meta = self.read_meta(asset_id, version)
            if meta is None:
                raise FakeStoreError(f"未知版本：{asset_id} {version}")
            if meta.get("content_hash") != content_hash:
                raise FakeStoreError(
                    f"content_hash 不一致：meta={meta.get('content_hash')} 入参={content_hash}"
                )
            if not review_cycle_id:
                raise FakeStoreError("set_lock 必须携带 review_cycle_id（models.AssetVersion 硬约束）")
            if meta.get("lock_status") == "LOCKED":
                same = (
                    meta.get("lock_source") == source
                    and meta.get("content_hash") == content_hash
                    and meta.get("review_cycle_id") == review_cycle_id
                )
                if same:
                    return dict(meta)  # 幂等：不重写文件
                raise FakeStoreError("该版本已有不同结论的 LOCK；需先 clear_lock 并留痕")
            meta["lock_status"] = "LOCKED"
            meta["lock_source"] = source
            meta["content_hash"] = content_hash
            meta["review_cycle_id"] = review_cycle_id
            meta["lock_id"] = f"LOCK_{asset_id}_{version}"
            meta["locked_at"] = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
            self._write_meta(asset_id, version, meta)
            self.set_lock_calls.append({
                "asset_id": asset_id, "version": version, "lock_source": source,
                "content_hash": content_hash, "review_cycle_id": review_cycle_id,
            })
        return meta

    def clear_lock(self, asset_id: str, version: str, *, reason: str | None = None) -> dict:
        with self._lock:
            meta = self.read_meta(asset_id, version)
            if meta is None:
                raise FakeStoreError(f"未知版本：{asset_id} {version}")
            meta.update({"lock_status": "UNLOCKED", "lock_source": None,
                         "review_cycle_id": None, "clear_reason": reason})
            self._write_meta(asset_id, version, meta)
        return meta

    def resolve_lock(self, asset_id: str, version: str) -> dict | None:
        meta = self.read_meta(asset_id, version)
        if not meta or meta.get("lock_status") != "LOCKED":
            return None
        return meta

    def load_index(self) -> dict:
        return {"assets": {}}

    def call_count(self, name: str) -> int:
        with self._lock:
            return int(self.calls.get(name, 0))

    def snapshot_calls(self) -> dict:
        with self._lock:
            return dict(self.calls)


class FailingSetLockStore(FakeAssetStore):
    """`set_lock` 永远失败（用于验证「claim 被释放，可以安全重试」）。"""

    def set_lock(self, asset_id: str, version: str, *, lock_source: Any, content_hash: str,
                 review_cycle_id: str | None = None, allow_unverified_disk: bool = False) -> dict:
        with self._lock:
            self.calls["set_lock"] = self.calls.get("set_lock", 0) + 1
        raise FakeStoreError("模拟 LOCK 写入失败（磁盘/权限）")


class FailingStatusAfterLockStore(FakeAssetStore):
    """`set_lock` 成功但 `set_version_status` 失败（验证保留 claim、不重复写 LOCK）。"""

    def set_version_status(self, asset_id: str, version: str, status: str, **fields: Any) -> dict:
        with self._lock:
            self.calls["set_version_status"] = self.calls.get("set_version_status", 0) + 1
        if str(status) in ("ALLOWED", "FALLBACK"):
            raise FakeStoreError("模拟状态写入失败")
        return super().set_version_status(asset_id, version, status, **fields)


class ScriptedModel:
    """按脚本返回模型审核结果；脚本项可以是 dict / Exception / callable。"""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self, material: dict) -> dict:
        self.calls += 1
        item = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(material)
        return dict(item)


class RecordingChannel:
    def __init__(self, name: str = "REC", fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[dict] = []

    def send(self, *, title: str, message: str, severity: str = "INFO", detail: dict | None = None) -> None:
        if self.fail:
            raise RuntimeError(f"模拟通知渠道 {self.name} 失败")
        self.sent.append({"title": title, "message": message, "severity": severity, "detail": detail or {}})


def recording_channel(name: str = "REC", fail: bool = False) -> CallableChannel:
    channel = RecordingChannel(name=name, fail=fail)
    return CallableChannel(name=name, send_fn=channel.send)


def make_material(payload: bytes = b"review-payload", **extra: Any) -> dict:
    material = {"file_path": "assets/x/versions/v1/file.bin", "content_hash": sha256_of(payload)}
    material.update(extra)
    return material


def make_reviewer(store: FakeAssetStore | None = None, *, clock: FakeClock | None = None, **kwargs: Any) -> AssetReviewer:
    """默认：内存事件日志 + 进程内 ledger + 确定性时钟（测试不依赖真实时间/网络）。"""
    from bettercomfyui.review.commit import FinalizationLedger

    store = store if store is not None else FakeAssetStore()
    clock = clock if clock is not None else FakeClock()
    kwargs.setdefault("config", {"model_candidate_limit": 3, "score_threshold": 70.0})
    kwargs.setdefault("ledger", FinalizationLedger(None))
    kwargs.setdefault("event_log", ReviewEventLog(project=None, clock=clock))
    reviewer = AssetReviewer(store, clock=clock, **kwargs)
    reviewer._test_clock = clock  # type: ignore[attr-defined]
    return reviewer


def seed_version(store: FakeAssetStore, name: str = "hero", payload: bytes | None = None) -> tuple[str, dict]:
    asset = store.create_asset(asset_type="CHARACTER", name=name)
    version = store.add_version(asset["asset_id"], payload=payload or f"{name}-v1".encode())
    return asset["asset_id"], version


__all__ = [
    "FakeClock",
    "FakeAssetStore",
    "FakeStoreError",
    "FailingSetLockStore",
    "FailingStatusAfterLockStore",
    "ScriptedModel",
    "RecordingChannel",
    "recording_channel",
    "CounterStore",
    "sha256_of",
    "make_material",
    "make_reviewer",
    "seed_version",
]
