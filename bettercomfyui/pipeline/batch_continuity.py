# -*- coding: utf-8 -*-
"""ROUND55-3：跨批「尾帧参考」增强（**近似，绝不冒充原生 pin**）。

背景（只读参考，不改动）
------------------------
`bettercomfyui_comfy/one_stop_node.py::_generate_batches` 里 **批内** 的
`continuityFromPrev` 是 vendor 导演台原生《22 帧连续性》pin（首段永远不 pin）。
本模块 **不修改** 这条原生逻辑，也不写任何 `continuityFromPrev`。

**跨批** 无法使用 vendor 原生 pin（原生 pin 只存在于同一个 timeline 内），因此本
模块提供的只是「上一批最后 1-3 段各抽一帧尾帧 → 作为下一批首段的额外参考图」的
**近似衔接**，并在返回值/审计里显式标注，禁止标成 native：

    method              = "tail_frames_approx"
    continuity_mode     = "batch_tail_ref_approx"
    native_pin          = False
    approx              = True

字段语义
--------
- `extract_tail_frames(...)`：按 `-sseof` 抽尾帧；失败写 warnings，不静默。
- `build_cross_batch_refs(...)`：给 Lead 挂到下一批首段的参考图包（refs/audit/method）。
- `plan_batch_continuity(...)`：**纯函数**，只算参数与审计字段，不生成、不改输入。
- `suggest_overlap_frames(...)`：跨批重叠生成建议值（只算参数，留给后续 GPU 验证）。

本模块只依赖标准库；ffmpeg 定位复用 `bettercomfyui.pipeline.core._ffmpeg_exe`
（PATH → imageio-ffmpeg 自带二进制），导入期不做重活。

Lead 在 `_generate_batches` 的接入要点（建议，不在本模块执行）
------------------------------------------------------------
1. 分批计划算好后（`plan = lf.plan_batches(...)` 之后）::

       from bettercomfyui.pipeline import batch_continuity as bc
       ctx["batch_continuity_plan"] = bc.plan_batch_continuity(
           plan, enabled=bool(ctx["段间连续"]) and ctx["task_key"] == "r2v")
       ctx["cross_batch_overlap_frames"] = bc.suggest_overlap_frames(
           fps=float(ctx["帧率"]), overlap_ms=500)   # 仅审计；本步不改变生成
       audit.extend(ctx["batch_continuity_plan"]["audit"])

2. 批末（现有 `ctx["tail_ref"] = lf.extract_last_frame(...)` 处，替换为）::

       info = bc.build_cross_batch_refs(
           [str(seg_files[i]) for i in range(start, end)],
           ctx["seg_dir"] / f"tailrefs_b{bi + 1}",
           max_refs=3, warnings=ctx["warnings"])
       ctx["cross_batch_refs"] = info
       ctx["tail_ref"] = str(info["refs"][-1]) if info["refs"] else ""
       audit.extend(info["audit"])

3. 下一批首段（现有 `j == 0 and gi > 0` 的 `tail_ref` 追加处，改为）::

       refs = list(s.get("refs") or [])
       for ref in (ctx.get("cross_batch_refs") or {}).get("refs") or []:
           refs.append(dr.image_ref(str(ref), index=len(refs)))
       s["refs"] = refs
       s["cross_batch_continuity_mode"] = "batch_tail_ref_approx"
       s["cross_batch_refs_count"] = len(refs) - len(s.get("refs") or [])

4. 批记录（`state["batches"][str(bi)]`）追加审计字段::

       "continuity_mode": plan_row["continuity_mode"],
       "native_pin": False,
       "cross_batch_approx": bool(plan_row["cross_batch_approx"]),
       "tail_refs": len(info["refs"]),
       "overlap_frames_suggested": ctx["cross_batch_overlap_frames"],

批内原生 `continuityFromPrev` 保持原样（首段永远不 pin）——本模块绝不触碰。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

#: 跨批近似衔接的方法标签（审计用；不是 vendor 原生 pin）。
METHOD: str = "tail_frames_approx"
#: 审计里的连续性模式标记；出现该标记即代表"近似"，不是原生。
CONTINUITY_MODE: str = "batch_tail_ref_approx"
#: 每批最多取上一批的 3 段尾帧。
MAX_TAIL_REFS: int = 3
#: 抽尾帧最多往前找多少秒（短段更稳，与 director_longform.extract_last_frame 一致）。
SSEEF_SECONDS: float = 1.0
#: 单次 ffmpeg 抽帧超时（秒）。
FFMPEG_TIMEOUT_S: float = 60.0

__all__ = [
    "METHOD",
    "CONTINUITY_MODE",
    "MAX_TAIL_REFS",
    "ffmpeg_exe",
    "ffmpeg_available",
    "extract_tail_frames",
    "build_cross_batch_refs",
    "plan_batch_continuity",
    "suggest_overlap_frames",
]


# ---------------------------------------------------------------------------
# ffmpeg 定位（复用项目既有约定，找不到返回 None，绝不抛出到调用方之外）
# ---------------------------------------------------------------------------
def ffmpeg_exe() -> str | None:
    """返回可用的 ffmpeg 可执行文件路径；找不到返回 None（不抛异常）。"""
    try:
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        exe = _ffmpeg_exe()
        if exe and Path(str(exe)).is_file():
            return str(exe)
    except Exception:  # noqa: BLE001 - 没有 pipeline.core / 没有 imageio-ffmpeg 都算不可用
        pass
    found = shutil.which("ffmpeg")
    return found or None


def ffmpeg_available() -> bool:
    """ffmpeg 是否可用（测试用 skip 判断；不影响纯函数逻辑）。"""
    return ffmpeg_exe() is not None


def _append_warning(warnings: list[str] | None, message: str) -> None:
    """把告警写进调用方列表（Lead 的 ctx['warnings']）并不静默：同时记 logger。"""
    logger.warning(message)
    if warnings is not None:
        try:
            warnings.append(message)
        except AttributeError:  # pragma: no cover - 传进来的不是 list 时退化为只 logger
            pass


def _as_paths(seg_files: Any) -> list[Path]:
    """把段文件入参规范成 Path 列表（允许单个路径 / 任意可迭代）。"""
    if seg_files is None:
        return []
    if isinstance(seg_files, (str, Path)):
        return [Path(seg_files)]
    try:
        return [Path(str(x)) for x in seg_files]
    except TypeError:
        return [Path(str(seg_files))]


# ---------------------------------------------------------------------------
# 1) 抽尾帧
# ---------------------------------------------------------------------------
def _clamp_count(value: Any, *, name: str, warnings: list[str] | None) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数：{value!r}") from exc
    if count < 1:
        raise ValueError(f"{name} 必须 >= 1（实际 {count}）")
    if count > MAX_TAIL_REFS:
        _append_warning(
            warnings,
            f"⚠️ 跨批尾帧 {name}={count} 超过上限 {MAX_TAIL_REFS}，已按 {MAX_TAIL_REFS} 处理",
        )
        count = MAX_TAIL_REFS
    return count


def extract_tail_frames(
    seg_files: Iterable[str | Path] | str | Path,
    out_dir: str | Path,
    count: int = 3,
    warnings: list[str] | None = None,
) -> list[Path]:
    """从上批 **最后 1-3 段** 各抽最后一帧（ffmpeg `-sseof`）→ 下一批参考图。

    参数
    ----
    seg_files : 上一批已生成的段文件（按播放顺序；只会取最后 ``count`` 个存在的）。
    out_dir   : 尾帧输出目录（不存在会创建；建议 Lead 用 ``ctx["seg_dir"]/f"tailrefs_b{bi+1}"``）。
    count     : 取最后几个段，1..3；超过 3 会告警并截断。
    warnings  : 可选，告警会 append 进去（同时记 logger），失败绝不静默。

    返回
    ----
    成功抽到的尾帧 Path 列表（按段顺序旧→新）。全部失败/无 ffmpeg 时返回 ``[]``，
    并在 warnings 里给出原因——调用方据此降级为"无跨批参考图"，**不冒充原生 pin**。
    """
    count = _clamp_count(count, name="count", warnings=warnings)
    files = _as_paths(seg_files)
    out_path = Path(out_dir)
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _append_warning(warnings, f"⚠️ 跨批尾帧输出目录创建失败（{out_path}）：{exc}")
        return []

    usable: list[Path] = []
    missing = 0
    for src in files:
        try:
            if src.is_file() and src.stat().st_size > 0:
                usable.append(src)
            else:
                missing += 1
        except OSError:
            missing += 1
    if missing:
        _append_warning(
            warnings,
            f"⚠️ 跨批尾帧：上一批有 {missing} 个段文件不存在/为空，已跳过（不静默）",
        )
    chosen = usable[-count:]
    if not chosen:
        _append_warning(
            warnings,
            "⚠️ 跨批尾帧：上一批没有可用于抽尾帧的段文件，下一批将没有跨批近似参考图",
        )
        return []

    exe = ffmpeg_exe()
    if not exe:
        _append_warning(
            warnings,
            "⚠️ 未找到 ffmpeg（PATH 与 imageio-ffmpeg 都不可用）：跨批尾帧参考不可用，"
            "降级为无跨批参考（不冒充原生 pin）",
        )
        return []

    refs: list[Path] = []
    for idx, src in enumerate(chosen, start=1):
        out = out_path / f"tail_{idx:02d}_{src.stem}.jpg"
        cmd = [
            exe, "-y", "-v", "error",
            "-sseof", f"-{SSEEF_SECONDS:.1f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "2", str(out),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FFMPEG_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _append_warning(warnings, f"⚠️ 跨批尾帧抽取异常（{src.name}）：{exc}")
            continue
        ok = proc.returncode == 0 and out.is_file() and out.stat().st_size > 0
        if not ok:
            tail = (proc.stderr or "")[-300:]
            _append_warning(
                warnings,
                f"⚠️ 跨批尾帧抽取失败（{src.name}）：exit={proc.returncode} {tail}".strip(),
            )
            continue
        refs.append(out)

    if not refs:
        _append_warning(
            warnings,
            "⚠️ 跨批尾帧全部抽取失败：下一批没有跨批近似参考图（跨批连续如实降级，不冒充原生 pin）",
        )
    return refs


# ---------------------------------------------------------------------------
# 2) 构建跨批参考包（给 Lead 挂到下一批首段）
# ---------------------------------------------------------------------------
def build_cross_batch_refs(
    prev_seg_files: Iterable[str | Path] | str | Path,
    out_dir: str | Path,
    max_refs: int = MAX_TAIL_REFS,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """上一批 → 下一批的跨批参考包。

    返回
    ----
    ``{"refs": [Path, ...], "audit": [str, ...], "method": "tail_frames_approx",
       "native_pin": False, "approx": True, "ref_count": int, "max_refs": int}``

    严禁把 ``method`` 改成 native / 把 ``native_pin`` 置 True：跨批只有尾帧近似，
    vendor 原生 22 帧 pin 只在批内 timeline（本模块不参与）。
    """
    files = _as_paths(prev_seg_files)
    max_refs = _clamp_count(max_refs, name="max_refs", warnings=warnings)
    refs = extract_tail_frames(files, out_dir, count=max_refs, warnings=warnings)
    expected = min(len(files), max_refs)
    audit: list[str] = [
        f"跨批衔接：method={METHOD}（上一批尾帧参考近似；**不是** vendor 原生 22 帧 pin）",
        f"上一批段数={len(files)}；候选末段={expected}；抽取成功尾帧={len(refs)}/{expected}",
        "native_pin=False：本包只作为下一批首段额外参考图（refs），不写 "
        "continuityFromPrev；批内原生 pin 由 one_stop 原逻辑保持（首段永不 pin）",
    ]
    if refs:
        audit.append(f"尾帧参考文件：{', '.join(p.name for p in refs)}")
    else:
        audit.append("⚠️ 无可用的跨批尾帧参考：下一批按无跨批参考生成（如实降级，不冒充原生）")
    if len(refs) < expected:
        audit.append(f"⚠️ 尾帧参考不足：期望 {expected} 张、实际 {len(refs)} 张（失败原因见 warnings）")
    return {
        "refs": refs,
        "audit": audit,
        "method": METHOD,
        "native_pin": False,
        "approx": True,
        "ref_count": len(refs),
        "max_refs": max_refs,
    }


# ---------------------------------------------------------------------------
# 3) 分批连续性计划（纯函数）
# ---------------------------------------------------------------------------
def _normalize_batches(batches: Any) -> list[tuple[int, int]]:
    """把分批计划规范成 0-based ``[(start, end), ...]``；接受 tuple/list/dict。"""
    if batches is None:
        return []
    try:
        raw = list(batches)
    except TypeError as exc:
        raise ValueError(f"batches 必须是分批计划列表：{batches!r}") from exc
    normalized: list[tuple[int, int]] = []
    for i, item in enumerate(raw):
        start = end = None
        if isinstance(item, dict):
            if "segments" in item:
                seg = item["segments"]
                if isinstance(seg, (list, tuple)) and len(seg) == 2:
                    start, end = int(seg[0]) - 1, int(seg[1])
            else:
                a = item.get("start", item.get("a"))
                b = item.get("end", item.get("b"))
                if a is not None and b is not None:
                    start, end = int(a), int(b)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = int(item[0]), int(item[1])
        if start is None or end is None or start < 0 or end <= start:
            raise ValueError(f"batches[{i}] 不是合法的 0-based 分批区间：{item!r}")
        normalized.append((start, end))
    return normalized


def plan_batch_continuity(
    batches: Any,
    *,
    enabled: bool = True,
    max_refs: int = MAX_TAIL_REFS,
) -> dict[str, Any]:
    """**纯函数**：给出每批"尾帧参考数量 + 跨批近似标记"，不生成、不改输入。

    参数
    ----
    batches : 分批计划，0-based 区间 ``[(start, end), ...]``（与 ``director_longform.plan_batches``
              一致）；也接受 ``{"start":..,"end":..}`` 或 ``{"segments":[a,b]}``（1-based）。
    enabled : 是否启用跨批尾帧近似（建议 ``段间连续 and task_key == "r2v"``）。
    max_refs: 每批最多参考上一批末几段，1..3。

    返回
    ----
    ``{"enabled", "method", "continuity_mode", "native_pin": False, "max_refs",
       "batches": [{batch_index, segments, segment_count, prev_batch_segment_count,
                    tail_ref_count, cross_batch_approx, continuity_mode, native_pin,
                    note}], "expected_tail_refs", "approx_batches", "audit"}``

    - 首批 ``tail_ref_count=0``（没有上一批）；
    - 后续每批 ``tail_ref_count = min(max_refs, 上一批段数)``（最后 1-3 段各一帧）；
    - 只要 ``enabled=False`` 全部为 0、模式标 ``disabled``；
    - **任何情况下都不产生 native pin 标记**（``native_pin`` 恒为 False，
      不含 ``continuityFromPrev`` 字段）。
    """
    max_refs = _clamp_count(max_refs, name="max_refs", warnings=None)
    plan = _normalize_batches(batches)
    rows: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(plan):
        prev_count = (plan[i - 1][1] - plan[i - 1][0]) if i > 0 else 0
        if enabled and i > 0:
            tail_ref_count = min(max_refs, max(1, prev_count))
            approx = True
            mode = CONTINUITY_MODE
            note = f"取上一批最后 {tail_ref_count} 段各 1 帧尾帧作首段额外参考（近似）"
        else:
            tail_ref_count = 0
            approx = False
            if not enabled:
                mode = "disabled"
                note = "跨批尾帧近似已禁用（enabled=False）"
            else:
                mode = "none_first_batch"
                note = "首批没有上一批，不挂跨批尾帧参考"
        rows.append(
            {
                "batch_index": i + 1,
                "segments": [start + 1, end],
                "segment_count": end - start,
                "prev_batch_segment_count": prev_count,
                "tail_ref_count": tail_ref_count,
                "cross_batch_approx": approx,
                "continuity_mode": mode,
                "native_pin": False,
                "method": METHOD if approx else "none",
                "note": note,
            }
        )

    approx_batches = sum(1 for r in rows if r["cross_batch_approx"])
    expected_tail_refs = sum(int(r["tail_ref_count"]) for r in rows)
    audit: list[str] = [
        f"跨批尾帧参考计划：method={METHOD}，continuity_mode={CONTINUITY_MODE if enabled else 'disabled'}"
        "（**近似；不是 vendor 原生 22 帧 pin**）",
        f"批次数={len(rows)}；跨批近似批次数={approx_batches}；"
        f"每批尾帧参考数={[r['tail_ref_count'] for r in rows]}（上限 {max_refs}）",
        "native_pin=False：本计划不写 continuityFromPrev；批内原生 pin 仍由 one_stop 原逻辑处理"
        "（首段永不 pin，必须保持不变）",
    ]
    if not enabled:
        audit.append("跨批尾帧近似已禁用（enabled=False）：全部批次 tail_ref_count=0")
    elif not rows:
        audit.append("分批计划为空：没有可规划的跨批尾帧参考")
    return {
        "enabled": bool(enabled),
        "method": METHOD if enabled else "disabled",
        "continuity_mode": CONTINUITY_MODE if enabled else "disabled",
        "native_pin": False,
        "max_refs": max_refs,
        "batches": rows,
        "expected_tail_refs": expected_tail_refs,
        "approx_batches": approx_batches,
        "audit": audit,
    }


# ---------------------------------------------------------------------------
# 4) 跨批重叠生成建议（只算参数，不做生成）
# ---------------------------------------------------------------------------
def suggest_overlap_frames(fps: float = 24, overlap_ms: float = 500) -> int:
    """跨批"重叠生成"建议帧数 = ``fps * overlap_ms / 1000``（四舍五入）。

    仅用于后续 GPU 验证/参数评估；本模块与 one_stop 当前**不会**改变生成长度。
    """
    try:
        fps_f = float(fps)
        overlap_f = float(overlap_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"fps/overlap_ms 必须是数字：fps={fps!r}, overlap_ms={overlap_ms!r}") from exc
    if fps_f <= 0:
        raise ValueError(f"fps 必须 > 0（实际 {fps}）")
    if overlap_f < 0:
        raise ValueError(f"overlap_ms 不能为负（实际 {overlap_ms}）")
    return int(round(fps_f * overlap_f / 1000.0))
