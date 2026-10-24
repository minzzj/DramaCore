# -*- coding: utf-8 -*-
"""ROUND54：多段剧本 / 素材上限 回归测试（离线，无需 GPU）。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bettercomfyui.pipeline.core import _ffmpeg_exe  # noqa: E402
from bettercomfyui_comfy.one_stop_node import BetterComfyUIOneStop  # noqa: E402


def _make_video(path: Path, seconds: float = 0.4) -> bool:
    proc = subprocess.run(
        [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc2=size=160x96:rate=12:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
         "-c:a", "aac", "-b:a", "48k", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode == 0


def test_split_multi_script_line_and_inline():
    txt = "第一章 A\n---\n第二章 B\n---\n第三章 C"
    assert BetterComfyUIOneStop._split_multi_script(txt, "---") == [
        "第一章 A", "第二章 B", "第三章 C"]
    assert BetterComfyUIOneStop._split_multi_script("A===B", "===") == ["A", "B"]
    assert BetterComfyUIOneStop._split_multi_script("", "---") == []
    assert BetterComfyUIOneStop._split_multi_script("单章无分隔", "---") == ["单章无分隔"]


def test_asset_limit_raised_to_64():
    req = BetterComfyUIOneStop.INPUT_TYPES()["required"]
    assert req["素材最多生成个数"][1]["max"] == 64
    assert req["素材最多生成个数"][1]["default"] == 12
    # 多段剧本参数已暴露
    assert "多段剧本" in req and "多段剧本分隔符" in req


def test_multi_script_runs_chapters_and_splices(tmp_path):
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    calls: list[str] = []

    def fake_single(**kwargs):
        calls.append(kwargs["剧本"])
        f = out_dir / kwargs["输出文件名"]
        if not _make_video(f):
            pytest.skip("ffmpeg 合成失败")
        report = {"verdict": "通过", "project_context": {"api_calls": 1},
                  "占位": True}
        return {"ui": {"text": ["chapter " + kwargs["剧本"]]},
                "result": (str(f), json.dumps(report, ensure_ascii=False), "{}",
                           "chapter audit", "{}", "director report")}

    node._run_single = fake_single
    node._export_to_comfyui = lambda path: None  # type: ignore[assignment]
    res = node._run_multi_script({
        "多段剧本": "第一章 雪夜\n---\n第二章 清晨",
        "多段剧本分隔符": "---",
        "输出文件名": "总片.mp4",
        "项目": "多段测试",
        "断点续跑": False,
    })
    assert len(calls) == 2 and calls[0].startswith("第一章")
    master = Path(res["result"][0])
    assert master.is_file() and master != out_dir / "无"
    report = json.loads(res["result"][1])
    assert report["multi_script"]["api_calls_total"] == 2
    assert len(report["multi_script"]["chapters"]) == 2
    chapters = [c for c in report["multi_script"]["chapters"] if c.get("ok")]
    assert len(chapters) == 2
    assert "多段剧本" in res["result"][3]


def test_multi_script_partial_failure_continues(tmp_path):
    node = BetterComfyUIOneStop.__new__(BetterComfyUIOneStop)
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    def fake_single(**kwargs):
        if "失败章" in kwargs["剧本"]:
            raise RuntimeError("模拟失败")
        f = out_dir / kwargs["输出文件名"]
        if not _make_video(f):
            pytest.skip("ffmpeg 合成失败")
        report = {"project_context": {"api_calls": 1}}
        return {"ui": {}, "result": (str(f), json.dumps(report), "{}", "a", "{}", "r")}

    node._run_single = fake_single
    node._export_to_comfyui = lambda path: None  # type: ignore[assignment]
    res = node._run_multi_script({
        "多段剧本": "第一章 正常\n---\n失败章 内容\n---\n第三章 正常",
        "多段剧本分隔符": "---",
        "输出文件名": "总片2.mp4",
        "项目": "多段失败测试",
        "断点续跑": False,
    })
    report = json.loads(res["result"][1])
    assert report["multi_script"]["api_calls_total"] == 2
    ok = [c for c in report["multi_script"]["chapters"] if c.get("ok")]
    bad = [c for c in report["multi_script"]["chapters"] if not c.get("ok")]
    assert len(ok) == 2 and len(bad) == 1
    assert "失败" in res["result"][3]
