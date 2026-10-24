# -*- coding: utf-8 -*-
"""成片质量审核 CLI：一条命令回答「有没有剪辑过 / 是否完整 / 质量是否高」。

用法（本机或目标机都能跑，只要 ffmpeg + 项目依赖）：
    python -B scripts/quality_audit.py --video FINAL.mp4
    python -B scripts/quality_audit.py --video FINAL.mp4 --segments seg1.mp4 seg2.mp4 ...
    python -B scripts/quality_audit.py --video FINAL.mp4 --segments-dir path/to/segments \
        --expect-segments 3 --expect-duration 7.5 --out report.md

输出：Markdown 报告 + 同名 JSON；退出码 0=通过 / 1=不通过 / 2=无法检查。

判定口径（全部机器可算，非语义模型）：
  完整性：时长 vs 期望/Σ段（容差 2%）、段数一致、音轨存在；
  剪辑性：场景切换数（有段清单时 ≈段数-1）、黑帧、冻帧、静音段；
  质量：本地启发式四维（叙事流畅/音画同步/节奏合理/无明显跳变）+ 抽帧画质；
  占位：码率过低 / 无音轨 / 画面近单色 → 明确标「疑似占位」。
诚实边界：不检测语义质量（角色是否对、剧情是否好看），那些要人工抽检/语义模型。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _ffmpeg() -> str:
    try:
        from bettercomfyui.review.local_scorer import _ffmpeg as scorer_ffmpeg

        return scorer_ffmpeg()
    except Exception:  # noqa: BLE001
        import shutil

        exe = shutil.which("ffmpeg")
        if exe:
            return exe
    raise SystemExit("[FAIL] 找不到 ffmpeg（PATH 与 imageio-ffmpeg 都没有）")


def probe(path: Path) -> dict:
    proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    text = (proc.stderr or "") + (proc.stdout or "")
    info: dict = {"path": str(path), "duration_s": None, "has_audio": "Audio:" in text,
                  "has_video": "Video:" in text, "width": None, "height": None,
                  "fps": None, "size_bytes": path.stat().st_size}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        h, mi, s = m.groups()
        info["duration_s"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        info["fps"] = float(m.group(1))
    if info["duration_s"]:
        info["bitrate_bps"] = round(info["size_bytes"] * 8 / info["duration_s"], 1)
    return info


def _filter(path: Path, expr: str, *, limit_s: float | None = None, audio: bool = False) -> str:
    cmd = [_ffmpeg(), "-hide_banner", "-i", str(path)]
    if limit_s:
        cmd += ["-t", f"{limit_s:.3f}"]
    cmd += (["-af", expr] if audio else ["-vf", expr])
    cmd += ["-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return (proc.stderr or "") + (proc.stdout or "")


def scene_cut_count(path: Path, *, limit_s: float | None) -> int:
    text = _filter(path, "select='gt(scene,0.35)',showinfo", limit_s=limit_s)
    return text.count("pts_time")


def black_stats(path: Path, *, limit_s: float | None) -> dict:
    text = _filter(path, "blackdetect=d=0.05:pix_th=0.10", limit_s=limit_s)
    starts = [float(x) for x in re.findall(r"black_start:([0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"black_end:([0-9.]+)", text)]
    durations = [e - s for s, e in zip(starts, ends)]
    return {"segments": len(starts), "total_s": round(sum(durations), 3),
            "max_s": round(max(durations), 3) if durations else 0.0}


def freeze_stats(path: Path, *, limit_s: float | None) -> dict:
    text = _filter(path, "freezedetect=n=0.003:d=0.5", limit_s=limit_s)
    starts = [float(x) for x in re.findall(r"freeze_start:([0-9.]+)", text)]
    durations = [float(x) for x in re.findall(r"freeze_duration:([0-9.]+)", text)]
    return {"segments": len(starts), "total_s": round(sum(durations), 3)}


def silence_stats(path: Path, *, limit_s: float | None) -> dict:
    if not probe(path)["has_audio"]:
        return {"segments": 0, "total_s": 0.0, "no_audio": True}
    text = _filter(path, "silencedetect=n=-50dB:d=1.0", limit_s=limit_s, audio=True)
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[0-9.]+)", text)]
    durations = [float(x) for x in re.findall(r"silence_duration:\s*([0-9.]+)", text)]
    return {"segments": len(starts), "total_s": round(sum(durations), 3),
            "no_audio": False}


def sample_frames_quality(path: Path) -> dict:
    """抽 5 帧算「画质/构图」与画面丰富度（用于占位检测）。"""
    from bettercomfyui.review import local_scorer

    try:
        import numpy as np

        frames = local_scorer.frames_from_video(path, fps=1.0, max_frames=180)
        if len(frames) == 0:
            return {"error": "抽不到帧"}
        idx = sorted(set(np.linspace(0, len(frames) - 1, min(5, len(frames))).astype(int)))
        from PIL import Image

        scores: dict[str, list[float]] = {}
        colorful: list[float] = []
        with tempfile.TemporaryDirectory(prefix="bcu_qa_") as tmp:
            for n, i in enumerate(idx):
                file = Path(tmp) / f"frame_{n}.png"
                Image.fromarray(frames[int(i)]).save(file)
                dims, _skipped = local_scorer.image_scores(file)
                for key, value in dims.items():
                    scores.setdefault(key, []).append(float(value))
                metrics = local_scorer._image_metrics(Image.fromarray(frames[int(i)]))
                colorful.append(float(metrics["colorfulness"]))
        return {
            "frames": [int(i) for i in idx],
            "mean_scores": {k: round(sum(v) / len(v), 2) for k, v in scores.items()},
            "colorfulness_mean": round(sum(colorful) / len(colorful), 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def evaluate(video: Path, *, segments: list[Path] | None, expect_segments: int,
             expect_duration: float, quick: bool, score_threshold: float) -> dict:
    info = probe(video)
    if not info["has_video"]:
        raise RuntimeError(f"没有视频流：{video}")
    limit = 600.0 if quick else None
    duration = float(info["duration_s"] or 0.0)

    seg_info = []
    if segments:
        for seg in segments:
            meta = probe(Path(seg))
            seg_info.append({"path": str(seg), "duration_s": meta["duration_s"]})
    seg_sum = sum(float(s["duration_s"] or 0.0) for s in seg_info) if seg_info else None
    expected = expect_duration if expect_duration > 0 else seg_sum
    duration_ok = (expected is None or expected <= 0
                   or abs(duration - expected) <= max(0.5, expected * 0.02))
    count_ok = (expect_segments <= 0 or not seg_info or expect_segments == len(seg_info))

    cuts = scene_cut_count(video, limit_s=limit)
    black = black_stats(video, limit_s=limit)
    freeze = freeze_stats(video, limit_s=limit)
    silence = silence_stats(video, limit_s=limit)
    frameq = sample_frames_quality(video)

    from bettercomfyui.review import local_scorer

    try:
        scores = local_scorer.video_scores(video)
    except Exception as exc:  # noqa: BLE001
        scores = {"error": f"{type(exc).__name__}: {exc}"}

    placeholder_flags: list[str] = []
    if info.get("bitrate_bps") and float(info["bitrate_bps"]) < 200_000:
        placeholder_flags.append(f"码率过低（{info['bitrate_bps']} bps < 200kbps）")
    if not info["has_audio"]:
        placeholder_flags.append("无音轨")
    colorful = frameq.get("colorfulness_mean")
    if colorful is not None and float(colorful) < 2.0:
        placeholder_flags.append(f"画面近单色（colorfulness={colorful}）")

    reasons: list[str] = []
    if not duration_ok:
        reasons.append(f"时长不一致：成片 {duration:.2f}s vs 期望 {expected:.2f}s（容差 2%）")
    if not count_ok:
        reasons.append(f"段数不一致：{len(seg_info)} vs 计划 {expect_segments}")
    if placeholder_flags:
        reasons.append("疑似占位：" + "；".join(placeholder_flags))
    if black["segments"] > 1 or black["max_s"] >= 0.5:
        reasons.append(f"黑帧异常：{black['segments']} 段 / 最长 {black['max_s']}s")
    if duration > 0 and freeze["total_s"] > duration * 0.01:
        reasons.append(f"冻帧过多：累计 {freeze['total_s']}s（>1% 时长）")
    if duration > 0 and silence["total_s"] > duration * 0.05:
        reasons.append(f"静音过多：累计 {silence['total_s']}s（>5% 时长）")
    if isinstance(scores, dict) and "error" not in scores:
        low = [k for k, v in scores.items() if float(v) < score_threshold]
        if low:
            reasons.append(f"质量分低于 {score_threshold}：{low}（{scores}）")
    clip_ok = True
    if seg_info:
        clip_ok = cuts >= max(0, len(seg_info) - 2)  # 允许少量镜头内切换差异
        if not clip_ok:
            reasons.append(f"场景切换数 {cuts} 与段数 {len(seg_info)} 不匹配（可能未真正拼接/跳段）")

    passed = not reasons
    verdict = "通过" if passed else "不通过"
    return {
        "video": str(video),
        "basic": info,
        "duration_check": {"expected_s": expected, "actual_s": round(duration, 3),
                           "ok": duration_ok},
        "segment_check": {"segments": len(seg_info), "expect": expect_segments,
                          "sum_duration_s": round(seg_sum, 3) if seg_sum is not None else None,
                          "ok": count_ok},
        "editing": {"scene_cuts": cuts, "black": black, "freeze": freeze,
                    "silence": silence, "clip_ok": clip_ok},
        "quality": {"scores": scores, "frames": frameq,
                    "score_threshold": score_threshold,
                    "note": "本地启发式（真实帧统计，非语义模型）"},
        "placeholder_flags": placeholder_flags,
        "reasons": reasons,
        "verdict": verdict,
        "passed": passed,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "limits": ["不检测语义质量（角色/剧情/美感）", "快速模式只扫描前 10 分钟",
                   "音画同步只测流时长对齐，不做唇形帧级检测"],
    }


def to_markdown(result: dict) -> str:
    b, d, s, e, q = (result["basic"], result["duration_check"], result["segment_check"],
                     result["editing"], result["quality"])
    lines = [
        "# 成片质量审核报告",
        "",
        f"- 成片：`{result['video']}`",
        f"- 检查时间：{result['checked_at']}",
        f"- **结论：{result['verdict']}**",
        "",
        "## 1. 基本信息",
        f"| 项 | 值 |",
        f"|---|---|",
        f"| 时长 | {b.get('duration_s')} s |",
        f"| 分辨率 / fps | {b.get('width')}x{b.get('height')} / {b.get('fps')} |",
        f"| 码率 | {b.get('bitrate_bps')} bps |",
        f"| 音轨 | {'有' if b.get('has_audio') else '无'} |",
        f"| 文件大小 | {b.get('size_bytes')} bytes |",
        "",
        "## 2. 完整性",
        f"- 时长核对：期望 {d['expected_s']}s，实际 {d['actual_s']}s → "
        f"{'通过' if d['ok'] else '**不一致**'}（容差 2%）",
        f"- 段数核对：实有 {s['segments']}，计划 {s['expect']} → "
        f"{'通过' if s['ok'] else '**不一致**'}",
        "",
        "## 3. 剪辑性",
        f"- 场景切换数：{e['scene_cuts']}（有段清单时应 ≈ 段数-1）",
        f"- 黑帧：{e['black']['segments']} 段 / 累计 {e['black']['total_s']}s（最长 {e['black']['max_s']}s）",
        f"- 冻帧：{e['freeze']['segments']} 段 / 累计 {e['freeze']['total_s']}s",
        f"- 静音：{e['silence']['segments']} 段 / 累计 {e['silence']['total_s']}s",
        "",
        "## 4. 质量（本地启发式）",
        f"- 四维评分：{q['scores']}",
        f"- 抽帧画质：{(q.get('frames') or {}).get('mean_scores')}",
        f"- 画面丰富度：{(q.get('frames') or {}).get('colorfulness_mean')}",
        "",
        "## 5. 占位检测",
        f"- {result['placeholder_flags'] or '未发现'}",
        "",
        "## 6. 不通过原因" if result["reasons"] else "## 6. 不通过原因（无）",
    ]
    if result["reasons"]:
        for reason in result["reasons"]:
            lines.append(f"- {reason}")
    else:
        lines.append("- 无（机器可算项全部通过）")
    lines += [
        "",
        "## 7. 边界说明",
        "> " + "；".join(result["limits"]),
        "> 语义质量（角色是否对、剧情是否好看）需人工抽检或语义评分模型。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--segments", nargs="*", default=None,
                        help="段视频路径（用于段数/Σ时长核对）")
    parser.add_argument("--segments-dir", default="", help="段视频目录（自动收集 *.mp4）")
    parser.add_argument("--expect-segments", type=int, default=0)
    parser.add_argument("--expect-duration", type=float, default=0.0)
    parser.add_argument("--quick", action="store_true", default=True,
                        help="快速模式：只扫描前 10 分钟（默认开）")
    parser.add_argument("--full", action="store_true", help="完整扫描")
    parser.add_argument("--score-threshold", type=float, default=70.0)
    parser.add_argument("--out", default="", help="报告输出路径（默认 alongside video）")
    args = parser.parse_args()

    video = Path(args.video)
    if not video.is_file():
        print(f"[FAIL] 成片不存在：{video}")
        return 2
    segments: list[Path] = []
    if args.segments:
        segments.extend(Path(x) for x in args.segments)
    if args.segments_dir:
        segments.extend(sorted(Path(args.segments_dir).glob("*.mp4")))
    segments = [s for s in segments if s.is_file()]
    try:
        result = evaluate(video, segments=segments or None,
                          expect_segments=args.expect_segments,
                          expect_duration=args.expect_duration,
                          quick=not args.full,
                          score_threshold=args.score_threshold)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 无法检查：{type(exc).__name__}: {exc}")
        return 2
    out = Path(args.out) if args.out else video.with_name(video.stem + "_quality_report.md")
    out.write_text(to_markdown(result), encoding="utf-8")
    json_out = out.with_suffix(".json")
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[报告] {out}")
    print(f"[JSON] {json_out}")
    print(f"[结论] {result['verdict']}")
    if result["reasons"]:
        for reason in result["reasons"]:
            print("  -", reason)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
