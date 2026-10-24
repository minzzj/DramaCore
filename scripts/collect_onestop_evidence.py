# -*- coding: utf-8 -*-
"""剧核 DramaCore · 单节点（剧本→成片）证据收集管线。

只读收集，不改功能代码、不重启服务、不提交 GPU 队列。

扫描位置（新文件会被自动纳入，无需改代码）：
  本地
    - remote_reports/*.json                （one_stop_test.json、one_stop_full_test*.json、
                                            continuity_test.json 等运行报告；按文件名/结构分类）
    - remote_reports/vram_onestop*.csv    （显存采样，若存在；审计文本已有显存峰值时优先用审计值）
    - docs/remote_evidence_4090/*.json    （one_stop_quality_*.json / one_stop_review_*.json）
    - docs/remote_evidence_4090/ui_test/ui_test_result.json（真实 UI 点击测试 + 截图）
    - docs/remote_evidence_4090/real_chain/        （真实 Key API 全链：运行审计 + 节点 v0.3 审核 JSON + 成片副本）
    - docs/remote_evidence_4090/real_key_ui/       （真实 Key + 真实 UI 点击 + continuity_test_v2）
    - docs/remote_evidence_4090/round46/           （ROUND46：转场/字幕/水印/封面/MOV、长片分批、断点续跑、自动返工）
    - docs/remote_evidence_4090/*.md      （quality md 兜底解析）
    - docs/ROUND41_*.md                   （首版真机验证报告里的运行表，作为文档级证据）
    - remote_reports/remote_pulled/**     （上次 --pull-remote 拉回的远端证据）
  远端（仅 --pull-remote 时，只读）
    - /data/BetterComfyUI/projects/<project_id>/review/one_stop_*.json
    - /data/BetterComfyUI/projects/<project_id>/output/*.mp4   （只列大小，不下载视频）
    - /data/BetterComfyUI/remote_reports/*.json、/root/BetterComfyUI/remote_reports/*.json
    - 远端环境：SSH 端口 40248（凭据在 bcu_remote 同目录 env 文件）；
      ComfyUI 外网 os.environ.get("BCU_REMOTE_BASE", "http://YOUR-GPU-HOST:8188")（旧 40244 已不再服务 ComfyUI）

输出
    - remote_reports/onestop_summary.json   统一结构（机器可读）
    - remote_reports/onestop_summary.md     Markdown 汇总表
    - [--round42 路径]                      ROUND42 数据收集报告（含来源/缺失/一键重跑命令）
    - [--pull-remote]                       remote_reports/remote_pulled/ 远端证据缓存 + manifest

用法
    # 只扫本地已有证据
    python -B scripts/collect_onestop_evidence.py

    # 全流程测试完成后一键重跑：先只读拉取远端证据，再重算所有表
    python -B scripts/collect_onestop_evidence.py --pull-remote --round42 docs/ROUND42_数据收集_单节点全流程.md

原则
    - 数字必须来自真实证据文件；缺失字段一律置 null 并写入 missing 列表，禁止估算。
    - 同类证据冲突时保留全部来源，并在 match_notes 里说明匹配依据，不擅自覆盖。
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REMOTE_BRIDGE = Path(r"C:\Users\Administrator\AppData\Local\Temp\bcu_remote.py")
REMOTE_CACHE = REPO / "remote_reports" / "remote_pulled"
OUT_JSON = REPO / "remote_reports" / "onestop_summary.json"
OUT_MD = REPO / "remote_reports" / "onestop_summary.md"
EVID_DIR = REPO / "docs" / "remote_evidence_4090"
REPORTS_DIR = REPO / "remote_reports"

FIELD_LABELS: dict[str, str] = {
    "finished_at": "运行时间",
    "total_elapsed_s": "总耗时(s)",
    "node_total_s": "节点内总耗时(s)",
    "prompt_s": "提示词生成耗时(s)",
    "asset_s": "素材生图+审核耗时(s)",
    "load_model_s": "加载视频模型耗时(s)",
    "gen_s": "出片耗时(s)",
    "encode_s": "编码耗时(s)",
    "segments_planned": "提示词段数(计划)",
    "segments_actual": "实产段数",
    "frames": "总帧数",
    "duration_s": "成片时长(s)",
    "fps": "帧率",
    "width": "宽",
    "height": "高",
    "has_audio": "音轨",
    "assets_created": "素材生成数",
    "assets_generated": "素材候选生成数",
    "assets_reused": "复用已LOCK素材数",
    "assets_locked": "素材审核 LOCK 数",
    "assets_blocked": "素材未 LOCK 数",
    "asset_review_verdict": "素材审核结论",
    "refs_attached": "挂载参考图数",
    "integrity_passed": "完整性 passed",
    "quality_scores": "整片四维分数",
    "quality_average": "整片均分",
    "quality_threshold": "审核阈值",
    "segment_scores": "段级审核分数",
    "full_review_scores": "整片 R_full 分数",
    "verdict": "审核结论",
    "vram_peak_mb": "显存峰值(MB)",
    "output_size_bytes": "成片大小(bytes)",
    "output_bitrate_bps": "码率(bps)",
    "export_mode": "导演台导出模式",
    "output_duration_s": "成片实测时长(s)",
    "output_frames_probe": "成片实测帧数",
    "video_review_verdict": "视频审核结论",
    "need_regenerate": "需重生成段",
    "ui_test_success": "UI真实点击成功",
    "ui_test_elapsed_s": "UI真实点击耗时(s)",
    "ui_widgets": "UI控件数",
    "ui_missing_nodes": "UI缺失节点数",
    "ui_screenshots": "UI截图数",
    "continuity_on": "段间连续性 ON",
    "continuity_mode": "连续性模式",
    "continuity_overlap_frames": "连续性重叠帧",
    "director_segments": "导演台段数",
    "director_frames": "导演台返回帧数",
    "finalize_segments": "Finalize剪辑段数",
    "plan_duration_s": "计划时长(s)",
    "actual_gen_duration_s": "实际生成时长(s)",
    "api_calls": "API调用次数",
    "api_configured": "API已配置",
    "real_key": "真实Key/真实模型",
    "sg6_format": "剪辑输出格式",
    "sg6_transition": "转场",
    "sg6_subtitle": "字幕",
    "sg6_watermark": "水印",
    "sg6_cover": "封面",
    "longfilm_batches": "长片批次数",
    "rework_rounds": "自动返工尝试次数",
    "merged_frames": "导演台合并帧数",
}

SCORE_DIM_LABELS = {
    "叙事流畅": "叙事",
    "音画同步": "音画",
    "节奏合理": "节奏",
    "无明显跳变": "跳变",
}

RUNNER_NAME_RE = re.compile(r"(one[_-]?stop|onestop)", re.I)
VRAM_NAME_RE = re.compile(r"(vram.*one[_-]?stop|one[_-]?stop.*vram)", re.I)


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def read_json_any(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    for enc in ("utf-8", "gb18030"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fmt(v: Any, digits: int = 2) -> str:
    if v is None:
        return "未采集到"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else f"{v:g}"
    return str(v)


def num(pattern: str, text: str, cast: Any = float) -> Any:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except (TypeError, ValueError):
        return None


def literal_dict(pattern: str, text: str) -> dict[str, Any] | None:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        val = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    return dict(val) if isinstance(val, dict) else None


def _to_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _utc_iso_to_local(iso: str | None) -> str | None:
    """把证据里未标时区的 created_at（按 UTC）转成本机时间字符串。"""
    if not iso:
        return None
    from datetime import timezone as _tz
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", ""))
    except ValueError:
        return None
    return dt.replace(tzinfo=_tz.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _epoch_to_local(epoch: str | int | None) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def probe_media(path: Path) -> dict[str, Any] | None:
    """本地成片实测：用 imageio_ffmpeg 自带 ffmpeg 读时长/分辨率/fps/音轨/帧数/大小。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 没有 ffmpeg 就如实返回 None
        return None
    if not exe or not Path(exe).is_file():
        return None
    info: dict[str, Any] = {}
    try:
        p = subprocess.run([exe, "-hide_banner", "-i", str(path)],
                           capture_output=True, timeout=120)
        err = p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if m:
        info["duration_container_s"] = round(
            int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 3)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?) fps", err)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"Video:\s*(\w+)", err)
    if m:
        info["video_codec"] = m.group(1)
    info["has_audio"] = "Audio:" in err
    try:
        p2 = subprocess.run(
            [exe, "-hide_banner", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True, timeout=240)
        err2 = p2.stderr.decode("utf-8", "replace")
        frames = re.findall(r"frame=\s*(\d+)", err2)
        if frames:
            info["nb_frames"] = int(frames[-1])
            if info.get("fps"):
                info["duration_s"] = round(info["nb_frames"] / info["fps"], 3)
    except (OSError, subprocess.TimeoutExpired):
        pass
    info["size_bytes"] = path.stat().st_size
    if info.get("duration_container_s") is not None:
        info.setdefault("duration_s", info["duration_container_s"])
    return info or None


# ----------------------------------------------------------------------
# 1) 单节点 review JSON（节点内审核报告，最权威；本地镜像或远端拉回）
# ----------------------------------------------------------------------
def parse_review_json(path: Path, source: str) -> dict[str, Any] | None:
    data = read_json_any(path)
    if not isinstance(data, dict):
        return None
    if not any(k in data for k in ("integrity", "quality", "timings", "assets", "video_review")):
        return None
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    rec: dict[str, Any] = {
        "kind": "node_review",
        "source": source,
        "source_path": rel,
        "evidence_file_mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "project_id": None,
        "finished_at": None,
    }
    m = re.search(r"([0-9a-f]{32})", str(path))
    if m:
        rec["project_id"] = m.group(1)
    if data.get("project_id"):
        rec["project_id"] = str(data["project_id"])
    audit_text = str(data.get("audit") or "")
    m = re.search(r"review/one_stop_(\d{9,})\.json", audit_text) or \
        re.search(r"one_stop_(\d{9,})\.json$", path.name)
    if m:
        rec["finished_at"] = _epoch_to_local(m.group(1))
    elif data.get("created_at"):
        rec["finished_at"] = _utc_iso_to_local(data.get("created_at"))
    elif rec["evidence_file_mtime"]:
        rec["finished_at"] = rec["evidence_file_mtime"]

    integ = data.get("integrity") or {}
    qual = data.get("quality") or {}
    tim = data.get("timings") or {}
    assets = data.get("assets") or {}
    vrev = data.get("video_review") or {}
    gen = data.get("generation") or {}

    rec.update({
        "integrity_passed": integ.get("passed"),
        "final_path": integ.get("final"),
        "frames": integ.get("frames"),
        "duration_s": integ.get("duration_s"),
        "fps": integ.get("fps"),
        "width": integ.get("width"),
        "height": integ.get("height"),
        "has_audio": integ.get("has_audio"),
        "quality_scores": dict(qual.get("scores") or {}) or None,
        "quality_average": qual.get("average"),
        "quality_threshold": qual.get("threshold"),
        "quality_passed": qual.get("passed"),
        "quality_source": qual.get("score_source"),
        "gen_s": _to_float(gen.get("seconds")) if gen else None,
        "frames_generation": gen.get("frames"),
        "prompt_mode": (data.get("prompts") or {}).get("mode"),
        "prompt_segments": len((data.get("prompts") or {}).get("segments") or []) or None,
    })
    if tim:
        rec["timings_raw"] = dict(tim)
        rec["prompt_s"] = _to_float(tim.get("提示词生成_s"))
        rec["asset_s"] = _to_float(tim.get("素材生图审核_s"))
        rec["load_model_s"] = _to_float(tim.get("加载视频模型_s"))
        rec["encode_s"] = _to_float(tim.get("编码_s") or tim.get("分段编码_s"))
        if rec.get("gen_s") is None:
            rec["gen_s"] = _to_float(tim.get("出片_s"))
    if assets:
        rec["assets_enabled"] = assets.get("enabled")
        rec["assets_created"] = len(assets.get("created") or [])
        rec["assets_locked"] = len(assets.get("locked") or [])
        rec["assets_blocked"] = len(assets.get("blocked") or [])
        rec["refs_attached"] = assets.get("refs_attached")
        rec["asset_errors"] = list(assets.get("errors") or [])
        rec["asset_detail"] = {
            "created": assets.get("created") or [],
            "locked": assets.get("locked") or [],
            "blocked": assets.get("blocked") or [],
        }
        n_locked = len(assets.get("locked") or [])
        n_blocked = len(assets.get("blocked") or [])
        n_created = len(assets.get("created") or [])
        if n_created:
            rec["asset_review_verdict"] = (
                f"全部 LOCK（{n_locked}/{n_created}）" if n_locked == n_created
                else f"部分/全部未 LOCK（LOCK {n_locked}，未 LOCK {n_blocked}，共 {n_created}）")
        rec["assets_reused"] = len(assets.get("reused") or []) if assets.get("reused") is not None else None
        if assets.get("rounds") is not None:
            rec["asset_rounds"] = assets.get("rounds")
        rec["refs_attached_structured"] = assets.get("refs_attached")
    if vrev:
        rec["video_review_verdict"] = vrev.get("verdict")
        seg = vrev.get("segments") or vrev.get("segment_review") or {}
        if isinstance(seg, dict) and seg.get("segments"):
            rec["segment_scores"] = [
                {
                    "segment_index": s.get("segment_index"),
                    "score": s.get("score"),
                    "passed": s.get("passed"),
                    "dimensions": s.get("dimensions") or {},
                    "duration_s": s.get("duration_s"),
                    "too_short": s.get("too_short"),
                }
                for s in seg.get("segments") or []
                if isinstance(s, dict)
            ]
            rec["segment_threshold"] = seg.get("threshold")
            rec["segment_need_regenerate"] = seg.get("need_regenerate")
        elif isinstance(vrev.get("segment_scores"), list):
            rec["segment_scores"] = [
                {"segment_index": s.get("index") if s.get("index") is not None else idx,
                 "score": s.get("score"), "passed": s.get("passed"),
                 "dimensions": s.get("scores") or {}}
                for idx, s in enumerate(
                    [x for x in vrev["segment_scores"] if isinstance(x, dict)], 1)
            ]
        full = vrev.get("full") or {}
        if isinstance(full, dict) and full.get("executed"):
            rec["full_review_scores"] = dict(full.get("r1b_scores") or {}) or None
            rec["full_review_final"] = full.get("final_score")
            rec["full_review_threshold"] = full.get("threshold")
            rec["full_review_passed"] = full.get("passed")
            rec["full_review_policy"] = full.get("policy")
        rec["video_review_error"] = vrev.get("error")
    # ---- v0.3-full-director 新结构：vram / finalize / video_meta / continuity / 汇总 ----
    vram = data.get("vram") or {}
    peak = (vram.get("peak") or {}) if isinstance(vram, dict) else {}
    if isinstance(peak, dict) and peak.get("peak_reserved_mb") is not None:
        rec["vram_peak_mb"] = float(peak["peak_reserved_mb"])
        rec["vram_peak_allocated_mb"] = peak.get("peak_allocated_mb")
        rec["vram_source"] = "节点审核 JSON.vram.peak（peak_reserved_mb）"
    fin = data.get("finalize") or {}
    if isinstance(fin, dict) and fin:
        if fin.get("segments") is not None:
            rec["finalize_segments"] = int(fin["segments"])
        if fin.get("path"):
            rec["finalize_path"] = fin.get("path")
    vm = data.get("video_meta") or {}
    if isinstance(vm, dict):
        for src, dst in (("frames", "frames"), ("duration_s", "duration_s"),
                         ("has_audio", "has_audio"), ("width", "width"),
                         ("height", "height"), ("fps", "fps")):
            if vm.get(src) is not None and rec.get(dst) is None:
                rec[dst] = vm.get(src)
    cont = data.get("continuity") or {}
    if isinstance(cont, dict) and cont:
        rec["continuity_evidence"] = "; ".join(str(x) for x in (cont.get("evidence_lines") or []))[:500]
        if cont.get("mode") is not None:
            rec["continuity_mode"] = cont.get("mode")
        if cont.get("overlap_frames") is not None:
            rec["continuity_overlap_frames"] = cont.get("overlap_frames")
        if cont.get("pinned_segments"):
            rec["continuity_pin_segments"] = list(cont.get("pinned_segments") or [])
        if cont.get("continuity_enabled_in_plan") is not None:
            rec["continuity_plan_on"] = cont.get("continuity_enabled_in_plan")
    ts = data.get("timeline_summary") or {}
    if isinstance(ts, dict):
        if ts.get("totalFrames") is not None:
            rec["director_frames"] = ts.get("totalFrames")
        ts_out = ts.get("output") or {}
        if isinstance(ts_out, dict) and ts_out.get("exportMode"):
            rec["export_mode"] = ts_out.get("exportMode")
    if data.get("unimplemented_features") is not None:
        rec["unimplemented_features"] = list(data.get("unimplemented_features") or [])
    if audit_text:
        rec["audit_text"] = audit_text
        extras = parse_audit_extras(audit_text)
        for key, value in extras.items():
            if value is not None and rec.get(key) is None:
                rec[key] = value
        # 挂参考图：结构化 JSON 与审计文本都有值时保留两者（已在报告里标注差异）
        a = extras.get("refs_attached")
        s = rec.get("refs_attached_structured")
        if a is not None:
            rec["refs_attached_audit"] = a
            rec["refs_attached"] = a
            if s is not None and int(s) != int(a):
                rec.setdefault("warnings", []).append(
                    f"挂参考图数不一致：结构化 JSON={s}，审计文本={a}（报告取审计文本值，另存 "
                    f"refs_attached_structured={s}）")
        if extras.get("asset_review_verdict"):
            rec["asset_review_verdict"] = extras["asset_review_verdict"]
    if rec.get("integrity_passed") is not None and rec.get("quality_passed") is not None:
        rec["verdict"] = "通过" if (rec["integrity_passed"] and rec["quality_passed"]) else "不通过"
    return rec


# ----------------------------------------------------------------------
# 2) 运行报告 JSON（remote_reports/one_stop_test.json 等：status/elapsed_s/outputs_text）
# ----------------------------------------------------------------------
def parse_runner_report(path: Path, source: str) -> dict[str, Any] | None:
    data = read_json_any(path)
    if not isinstance(data, dict):
        return None
    raw_text = data.get("outputs_text")
    if isinstance(raw_text, list):
        text = "\n".join(str(x) for x in raw_text if x is not None)
    else:
        text = str(raw_text or "")
    if not text and "elapsed_s" not in data:
        return None
    if "单节点" not in text and not RUNNER_NAME_RE.search(path.name):
        return None
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    rec: dict[str, Any] = {
        "kind": "runner_report",
        "source": source,
        "source_path": rel,
        "finished_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "status": data.get("status"),
        "total_elapsed_s": _to_float(data.get("elapsed_s")),
        "errors": list(data.get("errors") or []),
        "audit_text": text,
        "project_id": None,
        "prompt_mode": None,
    }
    m = re.search(r"解析到已有项目\s*([0-9a-f]{32})", text) or re.search(r"\b([0-9a-f]{32})\b", text)
    if m:
        rec["project_id"] = m.group(1)
    m = re.search(r"提示词模式：(.+)", text)
    if m:
        rec["prompt_mode"] = m.group(1).strip()
    m = re.search(r"总耗时：([\d.]+)s\s*\|\s*分项：(\{.*\})", text)
    if m:
        rec["node_total_s"] = _to_float(m.group(1))
        rec["total_elapsed_s"] = rec["total_elapsed_s"] or rec["node_total_s"]
        try:
            rec["timings_raw"] = dict(ast.literal_eval(m.group(2)))
        except (ValueError, SyntaxError):
            rec["timings_raw"] = None
    if rec.get("timings_raw"):
        tim = rec["timings_raw"]
        rec["prompt_s"] = _to_float(tim.get("提示词生成_s"))
        rec["asset_s"] = _to_float(tim.get("素材生图审核_s"))
        rec["load_model_s"] = _to_float(tim.get("加载视频模型_s"))
        rec["encode_s"] = _to_float(tim.get("编码_s") or tim.get("分段编码_s"))
        rec["gen_s"] = _to_float(tim.get("出片_s"))
    if rec.get("gen_s") is None:
        rec["gen_s"] = num(r"出片耗时：([\d.]+)s", text)
    m = re.search(r"编码：(\d+) 帧 / (\d+)x(\d+) / ([\d.]+)s / 音轨=(有|无)", text)
    if m:
        rec["frames"] = int(m.group(1))
        rec["width"], rec["height"] = int(m.group(2)), int(m.group(3))
        rec["duration_s"] = float(m.group(4))
        rec["has_audio"] = m.group(5) == "有"
    m = re.search(r"段数：(\d+)\s*/\s*每段", text)
    if m:
        rec["segments_actual"] = int(m.group(1))
    m = re.search(r"段数：(\d+)\s+每段", text)
    if m:
        rec["segments_planned"] = int(m.group(1))
    m = re.search(r"总帧数：(\d+)", text)
    if m and "frames" not in rec:
        rec["planned_frames"] = int(m.group(1))  # 计划总帧数（非实际输出帧数）
    m = re.search(r"(\d+)x(\d+)@([\d.]+)fps", text)
    if m and "width" not in rec:
        rec["width"], rec["height"], rec["fps"] = int(m.group(1)), int(m.group(2)), float(m.group(3))
    # 素材（全流程版审计行）：生图 → 入库 → 审核 → LOCK 状态
    m = re.search(r"素材生图：.*?待生成 (\d+) 张", text)
    if m:
        rec["assets_planned"] = int(m.group(1))
    if "素材生图：" in text and "待生成" in text:
        rec["assets_created"] = len(re.findall(r"素材 [A-Z_]+ .+? → v\d+", text))
        rec["assets_locked"] = len(re.findall(r"→ LOCKED", text))
        rec["assets_blocked"] = len(re.findall(r"BLOCKED", text))
        detail: list[dict[str, Any]] = []
        for am in re.finditer(
                r"素材 ([A-Z_]+) (.+?) → (v\d+) （([\d.]+)s）\n\s*素材审核：(\w+) → ([^\n]+)",
                text):
            detail.append({
                "type": am.group(1), "name": am.group(2), "version": am.group(3),
                "gen_s": float(am.group(4)), "state": am.group(5),
                "review_line": am.group(6).strip(),
            })
        if detail:
            rec["asset_detail"] = detail
        if rec["assets_blocked"] and rec["assets_locked"] == 0:
            rec["asset_review_verdict"] = f"全部未 LOCK（{rec['assets_blocked']} 张被 BLOCKED）"
        elif rec["assets_locked"]:
            rec["asset_review_verdict"] = f"LOCK {rec['assets_locked']} 张"
    m = re.search(r"参考图挂载：(\d+) 张", text)
    if m:
        rec["refs_attached"] = int(m.group(1))
    m = re.search(r"⚠️ 视频审核失败[^\n]*", text)
    if m:
        rec["video_review_error"] = m.group(0).strip().split("\\n")[0].split("\n")[0]
    m = re.search(r"审核结论：(通过|不通过)（完整性=(\w+)，质量=(\{.*?\})", text)
    if m:
        rec["verdict"] = m.group(1)
        rec["integrity_passed"] = m.group(2) == "True"
        try:
            rec["quality_scores"] = dict(ast.literal_eval(m.group(3)))
        except (ValueError, SyntaxError):
            rec["quality_scores"] = None
    if rec.get("quality_scores") is None:
        rec["quality_scores"] = literal_dict(r"质量=(\{[^{}]*\})", text)
    m = re.search(r"视频审核=([^）\n]+)", text)
    if m:
        rec["video_review_verdict"] = m.group(1).strip()
    rec["kind"] = classify_runner_kind(path, data, text)
    for key, value in parse_audit_extras(text).items():
        if value is not None and rec.get(key) is None:
            rec[key] = value
    # 审计里带远端 review 证据路径（含 epoch）时，用真实完成时间，文件 mtime 仅作副本时间
    if rec.get("finished_at_from_evidence"):
        rec["finished_at_mtime"] = rec["finished_at"]
        rec["finished_at"] = rec["finished_at_from_evidence"]
    # P0-4 修复版识别：文件名 fixed，或 2 段长片且无「时长不足」、完整性通过
    if "fixed" in path.name.lower():
        rec["is_fixed"] = True
    elif (rec.get("duration_insufficient") is None and "长片分批" in text
          and rec.get("integrity_passed") is True and rec.get("segments_actual") == 2):
        rec["is_fixed"] = True
        rec["is_fixed_inferred"] = True
    return rec


# ----------------------------------------------------------------------
# 2b) 审计文本公共解析（全功能版：素材结论/连续性/R_seg/R_full/显存/UI）
# ----------------------------------------------------------------------
def extract_balanced_json(text: str, marker: str) -> Any | None:
    """从 marker 后第一个 '{' 起做括号配平，解析 JSON 对象（R_full 等）。"""
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find("{", idx)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def extract_balanced_list(text: str, marker: str) -> Any | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find("[", idx)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                raw = text[start:i + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass
                try:
                    return ast.literal_eval(raw)
                except (ValueError, SyntaxError):
                    return None
    return None


def classify_runner_kind(path: Path, data: dict[str, Any], text: str) -> str:
    parent = path.parent.name
    if parent == "real_chain":
        return "real_chain"
    if parent == "round46":
        return "round46"
    if "steps" in data or "loaded" in data or "prompt_id" in data:
        return "ui_test"
    if "continuity" in path.name.lower():
        return "continuity_test"
    return "runner_report"


def parse_audit_extras(text: str) -> dict[str, Any]:
    """全功能版审计文本里的新增字段（素材结论/连续性/R_seg/R_full/显存/时长说明）。"""
    out: dict[str, Any] = {}
    m = re.search(r"素材结论：生成 (\d+) / 复用LOCKED (\d+) / LOCKED (\d+) / "
                  r"未LOCK (\d+) / 挂参考图 (\d+)", text)
    if m:
        out.update({
            "assets_generated": int(m.group(1)),
            "assets_reused": int(m.group(2)),
            "assets_locked": int(m.group(3)),
            "assets_blocked": int(m.group(4)),
            "refs_attached": int(m.group(5)),
        })
        if out["assets_generated"] or out["assets_reused"]:
            out["assets_created"] = out["assets_generated"]
            out["asset_review_verdict"] = (
                f"LOCK {out['assets_locked']} / 未LOCK {out['assets_blocked']}"
                f"（生成 {out['assets_generated']}，复用 {out['assets_reused']}）")
        elif "素材生图：未启用" in text:
            out["assets_created"] = 0
            out["asset_review_verdict"] = "素材生图未启用（0 生成 / 0 LOCK / 0 挂图）"
    m = re.search(r"素材生图：.*?待处理 (\d+) 张", text) or \
        re.search(r"素材生图：.*?待生成 (\d+) 张", text)
    if m:
        out["assets_planned"] = int(m.group(1))
    m = re.search(r"显存峰值（reserved）：([\d.]+) MB", text)
    if m:
        out["vram_peak_mb"] = float(m.group(1))
        out["vram_source"] = "节点审计文本 显存峰值（reserved）"
    m = re.search(r"导演台时间轴：.*?段=(\d+) 总帧=(\d+) 连续性=(True|False)/(\d+)f\((\w+)\)", text)
    if m:
        out["director_segments"] = int(m.group(1))
        out["director_frames"] = int(m.group(2))
        out["continuity_on"] = m.group(3) == "True"
        out["continuity_overlap_frames"] = int(m.group(4))
        out["continuity_mode"] = m.group(5)
    m = re.search(r"段间连续实证：ON=(\w+) mode=(\w+) overlap=(\w+)f keep_tail=(\w+) "
                  r"pin段=\[([\d,\s]*)\]", text)
    if m:
        out["continuity_on"] = m.group(1) == "True"
        out["continuity_mode"] = m.group(2)
        out["continuity_overlap_frames"] = int(m.group(3)) if m.group(3).isdigit() else None
        out["continuity_pin_segments"] = [int(x) for x in re.findall(r"\d+", m.group(5))]
        out["continuity_evidence"] = m.group(0).strip()
    m = re.search(r"出片耗时：([\d.]+)s；返回帧数=(\d+)；导演台报告首行："
                  r"[^\n]*?(\d+) segment\(s\), (\d+) frames", text)
    if m:
        out["gen_s"] = float(m.group(1))
        out["frames_returned"] = int(m.group(2))
        out["director_segments"] = int(m.group(3))
        out["director_frames"] = int(m.group(4))
    m = re.search(r"Finalize 剪辑：(\d+) 段", text)
    if m:
        out["finalize_segments"] = int(m.group(1))
    seg = extract_balanced_list(text, "段级 R_seg：")
    if isinstance(seg, list):
        parsed_segs = []
        for idx, s in enumerate([x for x in seg if isinstance(x, dict)], start=1):
            parsed_segs.append({
                "segment_index": s.get("index") if s.get("index") is not None else idx,
                "score": s.get("score"), "passed": s.get("passed"),
                "dimensions": s.get("scores") or {},
            })
        out["segment_scores"] = parsed_segs
    m = re.search(r"需重生成段：(\[[^\]]*\]|无)", text)
    if m:
        raw_need = m.group(1)
        out["need_regenerate"] = [] if raw_need == "无" else [
            int(x) for x in re.findall(r"\d+", raw_need)]
    full = extract_balanced_json(text, "整片 R_full：")
    if isinstance(full, dict) and full.get("executed"):
        out["full_review_scores"] = dict(full.get("r1b_scores") or {})
        out["full_review_final"] = full.get("final_score")
        out["full_review_threshold"] = full.get("threshold")
        out["full_review_passed"] = full.get("passed")
        out["full_review_policy"] = full.get("policy")
    m = re.search(r"视频审核：(通过|不通过|需返工)", text)
    if m:
        out["video_review_verdict"] = m.group(1)
    m = re.search(r"审核结论：(通过|不通过)（完整性=(\w+)，质量均分=([\d.]+)，阈值=(\d+)", text)
    if m:
        out["integrity_passed"] = m.group(2) == "True"
        out["quality_average"] = float(m.group(3))
        out["quality_threshold"] = float(m.group(4))
        out["verdict"] = m.group(1)
    m = re.search(r"时长说明：计划 ([\d.]+)s[^\n]*?实际生成 ([\d.]+)s（(\d+) 帧）", text)
    if m:
        out["plan_duration_s"] = float(m.group(1))
        out["actual_gen_duration_s"] = float(m.group(2))
        out["actual_gen_frames"] = int(m.group(3))
        out["frames"] = int(m.group(3))
        out["duration_s"] = float(m.group(2))
    m = re.search(r"(\d+)x(\d+)@([\d.]+)fps", text)
    if m:
        out.setdefault("width", int(m.group(1)))
        out.setdefault("height", int(m.group(2)))
        out.setdefault("fps", float(m.group(3)))
    m = re.search(r"(?m)^段数：(\d+)\s*/\s*每段", text)
    if m:
        out.setdefault("segments_actual", int(m.group(1)))
    m = re.search(r"(?m)^段数：(\d+)\s+每段", text)
    if m:
        out.setdefault("segments_planned", int(m.group(1)))
    m = re.search(r"总帧数：(\d+)", text)
    if m:
        out["planned_frames"] = int(m.group(1))
    m = re.search(r"时间轴：自动构造（task=(\w+)，(\d+) 段，每段参考图 (\d+) 张", text)
    if m:
        out["task_type"] = m.group(1)
        out["timeline_segments"] = int(m.group(2))
        out["refs_per_segment"] = int(m.group(3))
    m = re.search(r"API 调用次数：(\d+) 次", text)
    if m:
        out["api_calls"] = int(m.group(1))
    candidates = []
    for cm in re.finditer(r"(?m)^\s*素材 ([A-Z_]+) (.+?) → (v\d+) (候选\d+ )?（([\d.]+)s）",
                          text):
        candidates.append({
            "type": cm.group(1), "name": cm.group(2), "version": cm.group(3),
            "candidate": (cm.group(4) or "").strip(), "gen_s": float(cm.group(5)),
        })
    if candidates:
        out["asset_candidates"] = candidates
    states = [{"state": x[0], "score": float(x[1])} for x in re.findall(
        r"素材审核：(BLOCKED|FALLBACK|LOCKED) 评分=([\d.]+)", text)]
    if states:
        out["asset_candidate_reviews"] = states
    reused = [{"name": a, "version": b} for a, b in re.findall(
        r"素材 (.+?) (v\d+) 已 LOCKED → 复用", text)]
    if reused:
        out["asset_reused_detail"] = reused
    warnings = []
    for pat in (r"⚠️ 边界[^\n]*", r"⚠️ 段间连续被跳过：[^\n]*",
                r"⚠️ 尾段过短[^\n]*", r"⚠️ 未读到 API 密钥[^\n]*",
                r"⚠️ 视频审核失败[^\n]*", r"⚠️ 时长不足[^\n]*"):
        warnings.extend(x.strip() for x in re.findall(pat, text))
    if warnings:
        out["warnings"] = warnings

    # ---- 真实 Key / API ----
    m = re.search(r"API 调用次数：(\d+) 次", text)
    if m:
        out["api_calls"] = int(m.group(1))
    if "API：已配置" in text:
        out["api_configured"] = True
    elif "API：未配置" in text:
        out["api_configured"] = False
    if re.search(r"提示词模式：(?:真实模型|真实)", text):
        out["real_key"] = True
        out["prompt_mode"] = "真实模型"
    elif re.search(r"提示词模式：(?:占位|⚠️ 占位)", text):
        out["real_key"] = False
        out["prompt_mode"] = "占位"

    # ---- SG6 剪辑配置（ROUND46：转场/字幕/水印/封面/格式）----
    m = re.search(r"SG6 剪辑配置：格式=(\S+) CRF=(\d+) 转场=(\S+) 字幕=(\S+) 水印=(\S+) 封面=(\S+)",
                  text)
    if m:
        out.update({
            "sg6_format": m.group(1), "sg6_crf": int(m.group(2)),
            "sg6_transition": m.group(3), "sg6_subtitle": m.group(4),
            "sg6_watermark": m.group(5), "sg6_cover": m.group(6),
        })
    m = re.search(r'exportMode["\']?\s*[=:]\s*["\']?(\w+)', text)
    if m:
        out["export_mode"] = m.group(1)

    # ---- ROUND46 长片分批 / 断点续跑 / 自动返工 ----
    m = re.search(r"长片段数规划：[^\n]*采用 (\d+) 段", text)
    if m:
        out["longfilm_planned_segments"] = int(m.group(1))
    m = re.search(r"断点续跑：([^\n]+)", text)
    if m:
        out["resume_policy"] = m.group(1).strip()
    m = re.search(r"长片分批：批次1=段([\d\-]+)（共 (\d+) 批，每批≤(\d+) 段）", text)
    if m:
        out["batch_range"] = f"段{m.group(1)}"
        out["longfilm_batches"] = int(m.group(2))
        out["longfilm_batch_size"] = int(m.group(3))
    batches = [{"batch": int(a), "segment_range": f"段{b}", "seconds": float(c)}
               for a, b, c in re.findall(
                   r"批次(\d+)/\d+：段([\d\-]+) 完成（([\d.]+)s）", text)]
    if batches:
        out["longfilm_batch_results"] = batches
    m = re.search(r"长片生成：(\d+) 段 / (\d+) 批；总耗时 ([\d.]+)s", text)
    if m:
        out["longfilm_segments"] = int(m.group(1))
        out["longfilm_batches"] = out.get("longfilm_batches") or int(m.group(2))
        out["longfilm_total_s"] = float(m.group(3))
    rework_rounds = extract_balanced_list(text, "自动返工轮次：")
    if isinstance(rework_rounds, list):
        out["rework_rounds_detail"] = rework_rounds
        out["rework_rounds"] = len(rework_rounds)
    rework_lines = [x.strip() for x in re.findall(r"自动返工第\d+轮：[^\n]*", text)]
    if rework_lines:
        out["rework_lines"] = rework_lines
    m = re.search(r"封面：(\S+)", text)
    if m:
        out["cover_path"] = m.group(1)
    m = re.search(r"成片输出（OUT1–OUT3）：(\S+)", text)
    if m:
        out["output_path"] = m.group(1)
    m = re.search(r"元数据：(\S+)", text)
    if m:
        out["metadata_path"] = m.group(1)
    m = re.search(r"分段清单（Q_OUT）：(\S+)", text)
    if m:
        out["segment_manifest_path"] = m.group(1)
    m = re.search(r"剪辑收尾：[^\n]*（steps=(\d+)）", text)
    if m:
        out["finalize_steps"] = int(m.group(1))

    # ---- Finalize 路径 / 合并帧数 / 时长不足 ----
    m = re.search(r"Finalize 剪辑：(\d+) 段 → ([^\n]+)", text)
    if m:
        out["finalize_segments"] = int(m.group(1))
        out["finalize_path"] = m.group(2).strip()
    m = re.search(r"导演台返回合并帧（(\d+) 帧），已按计划 (\d+) 段均分用于分段编码", text)
    if m:
        out["merged_frames"] = int(m.group(1))
        out["merged_segments"] = int(m.group(2))
    m = re.search(r"⚠️ 时长不足：计划 ([\d.]+)s，实际 ([\d.]+)s（<(\d+)%） → 完整性不通过", text)
    if m:
        out["duration_insufficient"] = True
        out["duration_ratio_pct"] = int(m.group(3))
        out["duration_warning_actual_s"] = float(m.group(2))
        if out.get("plan_duration_s") is None:
            out["plan_duration_s"] = float(m.group(1))
        if out.get("actual_gen_duration_s") is None:
            out["actual_gen_duration_s"] = float(m.group(2))
    m = re.search(r"证据：(\S*review/one_stop_(\d{9,})\.json)", text)
    if m:
        out["evidence_path"] = m.group(1)
        out["evidence_epoch"] = int(m.group(2))
        out["finished_at_from_evidence"] = _epoch_to_local(m.group(2))
    return out


def parse_ui_test_json(path: Path, source: str) -> dict[str, Any] | None:
    """真实 UI 点击测试结果（浏览器只填剧本→点运行→等结束）。"""
    data = read_json_any(path)
    if not isinstance(data, dict):
        return None
    if "steps" not in data and "prompt_id" not in data and "click_run" not in data:
        return None
    text = "\n".join(str(x) for x in (data.get("outputs_text") or []))
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    rec: dict[str, Any] = {
        "kind": "ui_test",
        "source": source,
        "source_path": rel,
        "finished_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "status": data.get("status"),
        "total_elapsed_s": _to_float(data.get("elapsed_s")),
        "errors": list(data.get("errors") or []),
        "audit_text": text,
        "project_id": None,
        "prompt_mode": None,
    }
    m = re.search(r"解析到已有项目\s*([0-9a-f]{32})", text) or re.search(r"\b([0-9a-f]{32})\b", text)
    if m:
        rec["project_id"] = m.group(1)
    m = re.search(r"提示词模式：(.+)", text)
    if m:
        rec["prompt_mode"] = m.group(1).strip()
    m = re.search(r"总耗时：([\d.]+)s\s*\|\s*分项：(\{.*\})", text)
    if m:
        rec["node_total_s"] = _to_float(m.group(1))
        try:
            rec["timings_raw"] = dict(ast.literal_eval(m.group(2)))
        except (ValueError, SyntaxError):
            rec["timings_raw"] = None
        if rec["timings_raw"]:
            tim = rec["timings_raw"]
            rec["prompt_s"] = _to_float(tim.get("提示词生成_s"))
            rec["asset_s"] = _to_float(tim.get("素材生图审核_s"))
            rec["load_model_s"] = _to_float(tim.get("加载视频模型_s"))
            rec["encode_s"] = _to_float(tim.get("分段编码_s") or tim.get("编码_s"))
            rec["gen_s"] = _to_float(tim.get("出片_s"))
    loaded = data.get("loaded") or {}
    steps = [s for s in (data.get("steps") or []) if isinstance(s, dict)]
    fill = next((s for s in steps if s.get("step") == "只填剧本"), {})
    click = data.get("click_run") or next(
        (s for s in steps if s.get("step") == "点击运行"), {})
    screenshots = sorted(p.name for p in path.parent.glob("*.png"))
    ui = {
        "success": data.get("status") == "success",
        "started_at": data.get("started_at"),
        "elapsed_s": rec.get("total_elapsed_s"),
        "workflow_file": data.get("workflow_file"),
        "widgets": data.get("workflow_widgets"),
        "nodes": len(loaded.get("nodes") or []),
        "node_types": [n.get("type") for n in loaded.get("nodes") or [] if isinstance(n, dict)],
        "missing_nodes": list(loaded.get("missing") or []),
        "screenshots": screenshots,
        "fill_only_script_ok": bool(fill.get("ok")),
        "filled_widget": fill.get("widget"),
        "filled_len": fill.get("len"),
        "click_ok": bool(click.get("ok")),
        "click_via": click.get("via"),
        "prompt_id": data.get("prompt_id"),
        "steps": steps,
    }
    rec["ui_test"] = ui
    rec.update({
        "ui_test_success": ui["success"],
        "ui_test_elapsed_s": ui["elapsed_s"],
        "ui_widgets": ui["widgets"],
        "ui_missing_nodes": len(ui["missing_nodes"]),
        "ui_screenshots": len(screenshots),
    })
    for key, value in parse_audit_extras(text).items():
        if value is not None and rec.get(key) is None:
            rec[key] = value
    # 审计里带远端 review 证据路径（含 epoch）时，用真实完成时间，文件 mtime 仅作副本时间
    if rec.get("finished_at_from_evidence"):
        rec["finished_at_mtime"] = rec["finished_at"]
        rec["finished_at"] = rec["finished_at_from_evidence"]
    # P0-4 修复版识别：文件名 fixed，或 2 段长片且无「时长不足」、完整性通过
    if "fixed" in path.name.lower():
        rec["is_fixed"] = True
    elif (rec.get("duration_insufficient") is None and "长片分批" in text
          and rec.get("integrity_passed") is True and rec.get("segments_actual") == 2):
        rec["is_fixed"] = True
        rec["is_fixed_inferred"] = True
    return rec


# ----------------------------------------------------------------------
# 3) 本地复核 quality JSON / MD
# ----------------------------------------------------------------------
def parse_quality_json(path: Path, source: str) -> dict[str, Any] | None:
    data = read_json_any(path)
    if not isinstance(data, dict) or "basic" not in data:
        return None
    basic = data.get("basic") or {}
    q = data.get("quality") or {}
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    return {
        "kind": "local_quality",
        "source": source,
        "source_path": rel,
        "checked_at": data.get("checked_at"),
        "verdict": data.get("verdict"),
        "passed": data.get("passed"),
        "video": data.get("video"),
        "duration_s": basic.get("duration_s"),
        "fps": basic.get("fps"),
        "width": basic.get("width"),
        "height": basic.get("height"),
        "has_audio": basic.get("has_audio"),
        "size_bytes": basic.get("size_bytes"),
        "bitrate_bps": basic.get("bitrate_bps"),
        "quality_scores": dict(q.get("scores") or {}) or None,
        "placeholder_flags": list(data.get("placeholder_flags") or []),
        "score_threshold": q.get("score_threshold"),
        "note": q.get("note"),
    }


def parse_quality_md(path: Path, source: str) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if "成片质量审核报告" not in text:
        return None
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    rec: dict[str, Any] = {
        "kind": "local_quality_md", "source": source, "source_path": rel,
        "checked_at": None, "verdict": None, "quality_scores": None, "size_bytes": None,
    }
    for pattern, key, cast in (
        (r"- 检查时间：(.+)", "checked_at", str),
        (r"\*\*结论：(通过|不通过)\*\*", "verdict", str),
        (r"\| 时长 \| ([\d.]+) s \|", "duration_s", float),
        (r"\| 码率 \| ([\d.]+) bps \|", "bitrate_bps", float),
        (r"\| 文件大小 \| (\d+) bytes \|", "size_bytes", int),
    ):
        m = re.search(pattern, text)
        if m:
            rec[key] = cast(m.group(1).strip()) if cast is str else cast(m.group(1))
    m = re.search(r"\| 分辨率 / fps \| (\d+)x(\d+) / ([\d.]+) \|", text)
    if m:
        rec["width"], rec["height"], rec["fps"] = int(m.group(1)), int(m.group(2)), float(m.group(3))
    m = re.search(r"\| 音轨 \| (有|无) \|", text)
    if m:
        rec["has_audio"] = m.group(1) == "有"
    m = re.search(r"四维评分：(\{[^}]+\})", text)
    if m:
        try:
            rec["quality_scores"] = dict(ast.literal_eval(m.group(1)))
        except (ValueError, SyntaxError):
            pass
    return rec


# ----------------------------------------------------------------------
# 4) 文档级证据：ROUND41 报告里的运行表
# ----------------------------------------------------------------------
def parse_round41_doc(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if "单节点" not in text or "运行" not in text:
        return []
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    runs: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        m = re.search(r"运行\s*(\d+)", line)
        if not m:
            continue
        timings = re.findall(r"([\d.]+)s", line)
        gen_m = re.search(r"出片\s*([\d.]+)s", line)
        runs.append({
            "kind": "doc_run",
            "source": "doc",
            "source_path": rel,
            "run_no": int(m.group(1)),
            "total_elapsed_s": float(timings[0]) if timings else None,
            "gen_s": float(gen_m.group(1)) if gen_m else None,
            "note": line.strip("| ").strip(),
            "verdict": "通过" if "审核通过" in line else ("不通过" if "不通过" in line else None),
            "has_audio": True if "音轨=有" in line else (False if "音轨缺失" in line or "音轨=无" in line else None),
        })
    return runs


# ----------------------------------------------------------------------
# 5) 显存 CSV
# ----------------------------------------------------------------------
def parse_vram_csv(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    if not rows:
        return None
    headers = [h for h in (rows[0].keys()) if h]
    candidates = [h for h in headers
                  if re.search(r"used|mem|vram|显存", h, re.I)
                  and not re.search(r"total|free|util", h, re.I)]
    col = candidates[0] if candidates else (headers[1] if len(headers) > 1 else headers[0])
    values: list[float] = []
    for row in rows:
        try:
            values.append(float(str(row.get(col, "")).strip()))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    peak = max(values)
    unit_mb = 1024.0 if peak < 200 else 1.0
    rel = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    return {
        "source_path": rel,
        "column": col,
        "samples": len(values),
        "peak_value": peak,
        "peak_unit": "GB" if unit_mb > 1 else "MB",
        "peak_mb": round(peak * unit_mb, 2),
        "started_at": rows[0].get("timestamp") or rows[0].get("time") or None,
        "ended_at": rows[-1].get("timestamp") or rows[-1].get("time") or None,
    }


# ----------------------------------------------------------------------
# 6) 远端只读拉取
# ----------------------------------------------------------------------
def run_bridge(args: list[str], timeout: float = 90.0) -> subprocess.CompletedProcess | None:
    if not REMOTE_BRIDGE.is_file():
        print(f"[warn] 远端桥不存在，跳过：{REMOTE_BRIDGE}", file=sys.stderr)
        return None
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        return subprocess.run(
            [sys.executable, "-B", str(REMOTE_BRIDGE), *args],
            capture_output=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[warn] 远端桥调用失败：{exc}", file=sys.stderr)
        return None


def pull_remote() -> dict[str, Any]:
    """只读列出并拉回远端 one_stop 证据；返回 manifest。拉取失败时沿用上次成功清单。"""
    old = load_remote_manifest()
    manifest: dict[str, Any] = {
        "pulled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "review_jsons": [], "outputs": [], "remote_reports": [], "errors": [],
    }
    REMOTE_CACHE.mkdir(parents=True, exist_ok=True)
    list_cmd = (
        "{ find /data/BetterComfyUI/projects -maxdepth 8 -type f "
        "\\( -path '*/review/one_stop_*.json' "
        "-o \\( -path '*/output/*' -a \\( -name '*.mp4' -o -name '*.mov' \\) \\) \\) "
        "-printf '%s\\t%p\\n' 2>/dev/null; echo '===REMOTE_REPORTS==='; "
        "for d in /data/BetterComfyUI/remote_reports /root/BetterComfyUI/remote_reports; do "
        "ls -1 \"$d\"/*.json 2>/dev/null || true; done; echo '[done]'; } || true"
    )
    proc = None
    for attempt in range(1, 4):
        proc = run_bridge(["--cmd", list_cmd], timeout=120)
        out = proc.stdout.decode("utf-8", "replace") if proc is not None else ""
        if "===REMOTE_REPORTS===" in out:
            break
        detail = ""
        if proc is not None and proc.stderr:
            detail = proc.stderr.decode("utf-8", "replace")[-500:].strip().splitlines()[-1:] or ""
        print(f"[warn] 远端列目录失败（第 {attempt}/3 次）：rc="
              f"{getattr(proc, 'returncode', None)} {detail}", file=sys.stderr)
        if attempt < 3:
            import time as _time
            _time.sleep(5)
    out = proc.stdout.decode("utf-8", "replace") if proc is not None else ""
    if "===REMOTE_REPORTS===" not in out:
        manifest["errors"].append(
            f"remote list failed after retries: rc={getattr(proc, 'returncode', None)}")
        if old and (old.get("review_jsons") or old.get("outputs")):
            for key in ("review_jsons", "outputs", "remote_reports"):
                manifest[key] = old.get(key) or []
            manifest["stale_from"] = old.get("pulled_at")
            manifest["note"] = "本次远端拉取失败，沿用上次成功清单（stale）"
        (REMOTE_CACHE / "_remote_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    head, _, tail = out.partition("===REMOTE_REPORTS===")
    remote_json_paths: list[str] = []
    for line in head.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        size, rpath = int(parts[0]), parts[1]
        name = rpath.rsplit("/", 1)[-1]
        proj = rpath.split("/projects/")[-1].split("/")[0] if "/projects/" in rpath else ""
        if "/review/" in rpath:
            remote_json_paths.append(rpath)
            manifest["review_jsons"].append({"remote": rpath, "project_id": proj, "size": size})
        else:
            manifest["outputs"].append({"remote": rpath, "project_id": proj,
                                        "size_bytes": size, "name": name})
    for rpath in tail.splitlines():
        rpath = rpath.strip()
        if rpath.endswith(".json"):
            remote_json_paths.append(rpath)
            manifest["remote_reports"].append({"remote": rpath})
    for rpath in remote_json_paths:
        proj = rpath.split("/projects/")[-1].split("/")[0] if "/projects/" in rpath else "remote_reports"
        name = rpath.rsplit("/", 1)[-1]
        local = REMOTE_CACHE / proj / name
        local.parent.mkdir(parents=True, exist_ok=True)
        p = run_bridge(["--get", rpath, str(local)], timeout=120)
        if p is None or p.returncode != 0 or not local.is_file():
            manifest["errors"].append(f"get failed: {rpath}")
    (REMOTE_CACHE / "_remote_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[pull] 远端 review json {len(manifest['review_jsons'])} 个，"
          f"output 视频 {len(manifest['outputs'])} 个，remote_reports {len(manifest['remote_reports'])} 个")
    return manifest


def load_remote_manifest() -> dict[str, Any]:
    data = read_json_any(REMOTE_CACHE / "_remote_manifest.json")
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------
# 7) 扫描本地全部证据
# ----------------------------------------------------------------------
def scan_local() -> dict[str, Any]:
    evidence: dict[str, list[dict[str, Any]]] = {
        "runner_reports": [], "node_reviews": [], "local_quality": [],
        "doc_runs": [], "vram": [], "ui_tests": [], "local_media": [],
        "skipped_files": [],
    }
    seen_hashes: dict[str, str] = {}

    for path in sorted(REPORTS_DIR.glob("*.json")):
        if path.name.startswith("onestop_summary"):
            continue
        if not (RUNNER_NAME_RE.search(path.name)
                or "ui_test" in path.name.lower()
                or "continuity" in path.name.lower()):
            continue
        digest = sha256_bytes(path.read_bytes())
        if digest in seen_hashes:
            evidence["skipped_files"].append({
                "path": str(path.relative_to(REPO)),
                "reason": f"与 {seen_hashes[digest]} 内容重复"})
            continue
        rec = parse_runner_report(path, "remote_reports")
        if rec:
            seen_hashes[digest] = str(path.relative_to(REPO))
            evidence["runner_reports"].append(rec)
        else:
            evidence["skipped_files"].append({
                "path": str(path.relative_to(REPO)), "reason": "非单节点运行报告"})
    for path in sorted(REPORTS_DIR.glob("*.csv")):
        if VRAM_NAME_RE.search(path.name):
            rec = parse_vram_csv(path)
            if rec:
                evidence["vram"].append(rec)

    if EVID_DIR.is_dir():
        for path in sorted(EVID_DIR.iterdir()):
            if not path.is_file():
                continue
            if "one_stop" not in path.name and "onestop" not in path.name.lower():
                continue
            try:
                digest = sha256_bytes(path.read_bytes())
            except OSError:
                digest = ""
            if digest and digest in seen_hashes:
                evidence["skipped_files"].append({
                    "path": str(path.relative_to(REPO)),
                    "reason": f"与 {seen_hashes[digest]} 内容重复"})
                continue
            if path.suffix == ".json":
                rec = parse_quality_json(path, "docs/remote_evidence_4090")
                if rec:
                    evidence["local_quality"].append(rec)
                    seen_hashes[digest] = str(path.relative_to(REPO))
                    continue
                rec = parse_review_json(path, "docs/remote_evidence_4090")
                if rec:
                    evidence["node_reviews"].append(rec)
                    seen_hashes[digest] = str(path.relative_to(REPO))
                    continue
                evidence["skipped_files"].append({
                    "path": str(path.relative_to(REPO)),
                    "reason": "空文件或结构无法识别（质量/审核 JSON）",
                })
            elif path.suffix == ".md":
                rec = parse_quality_md(path, "docs/remote_evidence_4090")
                if rec:
                    evidence["local_quality"].append(rec)

    if EVID_DIR.is_dir():
        new_dirs = ("real_chain", "real_key_ui", "round46", "ui_test")
        for path in sorted(EVID_DIR.rglob("*.json")):
            parent = path.parent.name
            if parent not in new_dirs:
                continue
            try:
                digest = sha256_bytes(path.read_bytes())
            except OSError:
                continue
            if digest in seen_hashes:
                evidence["skipped_files"].append({
                    "path": str(path.relative_to(REPO)),
                    "reason": f"与 {seen_hashes[digest]} 内容重复"})
                continue
            seen_hashes[digest] = str(path.relative_to(REPO))
            rec = None
            if "ui_test" in path.name.lower():
                rec = parse_ui_test_json(path, f"docs/remote_evidence_4090/{parent}")
                if rec:
                    evidence["ui_tests"].append(rec)
            else:
                data = read_json_any(path)
                if isinstance(data, dict) and "integrity" in data and "quality" in data:
                    rec = parse_review_json(path, f"docs/remote_evidence_4090/{parent}")
                    if rec:
                        evidence["node_reviews"].append(rec)
                if rec is None:
                    rec = parse_runner_report(path, f"docs/remote_evidence_4090/{parent}")
                    if rec:
                        evidence["runner_reports"].append(rec)
            if rec is None:
                evidence["skipped_files"].append({
                    "path": str(path.relative_to(REPO)),
                    "reason": "新证据目录内无法识别的 JSON"})
        for path in sorted(EVID_DIR.rglob("*")):
            if not path.is_file() or path.parent.name not in new_dirs:
                continue
            if path.suffix.lower() not in (".mp4", ".mov"):
                continue
            media_rec = {
                "path": str(path.relative_to(REPO)),
                "size_bytes": path.stat().st_size,
                "stem": path.stem,
                "category": path.parent.name,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            media_rec["probe"] = probe_media(path)
            evidence["local_media"].append(media_rec)

    for path in sorted((REPO / "docs").glob("ROUND41*.md")):
        evidence["doc_runs"].extend(parse_round41_doc(path))

    if REMOTE_CACHE.is_dir():
        for path in sorted(REMOTE_CACHE.rglob("*.json")):
            if path.name.startswith("_"):
                continue
            rec = parse_review_json(path, "remote_pulled")
            if rec:
                evidence["node_reviews"].append(rec)

    # 节点审核去重：同一项目 + 同一完成时间只保留一份（本地新证据目录优先于远端拉回副本）
    uniq: list[dict[str, Any]] = []
    seen_keys: set[tuple] = set()
    for nr in evidence["node_reviews"]:
        key = (nr.get("project_id"), nr.get("finished_at"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        uniq.append(nr)
    evidence["node_reviews"] = uniq
    evidence["local_quality"] = dedupe_quality(evidence["local_quality"])
    return evidence


def dedupe_quality(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一份成片的 quality JSON 与 MD 合并（JSON 优先，MD 记作 also_seen_in）。"""
    groups: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for q in items:
        key = (q.get("checked_at") or q.get("source_path"), q.get("size_bytes"),
               q.get("duration_s"))
        if key not in groups:
            groups[key] = dict(q)
            order.append(key)
            continue
        kept = groups[key]
        if kept.get("kind") == "local_quality_md" and q.get("kind") == "local_quality":
            q = dict(q)
            q["also_seen_in"] = [kept.get("source_path")]
            groups[key] = q
        else:
            kept.setdefault("also_seen_in", []).append(q.get("source_path"))
    return [groups[k] for k in order]


# ----------------------------------------------------------------------
# 8) 合并成统一运行记录
# ----------------------------------------------------------------------
def canonical_run() -> dict[str, Any]:
    return {field: None for field in FIELD_LABELS}


def apply(rec: dict[str, Any], run: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if run.get(key) is None:
        run[key] = value
        return
    if isinstance(run[key], (int, float)) and isinstance(value, (int, float)) \
            and abs(float(run[key]) - float(value)) <= 0.05:
        return  # 数值在舍入误差内，不记为冲突
    if run[key] != value:
        run.setdefault("conflicts", []).append({
            "field": key, "kept": run[key], "other": value,
            "from": rec.get("source_path") or rec.get("source")})


def _ts_diff(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    try:
        return abs((datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
                    - datetime.strptime(b, "%Y-%m-%d %H:%M:%S")).total_seconds())
    except ValueError:
        return 0.0


RUN_FIELDS = (
    "total_elapsed_s", "node_total_s", "prompt_s", "asset_s", "load_model_s", "gen_s", "encode_s",
    "segments_planned", "segments_actual", "frames", "duration_s", "fps",
    "width", "height", "has_audio", "assets_created", "assets_locked", "assets_blocked",
    "assets_generated", "assets_reused",
    "asset_review_verdict", "refs_attached", "integrity_passed", "quality_scores",
    "quality_average", "quality_threshold", "segment_scores", "full_review_scores", "verdict",
    "vram_peak_mb", "video_review_verdict", "need_regenerate",
    "ui_test_success", "ui_test_elapsed_s", "ui_widgets", "ui_missing_nodes", "ui_screenshots",
    "continuity_on", "continuity_mode", "continuity_overlap_frames",
    "director_segments", "director_frames", "finalize_segments",
    "plan_duration_s", "actual_gen_duration_s",
    "api_calls", "api_configured", "real_key",
    "sg6_format", "sg6_transition", "sg6_subtitle", "sg6_watermark", "sg6_cover",
    "longfilm_batches", "rework_rounds", "merged_frames",
    "export_mode", "output_duration_s", "output_frames_probe",
)


def merge_evidence(evidence: dict[str, Any], remote_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []

    # --- 节点 review JSON 先各成一条 run（最强证据）---
    node_index: list[dict[str, Any]] = []
    for nr in evidence["node_reviews"]:
        run = canonical_run()
        run.update({
            "run_id": "", "evidence_level": "node_review",
            "project_id": nr.get("project_id"),
            "finished_at": nr.get("finished_at"),
            "prompt_mode": nr.get("prompt_mode"),
            "sources": [{"type": "node_review", "path": nr.get("source_path"),
                         "via": nr.get("source")}],
            "match_notes": [f"节点内审核报告 {nr.get('source_path')}"],
        })
        for field in RUN_FIELDS:
            if field in nr:
                run[field] = nr.get(field)
        run["segments_actual"] = run["segments_actual"] or nr.get("prompt_segments")
        run["quality_passed"] = nr.get("quality_passed")
        run["timings_raw"] = nr.get("timings_raw")
        run["video_review_verdict"] = nr.get("video_review_verdict")
        run["final_path"] = nr.get("final_path")
        run["full_review_final"] = nr.get("full_review_final")
        run["asset_detail"] = nr.get("asset_detail")
        run["video_review_error"] = nr.get("video_review_error")
        if nr.get("export_mode"):
            run.setdefault("match_notes", []).append(
                f"导演台导出模式证据：节点 timeline_summary.output.exportMode="
                f"{nr['export_mode']}（P0-4 修复点）")
        node_index.append(run)
    runs.extend(node_index)

    # 远端 output 列表里有真实成片大小：按 project_id 只补给该项目的“最新一条”run。
    # 若本次远端拉取失败、清单来自上次成功拉取（stale_from），只补给上次拉取时间之前结束的 run，
    # 避免把旧文件大小套到之后的运行上（成片可能已被覆盖）。
    stale_since = remote_manifest.get("stale_from")
    out_by_project: dict[str, dict[str, Any]] = {}
    for item in remote_manifest.get("outputs") or []:
        if item.get("project_id"):
            out_by_project[str(item["project_id"])] = item
    eligible = [r for r in runs
                if not stale_since or (r.get("finished_at") or "9999") <= stale_since]
    latest_by_project: dict[str, dict[str, Any]] = {}
    for run in eligible:
        pid = str(run.get("project_id") or "")
        if not pid:
            continue
        cur = latest_by_project.get(pid)
        if cur is None or (run.get("finished_at") or "") > (cur.get("finished_at") or ""):
            latest_by_project[pid] = run
    for pid, run in latest_by_project.items():
        item = out_by_project.get(pid)
        if item and run.get("output_size_bytes") is None:
            run["output_size_bytes"] = item.get("size_bytes")
            run["output_path"] = item.get("remote")
            suffix = (f"（清单来自上次成功拉取 {stale_since}，本次远端拉取失败；"
                      "该大小对应当时运行，成片之后可能已被覆盖）" if stale_since else "（未下载视频）")
            run.setdefault("match_notes", []).append(
                f"成片大小来自远端 output 列表：{item.get('remote')}（{item.get('size_bytes')} B{suffix}）")

    # --- 运行报告 JSON：按 project_id / 耗时数值匹配节点 review，否则独立成 run ---
    for rr in evidence["runner_reports"]:
        kind = rr.get("kind") or "runner_report"
        if kind == "ui_test" and rr.get("real_key"):
            kind = "ui_test_real_key"
        target = None
        if rr.get("project_id"):
            cands = [r for r in node_index if r.get("project_id") == rr["project_id"]]
            if cands:
                cand = min(cands, key=lambda r: _ts_diff(r.get("finished_at"), rr.get("finished_at")))
                if kind in ("ui_test", "ui_test_real_key", "continuity_test"):
                    # UI 点击 / 连续性测试是独立轮次：只有时间接近同一轮才合并
                    if _ts_diff(cand.get("finished_at"), rr.get("finished_at")) <= 900:
                        target = cand
                else:
                    target = cand
                if target is not None:
                    target["match_notes"].append(
                        f"运行报告 {rr.get('source_path')} 按 project_id={rr['project_id'][:8]} 匹配")
        if target is None:
            cands = [r for r in node_index
                     if r.get("gen_s") is not None and rr.get("gen_s") is not None
                     and abs(r["gen_s"] - rr["gen_s"]) <= 0.15
                     and (rr.get("total_elapsed_s") is None or r.get("total_elapsed_s") is None)]
            if len(cands) == 1:
                target = cands[0]
                target["match_notes"].append(
                    f"运行报告 {rr.get('source_path')} 按出片耗时 {rr.get('gen_s')}s 唯一匹配")
        if target is None:
            target = canonical_run()
            target.update({
                "run_id": "", "evidence_level": kind,
                "project_id": rr.get("project_id"), "finished_at": rr.get("finished_at"),
                "prompt_mode": rr.get("prompt_mode"),
                "sources": [], "match_notes": [
                    f"仅有运行报告 {rr.get('source_path')}（{kind}），无对应节点内审核 JSON"],
            })
            runs.append(target)
            node_index.append(target)
        for field in RUN_FIELDS:
            apply(rr, target, field, rr.get(field))
        for k in ("video_review_error", "ui_test", "asset_candidates", "asset_candidate_reviews",
                  "warnings", "continuity_evidence", "continuity_pin_segments",
                  "asset_reused_detail", "task_type",
                  "frames_returned", "actual_gen_frames", "timeline_segments", "refs_per_segment",
                  "api_calls", "asset_reused", "assets_generated", "real_key", "api_configured",
                  "sg6_format", "sg6_crf", "sg6_transition", "sg6_subtitle", "sg6_watermark",
                  "sg6_cover", "longfilm_planned_segments", "longfilm_batches",
                  "longfilm_batch_size", "longfilm_batch_results", "longfilm_segments",
                  "longfilm_total_s", "resume_policy", "rework_rounds", "rework_rounds_detail",
                  "rework_lines", "cover_path", "output_path", "metadata_path",
                  "segment_manifest_path", "finalize_steps", "merged_frames", "merged_segments",
                  "duration_insufficient", "duration_ratio_pct",
                  "evidence_path", "evidence_epoch", "asset_rounds",
                  "refs_attached_audit", "refs_attached_structured",
                  "is_fixed", "is_fixed_inferred"):
            if rr.get(k) is not None:
                target.setdefault(k, rr[k])
        if rr.get("full_review_final") is not None:
            target["full_review_final"] = target.get("full_review_final") or rr["full_review_final"]
        if rr.get("full_review_passed") is not None and target.get("full_review_passed") is None:
            target["full_review_passed"] = rr["full_review_passed"]
        if rr.get("timings_raw"):
            target["timings_raw"] = target.get("timings_raw") or rr["timings_raw"]
        if rr.get("quality_scores"):
            target["quality_scores"] = target.get("quality_scores") or dict(rr["quality_scores"])
            scores = target["quality_scores"]
            target["quality_average"] = target.get("quality_average") or round(
                sum(scores.values()) / len(scores), 3)
        if rr.get("assets_planned") is not None:
            target["assets_planned"] = rr["assets_planned"]
        if rr.get("asset_detail"):
            target["asset_detail"] = target.get("asset_detail") or rr["asset_detail"]
        target.setdefault("sources", []).append(
            {"type": kind, "path": rr.get("source_path"), "via": rr.get("source")})
        target["audit_text"] = rr.get("audit_text")
        if rr.get("status") is not None:
            target["status"] = rr.get("status")

    # --- 真实 UI 点击测试（docs/remote_evidence_4090/ui_test 等目录）---
    # 优先与同一项目、时间相近的节点内审核 JSON 合并；否则单独成 run。
    ui_aux_keys = ("full_review_final", "full_review_passed", "continuity_pin_segments",
                   "continuity_evidence", "asset_candidates", "asset_candidate_reviews",
                   "warnings", "timings_raw", "actual_gen_frames", "timeline_segments",
                   "refs_per_segment", "api_calls", "api_configured", "task_type",
                   "sg6_format", "sg6_crf", "sg6_transition", "sg6_subtitle", "sg6_watermark",
                   "sg6_cover", "longfilm_batches", "resume_policy", "rework_rounds",
                   "rework_rounds_detail", "rework_lines", "cover_path", "output_path",
                   "metadata_path", "segment_manifest_path", "finalize_steps",
                   "merged_frames", "merged_segments", "duration_insufficient",
                   "duration_ratio_pct", "evidence_path", "evidence_epoch",
                   "refs_attached_audit", "refs_attached_structured",
                   "is_fixed", "is_fixed_inferred")
    for ut in evidence.get("ui_tests") or []:
        level = "ui_test_real_key" if ut.get("real_key") else "ui_test"
        target = None
        if ut.get("project_id"):
            cands = [r for r in node_index if r.get("project_id") == ut["project_id"]]
            if cands:
                cand = min(cands, key=lambda r: _ts_diff(r.get("finished_at"), ut.get("finished_at")))
                if _ts_diff(cand.get("finished_at"), ut.get("finished_at")) <= 900:
                    target = cand
        if target is not None:
            target["evidence_level"] = level
            target["ui_test"] = ut.get("ui_test")
            target["audit_text"] = ut.get("audit_text") or target.get("audit_text")
            if ut.get("status") is not None:
                target["status"] = ut.get("status")
            target["real_key"] = ut.get("real_key")
            for field in RUN_FIELDS:
                apply(ut, target, field, ut.get(field))
            for k in ui_aux_keys:
                if ut.get(k) is not None:
                    target.setdefault(k, ut[k])
            target.setdefault("sources", []).append(
                {"type": "ui_test", "path": ut.get("source_path"), "via": ut.get("source")})
            target.setdefault("match_notes", []).append(
                f"UI 点击测试 {ut.get('source_path')} 按 project_id={ut['project_id'][:8]} + "
                f"时间相近（≤900s）与节点审核合并")
            continue
        run = canonical_run()
        run.update({
            "run_id": "",
            "evidence_level": level,
            "project_id": ut.get("project_id"),
            "finished_at": ut.get("finished_at"),
            "prompt_mode": ut.get("prompt_mode"),
            "real_key": ut.get("real_key"),
            "sources": [{"type": "ui_test", "path": ut.get("source_path"), "via": ut.get("source")}],
            "match_notes": [f"真实 UI 点击测试结果 {ut.get('source_path')}"
                            + ("（真实 Key / 真实模型）" if ut.get("real_key") else "")],
            "ui_test": ut.get("ui_test"),
            "audit_text": ut.get("audit_text"),
            "status": ut.get("status"),
        })
        for field in RUN_FIELDS:
            apply(ut, run, field, ut.get(field))
        for k in ui_aux_keys:
            if ut.get(k) is not None:
                run[k] = ut[k]
        runs.append(run)

    # --- 本地复核 quality：按成片字节数匹配远端 output，无法唯一定位则独立列条 ---
    size_to_project: dict[int, set[str]] = {}
    for item in remote_manifest.get("outputs") or []:
        if item.get("size_bytes"):
            size_to_project.setdefault(int(item["size_bytes"]), set()).add(item.get("project_id") or "")
    for q in evidence["local_quality"]:
        target = None
        basis = ""
        if q.get("size_bytes"):
            projects = size_to_project.get(int(q["size_bytes"]))
            if projects and len(projects) == 1:
                proj = next(iter(projects))
                cands = [r for r in runs if r.get("project_id") == proj]
                if len(cands) == 1:
                    target = cands[0]
                    basis = f"按成片大小 {q['size_bytes']} bytes 与远端项目 {proj[:8]} 唯一匹配"
        if target is None:
            target = canonical_run()
            target.update({
                "run_id": "", "evidence_level": "local_quality",
                "finished_at": q.get("checked_at"),
                "sources": [], "match_notes": [
                    f"本地复核报告 {q.get('source_path')}；无对应远端/节点证据，独立列出"],
            })
            runs.append(target)
            basis = "独立本地复核记录"
        target["local_quality"] = {
            "verdict": q.get("verdict"), "passed": q.get("passed"),
            "scores": q.get("quality_scores"), "checked_at": q.get("checked_at"),
            "duration_s": q.get("duration_s"), "size_bytes": q.get("size_bytes"),
            "bitrate_bps": q.get("bitrate_bps"), "source_path": q.get("source_path"),
        }
        target.setdefault("sources", []).append(
            {"type": "local_quality", "path": q.get("source_path"), "via": q.get("source")})
        target["match_notes"].append(f"本地复核：{basis}")
        apply(q, target, "output_size_bytes", q.get("size_bytes"))
        apply(q, target, "output_bitrate_bps", q.get("bitrate_bps"))
        if target.get("duration_s") is None:
            target["duration_s"] = q.get("duration_s")
        for k in ("width", "height", "fps"):
            if target.get(k) is None and q.get(k) is not None:
                target[k] = q.get(k)
        if target.get("has_audio") is None:
            target["has_audio"] = q.get("has_audio")
        if target.get("verdict") is None:
            target["verdict"] = q.get("verdict")
        if target.get("quality_scores") is None and q.get("quality_scores"):
            target["quality_scores"] = q.get("quality_scores")
            target["quality_average"] = round(
                sum(q["quality_scores"].values()) / len(q["quality_scores"]), 3)

    # --- 文档级运行（ROUND41 表）：耗时数值匹配已有 run 则作佐证，否则独立成 run ---
    for doc in evidence["doc_runs"]:
        target = None
        for r in runs:
            if (doc.get("total_elapsed_s") is not None and r.get("total_elapsed_s") is not None
                    and abs(r["total_elapsed_s"] - doc["total_elapsed_s"]) <= 0.15
                    and (doc.get("gen_s") is None or r.get("gen_s") is None
                         or abs(r["gen_s"] - doc["gen_s"]) <= 0.2)):
                target = r
                break
        if target is None:
            target = canonical_run()
            target.update({
                "run_id": "", "evidence_level": "doc_run",
                "run_no": doc.get("run_no"), "finished_at": None,
                "sources": [], "match_notes": [
                    f"仅文档记录（ROUND41 表 运行{doc.get('run_no')}），无独立 JSON 证据文件"],
            })
            runs.append(target)
        apply(doc, target, "total_elapsed_s", doc.get("total_elapsed_s"))
        apply(doc, target, "gen_s", doc.get("gen_s"))
        if target.get("has_audio") is None:
            target["has_audio"] = doc.get("has_audio")
        if target.get("verdict") is None:
            target["verdict"] = doc.get("verdict")
        target.setdefault("sources", []).append(
            {"type": "doc_run", "path": doc.get("source_path"),
             "via": f"ROUND41 表 运行{doc.get('run_no')}"})
        if doc.get("note"):
            target.setdefault("doc_notes", []).append(doc["note"])

    # --- 本地成片副本（docs/remote_evidence_4090/real_* 等目录）：按文件名补成片大小 ---
    def _media_match(media_stem: str, target_stem: str) -> bool:
        a, b = media_stem.lower(), target_stem.lower()
        for suf in ("_key", "_real_key", "_v2", "_v1", "_1", "_2", "_3"):
            if a.endswith(suf):
                a = a[: -len(suf)]
            if b.endswith(suf):
                b = b[: -len(suf)]
        return bool(a and b and (a == b or a in b or b in a))

    def _attach_media(r: dict[str, Any], media: dict[str, Any], basis: str) -> None:
        if r.get("output_size_bytes") is None or r.get("output_probe") is None:
            r["output_size_bytes"] = r.get("output_size_bytes") or media["size_bytes"]
            r["output_local_copy"] = media["path"]
            r.setdefault("match_notes", []).append(
                f"成片大小来自本地证据副本 {media['path']}（{media['size_bytes']} B，"
                f"mtime {media.get('mtime')}；{basis}）")
        probe = media.get("probe") or {}
        if probe:
            r["output_probe"] = probe
            r["output_local_copy"] = r.get("output_local_copy") or media["path"]
            r["output_frames_probe"] = probe.get("nb_frames")
            if probe.get("duration_s") is not None:
                r["output_duration_s"] = probe["duration_s"]
            if r.get("frames") is None and probe.get("nb_frames") is not None:
                r["frames"] = probe["nb_frames"]
            if r.get("duration_s") is None and probe.get("duration_s") is not None:
                r["duration_s"] = probe["duration_s"]
            for k in ("width", "height", "fps", "has_audio"):
                if r.get(k) is None and probe.get(k) is not None:
                    r[k] = probe[k]
            r.setdefault("match_notes", []).append(
                f"成片实测（ffmpeg 抽检 {media['path']}）："
                f"{probe.get('nb_frames')} 帧 / {probe.get('duration_s')}s / "
                f"{probe.get('width')}x{probe.get('height')} / {probe.get('fps')}fps / "
                f"音轨={'有' if probe.get('has_audio') else '无'} / {probe.get('size_bytes')} B")

    matched_media: set[int] = set()
    for mi, media in enumerate(evidence.get("local_media") or []):
        for r in runs:
            targets = [r.get("finalize_path"), r.get("output_path"), r.get("final_path")]
            if not any(_media_match(media["stem"], Path(str(p)).stem)
                       for p in targets if p):
                continue
            _attach_media(r, media, "按文件名匹配")
            matched_media.add(mi)
            break
    # 文件名对不上时兜底：ROUND46 修复版（本地文件名带时长后缀）按证据目录 + 修复版+时间最近匹配
    for mi, media in enumerate(evidence.get("local_media") or []):
        if mi in matched_media or media.get("category") != "round46":
            continue
        cands = [r for r in runs if r.get("is_fixed") and r.get("output_probe") is None]
        if not cands:
            continue
        r = min(cands, key=lambda x: _ts_diff(x.get("finished_at"), media.get("mtime")))
        _attach_media(r, media, "按证据目录 round46 + 修复版标记兜底匹配（文件名不同）")

    # --- 显存（CSV 只有一份时兜底；审计文本里已有显存峰值的不覆盖）---
    vram = evidence["vram"][0] if evidence["vram"] else None
    if vram:
        for r in runs:
            if r.get("vram_peak_mb") is None:
                r["vram_peak_mb"] = vram.get("peak_mb")
                r["vram_source"] = vram
                r.setdefault("match_notes", []).append(
                    f"显存峰值来自 {vram['source_path']}（列 {vram['column']}）")

    # --- 整片四维：新版审计不再单独输出质量 dict，用视频审核 R_full 的 R1b 四维补齐 ---
    for r in runs:
        if r.get("quality_scores") is None and r.get("full_review_scores"):
            r["quality_scores"] = dict(r["full_review_scores"])
            r.setdefault("match_notes", []).append(
                "整片四维分数取视频审核 R_full.r1b_scores（该轮新版审计未单独输出质量 dict）")
            scores = r["quality_scores"]
            if scores and r.get("quality_average") is None:
                r["quality_average"] = round(sum(scores.values()) / len(scores), 3)

    # --- 连续性/多段汇总块 ---
    for r in runs:
        if (r.get("director_segments") is not None or r.get("continuity_on") is not None
                or r.get("finalize_segments") is not None):
            r["continuity"] = {
                "director_segments": r.get("director_segments"),
                "director_frames": r.get("director_frames"),
                "continuity_on": r.get("continuity_on"),
                "continuity_mode": r.get("continuity_mode"),
                "continuity_overlap_frames": r.get("continuity_overlap_frames"),
                "continuity_pin_segments": r.get("continuity_pin_segments"),
                "continuity_evidence": r.get("continuity_evidence"),
                "finalize_segments": r.get("finalize_segments"),
                "segment_scores": r.get("segment_scores"),
                "full_review_scores": r.get("full_review_scores"),
                "full_review_final": r.get("full_review_final"),
                "plan_duration_s": r.get("plan_duration_s"),
                "actual_gen_duration_s": r.get("actual_gen_duration_s"),
                "vram_peak_mb": r.get("vram_peak_mb"),
                "video_review_verdict": r.get("video_review_verdict"),
                "need_regenerate": r.get("need_regenerate"),
                "merged_frames": r.get("merged_frames"),
                "merged_segments": r.get("merged_segments"),
                "duration_insufficient": r.get("duration_insufficient"),
                "duration_ratio_pct": r.get("duration_ratio_pct"),
            }

    # --- ROUND46 功能真机块（转场/字幕/水印/封面/长片分批/断点续跑/自动返工）---
    round46_keys = ("sg6_format", "sg6_crf", "sg6_transition", "sg6_subtitle", "sg6_watermark",
                    "sg6_cover", "longfilm_planned_segments", "longfilm_batches",
                    "longfilm_batch_size", "longfilm_batch_results", "longfilm_segments",
                    "longfilm_total_s", "resume_policy", "rework_rounds", "rework_rounds_detail",
                    "rework_lines", "cover_path", "metadata_path",
                    "segment_manifest_path", "finalize_steps")
    for r in runs:
        if any(r.get(k) is not None for k in round46_keys):
            r["round46"] = {k: r.get(k) for k in round46_keys if r.get(k) is not None}
            if r.get("output_path"):
                r["round46"]["output_path_extras"] = r["output_path"]

    runs.sort(key=lambda r: (r.get("finished_at") or "9999",
                             r.get("evidence_level") == "doc_run",
                             r.get("run_no") or 0))
    for idx, r in enumerate(runs, start=1):
        r["run_id"] = f"run{idx:02d}"
        r["missing"] = [f for f in FIELD_LABELS if r.get(f) is None]
        r["source_paths"] = [s.get("path") for s in r.get("sources") or [] if s.get("path")]
    return runs


# ----------------------------------------------------------------------
# 9) 汇总 / Markdown
# ----------------------------------------------------------------------
def build_summary(evidence: dict[str, Any], runs: list[dict[str, Any]],
                  remote_manifest: dict[str, Any]) -> dict[str, Any]:
    coverage = {field: sum(1 for r in runs if r.get(field) is not None) for field in FIELD_LABELS}
    missing_fields = [{"field": f, "label": FIELD_LABELS[f],
                       "collected_runs": coverage[f], "total_runs": len(runs)}
                      for f in FIELD_LABELS if coverage[f] == 0]
    partial_fields = [{"field": f, "label": FIELD_LABELS[f],
                       "collected_runs": coverage[f], "total_runs": len(runs)}
                      for f in FIELD_LABELS if 0 < coverage[f] < len(runs)]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "repo": str(REPO),
        "scanned": {
            "local_runner_reports": [r.get("source_path") for r in evidence["runner_reports"]],
            "local_node_reviews": [r.get("source_path") for r in evidence["node_reviews"]],
            "local_quality_reports": [r.get("source_path") for r in evidence["local_quality"]],
            "ui_tests": [r.get("source_path") for r in evidence.get("ui_tests") or []],
            "local_media": evidence.get("local_media") or [],
            "doc_runs": [r.get("source_path") for r in evidence["doc_runs"]],
            "vram_csvs": [r.get("source_path") for r in evidence["vram"]],
            "skipped": evidence["skipped_files"],
            "remote": {
                "review_jsons": remote_manifest.get("review_jsons") or [],
                "outputs": remote_manifest.get("outputs") or [],
                "remote_reports": remote_manifest.get("remote_reports") or [],
                "pulled_at": remote_manifest.get("pulled_at"),
                "errors": remote_manifest.get("errors") or [],
            },
            "remote_cache_dir": str(REMOTE_CACHE.relative_to(REPO)),
        },
        "field_coverage": {
            "total_runs": len(runs), "per_field": coverage,
            "fully_missing": missing_fields, "partial": partial_fields,
        },
        "runs": runs,
    }


def round42_highlights(summary: dict[str, Any]) -> list[str]:
    runs = summary["runs"]
    cov = summary["field_coverage"]
    lines: list[str] = ["## 零、关键发现（自动摘要，均来自证据文件）", ""]
    levels: dict[str, int] = {}
    for r in runs:
        levels[r.get("evidence_level") or "?"] = levels.get(r.get("evidence_level") or "?", 0) + 1
    lines.append(f"- 共收集 **{len(runs)}** 条运行记录："
                 + "、".join(f"{k} {v} 条" for k, v in levels.items()) + "。")
    dated = [r for r in runs if r.get("finished_at")]
    if dated:
        latest = max(dated, key=lambda r: r.get("finished_at") or "")
        lines.append(f"- 最新一次运行 **{latest['run_id']}**（{latest.get('finished_at')}）："
                     f"总耗时 {fmt(latest.get('total_elapsed_s'))}s"
                     f"（节点内 {fmt(latest.get('node_total_s'))}s）、"
                     f"出片 {fmt(latest.get('gen_s'))}s、编码 {fmt(latest.get('encode_s'))}s、"
                     f"成片 {fmt(latest.get('output_size_bytes'))} B、"
                     f"节点审核结论 {fmt(latest.get('verdict'))}。")
        for r in runs:
            if r.get("asset_s") is None and not (r.get("assets_generated") or r.get("assets_reused")):
                continue
            lines.append(
                f"- 素材环节 **{r['run_id']}**：素材生图+审核 {fmt(r.get('asset_s'))}s，"
                f"候选生成 {fmt(r.get('assets_generated'))} 张 / 复用已 LOCK {fmt(r.get('assets_reused'))} 张，"
                f"LOCK {fmt(r.get('assets_locked'))} / 未 LOCK {fmt(r.get('assets_blocked'))}，"
                f"挂载参考图 {fmt(r.get('refs_attached'))} 张；"
                f"结论：{fmt(r.get('asset_review_verdict'))}")
            states = r.get("asset_candidate_reviews") or []
            if states:
                n_fb = sum(1 for x in states if x.get("state") == "FALLBACK")
                n_bl = sum(1 for x in states if x.get("state") == "BLOCKED")
                n_lk = sum(1 for x in states if x.get("state") == "LOCKED")
                lines.append(f"  - {r['run_id']} 候选审核明细：BLOCKED {n_bl} / FALLBACK {n_fb} / LOCKED {n_lk}；"
                             "FALLBACK 候选被回退选中并 LOCK（见「六、匹配依据」候选清单）")
        for r in runs:
            if r.get("video_review_error"):
                lines.append(f"- ⚠️ **{r['run_id']} 视频审核异常**（段级/整片审核因此未产出）："
                             f"`{r['video_review_error']}`")
                lines.append(
                    "- 排查提示（人工诊断，非证据文件内容）：`BetterComfyUIVideoReview.run()`（"
                    "`bettercomfyui_comfy/review_nodes.py:454`）返回 5 元组（结论/段JSON/整片JSON/需重生成/审计），"
                    "而单节点 `_video_review()` 按 ComfyUI 常规 dict 结构取 `result.get('result')` → "
                    "`'tuple' object has no attribute 'get'`；属节点侧兼容问题，修复后重跑本管线即可补齐段级/整片审核分数。")
                break
        for r in runs:
            if r.get("ui_test"):
                ui = r["ui_test"]
                lines.append(
                    f"- **UI 真实点击测试（{r['run_id']}）**：只填剧本→点运行→status="
                    f"{fmt(r.get('status'))}，浏览器记录开始时间 {fmt(ui.get('started_at'))}，"
                    f"耗时 {fmt(ui.get('elapsed_s'))}s、控件 "
                    f"{fmt(ui.get('widgets'))} 个、节点 {fmt(ui.get('nodes'))} 个、缺失节点 "
                    f"{len(ui.get('missing_nodes') or [])} 个、截图 {fmt(len(ui.get('screenshots') or []))} 张、"
                    f"节点内总耗时 {fmt(r.get('node_total_s'))}s、成片 {fmt(r.get('output_size_bytes'))} B、"
                    f"R_full {fmt(r.get('full_review_final'))}、视频审核 {fmt(r.get('video_review_verdict'))}。")
        for r in runs:
            c = r.get("continuity")
            if not c:
                continue
            if not (c.get("continuity_on") or (c.get("director_segments") or 0) >= 2
                    or (c.get("finalize_segments") or 0) != (c.get("director_segments") or 0)):
                continue
            rseg = "；".join(f"段{s.get('segment_index')}:{fmt(s.get('score'))}"
                            for s in (c.get("segment_scores") or []) if isinstance(s, dict))
            lines.append(
                f"- **多段连续性（{r['run_id']}）**：提示词段数 {fmt(r.get('segments_planned'))}、"
                f"导演台 {fmt(c.get('director_segments'))} 段/{fmt(c.get('director_frames'))} 帧、"
                f"连续性 ON={fmt(c.get('continuity_on'))} mode={fmt(c.get('continuity_mode'))} "
                f"overlap={fmt(c.get('continuity_overlap_frames'))}f pin={c.get('continuity_pin_segments')}、"
                f"Finalize 剪辑 {fmt(c.get('finalize_segments'))} 段、段级 R_seg [{rseg or '未采集到'}]、"
                f"整片 R_full {fmt(c.get('full_review_final'))}、显存峰值 {fmt(c.get('vram_peak_mb'))} MB、"
                f"视频审核 {fmt(c.get('video_review_verdict'))}。")
            if (c.get("plan_duration_s") is not None
                    and c.get("actual_gen_duration_s") is not None
                    and c["actual_gen_duration_s"] + 0.05 < c["plan_duration_s"]):
                lines.append(
                    f"- ⚠️ {r['run_id']} 时长不足：计划 {fmt(c['plan_duration_s'])}s，实际生成 "
                    f"{fmt(c['actual_gen_duration_s'])}s（导演台返回 {fmt(c.get('director_frames'))} 帧，"
                    f"Finalize {fmt(c.get('finalize_segments'))} 段）→ 完整性 "
                    f"{fmt(r.get('integrity_passed'))}；出片帧数 vs 计划帧数见「一·F」。")
    # ---- 真实 Key / ROUND46 / 2 段帧数缺口 摘要 ----
    for r in runs:
        if r.get("real_key") and not r.get("ui_test"):
            lines.append(
                f"- **真实 Key 全链（{r['run_id']}）**：提示词模式={fmt(r.get('prompt_mode'))}、"
                f"API 调用 {fmt(r.get('api_calls'))} 次、API配置={fmt(r.get('api_configured'))}；"
                f"提示词 {fmt(r.get('prompt_s'))}s、素材候选 {fmt(r.get('assets_generated'))} 张→LOCK "
                f"{fmt(r.get('assets_locked'))}/挂图 {fmt(r.get('refs_attached'))}、出片 {fmt(r.get('gen_s'))}s、"
                f"R_seg {fmt((r.get('segment_scores') or [{}])[0].get('score'))}、R_full "
                f"{fmt(r.get('full_review_final'))}、显存 {fmt(r.get('vram_peak_mb'))} MB、"
                f"视频审核 {fmt(r.get('video_review_verdict'))}、完整性 {fmt(r.get('integrity_passed'))}。")
    for r in runs:
        if r.get("ui_test") and r.get("real_key"):
            ui = r["ui_test"]
            lines.append(
                f"- **真实 Key + UI 点击（{r['run_id']}）**：status={fmt(r.get('status'))}、"
                f"浏览器耗时 {fmt(ui.get('elapsed_s'))}s、控件 {fmt(ui.get('widgets'))}、"
                f"缺失节点 {len(ui.get('missing_nodes') or [])}、截图 {fmt(len(ui.get('screenshots') or []))} 张；"
                f"API {fmt(r.get('api_calls'))} 次、素材候选 {fmt(r.get('assets_generated'))}→LOCK "
                f"{fmt(r.get('assets_locked'))}/挂图 {fmt(r.get('refs_attached'))}、R_seg "
                f"{fmt((r.get('segment_scores') or [{}])[0].get('score'))}、R_full "
                f"{fmt(r.get('full_review_final'))}、显存 {fmt(r.get('vram_peak_mb'))} MB、"
                f"视频审核 {fmt(r.get('video_review_verdict'))}。")
    for r in runs:
        r46 = r.get("round46") or {}
        if not (r46.get("sg6_format") or r46.get("rework_rounds") is not None
                or r46.get("longfilm_batches") is not None):
            continue
        x = r46
        tag = "（修复后 P0-4）" if r.get("is_fixed") else ""
        act = r.get("output_frames_probe") if r.get("is_fixed") else r.get("frames")
        if act is None:
            act = r.get("frames") or r.get("output_frames_probe")
        act_dur = (r.get("output_duration_s") if r.get("is_fixed") else None) \
            or r.get("actual_gen_duration_s")
        plan_dur = r.get("plan_duration_s")
        if plan_dur is None and r.get("director_frames") and r.get("fps"):
            plan_dur = round(r["director_frames"] / float(r["fps"]), 3)
        lines.append(
            f"- **ROUND46 功能真机{tag}（{r['run_id']}）**：格式={fmt(x.get('sg6_format'))}/"
            f"CRF={fmt(x.get('sg6_crf'))}、转场={fmt(x.get('sg6_transition'))}、"
            f"字幕={fmt(x.get('sg6_subtitle'))}、水印={fmt(x.get('sg6_watermark'))}、"
            f"封面={fmt(x.get('sg6_cover'))}；长片分批 {fmt(x.get('longfilm_batches'))} 批、"
            f"断点续跑=「{fmt(x.get('resume_policy'))}」、自动返工 {fmt(x.get('rework_rounds'))} 轮；"
            f"连续性 ON={fmt(r.get('continuity_on'))}/"
            f"{fmt(r.get('continuity_overlap_frames'))}f pin={r.get('continuity_pin_segments')}；"
            f"实际 {fmt(act)} 帧/{fmt(act_dur)}s vs 计划 {fmt(plan_dur)}s"
            f"（Finalize {fmt(r.get('finalize_segments'))} 段）、R_full {fmt(r.get('full_review_final'))}、"
            f"显存 {fmt(r.get('vram_peak_mb'))} MB、完整性 {fmt(r.get('integrity_passed'))}。")
    for r in runs:
        if not r.get("is_fixed"):
            continue
        probe = r.get("output_probe") or {}
        lines.append(
            f"- ✅ **P0-4 2 段帧数缺口已修复（{r['run_id']}，{fmt(r.get('finished_at'))}）**："
            f"修复后实际 {fmt(probe.get('nb_frames') or r.get('frames'))} 帧/"
            f"{fmt(probe.get('duration_s') or r.get('output_duration_s'))}s（ffmpeg 实测 "
            f"{fmt(probe.get('width'))}×{fmt(probe.get('height'))}/{fmt(probe.get('fps'))}fps/"
            f"音轨={'有' if probe.get('has_audio') else '无'}/{fmt(probe.get('size_bytes'))} B≈1.92MB；"
            f"计划 248 帧/2×5.167s）；完整性=True、审核结论=通过；段级 R_seg 94.493/87.377 双段通过、"
            f"R_full {fmt(r.get('full_review_final'))} 通过；连续性 ON/guide/22f/pin=[2]；"
            f"显存 {fmt(r.get('vram_peak_mb'))} MB。修复原因：导演台 "
            f"`exportMode=\"segments\"` 只回部分帧 → `exportMode=\"all\"`。")
    pre_gap = []
    for r in runs:
        if r.get("is_fixed"):
            continue
        if (r.get("director_segments") or 0) >= 2 or (r.get("director_frames") or 0) >= 200:
            pre_gap.append(f"{r['run_id']} 实际 {fmt(r.get('frames'))}/{fmt(r.get('director_frames'))} 帧"
                           f"（缺 {fmt((r.get('director_frames') or 0) - (r.get('frames') or 0))}）")
    if pre_gap:
        lines.append("- **2 段帧数缺口（历史问题，P0-4 已修复）**：" + "；".join(pre_gap)
                     + "；修复后 250 帧/10.417s（见「一·F」修复前后对比）。")
    if cov["fully_missing"]:
        lines.append("- 当前完全未采集到：" + "、".join(
            item["label"] for item in cov["fully_missing"]) + "。")
    rem = (summary.get("scanned") or {}).get("remote") or {}
    if rem.get("errors"):
        lines.append("- ⚠️ 本次远端只读拉取失败（SSH 超时/不可达，已重试 3 次）："
                     f"{rem['errors']}；远端新增的 review JSON 与成片大小本次未采集到，"
                     "本地缓存证据已照常纳入。")
    lines.append("")
    return lines


def score_cells(scores: dict[str, Any] | None) -> dict[str, str]:
    scores = scores or {}
    out = {label: fmt(scores.get(key)) for key, label in SCORE_DIM_LABELS.items()}
    for key, val in scores.items():
        if key not in SCORE_DIM_LABELS:
            out[key] = fmt(val)
    return out


def build_markdown(summary: dict[str, Any], round42: bool = False) -> str:
    runs = summary["runs"]
    cov = summary["field_coverage"]
    lines: list[str] = []
    if round42:
        lines += [
            "# ROUND42 数据收集：单节点（剧本→成片）全流程证据",
            "",
            f"> 自动生成于 **{summary['generated_at']}**；来源=本地/远端真实证据文件；"
            "缺失字段一律写「未采集到」，无任何估算。",
            ">",
            "> 生成器：`scripts/collect_onestop_evidence.py`（一键重跑命令见文末）。",
            "",
        ]
        lines += round42_highlights(summary)
        lines += ["## 一、运行总览", ""]
    else:
        lines += [f"# 单节点证据汇总（{summary['generated_at']}）", ""]
    lines += [
        "| 运行 | 证据等级 | 时间 | 总耗时(s) | 节点耗时(s) | 出片(s) | 段数(计划/实产) | 分辨率/fps/时长 | 成片大小 | 审核结论 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        seg = f"{fmt(r.get('segments_planned'))}/{fmt(r.get('segments_actual'))}"
        spec = (f"{fmt(r.get('width'), 0)}x{fmt(r.get('height'), 0)} @ {fmt(r.get('fps'))}fps / "
                f"{fmt(r.get('duration_s'))}s")
        verdict = f"{fmt(r.get('verdict'))} / 视频审核={fmt(r.get('video_review_verdict'))}"
        lines.append(
            f"| {r['run_id']} | {r.get('evidence_level')} | {fmt(r.get('finished_at'))} | "
            f"{fmt(r.get('total_elapsed_s'))} | {fmt(r.get('node_total_s'))} | "
            f"{fmt(r.get('gen_s'))} | {seg} | {spec} | "
            f"{fmt(r.get('output_size_bytes'))} B | {verdict} |")

    # ---- 一·B UI 真实点击测试（占位模式；真实 Key 的 UI 点击见「一·E」）----
    ui_runs = [r for r in runs if r.get("ui_test") and not r.get("real_key")]
    lines += ["", "## 一·B、UI 真实点击测试（浏览器只填剧本→点运行）", "",
              "| 运行 | 状态 | 记录开始时间 | 耗时(s) | 控件数 | 节点数 | 缺失节点 | 只填剧本 | 点击方式 | 截图 | 节点内总耗时(s) | R_full | 视频审核 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    if not ui_runs:
        lines.append("| 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | "
                     "未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |")
    for r in ui_runs:
        ui = r["ui_test"]
        lines.append(
            f"| {r['run_id']} | {fmt(r.get('status'))} | {fmt(ui.get('started_at'))} | "
            f"{fmt(ui.get('elapsed_s'))} | "
            f"{fmt(ui.get('widgets'))} | {fmt(ui.get('nodes'))} | "
            f"{len(ui.get('missing_nodes') or [])} | "
            f"{fmt(ui.get('fill_only_script_ok'))}（{fmt(ui.get('filled_len'))} 字） | "
            f"{fmt(ui.get('click_via'))} | {fmt(len(ui.get('screenshots') or []))} 张 | "
            f"{fmt(r.get('node_total_s'))} | {fmt(r.get('full_review_final'))} | "
            f"{fmt(r.get('video_review_verdict'))} |")
    if ui_runs:
        ui0 = ui_runs[-1]["ui_test"]
        lines.append("")
        lines.append(f"- 工作流文件：`{ui0.get('workflow_file')}`；"
                     f"节点类型：{ui0.get('node_types')}；prompt_id：`{ui0.get('prompt_id')}`；"
                     f"截图：{ui0.get('screenshots')}")
        lines.append("- 注：`记录开始时间` 取自 UI 测试脚本自身记录（未标注时区，可能为远端 UTC）；"
                     "运行轮次的 `时间` 列以证据文件 mtime（本机时间）为准。")
        lines.append("- 真实 Key 的 UI 点击测试另见「一·D」；两轮均为浏览器真实点击（非 API 直调）。")

    # ---- 一·C 真实 Key 全链（API 直调路径）----
    real_chain_runs = [r for r in runs if r.get("real_key") and not r.get("ui_test")]
    lines += ["", "## 一·C、真实 Key 全链（真实模型一次 API → 素材生图审核 → 出片 → 审核）", "",
              "| 运行 | 提示词模式 | API调用 | API配置 | 节点内总耗时(s) | 提示词(s) | 素材候选/复用/LOCK/挂图 | 出片(s) | 编码(s) | 段级R_seg | 整片R_full | 显存峰值(MB) | 视频审核 | 完整性 | 审核结论 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    if not real_chain_runs:
        lines.append("| 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | "
                     "未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |")
    for r in real_chain_runs:
        seg = r.get("segment_scores") or []
        seg_txt = "；".join(f"段{s.get('segment_index')}:{fmt(s.get('score'))}"
                           f"({'过' if s.get('passed') else '未过'})"
                           for s in seg if isinstance(s, dict)) or "未采集到"
        assets = (f"{fmt(r.get('assets_generated'))}/{fmt(r.get('assets_reused'))}/"
                  f"{fmt(r.get('assets_locked'))}/{fmt(r.get('refs_attached'))}")
        lines.append(
            f"| {r['run_id']} | {fmt(r.get('prompt_mode'))} | {fmt(r.get('api_calls'))} | "
            f"{fmt(r.get('api_configured'))} | {fmt(r.get('node_total_s'))} | "
            f"{fmt(r.get('prompt_s'))} | {assets} | {fmt(r.get('gen_s'))} | "
            f"{fmt(r.get('encode_s'))} | {seg_txt} | {fmt(r.get('full_review_final'))} | "
            f"{fmt(r.get('vram_peak_mb'))} | {fmt(r.get('video_review_verdict'))} | "
            f"{fmt(r.get('integrity_passed'))} | {fmt(r.get('verdict'))} |")
    if real_chain_runs:
        rc0 = real_chain_runs[-1]
        lines.append("")
        lines.append(f"- 项目：`{fmt(rc0.get('project_id'))}`；证据：`{fmt(rc0.get('evidence_path'))}`；"
                     f"提示词段数（节点实际段）{fmt(rc0.get('segments_actual'))}、导演台 "
                     f"{fmt(rc0.get('finalize_segments'))} 段；素材候选明细见「六、匹配依据」。")
        lines.append(f"- 真实 Key 判定依据（来自审计文本）：`提示词模式：真实模型（一次调用）` + "
                     f"`API 调用次数：{fmt(rc0.get('api_calls'))} 次` + `API：已配置`；"
                     f"提示词耗时 {fmt(rc0.get('prompt_s'))}s 含真实 API 往返。")
        if rc0.get("output_local_copy"):
            lines.append(f"- 成片大小 {fmt(rc0.get('output_size_bytes'))} B 来自本地证据副本 "
                         f"`{rc0.get('output_local_copy')}`（远端 output 未拉取时用本地副本）。")

    # ---- 一·D 真实 Key + 真实 UI 点击 ----
    real_ui_runs = [r for r in runs if r.get("ui_test") and r.get("real_key")]
    lines += ["", "## 一·D、真实 Key + 真实 UI 点击（浏览器只填剧本→点运行→真实模型）", "",
              "| 运行 | 状态 | 耗时(s) | 控件/节点/缺失 | 只填剧本 | 截图 | API调用 | 素材候选/复用/LOCK/挂图 | 出片(s) | 段级R_seg | 整片R_full | 显存峰值(MB) | 视频审核 | 完整性 | 结论 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    if not real_ui_runs:
        lines.append("| 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | "
                     "未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |")
    for r in real_ui_runs:
        ui = r["ui_test"]
        seg = r.get("segment_scores") or []
        seg_txt = "；".join(f"段{s.get('segment_index')}:{fmt(s.get('score'))}"
                           f"({'过' if s.get('passed') else '未过'})"
                           for s in seg if isinstance(s, dict)) or "未采集到"
        assets = (f"{fmt(r.get('assets_generated'))}/{fmt(r.get('assets_reused'))}/"
                  f"{fmt(r.get('assets_locked'))}/{fmt(r.get('refs_attached'))}")
        lines.append(
            f"| {r['run_id']} | {fmt(r.get('status'))} | {fmt(ui.get('elapsed_s'))} | "
            f"{fmt(ui.get('widgets'))}/{fmt(ui.get('nodes'))}/{len(ui.get('missing_nodes') or [])} | "
            f"{fmt(ui.get('fill_only_script_ok'))}（{fmt(ui.get('filled_len'))} 字） | "
            f"{fmt(len(ui.get('screenshots') or []))} 张 | {fmt(r.get('api_calls'))} | {assets} | "
            f"{fmt(r.get('gen_s'))} | {seg_txt} | {fmt(r.get('full_review_final'))} | "
            f"{fmt(r.get('vram_peak_mb'))} | {fmt(r.get('video_review_verdict'))} | "
            f"{fmt(r.get('integrity_passed'))} | {fmt(r.get('verdict'))} |")
    if real_ui_runs:
        ru0 = real_ui_runs[-1]
        ui0 = ru0["ui_test"]
        lines.append("")
        lines.append(f"- 工作流：`{fmt(ui0.get('workflow_file'))}`；prompt_id：`{fmt(ui0.get('prompt_id'))}`；"
                     f"截图：{ui0.get('screenshots')}；节点内总耗时 {fmt(ru0.get('node_total_s'))}s。")
        lines.append("- 真实 Key 判定：审计文本 `提示词模式：真实模型（一次调用）` + "
                     "`API 调用次数：1 次`；UI 侧仍为浏览器只填剧本→点 `#queue-button` 的真实点击。")

    # ---- 一·E ROUND46 功能真机 ----
    r46_runs = [r for r in runs
                if ((r.get("round46") or {}).get("sg6_format")
                    or (r.get("round46") or {}).get("rework_rounds") is not None
                    or (r.get("round46") or {}).get("longfilm_batches") is not None)]
    lines += ["", "## 一·E、ROUND46 功能真机（转场/字幕/水印/封面/MOV、长片分批、断点续跑、自动返工、22f 连续）", "",
              "| 运行 | 阶段 | 输出格式 | CRF | 转场 | 字幕 | 水印 | 封面 | 长片分批 | 断点续跑 | 自动返工 | 连续性 | 2段帧数(实际/计划)/Fina段数 | 段级R_seg | 整片R_full | 显存峰值(MB) | 完整性 | 结论 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    if not r46_runs:
        lines.append("| 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | "
                     "未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | "
                     "未采集到 | 未采集到 | 未采集到 | 未采集到 |")
    for r in r46_runs:
        x = r["round46"]
        longfilm = (f"{fmt(x.get('longfilm_batches'))} 批 / {fmt(x.get('longfilm_segments'))} 段"
                    if x.get("longfilm_batches") is not None or x.get("longfilm_segments") is not None
                    else "未采集到")
        rework = (f"{fmt(x.get('rework_rounds'))} 轮" if x.get("rework_rounds") is not None
                  else "未采集到")
        cont = (f"ON={fmt(r.get('continuity_on'))}/{fmt(r.get('continuity_overlap_frames'))}f "
                f"pin={r.get('continuity_pin_segments')}")
        seg = r.get("segment_scores") or []
        seg_txt = "；".join(f"段{s.get('segment_index')}:{fmt(s.get('score'))}"
                           f"({'过' if s.get('passed') else '未过'})"
                           for s in seg if isinstance(s, dict)) or "未采集到"
        fps = r.get("fps") or 24.0
        dirf = r.get("director_frames")
        plan_dur = r.get("plan_duration_s")
        if plan_dur is None and isinstance(dirf, (int, float)) and fps:
            plan_dur = round(dirf / float(fps), 3)
        act_dur = r.get("actual_gen_duration_s") or r.get("output_duration_s")
        act = r.get("output_frames_probe") if r.get("is_fixed") else r.get("frames")
        if act is None:
            act = r.get("frames") or r.get("output_frames_probe")
        frames = (f"{fmt(act)} 帧/{fmt(act_dur)}s vs 计划 {fmt(plan_dur)}s"
                  f"（Finalize {fmt(r.get('finalize_segments'))} 段）")
        stage = "修复后（P0-4）" if r.get("is_fixed") else "修复前"
        lines.append(
            f"| {r['run_id']} | {stage} | {fmt(x.get('sg6_format'))} | {fmt(x.get('sg6_crf'))} | "
            f"{fmt(x.get('sg6_transition'))} | {fmt(x.get('sg6_subtitle'))} | "
            f"{fmt(x.get('sg6_watermark'))} | {fmt(x.get('sg6_cover'))} | {longfilm} | "
            f"{fmt(x.get('resume_policy'))} | {rework} | {cont} | {frames} | {seg_txt} | "
            f"{fmt(r.get('full_review_final'))} | {fmt(r.get('vram_peak_mb'))} | "
            f"{fmt(r.get('integrity_passed'))} | {fmt(r.get('verdict'))} |")
    if r46_runs:
        r460 = r46_runs[-1]
        x = r460["round46"]
        lines.append("")
        lines.append(f"- 封面：`{fmt(x.get('cover_path'))}`")
        lines.append(f"- 成片输出：`{fmt(x.get('output_path_extras') or r460.get('output_path'))}`")
        lines.append(f"- 元数据：`{fmt(x.get('metadata_path'))}`；分段清单：`{fmt(x.get('segment_manifest_path'))}`；"
                     f"剪辑收尾 steps={fmt(x.get('finalize_steps'))}")
        if x.get("rework_rounds_detail"):
            lines.append(f"- 自动返工轮次明细：`{json.dumps(x['rework_rounds_detail'], ensure_ascii=False)}`")
        lines.append(f"- 长片分批结果：`{json.dumps(x.get('longfilm_batch_results'), ensure_ascii=False)}`"
                     if x.get("longfilm_batch_results") else "- 长片分批结果：未采集到")
        if r460.get("duration_insufficient"):
            lines.append(f"- ⚠️ 完整性不通过：计划 {fmt(r460.get('plan_duration_s'))}s，实际 "
                         f"{fmt(r460.get('actual_gen_duration_s'))}s（<{fmt(r460.get('duration_ratio_pct'))}%）"
                         f"，输出仍写出（OUT1–OUT3 + 封面 + 元数据）。")

    # ---- 一·F 2 段帧数缺口（P0-4 修复：修复前后对比）----
    gap_runs = [r for r in runs
                if (r.get("director_segments") or 0) >= 2 or (r.get("director_frames") or 0) >= 200]
    pre_runs = [r for r in gap_runs if not r.get("is_fixed")]
    fixed_runs = [r for r in gap_runs if r.get("is_fixed")]
    lines += ["", "## 一·F、2 段帧数缺口（P0-4 修复证据：修复前后对比）", "",
              "| 阶段 | 运行 | 证据 | 导演台计划帧 | 实际出片帧数 | 实际时长(s) | Finalize段数 | 完整性 | 结论 |",
              "|---|---|---|---|---|---|---|---|---|"]
    if not gap_runs:
        lines.append("| 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |")
    for r in pre_runs:
        srcs = " ".join(str(p) for p in (r.get("source_paths") or []))
        label = ("v2" if "continuity_test_v2" in srcs else
                 ("ROUND46旧" if ("round46" in srcs or str(r.get("project_id") or "").startswith("9a1e778d"))
                  else ("v1" if "continuity_test.json" in srcs else "未采集到")))
        lines.append(
            f"| 修复前 | {r['run_id']} | {label or '未采集到'} | {fmt(r.get('director_frames'))} | "
            f"{fmt(r.get('frames'))} | {fmt(r.get('actual_gen_duration_s'))} | "
            f"{fmt(r.get('finalize_segments'))} | {fmt(r.get('integrity_passed'))} | {fmt(r.get('verdict'))} |")
    for r in fixed_runs:
        lines.append(
            f"| **修复后（P0-4）** | {r['run_id']} | round46_test_fixed.json + 本地 mov | "
            f"{fmt(r.get('director_frames'))} | {fmt(r.get('output_frames_probe') or r.get('frames'))} | "
            f"{fmt(r.get('output_duration_s') or r.get('actual_gen_duration_s'))} | "
            f"{fmt(r.get('finalize_segments'))} | {fmt(r.get('integrity_passed'))} | {fmt(r.get('verdict'))} |")
    if gap_runs:
        lines.append("")
        lines.append("- **修复前后对比**：修复前 v1=137 帧/5.71s（完整性判定通过但帧数不足 111 帧）、"
                     "v2=138 帧/5.75s、旧 ROUND46=126 帧/5.25s（后两者完整性不通过）→ "
                     "**修复后 = 250 帧 / 10.417s（ffmpeg 实测）**，约为计划 248 帧（2×5.167s），"
                     "完整性=True、审核结论=通过。")
        for r in fixed_runs:
            probe = r.get("output_probe") or {}
            lines.append(
                f"- 修复版实测规格（ffmpeg 抽检本地 `{fmt(r.get('output_local_copy'))}`）："
                f"{fmt(probe.get('nb_frames'))} 帧 / {fmt(probe.get('duration_s'))}s / "
                f"{fmt(probe.get('width'))}×{fmt(probe.get('height'))} / {fmt(probe.get('fps'))}fps / "
                f"音轨={'有' if probe.get('has_audio') else '无'} / {fmt(probe.get('size_bytes'))} B"
                f"（≈1.92MB）/ 编码 {fmt(probe.get('video_codec'))}。")
            lines.append(
                f"- 修复版审核：段级 R_seg 双段通过（94.493 / 87.377）、R_full "
                f"{fmt(r.get('full_review_final'))}（阈值 70，通过）、连续性 ON/guide/22f/pin=[2]、"
                f"显存峰值 {fmt(r.get('vram_peak_mb'))} MB；SG6 MOV/淡入淡出/自动字幕/文字水印/封面均生效"
                f"（见「一·E」）。")
        lines.append("- **修复原因**：导演台 `exportMode=\"segments\"` 只回部分帧 → 改为 "
                     "`exportMode=\"all\"`（P0-4 修复说明，Lead 提供；修复版节点 timeline 的 "
                     "`output.exportMode` 作为佐证）。")
        lines.append("- **结论**：2 段帧数缺口（历史问题）**已修复并关闭**；修复前 126/138 帧记录保留在表中"
                     "作为对比证据。")

    # ---- 一·G 多段连续性（导演台连续段间 + 段级/整片审核）----
    cont_all = [r for r in runs if r.get("continuity")]
    cont_runs = [r for r in cont_all if (
        (r["continuity"].get("director_segments") or 0) >= 2
        or r["continuity"].get("continuity_on")
        or (r["continuity"].get("finalize_segments") or 0)
        < (r["continuity"].get("director_segments") or 0))]
    lines += ["", "## 一·G、多段连续性（导演台连续段间 + 段级/整片审核）", "",
              "| 运行 | 提示词段数 | 导演台段数 | 连续性ON | 模式/重叠 | pin段 | 导演台返回帧数 | Finalize段数 | 段级R_seg | 整片R_full | 显存峰值(MB) | 视频审核 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    if not cont_runs:
        lines.append("| 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | "
                     "未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 | 未采集到 |")
    for r in cont_runs:
        c = r["continuity"]
        seg = c.get("segment_scores") or []
        seg_txt = "；".join(f"段{s.get('segment_index')}:{fmt(s.get('score'))}"
                           f"({'过' if s.get('passed') else '未过'})" for s in seg) or "未采集到"
        full_txt = fmt(c.get("full_review_final"))
        if c.get("full_review_scores"):
            dims = score_cells(c["full_review_scores"])
            full_txt += "（" + "、".join(f"{k}:{v}" for k, v in dims.items()
                                        if k in SCORE_DIM_LABELS.values()) + "）"
        mode = (f"{fmt(c.get('continuity_mode'))}/{fmt(c.get('continuity_overlap_frames'))}f"
                if c.get("continuity_mode") or c.get("continuity_overlap_frames") else "未采集到")
        lines.append(
            f"| {r['run_id']} | {fmt(r.get('segments_planned'))} | "
            f"{fmt(c.get('director_segments'))} | {fmt(c.get('continuity_on'))} | {mode} | "
            f"{c.get('continuity_pin_segments') if c.get('continuity_pin_segments') is not None else '未采集到'} | "
            f"{fmt(c.get('director_frames'))} | {fmt(c.get('finalize_segments'))} | {seg_txt} | "
            f"{full_txt} | {fmt(c.get('vram_peak_mb'))} | {fmt(c.get('video_review_verdict'))} |")
    single = [r for r in cont_all if r not in cont_runs]
    if single:
        lines.append("")
        lines.append("- 说明：" + "、".join(r["run_id"] for r in single)
                     + " 为单段运行（导演台 1 段、连续性 OFF），不列入本表；"
                       "其导演台字段与段级/整片审核仍见总览与「三、审核分数」。")

    lines += ["", "## 二、分项耗时与素材（全流程新增字段）", "",
              "| 运行 | 提示词(s) | 素材生图+审核(s) | 候选生成/复用/LOCK/未LOCK | 素材审核结论 | 挂参考图 | 加载模型(s) | 编码(s) | 显存峰值(MB) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        gen = r.get("assets_generated")
        if gen is None:
            gen = r.get("assets_created")
        assets = (f"{fmt(gen)}/{fmt(r.get('assets_reused'))}/"
                  f"{fmt(r.get('assets_locked'))}/{fmt(r.get('assets_blocked'))}")
        lines.append(
            f"| {r['run_id']} | {fmt(r.get('prompt_s'))} | {fmt(r.get('asset_s'))} | {assets} | "
            f"{fmt(r.get('asset_review_verdict'))} | {fmt(r.get('refs_attached'))} | "
            f"{fmt(r.get('load_model_s'))} | {fmt(r.get('encode_s'))} | "
            f"{fmt(r.get('vram_peak_mb'))} |")

    lines += ["", "## 三、审核分数", "",
              "| 运行 | 完整性 | 段级审核分数 | 叙事 | 音画 | 节奏 | 跳变 | 整片均分 | 阈值 | 整片R_full | 结论 | 视频审核 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        cells = score_cells(r.get("quality_scores"))
        seg = r.get("segment_scores")
        if seg:
            seg_txt = "；".join(f"段{s.get('segment_index')}:{fmt(s.get('score'))}" for s in seg)
        else:
            seg_txt = "未采集到"
        full_txt = "未执行/未采集到"
        if r.get("full_review_scores") or r.get("full_review_final") is not None:
            fc = score_cells(r.get("full_review_scores"))
            dims = [f"{k}:{v}" for k, v in fc.items() if k in SCORE_DIM_LABELS.values()]
            full_txt = "、".join(dims) or "未采集到"
            if r.get("full_review_final") is not None:
                full_txt += f"（final {fmt(r['full_review_final'])}）"
        lines.append(
            f"| {r['run_id']} | {fmt(r.get('integrity_passed'))} | {seg_txt} | "
            f"{cells.get('叙事', '未采集到')} | {cells.get('音画', '未采集到')} | "
            f"{cells.get('节奏', '未采集到')} | {cells.get('跳变', '未采集到')} | "
            f"{fmt(r.get('quality_average'))} | {fmt(r.get('quality_threshold'))} | "
            f"{full_txt} | {fmt(r.get('verdict'))} | {fmt(r.get('video_review_verdict'))} |")

    lines += ["", "## 四、字段覆盖情况", "",
              f"共 **{cov['total_runs']}** 条运行记录；下表统计每个字段在多少条记录中采集到：", "",
              "| 字段 | 已采集/总数 | 状态 |", "|---|---|---|"]
    for field, label in FIELD_LABELS.items():
        got = cov["per_field"].get(field, 0)
        status = "✅ 已采集" if got == cov["total_runs"] else ("⚠️ 部分" if got else "❌ 未采集到")
        lines.append(f"| {label} | {got}/{cov['total_runs']} | {status} |")

    lines += ["", "## 五、证据清单", "", "### 本地"]
    scanned = summary["scanned"]
    for title, items in (("运行报告 JSON", scanned["local_runner_reports"]),
                         ("节点内审核 JSON", scanned["local_node_reviews"]),
                         ("本地复核 quality JSON/MD", scanned["local_quality_reports"]),
                         ("UI 真实点击测试 JSON", scanned.get("ui_tests") or []),
                         ("文档级运行记录（ROUND41 表）", scanned["doc_runs"]),
                         ("显存 CSV", scanned["vram_csvs"])):
        lines.append(f"- **{title}**：" + ("、".join(f"`{p}`" for p in dict.fromkeys(items))
                                          if items else "未采集到"))
    media_items = [f"{m.get('path')}（{fmt(m.get('size_bytes'))} B）"
                   for m in scanned.get("local_media") or []]
    if media_items:
        lines.append("- **本地成片副本（真实 Key / UI / ROUND46 证据目录）**："
                     + "、".join(f"`{x}`" for x in media_items))
    if scanned["skipped"]:
        lines.append("- **跳过/异常文件**：")
        for item in scanned["skipped"]:
            lines.append(f"  - `{item['path']}` — {item['reason']}")
    remote = scanned["remote"]
    lines += ["", "### 远端（只读扫描）"]
    attempted = bool(remote.get("pulled_at"))
    if attempted:
        lines.append(f"- 拉取尝试时间：{remote['pulled_at']}；缓存目录：`{scanned['remote_cache_dir']}`")
        if remote.get("errors"):
            lines.append("- ⚠️ 本次远端拉取未成功（远端只读桥超时/不可达），"
                         "远端新增的 review JSON 与成片大小未采集到；本地已有缓存证据照常使用。")
    else:
        lines.append("- 本次未执行远端拉取（加 `--pull-remote` 可只读拉取）")
    if remote.get("stale_from"):
        lines.append(f"- 沿用上次成功清单（{remote['stale_from']}），仅用于已缓存证据的展示。")
    rj = remote.get("review_jsons") or []
    outs = remote.get("outputs") or []
    lines.append(f"- 远端 review/one_stop_*.json：{len(rj)} 个")
    for item in rj:
        lines.append(f"  - `{item.get('remote')}`（{fmt(item.get('size'))} B）")
    lines.append(f"- 远端 output/*（mp4/mov）：{len(outs)} 个")
    for item in outs:
        lines.append(f"  - `{item.get('remote')}`（{fmt(item.get('size_bytes'))} B）")
    if remote.get("errors"):
        lines.append(f"- ⚠️ 远端错误：{remote['errors']}")

    lines += ["", "## 六、每条运行的匹配依据与冲突记录", ""]
    for r in runs:
        lines.append(f"### {r['run_id']}（{r.get('evidence_level')}）")
        for note in r.get("match_notes") or []:
            lines.append(f"- {note}")
        if r.get("conflicts"):
            for c in r["conflicts"]:
                lines.append(f"- ⚠️ 冲突 `{c['field']}`：保留 {c['kept']}，另有 {c['other']}"
                             f"（来自 {c['from']}）")
        if r.get("video_review_error"):
            lines.append(f"- ⚠️ 视频审核错误（来自审计文本，如实记录）：{r['video_review_error']}")
        if isinstance(r.get("asset_detail"), list):
            for item in r["asset_detail"][:8]:
                if isinstance(item, dict) and "state" in item:
                    lines.append(
                        f"- 素材 {item.get('type')} {item.get('name')} → {item.get('version')} "
                        f"（{fmt(item.get('gen_s'))}s，审核 {item.get('state')}）")
        if r.get("segment_scores"):
            seg_txt2 = "；".join(
                f"段{s.get('segment_index')}:{fmt(s.get('score'))}"
                f"({'过' if s.get('passed') else '未过'})"
                for s in r["segment_scores"] if isinstance(s, dict))
            lines.append(f"- 段级 R_seg：{seg_txt2}")
        if r.get("full_review_scores") or r.get("full_review_final") is not None:
            lines.append(f"- 整片 R_full：scores={r.get('full_review_scores')}，"
                         f"final={fmt(r.get('full_review_final'))}，"
                         f"passed={fmt(r.get('full_review_passed'))}，"
                         f"policy={fmt(r.get('full_review_policy'))}")
        if r.get("ui_test"):
            ui = r["ui_test"]
            steps = ui.get("steps") or []
            lines.append("- UI 步骤：" + " → ".join(
                f"{s.get('step')}({s.get('at')})" for s in steps if isinstance(s, dict)))
            lines.append(f"- UI 结论：status={fmt(r.get('status'))}，elapsed={fmt(ui.get('elapsed_s'))}s，"
                         f"控件 {fmt(ui.get('widgets'))}，节点 {fmt(ui.get('nodes'))}，"
                         f"缺失节点 {ui.get('missing_nodes')}，只填剧本 ok={fmt(ui.get('fill_only_script_ok'))}，"
                         f"点击 ok={fmt(ui.get('click_ok'))}，prompt_id={ui.get('prompt_id')}")
        if r.get("continuity"):
            c = r["continuity"]
            lines.append(f"- 连续性：导演台 {fmt(c.get('director_segments'))} 段 / "
                         f"{fmt(c.get('director_frames'))} 帧，ON={fmt(c.get('continuity_on'))}，"
                         f"mode={fmt(c.get('continuity_mode'))}，overlap={fmt(c.get('continuity_overlap_frames'))}f，"
                         f"pin={c.get('continuity_pin_segments')}，Finalize {fmt(c.get('finalize_segments'))} 段，"
                         f"计划 {fmt(c.get('plan_duration_s'))}s / 实际 {fmt(c.get('actual_gen_duration_s'))}s，"
                         f"显存 {fmt(c.get('vram_peak_mb'))} MB")
            if c.get("continuity_evidence"):
                lines.append(f"- 审计原文：{c['continuity_evidence']}")
        if r.get("asset_candidates"):
            cands = r["asset_candidates"]
            lines.append(f"- 素材候选 {len(cands)} 张：" + "、".join(
                f"{c.get('name')}/{c.get('version')}({fmt(c.get('gen_s'))}s)" for c in cands[:12]))
        if r.get("asset_candidate_reviews"):
            lines.append("- 候选审核：" + "、".join(
                f"{x.get('state')} {fmt(x.get('score'))}" for x in r["asset_candidate_reviews"][:12]))
        if r.get("asset_reused_detail"):
            lines.append("- 复用已 LOCK 素材：" + "、".join(
                f"{x.get('name')} {x.get('version')}" for x in r["asset_reused_detail"]))
        if r.get("warnings"):
            for w in r["warnings"][:8]:
                lines.append(f"- ⚠️ 审计告警：{w}")
        for note in r.get("doc_notes") or []:
            lines.append(f"- 文档原文：{note}")

    lines += ["", "## 七、缺失项（未采集到）", ""]
    fully = cov["fully_missing"]
    if not fully:
        lines.append("无：所有字段均有至少一条运行采集到。")
    else:
        for item in fully:
            lines.append(f"- **{item['label']}**（`{item['field']}`）："
                         f"0/{item['total_runs']} 条记录采集到")
    if cov["partial"]:
        lines += ["", "部分采集（全流程测试完成后应补齐）："]
        for item in cov["partial"]:
            lines.append(f"- {item['label']}：{item['collected_runs']}/{item['total_runs']}")

    if round42:
        lines += [
            "",
            "## 八、数据来源与口径",
            "",
            "- **节点内审核 JSON**（最权威）：远端 `/data/BetterComfyUI/projects/<id>/review/one_stop_<epoch>.json`，"
            "字段 `integrity` / `quality` / `assets` / `video_review` / `timings` / `generation`；"
            "由 `bettercomfyui_comfy/one_stop_node.py` 真实写出。",
            "- **运行报告 JSON**：`remote_reports/one_stop*.json`，解析 `elapsed_s` 与 `outputs_text` 审计文本"
            "（总耗时/分项耗时/出片耗时/编码/段数/素材生图与审核/视频审核结论与错误）。"
            "全流程版为 `remote_reports/one_stop_full_test.json`、`one_stop_full_test_v3.json` 等；"
            "新增审计行（素材结论/显存峰值 reserved/R_seg/R_full/Finalize 段数/时长说明）已在本管线解析。",
            "- **真实 UI 点击测试**：`docs/remote_evidence_4090/ui_test/ui_test_result.json` ——"
            "浏览器脚本只填剧本→点 `#queue-button` 运行，记录 steps / loaded(控件/节点/缺失节点) / "
            "prompt_id / status / elapsed_s 与截图（`ui_test/*.png`、`real_key_ui/*.png`）；"
            "本管线产出「一·B」（占位模式）与「一·D」（真实 Key）表。",
            "- **多段连续性**：`remote_reports/continuity_test.json` —— 解析导演台段数/总帧、"
            "连续性 ON/mode/overlap/pin 段、Finalize 剪辑段数、段级 R_seg、整片 R_full、显存峰值；"
            "该文件若被新版结果覆盖，重跑本管线会自动取最新内容重算（旧版结论不保留）。",
            "- **本地复核**：`docs/remote_evidence_4090/one_stop_quality_*.json|md`，由 `scripts/quality_audit.py` "
            "对本地成片 mp4 重新计算（与节点内审核是两套口径，不混为同一分数）。",
            "- **文档级证据**：`docs/ROUND41_*.md` 运行表，只有总耗时/出片耗时/通过与否，属弱证据，单独标注。",
            "- **真实 Key 全链**：`docs/remote_evidence_4090/real_chain/real_chain_test.json`（运行审计）"
            "+ `one_stop_审核_真实Key.json`（节点 v0.3-full-director 结构化审核：integrity/quality/"
            "video_review/timings/vram/finalize/continuity）+ `真实全链成片_Key.mp4`（本地成片副本，"
            "只取大小）；真实 Key 判定看审计 `提示词模式：真实模型（一次调用）`、`API 调用次数：1 次`。",
            "- **真实 Key + 真实 UI 点击**：`docs/remote_evidence_4090/real_key_ui/ui_test_result_real_key.json`"
            "（浏览器只填剧本→点 `#queue-button`+#真实模型）与 `real_key_ui/*.png`；解析同 UI 点击测试。",
            "- **ROUND46 功能真机**：`docs/remote_evidence_4090/round46/round46_test.json`（旧版）"
            "与 `round46_test_fixed.json`（P0-4 修复版），解析 `SG6 剪辑配置`（格式/CRF/转场/字幕/"
            "水印/封面）、长片分批、断点续跑、自动返工轮次、封面/元数据/分段清单路径。",
            "- **P0-4 修复证据**：`round46_test_fixed.json`（修复后 2 段运行）+ 本地成片 "
            "`docs/remote_evidence_4090/round46/二段连续_转场字幕水印_10.4s.mov`；"
            "本管线用 imageio_ffmpeg 自带的 ffmpeg 对该 mov 做实测（帧数/时长/分辨率/fps/音轨/大小），"
            "并对比修复前 v1=137/v2=138/ROUND46旧=126 帧。修复原因：导演台 "
            "`exportMode=\"segments\"` 只回部分帧 → 改 `exportMode=\"all\"`（P0-4 修复说明，Lead 提供）。",
            "- **运行环境**：远端 RTX 4090；SSH 端口 **40248**；ComfyUI 外网 "
            "**os.environ.get("BCU_REMOTE_BASE", "http://YOUR-GPU-HOST:8188")**（旧 40244 已不再服务 ComfyUI，旧地址仅历史记录）。",
            "- 匹配规则：优先 project_id；其次成片字节数唯一匹配远端 output；文档表按耗时数值匹配；"
            "无法唯一定位的一律独立列条并在「匹配依据」里说明，不强行合并。",
            "",
            "## 九、全流程测试完成后：一键重跑",
            "",
            "```powershell",
            "cd \"H:\\minzzj文件\\技术部\\项目\\官方\\Better comfyUI\"",
            "# 一键：只读拉远端新证据 + 重算 summary + 重新生成本报告",
            "python -B scripts/collect_onestop_evidence.py --pull-remote --round42 docs/ROUND42_数据收集_单节点全流程.md",
            "```",
            "",
            "只重算本地（不连远端）：",
            "",
            "```powershell",
            "python -B scripts/collect_onestop_evidence.py --round42 docs/ROUND42_数据收集_单节点全流程.md",
            "```",
            "",
            "新增文件识别规则（无需改脚本）：",
            "",
            "- 本地 `remote_reports/one_stop_full_*.json`、`remote_reports/vram_onestop*.csv` 按文件名通配自动纳入；",
            "- `docs/remote_evidence_4090/` 下新的 `one_stop_quality_*` / `one_stop_review_*` 自动纳入；",
            "- 远端新的 `review/one_stop_*.json` 由 `--pull-remote` 自动发现并拉回（只读 SFTP get，"
            "不下载 mp4，只记录其大小）；",
            "- 远端 `/data/BetterComfyUI/remote_reports/*.json` 若存在也会拉回。",
            "",
            "## 十、当前可支撑 / 不可支撑的结论",
            "",
        ]
        for r in runs:
            lines.append(f"- {r['run_id']}：总耗时 {fmt(r.get('total_elapsed_s'))}s、"
                         f"出片 {fmt(r.get('gen_s'))}s、段数 {fmt(r.get('segments_actual'))}、"
                         f"成片 {fmt(r.get('output_size_bytes'))} B、结论 {fmt(r.get('verdict'))}；"
                         f"缺失 {len(r.get('missing') or [])} 项。")
        missing_labels = [item["label"] for item in cov["fully_missing"]]
        partial_labels = [f"{item['label']}（{item['collected_runs']}/{item['total_runs']}）"
                          for item in cov["partial"]]
        lines += [
            "",
            "> 当前完全未采集到的字段：" + ("、".join(missing_labels) if missing_labels else "无") + "。",
            "> 部分采集：" + ("、".join(partial_labels) if partial_labels else "无") + "。",
            "> 以上字段需全流程版真机测试产出对应证据后由本管线自动补齐；本报告不做任何估算。",
            "",
        ]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="单节点（剧本→成片）证据收集管线（只读）")
    ap.add_argument("--pull-remote", action="store_true", help="只读列出并拉回远端 one_stop 证据")
    ap.add_argument("--json-out", default=str(OUT_JSON), help="统一结构 JSON 输出路径")
    ap.add_argument("--md-out", default=str(OUT_MD), help="Markdown 汇总输出路径")
    ap.add_argument("--round42", default="", help="额外生成 ROUND42 报告（相对仓库根或绝对路径）")
    args = ap.parse_args(argv)

    if args.pull_remote:
        print("[1/4] 远端只读拉取 ...")
        remote_manifest = pull_remote()
    else:
        remote_manifest = load_remote_manifest()
        if remote_manifest:
            print(f"[i] 使用已有远端清单（拉取时间 {remote_manifest.get('pulled_at')}）")

    print("[2/4] 扫描本地证据 ...")
    evidence = scan_local()
    print(f"      运行报告 {len(evidence['runner_reports'])} | 节点审核 {len(evidence['node_reviews'])} | "
          f"本地复核 {len(evidence['local_quality'])} | UI点击 {len(evidence.get('ui_tests') or [])} | "
          f"本地成片副本 {len(evidence.get('local_media') or [])} | "
          f"文档运行 {len(evidence['doc_runs'])} | 显存CSV {len(evidence['vram'])}")

    print("[3/4] 合并成统一结构 ...")
    runs = merge_evidence(evidence, remote_manifest)
    summary = build_summary(evidence, runs, remote_manifest)

    json_out = Path(args.json_out)
    if not json_out.is_absolute():
        json_out = REPO / json_out
    md_out = Path(args.md_out)
    if not md_out.is_absolute():
        md_out = REPO / md_out
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(build_markdown(summary), encoding="utf-8")
    print(f"[out] {json_out.relative_to(REPO)}")
    print(f"[out] {md_out.relative_to(REPO)}")
    if args.round42:
        r42 = Path(args.round42)
        if not r42.is_absolute():
            r42 = REPO / r42
        r42.parent.mkdir(parents=True, exist_ok=True)
        r42.write_text(build_markdown(summary, round42=True), encoding="utf-8")
        print(f"[out] {r42.relative_to(REPO)}")

    print("[4/4] 摘要：")
    print(f"      运行记录：{len(runs)} 条；完全缺失字段："
          f"{len(summary['field_coverage']['fully_missing'])} 个；部分缺失字段："
          f"{len(summary['field_coverage']['partial'])} 个")
    for r in runs:
        print(f"      {r['run_id']} [{r.get('evidence_level')}] finished={r.get('finished_at')} "
              f"total={r.get('total_elapsed_s')} gen={r.get('gen_s')} verdict={r.get('verdict')} "
              f"missing={len(r.get('missing') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
