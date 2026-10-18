"""store 底层对抗性测试：确定性 JSON / 原子写 / 文件锁 / 无丢失更新。

这些用例是 task-2 的验收核心之一，**故意写成「破坏实现就会变红」**：

* :func:`test_two_processes_200_versions_exactly` —— 两个真实进程各做 100 次
  read-modify-write，若 ``locked_update_json`` 未在整个周期持锁，必然丢更新；
* :func:`test_concurrent_atomic_write_never_half_written` —— 两个进程绕过锁直接
  ``atomic_write_json`` 同一文件，读方**永远**只能看到完整内容之一。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_store_support import assert_child_ok, run_child, spawn_child  # noqa: E402

from bettercomfyui.models.enums import AssetType  # noqa: E402
from bettercomfyui.store import (  # noqa: E402
    CorruptFileError,
    FileLock,
    LockTimeoutError,
    SerializationError,
    StoreError,
    atomic_write_json,
    canonical_json,
    file_lock,
    locked_update_json,
    lock_path_for,
    now_iso,
    read_json,
)


# ---------------------------------------------------------------------------
# canonical_json
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_same_object_identical_bytes(self):
        payload = {
            "b": [1, 2, {"x": None}],
            "a": "中文名称",
            "f": 1.5,
            "s": {"k": True},
            "p": Path("assets/character/x/file.png"),
        }
        first = canonical_json(payload).encode("utf-8")
        for _ in range(50):
            assert canonical_json(payload).encode("utf-8") == first

    def test_key_order_and_construction_order_do_not_matter(self):
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_chinese_is_not_escaped_and_roundtrips(self):
        text = canonical_json({"名称": "张三·咖啡馆", "类型": "CHARACTER"})
        assert "\\u" not in text, "ensure_ascii=False 失效：中文被转义"
        assert "张三·咖啡馆" in text
        assert json.loads(text) == {"名称": "张三·咖啡馆", "类型": "CHARACTER"}
        assert not text.encode("utf-8").startswith(b"\xef\xbb\xbf")

    def test_negative_zero_normalized(self):
        assert canonical_json({"z": -0.0}) == '{"z":0.0}'

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_inf_rejected(self, bad):
        with pytest.raises(SerializationError):
            canonical_json({"x": bad})
        with pytest.raises(SerializationError):
            canonical_json({"outer": [{"x": bad}]})

    def test_unsupported_type_rejected(self):
        with pytest.raises(SerializationError):
            canonical_json({"x": object()})

    def test_key_collision_rejected(self):
        # 1 与 "1" 归一化后同名，若静默覆盖就会丢数据
        with pytest.raises(SerializationError):
            canonical_json({1: "a", "1": "b"})

    def test_cycle_rejected(self):
        payload: dict = {"a": 1}
        payload["self"] = payload
        with pytest.raises(SerializationError):
            canonical_json(payload)

    def test_enum_path_datetime_uuid_set_supported(self):
        text = canonical_json(
            {
                "type": AssetType.CHARACTER,
                "path": Path("a/b.png"),
                "ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "uuid": UUID("12345678-1234-5678-1234-567812345678"),
                "set": {3, 1, 2},
                "tuple": (1, 2),
            }
        )
        data = json.loads(text)
        assert data["type"] == "CHARACTER"
        assert data["path"] == "a/b.png"
        assert data["ts"] == "2026-01-01T00:00:00+00:00"
        assert data["uuid"] == "12345678-1234-5678-1234-567812345678"
        assert data["set"] == [1, 2, 3]
        assert data["tuple"] == [1, 2]

    def test_idempotent_on_own_output(self):
        payload = {"b": {"d": 2, "c": "中文"}, "a": [1, {"z": None}]}
        once = canonical_json(payload)
        assert canonical_json(json.loads(once)) == once

    def test_now_iso_is_backend_timestamp_with_offset(self):
        text = now_iso()
        assert "T" in text and ("+" in text or text.endswith("Z")), text
        parsed = datetime.fromisoformat(text)
        assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# atomic_write_json
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_write_and_no_tmp_left(self, tmp_path):
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"rev": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"rev": 1}
        assert list(tmp_path.glob("*.tmp")) == []
        assert not (tmp_path / "meta.json.tmp").exists()

    def test_fsync_failure_keeps_old_content(self, tmp_path, monkeypatch):
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"rev": 1})
        before = target.read_bytes()

        def boom(fd):  # noqa: ARG001
            raise OSError("模拟 tmp 阶段 fsync 失败")

        monkeypatch.setattr(os, "fsync", boom)
        with pytest.raises(OSError):
            atomic_write_json(target, {"rev": 2})
        assert target.read_bytes() == before, "失败时正式文件必须保持旧内容"
        assert list(tmp_path.glob("*.tmp")) == [], "失败时必须清理临时文件"

    def test_replace_failure_keeps_old_content(self, tmp_path, monkeypatch):
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"rev": 1})
        before = target.read_bytes()

        def boom(src, dst):  # noqa: ARG001
            raise OSError("模拟 os.replace 失败")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_json(target, {"rev": 2})
        assert target.read_bytes() == before
        assert list(tmp_path.glob("*.tmp")) == []

    def test_replace_retries_windows_sharing_violation(self, tmp_path, monkeypatch):
        """Windows 上目标被读方占用会让 os.replace 报 PermissionError，必须重试。"""
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"rev": 1})
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky)
        atomic_write_json(target, {"rev": 2})
        assert calls["n"] == 4, "应在共享冲突后重试并最终成功"
        assert json.loads(target.read_text(encoding="utf-8")) == {"rev": 2}

    def test_serialization_failure_touches_nothing(self, tmp_path):
        target = tmp_path / "meta.json"
        target.write_bytes(b'{"old":1}')
        with pytest.raises(SerializationError):
            atomic_write_json(target, {"x": float("nan")})
        assert target.read_bytes() == b'{"old":1}'
        assert list(tmp_path.glob("*.tmp")) == []

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "meta.json"
        atomic_write_json(target, {"ok": True})
        assert read_json(target) == {"ok": True}


class TestReadJson:
    def test_retries_through_windows_sharing_violation(self, tmp_path, monkeypatch):
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"rev": 3})
        real_read = Path.read_bytes
        calls = {"n": 0}

        def flaky(self_path):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(13, "Permission denied")
            return real_read(self_path)

        monkeypatch.setattr(Path, "read_bytes", flaky)
        assert read_json(target) == {"rev": 3}
        assert calls["n"] == 3

    def test_missing_file_raises_without_default(self, tmp_path):
        with pytest.raises(StoreError):
            read_json(tmp_path / "nope.json")

    def test_missing_file_returns_default_copy(self, tmp_path):
        default = {"assets": []}
        first = read_json(tmp_path / "nope.json", default)
        assert first == default
        first["assets"].append("mutated")
        assert read_json(tmp_path / "nope.json", default) == {"assets": []}

    def test_bom_tolerated(self, tmp_path):
        target = tmp_path / "bom.json"
        target.write_bytes("\ufeff{\"a\":1}".encode("utf-8"))
        assert read_json(target) == {"a": 1}


# ---------------------------------------------------------------------------
# locked_update_json
# ---------------------------------------------------------------------------


class TestLockedUpdateJson:
    def test_sequential_updates_all_survive(self, tmp_path):
        target = tmp_path / "meta.json"
        for _ in range(20):
            locked_update_json(target, lambda data: {**data, "n": data.get("n", 0) + 1})
        assert read_json(target)["n"] == 20

    def test_mutator_returning_none_is_rejected(self, tmp_path):
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"n": 1})
        before = target.read_bytes()
        with pytest.raises(StoreError):
            locked_update_json(target, lambda data: None)
        assert target.read_bytes() == before

    def test_mutator_returning_non_dict_is_rejected(self, tmp_path):
        target = tmp_path / "meta.json"
        with pytest.raises(StoreError):
            locked_update_json(target, lambda data: ["not", "a", "dict"])

    def test_mutator_exception_leaves_file_unchanged(self, tmp_path):
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"n": 1})
        before = target.read_bytes()

        def boom(data):
            raise RuntimeError("业务逻辑炸了")

        with pytest.raises(RuntimeError):
            locked_update_json(target, boom)
        assert target.read_bytes() == before

    def test_missing_file_starts_from_empty(self, tmp_path):
        target = tmp_path / "meta.json"
        result = locked_update_json(target, lambda data: {**data, "created": True})
        assert result["created"] is True
        assert read_json(target) == {"created": True}

    def test_corrupt_file_is_rejected_and_untouched(self, tmp_path):
        target = tmp_path / "meta.json"
        target.write_bytes(b"{not-json")
        before = target.read_bytes()
        with pytest.raises(CorruptFileError):
            locked_update_json(target, lambda data: {"n": 1})
        assert target.read_bytes() == before

    def test_lock_file_naming(self, tmp_path):
        target = tmp_path / "meta.json"
        locked_update_json(target, lambda data: data)
        assert (tmp_path / ".meta.json.lock").exists()
        assert lock_path_for(target).name == ".meta.json.lock"

    def test_mutator_receives_deep_copy(self, tmp_path):
        """mutator 就地改了自己的入参也不应影响后续逻辑（防别名 bug）。"""
        target = tmp_path / "meta.json"
        atomic_write_json(target, {"items": [1]})

        def mutator(data):
            data["items"].append(2)
            return data

        locked_update_json(target, mutator)
        assert read_json(target)["items"] == [1, 2]


# ---------------------------------------------------------------------------
# file_lock
# ---------------------------------------------------------------------------


class TestFileLock:
    def test_reentrant_same_thread(self, tmp_path):
        lock_path = tmp_path / "a.lock"
        with file_lock(lock_path):
            with file_lock(lock_path):
                with file_lock(lock_path):
                    assert lock_path.exists()

    def test_same_instance_double_acquire_rejected(self, tmp_path):
        lock = FileLock(tmp_path / "a.lock")
        lock.acquire()
        try:
            with pytest.raises(StoreError):
                lock.acquire()
        finally:
            lock.release()
        # 释放后可再次获取
        with file_lock(tmp_path / "a.lock", timeout_s=1.0):
            pass

    def test_timeout_when_held_by_other_thread(self, tmp_path):
        lock_path = tmp_path / "a.lock"
        ready = threading.Event()
        release = threading.Event()

        def holder():
            with file_lock(lock_path):
                ready.set()
                release.wait(10)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        assert ready.wait(10)
        try:
            started = time.monotonic()
            with pytest.raises(LockTimeoutError):
                with file_lock(lock_path, timeout_s=0.2):
                    pass
            assert time.monotonic() - started >= 0.15
        finally:
            release.set()
            thread.join(10)

    def test_cross_process_exclusion(self, tmp_path):
        lock_path = tmp_path / "c.lock"
        code = (
            "import sys, time\n"
            "from bettercomfyui.store.lockfile import file_lock\n"
            "with file_lock(sys.argv[1]):\n"
            "    print('LOCKED', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n"
        )
        proc = spawn_child(code, str(lock_path), "1.5")
        line = proc.stdout.readline()
        if "LOCKED" not in line:
            out, err = proc.communicate(timeout=30)
            raise AssertionError(f"子进程未能持锁：{line!r}\n{out}\n{err}")
        try:
            with pytest.raises(LockTimeoutError):
                with file_lock(lock_path, timeout_s=0.25):
                    pass
        finally:
            _, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, err
        # 子进程退出后锁必须自动释放（进程崩溃/退出不能永久锁死）
        with file_lock(lock_path, timeout_s=2.0):
            pass


# ---------------------------------------------------------------------------
# 无丢失更新（验收硬指标）
# ---------------------------------------------------------------------------

_WORKER_APPEND = """
import sys, time
from pathlib import Path
from bettercomfyui.store.atomic import locked_update_json

path = Path(sys.argv[1]); tag = sys.argv[2]; count = int(sys.argv[3])

def mutator(data):
    time.sleep(0.002)          # 放大 read-modify-write 窗口：没有锁必然丢更新
    data.setdefault("versions", []).append(tag)
    return data

for _ in range(count):
    locked_update_json(path, mutator, timeout_s=120)
"""


def test_two_processes_200_versions_exactly(tmp_path):
    """两个并发进程各加 100 个版本 -> 最终必须**恰好** 200 个，无丢失更新。"""
    meta = tmp_path / "meta.json"
    procs = [spawn_child(_WORKER_APPEND, str(meta), tag, "100") for tag in ("A", "B")]
    results = [proc.communicate(timeout=600) for proc in procs]
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, f"worker 失败：\n{out}\n{err}"

    data = json.loads(meta.read_text(encoding="utf-8"))
    versions = data["versions"]
    assert len(versions) == 200, f"丢失更新：期望 200，实际 {len(versions)}"
    assert versions.count("A") == 100
    assert versions.count("B") == 100
    assert list(tmp_path.glob("*.tmp")) == [], "不应残留临时文件"


_WORKER_ATOMIC = """
import sys
from pathlib import Path
from bettercomfyui.store.atomic import atomic_write_json

path = Path(sys.argv[1]); tag = sys.argv[2]; count = int(sys.argv[3])
payload = tag * 200000
for index in range(count):
    atomic_write_json(path, {"tag": tag, "index": index, "payload": payload}, fsync=False)
"""

#: 并发原子写用例的载荷规模（两个进程各写 WRITES 次，每次 200KB）
_ATOMIC_WRITES = 25
_PAYLOAD_MULTIPLIER = 200000


def _allowed_record(tag: str, index: int) -> dict:
    return {"tag": tag, "index": index, "payload": tag * _PAYLOAD_MULTIPLIER}


def test_concurrent_atomic_write_never_half_written(tmp_path):
    """两个进程**绕过** ``locked_update_json`` 直接并发原子写同一目标（Lead 硬要求）。

    这是「原子写」的正式验收用例，不依赖文件锁：

    * 读方（本进程）在竞争期间持续读取，**每一次**读到的都必须是某次写入的
      **完整内容**（合法 JSON + 载荷逐字节完整），绝不允许截断/半写/混合；
    * 写入结束后，正式文件必须**恰好等于** 两个写者之一的最后一次完整写入
      （用允许集合断言，而不是只检查长度）；
    * **反真空**：必须真的观察到写者的写入（否则这个用例什么都没证明）；
    * 不留临时文件。

    变异证据：把 ``atomic_write_json`` 改成原地两段写（无 tmp + 无 ``os.replace``）时，
    本用例报「读到非法 JSON（长度 100017）：Unterminated string」并变红。
    """
    target = tmp_path / "shared.json"
    atomic_write_json(
        target,
        {"tag": "INIT", "index": -1, "payload": "INIT" * _PAYLOAD_MULTIPLIER},
    )
    procs = [spawn_child(_WORKER_ATOMIC, str(target), tag, str(_ATOMIC_WRITES)) for tag in ("A", "B")]

    bad: list[str] = []
    observed: set[tuple[str, int]] = set()
    observations = 0
    deadline = time.monotonic() + 120
    while any(proc.poll() is None for proc in procs) and time.monotonic() < deadline:
        # 读方自己也要容忍 Windows 换名瞬间的共享冲突（产品侧 read_json 已带重试）
        raw = None
        for _attempt in range(50):
            try:
                raw = target.read_bytes()
                break
            except PermissionError:
                time.sleep(0.005)
            except FileNotFoundError as exc:  # 原子换名绝不允许让文件「消失」
                bad.append(f"读取时文件不存在：{exc}")
                break
        if bad:
            break
        assert raw is not None, "读方持续拿不到文件（应已超时重试）"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            bad.append(f"读到非法 JSON（长度 {len(raw)}）：{exc}")
            break
        tag = str(data.get("tag", ""))
        if data.get("payload") != tag * _PAYLOAD_MULTIPLIER:
            bad.append(
                f"读到半写内容：tag={tag} payload_len={len(data.get('payload') or '')}"
            )
            break
        observed.add((tag, int(data.get("index", -1))))
        observations += 1

    outs = [proc.communicate(timeout=180) for proc in procs]
    for proc, (out, err) in zip(procs, outs):
        assert proc.returncode == 0, f"{out}\n{err}"
    assert bad == [], bad
    assert observations > 0, "至少应观察到一次完整内容"
    # 反真空：必须读到过真正由写者写下的内容（否则竞争根本没发生）
    worker_observations = {item for item in observed if item[0] in {"A", "B"}}
    assert worker_observations, f"未观察到任何写者的写入，用例退化：observed={sorted(observed)}"

    # 正式文件必须恰好是「两个写者之一的最后一次完整写入」
    allowed_final = {
        ("A", _ATOMIC_WRITES - 1),
        ("B", _ATOMIC_WRITES - 1),
    }
    final = json.loads(target.read_text(encoding="utf-8"))
    assert (final["tag"], final["index"]) in allowed_final, (
        f"最终内容不是任何一次完整写入：{(final['tag'], final['index'])}"
    )
    assert final == _allowed_record(final["tag"], _ATOMIC_WRITES - 1)
    # 竞争期间读到的 (tag, index) 也必须全部来自允许集合（写者只会写这些记录）
    allowed_any = {
        (tag, index) for tag in ("A", "B") for index in range(_ATOMIC_WRITES)
    }
    allowed_any.add(("INIT", -1))
    unexpected = set(observed) - allowed_any
    assert not unexpected, f"观察到不属于任何合法写入的记录：{sorted(unexpected)}"
    assert list(tmp_path.glob("*.tmp")) == [], "不应残留临时文件"
