"""验收 5：新素材生成后创建版本和内容指纹；旧版本保留。

原文（任务书第 9 节第 5 条，第 187 行）：
    「新素材生成后创建版本和内容指纹；旧版本保留。」

需要模块：bettercomfyui.store.assets（AssetStore）、bettercomfyui.store.ids（content_hash_of / next_version）
依据：CONTRACTS.md 第 2、4 节；R2「素材库只追加不覆盖」；方案第七部分一（SHA-256）。

判定通过：
    P1 首次入库 -> version == "v1"，content_hash == "sha256:" + 文件实际字节 sha256；
    P2 再次入库 -> version == "v2"，两个版本目录同时存在；
    P3 v1 的素材文件字节级未被改动（旧版本保留，R2）；
    P4 v2 的 content_hash 与 v1 不同（内容指纹随内容变化）。

判定假通过（必须能变红）：
    F1 覆盖式写入（v2 覆盖 v1）—— P3 逐字节比对会变红，且 v1 路径消失会变红；
    F2 content_hash 用文件名/时间戳算而不是内容 —— P4 + 同内容同哈希判据会变红；
    F3 content_hash 不带 "sha256:" 前缀或不是 64 位小写 hex —— P1 格式断言会变红。
"""

from __future__ import annotations

import hashlib
import re

import pytest

from _api import asset_id_of, create_asset, create_project, make_asset_file
from _harness import contract_attr, load, make_config, sha256_of, tree_digest

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256_like(path) -> str:
    return "sha256:" + hashlib.sha256(open(path, "rb").read()).hexdigest()


def _setup(tmp_path):
    cfg = make_config()
    ha, _store, _root = create_project(tmp_path, "版本项目", cfg)
    ea = create_asset(ha, name="主场景", asset_type="SCENE")
    return ha, asset_id_of(ea)


def test_first_version_has_content_hash(tmp_path):
    """P1：v1 + 内容指纹格式与内容一致。"""
    ha, aid = _setup(tmp_path)
    f1 = make_asset_file(tmp_path, "v1.png", b"CONTENT-ONE")
    v1 = ha.assets.add_version(aid, source="AI", file_path=f1, status="DRAFT")

    assert str(v1.get("version")) == "v1", f"首个版本必须是 v1，实际 {v1.get('version')!r}"
    ch = str(v1.get("content_hash", ""))
    assert HASH_RE.match(ch), f"content_hash 必须是 sha256:{{64位小写hex}}，实际 {ch!r}"
    assert ch == _sha256_like(f1), (
        f"content_hash 必须按文件实际字节计算：期望 {_sha256_like(f1)}，实际 {ch}"
    )
    assert v1.get("file_path"), "版本记录必须带 file_path"


def test_new_version_keeps_old_version_intact(tmp_path):
    """P2/P3/P4：v2 追加后 v1 必须原封不动。"""
    ha, aid = _setup(tmp_path)
    f1 = make_asset_file(tmp_path, "v1.png", b"CONTENT-ONE")
    f2 = make_asset_file(tmp_path, "v2.png", b"CONTENT-TWO-DIFFERENT")

    v1 = ha.assets.add_version(aid, source="AI", file_path=f1, status="DRAFT")
    vdir = ha.versions_dir("SCENE", aid)
    v1_files = [p for p in (vdir / "v1").rglob("*") if p.is_file()]
    assert v1_files, f"v1 版本目录必须有素材文件，实际 {vdir} 下：{list(vdir.rglob('*'))}"
    before = {str(p.relative_to(vdir)): sha256_of(p) for p in v1_files}

    v2 = ha.assets.add_version(aid, source="AI", file_path=f2, status="DRAFT")
    assert str(v2.get("version")) == "v2", f"第二个版本必须是 v2，实际 {v2.get('version')!r}"

    # P2：两个版本目录同时存在
    assert (vdir / "v1").is_dir(), "R2 违背：v1 目录消失（被覆盖或被删除）"
    assert (vdir / "v2").is_dir(), "v2 目录必须存在"

    # P3：v1 文件字节级未变
    after = {str(p.relative_to(vdir)): sha256_of(p) for p in (vdir / "v1").rglob("*") if p.is_file()}
    assert set(before) <= set(after), f"R2 违背：v1 下文件丢失 {sorted(set(before) - set(after))}"
    changed = [k for k in before if before[k] != after.get(k)]
    assert not changed, f"R2 违背：v1 素材文件被原地覆盖：{changed}"

    # P4：指纹随内容变化
    assert v2.get("content_hash") != v1.get("content_hash"), "不同内容必须得到不同 content_hash"

    # 版本列表可追溯
    got1 = ha.assets.get_version(aid, "v1")
    got2 = ha.assets.get_version(aid, "v2")
    assert got1 and got2, f"get_version 必须能取回历史版本，实际 v1={got1} v2={got2}"
    assert got1["content_hash"] == v1["content_hash"], "v1 的 content_hash 被后续写入改动了"


def test_same_content_same_hash(tmp_path):
    """F2 加强：内容相同 -> 指纹相同（防止用时间戳/随机数冒充内容指纹）。"""
    ha, aid = _setup(tmp_path)
    payload = b"IDENTICAL-BYTES"
    fa = make_asset_file(tmp_path, "x1.bin", payload)
    fb = make_asset_file(tmp_path, "x2.bin", payload)
    va = ha.assets.add_version(aid, source="AI", file_path=fa, status="DRAFT")
    vb = ha.assets.add_version(aid, source="AI", file_path=fb, status="DRAFT")
    assert va["content_hash"] == vb["content_hash"], (
        "相同内容必须得到相同 content_hash（指纹必须来自文件字节）"
    )
    assert va["version"] != vb["version"], "两次入库必须是不同版本"


def test_content_hash_of_matches_disk(tmp_path):
    """P1 加强：store.ids.content_hash_of 与磁盘字节一致，且不随路径变化。"""
    ids = load("bettercomfyui.store.ids")
    fn = contract_attr(ids, "content_hash_of")
    p1 = make_asset_file(tmp_path, "a.bin", b"same-bytes")
    p2 = make_asset_file(tmp_path, "b.bin", b"same-bytes")
    h1, h2 = fn(p1), fn(p2)
    assert HASH_RE.match(str(h1)), f"格式必须是 sha256:{{64位小写hex}}，实际 {h1!r}"
    assert h1 == h2, "同一内容不同路径必须得到同一指纹"
    assert h1 == _sha256_like(p1)
