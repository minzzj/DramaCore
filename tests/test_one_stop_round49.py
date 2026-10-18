# -*- coding: utf-8 -*-
"""ROUND49 节点审核数据/上下文结构化：本地单元测试（不依赖 ComfyUI/GPU/远端）。"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui_comfy import one_stop_node as mod


class TestVideoSegmentMapping(unittest.TestCase):
    def test_old_style_segment_index_dimensions(self):
        """P1：旧结构 segment_index/dimensions → index/scores 必须填满。"""
        seg_obj = {
            "scope": "segment", "threshold": 70.0,
            "segments": [
                {"segment_index": 1, "file": "/a.mp4",
                 "dimensions": {"画面连贯": 82.5, "角色一致": 78.0},
                 "score": 80.25, "threshold": 70.0, "passed": True,
                 "score_source": "local_heuristic",
                 "counters": {}, "degrade": []},
                {"segment_index": 2, "file": "/b.mp4",
                 "dimensions": {"画面连贯": 61.0},
                 "score": 61.0, "threshold": 70.0, "passed": False,
                 "score_source": "local_heuristic",
                 "counters": {"CNT2": 1, "limit": 3},
                 "degrade": ["FALLBACK_DEGRADED"],
                 "fallback": {"source": "REVIEW_FALLBACK"}},
            ],
            "need_regenerate": [2],
        }
        rows = mod._map_video_segment_scores(seg_obj)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["index"], 1)
        self.assertEqual(rows[0]["scores"]["画面连贯"], 82.5)
        self.assertTrue(rows[0]["passed"])
        self.assertEqual(rows[1]["index"], 2)
        self.assertEqual(rows[1]["cnt2"], 1)
        self.assertEqual(rows[1]["degrade"], ["FALLBACK_DEGRADED"])
        self.assertIsNotNone(rows[1]["fallback"])

    def test_new_style_alias(self):
        seg_obj = {"segments": [{"index": 3, "scores": {"a": 1.0}, "score": 1.0}]}
        rows = mod._map_video_segment_scores(seg_obj)
        self.assertEqual(rows[0]["index"], 3)
        self.assertEqual(rows[0]["scores"], {"a": 1.0})

    def test_empty(self):
        self.assertEqual(mod._map_video_segment_scores(None), [])
        self.assertEqual(mod._map_video_segment_scores({}), [])

    def test_need_parse(self):
        self.assertEqual(mod._parse_need_regenerate("2,3"), [2, 3])
        self.assertEqual(mod._parse_need_regenerate("无"), [])
        self.assertEqual(mod._parse_need_regenerate(""), [])
        self.assertEqual(mod._parse_need_regenerate([1, 2]), [1, 2])


class TestAssetsSummary(unittest.TestCase):
    def test_summary_numbers(self):
        rows = [
            {"asset_id": "a1", "name": "主角", "type": "CHARACTER", "version": "v3",
             "state": "FALLBACK", "lock_status": "LOCKED", "score": 64.5},
            {"asset_id": "a2", "name": "场景", "type": "SCENE", "version": "v2",
             "state": "ALLOWED", "lock_status": "LOCKED", "score": 71.0},
            {"asset_id": "a3", "name": "配角", "type": "CHARACTER", "version": "v1",
             "state": "BLOCKED", "lock_status": "UNLOCKED", "score": 42.0},
        ]
        summary = mod._asset_review_summary(
            rows, threshold=70, review_mode="模型审核（本地启发式）",
            generated=3, fallback=1)
        self.assertEqual(summary["generated"], 3)
        self.assertEqual(summary["locked"], 2)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["fallback"], 1)
        self.assertAlmostEqual(summary["avg_score"], round((64.5 + 71.0 + 42.0) / 3, 2))
        self.assertEqual(summary["threshold"], 70)
        self.assertEqual(len(summary["states"]), 3)

    def test_summary_empty(self):
        summary = mod._asset_review_summary([], threshold=None, review_mode="模型审核",
                                            generated=0)
        self.assertEqual(summary["locked"], 0)
        self.assertIsNone(summary["avg_score"])


class TestProjectContext(unittest.TestCase):
    def test_context_from_everything_payload(self):
        payload = {
            "video_prompts": ["p1", "p2"],
            "assets_by_type": {"CHARACTER": [{}, {}], "SCENE": [{}]},
            "meta": {"mode": "占位（未调用真实模型）", "api_calls": 0,
                     "manifest_injected": True,
                     "library": {"created": 1, "reused": 2, "suspect": 0,
                                 "errors": 0, "skipped": ""}},
            "references": {"locked_assets": [
                {"asset_id": "x", "type": "CHARACTER", "name": "主角", "version": "v2"}],
                "max_refs": 9},
        }
        ctx = mod._build_project_context(payload, "剧核测试项目", "abc123")
        self.assertEqual(ctx["project_id"], "abc123")
        self.assertEqual(ctx["locked_count"], 1)
        self.assertEqual(ctx["locked_assets"][0]["version"], "v2")
        self.assertTrue(ctx["manifest_injected"])
        self.assertIn("同项目继续生成", ctx["note"])
        counts = mod._count_assets_by_type(payload)
        self.assertEqual(counts, {"CHARACTER": 2, "SCENE": 1})

    def test_context_no_project(self):
        ctx = mod._build_project_context({}, "空项目", "")
        self.assertEqual(ctx["locked_count"], 0)
        self.assertIn("不注入", ctx["note"])


class TestThresholdOverride(unittest.TestCase):
    def test_inject_and_restore(self):
        from bettercomfyui.review import asset_review as ar

        restore, ok = mod._review_threshold_override(55)
        if not ok:
            self.skipTest("review 库不可用，跳过阈值注入测试")
        try:
            params = ar.resolve_review_params(None)
            self.assertAlmostEqual(float(params.score_threshold), 55.0)
        finally:
            restore()
        params2 = ar.resolve_review_params(None)
        self.assertNotAlmostEqual(float(params2.score_threshold), 55.0)

    def test_negative_threshold_noop(self):
        restore, ok = mod._review_threshold_override(-1)
        self.assertIsNone(restore)
        self.assertFalse(ok)


class TestNodeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop

        cls.cls = BetterComfyUIOneStop
        cls.info = BetterComfyUIOneStop.INPUT_TYPES()

    def test_asset_threshold_param(self):
        req = self.info["required"]
        self.assertIn("素材审核阈值", req)
        sig = inspect.signature(self.cls.run)
        self.assertIn("素材审核阈值", sig.parameters)

    def test_ui_report_contract_source(self):
        src = (ROOT / "bettercomfyui_comfy" / "one_stop_node.py").read_text(encoding="utf-8")
        self.assertIn('ui["dramacore_report"] = report_obj', src)
        for key in ('"project_context":', '"review_summary":', '"segment_seconds":',
                    '"need_regenerate_segments":'):
            self.assertIn(key, src)

    def test_video_review_fake_node_mapping(self):
        """P1 端到端（桩）：旧 JSON 结构经 _video_review 后 index/scores 不再为 None。"""
        import json

        class FakeReview:
            def run(self, **kwargs):
                seg = {
                    "scope": "segment", "threshold": 70.0,
                    "segments": [{"segment_index": 1,
                                  "dimensions": {"画面连贯": 88.0},
                                  "score": 88.0, "threshold": 70.0, "passed": True}],
                    "need_regenerate": [],
                }
                full = {"executed": True, "r1b_scores": {"叙事流畅": 90.0},
                        "final_score": 90.0, "threshold": 70.0, "passed": True,
                        "policy": "none"}
                return ("通过", json.dumps(seg, ensure_ascii=False),
                        json.dumps(full, ensure_ascii=False), "无", "audit-line")

        from bettercomfyui_comfy import review_nodes

        orig = review_nodes.BetterComfyUIVideoReview
        review_nodes.BetterComfyUIVideoReview = FakeReview
        try:
            node = self.cls()
            out = node._video_review(Path("/tmp/fake.mp4"), "冒烟", 5.0, [])
            rows = out["segment_scores"]
            self.assertEqual(rows[0]["index"], 1)
            self.assertEqual(rows[0]["scores"], {"画面连贯": 88.0})
            self.assertTrue(rows[0]["passed"])
            self.assertEqual(out["need_regenerate_segments"], [])
            self.assertTrue(out["full"]["passed"])
        finally:
            review_nodes.BetterComfyUIVideoReview = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
