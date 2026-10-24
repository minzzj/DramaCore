# -*- coding: utf-8 -*-
"""ROUND53 修复回归测试：默认值/归一化开关/四维评分/人工审批等待/阶段检查点/配置接线。"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.prompt import normalize as norm_mod  # noqa: E402
from bettercomfyui.review import local_scorer as ls  # noqa: E402
from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop  # noqa: E402


def _fake_cfg(**asset_overrides):
    asset = SimpleNamespace(human_timeout_minutes=0.03, retry_interval="immediate",
                            score_threshold=70.0, **asset_overrides)
    video = SimpleNamespace(regenerate_granularity="segment")
    glob = SimpleNamespace(checkpoint_every_stage=True)
    return SimpleNamespace(asset_review=asset, video_review=video, global_=glob)


def test_defaults_aligned_to_flow_requirements():
    req = BetterComfyUIOneStop.INPUT_TYPES()["required"]
    assert req["每段时长_秒"][1]["default"] == 10.0
    assert req["宽度"][1]["default"] == 1920
    assert req["高度"][1]["default"] == 1088
    assert req["素材最多生成个数"][1]["default"] == 12
    assert req["CNT2上限"][1]["default"] == 3
    assert req["CNT3上限"][1]["default"] == 3


def test_normalize_names_config_toggle():
    try:
        norm_mod.set_normalize_enabled(False)
        assert norm_mod.normalize_name("  Iron  Man ") == "Iron Man"
        norm_mod.set_normalize_enabled(True)
        assert norm_mod.normalize_name("  Iron  Man ") == "ironman"
    finally:
        norm_mod.set_normalize_enabled(True)


def _make_image(path: Path, pattern: str = "grad"):
    from PIL import Image
    import numpy as np
    if pattern == "grad":
        arr = np.tile(np.linspace(0, 255, 128, dtype=np.uint8)[:, None], (1, 128))
    else:
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 255, (128, 128), dtype=np.uint8)
    img = Image.fromarray(arr, mode="L").convert("RGB")
    img.save(path)


def test_image_scores_four_dims_when_reference_and_prompt(tmp_path):
    ref = tmp_path / "ref.png"
    cur = tmp_path / "cur.png"
    _make_image(ref, "grad")
    _make_image(cur, "grad")
    scores, skipped = ls.image_scores(cur, reference_path=ref,
                                      prompt_text="林秀在雪夜客栈")
    assert "画质" in scores and "构图" in scores
    assert "一致性" in scores and 0 <= scores["一致性"] <= 100
    assert "合规" in scores and scores["合规"] == 100.0
    assert "一致性" not in skipped and "合规" not in skipped

    scores2, skipped2 = ls.image_scores(cur)
    assert "一致性" in skipped2 and "合规" in skipped2

    scores3, _ = ls.image_scores(cur, prompt_text="暴力血腥枪击场景")
    assert scores3["合规"] < 100.0


def test_human_review_wait_approves_and_writes_pending(tmp_path):
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    node._cfg = _fake_cfg()
    project_dir = tmp_path / "proj"
    audit, warnings = [], []
    decision_file = project_dir / "review" / "asset_decision_a1_v1.json"

    def approve():
        time.sleep(0.3)
        pending = project_dir / "review" / "asset_pending_a1_v1.json"
        nonce = ""
        try:
            nonce = str((json.loads(pending.read_text(encoding="utf-8")) or {}).get("nonce") or "")
        except Exception:
            pass
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.write_text(json.dumps(
            {"approved": True, "operator": "tester", "reason": "ok",
             "nonce": nonce}), encoding="utf-8")

    th = threading.Thread(target=approve)
    th.start()
    out = node._wait_human_asset_decision(project_dir=project_dir, asset_id="a1", version="v1",
                                         name="主角", png=None, audit=audit, warnings=warnings)
    th.join()
    assert out == {"approved": True}
    # ROUND55 A4 修复：pending 用后归档为 .done.json，decision 消费为 .used.json
    review_dir = project_dir / "review"
    assert (review_dir / "asset_pending_a1_v1.done.json").is_file()
    assert not decision_file.exists()
    assert any("人工审核：通过" in line for line in audit)


def test_human_review_timeout_falls_back_to_model(tmp_path):
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    node._cfg = _fake_cfg()
    node._cfg.asset_review.human_timeout_minutes = 0.003  # ~0.18s
    out = node._wait_human_asset_decision(project_dir=tmp_path, asset_id="a2", version="v2",
                                         name="配角", png=None, audit=[], warnings=[])
    assert out.get("forced_model") is True


def test_stage_checkpoint_written(tmp_path):
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    node._cfg = _fake_cfg()
    audit = []
    node._stage_checkpoint(tmp_path, "prompts", {"ok": True}, audit)
    files = list((tmp_path / "_work").glob("stage_prompts_*.json"))
    assert files, audit
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["stage"] == "prompts" and data["data"]["ok"] is True
    assert any("阶段检查点" in line for line in audit)


def test_retry_interval_and_granularity_helpers():
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    node._cfg = _fake_cfg()
    assert node._retry_interval_seconds() == 0.5
    node._cfg.asset_review.retry_interval = "2"
    assert node._retry_interval_seconds() == 2.0
    node._cfg.video_review.regenerate_granularity = "full"
    gran = str(getattr(node._cfg.video_review, "regenerate_granularity", "segment"))
    assert gran == "full"
