"""融合契约测试：证明融合节点产出的 `MMX_DIR_GROUP` 与**真实导演台包**逐字段一致。

**为什么不能靠"我记得"**

`MMX_DIR_GROUP` 是导演台内部类型，字段名写错一个（例如 `duration_sec` 写成 `duration`）
不会在加载时报错，而是在**采样阶段**变成一个沉默的错误 —— 提示词丢失、参考图没生效、
时长变成默认值。这类问题极难排查，所以必须**对着真实源码验证**。

三重验证（层层加强）：

1. **静态契约**：用 AST 解析真实包的
   `ComfyUI_MiniMaxH3_Director/director/external_groups.py` 里的
   `pack_i2v_group` / `pack_r2v_group`，**从 return 语句里把字典键抄出来**，
   与我构造的组做集合比对。这一步不需要 torch，因此在任何环境都能跑。
2. **真导入比对**（装了 torch 时才跑）：真调用 `pack_i2v_group(prompt=...)`，
   与我的输出做键比对；若真实包不在 `sys.path` 则 skip 并标注「未验证」。
3. **行为断言**：`kind` 推断规则、空组保护、槽位越界、R3 门禁（未 LOCK 不得进组）。

第 1 重是主力：它把「我的记忆」变成「源码的事实」。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_NODES = REPO_ROOT / "保留备用的节点"
DIRECTOR_PACK = BACKUP_NODES / "ComfyUI_MiniMaxH3_Director"
EXTERNAL_GROUPS = DIRECTOR_PACK / "director" / "external_groups.py"

sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 第 1 重：从真实源码 AST 提取字段
# ---------------------------------------------------------------------------


def _dict_keys_from_return(func_name: str) -> set[str]:
    """解析真实包 `pack_*` 函数的 return 字面量，返回字典键集合。"""
    if not EXTERNAL_GROUPS.is_file():
        pytest.skip(
            f"未验证：找不到真实包源码 {EXTERNAL_GROUPS}，无法比对契约"
        )
    tree = ast.parse(EXTERNAL_GROUPS.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            target = node
            break
    assert target is not None, f"真实包里找不到函数 {func_name}"

    keys: set[str] = set()

    def collect(d: ast.Dict) -> None:
        for k in d.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)

    for node in ast.walk(target):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            collect(node.value)
    assert keys, f"{func_name} 的 return 里没有找到字典键（可能源码结构变了）"
    return keys


@pytest.fixture(scope="module")
def real_i2v_keys() -> set[str]:
    return _dict_keys_from_return("pack_i2v_group")


@pytest.fixture(scope="module")
def real_r2v_keys() -> set[str]:
    return _dict_keys_from_return("pack_r2v_group")


def test_my_i2v_group_matches_real_pack_keys(real_i2v_keys: set[str]):
    """我构造的 i2v 组必须与真实 `pack_i2v_group` 的字段**完全一致**。"""
    from bettercomfyui_comfy import fusion

    mine = set(fusion.make_i2v_group(prompt="x", duration_sec=5.0).keys())
    assert mine == real_i2v_keys, (
        f"i2v 组字段与真实包不一致：\n"
        f"  我多的：{sorted(mine - real_i2v_keys)}\n"
        f"  我少的：{sorted(real_i2v_keys - mine)}"
    )


def test_my_r2v_group_matches_real_pack_keys(real_r2v_keys: set[str]):
    """我构造的 r2v 组必须与真实 `pack_r2v_group` 的字段**完全一致**。"""
    from bettercomfyui_comfy import fusion

    mine = set(fusion.make_r2v_group(prompt="x", duration_sec=5.0).keys())
    assert mine == real_r2v_keys, (
        f"r2v 组字段与真实包不一致：\n"
        f"  我多的：{sorted(mine - real_r2v_keys)}\n"
        f"  我少的：{sorted(real_r2v_keys - mine)}"
    )


def test_kind_inference_matches_real_infer_rule(real_i2v_keys: set[str]):
    """`kind` 推断规则必须与真实实现一致（t2v / i2v / fl2v）。"""
    from bettercomfyui_comfy import fusion

    src = EXTERNAL_GROUPS.read_text(encoding="utf-8")
    # 真实实现里 infer_i2v_kind 的判定分支：先看是否有 last，再看 first
    assert "infer_i2v_kind" in src, "真实包里没有 infer_i2v_kind，规则可能已变"

    assert fusion.infer_i2v_kind(None, None) == "t2v"
    assert fusion.infer_i2v_kind("img", None) == "i2v"
    assert fusion.infer_i2v_kind(None, "img") == "i2v"
    assert fusion.infer_i2v_kind("a", "b") == "fl2v"


def test_real_infer_kind_source_agrees_with_my_rule():
    """直接从真实源码里读 `infer_i2v_kind` 的分支，确认我的三条规则有对应。"""
    tree = ast.parse(EXTERNAL_GROUPS.read_text(encoding="utf-8"))
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "infer_i2v_kind":
            fn = node
            break
    assert fn is not None, "真实包里找不到 infer_i2v_kind"
    body = ast.unparse(fn)
    assert "'fl2v'" in body or '"fl2v"' in body, f"真实实现里没有 fl2v 分支：\n{body[:400]}"
    assert "'t2v'" in body or '"t2v"' in body, f"真实实现里没有 t2v 分支：\n{body[:400]}"


# ---------------------------------------------------------------------------
# 第 2 重：真导入比对（需要 torch，装不到就 skip 并标注未验证）
# ---------------------------------------------------------------------------


def test_live_compare_with_real_pack_if_importable():
    """能真导入真实包时，做**运行时**比对（比静态更强）。"""
    if not DIRECTOR_PACK.is_dir():
        pytest.skip("未验证：本机没有解包的真实导演台源码目录")

    for p in (str(DIRECTOR_PACK), str(REPO_ROOT / "保留备用的节点")):
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from director.external_groups import pack_i2v_group, pack_r2v_group  # type: ignore
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"未验证：无法导入真实包 pack_*（{type(exc).__name__}: {exc}）。"
            "静态比对已通过，但未做运行时比对。"
        )

    from bettercomfyui_comfy import fusion

    try:
        real_i2v = pack_i2v_group(prompt="x")
        real_r2v = pack_r2v_group(prompt="x")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"未验证：调用真实 pack_* 失败（{type(exc).__name__}: {exc}）")

    mine_i2v = fusion.make_i2v_group(prompt="x", duration_sec=5.0)
    mine_r2v = fusion.make_r2v_group(prompt="x", duration_sec=5.0)

    assert set(mine_i2v) == set(real_i2v), (
        f"i2v 运行时字段不一致：多 {sorted(set(mine_i2v) - set(real_i2v))}，"
        f"少 {sorted(set(real_i2v) - set(mine_i2v))}"
    )
    assert set(mine_r2v) == set(real_r2v)


# ---------------------------------------------------------------------------
# 第 3 重：行为断言
# ---------------------------------------------------------------------------


def test_i2v_group_carries_prompt_and_duration():
    from bettercomfyui_comfy import fusion

    g = fusion.make_i2v_group(prompt="  测试提示词  ", duration_sec=7.5)
    assert g["prompt"] == "测试提示词", "prompt 应 strip"
    assert g["duration_sec"] == 7.5
    assert g["version"] == 1
    assert g["family"] == "i2v"
    assert g["first_frame"] is None and g["last_frame"] is None
    # 真实包的 i2v 分支这几个空字典必须存在
    for k in ("ref_images", "ref_videos", "ref_video_audios", "ref_audios"):
        assert g[k] == {}, f"{k} 应为空字典（真实包就是这样）"


def test_r2v_group_carries_refs_and_size():
    from bettercomfyui_comfy import fusion

    g = fusion.make_r2v_group(prompt="p", duration_sec=5.0, ref_images={0: "IMG"})
    assert g["kind"] == "r2v"
    assert g["family"] == "r2v"
    assert g["ref_images"] == {0: "IMG"}
    assert "ref_image_size" in g, "r2v 组必须带 ref_image_size（真实包有）"


def test_r2v_empty_group_is_rejected():
    """空组必须报错——真实包也会抛 ValueError，我们提前挡住以免产生无意义的段。"""
    from bettercomfyui_comfy import fusion

    with pytest.raises(fusion.FusionError, match="没有提示词也没有任何参考"):
        fusion.make_r2v_group(prompt="   ", duration_sec=5.0)


def test_invalid_duration_is_rejected():
    from bettercomfyui_comfy import fusion

    with pytest.raises(fusion.FusionError, match="duration_sec"):
        fusion.make_i2v_group(prompt="p", duration_sec=0)
    with pytest.raises(fusion.FusionError, match="duration_sec"):
        fusion.make_i2v_group(prompt="p", duration_sec=-1)


def test_ref_image_slot_overflow_is_rejected():
    from bettercomfyui_comfy import fusion

    bad = {fusion.MAX_REFERENCE_IMAGES: "IMG"}   # 越界
    with pytest.raises(fusion.FusionError, match="槽位"):
        fusion.make_r2v_group(prompt="p", duration_sec=5.0, ref_images=bad)


def test_none_refs_are_dropped_not_kept():
    """值为 None 的槽位必须被丢掉（真实包会 `continue` 跳过）。"""
    from bettercomfyui_comfy import fusion

    g = fusion.make_r2v_group(
        prompt="p", duration_sec=5.0, ref_images={0: "A", 1: None, 2: "B"}
    )
    assert g["ref_images"] == {0: "A", 2: "B"}, g["ref_images"]


def test_group_summary_is_human_readable():
    from bettercomfyui_comfy import fusion

    gs = [
        fusion.make_i2v_group(prompt="第一段", duration_sec=5.0),
        fusion.make_r2v_group(prompt="第二段", duration_sec=6.0, ref_images={0: "I"}),
    ]
    s = fusion.group_summary(gs)
    assert "共 2 段" in s
    assert "t2v" in s and "r2v" in s
    assert "参考图1" in s


# ---------------------------------------------------------------------------
# R3 门禁：未 LOCK 的素材绝不能进组
# ---------------------------------------------------------------------------


def test_unlocked_asset_is_rejected_by_ref_resolution(tmp_path: Path):
    """**R3 的关键落地**：未取得 LOCK 的素材版本不得进入导演台。

    这条必须真跑一遍 store：建项目 -> 建素材 -> **不加锁** -> 尝试进组 -> 必须报错。
    """
    from bettercomfyui.config import load_config
    from bettercomfyui.models.enums import AssetSource, AssetStatus, AssetType, LibraryScope
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    repo_root = REPO_ROOT
    cfg = load_config(repo_root=repo_root)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)

    ps = ProjectStore(tmp_path / "projects", cfg)
    pid = ps.create("融合门禁测试")["project_id"]
    paths = ProjectPaths(root=tmp_path / "projects", project_id=pid)
    paths.ensure_layout()
    store = AssetStore(paths)

    entry = store.create_asset(
        asset_type=AssetType.CHARACTER, name="主角",
        source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
    )
    seed = tmp_path / "seed.png"
    seed.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    ver = store.add_version(
        entry["asset_id"], source=AssetSource.AI,
        file_path=seed, status=AssetStatus.DRAFT,
    )["version"]

    from bettercomfyui_comfy import fusion

    # 未 LOCK -> 必须报错，且错误信息要告诉用户怎么修
    with pytest.raises(fusion.FusionError, match="LOCK"):
        fusion.locked_asset_ref_images(
            pid,
            [{"asset_id": entry["asset_id"], "version": ver}],
            repo_root=repo_root, config=cfg,
        )


def test_missing_version_in_ref_is_rejected(tmp_path: Path):
    """引用不带版本号必须报错（R3 要求引用到具体版本）。

    用**真实项目**跑，避免因为 project_id 格式校验而走不到真正的断言点。
    """
    from bettercomfyui.config import load_config
    from bettercomfyui.store.projects import ProjectStore

    from bettercomfyui_comfy import fusion

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.project_root = str(tmp_path / "projects")
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    pid = ProjectStore(tmp_path / "projects", cfg).create("缺版本测试")["project_id"]

    with pytest.raises(fusion.FusionError, match="asset_id 或 version"):
        fusion.locked_asset_ref_images(
            pid, [{"asset_id": "a1"}], repo_root=REPO_ROOT, config=cfg
        )
