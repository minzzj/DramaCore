"""MAJOR 发现收敛回归用例（task-7 · teammate `bugfix-majors`）。

本文件只钉一件事：**技术失败绝不能被伪装成正常结果**（任务书第 9 节末句 /
`docs/VERIFY.md` §2.2 的 FP-3、FP-6）。每条用例都写明：

* 发现编号（对应 `docs/VERIFY.md` §4.2 / §4.3）；
* 稳定复现命令；
* 「旧行为」是什么（即：把实现还原成旧行为时，本用例为什么会变红）；
* 新约束（判据）。

复现命令
--------
    python -m pytest tests/test_bugfix_majors.py -q
    python -m pytest "tests/test_bugfix_majors.py::test_resolve_lock_raises_on_unreadable_index_not_none" -q
    python -m pytest "tests/test_bugfix_majors.py::test_poll_queue_query_failure_is_unknown_not_job_lost" -q

边界声明
--------
* 本文件**不**用 `pytest-rerunfailures`、不 `sleep`、不放宽任何断言。
* 「合法返回 None」的既有语义（未 LOCK / 版本不存在 / 文件丢失）单独有用例守住，
  防止把门禁修成「一律抛错」。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _path in (str(_REPO), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_store_support import add_bytes_version, make_asset_store  # noqa: E402

from bettercomfyui.executors.base import (  # noqa: E402
    ExecutorUnavailable,
    JobRef,
)
from bettercomfyui.executors.comfyui_http import (  # noqa: E402
    DEFAULT_BASE_URL,
    ComfyUIHttpExecutor,
)
from bettercomfyui.models.entities import MovieMetadata  # noqa: E402
from bettercomfyui.models.enums import ErrorClass, JobStatus  # noqa: E402
from bettercomfyui.review import events as review_events  # noqa: E402
from bettercomfyui.review.events import ReviewEventLog  # noqa: E402
from bettercomfyui.store.errors import (  # noqa: E402
    PathIsolationError,
    StoreError,
)


# ---------------------------------------------------------------------------
# store.resolve_lock（M-1）
# ---------------------------------------------------------------------------


def _locked_asset(tmp_path: Path, payload: bytes = b"lock-me-bytes"):
    """建一个「已通过审核并 LOCK」的素材版本，返回 (paths, store, asset_id)。"""
    paths, store = make_asset_store(tmp_path)
    asset = store.create_asset(
        asset_type="CHARACTER", name="张三", source="AI", library_scope="PROJECT"
    )
    asset_id = asset["asset_id"]
    version = add_bytes_version(store, asset_id, payload)
    store.set_lock(
        asset_id,
        version["version"],
        lock_source="HUMAN_PASS",
        content_hash=version["content_hash"],
        review_cycle_id=f"ASSET:{asset_id}:{version['version']}",
    )
    return paths, store, asset_id


def test_resolve_lock_raises_on_unreadable_index_not_none(tmp_path):
    """M-1（最重要）：索引读不出来时必须抛 StoreError，不得返回 None。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_resolve_lock_raises_on_unreadable_index_not_none" -q``
    旧行为：``except StoreError: return None`` —— 「文件损坏/半写入/读失败」被伪装成
    「该版本未审核（正常拒绝）」，调用方无法区分`未审核`与`技术故障`（正是任务书要防的假成功）。
    新约束：读/解析失败一律抛 StoreError，且 ``context["error_class"] == ErrorClass.FILE_ENCODING``。
    """
    paths, store, asset_id = _locked_asset(tmp_path)
    assert store.resolve_lock(asset_id, "v1") is not None, "前置条件：该版本已 LOCK 且可用"

    paths.meta_json.write_bytes(b'{ "assets": [ truncated/corrupt')
    with pytest.raises(StoreError) as caught:
        store.resolve_lock(asset_id, "v1")

    assert caught.value.context.get("error_class") == ErrorClass.FILE_ENCODING, (
        "读取失败必须带 ErrorClass.FILE_ENCODING，便于上层按技术失败分类；"
        f"实际 context={caught.value.context!r}"
    )
    assert caught.value.context.get("version") == "v1", (
        f"错误上下文必须能定位版本；实际 context={caught.value.context!r}"
    )
    assert caught.value.context.get("asset_id") == asset_id


def test_resolve_lock_raises_on_path_escape_not_none(tmp_path):
    """M-1 第二例：元数据把素材路径指到项目外（R5 违规）时必须抛错，不得返回 None。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_resolve_lock_raises_on_path_escape_not_none" -q``
    旧行为：``except (PathIsolationError, StoreError): return None`` —— 路径越界（安全事件）
    被伪装成「未审核」，越界证据被静默吞掉。
    新约束：``PathIsolationError``（code=PATH_ISOLATION）必须原样抛出并带 ErrorClass。
    """
    paths, store, asset_id = _locked_asset(tmp_path)
    index = json.loads(paths.meta_json.read_text(encoding="utf-8"))
    version_entry = index["assets"][0]["versions"][0]
    version_entry["file_path"] = str(tmp_path.resolve().parent / "outside-project.bin")
    paths.meta_json.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PathIsolationError) as caught:
        store.resolve_lock(asset_id, "v1")

    assert caught.value.code == "PATH_ISOLATION"
    assert caught.value.context.get("error_class") == ErrorClass.FILE_ENCODING, (
        f"实际 context={caught.value.context!r}"
    )


def test_resolve_lock_still_returns_none_for_normal_refusals(tmp_path):
    """守住既有门禁语义（防止把「读失败抛错」过度修成「一律抛错」）。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_resolve_lock_still_returns_none_for_normal_refusals" -q``
    ``None`` = 「没有可授权的 LOCK 记录」（正常拒绝），必须保留：

    1. 未 LOCK；2. 版本不存在；3. 素材不存在；4. LOCK 后磁盘文件丢失（既有语义）。

    （第 5 类「LOCKED 但缺 lock_source/非法 content_hash」无法用公开 API 构造：
    ``models.entities.AssetVersion.__post_init__`` 与 ``AssetStore._validate_index``
    已经先一步拒绝这类记录，因此它只能作为防御分支存在。）
    """
    paths, store = make_asset_store(tmp_path)
    asset = store.create_asset(
        asset_type="PROP", name="道具", source="AI", library_scope="PROJECT"
    )
    asset_id = asset["asset_id"]
    version = add_bytes_version(store, asset_id, b"payload-bytes")

    assert store.resolve_lock(asset_id, "v1") is None, "未 LOCK 必须 None"
    assert store.resolve_lock(asset_id, "v9") is None, "版本不存在必须 None"
    assert store.resolve_lock("no-such-asset", "v1") is None, "素材不存在必须 None"

    store.set_lock(
        asset_id,
        "v1",
        lock_source="MODEL_PASS",
        content_hash=version["content_hash"],
        review_cycle_id=f"ASSET:{asset_id}:v1",
    )
    assert store.resolve_lock(asset_id, "v1") is not None

    paths.absolute_from_relative(version["file_path"]).unlink()
    assert store.resolve_lock(asset_id, "v1") is None, (
        "文件不在磁盘上 = 该版本不可用于生成（正常拒绝），不是「读失败」，不得改成抛错"
    )


# ---------------------------------------------------------------------------
# executors.comfyui_http poll 归因（M-2）
# ---------------------------------------------------------------------------


class _RouteTransport:
    """最小离线传输层替身：按 (method, path) 返回响应或抛错（测试绝不发真实网络请求）。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    def request(self, method, url, *, json_body=None, timeout=None):  # noqa: ARG002
        path = "/" + url[len(DEFAULT_BASE_URL):].lstrip("/")
        self.calls.append(path)
        return self.handler(method, path)


def _executor(handler, *, poll_max_attempts: int = 3) -> ComfyUIHttpExecutor:
    return ComfyUIHttpExecutor(
        DEFAULT_BASE_URL,
        transport=_RouteTransport(handler),
        poll_max_attempts=poll_max_attempts,
    )


def test_poll_queue_query_failure_is_unknown_not_job_lost():
    """M-2：``/queue`` 查询失败必须体现为「无法判定/可重试」，不得归因成「作业丢失」。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_poll_queue_query_failure_is_unknown_not_job_lost" -q``
    旧行为：``_queue_state`` 把查询异常/HTTP>=400 一律 ``return None``，上层于是把**一次瞬时
    故障**判成「history 与 queue 中都不存在作业 → 作业丢失 → FAILED」，归因失真且不会重试。
    新约束：查询失败 -> ``JobStatus.UNKNOWN``（带 /queue 说明与 error_class）；成功查询且队列
    确实没有该作业 -> 仍然 ``FAILED``（失败闭合不放松）；UNKNOWN 绝不当成功。
    """
    def handler(method, path):  # noqa: ARG001
        if path.startswith("/history/"):
            return (200, {})
        raise ExecutorUnavailable("瞬时 /queue 故障", error_class=ErrorClass.COMFY_WORKFLOW)

    state = _executor(handler, poll_max_attempts=3).poll(
        JobRef(job_id="PID", executor="comfyui_http")
    )
    assert state.status is JobStatus.UNKNOWN, (
        f"查询失败只能得出「无法判定」，实际 {state.status}（error={state.error!r}）"
    )
    assert state.status is not JobStatus.FAILED, "瞬时查询故障不得被当成「作业不存在」"
    assert state.is_success is False, "UNKNOWN 绝不当成功（既有不变量）"
    assert state.error and "queue" in state.error.lower(), (
        f"归因必须写明是 /queue 查询失败，实际 error={state.error!r}"
    )
    assert state.error_class is ErrorClass.COMFY_WORKFLOW


def test_poll_retries_after_transient_queue_failure():
    """M-2 加强：一次瞬时 /queue 故障之后必须还能重新判定（可重试，而不是直接判死）。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_poll_retries_after_transient_queue_failure" -q``
    旧行为：第一次 /queue 500 就 return None -> 立即 FAILED，``poll_max_attempts`` 完全没机会生效。
    新约束：把 /queue 故障计入重试；恢复后按真实队列状态返回 RUNNING。
    """
    seq = {"n": 0}

    def handler(method, path):  # noqa: ARG001
        if path.startswith("/history/"):
            return (200, {})
        if path == "/queue":
            seq["n"] += 1
            if seq["n"] == 1:
                return (500, {"error": "temporary"})
            return (200, {"queue_running": [[1, "PID"]], "queue_pending": []})
        return (404, {})

    state = _executor(handler, poll_max_attempts=3).poll(
        JobRef(job_id="PID", executor="comfyui_http")
    )
    assert state.status is JobStatus.RUNNING, (
        f"瞬时故障恢复后必须能判定为 RUNNING，实际 {state.status}（error={state.error!r}）"
    )


def test_poll_successful_queue_query_without_job_is_still_failed():
    """M-2 反向守卫：查询**成功**且队列里确实没有该作业 -> 仍然是 FAILED（不得放松失败闭合）。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_poll_successful_queue_query_without_job_is_still_failed" -q``
    """
    def handler(method, path):  # noqa: ARG001
        if path.startswith("/history/"):
            return (200, {})
        return (200, {"queue_running": [], "queue_pending": []})

    state = _executor(handler, poll_max_attempts=3).poll(
        JobRef(job_id="GONE", executor="comfyui_http")
    )
    assert state.status is JobStatus.FAILED
    assert state.status is not JobStatus.UNKNOWN
    assert state.error and "GONE" in state.error, f"错误信息必须点名作业，实际 {state.error!r}"
    assert state.error_class is ErrorClass.COMFY_WORKFLOW


def test_poll_unusable_queue_response_is_unknown_not_job_lost():
    """M-2 边界：200 但响应体缺少 queue_* 键（代理/异常版本）＝ 无法判定，不得当成作业丢失。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_poll_unusable_queue_response_is_unknown_not_job_lost" -q``
    旧行为：``not isinstance(body, dict)`` 之外的畸形 200 响应会被 ``.get()`` 静默当成空队列 -> FAILED。
    """
    def handler(method, path):  # noqa: ARG001
        if path.startswith("/history/"):
            return (200, {})
        return (200, {"status": "ok"})

    state = _executor(handler, poll_max_attempts=1).poll(
        JobRef(job_id="PID", executor="comfyui_http")
    )
    assert state.status is JobStatus.UNKNOWN, (
        f"无法判定的 /queue 响应不得判成「作业不存在」，实际 {state.status}"
    )
    assert state.is_success is False


# ---------------------------------------------------------------------------
# review.events 事件桥接（M-4）
# ---------------------------------------------------------------------------


class _UninspectableAppendEvent:
    """签名无法自省的 append_event（``inspect.signature`` 抛 TypeError）。"""

    def __init__(self, calls: list) -> None:
        self._calls = calls

    def __call__(self, project, **kwargs):
        self._calls.append((project, kwargs))
        return {"ok": True}


def _install_fake_store_events(monkeypatch, append_event) -> None:
    fake = types.ModuleType("bettercomfyui.store.events")
    fake.append_event = append_event
    monkeypatch.setitem(sys.modules, "bettercomfyui.store.events", fake)
    review_events.reset_store_events_cache()


def _fixed_clock() -> str:
    return "2026-10-16T10:00:00+08:00"


def test_events_uninspectable_writer_is_memory_mode_not_silent_drop(monkeypatch):
    """M-4：签名无法自省时绝不盲调 append_event —— 否则「事件没落盘，流程以为已记录」。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_events_uninspectable_writer_is_memory_mode_not_silent_drop" -q``
    旧行为：``_accepted()`` 吞掉 ``TypeError/ValueError`` 返回 None，调用方 ``or set()`` 后
    以**空 kwargs** 调用 ``append_event(project)`` —— 事件名根本没传进去、也没报错
    （``mode='store'``、``persistence_available=True``、``write_failures=0``），
    与 review 层「未落盘必须显式可见 / `require_store_events()`」的约定冲突。
    新约束：无法安全桥接时显式降级为 memory 模式（WARN），且**不得**发生任何盲目调用。
    """
    calls: list = []
    opaque = _UninspectableAppendEvent(calls)
    opaque.__signature__ = "bogus"  # 让 inspect.signature 抛 TypeError
    _install_fake_store_events(monkeypatch, opaque)
    try:
        log = ReviewEventLog(project=Path("."), clock=_fixed_clock)
        record = log.emit("review_mode_switched", detail={"to": "AUTO_SWITCHED"})

        assert record["event"] == "review_mode_switched", "内存镜像仍须记录事件"
        assert calls == [], (
            "签名未知时不得盲目调用 append_event（会把事件静默丢掉却看起来已记录）："
            f"实际调用={calls!r}"
        )
        assert log.persistence_available is False, "无法安全桥接必须显式声明「未落盘」"
        assert log.mode == "memory"
        assert log.write_failures == 0, "没有盲目调用就不该出现「写失败」计数"
    finally:
        review_events.reset_store_events_cache()


def test_events_append_event_without_event_kwarg_is_not_used(monkeypatch):
    """M-4 第二例：``append_event`` 签名不接受 ``event`` 时不得被当成可用的落盘通道。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_events_append_event_without_event_kwarg_is_not_used" -q``
    旧行为：``_accepted()`` 得到空集合后仍然创建 writer，并调用 ``append_event(project)``。
    新约束：显式判定「不接受 event」并降级为 memory 模式。
    """
    calls: list = []

    def append_event(project, *args):  # 没有 **kwargs，也不接受 event 关键字
        calls.append((project, args))
        return {"ok": True}

    _install_fake_store_events(monkeypatch, append_event)
    try:
        log = ReviewEventLog(project=Path("."), clock=_fixed_clock)
        log.emit("review_state_changed")
        assert calls == [], f"不得调用不接受 event 的写入函数：实际={calls!r}"
        assert log.persistence_available is False
        assert log.mode == "memory"
    finally:
        review_events.reset_store_events_cache()


def test_events_bridge_still_delegates_for_normal_append_event(monkeypatch, tmp_path):
    """反向守卫：正常签名（接受 event/namespace）时桥接行为必须保持不变。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_events_bridge_still_delegates_for_normal_append_event" -q``
    """
    calls: list[dict] = []

    def append_event(project, *, event, namespace="BETTERCOMFYUI", stage=None,  # noqa: ARG001
                     object_id=None, version=None, mode=None, detail=None,
                     severity="INFO", fsync=True):  # noqa: ARG001
        calls.append({"event": event, "namespace": namespace, "detail": detail,
                      "severity": severity})
        return {"ok": True}

    _install_fake_store_events(monkeypatch, append_event)
    try:
        log = ReviewEventLog(project=tmp_path, clock=_fixed_clock)
        assert log.mode == "store"
        assert log.persistence_available is True
        log.emit("mode_switched", detail={"a": 1}, severity="WARN")
        assert calls == [{"event": "mode_switched", "namespace": "REVIEW",
                          "detail": {"a": 1}, "severity": "WARN"}]
        assert log.write_failures == 0
    finally:
        review_events.reset_store_events_cache()


# ---------------------------------------------------------------------------
# 成片元数据 mock 标记（m-2，Lead 已修，此处补回归）
# ---------------------------------------------------------------------------


def test_write_output_persists_generation_provenance_mock_marker(tmp_path, repo_root):
    """m-2：成片 metadata.json 必须机器可读地标注 mock/未验证（**不依赖 ffmpeg**）。

    复现：``python -m pytest "tests/test_bugfix_majors.py::test_write_output_persists_generation_provenance_mock_marker" -q``
    旧行为：metadata.json 里没有 ``generation_provenance``，只拿到 mp4+metadata 的下游会把
    占位媒体误当真实产出（VERIFY.md m-2 / 任务书第 9 节末句）。
    新约束：``write_output`` 落盘的 metadata.json 必须能读到
    ``generation_provenance.is_mock is True`` 且 ``MovieMetadata.from_dict`` 往返一致。

    不依赖 ffmpeg 的做法：用占位媒体文件 + 注入 ``ffprobe_runner``；``segments`` 为空，
    因此不会触发封面提取（CONTRACTS §8 / pipeline/core.py::write_output）。
    """
    from bettercomfyui.config import load_config
    from bettercomfyui.pipeline.core import write_output

    work = tmp_path / "deliver"
    work.mkdir()
    movie = work / "placeholder.mp4"
    movie.write_bytes(b"\0" * 8192)  # 非空、可读的占位媒体

    cfg = load_config(repo_root=repo_root)
    metadata = MovieMetadata(
        project_id="proj-provenance",
        project_display_name="探针项目",
        generation_provenance={
            "is_mock": True,
            "executor": "mock",
            "reason": "回归用例占位媒体，非真实生成",
            "unverified": ["真实视频生成"],
        },
    )
    result = write_output(
        movie,
        cfg,
        project_dir=work,
        metadata=metadata,
        ffprobe_runner=lambda _p: {
            "duration_s": 3.0,
            "has_audio": True,
            "has_video": True,
            "video_codec": "h264",
            "audio_codec": "aac",
        },
    )
    assert result["checks"]["probed"] is True

    payload = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
    provenance = payload.get("generation_provenance")
    assert isinstance(provenance, dict), (
        f"成片 metadata.json 必须带 generation_provenance（mock 标记），实际键={sorted(payload)}"
    )
    assert provenance["is_mock"] is True
    assert provenance["unverified"] == ["真实视频生成"]
    assert MovieMetadata.from_dict(payload).is_mock_output is True
