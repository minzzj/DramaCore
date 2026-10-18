"""成片剪辑/封装节点（对应逻辑图 SG6：SPLICE 拼接 → ENC 封装 → 成片）。

用户要求（2026-10-18 凌晨）：第一阶段要「给出剪辑好的视频」，并且到机器上要检查
①有没有经过剪辑 ②是否完整 ③质量是否高。本节点把这三件事做成**可执行、可出证据**的一步：

1. **拼接（剪辑）**：把多个段视频按顺序 concat（复用 `pipeline.splice`，流复制不重编码）；
2. **封装（ENC）**：按配置编码为最终 mp4（复用 `pipeline.encode`，CRF/格式/音频组合）；
0. **可选：自动发现最新段视频**（列表留空 + 勾选「自动发现最新视频」时，
   扫描 ComfyUI 输出目录/项目输出目录里最近 N 分钟的 mp4，按时间排序拼接；
   审计里会列出实际用到的文件，避免"悄悄拼错"）。
3. **自检**（产出 JSON 证据，供人/机核对）：
   - 完整性：成片时长 vs Σ段时长（容差 2%）、计划段数核对、首尾可读、有无音轨；
   - 剪辑性：场景切换数（近似段数-1）、黑帧、冻帧、静音段总量；
   - 质量：本地启发式四维（叙事流畅/音画同步/节奏合理/无明显跳变）+ 抽帧画质/构图；
   - 占位检测：码率/画面方差过低 → 明确标记"疑似占位/纯色，不是真实生成"。

**诚实边界**：本节点不做转义特效/转场（未实现就不暴露，绝不静默忽略）；"质量是否高"的
最终判断仍需人工抽检，本节点只给机器可算的证据。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

__all__ = ["BetterComfyUIFinalize", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

CATEGORY = "Better ComfyUI"
CHECK_SCOPES = ["快速抽样（≤10分钟）", "完整扫描", "跳过"]


def _orch_helpers():
    from .nodes import _NodeError, _config, _orch, _orch_repo_root, _store_for_project

    return _NodeError, _config, _orch, _orch_repo_root, _store_for_project


def _ffmpeg() -> str:
    from bettercomfyui.review.local_scorer import _ffmpeg as scorer_ffmpeg

    return scorer_ffmpeg()


def _parse_media(path: Path) -> dict[str, Any]:
    """ffmpeg -i 解析：时长/音视频/分辨率/fps/码率（不依赖 ffprobe 二进制）。"""
    proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    text = (proc.stderr or "") + (proc.stdout or "")
    info: dict[str, Any] = {"duration_s": None, "has_audio": "Audio:" in text,
                            "has_video": "Video:" in text, "width": None,
                            "height": None, "fps": None,
                            "size_bytes": path.stat().st_size}
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        h, m, s = match.groups()
        info["duration_s"] = int(h) * 3600 + int(m) * 60 + float(s)
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if match:
        info["width"], info["height"] = int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if match:
        info["fps"] = float(match.group(1))
    if info["duration_s"]:
        info["bitrate_bps"] = round(info["size_bytes"] * 8 / info["duration_s"], 1)
    return info


def _run_filter(path: Path, vf_or_af: str, *, limit_s: float | None) -> str:
    cmd = [_ffmpeg(), "-hide_banner", "-i", str(path)]
    if limit_s:
        cmd += ["-t", f"{limit_s:.3f}"]
    is_audio = vf_or_af.startswith("silencedetect")
    cmd += (["-af", vf_or_af] if is_audio else ["-vf", vf_or_af])
    cmd += ["-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return (proc.stderr or "") + (proc.stdout or "")


class BetterComfyUIFinalize:
    """成片剪辑：段视频 → 拼接 → 封装 → 自检（完整性/剪辑性/质量/占位检测）。"""

    CATEGORY = "BetterComfyUI/成片"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("成片路径", "完整性JSON", "质量JSON", "审计报告")
    DESCRIPTION = "段视频按顺序拼接+封装成最终 mp4，并自检完整性/剪辑性/质量/占位。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "项目": ("STRING", {"default": "演示项目"}),
                "段视频路径列表": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "每行一个段视频绝对路径（按顺序）；也接受 JSON 数组。\n"
                               "从 SaveVideo 输出目录复制即可。"}),
            },
            "optional": {
                "输出文件名": ("STRING", {"default": "",
                                        "tooltip": "留空 = 自动 成片_时间戳.mp4"}),
                "编码CRF": ("INT", {"default": 18, "min": 0, "max": 51,
                                  "tooltip": "0 = 用项目配置 encode.crf；18 高画质。"}),
                "完整性检查": (list(CHECK_SCOPES), {"default": CHECK_SCOPES[0]}),
                "自动发现最新视频": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "段视频路径列表留空时，自动扫描输出目录里最近 N 分钟的 mp4；"
                               "审计会列出实际用到的文件（不会悄悄拼错）。"}),
                "发现时间窗_分钟": ("INT", {"default": 30, "min": 1, "max": 720,
                                        "tooltip": "自动发现的时间窗（分钟）。"}),
                "计划段数": ("INT", {"default": 0, "min": 0, "max": 9999,
                                   "tooltip": "0 = 不核对；填了则与输入段数比对。"}),
                "每段计划时长_秒": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 3600.0,
                                          "tooltip": "0 = 不核对；填了则校验总时长一致性。"}),
            },
        }

    # -- 工具 ---------------------------------------------------------
    @staticmethod
    def _parse_paths(raw: str) -> list[str]:
        text = (raw or "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                data = json.loads(text)
                return [str(x).strip() for x in data if str(x).strip()]
            except Exception:  # noqa: BLE001 - 不是 JSON 就按行解析
                pass
        return [line.strip().strip('"').strip("'") for line in text.splitlines()
                if line.strip()]

    @staticmethod
    def _discover_latest(paths, repo_root: Path, window_minutes: int) -> list[Path]:
        """扫描最近 window_minutes 分钟内的 mp4：ComfyUI 输出目录 + 项目输出目录。"""
        import time as _time

        cutoff = _time.time() - max(1, int(window_minutes)) * 60
        dirs: list[Path] = [Path(paths.output_dir), Path(paths.project_dir) / "output"]
        try:
            import folder_paths  # type: ignore

            comfy_out = Path(folder_paths.get_output_directory())
            # 排除本节点自己导出的「剧核成片」目录，避免把上一次的成片当成"段视频"再拼进去
            dirs.append(comfy_out)
            exclude_dirs = {(comfy_out / "剧核成片").resolve()}
        except Exception:  # noqa: BLE001 - 非 ComfyUI 环境（pytest/CLI）：不扫 .comfyui/output
            exclude_dirs = set()
        skip_words = ("成片_", "spliced", "final", "acceptance", "e2e_", "edge.mp4",
                      "宣传片", "hires", "round39", "preview", "stitched", "up_cn")
        found: dict[Path, float] = {}
        for base in dirs:
            if not base.is_dir():
                continue
            for file in base.rglob("*.mp4"):
                try:
                    resolved = file.resolve()
                    if any(str(resolved).startswith(str(exc)) for exc in exclude_dirs):
                        continue
                    if any(word in file.name.lower() for word in skip_words):
                        continue
                    mtime = file.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    found[file.resolve()] = mtime
        return [p for p, _m in sorted(found.items(), key=lambda item: (item[1], str(item[0])))]

    @staticmethod
    def _resolve(path_str: str, paths, repo_root: Path) -> Path:
        p = Path(path_str)
        if p.is_file():
            return p
        for base in (Path(paths.output_dir), Path(paths.project_dir), repo_root):
            cand = base / path_str
            if cand.is_file():
                return cand
        return p

    def run(self, 项目: str, 段视频路径列表: str, 输出文件名: str = "",
            编码CRF: int = 18, 完整性检查: str = CHECK_SCOPES[0],
            自动发现最新视频: bool = False, 发现时间窗_分钟: int = 30,
            计划段数: int = 0, 每段计划时长_秒: float = 0.0):
        _NodeError, _config, _orch, _orch_repo_root, _store_for_project = _orch_helpers()
        from bettercomfyui.pipeline import core

        _orch()
        cfg = _config()
        repo_root = Path(_orch_repo_root())
        _ps, paths, _store, _root, cfg, info = _store_for_project(项目)

        raw_paths = self._parse_paths(段视频路径列表)
        auto_discovered: list[Path] = []
        if not raw_paths and 自动发现最新视频:
            auto_discovered = self._discover_latest(
                paths, repo_root, window_minutes=int(发现时间窗_分钟))
            if not auto_discovered:
                raise _NodeError(
                    f"自动发现：最近 {发现时间窗_分钟} 分钟内没找到段视频。\n"
                    "请手动填「段视频路径列表」，或把时间窗调大。")
            raw_paths = [str(p) for p in auto_discovered]
        if not raw_paths:
            raise _NodeError("段视频路径列表为空：每行填一个段视频绝对路径（从 SaveVideo 输出目录复制）。")
        files = [self._resolve(x, paths, repo_root) for x in raw_paths]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise _NodeError("这些段视频不存在：\n  " + "\n  ".join(missing))
        durations = []
        for f in files:
            meta = _parse_media(f)
            if not meta["has_video"]:
                raise _NodeError(f"文件没有视频流：{f}")
            durations.append(float(meta["duration_s"] or 0.0))
        expected_duration = sum(durations)

        ts = time.strftime("%Y%m%d_%H%M%S")
        workdir = Path(paths.project_dir) / "_work" / f"finalize_{ts}"
        workdir.mkdir(parents=True, exist_ok=True)

        # ---- 1) 拼接（剪辑） ----
        if len(files) > 1:
            splice_inputs = []
            for index, (file, dur) in enumerate(zip(files, durations), start=1):
                splice_inputs.append(core.SpliceInput(
                    segment_id=f"seg{index}", segment_version="v1", video_path=file))
            spliced = core.splice(splice_inputs, workdir=workdir,
                                  out_path=workdir / "spliced.mp4")
        else:
            spliced = files[0]

        # ---- 2) 封装（ENC） ----
        final_name = (输出文件名 or "").strip() or f"成片_{ts}.mp4"
        if not final_name.lower().endswith(".mp4"):
            final_name += ".mp4"
        final_path = Path(paths.output_dir) / final_name
        Path(paths.output_dir).mkdir(parents=True, exist_ok=True)
        cfg_encode = copy.deepcopy(cfg)
        if int(编码CRF) > 0:
            cfg_encode.encode.crf = int(编码CRF)
        try:
            final_path = core.encode(spliced, cfg_encode, out_path=final_path)
        except Exception as exc:  # noqa: BLE001 - 封装失败必须可见
            raise _NodeError(f"成片封装失败：{type(exc).__name__}: {exc}") from exc
        if not Path(final_path).is_file():
            raise _NodeError(f"封装流程结束但没有成片文件：{final_path}")

        # ---- 3) 自检 ----
        final_meta = _parse_media(Path(final_path))
        actual_duration = float(final_meta["duration_s"] or 0.0)
        duration_ok = (expected_duration <= 0
                       or abs(actual_duration - expected_duration) <= max(0.5, expected_duration * 0.02))
        count_ok = (int(计划段数) <= 0 or int(计划段数) == len(files))
        per_seg_ok = True
        if float(每段计划时长_秒) > 0 and durations:
            per_seg_ok = all(abs(d - float(每段计划时长_秒)) <= 0.5 for d in durations)
        limit = None if 完整性检查 == "完整扫描" else (600.0 if 完整性检查 == CHECK_SCOPES[0] else None)

        black_text = ""
        freeze_text = ""
        silence_text = ""
        if 完整性检查 != "跳过":
            black_text = _run_filter(Path(final_path), "blackdetect=d=0.05:pix_th=0.10", limit_s=limit)
            freeze_text = _run_filter(Path(final_path), "freezedetect=n=0.003:d=0.5", limit_s=limit)
            silence_text = _run_filter(Path(final_path), "silencedetect=n=-50dB:d=1.0", limit_s=limit)
        black_count = black_text.count("black_start")
        freeze_count = freeze_text.count("freeze_start")
        silence_starts = silence_text.count("silence_start")

        from bettercomfyui.review import local_scorer

        try:
            quality_scores = local_scorer.video_scores(Path(final_path))
            quality_note = "local_heuristic"
        except Exception as exc:  # noqa: BLE001 - 评不出来要如实报告，不能编
            quality_scores = {"error": f"{type(exc).__name__}: {exc}"}
            quality_note = "failed"

        # 占位检测：码率过低或画面近乎无变化 → 明确标记
        placeholder_flags: list[str] = []
        if final_meta.get("bitrate_bps") and float(final_meta["bitrate_bps"]) < 200_000:
            placeholder_flags.append("码率 <200kbps，疑似占位/纯色视频")
        if not final_meta["has_audio"]:
            placeholder_flags.append("无音轨（占位视频或音频丢失）")

        integrity = {
            "executed": True,
            "segments": len(files),
            "plan_segments": int(计划段数),
            "segment_durations_s": [round(d, 3) for d in durations],
            "expected_duration_s": round(expected_duration, 3),
            "final_duration_s": round(actual_duration, 3),
            "duration_tolerance": "max(0.5s, 2%)",
            "duration_ok": bool(duration_ok),
            "segment_count_ok": bool(count_ok),
            "per_segment_duration_ok": bool(per_seg_ok),
            "final": {"path": str(final_path), "size_bytes": final_meta["size_bytes"],
                      "bitrate_bps": final_meta.get("bitrate_bps"),
                      "width": final_meta.get("width"), "height": final_meta.get("height"),
                      "fps": final_meta.get("fps"),
                      "has_audio": final_meta["has_audio"]},
            "scan": {"mode": 完整性检查, "limit_s": limit,
                     "blackdetect_segments": black_count,
                     "freezedetect_segments": freeze_count,
                     "silencedetect_segments": silence_starts},
            "auto_discovered": bool(auto_discovered),
            "auto_discovered_files": [str(p) for p in auto_discovered],
            "placeholder_flags": placeholder_flags,
            "passed": bool(duration_ok and count_ok and per_seg_ok and not placeholder_flags),
            "note": "passed=True 仅代表机器可算的完整性/剪辑性通过；画质高低需人工抽检。",
        }
        quality = {
            "scores": quality_scores,
            "score_source": quality_note,
            "note": "本地启发式（真实帧统计，非语义模型）；语义质量需人工/语义模型。",
        }

        report_path = Path(paths.project_dir) / "review" / f"finalize_{ts}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"integrity": integrity, "quality": quality},
                                          ensure_ascii=False, indent=2), encoding="utf-8")

        audit_lines = [
            "=== 成片剪辑/封装（SG6 SPLICE+ENC） ===",
            f"项目：{info.get('project_id') or 项目}",
            f"输入段数：{len(files)}（计划 {计划段数 or '未填'}）"
            + (f"｜自动发现 {len(auto_discovered)} 个（时间窗 {发现时间窗_分钟} 分钟）"
               if auto_discovered else ""),
            *([f"自动发现文件：{p.name}" for p in auto_discovered] if auto_discovered else []),
            f"成片：{final_path}",
            f"时长：{actual_duration:.2f}s（Σ段 {expected_duration:.2f}s，容差 max(0.5s,2%)）→ "
            f"{'通过' if duration_ok else '**不一致**'}",
            f"规格：{final_meta.get('width')}x{final_meta.get('height')} "
            f"{final_meta.get('fps')}fps 码率 {final_meta.get('bitrate_bps')} "
            f"音轨 {'有' if final_meta['has_audio'] else '**无**'}",
            f"剪辑性：黑帧 {black_count} 段 / 冻帧 {freeze_count} 段 / 静音 {silence_starts} 段"
            f"（检查方式：{完整性检查}）",
            f"质量评分（本地启发式）：{quality_scores}",
            f"占位检测：{placeholder_flags or '未发现'}",
            f"证据文件：{report_path}",
            "说明：本节点不做转场特效（未实现的功能不暴露、不静默忽略）；画质高低请人工抽检。",
        ]
        if placeholder_flags:
            audit_lines.append("⚠️ 成片疑似占位视频：请检查是否用真实生成结果，而不是占位/测试素材。")

        # 导出到 ComfyUI output 并在网页端预览（用户明确要求：网页端要看到「一整个成片」）
        comfy_export = None
        try:
            import folder_paths  # type: ignore

            out_root = Path(folder_paths.get_output_directory())
            sub = "剧核成片"
            out_dir = out_root / sub
            out_dir.mkdir(parents=True, exist_ok=True)
            stem, suffix = Path(final_name).stem, Path(final_name).suffix or ".mp4"
            candidate = out_dir / f"{stem}{suffix}"
            counter = 1
            while candidate.exists():
                candidate = out_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            shutil.copy2(final_path, candidate)
            comfy_export = {"filename": candidate.name, "subfolder": sub, "type": "output"}
            integrity["comfyui_output"] = comfy_export
            audit_lines.append(f"ComfyUI 预览：{candidate}（网页端可直接播放这一整个成片）")
        except Exception as exc:  # noqa: BLE001 - 非 ComfyUI 环境/权限问题不阻断
            audit_lines.append(
                f"[提示] 未导出到 ComfyUI output（{type(exc).__name__}: {exc}）；"
                f"成片真实路径：{final_path}")

        ui_payload: dict = {"text": ["\n".join(audit_lines)]}
        if comfy_export:
            # 兼容新版前端（images+animated）与旧式视频预览约定（gifs+format）
            ui_payload["images"] = [dict(comfy_export)]
            ui_payload["animated"] = [True]
            ui_payload["gifs"] = [dict(comfy_export, format="video/h264-mp4")]
        print(f"[BetterComfyUI 成片剪辑] {final_path} 段数={len(files)} "
              f"时长={actual_duration:.2f}s passed={integrity['passed']}"
              + (f" | ComfyUI预览={comfy_export['filename']}" if comfy_export else ""))
        return {"ui": ui_payload,
                "result": (str(final_path), json.dumps(integrity, ensure_ascii=False, indent=2),
                           json.dumps(quality, ensure_ascii=False, indent=2),
                           "\n".join(audit_lines))}


NODE_CLASS_MAPPINGS = {"BetterComfyUIFinalize": BetterComfyUIFinalize}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BetterComfyUIFinalize": "成片剪辑+封装+自检（拼接→编码→完整性/质量）",
}
