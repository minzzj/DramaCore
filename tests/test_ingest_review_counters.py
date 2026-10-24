"""回灌节点的**段级审核 + CNT2/CNT3 记账**测试（goal 第 3 项的另一半）。

## 为什么这半边很关键

「回灌」如果只是把文件存下来，那「未审核前不得进入拼接」这条规则就无从执行 ——
谁都不知道审没审、通过没有。所以回灌必须**顺手触发段级审核**。

同时，重做版本必须按语义记账（硬规则 R4）：

* `CNT2` = 本段**自己质量不合格**而重做；
* `CNT3` = **因前段变化**触发本段衔接重做。

混在一起就无法回答"返工是我自己的问题还是被上游带的"。

## 本文件钉住的一个真实 bug

`SegmentStore.add_version` 原本只推进 `candidate_version`，不推进 `current_version`。
审核器要求 `version == segment.current_version`（否则抛 `StaleSegmentVersion`），
于是**每个新版本都无法被审核** —— 实测回灌 v3 时报
"审核对象版本 v3 与段当前版本 v1 不一致"。

而且这条守卫本身是对的：它正是「迟到结果不得覆盖新版本」（验收 9）的实现基础。
所以修法是让新版本成为 current，而不是放宽守卫。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _setup(tmp_path: Path):
    """建一个真实项目，并把**融合节点**指向它。返回 (cfg, pid, root)。

    注意：融合节点内部用的是 `fusion_nodes._cfg()` 解析配置与路径，
    所以要 patch 的是它（patch `nodes._config` 没用 —— 那是另一个函数）。
    """
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("回灌审核测试")["project_id"]

    from bettercomfyui_comfy import fusion_nodes

    fusion_nodes._cfg = lambda repo_root=None: cfg       # noqa: ARG005
    return cfg, pid, root


def _frames(n: int = 6):
    import torch

    return torch.rand(n, 32, 32, 3)


@pytest.fixture()
def node():
    torch = pytest.importorskip("torch")          # noqa: F841 - 无 torch 时跳过
    from bettercomfyui_comfy import fusion_nodes

    return fusion_nodes.BetterComfyUIFusionIngest()


def _fresh_ingest(node, pid, *, retry_kind="自动判断", frames=6):
    return node.ingest(project_id=pid, segment_index=1, images=_frames(frames),
                       fps=24.0, retry_kind=retry_kind)


# ---------------------------------------------------------------------------
# 审核真的被触发
# ---------------------------------------------------------------------------


def test_first_ingest_is_reviewed_and_not_counted(tmp_path: Path, node):
    """首次回灌：必须审核，且**不计** CNT2/CNT3（首次生成不是返工）。"""
    _cfg, pid, root = _setup(tmp_path)
    _aid, _n, report = _fresh_ingest(node, pid)

    assert "首次生成" in report, report
    assert "不计 CNT2/CNT3" in report, report
    assert "审核：" in report, report
    # 首次不应留下任何 CNT2/CNT3 计数
    counters = json.loads((root / pid / "counters.json").read_text(encoding="utf-8"))
    assert counters["counts"] == {}, f"首次生成不该计 CNT2/CNT3：{counters}"


def test_review_verdict_is_written_back_to_segment(tmp_path: Path, node):
    """审核结论必须写回段存储 —— 否则"审过了"只存在于当次输出。"""
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.segments import SegmentStore

    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)

    seg = SegmentStore(ProjectPaths(root=root, project_id=pid)).load_or_raise("SEG0001")
    assert seg.review_status is not None, "审核状态没有写回段"
    assert seg.review_result is not None, "审核结果没有写回段"
    assert seg.review_result.get("audit_id"), seg.review_result
    assert "score" in seg.review_result


def test_review_material_records_verdict(tmp_path: Path, node):
    """审核材料 JSON 里也要有审核结论（人直接打开就能看到）。"""
    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)
    m = json.loads(
        (root / pid / "ingest" / "SEG0001" / "review_material.json")
        .read_text(encoding="utf-8")
    )
    assert m.get("review"), f"审核材料里没有审核结论：{sorted(m)}"
    assert m.get("counters") is not None
    assert m["review_state"] in ("ALLOWED", "BLOCKED", "PENDING_REVIEW"), m["review_state"]


# ---------------------------------------------------------------------------
# CNT2 / CNT3 语义分离（R4）
# ---------------------------------------------------------------------------


def test_quality_retry_increments_cnt2_only(tmp_path: Path, node):
    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)                                    # v1 首次
    _aid, _n, report = node.ingest(
        project_id=pid, segment_index=1, images=_frames(7), fps=24.0,
        retry_kind="质量重做(CNT2)",
    )
    assert "CNT2" in report and "质量重做" in report, report
    counters = json.loads((root / pid / "counters.json").read_text(encoding="utf-8"))
    assert counters["counts"].get("CNT_SEG0001_CNT2") == 1, counters
    assert "CNT_SEG0001_CNT3" not in counters["counts"], "质量重做不该动 CNT3"


def test_cascade_retry_increments_cnt3_only(tmp_path: Path, node):
    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)
    _aid, _n, report = node.ingest(
        project_id=pid, segment_index=1, images=_frames(7), fps=24.0,
        retry_kind="级联重做(CNT3)",
    )
    assert "CNT3" in report and "级联重做" in report, report
    counters = json.loads((root / pid / "counters.json").read_text(encoding="utf-8"))
    assert counters["counts"].get("CNT_SEG0001_CNT3") == 1, counters
    assert "CNT_SEG0001_CNT2" not in counters["counts"], "级联重做不该动 CNT2"


def test_cnt2_and_cnt3_stay_separate(tmp_path: Path, node):
    """R4 的核心：两个计数器**互不混淆**、各自独立累计。"""
    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)
    node.ingest(project_id=pid, segment_index=1, images=_frames(7), fps=24.0,
                retry_kind="质量重做(CNT2)")
    node.ingest(project_id=pid, segment_index=1, images=_frames(8), fps=24.0,
                retry_kind="质量重做(CNT2)")
    node.ingest(project_id=pid, segment_index=1, images=_frames(9), fps=24.0,
                retry_kind="级联重做(CNT3)")

    counters = json.loads((root / pid / "counters.json").read_text(encoding="utf-8"))
    assert counters["counts"]["CNT_SEG0001_CNT2"] == 2, counters
    assert counters["counts"]["CNT_SEG0001_CNT3"] == 1, counters


def test_auto_mode_uses_cascade_flag(tmp_path: Path, node):
    """「自动判断」按段的级联标记决定 CNT3 还是 CNT2。"""
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.segments import SegmentStore

    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)

    paths = ProjectPaths(root=root, project_id=pid)
    st = SegmentStore(paths)
    seg = st.load_or_raise("SEG0001")
    seg.cascade_marked = True          # 标记为"因上游变化待复核"
    st.update(seg)

    node.ingest(project_id=pid, segment_index=1, images=_frames(7), fps=24.0,
                retry_kind="自动判断")
    counters = json.loads((root / pid / "counters.json").read_text(encoding="utf-8"))
    assert counters["counts"].get("CNT_SEG0001_CNT3") == 1, (
        f"cascade_marked=True 时应记 CNT3，实际 {counters}"
    )


def test_counters_survive_reload(tmp_path: Path, node):
    """计数必须落盘：重启后返工次数不能丢。"""
    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)
    node.ingest(project_id=pid, segment_index=1, images=_frames(7), fps=24.0,
                retry_kind="质量重做(CNT2)")
    # 直接读文件（模拟"另一个进程/重启后再看"）
    counters = json.loads((root / pid / "counters.json").read_text(encoding="utf-8"))
    assert counters["counts"]["CNT_SEG0001_CNT2"] == 1


# ---------------------------------------------------------------------------
# current_version 必须随新版本推进（否则新版本永远审不了）
# ---------------------------------------------------------------------------


def test_new_version_becomes_current_so_it_can_be_reviewed(tmp_path: Path):
    """`add_version` 必须推进 `current_version`。

    审核器要求 `version == segment.current_version`。只推进 `candidate_version`
    会让每个新版本都被判为 stale 而无法审核（实测踩到的 bug）。
    反过来，正是"新版本成为 current"让旧版的审核结论立刻失效，
    从而实现「迟到结果不得覆盖新版本」（验收 9）。
    """
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.segments import SegmentStore

    _cfg, pid, root = _setup(tmp_path)
    from bettercomfyui.models.entities import Segment

    paths = ProjectPaths(root=root, project_id=pid)
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="INGEST"))
    assert st.load_or_raise("SEG0001").current_version == "v1"

    st.add_version("SEG0001", version="v2", content_hash="sha256:" + "a" * 64)
    seg = st.load_or_raise("SEG0001")
    assert seg.current_version == "v2", (
        f"新版本没有成为 current_version（={seg.current_version}），"
        "审核器会以 StaleSegmentVersion 拒绝它"
    )
    assert seg.candidate_version == "v2"


def test_stale_review_is_still_rejected_after_new_version(tmp_path: Path, node):
    """反向断言（验收 9）：旧版本的审核结论**仍然必须**被拒绝。

    怕的是为了修 bug 而放宽守卫 —— 那会让迟到结果覆盖新版本。
    """
    from bettercomfyui.director.errors import StaleSegmentVersion
    from bettercomfyui.director.review import SegmentReviewer
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.segments import SegmentStore

    cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid, frames=6)                 # v1
    node.ingest(project_id=pid, segment_index=1, images=_frames(7),
                fps=24.0, retry_kind="质量重做(CNT2)")   # v2

    st = SegmentStore(ProjectPaths(root=root, project_id=pid))
    seg = st.load_or_raise("SEG0001")
    assert seg.current_version == "v2"

    reviewer = SegmentReviewer(
        cfg=cfg, project_id=pid,
        scorer=lambda s, v: {d: 90.0 for d in cfg.video_review.r1a_dimensions},
    )
    with pytest.raises(StaleSegmentVersion):
        reviewer.review(seg, "v1", material={"dimensions": {
            d: 90.0 for d in cfg.video_review.r1a_dimensions}})


def test_review_failure_does_not_fake_pass(tmp_path: Path, node, monkeypatch):
    """审核失败时必须明说"未完成"，**绝不能**当成通过放行。"""
    _cfg, pid, root = _setup(tmp_path)
    _fresh_ingest(node, pid)

    import bettercomfyui.director.review as rv

    def boom(self, segment, version, *, material=None):
        raise RuntimeError("模拟评分器崩溃")

    monkeypatch.setattr(rv.SegmentReviewer, "review", boom)

    _aid, _n, report = node.ingest(
        project_id=pid, segment_index=1, images=_frames(7), fps=24.0,
        retry_kind="质量重做(CNT2)",
    )
    assert "未完成" in report, report
    assert "不得进入拼接" in report, report
    assert "通过" not in report.replace("未通过", ""), (
        f"审核崩了却像通过：{report}"
    )


def test_review_can_be_skipped_explicitly(tmp_path: Path, node):
    """显式跳过审核时必须**说明**跳过（不能静默假装审过了）。"""
    _cfg, pid, root = _setup(tmp_path)
    _aid, _n, report = node.ingest(
        project_id=pid, segment_index=1, images=_frames(6), fps=24.0,
        review_and_count=False,
    )
    assert "跳过审核" in report, report
    assert "不得拼接" in report, report
