"""契约 API 适配层：把「CONTRACTS.md 里写了、但构造方式没写全」的对象取出来。

为什么需要这一层：
    docs/CONTRACTS.md 定义了 `AssetStore.__init__(self, project: ProjectPaths)`，
    但 **没有定义 `ProjectPaths` 属于哪个模块、也没有定义怎么得到它**（契约缺口 G-2）。
    为了不把「契约没写清楚」变成「实现被判失败」，这里用**行为探测**：
    依次尝试若干合法的构造方式，只要某一种能让 `load_index()` 正常工作即视为可用。

    若全部失败，抛 AssertionError 并列出所有尝试过的候选与各自的异常 ——
    这是可复现的证据，而不是猜测。

同时提供 `call_with_timeout`，让「无限循环」类缺陷表现为失败而不是挂死。
"""

from __future__ import annotations

import threading
import time
import types
from dataclasses import dataclass
from datetime import date as _real_date
from datetime import datetime as _real_datetime
from datetime import timedelta as _real_timedelta
from datetime import timezone as _real_timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from _harness import contract_attr, load, load_optional, make_config


# ---------------------------------------------------------------------------
# 通用构造探测（契约未定义构造签名的类，见 docs/VERIFY.md 契约缺口 G-6）
# ---------------------------------------------------------------------------


def construct(cls, candidates, *, probe: Callable[[Any], Any] | None = None, what: str = ""):
    """按候选签名依次尝试构造 cls；probe 用来确认构造结果真的可用。

    全部失败 -> AssertionError（附全部尝试记录）。这不是「未验证」，
    而是契约缺口暴露出来的可复现证据。
    """
    failures: list[str] = []
    for label, args, kwargs in candidates:
        try:
            obj = cls(*args, **kwargs)
            if probe is not None:
                probe(obj)
        except Exception as exc:  # noqa: BLE001 - 探测，失败即换下一种
            failures.append(f"  - {label}: {type(exc).__name__}: {exc}")
            continue
        return obj
    raise AssertionError(
        f"无法构造 {getattr(cls, '__name__', cls)}"
        + (f"（{what}）" if what else "")
        + "：CONTRACTS.md 未定义该类的构造签名（契约缺口 G-6）。\n已尝试：\n"
        + "\n".join(failures)
    )


def build_asset_reviewer(
    assets,
    cfg,
    tmp_path: Path | None = None,
    *,
    project=None,
    clock=None,
    model_review=None,
    counters=None,
    notifier=None,
    auto_commit=None,
):
    """构造 AssetReviewer。

    契约缺口 G-6（构造签名未定义）已实测为：
        `AssetReviewer(store=None, *, config=, model_review=, notifier=, event_log=,
                        counters=, clock=, project=, project_id=, ledger=, ledger_path=,
                        downstream=, max_notify_retries=)`

    重要（实测）：不传 `project=` 时，审核事件只留在内存镜像里，并打印
    「审核事件未落盘……这不算「已记录」」的 WARN。要让事件真正落到
    `logs/events.jsonl`（契约第 10 节要求），必须传 `project=ProjectPaths`。
    另：`clock` 是可注入的字符串时间源，用它做确定性超时测试，
    比 monkeypatch `time.time` 更可靠（见验收 7）。
    """
    ar = load("bettercomfyui.review.asset_review")
    cls = contract_attr(ar, "AssetReviewer", kind="class")

    def probe(o):
        assert hasattr(o, "review_version"), "AssetReviewer 必须有 review_version"
        assert hasattr(o, "on_human_timeout"), "AssetReviewer 必须有 on_human_timeout"

    kwargs: dict[str, Any] = {"config": cfg}
    if project is not None:
        kwargs["project"] = project
    if clock is not None:
        kwargs["clock"] = clock
    if model_review is not None:
        kwargs["model_review"] = model_review
    if counters is not None:
        kwargs["counters"] = counters
    if notifier is not None:
        kwargs["notifier"] = notifier
    if auto_commit is not None:
        # review-lock 支持的选项：auto_commit=False 时只推进到结论态、**不**替调用方提交，
        # 返回值即「可直接喂给 commit_final 的 cycle dict」——并发唯一提交用例必须用它。
        kwargs["auto_commit"] = auto_commit

    return construct(
        cls,
        [
            (f"({', '.join(sorted(kwargs))})", (assets,), kwargs),
            ("(store, config=cfg)", (assets,), {"config": cfg}),
        ],
        probe=probe,
        what="review.asset_review.AssetReviewer",
    )


class FakeClock:
    """可推进的字符串时钟，用于 AssetReviewer(clock=...) 的确定性超时测试。"""

    def __init__(self, start_iso: str = "2026-10-16T12:00:00+08:00"):
        self._t = _real_datetime.fromisoformat(start_iso)

    def __call__(self) -> str:
        return self._t.isoformat()

    def advance(self, *, seconds: float = 0, minutes: float = 0) -> str:
        self._t = self._t + _real_timedelta(seconds=seconds, minutes=minutes)
        return self._t.isoformat()

    @property
    def value(self) -> str:
        return self._t.isoformat()


def build_counter_store(cfg=None, assets=None):
    counters = load("bettercomfyui.review.counters")
    cls = contract_attr(counters, "CounterStore", kind="class")
    cfg = cfg or make_config()

    def probe(o):
        assert hasattr(o, "increment") and hasattr(o, "get") and hasattr(o, "reset")

    return construct(
        cls,
        [
            ("()", (), {}),
            ("(cfg)", (cfg,), {}),
            ("(config=cfg)", (), {"config": cfg}),
            ("(store, cfg)", (assets, cfg), {}),
            ("(assets,)", (assets,), {}),
        ],
        probe=probe,
        what="review.counters.CounterStore",
    )


def build_cascade_controller(cfg, assets=None, counters=None, tmp_path: Path | None = None):
    cascade = load("bettercomfyui.director.cascade")
    cls = contract_attr(cascade, "CascadeController", kind="class")

    def probe(o):
        assert hasattr(o, "on_segment_changed"), "CascadeController 必须有 on_segment_changed"
        assert hasattr(o, "mark_next") and hasattr(o, "run")

    pdir = Path(tmp_path) if tmp_path else Path(".")
    return construct(
        cls,
        [
            ("(cfg)", (cfg,), {}),
            ("(config=cfg)", (), {"config": cfg}),
            ("(assets, cfg)", (assets, cfg), {}),
            ("(cfg, counters)", (cfg, counters), {}),
            ("(store=assets, config=cfg, path=pdir)", (), {"store": assets, "config": cfg, "path": pdir}),
        ],
        probe=probe,
        what="director.cascade.CascadeController",
    )


def build_checkpoint_store(tmp_path: Path, cfg=None, project_dir: Path | None = None):
    ckpt = load("bettercomfyui.store.checkpoints")
    cls = contract_attr(ckpt, "CheckpointStore", kind="class")
    cfg = cfg or make_config()
    pdir = Path(project_dir) if project_dir else Path(tmp_path)

    def probe(o):
        assert hasattr(o, "save") and hasattr(o, "latest")

    return construct(
        cls,
        [
            ("(project_dir)", (pdir,), {}),
            ("(root=pdir)", (), {"root": pdir}),
            ("(project=pdir)", (), {"project": pdir}),
            ("(project_dir, cfg)", (pdir, cfg), {}),
            ("(pdir, config=cfg)", (pdir,), {"config": cfg}),
        ],
        probe=probe,
        what="store.checkpoints.CheckpointStore",
    )


# ---------------------------------------------------------------------------
# 假时钟：让「10 分钟超时」可以在毫秒级内可复现地验证
# ---------------------------------------------------------------------------


def install_fake_clock(monkeypatch, module=None, *, start_ts: float = 1_700_000_000.0):
    """接管 time/datetime，返回 advance(seconds)。

    同时覆盖 `time.time()`、`time.monotonic()`、`time.time_ns()` 与模块命名空间里的
    `datetime`（`from datetime import datetime` 与 `import datetime` 两种写法）。
    """

    class FakeDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime.fromtimestamp(_STATE["t"], tz=tz or _real_timezone.utc)

        @classmethod
        def utcnow(cls):
            return _real_datetime.fromtimestamp(_STATE["t"], tz=_real_timezone.utc).replace(
                tzinfo=None
            )

        @classmethod
        def today(cls):
            return cls.now()

    _STATE = {"t": float(start_ts)}

    monkeypatch.setattr(time, "time", lambda: _STATE["t"], raising=False)
    monkeypatch.setattr(time, "monotonic", lambda: _STATE["t"], raising=False)
    monkeypatch.setattr(time, "time_ns", lambda: int(_STATE["t"] * 1e9), raising=False)

    if module is not None and hasattr(module, "datetime"):
        cur = getattr(module, "datetime")
        if isinstance(cur, types.ModuleType):
            monkeypatch.setattr(
                module,
                "datetime",
                SimpleNamespace(
                    datetime=FakeDatetime,
                    timedelta=_real_timedelta,
                    timezone=_real_timezone,
                    date=_real_date,
                ),
                raising=False,
            )
        else:
            monkeypatch.setattr(module, "datetime", FakeDatetime, raising=False)

    def advance(seconds: float) -> float:
        _STATE["t"] += float(seconds)
        return _STATE["t"]

    return advance



# ---------------------------------------------------------------------------
# 超时护栏：级联无限循环必须变成 FAIL，而不是让测试挂死
# ---------------------------------------------------------------------------


def call_with_timeout(fn: Callable[[], Any], *, timeout_s: float, what: str) -> Any:
    """在工作线程里执行 fn；超时说明疑似无限循环/死锁。"""
    box: dict[str, Any] = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样回抛
            box["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise AssertionError(
            f"{what}：{timeout_s}s 内未返回，疑似无限循环或死锁（任务书第 6 节：不得无限级联）"
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# 项目 / 素材库
# ---------------------------------------------------------------------------


@dataclass
class ProjectHandle:
    """一个已打开项目的验证句柄。"""

    project_id: str
    root: Path          # 项目根目录 projects/<project_id>
    paths: Any          # 传给 AssetStore 的对象（契约缺口 G-2 的适配结果）
    assets: Any         # AssetStore 实例

    def meta_path(self) -> Path:
        return self.root / "meta.json"

    def events_path(self) -> Path:
        return self.root / "logs" / "events.jsonl"

    def versions_dir(self, asset_type: str, asset_id: str) -> Path:
        return self.root / "assets" / asset_type.lower() / asset_id / "versions"


def new_project_root(tmp_path: Path) -> Path:
    root = Path(tmp_path) / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_store(tmp_path: Path, cfg=None):
    """构造 ProjectStore 实例；返回 (store, projects_root)。"""
    projects = load("bettercomfyui.store.projects")
    cfg = cfg or make_config()
    root = new_project_root(tmp_path)
    cls = contract_attr(projects, "ProjectStore", kind="class")
    return cls(root, cfg), root


def _pid_of(created: Any) -> str:
    if isinstance(created, dict):
        for key in ("project_id", "id", "pid"):
            if key in created:
                return str(created[key])
    if isinstance(created, str):
        return created
    raise AssertionError(
        f"契约违背：ProjectStore.create() 应返回含 project_id 的 dict，实际 {type(created)!r}: {created!r}"
    )


def create_project(tmp_path: Path, display_name: str, cfg=None):
    """建一个项目；返回 (handle, ProjectStore 实例, projects_root)。"""
    store, root = project_store(tmp_path, cfg)
    created = store.create(display_name)
    pid = _pid_of(created)
    handle = open_project(tmp_path, pid, cfg=cfg, store=store, root=root)
    return handle, store, root


def _path_candidates(root: Path, pid: str) -> list[tuple[str, Any]]:
    """收集所有可能被 AssetStore 接受的 project 参数候选。"""
    cands: list[tuple[str, Any]] = []
    pdir = root / pid
    for modname in ("bettercomfyui.store.paths", "bettercomfyui.store.projects"):
        mod = load_optional(modname)
        if mod is None:
            continue
        cls = getattr(mod, "ProjectPaths", None)
        if cls is None:
            continue
        attempts = (
            ("ProjectPaths(root, pid)", (root, pid), {}),
            ("ProjectPaths(root, project_id=pid)", (root,), {"project_id": pid}),
            ("ProjectPaths(pdir)", (pdir,), {}),
            ("ProjectPaths(pid, root)", (pid, root), {}),
            ("ProjectPaths(project_id=pid, root=root)", (), {"project_id": pid, "root": root}),
        )
        for label, args, kwargs in attempts:
            try:
                cands.append((f"{modname}.{label}", cls(*args, **kwargs)))
            except Exception:  # noqa: BLE001 - 探测，失败即换下一种
                continue
    cands.append(("PosixPath(项目目录)", pdir))
    cands.append(("str(项目目录)", str(pdir)))
    cands.append(
        (
            "SimpleNamespace(project_id/root/dir/path/project_root=项目目录)",
            SimpleNamespace(
                project_id=pid,
                root=pdir,
                project_dir=pdir,
                dir=pdir,
                path=pdir,
                project_root=pdir,
            ),
        )
    )
    cands.append(('dict{"project_id","root","path"}', {"project_id": pid, "root": str(pdir), "path": str(pdir)}))
    return cands


def open_project(
    tmp_path: Path,
    project_id: str,
    *,
    cfg=None,
    store=None,
    root: Path | None = None,
) -> ProjectHandle:
    """打开项目并构造可用的 AssetStore。

    以「AssetStore(...).load_index() 返回 dict」作为构造成功的判定标准；
    全部候选失败 -> AssertionError（附全部尝试记录，可复现）。
    """
    assets_mod = load("bettercomfyui.store.assets")
    cfg = cfg or make_config()
    root = Path(root) if root is not None else new_project_root(tmp_path)
    AssetStore = contract_attr(assets_mod, "AssetStore", kind="class")

    failures: list[str] = []
    for label, cand in _path_candidates(root, project_id):
        try:
            inst = AssetStore(cand)
            idx = inst.load_index()
            assert isinstance(idx, dict), f"load_index() 应返回 dict，实际 {type(idx)!r}"
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 记录后继续探测
            failures.append(f"  - {label}: {type(exc).__name__}: {exc}")
            continue
        return ProjectHandle(
            project_id=project_id, root=root / project_id, paths=cand, assets=inst
        )

    raise AssertionError(
        "契约缺口 G-2：无法用任何合法方式构造 AssetStore（CONTRACTS.md 只写了 "
        "`AssetStore(project: ProjectPaths)`，未定义 ProjectPaths 的模块与构造方式）。\n"
        "已尝试：\n" + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# 素材：造一个真实文件当素材内容
# ---------------------------------------------------------------------------


def make_asset_file(tmp_path: Path, name: str, content: bytes) -> Path:
    p = Path(tmp_path) / "sources" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


#: 验证用合成 project_id（32 位小写 hex，符合 CONTRACTS.md 第 2 节）
VERIFY_PROJECT_ID = "0123456789abcdef0123456789abcdef"


def plan_segments_for_verify(script: dict, cfg, *, project_id: str = VERIFY_PROJECT_ID, as_dicts: bool = True, **kw):
    """调用 plan_segments：R5 要求显式传入 project_id（段 ID 必须包含 project_id）。"""
    plan_mod = load("bettercomfyui.director.plan")
    fn = contract_attr(plan_mod, "plan_segments")
    kw.setdefault("locked_assets", {})
    return fn(script, cfg, project_id=project_id, as_dicts=as_dicts, **kw)


def field(seg: Any, name: str, default: Any = None) -> Any:
    """同时支持 Segment 对象与 dict 的字段读取。"""
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)


def create_asset(handle: ProjectHandle, *, asset_type: str = "CHARACTER", name: str = "角色A", **kw):
    """create_asset 的稳健包装：返回素材条目 dict。"""
    fn = contract_attr(handle.assets, "create_asset")
    kwargs = dict(
        asset_type=asset_type,
        name=name,
        source=kw.pop("source", "AI"),
        library_scope=kw.pop("library_scope", "PROJECT"),
    )
    kwargs.update(kw)
    return fn(**kwargs)


def asset_id_of(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("asset_id", "id"):
            if key in entry:
                return str(entry[key])
    raise AssertionError(f"契约违背：素材条目应含 asset_id，实际 {type(entry)!r}: {entry!r}")


def version_of(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("version", "version_id"):
            if key in entry:
                return str(entry[key])
    raise AssertionError(f"契约违背：版本记录应含 version，实际 {type(entry)!r}: {entry!r}")


def set_lock(assets, asset_id: str, version: str, *, lock_source: str, content_hash: str, review_cycle_id: str | None = None):
    """写 LOCK 的验证用包装。

    **契约偏差 D-1**：CONTRACTS.md §4 的签名是
    `set_lock(asset_id, version, *, lock_source, content_hash)`，
    但实现（`store/assets.py`）额外要求 `review_cycle_id`：LOCKED 版本必须能追溯到授权它的
    审核周期。这个要求本身符合方案「最终结果只能提交一次 + LOCK 携带来源」的精神，
    但契约文本未同步 —— 需要 Lead 在 CONTRACTS.md 补上该参数（MAJOR，见 docs/VERIFY.md）。
    这里两种签名都尝试，保证验证的是 LOCK 语义而不是签名。
    """
    kw = {"lock_source": lock_source, "content_hash": content_hash}
    cycle = review_cycle_id or f"VERIFY:{asset_id}:{version}"
    try:
        return assets.set_lock(asset_id, version, review_cycle_id=cycle, **kw)
    except TypeError:
        return assets.set_lock(asset_id, version, **kw)
