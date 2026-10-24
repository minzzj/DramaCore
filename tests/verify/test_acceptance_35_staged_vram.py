"""验收 35：32GB 生产显存的「分两段执行」编排（结构可本地验证，显存效果需 GPU 实测）。

用户要求：真实生产环境 5090-32G，整体显存必须控制在 32GB 以下。
图像模型 + H3 同时驻留 ≈46GB → 必须分阶段；本验收验证我们提供的编排工具：

    P1 能把 GPU 总流程正确拆成 A（素材）/B（出片+剪辑+审核）两段：
       A 含素材链、B 含视频/剪辑/审核链，Everything/Settings 两段共用；
    P2 拆分后的 API prompt 只包含本段节点，且没有悬空的输入引用；
    P3 /free 路由可用（两段之间释放模型显存；CPU 上只能验证路由通，显存下降需 GPU）；
    P4 CLI `scripts/staged_run.py --dry-run` 可跑并报告拆分结果（不提交队列）。

诚实边界：本验收**不能**证明 5090 上峰值 <32GB——那必须在目标机用
scripts/vram_monitor.py + nvidia-smi 实测，并回填 docs/实测数据回填模板.md。
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

BASE = "http://127.0.0.1:8188"
SRC_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
GPU_NAME = "00_【总流程】剧本→成片（GPU）.json"


def _get(path: str, timeout: int = 60):
    return json.load(urllib.request.urlopen(f"{BASE}{path}", timeout=timeout))


def _info():
    try:
        return _get("/object_info", timeout=20)
    except Exception:  # noqa: BLE001
        pytest.skip("未验证：ComfyUI 不可达")


def _wf() -> dict:
    path = SRC_DIR / GPU_NAME
    assert path.is_file(), f"缺少工作流：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# P1 拆分结构
# ---------------------------------------------------------------------------


def test_p1_split_into_asset_and_video_stages():
    import staged_run

    wf = _wf()
    stage_a, stage_b, meta = staged_run.split_workflow(wf)
    a_types = {n["type"] for n in stage_a["nodes"]}
    b_types = {n["type"] for n in stage_b["nodes"]}

    # A 段：素材生成/入库/审核 + 图像生成链
    for required in ("BetterComfyUIAssetGen", "BetterComfyUIAssetIngest",
                     "BetterComfyUIAssetReview", "CheckpointLoaderSimple", "KSampler"):
        assert required in a_types, f"A 段缺少素材链节点：{required}"
    # B 段：视频权重 + 出片 + 剪辑 + 视频审核
    for required in ("UNETLoader", "CLIPLoader", "VAELoader", "MiniMaxH3Director",
                     "CreateVideo", "SaveVideo", "BetterComfyUIFinalize",
                     "BetterComfyUIVideoReview"):
        assert required in b_types, f"B 段缺少出片/剪辑/审核节点：{required}"
    # 共用：配置与剧本（Everything 在 B 段命中执行缓存，不再调 API）
    for shared in ("BetterComfyUISettings", "BetterComfyUIEverything"):
        assert shared in a_types and shared in b_types, f"{shared} 应两段共用"
    # 互不混杂
    assert "MiniMaxH3Director" not in a_types, "A 段不应加载视频权重（会占用 32G 显存）"
    assert "CheckpointLoaderSimple" not in b_types, "B 段不应加载图像模型（会与 H3 同驻留）"
    assert meta["stage_a_nodes"] >= 10 and meta["stage_b_nodes"] >= 10
    assert meta["total_nodes"] == len(wf["nodes"])


# ---------------------------------------------------------------------------
# P2 拆分后的 API prompt 无悬空引用
# ---------------------------------------------------------------------------


def test_p2_stage_prompts_have_no_dangling_links():
    import staged_run

    info = _info()
    wf = _wf()
    full = staged_run.workflow_to_api(info, wf)
    stage_a, stage_b, _meta = staged_run.split_workflow(wf)
    for label, stage in (("A", stage_a), ("B", stage_b)):
        ids = {n["id"] for n in stage["nodes"]}
        prompt, dropped = staged_run.prune_prompt(full, ids)
        assert len(prompt) == len(ids), f"阶段{label} 提交节点数不符"
        for nid, node in prompt.items():
            for key, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and str(value[0]).isdigit():
                    assert int(value[0]) in ids, (
                        f"阶段{label} {nid}.{key} 引用了不在本段的节点 {value[0]}")
        assert not dropped, f"阶段{label} 出现被剪断的上游输入：{dropped[:5]}"


# ---------------------------------------------------------------------------
# P3 /free 路由
# ---------------------------------------------------------------------------


def test_p3_free_route_is_available():
    try:
        _get("/system_stats", timeout=10)
    except Exception:  # noqa: BLE001
        pytest.skip("未验证：ComfyUI 不可达")
    req = urllib.request.Request(
        f"{BASE}/free", data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert int(getattr(resp, "status", 200)) == 200, "/free 路由不可用"
    proc = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "scripts" / "free_vram.py"),
                           "--check"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, f"free_vram.py --check 退出码 {proc.returncode}"


# ---------------------------------------------------------------------------
# P4 CLI dry-run
# ---------------------------------------------------------------------------


def test_p4_staged_run_dry_run_cli():
    proc = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts" / "staged_run.py"), "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    assert proc.returncode == 0, f"dry-run 失败：{proc.stdout[-500:]} {proc.stderr[-500:]}"
    out = proc.stdout
    assert "拆分" in out
    assert "阶段A" in out and "阶段B" in out
    assert "dry-run" in out
