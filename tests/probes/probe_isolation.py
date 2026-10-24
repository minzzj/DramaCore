"""Lead 的独立验收探测：项目隔离（任务书第 9 节验收 1）。

**这份脚本不依赖任何队友的测试文件**，直接用公开接口做端到端验证，
目的是不把「队友说通过了」当成「通过了」。

验收 1 原文：
    「两个项目的数据和配置互不串用，切换后可恢复。」

判定方式（三条独立证据，全部通过才算通过）：
  E1. 两个项目写入同名素材，各自 list_assets 只看到自己的那一份。
  E2. 在 A 项目里按归一化名查找，绝不会查到 B 项目的同名单条。
  E3. 切换（open A -> open B -> open A）后，A 的素材数量与内容指纹不变。

任何一条失败即打印 FAIL 并以非零退出码结束。
"""

from __future__ import annotations

import hashlib
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from bettercomfyui.config import load_config  # noqa: E402
from bettercomfyui.models.enums import (  # noqa: E402
    AssetSource,
    AssetStatus,
    AssetType,
    LibraryScope,
)
from bettercomfyui.store import paths as paths_mod  # noqa: E402
from bettercomfyui.store.assets import AssetStore  # noqa: E402
from bettercomfyui.store.projects import ProjectStore  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    cfg = load_config(repo_root=REPO_ROOT)
    root = REPO_ROOT / cfg.global_.project_root
    ps = ProjectStore(root, cfg)

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    print("=" * 68)
    print("独立验收 · 验收 1：项目隔离")
    print("=" * 68)

    # --- 准备两个全新项目 ---
    a = ps.create("隔离验证A")["project_id"]
    b = ps.create("隔离验证B")["project_id"]
    print(f"项目 A = {a}")
    print(f"项目 B = {b}\n")

    pa = paths_mod.ProjectPaths(root=root, project_id=a)
    pb = paths_mod.ProjectPaths(root=root, project_id=b)
    sa = AssetStore(pa)
    sb = AssetStore(pb)

    # --- 写入同名素材（名称相同、内容不同）---
    # 源文件放在仓库根的临时位置，避免与项目内部布局耦合
    file_a = REPO_ROOT / "_probe_seed_a.png"
    file_b = REPO_ROOT / "_probe_seed_b.png"
    file_a.write_bytes(b"AAAA-identical-name-different-content")
    file_b.write_bytes(b"BBBB-identical-name-different-content")

    ea = sa.create_asset(asset_type=AssetType.CHARACTER, name="主角",
                         source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    eb = sb.create_asset(asset_type=AssetType.CHARACTER, name="主角",
                         source=AssetSource.AI, library_scope=LibraryScope.PROJECT)
    sa.add_version(ea["asset_id"], source=AssetSource.AI, file_path=str(file_a), status=AssetStatus.DRAFT)
    sb.add_version(eb["asset_id"], source=AssetSource.AI, file_path=str(file_b), status=AssetStatus.DRAFT)

    print("[E1] 各自 list_assets 只看到自己的素材")
    la = sa.list_assets()
    lb = sb.list_assets()
    check("A 恰好 1 个素材", len(la) == 1, f"实际 {len(la)}")
    check("B 恰好 1 个素材", len(lb) == 1, f"实际 {len(lb)}")
    check("A 的 asset_id 属于 A", la and la[0]["asset_id"] == ea["asset_id"],
          f"期望 {ea['asset_id']} 实际 {la[0]['asset_id'] if la else None}")
    check("A/B 的 asset_id 不同（身份不共用）", ea["asset_id"] != eb["asset_id"])

    print("\n[E2] 归一化名查找不跨项目")
    fa = sa.find_by_normalized_name("主角")
    fb = sb.find_by_normalized_name("主角")
    ids_a = {r.get("asset_id") for r in fa}
    ids_b = {r.get("asset_id") for r in fb}
    check("A 查到自己的", ea["asset_id"] in ids_a, f"查到 {ids_a}")
    check("A 查不到 B 的", eb["asset_id"] not in ids_a)
    check("B 查不到 A 的", ea["asset_id"] not in ids_b)

    print("\n[E3] 切换后状态可恢复")
    before_meta = _sha(pa.meta_json)
    before_rows = json.dumps(sa.list_assets(), ensure_ascii=False, sort_keys=True, default=str)
    ps.open(a)
    ps.open(b)
    ps.open(a)
    after_meta = _sha(pa.meta_json)
    after_rows = json.dumps(sa.list_assets(), ensure_ascii=False, sort_keys=True, default=str)
    check("切换后 A 的 meta.json 字节不变", before_meta == after_meta)
    check("切换后 A 的素材列表不变", before_rows == after_rows)

    print("\n[E4] B 的数据没被 A 的操作污染")
    lb2 = sb.list_assets()
    check("B 仍恰好 1 个素材", len(lb2) == 1, f"实际 {len(lb2)}")
    vb = sb.get_version(eb["asset_id"], lb2[0]["versions"][-1]["version"]) if lb2 else None
    if vb:
        vpath = Path(vb.get("file_path", ""))
        if not vpath.is_absolute():
            vpath = pb.project_dir / vpath
        check("B 的素材文件内容仍是 B 的",
              vpath.exists() and vpath.read_bytes().startswith(b"BBBB"),
              f"path={vpath}")
    else:
        check("B 的版本可读", False, "取不到版本记录")

    # --- 清理 ---
    # 注意：ProjectStore 要求先 close 再 delete（有未保存状态时拒绝删除，这是正确行为）。
    # 若不 close 就 delete，会抛 ProjectBusyError 并**留下残留项目**，
    # 使重复运行本探测时污染 projects/ 目录。
    for pid in (a, b):
        try:
            ps.close(pid)
        except Exception as exc:  # noqa: BLE001
            print(f"  (close {pid} 失败：{type(exc).__name__}: {exc}）")
        try:
            ps.delete(pid, confirm_token=pid)
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] 清理 {pid} 失败，projects/ 下会残留该项目：{type(exc).__name__}: {exc}")
    for f in (file_a, file_b):
        f.unlink(missing_ok=True)

    # 确认清理干净：残留会让重复运行产生假结果
    leftovers = [p for p in (a, b) if (root / p).exists()]
    check("探测结束后不残留测试项目", not leftovers, f"残留 {leftovers}")

    failed = [c for c in checks if not c[1]]
    print("\n" + "=" * 68)
    if failed:
        print(f"结论：验收 1 失败 —— {len(failed)}/{len(checks)} 条不通过")
        for name, _, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print(f"结论：验收 1 通过 —— {len(checks)}/{len(checks)} 条独立证据全部成立")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - 探测脚本必须暴露真实异常
        traceback.print_exc()
        raise SystemExit(2)
