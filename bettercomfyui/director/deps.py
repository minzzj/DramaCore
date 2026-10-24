"""director 层依赖协议与默认解析。

依赖方向（契约 §4/§7）：`director` -> `store` + `review` + `executors` + `models`。
`director` **不得** import `pipeline`。

所有外部依赖都通过构造函数/参数注入，默认解析器只在真正需要时 import 真实模块，
并在 import 失败时抛出**可操作**的错误（绝不静默降级为内存替身——那是假成功）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bettercomfyui.director.errors import DirectorDependencyError

__all__ = [
    "AssetLockResolver",
    "CounterLike",
    "IdFactory",
    "default_counters",
    "default_ids",
    "resolve_lock_resolver",
]


@runtime_checkable
class CounterLike(Protocol):
    """`review.CounterStore` 的最小协议（契约 §6）。"""

    def increment(self, obj_id: str, kind: Any) -> int: ...

    def get(self, obj_id: str, kind: Any) -> int: ...

    def reset(self, obj_id: str, kind: Any) -> None: ...


@runtime_checkable
class AssetLockResolver(Protocol):
    """`store.AssetStore.resolve_lock` 的最小协议（契约 §4）。"""

    def resolve_lock(self, asset_id: str, version: str) -> dict | None: ...


class IdFactory(Protocol):
    """`store.ids`（契约 §2）。参数顺序与本文件一致，集成时以 store.ids 为准。"""

    def chapter_id(self, index: int) -> str: ...

    def segment_id(self, project_id: str, chapter_no: int, seg_no: int) -> str: ...

    def next_version(self, version: str | None) -> str: ...

    def attempt_id(self, segment_id: str, version: str, index: int) -> str: ...

    def audit_id(self, segment_id: str, version: str) -> str: ...

    def review_cycle_id(self, target_kind: str, target_id: str, version: str) -> str: ...

    def new_cascade_id(self, segment_id: str) -> str: ...

    def cnt_id(self, obj_id: str, kind: Any) -> str: ...

    def idempotency_key(self, namespace: str, payload: dict) -> str: ...


def default_ids() -> IdFactory:
    """解析真实的 `bettercomfyui.store.ids`。"""
    try:
        from bettercomfyui.store import ids as store_ids  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise DirectorDependencyError(
            "无法导入 bettercomfyui.store.ids（ID 唯一入口）。"
            "契约要求所有 ID 由 store.ids 生成，director 不得自造 ID。"
            f"原始错误：{type(exc).__name__}: {exc}"
        ) from exc
    return store_ids  # type: ignore[return-value]


def default_counters(project_dir: Path | str | None = None) -> CounterLike:
    """解析真实的 `bettercomfyui.review.CounterStore`。

    构造函数签名不在契约里，因此这里做有限尝试；失败必须**显式报错**，
    由调用方注入（绝不退化成内存计数器）。
    """
    try:
        from bettercomfyui.review import counters as review_counters  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise DirectorDependencyError(
            "无法导入 bettercomfyui.review.counters.CounterStore；"
            "CNT2/CNT3 必须使用 review 层统一计数器，director 不得另建一套 CNT 存储。"
            f"原始错误：{type(exc).__name__}: {exc}"
        ) from exc
    store_cls = getattr(review_counters, "CounterStore", None)
    if store_cls is None:
        raise DirectorDependencyError("bettercomfyui.review.counters 中没有 CounterStore")
    attempts = []
    if project_dir is not None:
        attempts.append(lambda: store_cls(project_dir))
    attempts.append(lambda: store_cls())
    last_exc: Exception | None = None
    for factory in attempts:
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise DirectorDependencyError(
        "无法构造 review.CounterStore；请在构造函数中显式注入 counters。"
        f"原始错误：{type(last_exc).__name__}: {last_exc}"
    )


def resolve_lock_resolver(
    store: AssetLockResolver | None, lock_resolver: Any | None
) -> Any:
    """取 R3 门禁使用的 resolve_lock 可调用对象。缺失时必须报错（不得跳过门禁）。"""
    if lock_resolver is not None:
        if not callable(lock_resolver):
            raise DirectorDependencyError("lock_resolver 必须是可调用对象")
        return lock_resolver
    if store is not None and hasattr(store, "resolve_lock"):
        return store.resolve_lock
    raise DirectorDependencyError(
        "缺少 R3 门禁依赖：必须注入 store（含 resolve_lock）或 lock_resolver。"
        "契约 §7 要求生成只能使用 resolve_lock() 通过的素材版本。"
    )
