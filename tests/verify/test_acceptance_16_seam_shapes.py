"""验收 16：**接缝形状**与**校验闸门**（ROUND16/19 新增）。

## 为什么要新增这一条

原有 15 条验收各自绿，但**接缝没人验**：

* 验收 02 验 `validate_script`（返回**顶层** `assets`）；
* 验收 15 验端到端，但它用 `demo.py` **自带的 `DEMO_SCRIPT`**（当时 `assets` 在**章节级**、
  用 `type` 键），而且**从不过校验**。

两半都能过，接缝却是断的。实测证据（修前）：

    规范剧本 → run_demo → ok=True，素材新建=0、复用=0、LOCK=0，段=2

**"成功"却零素材** —— 素材阶段整段静默跳过，导演台拿不到 `locked_assets`，
图二的"身份一致性"直接失去依据。而调用方看不到任何异常。

## 判定通过

    P1 `canonicalize_script` 能把「章节级 + type 键」规整成「顶层 + asset_type」；
    P2 **规范剧本**喂 `run_demo`：assets_created > 0 且 assets_locked > 0（核心回归）；
    P3 **老形状**（章节级 + type）也照样能消费（向后兼容，不砸旧调用方）；
    P4 内置 `DEMO_SCRIPT` 能通过 `validate_script`（演示脚本必须符合自己的契约）；
    P5 **校验闸门**：无效引用 -> ok=False、assets_created=0、段=0，且错误信息点明原因；
    P6 `per_class_limit` 真的拦截（图一 SG2 G4）：达到上限后再建必抛错。

## 判定假通过（必须能变红）

    F1 把 `run_demo` 的校验闸门去掉 -> P5 变红；
    F2 让 `run_demo` 回到读章节级 assets -> P2 变红（素材又变 0）；
    F3 把 `create_asset` 的上限判断删掉 -> P6 变红。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _canonical_script() -> dict:
    """`validate_script` 认的规范形状（顶层 assets + asset_type）。"""
    return {
        "title": "接缝章节",
        "chapters": [
            {
                "chapter_id": "CH001",
                "title": "雪夜",
                "segments": [
                    {"segment_index": 1, "prompt": "雪夜长街，林澈独行",
                     "asset_refs": ["A1", "A2"], "duration_s": 3},
                    {"segment_index": 2, "prompt": "林澈拔剑回身",
                     "asset_refs": ["A1"], "duration_s": 3},
                ],
            }
        ],
        "assets": [
            {"id": "A1", "asset_type": "CHARACTER", "name": "林澈",
             "prompt": "少年剑客，白衣"},
            {"id": "A2", "asset_type": "SCENE", "name": "雪夜长街",
             "prompt": "大雪，青石板路"},
        ],
    }


def _legacy_script() -> dict:
    """老形状：assets 在**章节级**、用 `type` 键（`DEMO_SCRIPT` 修复前的样子）。"""
    s = copy.deepcopy(_canonical_script())
    assets = s.pop("assets")
    s["chapters"][0]["assets"] = [
        {("type" if k == "asset_type" else k): v for k, v in a.items()}
        for a in assets
    ]
    return s


def _run(script: dict, tmp: Path, tag: str) -> dict:
    from bettercomfyui.config import load_config
    from bettercomfyui.demo import run_demo

    work = tmp / tag
    work.mkdir(parents=True, exist_ok=True)
    f = work / "script.json"
    f.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")

    cfg = load_config(repo_root=REPO_ROOT)
    cfg.global_.project_root = str(work / "projects")
    (work / "projects").mkdir(parents=True, exist_ok=True)
    return run_demo(repo_root=REPO_ROOT, config=cfg, project=None,
                    chapter=f, executor="mock", workdir=work)


# ---------------------------------------------------------------------------
# P1 规整函数
# ---------------------------------------------------------------------------


def test_p1_canonicalize_moves_assets_and_renames_key():
    """章节级 + `type` -> 顶层 + `asset_type`。"""
    from bettercomfyui.prompt.validate import canonicalize_script

    out = canonicalize_script(_legacy_script())
    assert len(out.get("assets") or []) == 2, "章节级 assets 没有搬到顶层"
    assert all("asset_type" in a for a in out["assets"]), "type 没有改名为 asset_type"
    assert all("type" not in a for a in out["assets"]), "旧的 type 键没删掉"
    assert not (out["chapters"][0].get("assets") or []), "章节级还留着 assets（会重复）"


def test_p1b_canonicalize_prefers_toplevel_and_dedups():
    """顶层优先；两处都写同一个 id 时只保留一份。"""
    from bettercomfyui.prompt.validate import canonicalize_script

    s = _canonical_script()
    s["chapters"][0]["assets"] = [
        {"id": "A1", "type": "CHARACTER", "name": "林澈", "prompt": "重复的"},
        {"id": "A9", "type": "PROP", "name": "青锋剑", "prompt": "只有章节级有"},
    ]
    out = canonicalize_script(s)
    ids = [a["id"] for a in out["assets"]]
    assert ids == ["A1", "A2", "A9"], f"合并/去重结果不对：{ids}"
    assert out["assets"][0]["prompt"] != "重复的", "顶层应当优先"


# ---------------------------------------------------------------------------
# P2/P3 核心回归：素材必须真的建起来
# ---------------------------------------------------------------------------


def test_p2_canonical_script_actually_creates_and_locks_assets(tmp_path):
    """**核心回归**：规范剧本必须产出素材（修前是 0 —— 假成功）。"""
    rep = _run(_canonical_script(), tmp_path, "canon")
    assert rep["ok"] is True, f"规范剧本跑失败了：{rep.get('error')}"
    assert rep["assets_created"] > 0, (
        "规范剧本下素材新建为 0 —— 接缝断了（素材阶段被静默跳过）"
    )
    assert rep["assets_locked"] > 0, "没有素材取得 LOCK，导演台会拿不到参考图"
    assert rep["segments_generated"] > 0, "段没有生成"


def test_p3_legacy_shape_still_works(tmp_path):
    """老形状（章节级 + type）必须继续可用 —— 不能为了修接缝砸掉旧调用方。"""
    rep = _run(_legacy_script(), tmp_path, "legacy")
    assert rep["ok"] is True, f"老形状跑失败了：{rep.get('error')}"
    assert rep["assets_created"] > 0, "老形状下素材新建为 0（向后兼容被破坏）"


# ---------------------------------------------------------------------------
# P4 内置剧本必须符合自己的契约
# ---------------------------------------------------------------------------


def test_p4_demo_script_passes_validation():
    """内置 `DEMO_SCRIPT` 必须能过 `validate_script`。

    修前它通不过（assets 在章节级），只因从不被送去校验 ——
    "唯一能跑的例子却不符合契约"是最坏的情况。
    """
    from bettercomfyui.demo import DEMO_SCRIPT
    from bettercomfyui.prompt.validate import validate_script

    validated = validate_script(copy.deepcopy(DEMO_SCRIPT))
    assert validated.get("assets"), "内置剧本校验后没有 assets"


# ---------------------------------------------------------------------------
# P5 校验闸门
# ---------------------------------------------------------------------------


def _invalid_script() -> dict:
    """引用一个不存在的素材 —— 属于「无效引用」，必须在生成前被拦。"""
    return {
        "chapters": [
            {"segments": [{"prompt": "引用不存在的素材",
                           "asset_refs": ["根本不存在的素材"]}]}
        ],
        "assets": [],
    }


def test_p5_invalid_script_is_blocked_before_generation(tmp_path):
    """无效引用 -> ok=False、零素材、零段（任务书 §B：不得直接进入素材生成）。"""
    rep = _run(_invalid_script(), tmp_path, "invalid")
    assert rep["ok"] is False, "无效剧本竟然返回 ok=True"
    assert rep["assets_created"] == 0, "无效剧本竟然创建了素材"
    assert rep["segments_generated"] == 0, "无效剧本竟然生成了段"
    err = rep.get("error") or ""
    assert "校验未通过" in err, f"错误信息没点明校验失败：{err[:200]}"
    assert "不得直接进入生成" in err, f"错误信息没引用任务书 §B 的约束：{err[:200]}"


# ---------------------------------------------------------------------------
# P6 每类上限（图一 SG2 G4）
# ---------------------------------------------------------------------------


def test_p6_per_class_limit_is_enforced(tmp_path):
    """达到 `per_class_limit` 后再建同类素材必须被拒。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("上限验收")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    store = AssetStore(paths)

    limit = cfg.assets.per_class_limit
    assert isinstance(limit, int) and limit >= 1

    for i in range(limit):
        store.create_asset(asset_type=AssetType.CHARACTER, name=f"角色{i}",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                           per_class_limit=limit)

    with pytest.raises(Exception) as excinfo:
        store.create_asset(asset_type=AssetType.CHARACTER, name="超限角色",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                           per_class_limit=limit)
    msg = str(excinfo.value)
    assert "上限" in msg, f"拒绝理由没说清是上限：{msg[:160]}"
    assert str(limit) in msg, f"拒绝理由里没有具体上限值 {limit}：{msg[:160]}"

    # 不同类型的素材不受影响（上限是**每类**，不是全局）
    store.create_asset(asset_type=AssetType.SCENE, name="场景1",
                       source=AssetSource.AI, library_scope=LibraryScope.PROJECT,
                       per_class_limit=limit)


def test_p6b_limit_is_optional(tmp_path):
    """不传 `per_class_limit` 时不限制 —— 保持旧调用方行为不变。"""
    from bettercomfyui.config import load_config
    from bettercomfyui.models.enums import AssetSource, AssetType, LibraryScope
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO_ROOT)
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("无上限验收")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    store = AssetStore(paths)

    # 超过默认上限（9）也不该报错
    for i in range(cfg.assets.per_class_limit + 3):
        store.create_asset(asset_type=AssetType.CHARACTER, name=f"角色{i}",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
