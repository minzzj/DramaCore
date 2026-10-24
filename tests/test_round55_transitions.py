# -*- coding: utf-8 -*-
"""ROUND55 测试：转场扩展（director_edit）+ AI/规则转场位置决策（transition_planner）。

原则：
- 纯函数测试不依赖 GPU/网络；
- 端到端用 ffmpeg 现场合成 4 段 0.5s 小视频（带音轨），真实拼接验证：
  时长账本、每处转场标签真的生效（fade / 黑场 / 滑像 各一）、审计字段、回退行为。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bettercomfyui.pipeline import transition_planner as tp  # noqa: E402
from bettercomfyui_comfy import director_edit as de  # noqa: E402


def _duration(path: Path) -> float:
    info = de.probe_media(path)
    return float(info.get("duration_s") or 0.0)


class TestTransitionLabelResolve(unittest.TestCase):
    """转场标签解析：中文/英文 → 本机 xfade 标签；未知/不支持回退并告警。"""

    def test_known_chinese_labels(self):
        expected = {
            "无": "", "淡入淡出": "fade", "叠化": "dissolve", "黑场": "fadeblack",
            "白场": "fadewhite", "左滑": "slideleft", "右滑": "slideright",
            "上滑": "slideup", "下滑": "slidedown",
        }
        for label, tag in expected.items():
            self.assertEqual(de.resolve_transition(label), tag, f"标签 {label}")

    def test_extended_labels(self):
        expected = {
            "擦除左": "wipeleft", "擦除右": "wiperight", "擦除上": "wipeup",
            "擦除下": "wipedown", "圆开": "circleopen", "圆闭": "circleclose",
            "像素化": "pixelize", "模糊": "hblur", "推近": "zoomin",
        }
        for label, tag in expected.items():
            self.assertEqual(de.resolve_transition(label), tag, f"标签 {label}")

    def test_english_aliases(self):
        self.assertEqual(de.resolve_transition("dissolve"), "dissolve")
        self.assertEqual(de.resolve_transition("fadeblack"), "fadeblack")
        self.assertEqual(de.resolve_transition("crossfade"), "dissolve")
        self.assertEqual(de.resolve_transition("none"), "")

    def test_unknown_label_falls_back_with_warning(self):
        warnings: list[str] = []
        self.assertEqual(de.resolve_transition("花式旋转", warnings=warnings), "fade")
        self.assertTrue(any("未知转场标签" in w for w in warnings),
                        f"未知标签必须告警，实际 {warnings}")

    def test_unsupported_zoomout_falls_back_with_warning(self):
        # xfade 只有 zoomin，没有 zoomout：必须回退 fade 并明确标注「不支持」
        warnings: list[str] = []
        self.assertEqual(de.resolve_transition("拉远", warnings=warnings), "fade")
        self.assertTrue(any("不支持" in w for w in warnings), f"实际 {warnings}")

    def test_available_transition_labels(self):
        labels = de.available_transition_labels()
        self.assertEqual(labels[0], "无")
        for label in ("淡入淡出", "叠化", "黑场", "白场",
                      "左滑", "右滑", "上滑", "下滑"):
            self.assertIn(label, labels)
        self.assertNotIn("拉远", labels, "本机不支持 zoomout，不应出现在可用清单")


class TestTransitionPlannerRules(unittest.TestCase):
    """transition_planner 纯规则：pin/章节/时间跳跃/场景切换/相似度/评分/用户策略。"""

    def test_pin_continuous_forces_no_transition(self):
        segments = [{"prompt": "同一场戏 A"}, {"prompt": "同一场戏 B", "pin_continuous": True}]
        decision = tp.decide_transitions(segments)
        cut = decision["cuts"][0]
        self.assertEqual(cut["transition"], "无")
        self.assertEqual(cut["duration"], 0.0)
        self.assertEqual(cut["decision_source"], "pin_continuous")
        self.assertIn("pin", decision["audit"]["lines"][0])

    def test_time_jump_keyword_uses_black(self):
        segments = [{"prompt": "夜晚的办公室"}, {"prompt": "次日清晨，城市天际线"}]
        decision = tp.decide_transitions(segments)
        cut = decision["cuts"][0]
        self.assertEqual(cut["transition"], "黑场")
        self.assertEqual(cut["decision_source"], "time_jump")
        self.assertIn("时间跳跃", cut["reason"])

    def test_scene_change_keyword_uses_dissolve(self):
        segments = [{"prompt": "第一场 咖啡馆"}, {"prompt": "画面切到：战场硝烟"}]
        decision = tp.decide_transitions(segments)
        cut = decision["cuts"][0]
        self.assertEqual(cut["transition"], "叠化")
        self.assertEqual(cut["decision_source"], "scene_change")

    def test_chapter_boundary_uses_black(self):
        segments = [{"prompt": "第一场"}, {"prompt": "第二章 归途", "is_chapter_start": True}]
        decision = tp.decide_transitions(segments)
        cut = decision["cuts"][0]
        self.assertEqual(cut["transition"], "黑场")
        self.assertEqual(cut["decision_source"], "chapter_boundary")

    def test_boundary_similarity_rules(self):
        high = tp.decide_transitions([
            {"prompt": "同一场景延续", "boundary_similarity": 0.9},
            {"prompt": "同一场景延续"}])
        self.assertEqual(high["cuts"][0]["transition"], "无")
        self.assertEqual(high["cuts"][0]["decision_source"], "boundary_similar")
        low = tp.decide_transitions([
            {"prompt": "室内", "boundary_similarity": 0.1},
            {"prompt": "室外"}])
        self.assertEqual(low["cuts"][0]["transition"], "叠化")
        self.assertEqual(low["cuts"][0]["decision_source"], "boundary_dissimilar")

    def test_low_score_uses_dissolve(self):
        decision = tp.decide_transitions([
            {"prompt": "镜头 a", "score": 88.0},
            {"prompt": "镜头 b", "score": 55.0}])
        cut = decision["cuts"][0]
        self.assertEqual(cut["transition"], "叠化")
        self.assertEqual(cut["decision_source"], "low_score")

    def test_default_rule_without_signal(self):
        decision = tp.decide_transitions([{"prompt": "镜头 a"}, {"prompt": "镜头 b"}])
        cut = decision["cuts"][0]
        self.assertEqual(cut["transition"], "叠化")
        self.assertEqual(cut["decision_source"], "default")
        self.assertTrue(cut["reason"])

    def test_user_fixed_closes_auto(self):
        segments = [{"prompt": "x"}, {"prompt": "次日"}, {"prompt": "切到 y"}]
        fixed = tp.decide_transitions(segments, fixed_transition="黑场",
                                      fixed_duration=0.8)
        self.assertFalse(fixed["auto"])
        self.assertEqual(fixed["mode"], "fixed")
        for cut in fixed["cuts"]:
            self.assertEqual(cut["transition"], "黑场")
            self.assertEqual(cut["duration"], 0.8)
            self.assertEqual(cut["decision_source"], "user_fixed")

    def test_policy_off_all_hard_cuts(self):
        segments = [{"prompt": "x"}, {"prompt": "y"}, {"prompt": "z"}]
        decision = tp.decide_transitions(segments, user_policy={"mode": "off"})
        self.assertFalse(decision["auto"])
        self.assertEqual([c["transition"] for c in decision["cuts"]], ["无", "无"])
        self.assertEqual([c["duration"] for c in decision["cuts"]], [0.0, 0.0])

    def test_decide_transitions_off_api(self):
        segments = [{"prompt": "x"}, {"prompt": "y"}, {"prompt": "z"}]
        decision = tp.decide_transitions_off(segments)
        self.assertFalse(decision["auto"])
        self.assertEqual(decision["cut_count"], 2)
        self.assertEqual([c["transition"] for c in decision["cuts"]], ["无", "无"])
        self.assertEqual(decision["audit"]["counts"].get("user_off"), 2)

    def test_user_cut_override(self):
        segments = [{"prompt": "x"}, {"prompt": "y"}, {"prompt": "z"}]
        decision = tp.decide_transitions(
            segments, user_policy={"cut_overrides": {1: {"transition": "右滑",
                                                         "duration": 0.6,
                                                         "reason": "用户指定"}}})
        cut = decision["cuts"][1]
        self.assertEqual(cut["transition"], "右滑")
        self.assertEqual(cut["duration"], 0.6)
        self.assertEqual(cut["decision_source"], "user_override")

    def test_empty_segment_list(self):
        decision = tp.decide_transitions([])
        self.assertEqual(decision["cut_count"], 0)
        self.assertEqual(decision["cuts"], [])
        self.assertTrue(decision["warnings"])

    def test_planner_audit_and_plan_shape(self):
        segments = [
            {"prompt": "开场", "score": 90.0},
            {"prompt": "次日清晨", "score": 85.0},
            {"prompt": "同一场戏延续", "pin_continuous": True, "score": 80.0},
        ]
        decision = tp.decide_transitions(segments)
        self.assertIn("rules_version", decision["audit"])
        self.assertEqual(decision["audit"]["cut_count"], 2)
        self.assertTrue(decision["audit"]["lines"])
        self.assertIn("counts", decision["audit"])
        self.assertIn("note", decision["audit"])
        plan = tp.plan_from_decision(decision)
        self.assertEqual(len(plan), 2)
        for i, item in enumerate(plan):
            self.assertEqual(item["cut_index"], i)
            self.assertIn("transition", item)
            self.assertIn("duration", item)
            self.assertIn("reason", item)
        summary = tp.summary(decision)
        self.assertEqual(summary["cut_count"], 2)
        self.assertIn("黑场", summary["transition_counts"])
        lines = tp.audit_lines(decision)
        self.assertTrue(lines and "转场决策" in lines[0])


class TestSpliceTransitionsE2E(unittest.TestCase):
    """ffmpeg 端到端：真实 4 段小视频（320x180 / 0.5s / AAC）拼接。"""

    @classmethod
    def setUpClass(cls):
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        cls.ffmpeg = _ffmpeg_exe()
        cls.tmp = tempfile.TemporaryDirectory(prefix="bcu_r55_")
        cls.dir = Path(cls.tmp.name)
        cls.segs: list[Path] = []
        colors = [("red", 440), ("green", 550), ("blue", 660), ("yellow", 770)]
        for i, (color, freq) in enumerate(colors, start=1):
            path = cls.dir / f"seg_{i}.mp4"
            subprocess.run([
                cls.ffmpeg, "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c={color}:size=320x180:rate=24:duration=0.5",
                "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=0.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(path),
            ], check=True, capture_output=True)
            cls.segs.append(path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, tag: str, **kwargs):
        warnings: list[str] = []
        audit: list[str] = []
        report: dict = {}
        out = de.splice_segments(self.segs, workdir=self.dir / tag, warnings=warnings,
                                 audit=audit, report=report, **kwargs)
        return Path(out), warnings, audit, report

    def test_01_multi_transition_chain(self):
        # 三处接缝 = fade / 黑场 / 左滑（覆盖题目要求的三种）
        plan = [
            {"cut_index": 0, "transition": "淡入淡出", "duration": 0.15, "reason": "测试 fade"},
            {"cut_index": 1, "transition": "黑场", "duration": 0.15, "reason": "测试黑场"},
            {"cut_index": 2, "transition": "左滑", "duration": 0.15, "reason": "测试滑像"},
        ]
        out, warnings, audit, report = self._run("chain", transition_plan=plan)
        self.assertTrue(out.is_file())
        self.assertEqual(report["engine"], "xfade_chain")
        self.assertEqual(report["cut_count"], 3)
        self.assertEqual(report["overlap_total_s"], 0.45)
        expected = 2.0 - 0.45
        self.assertGreater(_duration(out), 1.0, "3 处转场后总时长仍应 >1s")
        self.assertLess(abs(_duration(out) - expected), 0.25,
                        f"时长应接近账本 {expected}s，实际 {_duration(out)}s")
        tags = [cut["xfade"] for cut in report["cuts"]]
        self.assertEqual(tags, ["fade", "fadeblack", "slideleft"])
        self.assertTrue(all(cut["applied"] for cut in report["cuts"]))
        self.assertTrue(all(cut["mode"] == "xfade" for cut in report["cuts"]))
        # 审计：每处转场都有前后帧号/时长/标签/原因
        for i, cut in enumerate(report["cuts"]):
            self.assertIn("before_frame", cut)
            self.assertIn("after_frame", cut)
            self.assertIn("reason", cut)
        joined = "\n".join(audit)
        for token in ("转场[1/3]", "转场[2/3]", "转场[3/3]", "时长账本"):
            self.assertIn(token, joined)
        self.assertTrue(any("淡入淡出(fade)" in line for line in audit))
        self.assertTrue(any("黑场(fadeblack)" in line for line in audit))
        self.assertTrue(any("左滑(slideleft)" in line for line in audit))
        self.assertEqual(warnings, [])
        # 音轨在（音画分离合成后再封装）
        self.assertTrue(de.probe_media(out).get("has_audio"))

    def test_02_mixed_hardcut_and_transition(self):
        plan = [
            {"cut_index": 0, "transition": "淡入淡出", "duration": 0.15, "reason": "a"},
            {"cut_index": 1, "transition": "无", "duration": 0.0, "reason": "pin 连续"},
            {"cut_index": 2, "transition": "左滑", "duration": 0.15, "reason": "b"},
        ]
        out, warnings, audit, report = self._run("mixed", transition_plan=plan)
        self.assertTrue(out.is_file())
        self.assertEqual(report["engine"], "pairwise")
        self.assertEqual([c["applied"] for c in report["cuts"]], [True, False, True])
        self.assertEqual([c["mode"] for c in report["cuts"]], ["xfade", "cut", "xfade"])
        expected = 2.0 - 0.30
        self.assertLess(abs(_duration(out) - expected), 0.25)
        self.assertTrue(de.probe_media(out).get("has_audio"))
        self.assertTrue(any("硬切[2/3]" in line for line in audit))
        self.assertTrue(any("逐遍合成" in w for w in warnings),
                        "混合合成必须如实标注逐遍重编码")

    def test_03_all_hardcuts_use_stream_copy(self):
        out, warnings, audit, report = self._run("hardcut", transition="无")
        self.assertTrue(out.is_file())
        self.assertEqual(report["engine"], "copy")
        self.assertGreater(abs(_duration(out) - 2.0), 0.0)
        self.assertLess(abs(_duration(out) - 2.0), 0.2)
        self.assertEqual([c["transition"] for c in report["cuts"]], ["无", "无", "无"])
        self.assertEqual(report["overlap_total_s"], 0.0)

    def test_04_unknown_label_falls_back_loudly(self):
        out, warnings, _audit, report = self._run("unknown", transition="花式旋转")
        self.assertTrue(out.is_file())
        self.assertEqual(report["engine"], "xfade_chain")
        self.assertTrue(any("未知转场标签" in w for w in warnings))
        self.assertTrue(all(c["xfade"] == "fade" for c in report["cuts"]))
        self.assertTrue(all(c["fallback"] for c in report["cuts"]))

    def test_05_duration_clamp_and_overlap_guard(self):
        # 0.5s 段最多允许 0.45s 转场；请求 5s 应被钳制并告警
        warnings: list[str] = []
        cuts = de._normalize_transition_plan(
            4, [0.5, 0.5, 0.5, 0.5], transition="淡入淡出", transition_plan=None,
            fade_seconds=0.0, transition_seconds=5.0, warnings=warnings)
        self.assertTrue(all(c["duration"] <= 0.45 + 1e-9 for c in cuts), cuts)
        self.assertTrue(any("钳制" in w for w in warnings))
        self.assertTrue(any("总重叠" in w or "上限" in w for w in warnings),
                        f"总重叠保护应告警，实际 {warnings}")

    def test_06_legacy_fade_seconds_still_works(self):
        out, _warnings, _audit, report = self._run("legacy", fade_seconds=0.15)
        self.assertTrue(out.is_file())
        self.assertEqual(report["engine"], "xfade_chain")
        self.assertTrue(all(c["xfade"] == "fade" for c in report["cuts"]))
        self.assertLess(_duration(out), 2.0)

    def test_07_planner_plan_end_to_end(self):
        segments = [
            {"prompt": "开场镜头", "score": 92.0},
            {"prompt": "画面切到：另一处场景", "score": 88.0},
            {"prompt": "同一场戏延续", "pin_continuous": True, "score": 90.0},
            {"prompt": "次日清晨", "score": 85.0},
        ]
        decision = tp.decide_transitions(segments)
        plan = tp.plan_from_decision(decision)
        self.assertEqual(len(plan), 3)
        out, _warnings, audit, report = self._run("planner", transition_plan=plan)
        self.assertTrue(out.is_file())
        # 场景切换→叠化、pin→硬切、时间跳跃→黑场
        self.assertEqual([c["transition"] for c in report["cuts"]],
                         ["叠化", "无", "黑场"])
        self.assertEqual([c["applied"] for c in report["cuts"]], [True, False, True])
        self.assertEqual(report["engine"], "pairwise")
        self.assertTrue(any("转场拼接" in line for line in audit))
        expected = sum(report["segment_durations_s"]) - report["overlap_total_s"]
        self.assertLess(abs(_duration(out) - expected), 0.3)

    def test_08_empty_and_single_input(self):
        with self.assertRaises(ValueError):
            de.splice_segments([], workdir=self.dir / "empty")
        report: dict = {}
        audit: list[str] = []
        single = de.splice_segments([self.segs[0]], workdir=self.dir / "single",
                                    report=report, audit=audit)
        self.assertEqual(Path(single), self.segs[0])
        self.assertEqual(report["engine"], "single_segment")
        self.assertTrue(any("单段" in line for line in audit))

    def test_09_transition_visual_effect_black(self):
        """黑场转场确实生效：转场中点帧亮度显著低于前后帧。"""
        try:
            import numpy as np
            from bettercomfyui.review.local_scorer import frames_from_video
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"缺少 numpy/抽帧工具：{exc}")
        plan = [
            {"cut_index": 0, "transition": "淡入淡出", "duration": 0.2, "reason": "t"},
            {"cut_index": 1, "transition": "黑场", "duration": 0.2, "reason": "t"},
            {"cut_index": 2, "transition": "左滑", "duration": 0.2, "reason": "t"},
        ]
        out, _warnings, _audit, report = self._run("visual", transition_plan=plan)
        black_cut = report["cuts"][1]
        mid_s = (black_cut["output_start_s"] + black_cut["duration_s"] / 2)
        frames = frames_from_video(out, fps=24.0, max_frames=200)
        if len(frames) == 0:
            self.skipTest("抽不到帧")
        idx = min(int(mid_s * 24.0), len(frames) - 1)
        luma = [float(np.asarray(frames[i], dtype=float).mean()) for i in
                (max(0, idx - 8), idx, min(len(frames) - 1, idx + 8))]
        self.assertGreater(luma[0], 20.0)
        self.assertGreater(luma[2], 20.0)
        self.assertLess(luma[1], min(luma[0], luma[2]) * 0.6,
                        f"黑场转场中点应明显变暗，实际 {luma}")


class TestFinishMovieRound55Integration(unittest.TestCase):
    """finish_movie 接入：新转场标签 + per-cut transition_plan 走通 SG6 收尾。"""

    @classmethod
    def setUpClass(cls):
        from bettercomfyui.pipeline.core import _ffmpeg_exe

        cls.ffmpeg = _ffmpeg_exe()
        cls.tmp = tempfile.TemporaryDirectory(prefix="bcu_r55_fin_")
        cls.dir = Path(cls.tmp.name)
        cls.segs = []
        for i, color in enumerate(("red", "green", "blue", "yellow"), start=1):
            path = cls.dir / f"seg_{i}.mp4"
            subprocess.run([
                cls.ffmpeg, "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c={color}:size=320x180:rate=24:duration=0.5",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(path)], check=True, capture_output=True)
            cls.segs.append(path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _finish(self, tag: str, **option_overrides):
        from bettercomfyui_comfy import director_longform as lf

        proj = self.dir / f"project_{tag}"
        proj.mkdir(exist_ok=True)
        specs = [{"prompt": f"第{i + 1}段", "duration_sec": 0.5} for i in range(4)]
        manifest = lf.build_segment_manifest(
            project_id="", segment_specs=specs,
            seg_files=[str(p) for p in self.segs])
        options = {
            "format_label": "mp4", "crf": 23, "subtitle_label": "无",
            "watermark_label": "无", "cover_label": "第一帧",
            "segment_seconds": 0.5, "prompts": [s["prompt"] for s in specs],
        }
        options.update(option_overrides)
        audit, warnings = [], []
        out = de.finish_movie(
            seg_files=self.segs, work_dir=self.dir / f"edit_{tag}",
            project_dir=proj, project_id="", project_display_name="ROUND55单测",
            output_filename=f"成片_{tag}", options=options, manifest=manifest,
            metadata_extra={"script_id": "s1", "workflow_version": "test"},
            provenance={"is_mock": True, "executor": "unit-test",
                        "reason": "synthetic ffmpeg", "unverified": []},
            use_finalize=False, audit=audit, warnings=warnings)
        return out, audit, warnings

    def test_uniform_black_transition(self):
        out, audit, _warnings = self._finish("black", transition_label="黑场",
                                              transition_seconds=0.15)
        final = Path(out["final_path"])
        self.assertTrue(final.is_file(), f"成片不存在：{final}")
        transition = out["transition"]
        self.assertEqual(transition["engine"], "xfade_chain")
        self.assertTrue(all(c["applied"] for c in transition["cuts"]))
        self.assertTrue(all(c["transition"] == "黑场" for c in transition["cuts"]))
        self.assertTrue(any("转场引擎=" in step for step in out["steps"]))
        self.assertLess(_duration(final), 2.0, "转场重叠后总时长应缩短")

    def test_per_cut_plan_with_pin_hardcut(self):
        plan = [
            {"cut_index": 0, "transition": "叠化", "duration": 0.15, "reason": "场景切换"},
            {"cut_index": 1, "transition": "无", "duration": 0.0, "reason": "pin 连续"},
            {"cut_index": 2, "transition": "左滑", "duration": 0.15, "reason": "段落收尾"},
        ]
        out, _audit, _warnings = self._finish(
            "plan", transition_label="无", transition_plan=plan)
        final = Path(out["final_path"])
        self.assertTrue(final.is_file())
        transition = out["transition"]
        self.assertEqual(transition["engine"], "pairwise")
        self.assertEqual([c["applied"] for c in transition["cuts"]], [True, False, True])
        self.assertEqual([c["mode"] for c in transition["cuts"]],
                         ["xfade", "cut", "xfade"])
        expected = sum(transition["segment_durations_s"]) - transition["overlap_total_s"]
        self.assertLess(abs(_duration(final) - expected), 0.3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
