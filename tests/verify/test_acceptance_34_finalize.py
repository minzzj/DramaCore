"""验收 34：成片剪辑（SG6 SPLICE→ENC→自检）+ 视频审核闭环。

用户要求（2026-10-18 凌晨）：第一阶段要「给出剪辑好的视频」，到机器上要检查
①有没有经过剪辑 ②是否完整 ③质量是否高；且整体显存控制在 32GB 以下。

逻辑图依据：SG6 的 SPLICE（拼接）→ ENC（封装）→ R_full（整片审核）→ S_OUT 链。

判定通过：
    P1 成片剪辑节点已注册（24 节点之一），输入输出齐全；
    P2 本机剪辑工作流在界面目录、结构正确、Finalize→视频审核 连线存在；
    P3 真 ComfyUI 执行：3 段合成视频 → 拼接封装 → 成片时长≈Σ段、完整性 passed、
       报告 JSON 落盘、视频审核给出结论与 R_full（真机，非模拟）；
    P4 GPU 总流程包含 Finalize 且把成片路径接给视频审核；
    P5 诚实性：不暴露未实现的转场；审计明确"剪辑性/质量"证据与"人工抽检"边界；
    P6 单段也能出成片；P7 计划段数不符判失败；P8 占位视频被标记；
    P9 段文件不存在明确报错；P10 自动发现最新段视频；P11 自动发现无文件时明确报错。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

UI_DIR = REPO_ROOT / ".comfyui" / "user" / "default" / "workflows"
SRC_DIR = REPO_ROOT / "bettercomfyui_comfy" / "workflows"
BASE = "http://127.0.0.1:8188"
FINAL_NAME = "00_【总流程】段视频→剪好成片（本机可跑）.json"
GPU_NAME = "00_【总流程】剧本→成片（GPU）.json"


def _get(url, timeout=60):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def _post(url, payload, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def _info():
    try:
        return _get(f"{BASE}/object_info", timeout=20)
    except Exception:  # noqa: BLE001
        pytest.skip("未验证：ComfyUI 不可达")


def _widget_names(info, node_type):
    spec = info[node_type]["input"]
    names = []
    for section in (spec.get("required") or {}, spec.get("optional") or {}):
        for name, decl in section.items():
            dtype = decl[0]
            if isinstance(dtype, list) or dtype in ("STRING", "INT", "FLOAT", "BOOLEAN"):
                names.append(name)
    return names


def _api(info, wf):
    out = {}
    links = {int(raw[0]): raw for raw in wf.get("links") or []}
    for node in wf["nodes"]:
        node_type = node["type"]
        inputs = {}
        widgets = list(node.get("widgets_values") or [])
        for i, name in enumerate(_widget_names(info, node_type)):
            if i < len(widgets):
                inputs[name] = widgets[i]
        for entry in node.get("inputs") or []:
            link_id = entry.get("link")
            if link_id is None:
                continue
            raw = links.get(int(link_id))
            if raw is None:
                continue
            inputs[entry["name"]] = [str(int(raw[1])), int(raw[2])]
        out[str(node["id"])] = {"class_type": node_type, "inputs": inputs}
    return out


# ---------------------------------------------------------------------------
# P1 节点注册
# ---------------------------------------------------------------------------


def test_p1_finalize_node_registered():
    info = _info()
    assert "BetterComfyUIFinalize" in info, "成片剪辑节点未注册"
    spec = info["BetterComfyUIFinalize"]
    required = spec["input"]["required"]
    assert "段视频路径列表" in required and "项目" in required
    assert tuple(spec["output_name"])[:1] == ("成片路径",)
    assert "完整性JSON" in spec["output_name"] and "质量JSON" in spec["output_name"]
    assert "审计报告" in spec["output_name"]


# ---------------------------------------------------------------------------
# P2 本机剪辑工作流结构
# ---------------------------------------------------------------------------


def test_p2_local_finalize_workflow_structure():
    from build_master_workflow import validate

    info = _info()
    wf = json.loads((SRC_DIR / FINAL_NAME).read_text(encoding="utf-8"))
    assert (UI_DIR / FINAL_NAME).is_file(), "本机剪辑工作流不在界面目录（用户看不到）"
    types = {n["type"] for n in wf["nodes"]}
    assert "BetterComfyUIFinalize" in types and "BetterComfyUIVideoReview" in types
    assert "PrimitiveStringMultiline" in types, "缺少中文说明区"
    assert len(wf.get("groups") or []) >= 3
    # 连线：Finalize 成片路径(0) → 视频审核 视频文件路径
    by_id = {n["id"]: n for n in wf["nodes"]}
    linked = False
    for link in wf.get("links") or []:
        src = by_id.get(int(link[1]))
        dst = by_id.get(int(link[3]))
        if (src and dst and src["type"] == "BetterComfyUIFinalize"
                and dst["type"] == "BetterComfyUIVideoReview"
                and link[5] == "STRING"):
            linked = True
    assert linked, "成片路径没有接到视频审核（用户还得手抄路径）"
    problems = validate(info, wf)
    assert not problems, "schema 校验失败：\n  " + "\n  ".join(problems[:8])


# ---------------------------------------------------------------------------
# P3 真机执行：3 段 → 成片 → 完整性 → 视频审核
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def finalize_project():
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = REPO_ROOT / str(cfg.global_.project_root or "projects")
    ps = ProjectStore(root, cfg)
    info = ps.create(f"ROUND29成片验收-{time.strftime('%m%d-%H%M%S')}")
    pid = str(info["project_id"])
    seg_dir = root / pid / "_work" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    segments = []
    for i, seconds in enumerate((2.0, 3.0, 2.5), start=1):
        seg = seg_dir / f"seg_{i}.mp4"
        cmd = [
            _ffmpeg_exe(), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=24:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "64k", str(seg),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            pytest.skip(f"未验证：本机 ffmpeg 合成测试段失败：{proc.stderr[-200:]}")
        segments.append(seg)
    yield {"pid": pid, "root": root, "segments": segments, "expected": 7.5}
    try:
        ps.close(pid)
        ps.delete(pid, confirm_token=pid)
    except Exception:  # noqa: BLE001
        pass


def test_p3_finalize_really_stitches_and_audits(finalize_project):
    info = _info()
    ctx = finalize_project
    wf = json.loads((SRC_DIR / FINAL_NAME).read_text(encoding="utf-8"))
    for node in wf["nodes"]:
        values = list(node.get("widgets_values") or [])
        if node["type"] == "BetterComfyUIFinalize":
            names = _widget_names(info, node["type"])
            values[names.index("项目")] = ctx["pid"]
            values[names.index("段视频路径列表")] = "\n".join(str(s) for s in ctx["segments"])
            values[names.index("输出文件名")] = "acceptance_final.mp4"
            values[names.index("完整性检查")] = "完整扫描"
            values[names.index("计划段数")] = 3
            node["widgets_values"] = values
        elif node["type"] == "BetterComfyUIVideoReview":
            names = _widget_names(info, node["type"])
            values[names.index("项目")] = ctx["pid"]
            node["widgets_values"] = values

    api = _api(info, wf)
    result = _post(f"{BASE}/prompt", {"prompt": api, "client_id": "acceptance-34"})
    prompt_id = result.get("prompt_id")
    assert prompt_id, f"/prompt 未返回：{result}"
    deadline = time.time() + 300
    entry = None
    while time.time() < deadline:
        data = _get(f"{BASE}/history/{prompt_id}")
        if prompt_id in data:
            entry = data[prompt_id]
            break
        time.sleep(1.0)
    assert entry is not None, "等待执行超时"
    assert (entry.get("status") or {}).get("status_str") == "success", entry.get("status")
    texts = []
    for _nid, out in (entry.get("outputs") or {}).items():
        texts.extend(str(x) for x in out.get("text") or [])
    joined = "\n".join(texts)

    final = ctx["root"] / ctx["pid"] / "output" / "acceptance_final.mp4"
    assert final.is_file(), "成片文件不存在"
    reports = list((ctx["root"] / ctx["pid"] / "review").glob("finalize_*.json"))
    assert reports, "剪辑自检报告未落盘"
    integrity = json.loads(reports[0].read_text(encoding="utf-8"))["integrity"]
    assert integrity["passed"] is True
    assert integrity["segments"] == 3
    assert integrity["duration_ok"] is True
    assert abs(integrity["final_duration_s"] - ctx["expected"]) <= 0.5
    assert "r1b_scores" in joined, "视频审核没有输出整片 R_full"
    assert ("通过" in joined or "需返工" in joined), "视频审核没有给出结论"


# ---------------------------------------------------------------------------
# P4 GPU 总流程包含剪辑并接到视频审核
# ---------------------------------------------------------------------------


def test_p4_gpu_flow_includes_finalize():
    info = _info()
    wf = json.loads((SRC_DIR / GPU_NAME).read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in wf["nodes"]}
    assert "BetterComfyUIFinalize" in {n["type"] for n in wf["nodes"]}, "GPU 总流程没有剪辑节点"
    linked = False
    for link in wf.get("links") or []:
        src = by_id.get(int(link[1]))
        dst = by_id.get(int(link[3]))
        if src and dst and src["type"] == "BetterComfyUIFinalize" \
                and dst["type"] == "BetterComfyUIVideoReview":
            linked = True
    assert linked, "GPU 总流程里成片路径没接给视频审核"


# ---------------------------------------------------------------------------
# P5 诚实性
# ---------------------------------------------------------------------------


def test_p5_finalize_is_honest():
    source = (REPO_ROOT / "bettercomfyui_comfy" / "finalize_nodes.py").read_text(encoding="utf-8")
    assert "转场" in source and "未实现" in source, "未实现的转场必须在源码/审计里显式说明"
    assert "placeholder_flags" in source, "缺少占位视频检测（防止占位冒充真实生成）"
    assert "passed=True 仅代表机器可算" in source, "必须声明自检边界，不冒充画质结论"
    info = _info()
    spec = info["BetterComfyUIFinalize"]["input"]
    exposed = set(spec.get("required") or {}) | set(spec.get("optional") or {})
    assert "转场" not in exposed, "不得暴露未实现的转场参数"


# ---------------------------------------------------------------------------
# P6~P9 边界用例：直接调用节点（不依赖 ComfyUI 队列，速度更快、断言更细）
# ---------------------------------------------------------------------------


def _new_temp_project():
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = REPO_ROOT / str(cfg.global_.project_root or "projects")
    ps = ProjectStore(root, cfg)
    info = ps.create(f"ROUND31成片边界-{time.strftime('%m%d-%H%M%S')}")
    return ps, root, str(info["project_id"])


def _make_segment(path: Path, seconds: float, *, source: str = "testsrc2") -> Path:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    if source == "testsrc2":
        video = f"testsrc2=size=320x240:rate=24:duration={seconds}"
        cmd = [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", video,
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
               "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
               "-c:a", "aac", "-b:a", "64k", str(path)]
    else:  # 纯色、无音轨 → 用于占位检测
        video = f"color=c=gray:s=320x240:d={seconds}:r=24"
        cmd = [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", video,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"合成失败：{proc.stderr[-300:]}")
    return path


def _run_node(项目, 路径列表, *, 计划段数=0, 每段时长=0.0, 输出名="edge.mp4",
              检查="完整扫描"):
    from bettercomfyui_comfy.finalize_nodes import BetterComfyUIFinalize

    out = BetterComfyUIFinalize().run(
        项目=项目, 段视频路径列表=路径列表, 输出文件名=输出名, 编码CRF=18,
        完整性检查=检查, 计划段数=计划段数, 每段计划时长_秒=每段时长)
    result = out["result"]
    return {
        "final": Path(result[0]),
        "integrity": json.loads(result[1]),
        "quality": json.loads(result[2]),
        "audit": result[3],
    }


def test_p6_single_segment_is_finalized_and_passes(tmp_path):
    ps, root, pid = _new_temp_project()
    try:
        seg = _make_segment(tmp_path / "single.mp4", 2.0)
        out = _run_node(pid, str(seg), 计划段数=1, 每段时长=2.0)
        assert out["final"].is_file()
        assert out["integrity"]["segments"] == 1
        assert out["integrity"]["segment_count_ok"] is True
        assert out["integrity"]["duration_ok"] is True
        assert out["integrity"]["passed"] is True
        assert out["integrity"]["placeholder_flags"] == []
        assert isinstance(out["quality"].get("scores"), dict)
        assert "error" not in out["quality"]["scores"]
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p7_planned_count_mismatch_is_flagged_not_silent(tmp_path):
    ps, root, pid = _new_temp_project()
    try:
        segs = [_make_segment(tmp_path / f"s{i}.mp4", 2.0) for i in range(2)]
        out = _run_node(pid, "\n".join(str(s) for s in segs), 计划段数=5,
                        输出名="mismatch.mp4")
        assert out["integrity"]["segments"] == 2
        assert out["integrity"]["plan_segments"] == 5
        assert out["integrity"]["segment_count_ok"] is False
        assert out["integrity"]["passed"] is False, "计划段数不符必须判失败，不能静默通过"
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p8_placeholder_video_is_flagged(tmp_path):
    ps, root, pid = _new_temp_project()
    try:
        seg = _make_segment(tmp_path / "gray.mp4", 2.0, source="color")
        out = _run_node(pid, str(seg), 计划段数=1, 输出名="placeholder.mp4")
        flags = out["integrity"]["placeholder_flags"]
        assert flags, "纯色/无音轨的占位视频必须被标记，不能冒充真实成片"
        assert any(("无音轨" in f) or ("码率" in f) for f in flags)
        assert out["integrity"]["passed"] is False
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p9_missing_segment_fails_loudly(tmp_path):
    ps, root, pid = _new_temp_project()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            _run_node(pid, str(tmp_path / "does_not_exist.mp4"))
        assert "不存在" in str(excinfo.value)
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p10_auto_discover_recent_segments(tmp_path):
    """列表留空 + 自动发现：应扫描项目输出目录里最近的 mp4 并拼接，且在证据里列出文件。"""
    ps, root, pid = _new_temp_project()
    try:
        auto_dir = root / pid / "output" / "auto_segments"
        auto_dir.mkdir(parents=True, exist_ok=True)
        segs = [_make_segment(auto_dir / f"auto_seg_{i}.mp4", 2.0) for i in (1, 2)]
        # 直接调用节点（自动发现开启；项目输出目录是全新项目，不会混入别的文件）
        from bettercomfyui_comfy.finalize_nodes import BetterComfyUIFinalize

        out = BetterComfyUIFinalize().run(
            项目=pid, 段视频路径列表="", 输出文件名="auto_final.mp4", 编码CRF=18,
            完整性检查="快速抽样（≤10分钟）", 自动发现最新视频=True,
            发现时间窗_分钟=15, 计划段数=2, 每段计划时长_秒=0.0)
        result = out["result"]
        integrity = json.loads(result[1])
        assert Path(result[0]).is_file()
        assert integrity["auto_discovered"] is True
        assert len(integrity["auto_discovered_files"]) >= 2
        assert integrity["segments"] == len(integrity["auto_discovered_files"])
        assert integrity["segment_count_ok"] is True
        assert integrity["duration_ok"] is True
        assert integrity["passed"] is True
        assert "自动发现" in result[3]
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass


def test_p11_auto_discover_without_files_fails_loudly(tmp_path):
    """开启自动发现但窗口内没有文件时，必须明确报错，不能假装出一条空成片。"""
    ps, root, pid = _new_temp_project()
    try:
        from bettercomfyui_comfy.finalize_nodes import BetterComfyUIFinalize

        with pytest.raises(RuntimeError) as excinfo:
            BetterComfyUIFinalize().run(
                项目=pid, 段视频路径列表="", 输出文件名="nothing.mp4",
                完整性检查="跳过", 自动发现最新视频=True, 发现时间窗_分钟=1)
        assert "自动发现" in str(excinfo.value)
    finally:
        try:
            ps.close(pid); ps.delete(pid, confirm_token=pid)
        except Exception:  # noqa: BLE001
            pass
