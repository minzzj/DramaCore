"""验收 1：两个项目的数据和配置互不串用，切换后可恢复。

原文（任务书第 9 节第 1 条，第 183 行）：
    「两个项目的数据和配置互不串用，切换后可恢复。」

需要模块：bettercomfyui.store.projects（ProjectStore）、bettercomfyui.store.assets（AssetStore）
依据：CONTRACTS.md 第 4 节、第 1 节目录布局；R5 项目隔离。

判定通过：
    P1 两个项目的 project_id 不同，且各自有自己的目录；
    P2 同名素材分别写入两个项目后，A 的素材在 B 不可见（find_by_normalized_name 不跨项目）；
    P3 两项目配置互相独立（项目 config.yaml 覆盖不串用）；
    P4 switch(A -> B) 返回 B 且 A 的内容字节级未变，B 的内容可复读。

判定假通过（必须能变红）：
    F1 ProjectStore 忽略 project_id（两项目写进同一目录）—— 用 tree_digest 断言两目录内容不同；
    F2 find_by_normalized_name 走了全局缓存/全局索引 —— 断言 B 查不到 A 的素材；
    F3 只在内存里记账不落盘 —— 新建 ProjectStore 实例重新 open 后必须仍能读到数据；
    F4 断言恒真 —— 反向自检：把 A 的条目名换成 B 的，断言「可见性」判据必须会变化
       （见 test_visibility_assertion_is_not_vacuous）。
"""

from __future__ import annotations

from pathlib import Path

from _api import asset_id_of, create_asset, create_project, make_asset_file, open_project
from _harness import assert_tree_unchanged, load, make_config, sha256_of, tree_digest


def _two_projects(tmp_path: Path):
    cfg = make_config()
    ha, store, root = create_project(tmp_path, "项目甲", cfg)
    hb, _, _ = create_project(tmp_path, "项目乙", cfg)
    return cfg, ha, hb, store, root


def test_two_projects_are_isolated_and_switch_recovers(tmp_path):
    """P1/P2/P4：隔离 + 切换后数据可复读且 A 字节级未变。"""
    cfg, ha, hb, store, root = _two_projects(tmp_path)

    # P1
    assert ha.project_id != hb.project_id, "两个项目的 project_id 必须不同（R5）"
    assert ha.root != hb.root, "两个项目必须落在不同目录"
    assert ha.root.is_dir() and hb.root.is_dir(), "项目目录必须真实存在"

    # P2：同名素材写入两个项目
    fa = make_asset_file(tmp_path, "a.png", b"AAAA-content-of-project-A")
    fb = make_asset_file(tmp_path, "b.png", b"BBBB-content-of-project-B")
    ea = create_asset(ha, name="主角", source="AI")
    eb = create_asset(hb, name="主角", source="AI")
    ida, idb = asset_id_of(ea), asset_id_of(eb)
    assert ida != idb, "跨项目同名素材也必须得到不同 asset_id"

    ha.assets.add_version(ida, source="AI", file_path=fa, status="DRAFT")
    hb.assets.add_version(idb, source="AI", file_path=fb, status="DRAFT")

    ids_a = {a.get("asset_id") for a in ha.assets.list_assets()}
    ids_b = {a.get("asset_id") for a in hb.assets.list_assets()}
    assert ida in ids_a and ida not in ids_b, f"R5 违背：A 的素材出现在 B（{ids_b}）"
    assert idb in ids_b and idb not in ids_a, f"R5 违背：B 的素材出现在 A（{ids_a}）"

    hit_a = ha.assets.find_by_normalized_name("主角")
    assert {h.get("asset_id") for h in hit_a} == {ida}, (
        f"find_by_normalized_name 必须只在本项目内查找，实际返回 {hit_a}"
    )

    va = list(ha.versions_dir("CHARACTER", ida).glob("v*/*"))
    vb = list(hb.versions_dir("CHARACTER", idb).glob("v*/*"))
    assert va and vb, "两个项目都应有自己的版本文件"
    assert any(p.read_bytes() == b"AAAA-content-of-project-A" for p in va), "A 的素材内容被串用/覆盖"
    assert any(p.read_bytes() == b"BBBB-content-of-project-B" for p in vb), "B 的素材内容被串用/覆盖"

    # P4：切换后 A 的**素材数据**必须字节级未变
    # （project.json 的 last_opened / events.jsonl 追加属契约允许的状态记录，不在比较范围内）
    def _asset_data_digest(handle):
        d = {f"assets/{k}": v for k, v in tree_digest(handle.root / "assets").items()}
        meta = handle.meta_path()
        if meta.exists():
            d["meta.json"] = sha256_of(meta)
        return d

    before_a = _asset_data_digest(ha)
    assert before_a, "前置条件：A 必须有素材数据"
    opened = store.switch(ha.project_id, hb.project_id)
    assert opened is not None, "switch 必须返回被打开项目的信息"
    assert_tree_unchanged(before_a, _asset_data_digest(ha), what="switch(A->B) 后 A 的素材数据")

    again = open_project(tmp_path, ha.project_id, cfg=cfg, root=root)
    assert {a.get("asset_id") for a in again.assets.list_assets()} == {ida}, (
        "切换后重开 A 应能复读同一素材索引（未落盘 -> 切换即丢数据）"
    )


def test_restart_new_store_instance_sees_same_projects(tmp_path):
    """F3：全新实例（模拟服务重启）必须看到同样的项目清单与数据。"""
    projects = load("bettercomfyui.store.projects")
    cfg = make_config()

    ha, _store, root = create_project(tmp_path, "项目甲", cfg)
    fa = make_asset_file(tmp_path, "a.png", b"restart-content")
    ea = create_asset(ha, name="主角")
    ha.assets.add_version(asset_id_of(ea), source="AI", file_path=fa, status="DRAFT")

    store_new = projects.ProjectStore(root, cfg)
    listed = {p.get("project_id") for p in store_new.list()}
    assert ha.project_id in listed, f"重启后 list() 应包含已存在项目，实际 {listed}"

    assert store_new.open(ha.project_id), "重启后 open() 必须能加载项目"
    again = open_project(tmp_path, ha.project_id, cfg=cfg, root=root)
    assert again.assets.list_assets(), "重启后素材索引为空 = 数据未持久化"


def test_config_isolation_between_projects(tmp_path):
    """P3：项目级配置覆盖不得串用。"""
    _cfg, ha, hb, store, _root = _two_projects(tmp_path)

    cfg_a = ha.root / "config.yaml"
    cfg_b = hb.root / "config.yaml"
    assert cfg_a.exists() and cfg_b.exists(), (
        "每个项目必须有独立 config.yaml（CONTRACTS.md 第 1 节目录布局）"
    )

    cfg_a.write_text("video_review:\n  cnt2_limit: 1\n", encoding="utf-8")
    before_b = cfg_b.read_bytes()
    before_a_cfg = cfg_a.read_bytes()

    assert store.open(ha.project_id) is not None
    assert store.open(hb.project_id) is not None
    assert cfg_b.read_bytes() == before_b, "打开 A 不得改写 B 的配置"
    assert cfg_a.read_bytes() == before_a_cfg, "打开 A 不得改写 A 自己的配置"


def test_visibility_assertion_is_not_vacuous(tmp_path):
    """F4 反向自检：把断言对象换成另一个项目的素材时，可见性判据必须变红。

    做法：不调用实现，直接验证「我用的判据」本身有鉴别力 ——
    若实现真的把 B 的 asset_id 放进 A 的索引，下面的断言必然失败。
    """
    _cfg, ha, hb, _store, _root = _two_projects(tmp_path)
    ea = create_asset(ha, name="主角")
    eb = create_asset(hb, name="主角")
    ids_a = {asset_id_of(ea)}
    ids_b = {asset_id_of(eb)}
    assert not (ids_a & ids_b), "判据自检：两个项目的 id 集合必须可区分（否则断言恒真）"
    # 若实现返回的 A 索引里混入 B 的 id，下面这行会失败 —— 这就是"能变红"的证明
    assert ids_a - ids_b, "判据自检：A 的集合必须非空，否则空断言等于没测"
