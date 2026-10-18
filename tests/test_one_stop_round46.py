# -*- coding: utf-8 -*-
"""ROUND46 单节点功能补齐：本地单元测试（不依赖 ComfyUI/GPU/远端）。"""
from __future__ import annotations

import pytest

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui_comfy import director_edit as de
from bettercomfyui_comfy import director_longform as lf


class TestLongform(unittest.TestCase):
    def test_plan_batches(self):
        self.assertEqual(lf.plan_batches(10, 8), [(0, 8), (8, 10)])
        self.assertEqual(lf.plan_batches(3, 8), [(0, 3)])
        self.assertEqual(lf.plan_batches(1, 8), [(0, 1)])
        self.assertEqual(len(lf.plan_batches(360, 8)), 45)

    def test_plan_segment_count_one_hour(self):
        plan = lf.plan_segment_count(requested_segments=4, target_minutes=60.0,
                                     segment_seconds=10.0, max_segments=360)
        self.assertEqual(plan["auto_segments_from_target"], 360)
        self.assertEqual(plan["chosen_segments"], 360)
        self.assertEqual(plan["planned_minutes"], 60.0)
        cap = lf.plan_segment_count(requested_segments=500, target_minutes=0.0,
                                    segment_seconds=10.0, max_segments=360)
        self.assertEqual(cap["chosen_segments"], 360)
        self.assertTrue(cap["capped_by_max_segments"])

    def test_checkpoint_roundtrip_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            fp = lf.parameter_fingerprint({"a": 1, "b": [2, 3]})
            self.assertNotEqual(fp, lf.parameter_fingerprint({"a": 2}))
            note = lf.load_checkpoint(proj, fp)
            self.assertIn("无检查点", note["note"])
            state = {"batches": {"0": {"seg_files": []}}}
            path = lf.save_checkpoint(proj, fp, state)
            self.assertTrue(path.is_file())
            loaded = lf.load_checkpoint(proj, fp)
            self.assertEqual(loaded["fingerprint"], fp)
            self.assertIn("0", loaded["batches"])
            stale = lf.load_checkpoint(proj, "other")
            self.assertIn("指纹变化", stale["note"])
            self.assertEqual(lf.reuse_batch_files({"seg_files": ["/no/such.mp4"]}), None)
            self.assertEqual(lf.reuse_batch_files({}), None)

    def test_segment_manifest_fields(self):
        specs = [{"start": 0, "frames": 124, "duration_sec": 5.167, "task_type": "r2v",
                  "prompt": "p1", "asset_refs": [{"asset_id": "a", "version": "v1"}]},
                 {"start": 124, "frames": 124, "duration_sec": 5.167, "task_type": "r2v",
                  "prompt": "p2"}]
        rows = lf.build_segment_manifest(
            project_id="bad-id", segment_specs=specs,
            seg_files=["/tmp/seg1.mp4", "/tmp/seg2.mp4"],
            segment_reviews=[{"score": 88, "passed": True}],
            cnt2=[1, 0], cnt3=[0, 1],
            fallback_details=[{"segment_index": 1, "note": "fallback"}])
        self.assertEqual(len(rows), 2)
        self.assertIn("CH1_SEG1", rows[0]["segment_id"])
        self.assertEqual(rows[0]["video_path"], "/tmp/seg1.mp4")
        self.assertEqual(rows[0]["cnt2"], 1)
        self.assertEqual(rows[1]["cnt3"], 1)
        self.assertEqual(rows[0]["fallback"]["note"], "fallback")
        self.assertTrue(rows[0]["id_note"])  # 非法 project_id 有 note，不丢清单

    def test_extract_last_frame_missing(self):
        with self.assertRaises(FileNotFoundError):
            lf.extract_last_frame("/no/such/video.mp4", "/tmp/tail.png")


class TestEdit(unittest.TestCase):
    def test_resolve_format(self):
        self.assertEqual(de.resolve_format("mp4（H.264）"), "MP4")
        self.assertEqual(de.resolve_format("mov（H.264）"), "MOV")
        self.assertEqual(de.resolve_format("webm（VP9/Opus）"), "WebM")
        with self.assertRaises(ValueError):
            de.resolve_format("avi")

    def test_subtitle_srt(self):
        entries = de.build_subtitle_entries(["第一段很好", "第二段也不错"], segment_seconds=5.0)
        self.assertEqual(len(entries), 2)
        with tempfile.TemporaryDirectory() as tmp:
            p = de.write_srt(entries, Path(tmp) / "s.srt")
            text = p.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:05,000", text)
            self.assertIn("00:00:05,000 --> 00:00:10,000", text)
            self.assertIn("第一段很好", text)

    def test_watermark_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                p = de.write_watermark_png("剧核 DramaCore", Path(tmp) / "wm.png")
            except RuntimeError as exc:
                self.skipTest(f"Pillow 不可用：{exc}")
            self.assertTrue(p.is_file())
            self.assertGreater(p.stat().st_size, 100)


class TestNodeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop
        cls.cls = BetterComfyUIOneStop
        cls.info = BetterComfyUIOneStop.INPUT_TYPES()

    def test_params_present(self):
        req = self.info["required"]
        for key in ("目标总时长_分钟", "每批段数", "断点续跑", "批次间卸载模型",
                    "显存档位", "剪枝模型", "tiled采样", "自动返工", "CNT2上限",
                    "级联返工", "CNT3上限", "水印文字"):
            self.assertIn(key, req)
        self.assertGreaterEqual(int(req["段数"][1]["max"]), 360)

    def test_sg6_no_longer_unimplemented(self):
        req = self.info["required"]
        self.assertIn("淡入淡出", req["转场"][0])
        self.assertIn("自动字幕", req["字幕"][0])
        self.assertIn("文字水印", req["水印"][0])
        self.assertTrue(any("webm" in str(x) for x in req["输出格式"][0]))

    def test_run_signature_covers_inputs(self):
        sig = inspect.signature(self.cls.run)
        missing = [k for k in list(self.info["required"]) + list(self.info.get("optional", {}))
                   if k not in sig.parameters]
        self.assertEqual(missing, [])

    def test_i2v_no_redundant_pop(self):
        src = (ROOT / "bettercomfyui_comfy" / "one_stop_node.py").read_text(encoding="utf-8")
        self.assertNotIn('seg.pop("genImage", None)', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# ===========================================================================
# ROUND46 追加：SG6 端到端（本地 ffmpeg 合成段）+ 批次/返工编排冒烟
# ===========================================================================
import subprocess


class TestSg6Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        cls.ffmpeg = _ffmpeg_exe()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.segs = []
        for i, freq in ((1, 440), (2, 660)):
            p = cls.dir / f"seg_{i}.mp4"
            cmd = [cls.ffmpeg, "-y", "-v", "error",
                   "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=24:duration=2",
                   "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=2",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-shortest", str(p)]
            subprocess.run(cmd, check=True, capture_output=True)
            cls.segs.append(p)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_crossfade_splice(self):
        out = de.splice_segments(self.segs, workdir=self.dir / "fade", fade_seconds=0.5,
                                 warnings=[])
        self.assertTrue(out.is_file())
        info = de.probe_media(out)
        self.assertAlmostEqual(float(info["duration_s"]), 3.5, delta=0.25)

    def test_finish_movie_mov_subtitle_watermark_cover(self):
        proj = self.dir / "project"
        proj.mkdir(exist_ok=True)
        specs = [{"prompt": "第一段：雪夜客栈", "duration_sec": 2.0},
                 {"prompt": "第二段：灯影摇曳", "duration_sec": 2.0}]
        manifest = lf.build_segment_manifest(
            project_id="", segment_specs=specs,
            seg_files=[str(p) for p in self.segs])
        options = {
            "format_label": "mov（H.264）", "crf": 24,
            "transition_label": "淡入淡出", "subtitle_label": "自动字幕",
            "watermark_label": "文字水印", "cover_label": "第一帧",
            "watermark_text": "剧核 DramaCore", "fade_seconds": 0.5,
            "segment_seconds": 2.0, "prompts": [s["prompt"] for s in specs],
        }
        audit, warnings = [], []
        out = de.finish_movie(
            seg_files=self.segs, work_dir=self.dir / "edit", project_dir=proj,
            project_id="", project_display_name="单测项目",
            output_filename="成片", options=options, manifest=manifest,
            metadata_extra={"script_id": "s1", "workflow_version": "test"},
            provenance={"is_mock": True, "executor": "unit-test",
                        "reason": "synthetic ffmpeg", "unverified": []},
            use_finalize=False, audit=audit, warnings=warnings)
        final = Path(out["final_path"])
        self.assertTrue(final.is_file(), f"成片不存在：{final}")
        self.assertEqual(final.suffix, ".mov")
        self.assertTrue(Path(out["output"]["metadata_path"]).is_file())
        self.assertIsNotNone(out["output"]["cover_path"])
        self.assertTrue(Path(out["output"]["cover_path"]).is_file())
        meta = de.probe_media(final)
        self.assertGreater(float(meta["duration_s"]), 2.5)
        self.assertTrue(any("字幕" in s for s in out["steps"]))
        self.assertTrue(any("水印" in s for s in out["steps"]))


class TestOrchestrationSmoke(unittest.TestCase):
    """不占 GPU：桩掉导演台/编码/尾帧，验证分批续跑与返工编排的运行时契约。"""

    def _node(self):
        from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop

        return BetterComfyUIOneStop()

    def _ctx(self, tmp, n=3):
        specs = [{"prompt": f"p{i + 1}", "duration_sec": 2.0, "frames": 48,
                  "start": i * 48, "task_type": "r2v", "refs": []} for i in range(n)]
        return {
            "seg_specs": specs, "task_type": "r2v", "task_key": "r2v",
            "global_prompt": "p1", "prompts": [s["prompt"] for s in specs],
            "out_cfg": {"mode": "fixed", "exportMode": "segments"},
            "negative": "低质量",
            "时间轴JSON": "", "project_dir": tmp, "project_id": "p",
            "项目": "冒烟", "seg_dir": tmp / "segs", "audit": [], "warnings": [],
            "timings": {}, "packs": {"semantic_bridge": None, "selflift": None,
                                     "refine": None, "face_refine": None},
            "models": None, "tail_ref": None, "reports": [], "batch_rows": [],
            "视频UNET": "u", "CLIP名": "c", "视频VAE": "v", "音频VAE": "a",
            "视频LoRA": "", "LoRA强度": 0.0, "宽度": 320, "高度": 180,
            "参考最长边": 320, "帧率": 24.0, "每段时长_秒": 2.0,
            "步数": 1, "采样器": "euler", "调度器": "simple", "cfg": 1.0,
            "seed": 0, "噪声偏移_视频": 12.0, "噪声偏移_音频": 3.0,
            "显存清理_段间": False, "显存清理_二采前": False,
            "显存清理_修脸前": False, "缓存编码": "raw", "缓存目录名": "",
            "导出原始帧": False, "输出修脸前": False, "每批段数": 2,
            "断点续跑": True, "批次间卸载模型": False, "显存档位": "自动",
            "剪枝模型": False, "段间连续": True, "连续帧数": 22,
            "连续模式": "guide", "每段引用上段": True, "自动返工": True,
            "CNT2上限": 1, "级联返工": True, "CNT3上限": 1,
            "段阈值覆盖": -1.0, "边界相似度阈值": 55.0,
        }

    def test_generate_batches_and_resume(self):
        pytest.importorskip("torch", reason="需要 .comfyenv（ComfyUI venv）里的 torch")
        import torch

        from bettercomfyui_comfy import one_stop_node as mod

        node = self._node()
        tmp = Path(tempfile.mkdtemp())
        calls = {"exec": 0}

        def fake_exec(ctx, tl, *, unique_id, seed_value):
            from bettercomfyui_comfy import director_runtime as dr

            calls["exec"] += 1
            n = max(1, int(dr.timeline_summary(tl).get("segmentCount") or 1))
            feats = [torch.full((24, 8, 8, 3), 0.5) for _ in range(n)]
            audio = {"waveform": torch.zeros(1, 2400), "sample_rate": 24000}
            report = ("Segment continuity: ON (guide motion context 22f, keep full)\n"
                      "  Pin from prev: #2")
            return feats, audio, 24.0, 24 * n, None, report, None, None

        node._execute_timeline = fake_exec  # type: ignore[method-assign]
        node._load_video_models = lambda *a, **k: (None, None, None, None)  # type: ignore
        old_encode = mod._encode_movie

        def fake_encode(frames, audio, path, fps):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"fake-mp4")
            return {"path": str(path), "frames": 24, "width": 8, "height": 8,
                    "fps": fps, "has_audio": True, "duration_s": 1.0}

        old_extract = lf.extract_last_frame
        mod._encode_movie = fake_encode
        lf.extract_last_frame = lambda v, o: Path(o) if Path(o).write_bytes(b"png") else Path(o)
        try:
            ctx = self._ctx(tmp, n=3)
            gen = node._generate_batches(ctx)
            self.assertEqual(len(gen["seg_files"]), 3)
            self.assertEqual(len(gen["batch_rows"]), 2)
            self.assertEqual(calls["exec"], 2)
            self.assertIn("Pin from prev", "\n\n".join(gen["reports"]))
            self.assertTrue(Path(gen["checkpoint"]).is_file())
            # 第二次同参数：应按检查点全部跳过
            ctx2 = self._ctx(tmp, n=3)
            gen2 = node._generate_batches(ctx2)
            self.assertEqual(calls["exec"], 2, "检查点未生效：重复执行了导演台")
            self.assertTrue(all(r["reused"] for r in gen2["batch_rows"]))
            self.assertEqual(len(gen2["seg_files"]), 3)
        finally:
            mod._encode_movie = old_encode
            lf.extract_last_frame = old_extract

    def test_auto_rework_calls(self):
        from bettercomfyui_comfy import director_edit as de_mod
        from bettercomfyui_comfy import one_stop_node as mod

        node = self._node()
        tmp = Path(tempfile.mkdtemp())
        ctx = self._ctx(tmp, n=3)
        ctx["seg_specs"] = ctx["seg_specs"]
        seg_files = []
        for i in range(3):
            p = tmp / f"seg_{i + 1}.mp4"
            p.write_bytes(b"x")
            seg_files.append(str(p))
        reviews = [{"verdict": "需返工", "need_regenerate": "2"},
                   {"verdict": "段级", "need_regenerate": "无",
                    "segment_scores": [{"index": 1, "score": 88.0, "passed": True}]},
                   {"verdict": "通过", "need_regenerate": "无"}]
        node._video_review = lambda *a, **k: reviews.pop(0)  # type: ignore
        node._load_video_models = lambda *a, **k: (None, None, None, None)  # type: ignore

        def fake_regen(*, ctx, index, attempt, old_file, work, cascade=False):
            out = Path(work) / f"regen_{index}_{attempt}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"y")
            return out

        node._regen_one_segment = fake_regen  # type: ignore
        old_splice = de_mod.splice_segments
        de_mod.splice_segments = lambda files, *, workdir, fade_seconds=0.0, warnings=None: (
            Path(workdir).mkdir(parents=True, exist_ok=True) or Path(workdir) / "p.mp4")
        try:
            res = node._auto_rework(ctx=ctx, seg_files=seg_files, project_id="p",
                                    project_dir=tmp, audit=[], warnings=[])
            self.assertTrue(res["enabled"])
            self.assertEqual(res["cnt2"][1], 1)
            self.assertEqual(len(res["rounds"]), 2)
            self.assertEqual(res["rounds"][1]["need"], [])
            self.assertTrue(res["fallback_pass"], "返工后通过应记录 fallback_pass")
        finally:
            de_mod.splice_segments = old_splice

    def test_generate_batches_user_timeline(self):
        pytest.importorskip("torch", reason="需要 .comfyenv（ComfyUI venv）里的 torch")
        import torch

        from bettercomfyui_comfy import director_runtime as dr
        from bettercomfyui_comfy import one_stop_node as mod

        node = self._node()
        tmp = Path(tempfile.mkdtemp())
        tl = dr.build_timeline_json(
            task_type="t2v",
            segments=[{"prompt": f"x{i}", "duration_sec": 2.0} for i in range(2)])
        ctx = self._ctx(tmp, n=0)
        ctx["时间轴JSON"] = tl
        ctx["seg_specs"] = []

        def fake_exec(c, t, *, unique_id, seed_value):
            return ([torch.full((48, 8, 8, 3), 0.5)] * 2,
                    {"waveform": torch.zeros(1, 100), "sample_rate": 100},
                    24.0, 48, None, "Segment continuity: OFF", None, None)

        node._execute_timeline = fake_exec  # type: ignore
        node._load_video_models = lambda *a, **k: (None, None, None, None)  # type: ignore
        old_encode = mod._encode_movie
        def _encode(frames, audio, path, fps):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"z")
            return {"path": str(path), "frames": 48, "width": 8, "height": 8,
                    "fps": fps, "has_audio": True, "duration_s": 2.0}

        mod._encode_movie = _encode
        try:
            gen = node._generate_batches(ctx)
            self.assertEqual(len(gen["seg_files"]), 2)
        finally:
            mod._encode_movie = old_encode


if __name__ == "__main__":
    unittest.main(verbosity=2)
