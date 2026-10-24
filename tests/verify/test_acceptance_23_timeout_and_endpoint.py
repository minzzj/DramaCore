"""验收 23：任务超时看门狗（`global.task_timeout_s`）与 ComfyUI 地址（`global.comfyui_endpoint`）。

## 为什么单列

这两个配置项此前都是「配了不读」（ROUND18 棘轮发现）：

* `global.task_timeout_s` —— **没有看门狗**意味着一个卡死的任务会永久挂住整条流水线，
  用户只看到"没反应"。
* `global.comfyui_endpoint` —— `user.yaml` 里明明写着 `http://127.0.0.1:8188`，
  产品代码却用硬编码的 `DEFAULT_BASE_URL = http://127.0.0.1:8000`（**端口都不对**）。
  用户改配置发现不生效，这种"假可配置"比没有配置更糟。

## 判定通过

    P1 `rework_timeout_s()` 读 `global.task_timeout_s`；
    P2 单段 `Segment.overrides.timeout_s` **优先于**全局值；
    P3 轮询超过超时 -> 抛错（**不无限等待**），错误信息含段 id 与超时值；
    P4 未超时则正常返回终态（不误杀）；
    P5 `resolve_comfyui_endpoint` 从配置读出真实地址（8188，而非硬编码 8000）；
    P6 空配置退回默认且仍可用；
    P7 非法协议（非 http/https）-> 报错（不静默接受）；
    P8 `comfyui_base_path` 与 `comfyui_endpoint` **是两件事**，不得被合并
       （一个是安装目录，一个是 HTTP 地址）。

## 判定假通过（必须能变红）

    F1 去掉超时判断 -> P3 变红；
    F2 忽略单段覆盖 -> P2 变红；
    F3 让 resolver 硬编码返回默认 -> P5 变红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _cfg():
    from bettercomfyui.config import load_config

    return load_config(repo_root=REPO_ROOT)


def _gen(cfg, **kwargs):
    """构造 SegmentGenerator。

    它要求 **R3 门禁依赖**（store 或 lock_resolver）—— 这是契约 §7 的硬要求：
    生成只能使用 
esolve_lock() 通过的素材版本。这里给一个恒通过的假 resolver，
    因为我们测的是超时读数/看门狗，不是门禁本身。
    """
    from bettercomfyui.director.generate import SegmentGenerator

    kwargs.setdefault(
        "lock_resolver",
        lambda asset_id, version: {"asset_id": str(asset_id), "version": str(version)},
    )
    return SegmentGenerator(store=None, counters=None, cfg=cfg, **kwargs)


def _segment():
    from bettercomfyui.models.entities import Segment
    from bettercomfyui.models.enums import SegmentStatus

    seg = Segment(
        segment_id="P_CH1_SEG1",
        chapter_id="CH1",
        segment_index=1,
        prompt="测试",
        status=SegmentStatus.PLANNED,
    )
    return seg


# ---------------------------------------------------------------------------
# P1 / P2 超时读数
# ---------------------------------------------------------------------------


def test_p1_timeout_reads_global_config():
    from bettercomfyui.director.generate import SegmentGenerator

    cfg = _cfg()
    cfg.global_.task_timeout_s = 123.0
    gen = _gen(cfg)
    assert gen.rework_timeout_s(None) == 123.0


def test_p2_segment_override_wins_over_global():
    """单段 `overrides.timeout_s` 必须优先于全局值。"""
    from bettercomfyui.director.generate import SegmentGenerator

    cfg = _cfg()
    cfg.global_.task_timeout_s = 900.0
    seg = _segment()
    seg.overrides.timeout_s = 7.0
    gen = _gen(cfg)
    assert gen.rework_timeout_s(seg) == 7.0, "单段覆盖没有生效"


# ---------------------------------------------------------------------------
# P3 / P4 看门狗
# ---------------------------------------------------------------------------


class _StuckExecutor:
    """永远不终态的执行器 —— 模拟卡死的工作流。"""

    name = "stuck"

    def __init__(self):
        self.polls = 0

    def poll(self, ref):
        from bettercomfyui.executors.base import JobState
        from bettercomfyui.models.enums import JobStatus

        self.polls += 1
        return JobState(status=JobStatus.RUNNING, outputs=[])


def test_p3_timeout_raises_instead_of_waiting_forever():
    """轮询超时必须抛错，**不能无限等**。"""
    from bettercomfyui.director.generate import SegmentGenerator
    from bettercomfyui.executors.base import JobRef, ExecutorError

    cfg = _cfg()
    cfg.global_.task_timeout_s = 5.0

    # 假时钟：每次调用前进 1 秒；轮询 10 次肯定超过 5 秒
    tick = {"t": 0.0}

    def clock() -> float:
        tick["t"] += 1.0
        return tick["t"]

    gen = _gen(cfg, clock=clock, sleeper=lambda _s: None, poll_limit=100)
    seg = _segment()
    with pytest.raises(ExecutorError) as excinfo:
        gen._poll_until_terminal(JobRef(job_id="x", executor="stub"), _StuckExecutor(), segment=seg)
    msg = str(excinfo.value)
    assert "超时" in msg, msg[:200]
    assert "P_CH1_SEG1" in msg, f"错误信息没有段 id：{msg[:200]}"
    assert "5.0" in msg or "5" in msg, f"错误信息没有超时值：{msg[:200]}"


def test_p4_terminal_state_returns_without_timeout():
    """已经终态时立刻返回，不误杀。"""
    from bettercomfyui.director.generate import SegmentGenerator
    from bettercomfyui.executors.base import JobRef, JobState
    from bettercomfyui.models.enums import JobStatus

    class Done:
        name = "done"

        def poll(self, ref):
            return JobState(status=JobStatus.SUCCEEDED, outputs=[])

    cfg = _cfg()
    gen = _gen(cfg, clock=lambda: 0.0, sleeper=lambda _s: None)
    state = gen._poll_until_terminal(JobRef(job_id="x", executor="stub"), Done(), segment=_segment())
    assert state.is_terminal


# ---------------------------------------------------------------------------
# P5 – P8 endpoint
# ---------------------------------------------------------------------------


def test_p5_endpoint_resolved_from_config():
    """必须读出配置里的真实地址，而不是硬编码默认值。"""
    from bettercomfyui.executors.comfyui_http import (
        DEFAULT_BASE_URL,
        resolve_comfyui_endpoint,
    )

    cfg = _cfg()
    cfg.global_.comfyui_endpoint = "http://127.0.0.1:8188"
    resolved = resolve_comfyui_endpoint(cfg)
    assert resolved == "http://127.0.0.1:8188"
    assert resolved != DEFAULT_BASE_URL, "仍在使用硬编码默认地址"


def test_p6_empty_endpoint_falls_back_to_default():
    from bettercomfyui.executors.comfyui_http import (
        DEFAULT_BASE_URL,
        resolve_comfyui_endpoint,
    )

    cfg = _cfg()
    cfg.global_.comfyui_endpoint = ""
    assert resolve_comfyui_endpoint(cfg) == DEFAULT_BASE_URL


def test_p7_invalid_protocol_raises():
    from bettercomfyui.executors.comfyui_http import resolve_comfyui_endpoint

    cfg = _cfg()
    cfg.global_.comfyui_endpoint = "ftp://127.0.0.1:21"
    with pytest.raises(Exception) as excinfo:
        resolve_comfyui_endpoint(cfg)
    assert "comfyui_endpoint" in str(excinfo.value)


def test_p8_base_path_and_endpoint_are_distinct_concepts():
    """两者是不同概念（安装目录 vs HTTP 地址），**不得合并**。"""
    cfg = _cfg()
    cfg.global_.comfyui_base_path = r"H:\some\install\dir"
    cfg.global_.comfyui_endpoint = "http://127.0.0.1:8188"

    from bettercomfyui.executors.comfyui_http import resolve_comfyui_endpoint

    # endpoint 解析不受 base_path 影响
    assert resolve_comfyui_endpoint(cfg) == "http://127.0.0.1:8188"
    # base_path 也不会被当成 URL 用
    assert not str(cfg.global_.comfyui_base_path).startswith("http")
