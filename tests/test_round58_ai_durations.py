# -*- coding: utf-8 -*-
"""ROUND58 新需求：段时长由 AI 按剧情决定（4–15s），不固定每段 15s。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.prompt.h3_handbook import CONCISE_SUFFIX, SUFFIX  # noqa: E402
from bettercomfyui.prompt.inject import (  # noqa: E402
    OUTPUT_CONTRACT_VERSION,
    _json_contract,
)
from bettercomfyui.prompt.script_build import (  # noqa: E402
    build_placeholder_prompts,
    collect_video_segments,
)
from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop  # noqa: E402


def _script(*durations):
    segs = []
    for i, d in enumerate(durations):
        seg = {"segment_index": i + 1, "prompt": f"prompt-{i+1}", "asset_refs": []}
        if d is not None:
            seg["duration_s"] = d
        segs.append(seg)
    return {"title": "t", "assets": [], "chapters": [{"chapter_id": "CH001", "segments": segs}]}


def test_collect_video_segments_keeps_ai_durations_and_clamps_range():
    script = _script(4, 8.5, 15, 18, 2, None)
    segs = collect_video_segments(script)
    assert [s["duration_s"] for s in segs] == [4.0, 8.5, 15.0, 15.0, 4.0, 8.0]
    assert [s["segment_index"] for s in segs] == [1, 2, 3, 4, 5, 6]
    assert [s["prompt"] for s in segs] == [f"prompt-{i}" for i in range(1, 7)]


def test_uniform_duration_is_flagged():
    """模型仍把每段写成同一时长时必须显式提示（不静默接受）。"""
    from bettercomfyui.prompt import script_build

    class _Gateway:
        def complete(self, **_kwargs):
            return (
                '{"title":"t","assets":[],"chapters":[{"segments":['
                '{"prompt":"a","asset_refs":[],"duration_s":15},'
                '{"prompt":"b","asset_refs":[],"duration_s":15}]}]}'
            )

        def describe(self):
            return {}

    result = script_build.generate_script_prompts(
        cfg=None, script_text="两段测试剧本", gateway=_Gateway(), output_format="json")
    assert result.meta.get("duration_uniform") is True
    assert any("所有视频段时长都相同" in n for n in result.meta.get("notes") or [])


def test_placeholder_result_has_per_segment_durations_not_uniform_15():
    result = build_placeholder_prompts("一个人推门进来，看见桌上的信。", segment_seconds=7.5)
    assert result.video_durations
    assert all(4 <= d <= 15 for d in result.video_durations)
    assert result.meta.get("duration_source") == "placeholder"


def test_output_contract_demands_duration_and_forbids_fixed_15():
    text = _json_contract()
    assert OUTPUT_CONTRACT_VERSION in text
    assert "duration_s 必填" in text
    assert "4–15" in text
    assert "禁止所有段都写同一个值" in text
    assert "尤其禁止全部写 15" in text


def test_concise_prompt_suffix_has_ai_duration_rule():
    assert "duration_s 由你按剧情决定" in CONCISE_SUFFIX
    assert "不得每段固定 15" in CONCISE_SUFFIX
    assert "duration_s 由剧情决定" in SUFFIX


def test_one_stop_duration_mode_default_is_ai_and_widget_exists():
    required = BetterComfyUIOneStop.INPUT_TYPES()["required"]
    assert "时长模式" in required
    assert required["时长模式"][1]["default"] == "AI自动（按剧情，推荐）"
    assert "AI自动" in required["时长模式"][0][0]
    # 固定时长参数仍在（固定模式下使用）
    assert "每段时长_秒" in required


def test_everything_placeholder_payload_carries_per_segment_durations(tmp_path, monkeypatch):
    """一体化节点输出必须带 video_durations / duration_policy，导演组逐段 duration_s 与之一致。"""
    import json

    from bettercomfyui.config import Config
    from bettercomfyui_comfy import everything_node as E

    cfg = Config()
    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(project_root)
    monkeypatch.setattr(E, "_load_config", lambda: cfg)
    monkeypatch.setattr(E, "_repo_root", lambda: tmp_path)
    node = E.BetterComfyUIEverything()
    out = node.run(
        剧本="第一段：林秀推门进来，看见桌上的信。\n第二段：老陈抬头，两人对视。",
        使用占位结果=True, 项目="时长测试项目", 每段时长_秒=7.5,
    )
    payload = json.loads(out[2])
    durations = payload.get("video_durations")
    assert durations, payload.keys()
    assert all(4.0 <= float(d) <= 15.0 for d in durations)
    assert payload.get("duration_policy", "").startswith("每段 4–15s")
    group_durations = [float(g["duration_s"]) for g in payload.get("director_groups") or []]
    assert group_durations == [float(d) for d in durations]
