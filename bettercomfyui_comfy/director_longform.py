"""剧核 · 单节点长片模式（V4/Q_OUT）：自动分段 → 多批生成 → 断点续跑 → 拼接。

用户需求（愿景 V4）：「一次交付约一小时整片」。单次导演台执行受显存/时间限制，
本模块把「约一小时」拆成**多批**：每批 N 段独立执行（显存安全），段 mp4 立即落盘，
每批结束写**检查点**；中断后用同一参数重跑可从最后一个完成的批次续跑（已完成批次不重跑）。

诚实边界（不假成功）：
- 段间 22 帧 motion-context pin 只在**同一批**内是 vendor 原生能力；跨批时 vendor 单次
  execute 拿不到上一批的 AV latent，因此跨批衔接用「上一批最后一帧作为下一批参考图」
  近似（报告中明确标注 `cross_batch_mode="last_frame_ref"`，不冒充原生 pin）。
- 检查点只复用「参数指纹 + 段文件都还在」的已完成批次；指纹变了自动作废并重跑。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

CHECKPOINT_FILENAME = "onestop_batches.json"


def plan_batches(segment_count: int, batch_size: int) -> list[tuple[int, int]]:
    """把 seg_count 段切成 [(start, end)) 批；batch_size<=0 / 覆盖全部 → 单批。"""
    total = max(1, int(segment_count))
    size = max(1, int(batch_size))
    if size >= total:
        return [(0, total)]
    return [(start, min(start + size, total)) for start in range(0, total, size)]


def plan_segment_count(
    *,
    requested_segments: int,
    target_minutes: float,
    segment_seconds: float,
    max_segments: int = 360,
) -> dict:
    """段数规划：显式段数 vs 「目标总时长_分钟」。返回规划明细（审计可见）。"""
    seg_s = max(0.1, float(segment_seconds))
    requested = max(1, int(requested_segments))
    target = float(target_minutes or 0.0)
    auto = 0
    if target > 0:
        auto = max(1, int(round(target * 60.0 / seg_s)))
    chosen = max(requested, auto)
    capped = min(chosen, max(0, int(max_segments)))
    return {
        "requested_segments": requested,
        "target_minutes": target,
        "auto_segments_from_target": auto,
        "chosen_segments": int(capped),
        "capped_by_max_segments": bool(chosen > capped),
        "max_segments": int(max_segments),
        "segment_seconds": seg_s,
        "planned_minutes": round(capped * seg_s / 60.0, 2),
    }


def parameter_fingerprint(payload: dict) -> str:
    """批次检查点指纹：参数一变化就作废旧批次（绝不复用旧内容的成片）。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def checkpoint_path(project_dir: Path) -> Path:
    return Path(project_dir) / "_work" / CHECKPOINT_FILENAME


def load_checkpoint(project_dir: Path, fingerprint: str) -> dict:
    """读取检查点；指纹不匹配/文件损坏 → 返回空状态（并带出原因供审计）。"""
    path = checkpoint_path(project_dir)
    if not path.is_file():
        return {"fingerprint": fingerprint, "batches": {}, "note": "无检查点（首次运行）"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 损坏的检查点不能复用，但要说清楚
        return {"fingerprint": fingerprint, "batches": {},
                "note": f"检查点损坏已忽略：{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return {"fingerprint": fingerprint, "batches": {},
                "note": "参数指纹变化 → 旧检查点作废，全部批次重跑"}
    data.setdefault("batches", {})
    data["note"] = "检查点命中"
    return data


def save_checkpoint(project_dir: Path, fingerprint: str, state: dict) -> Path:
    """原子写检查点（先写临时文件再替换，避免中断留下半个 JSON）。"""
    path = checkpoint_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["fingerprint"] = fingerprint
    payload["updated_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def reuse_batch_files(entry: dict) -> list[str] | None:
    """已完成批次条目 → 段文件列表；任一文件缺失则不复用（返回 None）。"""
    files = [str(x) for x in (entry or {}).get("seg_files") or []]
    if not files:
        return None
    if not all(Path(f).is_file() for f in files):
        return None
    return files


def extract_last_frame(video_path: Path | str, out_png: Path | str) -> Path:
    """取视频最后一帧（跨批衔接参考图）；失败要能定位，不静默。"""
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    src = Path(video_path)
    if not src.is_file():
        raise FileNotFoundError(f"无法取尾帧：视频不存在 {src}")
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    # -sseof 大负值 + 只出一帧：拿到接近文件尾的最后一帧（短段更稳）。
    cmd = [_ffmpeg_exe(), "-y", "-v", "error", "-sseof", "-1.0", "-i", str(src),
           "-frames:v", "1", "-q:v", "2", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"取尾帧失败（{src}）：{(proc.stderr or '')[-400:]}")
    return out


def build_segment_manifest(
    *,
    project_id: str,
    segment_specs: list[dict],
    seg_files: list[str],
    segment_reviews: list[dict] | None = None,
    cnt2: list[int] | None = None,
    cnt3: list[int] | None = None,
    fallback_details: list[dict] | None = None,
    chapter: int = 1,
) -> list[dict]:
    """Q_OUT 分段清单：segment_id/版本/素材引用/段文件/审核分数/CNT2/CNT3/兜底。

    段 ID 用库层 `store/ids.py::segment_id({project_id}_CHn_SEGm)`；项目 id 非法时
    退化为本地可读 id（并在 note 标注），绝不因为 ID 生成失败丢掉清单。
    """
    reviews = segment_reviews or []
    c2 = cnt2 or []
    c3 = cnt3 or []
    fallbacks = fallback_details or []
    out: list[dict] = []
    for idx, spec in enumerate(segment_specs):
        seg_id = f"CH{int(chapter)}_SEG{idx + 1}"
        id_note = ""
        try:
            from bettercomfyui.store.ids import segment_id as _segment_id

            seg_id = _segment_id(project_id, int(chapter), idx + 1)
        except Exception as exc:  # noqa: BLE001 - ID 失败不丢清单，但标注
            id_note = f"{type(exc).__name__}: {exc}"
        review = reviews[idx] if idx < len(reviews) else {}
        fallback = next(
            (f for f in fallbacks
             if int(f.get("segment_index", -1)) in (idx, idx + 1)), None
        )
        out.append({
            "segment_id": seg_id,
            "segment_index": idx + 1,
            "segment_version": str(review.get("version") or f"v{int(review.get('attempt') or 1)}"),
            "start": spec.get("start"),
            "frames": spec.get("frames"),
            "duration_sec": spec.get("duration_sec"),
            "task_type": spec.get("task_type"),
            "prompt": str(spec.get("prompt") or "")[:400],
            "asset_refs": list(spec.get("asset_refs") or []),
            "video_path": seg_files[idx] if idx < len(seg_files) else None,
            "final_score": review.get("score"),
            "review_scores": review.get("scores"),
            "review_passed": review.get("passed"),
            "cnt2": int(c2[idx]) if idx < len(c2) else 0,
            "cnt3": int(c3[idx]) if idx < len(c3) else 0,
            "cascade_status": review.get("cascade_status"),
            "fallback": fallback,
            "degrade_flags": list(review.get("degrade_flags") or []),
            "source": "NORMAL",
            "id_note": id_note,
        })
    return out


def manifest_to_metadata_items(manifest: list[dict]) -> list[Any]:
    """清单 → `MovieMetadata.segments` 的 SegmentManifestItem（OUT3）。"""
    from bettercomfyui.models.entities import SegmentManifestItem

    items = []
    for row in manifest:
        items.append(
            SegmentManifestItem(
                segment_id=str(row.get("segment_id") or ""),
                segment_version=str(row.get("segment_version") or "v1"),
                asset_refs=list(row.get("asset_refs") or []),
                video_path=row.get("video_path"),
                final_score=row.get("final_score"),
                review_source="local_heuristic",
                cnt2=int(row.get("cnt2") or 0),
                cnt3=int(row.get("cnt3") or 0),
                cascade_status=row.get("cascade_status"),
            )
        )
    return items


def batch_report_rows(plan: list[tuple[int, int]], state: dict) -> list[dict]:
    """批次执行明细（交给报告/审计）。"""
    rows = []
    for batch_index, (start, end) in enumerate(plan):
        entry = (state.get("batches") or {}).get(str(batch_index)) or {}
        rows.append({
            "batch_index": batch_index,
            "segments": [start + 1, end],
            "status": entry.get("status"),
            "reused": bool(entry.get("reused")),
            "seg_files": list(entry.get("seg_files") or []),
            "elapsed_s": entry.get("elapsed_s"),
            "report_first_line": entry.get("report_first_line"),
        })
    return rows
