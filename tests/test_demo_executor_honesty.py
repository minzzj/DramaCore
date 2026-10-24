"""回归：`run_demo` / `BetterComfyUIRun` 的「真实执行器」必须是**失败闭合**。

## 为什么要有这个文件（审计 R1 发现）

`run_demo(executor="comfyui")` 历史实现只把 `used_mock` 置 False，**实际生成链路仍是
ffmpeg 占位媒体**：产出纯色占位 mp4，却在成片元数据里写
`is_mock_output=false / generation_provenance.executor="comfyui"`，节点状态还显示「成功」。

这正是任务书第 9 节明令禁止的「把模拟结果说成真实生成已通过」。
本文件锁住修复后的契约：

    P1 run_demo 只接受 mock；非 mock -> ok=False + 可操作错误 + 不产出成片；
    P2 节点选择「真实 ComfyUI」必须显式报错，绝不静默退回占位；
    P3 mock 路径产出的元数据永远是 is_mock=True / executor=placeholder-ffmpeg；
    P4 元数据 full_review_state 是语义字符串（PASSED/FAILED），不是 "True"/"False"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from bettercomfyui.config import load_config  # noqa: E402


def _cfg():
    return load_config(repo_root=REPO_ROOT)


# ---------------------------------------------------------------------------
# P1：非 mock 执行器必须在产生任何产物前失败闭合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("executor", ["comfyui", "real", "CUDA", ""])
def test_run_demo_rejects_non_mock_executor(executor, tmp_path):
    from bettercomfyui.demo import run_demo

    report = run_demo(
        repo_root=REPO_ROOT,
        config=_cfg(),
        executor=executor,
        workdir=tmp_path / "work",
    )
    assert report["ok"] is False, (
        f"executor={executor!r} 竟返回 ok=True —— 会用占位媒体冒充真实生成")
    assert report["used_mock"] is True, "run_demo 只有占位媒体路径，used_mock 必须恒为 True"
    assert report["real_generation"] is False
    assert report.get("output_path") is None, "失败闭合路径不得产出成片"
    assert report.get("project_id") is None, "失败闭合路径不得创建项目"
    error = str(report.get("error") or "")
    assert "占位媒体" in error and ("未接入" in error or "没有真实" in error), (
        f"错误信息必须说明「没有真实执行器/只产出占位媒体」，实际：{error!r}")


# ---------------------------------------------------------------------------
# P2：ComfyUI 节点必须显式报错，绝不静默退回占位
# ---------------------------------------------------------------------------


def test_comfy_node_real_executor_raises_explicit_error():
    from bettercomfyui_comfy import nodes as N

    node = N.BetterComfyUIRun()
    with pytest.raises(N._NodeError) as ei:
        node.run(project_id="", script_text="", review_mode="MODEL",
                 asset_review_mode="MODEL", executor="真实 ComfyUI",
                 max_segments=1, enable_cascade=True)
    msg = str(ei.value)
    assert "占位媒体" in msg and "假成功" in msg, (
        f"节点报错必须点明「占位媒体/假成功」并提供真实出片指引，实际：{msg!r}")
    assert "S2" in msg or "W3" in msg or "00_" in msg, "必须给出真实出片的工作流指引"


# ---------------------------------------------------------------------------
# P3/P4：mock 路径的元数据必须诚实
# ---------------------------------------------------------------------------


def test_mock_demo_metadata_declares_placeholder_provenance(tmp_path):
    from bettercomfyui.demo import run_demo

    report = run_demo(
        repo_root=REPO_ROOT,
        config=_cfg(),
        executor="mock",
        workdir=tmp_path / "work",
    )
    assert report["ok"] is True, f"占位闭环本身应当成功：error={report.get('error')}"
    assert report["used_mock"] is True
    assert report["real_generation"] is False
    assert report.get("metadata_path"), "mock 路径也必须落盘元数据（否则成片无法区分来源）"
    meta = json.loads(Path(report["metadata_path"]).read_text(encoding="utf-8"))
    assert meta["is_mock_output"] is True, "mock 产出的元数据必须标 is_mock_output=True"
    prov = meta["generation_provenance"]
    assert prov["is_mock"] is True
    assert prov["executor"] == "placeholder-ffmpeg", (
        f"executor 必须是 placeholder-ffmpeg，实际 {prov['executor']!r}")
    assert "warning" in prov, "mock 元数据必须带醒目 warning"
    assert meta["full_review_state"] in ("PASSED", "FAILED"), (
        f"full_review_state 必须是语义字符串，实际 {meta['full_review_state']!r}")
