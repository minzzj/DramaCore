"""审计：**素材审核节点的人工操作是否留下审核记录与事件**。

## 为什么查这个

任务书 §D 明确要求：

> 每个具体版本保存**审核记录、评分/原因、审核模式、LOCK 来源**。

愿景（完整方案）也说：

> 记录状态变化、参数快照、重试原因、版本关系、**人工操作**和模式切换。

审核路径有两条：

| 路径 | 实现 | 审核记录 | 事件落盘 |
|---|---|---|---|
| 库路径 | `AssetReviewer`（`demo.py` 用它，传了 `project=paths`） | 有 | 有 |
| **界面路径** | `BetterComfyUIAssetReview`（用户实际点的节点） | **?** | **?** |

后者是直接调 `assets.set_lock(..., review_cycle_id=f"HUMAN:{aid}:{ver}")` ——
**合成**的 cycle id。本脚本实测它到底有没有留下记录与事件。

## 判定依据

* 审核记录：`<项目>/assets/<asset>/versions/<v>.json` 里的 `review_history`；
* 事件：`<项目>/events/*.jsonl`（`store/events.py` 的落盘位置）。

两者都按「文件里实际有多少条」数，不靠猜。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def make_project(tmp: Path):
    from bettercomfyui.config import load_config
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    cfg = load_config(repo_root=REPO)
    root = tmp / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("审核落盘审计")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    return cfg, pid, paths


def add_version(paths, *, tmp: Path):
    """造一个待审版本（DRAFT -> PENDING_REVIEW）。"""
    import numpy as np
    from PIL import Image

    from bettercomfyui.models.enums import (
        AssetSource, AssetStatus, AssetType, LibraryScope,
    )
    from bettercomfyui.store.assets import AssetStore

    store = AssetStore(paths)
    img = tmp / "candidate.png"
    Image.fromarray(np.full((32, 32, 3), 120, dtype=np.uint8)).save(img)
    e = store.create_asset(asset_type=AssetType.CHARACTER, name="审计角色",
                           source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    v = store.add_version(e["asset_id"], source=AssetSource.AI, file_path=img,
                          status=AssetStatus.PENDING_REVIEW)
    return store, e["asset_id"], v["version"]


def count_events(paths) -> tuple[int, list[str]]:
    """数项目目录下的事件文件条数（jsonl 逐行算一条）。"""
    total = 0
    files: list[str] = []
    for p in Path(paths.project_dir).rglob("*.jsonl"):
        files.append(str(p.relative_to(paths.project_dir)))
        try:
            total += sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
        except Exception:  # noqa: BLE001
            pass
    return total, files


def review_history(paths, asset_id: str, version: str) -> list:
    """读某个版本元数据里的 review_history。

    ⚠️ 实际布局是 `assets/<type>/<asset>/versions/<version>/meta.json`。
    第一版我按 `<version>.json` 找，于是**两边都读成 0 条** ——
    差点得出「两条路径都不写审核记录」的错误结论。
    先确认布局再读，别猜。
    """
    base = Path(paths.project_dir) / "assets"
    for p in base.rglob("meta.json"):
        if p.parent.name != version or p.parent.parent.name != "versions":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        hist = data.get("review_history")
        if isinstance(hist, list):
            return hist
    return []


def main() -> int:
    from bettercomfyui_comfy import nodes as N

    # 节点内部用 _config() 解析配置与项目根（默认指向真实 projects/）。
    # 按既有测试的做法注入，把它的项目根指到临时目录 —— 否则会去动真实项目。
    tmpcfg_holder = {}

    def _patched_config():
        return tmpcfg_holder["cfg"]

    N._config = _patched_config

    from bettercomfyui_comfy.nodes import BetterComfyUIAssetReview

    print("=" * 76)
    print("素材审核节点：人工操作是否留下审核记录与事件")
    print("=" * 76)

    tmp = Path(tempfile.mkdtemp())
    cfg, pid, paths = make_project(tmp)
    tmpcfg_holder["cfg"] = cfg
    store, aid, ver = add_version(paths, tmp=tmp)
    print(f"\n项目 {pid[:8]}…  素材 {aid[:8]}…  版本 {ver}")

    ev_before, files_before = count_events(paths)
    print(f"[前] 事件条数={ev_before}  事件文件={files_before}")

    # ---- 走**界面节点**的人工通过 ----
    node = BetterComfyUIAssetReview()
    out = node.run(project_id=pid, action="人工通过", asset_id=aid, version=ver,
                   operator="tester", reason="")
    print(f"\n[节点] 人工通过 -> {out['result']}")

    locked = store.resolve_lock(aid, ver)
    ev_after, files_after = count_events(paths)
    hist = review_history(paths, aid, ver)

    print(f"\n结果：")
    print(f"  LOCK 生效        : {locked is not None}"
          + (f"  (lock_source={locked.get('lock_source')})" if locked else ""))
    print(f"  review_history 条数: {len(hist)}")
    print(f"  事件条数 {ev_before} -> {ev_after}（新增 {ev_after - ev_before}）")
    print(f"  事件文件: {files_after or '（无）'}")

    # ---- 对照：走**库路径** AssetReviewer（传 project=paths） ----
    print("\n" + "-" * 76)
    print("对照：库路径 AssetReviewer（传 project=paths）")
    store2, aid2, ver2 = add_version(paths, tmp=tmp)
    from bettercomfyui.models.enums import ReviewMode
    from bettercomfyui.review.asset_review import AssetReviewer

    ev_b2, _ = count_events(paths)
    AssetReviewer(store2, config=cfg, project_id=pid, project=paths,
                  model_review=lambda m: {"score": 91.0, "passed": True},
                  auto_commit=True).review_version(
        aid2, ver2, mode=ReviewMode.MODEL,
        material={"file_path": str(tmp / "candidate.png"),
                  "content_hash": store2.get_version(aid2, ver2)["content_hash"]})
    ev_a2, files2 = count_events(paths)
    hist2 = review_history(paths, aid2, ver2)
    print(f"  LOCK 生效        : {store2.resolve_lock(aid2, ver2) is not None}")
    print(f"  review_history 条数: {len(hist2)}")
    print(f"  事件条数 {ev_b2} -> {ev_a2}（新增 {ev_a2 - ev_b2}）")
    print(f"  事件文件: {files2 or '（无）'}")

    print("\n" + "=" * 76)
    node_records = len(hist) > 0
    node_events = (ev_after - ev_before) > 0
    lib_records = len(hist2) > 0
    lib_events = (ev_a2 - ev_b2) > 0
    print(f"界面节点：审核记录={node_records} 事件={node_events}")
    print(f"库路径  ：审核记录={lib_records} 事件={lib_events}")
    if not node_records and lib_records:
        print()
        print("结论：**界面节点的审核不留记录**（库路径留）—— 两条路径保真度不一致")
        return 1
    if node_records and node_events:
        print("\n结论：界面节点也留记录与事件，**无缺口**")
        return 0
    print("\n结论：见上面的数字，按需处理")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
