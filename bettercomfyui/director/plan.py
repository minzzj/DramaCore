"""段计划（J_IN -> J_PARAM -> O -> P 的 P 侧输入，契约 §7 `plan_segments`）。

消费 `prompt/validate.py::validate_script` 冻结后的规范结构：
```json
{"chapters":[{"chapter_id":"CH001","segments":[
    {"segment_index":1,"prompt":"...","asset_refs":["A1","林澈"],
     "duration_s":10,"first_frame_ref":"A2","last_frame_ref":"A2",
     "generation_params":{"resolution":"1080P"}}]}]}
```
要点：
1. `asset_refs` 是 **引用字符串**（asset id / 名称），**不含版本**。
   版本解析属于 R3：由 `locked_assets`（或 store.resolve_lock）给出，本模块只认「有具体版本号」的引用。
   解析不出唯一版本 -> 抛 `LockGateError`，绝不猜版本、绝不新建版本。
2. 校验不通过**绝不返回部分结果**（任务书第 9 节验收 2）。
3. 输出 `list[models.entities.Segment]`（Lead 指定的唯一数据模型，带 `__post_init__` 守卫）。
   需要落盘时用 `Segment.to_dict()`。

依赖引用编码（`Segment.dependency_refs` 是 `list[str]`，实体不承载依赖种类，
故用显式编码，不引入平行数据结构）：`"<segment_id>|<kind>|<ref_facet>"`，其中
`ref_facet` 未知时写 `-`。解析见 `parse_dependency_ref`。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from bettercomfyui.director.deps import IdFactory, default_ids
from bettercomfyui.director.errors import (
    DirectorDependencyError,
    LockGateError,
    SegmentPlanError,
)
from bettercomfyui.director.settings import cfg_get, cfg_section
from bettercomfyui.models.entities import Config, Segment, SegmentOverrides
from bettercomfyui.models.enums import AuditScope, SegmentSource, SegmentStatus

__all__ = [
    "DEPENDENCY_KINDS",
    "FACET_KINDS",
    "UNKNOWN_FACET",
    "dependency_edge",
    "encode_dependency_ref",
    "ensure_segment",
    "facet_value",
    "locked_asset_lookup",
    "parse_dependency_ref",
    "plan_segments",
    "record_segment_asset_references",
]

#: 依赖类别（同时是「段指纹」维度）：只有这些维度的依赖才可能因变化而失效。
FACET_KINDS = (
    "first_frame",
    "last_frame",
    "reference_frame",
    "motion",
    "action",
    "timeline",
    "prompt",
)
DEPENDENCY_KINDS = frozenset(FACET_KINDS)

UNKNOWN_FACET = "-"
_SEG_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


# ---------------------------------------------------------------------------
# 依赖引用编码 / 指纹
# ---------------------------------------------------------------------------


def encode_dependency_ref(
    segment_id: str,
    kind: str,
    ref_facet: str | None = None,
    ref_version: str | None = None,
) -> str:
    if not segment_id:
        raise SegmentPlanError("dependency_ref.segment_id 不得为空")
    if kind not in DEPENDENCY_KINDS:
        raise SegmentPlanError(
            f"未知依赖类别 {kind!r}；允许：{sorted(DEPENDENCY_KINDS)}"
        )
    return f"{segment_id}|{kind}|{ref_facet or UNKNOWN_FACET}|{ref_version or UNKNOWN_FACET}"


def parse_dependency_ref(raw: str | dict) -> dict:
    """解析依赖引用为 `{"segment_id","kind","ref_facet","ref_version"}`。非法输入直接抛错。"""
    if isinstance(raw, Mapping):
        segment_id = raw.get("segment_id") or raw.get("depends_on") or raw.get("id")
        kind = str(raw.get("kind") or raw.get("facet") or "reference_frame")
        ref_facet = raw.get("ref_facet")
        ref_version = raw.get("ref_version")
        if not segment_id:
            raise SegmentPlanError(f"dependency_refs 元素缺少 segment_id：{raw!r}")
        if kind not in DEPENDENCY_KINDS:
            raise SegmentPlanError(
                f"依赖类别非法：{kind!r}；允许：{sorted(DEPENDENCY_KINDS)}"
            )
        return {
            "segment_id": str(segment_id),
            "kind": kind,
            "ref_facet": None if ref_facet in (None, UNKNOWN_FACET) else str(ref_facet),
            "ref_version": None if ref_version in (None, UNKNOWN_FACET) else str(ref_version),
        }
    if not isinstance(raw, str) or not raw.strip():
        raise SegmentPlanError(f"dependency_refs 元素必须是非空字符串或映射，收到 {raw!r}")
    parts = raw.split("|")
    if len(parts) > 4:
        raise SegmentPlanError(f"dependency_refs 元素格式非法（最多 4 段）：{raw!r}")
    padded = list(parts) + [UNKNOWN_FACET] * (4 - len(parts))
    segment_id, kind, ref_facet, ref_version = padded
    if not kind or kind == UNKNOWN_FACET:
        kind = "reference_frame"
    if not segment_id or not _SEG_ID_RE.match(segment_id):
        raise SegmentPlanError(f"dependency_refs 中的 segment_id 非法：{segment_id!r}")
    if kind not in DEPENDENCY_KINDS:
        raise SegmentPlanError(
            f"dependency_refs 中的依赖类别非法：{kind!r}；允许：{sorted(DEPENDENCY_KINDS)}"
        )
    return {
        "segment_id": segment_id,
        "kind": kind,
        "ref_facet": None if ref_facet in (None, "", UNKNOWN_FACET) else ref_facet,
        "ref_version": None if ref_version in (None, "", UNKNOWN_FACET) else ref_version,
    }


def ensure_segment(obj: Any, *, where: str = "director") -> Segment:
    """把 `Segment` 或等价 dict 统一成 `models.entities.Segment`。

    契约 §7 的 `plan_segments` 返回类型写作 `list[dict]`，而项目唯一数据模型是
    `models.entities.Segment`（Lead 指定）。为兼容两种调用方，本层所有公开接口
    都接受 `Segment | Mapping`；传入 dict 时用 `Segment.from_dict` 还原并保留守卫。
    """
    if isinstance(obj, Segment):
        return obj
    if isinstance(obj, Mapping):
        try:
            return Segment.from_dict(dict(obj))
        except (TypeError, ValueError) as exc:
            raise SegmentPlanError(
                f"{where} 收到的段 dict 无法还原为 Segment：{type(exc).__name__}: {exc}"
            ) from exc
    raise DirectorDependencyError(
        f"{where} 需要 models.entities.Segment 或等价 dict，收到 {type(obj).__name__}"
    )


def dependency_edge(segment: Segment) -> list[dict]:
    """取出某段声明的全部依赖边。"""
    return [parse_dependency_ref(r) for r in (segment.dependency_refs or [])]


def facet_value(segment: Segment, kind: str) -> str | None:
    """段在指定维度上的当前指纹。取不到返回 None（表示**无证据**，不得据此判定失效）。"""
    if kind not in DEPENDENCY_KINDS:
        raise SegmentPlanError(f"未知依赖类别 {kind!r}")
    if kind == "prompt":
        return segment.prompt or None
    attempt = segment.current_attempt()
    if attempt is None:
        return None
    if kind == "first_frame":
        return attempt.first_frame_path or attempt.content_hash
    if kind == "last_frame":
        return attempt.last_frame_path or attempt.content_hash
    if kind in ("reference_frame", "motion", "action", "timeline"):
        return attempt.content_hash or attempt.video_path
    return None


# ---------------------------------------------------------------------------
# R3：locked_assets 解析
# ---------------------------------------------------------------------------


def locked_asset_lookup(locked_assets: Any) -> Callable[[str], dict | None]:
    """把多种 `locked_assets` 形态归一化为 `ref -> {"asset_id","version",...}` 查询函数。

    支持（按优先级）：
    1. 可调用对象：`locked_assets(ref) -> dict|None`
    2. `{ref: {"asset_id","version",...}}`
    3. `{ref: [ {...}, ... ]}`（必须**恰好一个**候选，否则视为歧义 -> 报错）
    4. `{ref: "v2"}`（版本字符串，asset_id 取 ref）
    5. `{(asset_id, version): {...}}` / `{asset_id: {version: {...}}}`
    查不出唯一可用版本时由调用方抛 `LockGateError`。
    """
    if locked_assets is None:
        return lambda ref: None
    if callable(locked_assets):
        return lambda ref: locked_assets(ref)

    if not isinstance(locked_assets, Mapping):
        raise LockGateError(
            f"locked_assets 必须是映射或可调用对象，收到 {type(locked_assets).__name__}"
        )

    def lookup(ref: str) -> dict | None:
        if ref in locked_assets:
            value = locked_assets[ref]
            # {asset_id: {version: entry}} 形态：外层键是 asset_id，内层键是版本号
            if (
                isinstance(value, Mapping)
                and "asset_id" not in value
                and "version" not in value
                and value
                and all(isinstance(v, Mapping) for v in value.values())
            ):
                entries = [
                    _normalize_entry(ref, v, version_hint=str(k)) for k, v in value.items()
                ]
                return _single(ref, entries)
            return _normalize_entry(ref, value)
        # (asset_id, version) 复合键
        hits = []
        for key, value in locked_assets.items():
            if isinstance(key, tuple) and len(key) == 2 and str(key[0]) == ref:
                entry = _normalize_entry(ref, value)
                if entry and not entry.get("version"):
                    entry["version"] = str(key[1])
                hits.append(entry)
        if hits:
            return _single(ref, hits)
        # 嵌套 {asset_id: {version: entry}}
        nested = locked_assets.get(ref)
        if isinstance(nested, Mapping):
            entries = [_normalize_entry(ref, v, version_hint=str(k)) for k, v in nested.items()]
            entries = [e for e in entries if e]
            if entries:
                return _single(ref, entries)
        return None

    return lookup


def _single(ref: str, entries: list[dict | None]) -> dict | None:
    clean = [e for e in entries if e]
    if not clean:
        return None
    if len(clean) > 1:
        versions = sorted({str(e.get("version")) for e in clean})
        raise LockGateError(
            f"素材引用 {ref!r} 解析出多个候选版本 {versions}，歧义引用禁止进入生成（R1/R3）",
            detail={"ref": ref, "versions": versions},
        )
    return clean[0]


def _normalize_entry(ref: str, value: Any, version_hint: str | None = None) -> dict | None:
    if value is None or value is False:
        return None
    if isinstance(value, list):
        return _single(ref, [_normalize_entry(ref, v) for v in value])
    if isinstance(value, str):
        return {"asset_id": ref, "version": value}
    if isinstance(value, Mapping):
        entry = dict(value)
        entry.setdefault("asset_id", ref)
        if not entry.get("version"):
            entry["version"] = version_hint or entry.get("current_version")
        return entry
    raise LockGateError(f"locked_assets[{ref!r}] 类型非法：{type(value).__name__}")


# ---------------------------------------------------------------------------
# 段计划
# ---------------------------------------------------------------------------


def _project_id_from_cfg(cfg: Any) -> str | None:
    for path in ("project_id", "project.id", "paths.project_id"):
        value = cfg_get(cfg, path, None)
        if value:
            return str(value)
    return None


def _project_id_from_store(store: Any) -> str | None:
    if store is None:
        return None
    direct = getattr(store, "project_id", None)
    if isinstance(direct, str) and direct:
        return direct
    paths = getattr(store, "paths", None)
    nested = getattr(paths, "project_id", None)
    return str(nested) if nested else None


def _chapter_key(chapter: Mapping, position: int, ids: IdFactory) -> str:
    """章节 ID：兼容 `chapter_id`（prompt 层冻结字段）与 `id`（早期脚本形态）。"""
    raw = chapter.get("chapter_id") or chapter.get("id")
    return str(raw) if raw else ids.chapter_id(position)


def plan_segments(
    script: dict,
    cfg: Config | dict | Any,
    *,
    locked_assets: Any,
    ids: IdFactory | None = None,
    project_id: str | None = None,
    store: Any | None = None,
    as_dicts: bool = False,
) -> list:
    """建立段计划（契约 §7）。校验不通过抛错，**绝不返回部分结果**。

    Args:
        script: `validate_script` 的规范输出（chapters[].segments[]）。
        cfg: `models.entities.Config` 或等价映射。
        locked_assets: R3 门禁数据源；见 `locked_asset_lookup`。
        ids: ID 生成入口（默认 `store.ids`，契约 §2 唯一入口）。
        project_id: 项目 ID；不在 script 里（prompt 层不写 UUID）。
        store: 可选，若提供则额外用 `store.resolve_lock(asset_id, version)` 复核每个引用。
        as_dicts: True 时返回 `list[dict]`（用 `Segment.to_dict()`），供直接落盘。
    """
    if not isinstance(script, Mapping):
        raise SegmentPlanError(f"script 必须是映射，收到 {type(script).__name__}")
    chapters = script.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise SegmentPlanError("script.chapters 必须是非空列表（无章节则不得建立段计划）")
    flat_segments = script.get("segments")
    if not chapters and not flat_segments:
        raise SegmentPlanError("script 既没有 chapters 也没有 segments")

    # project_id 延迟解析：先做输入校验与 R3 引用门禁（更可操作的错误优先），
    # 再解析项目身份并生成段 ID。
    pid_holder: dict[str, str] = {}

    def ensure_pid() -> str:
        if not pid_holder:
            resolved = (
                project_id
                or script.get("project_id")
                or _project_id_from_cfg(cfg)
                or _project_id_from_store(store)
            )
            if not resolved:
                raise SegmentPlanError(
                    "缺少 project_id：项目隔离要求任何段 ID 都必须包含 project_id。"
                    "提供方式（任一）：① plan_segments(..., project_id='...')；"
                    "② script['project_id']；③ cfg.project_id；"
                    "④ 传入 store（AssetStore.project_id 自动读取）。"
                )
            pid_holder["pid"] = str(resolved)
        return pid_holder["pid"]

    id_factory = ids if ids is not None else default_ids()
    lookup = locked_asset_lookup(locked_assets)

    director_cfg = cfg_section(cfg, "director")
    video_cfg = cfg_section(cfg, "video_review")
    audit_scope = video_cfg.get("audit_scope") or cfg_get(cfg, "video_review.audit_scope", AuditScope.BOTH)
    if not isinstance(audit_scope, AuditScope):
        audit_scope = AuditScope.parse(audit_scope)

    planned: list[Segment] = []
    by_id: dict[str, Segment] = {}
    global_index = 0
    auto_chain = bool(cfg_get(cfg, "director.auto_chain", True))

    chapter_list = chapters if isinstance(chapters, list) and chapters else [{"segments": flat_segments}]
    for ch_pos, chapter in enumerate(chapter_list, start=1):
        if not isinstance(chapter, Mapping):
            raise SegmentPlanError(f"chapters[{ch_pos - 1}] 必须是映射")
        chapter_id = _chapter_key(chapter, ch_pos, id_factory)
        chapter_last: Segment | None = None
        segments = chapter.get("segments")
        if not isinstance(segments, list) or not segments:
            raise SegmentPlanError(f"{chapter_id} 的 segments 必须是非空列表")
        for seg_pos, raw in enumerate(segments, start=1):
            if not isinstance(raw, Mapping):
                raise SegmentPlanError(f"{chapter_id} segments[{seg_pos - 1}] 必须是映射")
            global_index += 1
            seg_no = raw.get("segment_index") or raw.get("index") or seg_pos
            try:
                seg_no_int = int(seg_no)
            except (TypeError, ValueError) as exc:
                raise SegmentPlanError(
                    f"{chapter_id} segments[{seg_pos - 1}].segment_index 非法：{seg_no!r}"
                ) from exc
            prompt = raw.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise SegmentPlanError(
                    f"{chapter_id} SEG{seg_no} 缺少非空 prompt（缺字段不得进入生成）"
                )
            segment_id = id_factory.segment_id(ensure_pid(), ch_pos, seg_no_int)

            asset_refs = _resolve_asset_refs(
                raw.get("asset_refs"), lookup, store=store, where=f"{chapter_id} SEG{seg_no}"
            )
            overrides = raw.get("generation_params") or {}
            if overrides and not isinstance(overrides, Mapping):
                raise SegmentPlanError(
                    f"{chapter_id} SEG{seg_no} 的 generation_params 必须是映射"
                )

            # 单段审核覆盖（任务书第 3 节 E：「单段覆盖必须显式记录」；
            # 第 4 节把 audit_scope/threshold/retry_limit/timeout 列为**视频段**字段）。
            # 剧本里用 `review_overrides` 声明。
            rev_over = raw.get("review_overrides") or {}
            if rev_over and not isinstance(rev_over, Mapping):
                raise SegmentPlanError(
                    f"{chapter_id} SEG{seg_no} 的 review_overrides 必须是映射"
                )
            rev_reason = str(
                raw.get("override_reason") or rev_over.get("reason") or ""
            ).strip()
            # 生成参数覆盖已有 `params_source` 标记（可追踪即可）；
            # **审核参数覆盖**会改变"这一段算不算通过"的判定，风险更高，
            # 因此额外要求写明原因 —— 否则事后无法判断某个段为什么被判通过。
            if rev_over and not rev_reason:
                raise SegmentPlanError(
                    f"{chapter_id} SEG{seg_no} 声明了 review_overrides 却没有 `override_reason`。\n"
                    "审核参数（audit_scope/threshold/retry_limit/timeout）直接决定"
                    "「这一段算不算通过」，覆盖它必须写明原因，"
                    "否则事后无法判断覆盖是否合理。"
                )

            gen_over = {k: v for k, v in dict(overrides).items() if k != "reason"}
            segment_overrides = SegmentOverrides(
                audit_scope=rev_over.get("audit_scope"),
                threshold=rev_over.get("threshold"),
                retry_limit=rev_over.get("retry_limit"),
                timeout_s=(rev_over.get("timeout_s") or rev_over.get("timeout")),
                generation_params=gen_over,
                reason=rev_reason,
                by=str(rev_over.get("by") or "human"),
            )

            generation_params = {
                "segment_seconds": raw.get("duration_s", director_cfg.get("segment_seconds", 10)),
                "resolution": director_cfg.get("resolution", "1080P"),
                "fps": director_cfg.get("fps", 24),
                "tail_frames": director_cfg.get("tail_frames", 22),
                "vram_tier": director_cfg.get("vram_tier", "auto"),
                "pruned_model": director_cfg.get("pruned_model", True),
                "reference_mode": director_cfg.get("reference_mode", "auto"),
            }
            generation_params.update(gen_over)
            generation_params["overrides"] = dict(gen_over)
            generation_params["params_source"] = "segment_override" if gen_over else "global"
            # 审核参数同样记录下来，便于计划文件自解释
            generation_params["review_overrides_effective"] = segment_overrides.resolve(cfg)[0]

            segment = Segment(
                segment_id=segment_id,
                segment_index=global_index,
                chapter_id=str(chapter_id),
                prompt=prompt,
                asset_refs=asset_refs,
                task_mode=str(raw.get("task_mode") or "segment"),
                segment_duration=float(
                    raw.get("duration_s", director_cfg.get("segment_seconds", 10)) or 10
                ),
                resolution=str(generation_params.get("resolution")),
                fps=int(generation_params.get("fps") or 24),
                first_frame=str(
                    raw.get("first_frame_ref")
                    or raw.get("first_frame")
                    or director_cfg.get("first_frame", "first")
                ),
                last_frame=str(
                    raw.get("last_frame_ref")
                    or raw.get("last_frame")
                    or director_cfg.get("last_frame", "last")
                ),
                generation_params=generation_params,
                current_version=str(raw.get("current_version") or "v1"),
                status=SegmentStatus.PLANNED,
                source=SegmentSource.NORMAL,
                audit_scope=audit_scope,
                overrides=segment_overrides,
            )
            segment.dependency_refs = _resolve_dependency_refs(
                raw.get("dependency_refs"), planned, by_id, where=segment_id
            )
            if auto_chain and not segment.dependency_refs and chapter_last is not None:
                # O1 落地：同章节相邻段默认用「上一段尾部帧」衔接（自动分段与衔接）。
                # 记录 ref_version 作为级联失效判定的证据（方案二·7/9）。
                segment.dependency_refs = [
                    encode_dependency_ref(
                        chapter_last.segment_id,
                        "last_frame",
                        None,
                        chapter_last.current_version,
                    )
                ]
            planned.append(segment)
            by_id[segment_id] = segment
            chapter_last = segment

    if not planned:
        raise SegmentPlanError("段计划为空：没有任何合法段（不得返回空计划当作成功）")

    # 记录「素材 -> 段」反向索引（图七 GF「引用次数」/ 图四 F2B 依赖它）。
    # 只在调用方给了 store 时记录 —— 计划本身不依赖存储层。
    if store is not None:
        record_segment_asset_references(store, planned)

    return [s.to_dict() for s in planned] if as_dicts else planned


def record_segment_asset_references(store: Any, segments: Any) -> int:
    """把「哪些段引用了哪个素材版本」写进素材的 ``references``。返回写入条数。

    ## 为什么必须显式记录（这是 ROUND17 发现的缺口）

    `AssetStore.record_reference()` 早就实现了，但**产品代码零调用** ——
    实测跑完整个 `run_demo`，所有素材的 `references` 都是空的。后果：

    1. **图七 `GF` 素材条目契约里的「引用次数」永远无值**；
    2. **图一 SG8 `ERR1` 中程失败**「仅重跑依赖该素材且未通过的段」算不出来；
    3. **图四 `F2B`**「仅重跑依赖该角色且未通过的段」无法实现 ——
       这是愿景「爆炸半径」的核心（只重跑受影响的部分，已通过的用缓存保留）。
       没有反向索引就只能**整条重跑**，与「算力成本和质量取中间值」的原则冲突。

    记录内容刻意保持最小：`segment_id` / `segment_index` / `chapter_id`。
    不写 prompt 等大字段 —— 反向索引只需要「谁引用了我」。
    """
    if store is None or not hasattr(store, "record_reference"):
        return 0
    written = 0
    for seg in segments or []:
        seg_id = getattr(seg, "segment_id", None)
        if not seg_id:
            continue
        idx = getattr(seg, "segment_index", None)
        chapter_id = getattr(seg, "chapter_id", None)
        for ref in getattr(seg, "asset_refs", None) or []:
            if not isinstance(ref, Mapping):
                continue
            asset_id = ref.get("asset_id")
            version = ref.get("version")
            if not asset_id or not version:
                continue
            payload = {
                "segment_id": str(seg_id),
                "segment_index": idx,
                "chapter_id": chapter_id,
                "source": "director_plan",
            }
            try:
                store.record_reference(str(asset_id), str(version), payload)
                written += 1
            except Exception:  # noqa: BLE001
                # 记录失败**不应**阻断段计划（素材本身仍可用），
                # 但也不能静默当成成功 —— 计数照实返回，少记了调用方能看到。
                continue
    return written


def _resolve_asset_refs(
    raw_refs: Any, lookup: Callable[[str], dict | None], *, store: Any | None, where: str
) -> list[dict]:
    if raw_refs in (None, []):
        return []
    if not isinstance(raw_refs, list):
        raise SegmentPlanError(f"{where} 的 asset_refs 必须是列表，收到 {type(raw_refs).__name__}")
    resolved: list[dict] = []
    for ref in raw_refs:
        if isinstance(ref, Mapping):
            ref_key = ref.get("ref") or ref.get("asset_id") or ref.get("name")
            explicit_version = ref.get("version")
            kind = ref.get("kind")
        else:
            ref_key, explicit_version, kind = ref, None, None
        if not ref_key:
            raise SegmentPlanError(f"{where} 的 asset_refs 元素缺少引用名：{ref!r}")
        entry = lookup(str(ref_key))
        if entry is None and explicit_version:
            entry = {"asset_id": str(ref_key), "version": str(explicit_version)}
        if entry is None:
            raise LockGateError(
                f"{where} 引用了没有已 LOCK 版本的素材 {ref_key!r}："
                "R3 要求只有 resolve_lock() 通过的版本才能进入生成",
                detail={"where": where, "ref": str(ref_key)},
            )
        asset_id = str(entry.get("asset_id") or ref_key)
        version = entry.get("version") or explicit_version
        if not version:
            raise LockGateError(
                f"{where} 的素材引用 {ref_key!r} 没有具体版本号："
                "LOCK 绑定 asset_id+version+content_hash，无版本号不得进入生成",
                detail={"where": where, "asset_id": asset_id},
            )
        if store is not None and hasattr(store, "resolve_lock"):
            lock = store.resolve_lock(asset_id, str(version))
            if not lock:
                raise LockGateError(
                    f"{where} 的素材引用 {asset_id}@{version} 未通过 store.resolve_lock（R3 门禁）",
                    detail={"where": where, "asset_id": asset_id, "version": str(version)},
                )
            # 以 store 的真实 LOCK 记录为准补齐可核验字段（内容指纹 + LOCK 来源）
            for key in ("content_hash", "lock_source"):
                if lock.get(key):
                    entry = {**entry, key: lock[key]}
        item = {"asset_id": asset_id, "version": str(version)}
        if entry.get("content_hash"):
            item["content_hash"] = str(entry["content_hash"])
        if entry.get("lock_source"):
            item["lock_source"] = str(entry["lock_source"])
        if kind:
            item["kind"] = str(kind)
        item["ref"] = str(ref_key)
        resolved.append(item)
    return resolved


def _resolve_dependency_refs(
    raw_deps: Any, planned: list[Segment], by_id: dict[str, Segment], *, where: str
) -> list[str]:
    if raw_deps in (None, []):
        return []
    if not isinstance(raw_deps, list):
        raise SegmentPlanError(f"{where} 的 dependency_refs 必须是列表")
    out: list[str] = []
    for raw in raw_deps:
        edge = parse_dependency_ref(raw)
        target_id = edge["segment_id"]
        if target_id not in by_id:
            # 允许用「前一段」的相对写法
            if target_id in ("prev", "previous", "N-1"):
                if not planned:
                    raise SegmentPlanError(f"{where} 依赖前一段，但它已经是第一段")
                target_id = planned[-1].segment_id
                edge["segment_id"] = target_id
            else:
                raise SegmentPlanError(
                    f"{where} 依赖了尚未出现在计划中的段 {target_id!r}；"
                    "依赖必须指向更早的段（禁止前向/自引用）"
                )
        if target_id == where:
            raise SegmentPlanError(f"{where} 不得依赖自身")
        target = by_id[target_id]
        out.append(
            encode_dependency_ref(
                target_id,
                edge["kind"],
                edge["ref_facet"],
                edge.get("ref_version") or target.current_version,
            )
        )
    return out
