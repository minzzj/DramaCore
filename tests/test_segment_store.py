"""段版本持久化（`store/segments.py`）与融合回灌落盘的回归测试。

## 这个 store 补的是一个真实缺口

`store/paths.py` 早就声明并创建了 `segments/` 目录，但**没有任何代码往里写** ——
段只活在内存里。后果直接命中两条契约：

* 任务书第 9 节验收 12「服务重启后从检查点恢复，**不重复创建版本**」
  —— 没落盘就没有"已有版本"可查；
* 第 3 节 F 规则 4「新版本必须重新审核；旧版结论只对旧版本有效」
  —— 重启后不知道哪些版本已审过。

所以这里钉住：追加语义、幂等、指纹冲突、持久化往返、以及**回灌真的落盘**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.config import load_config  # noqa: E402
from bettercomfyui.models.entities import Segment, SegmentOverrides  # noqa: E402
from bettercomfyui.store.paths import ProjectPaths  # noqa: E402
from bettercomfyui.store.projects import ProjectStore  # noqa: E402
from bettercomfyui.store.segments import SegmentStore, SegmentStoreError  # noqa: E402


@pytest.fixture()
def project(tmp_path: Path):
    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    pid = ProjectStore(tmp_path / "projects", cfg).create("段存储测试")["project_id"]
    paths = ProjectPaths(root=tmp_path / "projects", project_id=pid)
    paths.ensure_layout()
    return paths, pid


# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------


def test_empty_store_has_no_segments(project):
    paths, _ = project
    st = SegmentStore(paths)
    assert st.list_segments() == []
    assert st.summary() == {
        "segment_count": 0, "version_count": 0,
        "segments_with_seed": 0, "segment_ids": [],
    }


def test_save_and_load_round_trip(project):
    paths, _ = project
    st = SegmentStore(paths)
    seg = Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001",
                  prompt="测试", seed=42, note="备注也要留住")
    st.save(seg)

    back = st.load_or_raise("SEG0001")
    assert back.segment_id == "SEG0001"
    assert back.prompt == "测试"
    assert back.seed == 42
    assert back.note == "备注也要留住", "note 没落盘 —— 来源说明会丢"


def test_load_missing_returns_none_not_error(project):
    paths, _ = project
    assert SegmentStore(paths).load("NOPE") is None


def test_add_version_before_segment_exists_is_rejected(project):
    """不许凭空建版本 —— 版本必须归属到具体段。"""
    paths, _ = project
    with pytest.raises(SegmentStoreError, match="不存在"):
        SegmentStore(paths).add_version("GHOST", version="v1",
                                        content_hash="sha256:" + "a" * 64)


def test_add_version_appends_and_sets_candidate(project):
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))

    st.add_version("SEG0001", version="v2", content_hash="sha256:" + "b" * 64)
    seg = st.load_or_raise("SEG0001")
    assert [a.version for a in seg.attempts] == ["v2"]
    assert seg.candidate_version == "v2"


# ---------------------------------------------------------------------------
# 幂等与冲突（验收 12 的核心）
# ---------------------------------------------------------------------------


def test_same_version_same_hash_is_idempotent(project):
    """**重启重跑不得重复建版本**（任务书第 9 节验收 12）。

    同版本同指纹再追加一次必须是无操作，而不是产生第二个版本。
    """
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    h = "sha256:" + "c" * 64

    st.add_version("SEG0001", version="v1", content_hash=h)
    st.add_version("SEG0001", version="v1", content_hash=h)   # 重跑
    st.add_version("SEG0001", version="v1", content_hash=h)   # 再重跑

    assert [a.version for a in st.load_or_raise("SEG0001").attempts] == ["v1"]


def test_same_version_different_hash_is_rejected(project):
    """同版本号配不同内容 = 数据损坏，**必须报错而不是覆盖**。

    静默覆盖会让旧审核结论"看起来"仍然有效 —— 这是最危险的一类错误。
    """
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    st.add_version("SEG0001", version="v1", content_hash="sha256:" + "d" * 64)

    with pytest.raises(SegmentStoreError, match="指纹"):
        st.add_version("SEG0001", version="v1", content_hash="sha256:" + "e" * 64)

    # 原版本必须原封不动
    assert st.load_or_raise("SEG0001").attempts[0].content_hash == "sha256:" + "d" * 64


def test_different_version_same_hash_is_allowed(project):
    """反向断言：换版本号就允许（内容相同但版本不同是合法的重跑）。"""
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    h = "sha256:" + "f" * 64
    st.add_version("SEG0001", version="v1", content_hash=h)
    st.add_version("SEG0001", version="v2", content_hash=h)
    assert [a.version for a in st.load_or_raise("SEG0001").attempts] == ["v1", "v2"]


def test_attempt_index_increments(project):
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    for i in range(1, 4):
        st.add_version("SEG0001", version=f"v{i}", content_hash=f"sha256:{'0' * 63}{i}")
    idx = [a.index for a in st.load_or_raise("SEG0001").attempts]
    assert idx == [1, 2, 3], f"attempt index 必须递增，实际 {idx}"


# ---------------------------------------------------------------------------
# 持久化往返（新实例能读回 = 真的落盘了）
# ---------------------------------------------------------------------------


def test_new_store_instance_reads_back(project):
    paths, pid = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    st.add_version("SEG0001", version="v1", content_hash="sha256:" + "1" * 64,
                   used_seed=12345)

    fresh = SegmentStore(ProjectPaths(root=paths.root, project_id=pid))
    seg = fresh.load_or_raise("SEG0001")
    assert [a.version for a in seg.attempts] == ["v1"]
    assert seg.attempts[0].used_seed == 12345, "种子必须随版本落盘（可复现的前提）"


def test_overrides_survive_persistence(project):
    """`overrides` 反序列化后必须仍是 SegmentOverrides。

    `_from_dict` 只做 dataclass 字段赋值，不会递归构造嵌套 dataclass。
    漏了这一步，`seg.overrides.resolve(cfg)` 会在**重启后**才崩 ——
    而内存里新建的段是好的，属最难查的一类不一致。
    """
    paths, pid = project
    st = SegmentStore(paths)
    seg = Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001")
    seg.overrides = SegmentOverrides(threshold=88.0, reason="持久化往返")
    st.save(seg)

    fresh = SegmentStore(ProjectPaths(root=paths.root, project_id=pid))
    back = fresh.load_or_raise("SEG0001")
    assert isinstance(back.overrides, SegmentOverrides), (
        f"overrides 反序列化成了 {type(back.overrides).__name__}，不是 SegmentOverrides"
    )
    cfg = load_config(repo_root=REPO_ROOT)
    eff, changed = back.overrides.resolve(cfg)      # 反序列化后仍可解析
    assert eff["threshold"] == 88.0
    assert changed == ["threshold"]


def test_update_does_not_create_new_segment(project):
    """`update()` 不得被用来新建段（否则绕过了计划阶段）。"""
    paths, _ = project
    with pytest.raises(SegmentStoreError, match="拒绝 update"):
        SegmentStore(paths).update(
            Segment(segment_id="GHOST", segment_index=1, chapter_id="CH001")
        )


def test_list_segments_sorted_by_index(project):
    paths, _ = project
    st = SegmentStore(paths)
    for i, sid in ((3, "SEG0003"), (1, "SEG0001"), (2, "SEG0002")):
        st.save(Segment(segment_id=sid, segment_index=i, chapter_id="CH001"))
    assert [s.segment_id for s in st.list_segments()] == ["SEG0001", "SEG0002", "SEG0003"]


def test_summary_counts_seeds_from_attempts(project):
    """可复现性统计必须把**候选上的 used_seed** 算进去。

    回灌路径把种子记在 `SegmentAttempt.used_seed`；
    只数 `Segment.seed` 会得出"没有一个段可复现"的错误结论（实测踩到）。
    """
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    st.add_version("SEG0001", version="v1", content_hash="sha256:" + "2" * 64,
                   used_seed=999)
    s = st.summary()
    assert s["segment_count"] == 1
    assert s["version_count"] == 1
    assert s["segments_with_seed"] == 1, (
        f"候选上有 used_seed 却统计为不可复现：{s}"
    )


def test_corrupt_segment_file_raises_not_silent(project):
    """损坏的段文件必须**报错**，不能静默返回"没有这个段"。

    静默返回 None 的后果很具体：调用方会以为"段还不存在"从而**新建**一个，
    把真实数据覆盖掉。所以这里要求明确抛错。
    """
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    st.path_for("SEG0001").write_text('{"broken": ', encoding="utf-8")

    with pytest.raises(Exception) as ei:
        st.load("SEG0001")
    # 不能是把损坏当"不存在"（那就返回 None 了，不会抛错）
    assert ei.value is not None
    assert not isinstance(ei.value, AttributeError), (
        f"抛出了无关异常，说明错误路径没处理好：{type(ei.value).__name__}: {ei.value}"
    )


def test_zero_byte_segment_file_is_corrupt_not_missing(project):
    """0 字节文件同样算损坏（与资产库的既有纪律一致）。"""
    paths, _ = project
    st = SegmentStore(paths)
    st.save(Segment(segment_id="SEG0001", segment_index=1, chapter_id="CH001"))
    st.path_for("SEG0001").write_bytes(b"")
    with pytest.raises(Exception):
        st.load("SEG0001")
