"""SG2 素材库结构审计：`G1` 四类分目录 / `G2` 命名规则 / `G4` 数量上限。

## 图一 SG2 原文

    G1["目录结构角色场景物品分镜四类分目录"]
    G2["命名规则类型_序号_名称"]
    G3["格式与尺寸默认统一分辨率可选自动缩放"]
    G4["数量上限每类默认最多9张可调"]
    G5["素材优先级角色>场景>物品>分镜"]

## 本脚本验什么

1. **G1**：四类素材是否真的各分一个目录（造 4 类，看磁盘）；
2. **G2**：目录/ID 里是否带 `类型_序号_名称` 三要素；
3. **G4**：`per_class_limit=9` 是**真的拦得住第 10 个**，还是只是个配置值。

## 为什么值得查 G4

静态扫描显示 `per_class_limit` 只出现在两个地方：

* `config.py`（配置边界）
* `entities.py`（字段默认值）

**没有任何一处代码读它做判断** —— 那它可能只是个摆设。
「可配置」与「会执行」是两件事，必须实测。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    import numpy as np
    from PIL import Image

    from bettercomfyui.config import load_config
    from bettercomfyui.models.enums import (
        AssetSource, AssetStatus, AssetType, LibraryScope,
    )
    from bettercomfyui.store.assets import AssetStore
    from bettercomfyui.store.paths import ProjectPaths
    from bettercomfyui.store.projects import ProjectStore

    print("=" * 76)
    print("SG2 素材库结构审计：G1 四类分目录 / G2 命名规则 / G4 数量上限")
    print("=" * 76)

    tmp = Path(tempfile.mkdtemp())
    cfg = load_config(repo_root=REPO)
    root = tmp / "projects"
    root.mkdir(parents=True, exist_ok=True)
    cfg.global_.project_root = str(root)
    pid = ProjectStore(root, cfg).create("SG2审计")["project_id"]
    paths = ProjectPaths(root=root, project_id=pid)
    paths.ensure_layout()
    store = AssetStore(paths)

    img = tmp / "seed.png"
    Image.fromarray(np.full((16, 16, 3), 100, dtype=np.uint8)).save(img)

    # ---------- G1：四类各造一个，看目录 ----------
    print("\n[G1] 四类分目录")
    made: dict[str, str] = {}
    for t, name in ((AssetType.CHARACTER, "李明"), (AssetType.SCENE, "客厅"),
                    (AssetType.PROP, "旧信"), (AssetType.SHOT, "特写")):
        e = store.create_asset(asset_type=t, name=name, source=AssetSource.AI,
                               library_scope=LibraryScope.PROJECT)
        store.add_version(e["asset_id"], source=AssetSource.AI, file_path=img,
                          status=AssetStatus.PENDING_REVIEW)
        made[t.value] = e["asset_id"]
        # 目录（asset_id 即目录名）
        d = next((p for p in (Path(paths.project_dir) / "assets").rglob(e["asset_id"])),
                 None)
        rel = d.relative_to(paths.project_dir) if d else None
        print(f"    {t.value:<10} name={name:<6} asset_id={e['asset_id']}")
        print(f"    {'':<10} 目录={rel}")

    top_dirs = sorted(p.name for p in (Path(paths.project_dir) / "assets").iterdir()
                      if p.is_dir())
    print(f"\n    assets/ 下的分类目录：{top_dirs}")
    g1_ok = len([d for d in top_dirs if d != "__pycache__"]) >= 4
    print(f"    => G1 四类分目录：{'✅' if g1_ok else '❌'}")

    # ---------- G2：命名规则 类型_序号_名称 ----------
    print("\n[G2] 命名规则「类型_序号_名称」")
    aid = made["CHARACTER"]
    has_type = any(k in aid for k in ("character", "角色", "CHARACTER"))
    has_seq = any(ch.isdigit() for ch in aid)
    has_name = "李明" in aid
    print(f"    asset_id = {aid}")
    print(f"    含类型：{has_type}   含序号：{has_seq}   含名称：{has_name}")
    name_field = store.get_asset(aid).get("name")
    print(f"    名称作为独立字段：{name_field!r}")
    print(f"    => 图一 G2 要求带「名称」，而 ID 里**没有**（名称是单独字段）")

    # ---------- G4：数量上限是否执行 ----------
    print("\n[G4] 每类上限 per_class_limit")
    limit = cfg.assets.per_class_limit
    print(f"    配置值 per_class_limit = {limit}")
    # 已经造了 1 个 CHARACTER，再补到 limit+2 个，看第 limit+1 个会不会被拒
    created = 1
    refused_at = None
    for i in range(limit + 2):
        try:
            e = store.create_asset(asset_type=AssetType.CHARACTER,
                                   name=f"批量角色{i}", source=AssetSource.AI,
                                   library_scope=LibraryScope.PROJECT)
            store.add_version(e["asset_id"], source=AssetSource.AI, file_path=img,
                              status=AssetStatus.PENDING_REVIEW)
            created += 1
        except Exception as exc:  # noqa: BLE001
            refused_at = (created + 1, type(exc).__name__, str(exc)[:70])
            break
    n_chars = len(store.list_assets(asset_type=AssetType.CHARACTER))
    print(f"    实际造出 CHARACTER 素材数：{n_chars}（上限设定 {limit}）")
    if refused_at:
        print(f"    第 {refused_at[0]} 个被拒：{refused_at[1]}: {refused_at[2]}")
    print(f"    => G4 是否执行：{'✅ 有拦截' if refused_at else '❌ 未拦截（配置值不生效）'}")

    print("\n" + "=" * 76)
    print(f"结论：G1 四类分目录 {'✅' if g1_ok else '❌'} | "
          f"G2 含名称 {'✅' if has_name else '⚠️ 名称不在 ID 里'} | "
          f"G4 上限执行 {'✅' if refused_at else '❌ 只是配置值'}")
    if not refused_at:
        print()
        print("  G4 缺口：`per_class_limit` 可配置但**不执行** ——")
        print("  每类可以无限加素材，与图一「每类默认最多9张可调」不符。")
        print("  影响：素材库会无边界膨胀；愿景的省算力/省显存原则也无从落地。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
