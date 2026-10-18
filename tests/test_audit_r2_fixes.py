"""R2 审计回归：R1 修复的跨进程复核 + R2 新发现缺陷的修复验证。

## 覆盖

A. R1-fix4（审核跨运行状态）——**真跨进程**复核：
   A1 三轮候选在 3 个独立 python 进程间累积，FALLBACK 选中最高分版本；
   A2 人工等待在进程1落盘、进程2超时自动切模型并继续执行模型轮；
   A3 状态文件损坏 -> 不崩、按新状态继续且摘要如实说明；
   A4 已 LOCK 后删掉审核状态（模拟崩溃丢状态）再跑 -> 不重复提交、不丢结论。
B. R2-F1（工作流项目引用可移植性）：
   B1 ProjectStore.resolve_reference 支持显示名查找/新建；32 位 id 不存在必须报错（不静默新建）；
   B2 节点 _store_for_project 支持显示名；发布的工作流里不得再出现开发机 32 位项目 id；
   B3 Everything 节点对「显示名自动新建/按名解析/坏 id」都写进审计（不静默）。
C. R2-F2（LOCK 指纹门禁）：LOCK 后替换文件内容 -> 素材节点/融合参考图入口拒绝；
   未替换内容 -> 正常通过。
D. R2-F3（S4 视频审核对未通过整片不应崩）：默认策略未配置时返回「未通过+unconfigured」结论；
   显式策略仍按策略返回 block。
E. R2-F4（CNT2/CNT3 达到上限后不再新增候选）：重复审核同一成片，计数封顶不无限增长。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.config import load_config  # noqa: E402
from bettercomfyui.models.enums import (  # noqa: E402
    AssetSource,
    AssetStatus,
    AssetType,
    FullReviewPolicy,
    LibraryScope,
)
from bettercomfyui.review.asset_review import AssetReviewer  # noqa: E402
from bettercomfyui.review.counters import CounterStore  # noqa: E402
from bettercomfyui.store.assets import AssetStore  # noqa: E402
from bettercomfyui.store.paths import ProjectPaths  # noqa: E402
from bettercomfyui.store.projects import ProjectStore  # noqa: E402

# ---------------------------------------------------------------------------
# 共享夹具
# ---------------------------------------------------------------------------


def _cfg(project_root: Path):
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.project_root = str(project_root)
    return cfg


def _make_project(tmp_path: Path, name: str = "R2审计项目"):
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(root)
    info = ProjectStore(root, cfg).create(name)
    pid = str(info["project_id"])
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    return cfg, pid, paths, AssetStore(paths)


def _png(path: Path, kind: str = "flat") -> Path:
    from PIL import Image

    if kind == "noise":
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 256, size=(128, 128, 3), dtype=np.uint8)
    elif kind == "dark":
        arr = np.full((128, 128, 3), 20, dtype=np.uint8)
    else:
        arr = np.full((128, 128, 3), 128, dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)
    return path


def _add_versions(assets: AssetStore, img_kinds: list[str], tmp_path: Path):
    entry = assets.create_asset(
        asset_type=AssetType.CHARACTER, name="R2角色", source=AssetSource.AI,
        library_scope=LibraryScope.PROJECT)
    recs = []
    for i, kind in enumerate(img_kinds):
        img = _png(tmp_path / f"v{i+1}_{kind}.png", kind)
        recs.append(assets.add_version(
            entry["asset_id"], source=AssetSource.AI, file_path=img,
            status=AssetStatus.PENDING_REVIEW))
    return entry["asset_id"], recs


# 跨进程 worker：在**独立 python 进程**里跑一次审核节点
_WORKER = r'''
import json, os, pathlib, sys
sys.path.insert(0, os.getcwd())
from bettercomfyui.config import load_config
from bettercomfyui_comfy import nodes as N

root = pathlib.Path(os.environ["AUDIT_PROJECT_ROOT"])
cfg = load_config(repo_root=pathlib.Path(os.getcwd()))
cfg.global_.project_root = str(root)
threshold = os.environ.get("AUDIT_THRESHOLD")
if threshold:
    cfg.asset_review.score_threshold = float(threshold)
N._config = lambda: cfg
node = N.BetterComfyUIAssetReview()
out = node.run(
    project_id=os.environ["AUDIT_PROJECT"],
    action=os.environ["AUDIT_ACTION"],
    asset_id=os.environ.get("AUDIT_ASSET", ""),
    version=os.environ.get("AUDIT_VERSION", ""),
    operator="audit-r2", reason="",
)
res = out if isinstance(out, tuple) else out["result"]
state, score_json, lock_info, summary = res
print("AUDIT_RESULT=" + json.dumps(
    {"state": state, "score": json.loads(score_json), "lock_info": lock_info,
     "summary": summary}, ensure_ascii=False))
'''


def _run_worker(tmp_path: Path, project_root: Path, pid: str, action: str, *,
                asset: str = "", version: str = "", threshold: float | None = None) -> dict:
    env = dict(os.environ)
    env.update({
        "AUDIT_PROJECT_ROOT": str(project_root),
        "AUDIT_PROJECT": pid,
        "AUDIT_ACTION": action,
        "AUDIT_ASSET": asset,
        "AUDIT_VERSION": version,
        "PYTHONIOENCODING": "utf-8",
    })
    if threshold is not None:
        env["AUDIT_THRESHOLD"] = str(threshold)
    proc = subprocess.run(
        [sys.executable, "-B", "-c", _WORKER],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    line = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("AUDIT_RESULT=")]
    if not line:
        raise AssertionError(
            f"worker 未输出结果（rc={proc.returncode}）:\nstdout={proc.stdout[-1500:]}\n"
            f"stderr={proc.stderr[-2000:]}")
    return json.loads(line[0].split("=", 1)[1])


# ---------------------------------------------------------------------------
# A1：三轮候选跨进程累积，FALLBACK 选最高分版本
# ---------------------------------------------------------------------------


def test_r1fix_cross_process_candidate_accumulation(tmp_path):
    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    pid = str(ProjectStore(project_root, cfg).create("跨进程审核")["project_id"])
    paths = ProjectPaths(root=project_root, project_id=pid)
    paths.ensure_layout()
    assets = AssetStore(paths)
    asset_id, recs = _add_versions(assets, ["flat", "noise", "dark"], tmp_path)

    results = []
    for rec in recs:  # 3 个独立进程，各自 review 一个版本（对应 K→L 的新版本流）
        results.append(_run_worker(
            tmp_path, project_root, pid, "模型审核（本地启发式）",
            asset=asset_id, version=rec["version"], threshold=99.9))

    state_file = paths.project_dir / "review" / "reviewer_state.json"
    assert state_file.is_file(), "审核状态没有跨进程落盘"
    snapshot = json.loads(state_file.read_text(encoding="utf-8"))
    candidates = snapshot["candidates"][asset_id]
    assert len(candidates) == 3, f"三轮候选没有跨进程累积：{candidates}"
    best = max(candidates, key=lambda c: (float(c["score"]), -int(c["round"])))
    lock = assets.resolve_lock(asset_id, best["version"])
    assert lock is not None and lock["lock_source"] == "FALLBACK_PASS", (
        f"跨进程兜底必须给最高分版本写 FALLBACK_PASS：best={best}, lock={lock}")
    # 非最高分版本不得获得 LOCK（旧结论不得被授权）
    for rec in recs:
        if rec["version"] != best["version"]:
            assert assets.resolve_lock(asset_id, rec["version"]) is None
    # CNT1 跨进程计满 3
    counters = json.loads((paths.project_dir / "counters.json").read_text(encoding="utf-8"))
    cnt1 = [v for k, v in counters["counts"].items() if k.endswith("_CNT1")]
    assert cnt1 == [3], counters


# ---------------------------------------------------------------------------
# A2：人工等待跨进程保留；超时后自动切模型并继续模型轮
# ---------------------------------------------------------------------------


def test_r1fix_cross_process_human_timeout_then_model_round(tmp_path):
    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    pid = str(ProjectStore(project_root, cfg).create("跨进程超时")["project_id"])
    paths = ProjectPaths(root=project_root, project_id=pid)
    paths.ensure_layout()
    assets = AssetStore(paths)
    asset_id, recs = _add_versions(assets, ["flat"], tmp_path)
    version = recs[0]["version"]

    first = _run_worker(tmp_path, project_root, pid, "发起人工审核（等待）",
                        asset=asset_id, version=version)
    assert first["state"] == "AWAITING_REVIEW", first

    state_file = paths.project_dir / "review" / "reviewer_state.json"
    snapshot = json.loads(state_file.read_text(encoding="utf-8"))
    old_ts = "2026-10-18T00:00:00+08:00"
    for window in (snapshot.get("activity") or {}).values():
        window["opened_ts"] = old_ts
        window["last_valid_op_ts"] = None
    state_file.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    second = _run_worker(tmp_path, project_root, pid, "模型审核（本地启发式）",
                         asset=asset_id, version=version, threshold=99.9)
    score = second["score"]
    assert second["state"] != "AWAITING_REVIEW", second
    assert str(score.get("mode")) == "AUTO_SWITCHED", (
        f"未按超时自动切模型：{score}")
    assert int(score.get("cnt1") or 0) >= 1, (
        f"切换后必须真的执行模型轮（CNT1>=1）：{score}")
    assert "人工等待超时" in second["summary"], second["summary"]


# ---------------------------------------------------------------------------
# A3：状态文件损坏 -> 不崩、不冒充旧状态
# ---------------------------------------------------------------------------


def test_r1fix_corrupt_reviewer_state_is_reported_not_silent(tmp_path):
    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    pid = str(ProjectStore(project_root, cfg).create("状态损坏")["project_id"])
    paths = ProjectPaths(root=project_root, project_id=pid)
    paths.ensure_layout()
    assets = AssetStore(paths)
    asset_id, recs = _add_versions(assets, ["flat"], tmp_path)
    review_dir = paths.project_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "reviewer_state.json").write_text("{ 这不是 JSON", encoding="utf-8")

    out = _run_worker(tmp_path, project_root, pid, "模型审核（本地启发式）",
                      asset=asset_id, version=recs[0]["version"], threshold=99.9)
    assert "审核状态恢复失败" in out["summary"], out["summary"]
    assert out["state"] in ("BLOCKED", "ALLOWED", "FALLBACK"), out


# ---------------------------------------------------------------------------
# A4：已 LOCK 后丢失审核状态（模拟崩溃）再跑 -> 不重复提交/不丢结论
# ---------------------------------------------------------------------------


def test_r1fix_replay_after_state_loss_does_not_duplicate_commit(tmp_path):
    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    pid = str(ProjectStore(project_root, cfg).create("重放提交")["project_id"])
    paths = ProjectPaths(root=project_root, project_id=pid)
    paths.ensure_layout()
    assets = AssetStore(paths)
    asset_id, recs = _add_versions(assets, ["noise"], tmp_path)
    version = recs[0]["version"]

    first = _run_worker(tmp_path, project_root, pid, "模型审核（本地启发式）",
                        asset=asset_id, version=version, threshold=1.0)
    lock1 = assets.resolve_lock(asset_id, version)
    if lock1 is None:  # 本地启发式分数低于默认阈值时不算通过，改用兜底不可行：直接跳过
        pytest.skip(f"本机启发式评分未过默认阈值，无法构造 LOCK（state={first['state']}）")
    ledger_file = paths.project_dir / "review" / "finalizations.json"
    before = ledger_file.read_text(encoding="utf-8") if ledger_file.is_file() else "{}"

    (paths.project_dir / "review" / "reviewer_state.json").unlink(missing_ok=True)
    second = _run_worker(tmp_path, project_root, pid, "模型审核（本地启发式）",
                         asset=asset_id, version=version, threshold=1.0)
    lock2 = assets.resolve_lock(asset_id, version)
    after = ledger_file.read_text(encoding="utf-8") if ledger_file.is_file() else "{}"
    assert lock2 is not None, "重放后 LOCK 不应丢失"
    assert lock1["lock_id"] == lock2["lock_id"], "重放不得产生第二个 LOCK"
    assert before == after, "重放不得重复写唯一提交账本（finalizations.json 应无变化）"
    assert second["state"] == "ALLOWED", f"重放应返回既有终态：{second}"


# ---------------------------------------------------------------------------
# B：工作流项目引用可移植性
# ---------------------------------------------------------------------------


def test_r2_project_resolve_reference(tmp_path):
    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    store = ProjectStore(project_root, cfg)
    info1, action1 = store.resolve_reference("我的短剧")
    assert action1 == "CREATED_NAME"
    info2, action2 = store.resolve_reference("我的短剧")
    assert action2 == "EXISTING_NAME"
    assert info1["project_id"] == info2["project_id"]
    info3, action3 = store.resolve_reference(info1["project_id"])
    assert action3 == "EXISTING_ID" and info3["project_id"] == info1["project_id"]
    with pytest.raises(Exception):
        store.resolve_reference("0123456789abcdef0123456789abcdef")


def test_r2_shipped_workflows_have_no_dev_project_id():
    dev_id = "a93ddac1a70046b0b6b8b916e9f8cdbb"
    roots = [
        REPO_ROOT / "bettercomfyui_comfy" / "workflows",
        REPO_ROOT / ".comfyui" / "user" / "default" / "workflows",
    ]
    bad = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.glob("*.json"):
            if dev_id in path.read_text(encoding="utf-8"):
                bad.append(str(path.relative_to(REPO_ROOT)))
    assert not bad, f"发布工作流仍硬编码开发机项目 id：{bad}"


def test_r2_master_flows_use_display_name_project(tmp_path):
    for name in ("00_【总流程】剧本→全部内容（本机可跑）.json",
                 "00_【总流程】剧本→成片（GPU）.json"):
        wf = json.loads((REPO_ROOT / "bettercomfyui_comfy" / "workflows" / name)
                        .read_text(encoding="utf-8"))
        refs = []
        for node in wf["nodes"]:
            for value in node.get("widgets_values") or []:
                if isinstance(value, str) and value == "我的短剧":
                    refs.append(node["type"])
        assert refs, f"{name} 的项目 widget 没有使用显示名（用户换机后跑不通）"
        note = "\n".join(
            value for node in wf["nodes"]
            for value in (node.get("widgets_values") or [])
            if isinstance(value, str) and "这是一套什么工作流" in value)
        assert "项目怎么填" in note, f"{name} 缺少项目填写说明（普通用户看不懂）"


def test_r2_everything_node_resolves_display_name_and_reports(tmp_path):
    from bettercomfyui_comfy import everything_node as E

    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(E, "_load_config", lambda: cfg)
    monkeypatch.setattr(E, "_repo_root", lambda: tmp_path)
    try:
        node = E.BetterComfyUIEverything()
        first = node.run(剧本="第一章：测试", 使用占位结果=True, 项目="我的短剧")
        audit1 = first[4]
        assert "已自动新建项目" in audit1, audit1
        second = node.run(剧本="第一章：测试", 使用占位结果=True, 项目="我的短剧")
        audit2 = second[4]
        assert "解析到已有项目" in audit2, audit2
        projects = ProjectStore(project_root, cfg).list()
        assert len(projects) == 1, f"显示名重复使用不应重复建项目：{projects}"
        bad = node.run(剧本="第一章", 使用占位结果=True,
                       项目="0123456789abcdef0123456789abcdef")
        assert "无法解析" in bad[4], bad[4]
        assert len(ProjectStore(project_root, cfg).list()) == 1, "坏 id 不得静默新建项目"
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# C：LOCK 指纹门禁
# ---------------------------------------------------------------------------


def test_r2_locked_content_tamper_is_rejected(tmp_path):
    from bettercomfyui_comfy import asset_nodes
    from bettercomfyui_comfy import fusion

    cfg, pid, paths, assets = _make_project(tmp_path)
    asset_id, recs = _add_versions(assets, ["noise"], tmp_path)
    version = recs[0]["version"]
    material = {"file_path": recs[0]["file_path"], "content_hash": recs[0]["content_hash"]}
    reviewer = AssetReviewer(
        assets, config=cfg, counters=CounterStore(), project_id=pid, project=paths,
        model_review=lambda m: {"score": 95.0, "passed": True},
    )
    reviewer.review_version(asset_id, version, mode="MODEL", material=material)
    locked = assets.resolve_lock(asset_id, version)
    assert locked is not None, "构造 LOCK 失败"
    locked_path = Path(locked["absolute_path"])

    # 未篡改：素材节点文件校验正常通过
    assert asset_nodes._locked_file(locked, paths) == locked_path

    # 篡改文件内容：必须拒绝（而不是静默用旧指纹加载新内容）
    locked_path.write_bytes(b"tampered-bytes-not-the-approved-content")
    with pytest.raises(asset_nodes.AssetNodeError, match="内容校验失败|指纹"):
        asset_nodes._locked_file(locked, paths)
    # 融合参考图入口：校验发生在读取图像（torch）之前 —— 即使本测试进程没有 torch，
    # 也必须先抛出「指纹不一致」的 FusionError，而不是静默继续。
    with pytest.raises(fusion.FusionError, match="指纹"):
        fusion.locked_asset_ref_images(
            pid, [{"asset_id": asset_id, "version": version}],
            repo_root=REPO_ROOT, config=cfg)


# ---------------------------------------------------------------------------
# D：S4 视频审核对未通过整片不应崩（默认策略未配置）
# ---------------------------------------------------------------------------


def _make_av(tmp_path: Path, *, video_s: float, audio_s: float) -> Path:
    from bettercomfyui.pipeline.core import _ffmpeg_exe

    clip = tmp_path / "clip.mp4"
    cmd = [_ffmpeg_exe(), "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=10:duration={video_s}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_s}",
           "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：ffmpeg 合成失败：{(proc.stderr or '')[-300:]}")
    return clip


def test_r2_video_review_unconfigured_policy_returns_conclusion(tmp_path):
    from bettercomfyui_comfy import nodes as N
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    pid = str(ProjectStore(project_root, cfg).create("S4策略")["project_id"])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(N, "_config", lambda: cfg)
    try:
        clip = _make_av(tmp_path, video_s=8, audio_s=2)  # 音画不同步 -> 整片 < 阈值
        node = BetterComfyUIVideoReview()
        out = node.run(项目=pid, 视频文件路径=str(clip), 每段时长_秒=10.0,
                       审核范围="段级 + 整片（推荐）", 评分来源="本地启发式（真实计算）",
                       段阈值覆盖=-1.0, 整片阈值覆盖=-1.0, 整片失败策略="按项目配置",
                       人工分数JSON="", 本次是级联重做=False, 边界相似度阈值=55.0)
        res = out if isinstance(out, tuple) else out["result"]
        state, seg_json, full_json, need_regen, audit = res
        full = json.loads(full_json)
        assert full["executed"] is True
        assert full["passed"] is False, f"该视频应整片未通过：{full}"
        assert full["policy"] == "unconfigured", (
            f"默认策略未配置时必须如实标注 unconfigured：{full}")
        assert "整片未通过" in audit and "策略未配置" in audit, audit
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# E：CNT2 达到上限后不再新增候选
# ---------------------------------------------------------------------------


def test_r2_video_review_cnt2_caps_at_limit(tmp_path):
    from bettercomfyui_comfy import nodes as N
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    project_root = tmp_path / "projects"
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(project_root)
    pid = str(ProjectStore(project_root, cfg).create("S4计数")["project_id"])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(N, "_config", lambda: cfg)
    try:
        clip = _make_av(tmp_path, video_s=4, audio_s=4)
        node = BetterComfyUIVideoReview()
        counters = []
        for _ in range(5):
            out = node.run(项目=pid, 视频文件路径=str(clip), 每段时长_秒=10.0,
                           审核范围="仅段级 R_seg", 评分来源="本地启发式（真实计算）",
                           段阈值覆盖=100.0, 整片阈值覆盖=-1.0, 整片失败策略="按项目配置",
                           人工分数JSON="", 本次是级联重做=False, 边界相似度阈值=55.0)
            res = out if isinstance(out, tuple) else out["result"]
            counters.append(json.loads(res[1])["segments"][0]["counters"])
        counts = [int(c.get("CNT2") or 0) for c in counters]
        assert counts[:3] == [1, 2, 3], f"前三次应 1/2/3：{counts}"
        assert counts[3:] == [3, 3], f"达到上限后不得继续累加：{counts}"
        assert counters[3].get("capped") is True and counters[4].get("capped") is True
        stored = json.loads((tmp_path / "projects" / pid / "counters.json")
                            .read_text(encoding="utf-8"))
        cnt2 = [v for k, v in stored["counts"].items() if k.endswith("_CNT2")]
        assert cnt2 == [3], f"落盘计数应封顶 3：{stored}"
    finally:
        monkeypatch.undo()

# ---------------------------------------------------------------------------
# F：ComfyUI 节点缓存（IS_CHANGED）——有副作用节点必须每次真正执行
# ---------------------------------------------------------------------------
# 实测缺陷（审计 R2，真机复现）：
#   * 素材取任务：首个素材入库后重复排队仍返回同一素材（缓存命中，不推进）；
#   * 素材入库：同一 IMAGE 重复排队返回 v1，库里没有 v2（缓存命中，没追加）；
#   * 视频审核：同一路径文件被替换成不同内容后，重复排队仍返回旧结论（缓存命中，
#     旧内容的“通过”被当成新内容的结论 = 旧结论失效原则被破坏）。
# ComfyUI 依据节点输入哈希缓存输出；有副作用节点必须声明 IS_CHANGED 绕过缓存，
# 视频审核则必须把“文件真实内容”纳入缓存键。


def test_r2_stateful_nodes_declare_is_changed():
    import inspect

    from bettercomfyui_comfy import fusion_nodes, nodes, review_nodes, unified_director

    expected = {
        nodes.BetterComfyUIProject: "always",
        nodes.BetterComfyUIRun: "always",
        nodes.BetterComfyUIAssetReview: "always",
        nodes.BetterComfyUIManualAsset: "always",
        review_nodes.BetterComfyUIAssetGen: "always",
        review_nodes.BetterComfyUIAssetIngest: "always",
        fusion_nodes.BetterComfyUIFusionIngest: "always",
        fusion_nodes.BetterComfyUIFusionGroups: "always",
        unified_director.BetterComfyUIOneDirector: "always",
        review_nodes.BetterComfyUIVideoReview: "content",
    }
    missing = [cls.__name__ for cls in expected if not hasattr(cls, "IS_CHANGED")]
    assert not missing, f"有副作用节点缺少 IS_CHANGED（会命中 ComfyUI 缓存）：{missing}"
    for cls in expected:
        sig = inspect.signature(cls.IS_CHANGED)
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()), (
            f"{cls.__name__}.IS_CHANGED 必须接受 **kwargs（ComfyUI 按输入名调用）")
    # always 型：两次调用必须不同（否则仍会命中缓存）
    for cls, kind in expected.items():
        if kind != "always":
            continue
        first = cls.IS_CHANGED()
        second = cls.IS_CHANGED()
        assert first != second, f"{cls.__name__}.IS_CHANGED 返回值恒定，仍会被缓存"


def test_r2_video_review_is_changed_tracks_file_content(tmp_path):
    from bettercomfyui.pipeline.core import _ffmpeg_exe
    from bettercomfyui_comfy.review_nodes import BetterComfyUIVideoReview

    clip = tmp_path / "review.mp4"
    cmd = [_ffmpeg_exe(), "-y", "-v", "error",
           "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)]
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        pytest.skip(f"未验证：ffmpeg 合成失败：{(proc.stderr or '')[-300:]}")

    a1 = BetterComfyUIVideoReview.IS_CHANGED(视频文件路径=str(clip), 项目="p", 本次是级联重做=False)
    a2 = BetterComfyUIVideoReview.IS_CHANGED(视频文件路径=str(clip), 项目="p", 本次是级联重做=False)
    assert a1 == a2, "文件未变化时缓存键应稳定（避免无意义重复审核）"
    # 覆盖同一路径为新内容（模拟段重生成到同名文件）
    cmd2 = [_ffmpeg_exe(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=10:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(clip)]
    subprocess.run(cmd2, check=True, capture_output=True)
    b1 = BetterComfyUIVideoReview.IS_CHANGED(视频文件路径=str(clip), 项目="p", 本次是级联重做=False)
    assert a1 != b1, (
        "视频文件内容变化后缓存键必须变化，否则重复排队会复用旧审核结论（旧结论失效被破坏）")
    # 缺失文件：不能恒等（否则节点不执行、错误不暴露）
    c1 = BetterComfyUIVideoReview.IS_CHANGED(视频文件路径=str(tmp_path / "missing.mp4"))
    c2 = BetterComfyUIVideoReview.IS_CHANGED(视频文件路径=str(tmp_path / "missing.mp4"))
    assert c1 != c2
