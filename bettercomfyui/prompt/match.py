"""素材候选的三分支确定性匹配（CONTRACTS §5 / R1 落地）。

**这是 R1 的核心实现：存在性判断只能由程序做，不允许调用 AI。**
本模块不 import 任何网关/模型客户端，全流程为纯程序逻辑：

===================================== ============================================
``MatchOutcome.EXISTS_REUSE``          归一化名精确命中**唯一**素材 -> 复用既有版本，
                                       不重复生成、不覆盖
``MatchOutcome.NEW_GENERATE``          确定不存在 -> 进入生成队列
``MatchOutcome.SUSPECT_DUPLICATE``     名称歧义或高度相近 -> 建 SUSPECT_* 记录，
                                       进入待确认队列；**禁止自动覆盖、禁止静默丢弃**
===================================== ============================================

查找优先级（显式、固定）：项目库 → 公共库。命中公共库时 reason 里标注
``scope=PUBLIC``。跨项目的项目库查找一律不发生（R5）。

疑似重复的判定条件（任一命中）：

* 同一归一化名在**同一作用域**命中多个素材条目（歧义）；
* 库中不存在精确匹配，但存在「仅差标点/序数词/繁简」或高相似度（>=0.85）的名称；
* 已存在同名的未处理 SUSPECT 记录（复用该记录，不重复建）。

落盘要求：疑似记录通过 ``store.record_suspect`` 落盘（由 store 写入独立的
``suspects.json``，因此 ``meta.json`` 字节在 SUSPECT 分支下**完全不变**）。
store 若未提供该钩子 -> 抛 :class:`SuspectPersistenceError`，**绝不静默丢弃**。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, NamedTuple

from ..models.enums import AssetType, LibraryScope, MatchOutcome
from .errors import PromptDependencyError, SuspectPersistenceError
from .normalize import is_suspect, normalize_name

__all__ = [
    "MatchResult",
    "match_candidate",
    "build_suspect_queue",
    "select_reusable_version",
    "SUSPECT_SIMILARITY_THRESHOLD",
]


#: 相似度阈值：保守启发式。误判为疑似只是「进入人工确认队列」，绝不会自动覆盖；
#: 但漏判会让同一角色重复生成，因此取 0.75（可被 is_suspect 的 threshold 覆盖）。
#: 注意：归一化后完全相等由 store 的索引负责（store 侧 index_key 会忽略标点），
#: 所以「仅差标点」在真实 store 上是 EXISTS_REUSE 而不是疑似。
SUSPECT_SIMILARITY_THRESHOLD = 0.75


class MatchResult(NamedTuple):
    """匹配结论（CONTRACTS §5，字段顺序已冻结）。"""

    outcome: MatchOutcome
    asset_id: str | None
    version: str | None
    suspect_id: str | None
    reason: str


# ---------------------------------------------------------------------------
# store 鸭子类型适配
# ---------------------------------------------------------------------------


def _require_callable(store: Any, *names: str) -> Callable:
    for name in names:
        func = getattr(store, name, None)
        if callable(func):
            return func
    raise PromptDependencyError(
        f"AssetStore 缺少接口 {' / '.join(names)}（CONTRACTS §4）；"
        f"prompt 层不会静默跳过，也不会用内存假库替代"
    )


def _call_with_optional_scope(func: Callable, *args: Any, scope: Any = None, **kwargs: Any) -> Any:
    """调用 store 方法，兼容「不接受 scope 关键字」的实现。

    异常语义：仅当 TypeError 明确与 ``scope`` 关键字有关时才降级重试；
    **其余 TypeError 一律原样抛出**（绝不吞掉 store 的真实错误）。
    """
    try:
        return func(*args, scope=scope, **kwargs)
    except TypeError as exc:
        if "scope" not in str(exc):
            raise
        return func(*args, **kwargs)


def _find_by_normalized_name(store: Any, normalized: str, scope: LibraryScope) -> list[dict]:
    func = _require_callable(store, "find_by_normalized_name")
    result = _call_with_optional_scope(func, normalized, scope=scope)
    if result is None:
        return []
    if not isinstance(result, (list, tuple)):
        raise PromptDependencyError(
            "AssetStore.find_by_normalized_name 必须返回 list[dict]"
        )
    return [dict(item) for item in result if isinstance(item, Mapping)]


def _list_assets(store: Any, scope: LibraryScope) -> list[dict]:
    func = _require_callable(store, "list_assets")
    result = _call_with_optional_scope(func, scope=scope)
    if result is None:
        return []
    if not isinstance(result, (list, tuple)):
        raise PromptDependencyError("AssetStore.list_assets 必须返回 list[dict]")
    return [dict(item) for item in result if isinstance(item, Mapping)]


def _resolve_lock(store: Any, asset_id: str, version: str) -> dict | None:
    func = _require_callable(store, "resolve_lock")
    return func(asset_id, version)


def _version_number(version: Any) -> int:
    """版本号排序键：``v3`` -> 3；无法解析的形态返回 -1（排最前）。

    这里没有错误可吞：返回的是**排序键**，版本字符串本身仍原样保留在列表里，
    不会被丢弃。契约 §2 规定版本格式为 ``v{int}``，-1 只是防御性兜底。
    """
    text = str(version)
    if text.startswith("v") or text.startswith("V"):
        text = text[1:]
    try:
        return int(text)
    except ValueError:
        return -1


def _versions_of(asset: Mapping[str, Any]) -> list[str]:
    """从素材条目里提取版本号列表（按版本号升序），容忍多种存储形态。"""
    versions = asset.get("versions")
    collected: list[str] = []
    if isinstance(versions, Mapping):
        collected = [str(k) for k in versions.keys()]
    elif isinstance(versions, (list, tuple)):
        for item in versions:
            if isinstance(item, str):
                collected.append(item)
            elif isinstance(item, Mapping) and item.get("version") is not None:
                collected.append(str(item["version"]))
    if not collected:
        for key in ("latest_version", "current_version", "version", "version_id"):
            value = asset.get(key)
            if isinstance(value, str) and value.strip():
                collected.append(value.strip())
                break
    unique = sorted({v for v in collected if v}, key=_version_number)
    return unique


def select_reusable_version(store: Any, asset: Mapping[str, Any]) -> tuple[str | None, str]:
    """挑选可复用版本：优先 ``resolve_lock`` 通过的最新版本，否则最新版本。

    返回 ``(version, note)``。``note`` 会进入 reason 便于审计。
    R3：只有 resolve_lock 通过的版本才「可用于生成」；这里不越权——
    未锁定版本也会被复用（避免重复生成），但 note 会明确写出未锁定。
    """
    asset_id = str(asset.get("asset_id") or asset.get("id") or "")
    versions = _versions_of(asset)
    if not versions:
        return None, "no_version"
    for version in reversed(versions):
        lock = _resolve_lock(store, asset_id, version)
        if lock:
            source = ""
            if isinstance(lock, Mapping):
                source = str(lock.get("lock_source") or lock.get("lock_status") or "")
            return version, f"locked:{source}" if source else "locked"
    return versions[-1], "unlocked"


# ---------------------------------------------------------------------------
# SUSPECT 记录
# ---------------------------------------------------------------------------


def _suspect_signature(record: Mapping[str, Any]) -> tuple[str, str]:
    """疑似记录的去重签名。

    兼容 store 的真实字段名 ``candidate_name`` 与简化字段名 ``name``——
    两者不一致会导致重跑产生重复队列（Lead 明令禁止）。
    """
    name = record.get("name") or record.get("candidate_name") or ""
    return (
        str(record.get("normalized_name") or ""),
        normalize_name(str(name)),
    )


def _list_existing_suspects(store: Any, *, resolved: bool | None = None) -> list[dict]:
    """读取疑似队列（未提供 list_suspects 时**报错**，绝不返回空列表假装没有疑似记录）。"""
    func = getattr(store, "list_suspects", None)
    if callable(func):
        try:
            result = func(resolved=resolved)
        except TypeError as exc:
            # 预期控制流：兼容不支持 resolved= 关键字的简化实现。
            # 只有报错确实与 resolved 关键字相关才降级重试，其余 TypeError 原样抛出。
            if "resolved" not in str(exc):
                raise
            result = func()
        if result:
            return [dict(item) for item in result if isinstance(item, Mapping)]
        return []
    # 兼容实现：疑似记录存在 meta.json 的 suspects 字段（不推荐，但读取是无害的）
    loader = getattr(store, "load_index", None)
    if callable(loader):
        index = loader() or {}
        if isinstance(index, Mapping):
            records = index.get("suspects") or []
            out = [dict(item) for item in records if isinstance(item, Mapping)]
            if resolved is None:
                return out
            return [r for r in out if bool(r.get("resolved", False)) == bool(resolved)]
    raise SuspectPersistenceError(
        "AssetStore 未提供 list_suspects()，无法读取疑似重复队列；"
        "禁止静默丢弃疑似重复记录（CONTRACTS §5 / R1）"
    )


def _new_suspect_id(
    store: Any,
    normalized_name: str,
    *,
    id_factory: Callable | None = None,
    existing_ids: Iterable[str] = (),
) -> str:
    """生成 SUSPECT_* ID：显式工厂 -> store 钩子 -> store.ids 模块（R6 查重）。"""
    factory = id_factory or getattr(store, "new_suspect_id", None)
    if factory is None:
        try:
            from ..store import ids as store_ids  # CONTRACTS §2 唯一入口
        except ImportError as exc:  # pragma: no cover - store 未就绪时
            raise PromptDependencyError(
                "无法生成 suspect_id：store 未提供 new_suspect_id，"
                "且 bettercomfyui.store.ids 尚不可用"
            ) from exc
        factory = getattr(store_ids, "new_suspect_id", None)
    if not callable(factory):
        raise PromptDependencyError(
            "store.ids.new_suspect_id 不存在（CONTRACTS §2 要求 ID 生成必须走唯一入口）"
        )

    known = {str(x) for x in existing_ids}
    for _ in range(32):
        try:
            parameters = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            # 预期控制流：内建/被装饰的可调用对象没有可反射签名 -> 退化为「按无参调用」。
            # 不吞错：真正的调用失败会在下面正常抛出。
            parameters = {}
        if "normalized_name" in parameters:
            candidate = factory(normalized_name=normalized_name)
        elif "name" in parameters:
            candidate = factory(name=normalized_name)
        elif len(parameters) >= 1:
            candidate = factory(normalized_name)
        else:
            candidate = factory()
        text = str(candidate)
        if text and text not in known:  # R6：短码冲突检测
            return text
    raise SuspectPersistenceError("suspect_id 连续 32 次生成都冲突，请检查 ID 生成器（R6）")


def _call_suspect_recorder(recorder: Callable, record: dict) -> dict:
    """兼容三种落盘签名：关键字 / payload= / 单位置参数。

    签名不可反射时退化为单位置参数调用；调用失败会正常抛出（不吞错）。
    """
    try:
        parameters = inspect.signature(recorder).parameters
    except (TypeError, ValueError):
        # 预期控制流：无签名可反射 -> 退化为位置参数调用（见下方 return）。
        parameters = {}
    if "suspect_id" in parameters:
        return dict(recorder(**record) or record)
    if "payload" in parameters:
        return dict(recorder(payload=record) or record)
    if "record" in parameters:
        return dict(recorder(record=record) or record)
    return dict(recorder(record) or record)


def _persist_suspect(
    store: Any,
    record: dict,
    *,
    persist: Callable | None = None,
) -> dict:
    recorder = persist or getattr(store, "record_suspect", None)
    if recorder is None or not callable(recorder):
        raise SuspectPersistenceError(
            "AssetStore 未提供 record_suspect()，疑似重复记录无法落盘；"
            "禁止静默丢弃（CONTRACTS §5：SUSPECT_DUPLICATE 必须建 SUSPECT_* 记录）"
        )
    try:
        return _call_suspect_recorder(recorder, record)
    except SuspectPersistenceError:
        raise
    except Exception as exc:  # 包装为显式异常类型后抛出，绝不静默丢弃疑似记录
        raise SuspectPersistenceError(f"疑似重复记录落盘失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _parse_scope(raw: Any) -> LibraryScope:
    if raw is None:
        return LibraryScope.PROJECT
    if isinstance(raw, LibraryScope):
        return raw
    text = str(raw).strip()
    for variant in (text, text.upper(), text.lower()):
        try:
            return LibraryScope.parse(variant)
        except ValueError:
            continue  # 预期控制流：逐个大小写变体尝试；全部失败后在下方显式报错
    raise PromptDependencyError(
        f"candidate.library_scope={raw!r} 非法；允许值："
        f"{', '.join(m.value for m in LibraryScope)}"
    )


def _asset_name(asset: Mapping[str, Any]) -> str:
    value = asset.get("name")
    return value if isinstance(value, str) else ""


def _asset_id(asset: Mapping[str, Any]) -> str:
    for key in ("asset_id", "id"):
        value = asset.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _candidate_summary(asset: Mapping[str, Any]) -> dict:
    return {
        "asset_id": _asset_id(asset),
        "name": _asset_name(asset),
        "asset_type": str(asset.get("asset_type") or ""),
        "normalized_name": str(asset.get("normalized_name") or normalize_name(_asset_name(asset))),
        "versions": _versions_of(asset),
        "lock_status": str(asset.get("lock_status") or ""),
    }


def match_candidate(
    candidate: dict,
    store: Any,
    *,
    id_factory: Callable | None = None,
    persist: Callable | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MatchResult:
    """三分支确定性匹配（CONTRACTS §5）。**纯程序逻辑，不调用 AI。**

    :param candidate: 至少包含 ``{"name": str}``；可选 ``asset_type`` /
        ``library_scope`` / ``source`` / ``prompt`` / ``version``。
    :param store: ``AssetStore``（或其契约替身）。
    :param id_factory: 可注入的 ``suspect_id`` 生成器（默认走 store / store.ids）。
    :param persist: 可注入的疑似记录落盘函数（默认 ``store.record_suspect``）。
    :param clock: 可注入时钟（默认 UTC now），用于记录 ``created_at``。
    """
    if not isinstance(candidate, Mapping):
        raise PromptDependencyError(
            f"candidate 必须是 dict，收到 {type(candidate).__name__}"
        )
    name = candidate.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PromptDependencyError("candidate 必须包含非空字符串字段 name（素材名）")

    normalized = normalize_name(name)
    if not normalized:
        raise PromptDependencyError(f"candidate.name={name!r} 归一化后为空，无法做确定性匹配")

    scope = _parse_scope(candidate.get("library_scope"))

    # ---- 分支一/二：精确匹配（项目库优先，其次公共库） -----------------
    matches = _find_by_normalized_name(store, normalized, LibraryScope.PROJECT)
    matched_scope = LibraryScope.PROJECT
    public_only = False
    if not matches:
        public_matches = _find_by_normalized_name(store, normalized, LibraryScope.PUBLIC)
        if public_matches:
            matches = public_matches
            matched_scope = LibraryScope.PUBLIC
            public_only = True

    if len(matches) == 1:
        asset = matches[0]
        asset_id = _asset_id(asset)
        version, note = select_reusable_version(store, asset)
        prefix = "public" if public_only else "project"
        return MatchResult(
            MatchOutcome.EXISTS_REUSE,
            asset_id or None,
            version,
            None,
            f"exact_match:{prefix}:scope={matched_scope.value}:{note}",
        )

    if len(matches) > 1:
        candidates = [_candidate_summary(a) for a in matches]
        reason = f"ambiguous_multiple_assets:{len(matches)}:scope={matched_scope.value}"
        return _suspect_result(
            store,
            name=name,
            normalized=normalized,
            candidate=candidate,
            scope=matched_scope,
            candidates=candidates,
            reason=reason,
            id_factory=id_factory,
            persist=persist,
            clock=clock,
        )

    # ---- 分支三：无精确匹配 -> 检查疑似重复 ----------------------------
    existing = _list_assets(store, LibraryScope.PROJECT) + _list_assets(store, LibraryScope.PUBLIC)
    similar: list[dict] = []
    for asset in existing:
        other_name = _asset_name(asset)
        if not other_name:
            continue
        other_norm = normalize_name(other_name)
        if not other_norm or other_norm == normalized:
            continue
        if is_suspect(normalized, [other_name], threshold=SUSPECT_SIMILARITY_THRESHOLD):
            similar.append(asset)

    if similar:
        candidates = [_candidate_summary(a) for a in similar]
        names = ", ".join(sorted({_asset_name(a) for a in similar if _asset_name(a)}))
        reason = f"suspect_similar_name:{names}"
        asset_id = _asset_id(similar[0]) if len(similar) == 1 else None
        return _suspect_result(
            store,
            name=name,
            normalized=normalized,
            candidate=candidate,
            scope=scope,
            candidates=candidates,
            reason=reason,
            id_factory=id_factory,
            persist=persist,
            clock=clock,
            hint_asset_id=asset_id,
        )

    # ---- 分支四：确定不存在 -> 进入生成队列 ----------------------------
    return MatchResult(
        MatchOutcome.NEW_GENERATE,
        None,
        None,
        None,
        "no_match:no_similar_name",
    )


def _derive_asset_type(candidate: Mapping[str, Any], candidates: list[dict]) -> AssetType:
    """确定疑似记录的 asset_type。

    ``store.record_suspect`` 要求合法 :class:`AssetType`（不允许空值，否则
    SUSPECT 记录会落盘失败——那等于把疑似重复丢掉）。取值优先级：
    candidate.asset_type -> 冲突候选的 asset_type；都没有则显式报错。
    """
    raw = candidate.get("asset_type")
    if not (isinstance(raw, str) and raw.strip()):
        for item in candidates:
            value = item.get("asset_type")
            if isinstance(value, str) and value.strip():
                raw = value
                break
    if not (isinstance(raw, str) and raw.strip()):
        raise PromptDependencyError(
            "SUSPECT 记录需要 asset_type：candidate 与其冲突候选都没有提供；"
            "store.record_suspect 要求合法 AssetType，缺它无法落盘（绝不静默丢弃）"
        )
    text = raw.strip()
    for variant in (text, text.upper()):
        try:
            return AssetType.parse(variant)
        except ValueError:
            continue  # 预期控制流：大小写变体尝试；全部失败后在下方显式报错
    allowed = ", ".join(m.value for m in AssetType)
    raise PromptDependencyError(f"asset_type={raw!r} 非法；允许值：{allowed}")


def _suspect_result(
    store: Any,
    *,
    name: str,
    normalized: str,
    candidate: Mapping[str, Any],
    scope: LibraryScope,
    candidates: list[dict],
    reason: str,
    id_factory: Callable | None,
    persist: Callable | None,
    clock: Callable[[], datetime] | None,
    hint_asset_id: str | None = None,
) -> MatchResult:
    """统一的 SUSPECT_DUPLICATE 出口：复用既有记录或新建记录（幂等）。"""
    existing = _list_existing_suspects(store, resolved=False)
    existing_by_sig: dict[tuple[str, str], dict] = {}
    for record in existing:
        existing_by_sig.setdefault(_suspect_signature(record), record)

    signature = (normalized, normalize_name(name))
    found = existing_by_sig.get(signature)
    if found is not None:
        suspect_id = str(found.get("suspect_id") or "")
        return MatchResult(
            MatchOutcome.SUSPECT_DUPLICATE,
            hint_asset_id or found.get("asset_id") or None,
            None,
            suspect_id or None,
            f"{reason}:reused_existing_suspect({suspect_id})",
        )

    now = (clock or (lambda: datetime.now(timezone.utc)))()
    suspect_id = _new_suspect_id(
        store,
        normalized,
        id_factory=id_factory,
        existing_ids=[str(r.get("suspect_id") or "") for r in existing],
    )
    asset_type = _derive_asset_type(candidate, candidates)
    record = {
        "suspect_id": suspect_id,
        "name": name,
        "normalized_name": normalized,
        "asset_type": asset_type.value,
        "library_scope": scope.value,
        "candidates": candidates,
        "candidate_payload": dict(candidate),
        "reason": reason,
        "resolved": False,
        "created_at": now.isoformat(),
    }
    stored = _persist_suspect(store, record, persist=persist)
    return MatchResult(
        MatchOutcome.SUSPECT_DUPLICATE,
        hint_asset_id or None,
        None,
        str(stored.get("suspect_id") or suspect_id),
        reason,
    )


def build_suspect_queue(store: Any, *, resolved: bool | None = False) -> list[dict]:
    """读取疑似重复队列（CONTRACTS §5）。确定性排序，便于 UI/测试比对。"""
    records = _list_existing_suspects(store, resolved=resolved)
    return sorted(
        records,
        key=lambda r: (str(r.get("created_at") or ""), str(r.get("suspect_id") or "")),
    )
