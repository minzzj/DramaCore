"""变异测试（假通过探测器）—— task-6 第 4/5 项的核心工具。

原理：
    一个「真会失败的测试」在实现被改坏后必然变红。若改了实现测试依然全绿，
    说明该测试要么没覆盖这条路径，要么断言恒真 —— 这就是「假通过」。

安全设计（重要）：
    **绝不修改工作区的任何业务文件。** 每次变异都把仓库复制到临时目录，
    在副本里改代码、跑测试，然后丢弃副本。副本之外的仓库保持字节级不变
    （脚本会核对工作区文件哈希作为证据）。

用法：
    python tests/verify/mutation_check.py                 # 跑 mutations.json 里的全部变异
    python tests/verify/mutation_check.py --list          # 只列出变异清单
    python tests/verify/mutation_check.py --only M001 M003
    python tests/verify/mutation_check.py --jobs 1        # 串行（默认 2）

mutations.json 结构：
    [
      {"id": "M001", "file": "bettercomfyui/store/assets.py",
       "old": "return old_version", "new": "return new_version",
       "test": "tests/test_store_assets.py::test_old_version_retained",
       "note": "旧版本保留（验收 5）"}
    ]

退出码：0 = 全部变异被杀死；1 = 存在存活的变异（假通过风险）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MUTATIONS = HERE / "mutations.json"
COPY_ITEMS = ("bettercomfyui", "tests", "config")


def _digest_tree(root: Path, subdirs=COPY_ITEMS) -> dict[str, str]:
    out = {}
    for sub in subdirs:
        base = root / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in str(p):
                out[str(p.relative_to(root)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _make_copy(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for sub in COPY_ITEMS:
        src = REPO_ROOT / sub
        if not src.exists():
            continue
        shutil.copytree(
            src,
            dst / sub,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_last_run.json", "_contracts_report.json"),
        )


def _run_pytest(cwd: Path, selector: str, timeout_s: float = 900.0) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", selector, "-q", "--no-header", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout_s}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _apply_mutation(root: Path, mut: dict) -> tuple[bool, str]:
    p = root / mut["file"]
    if not p.exists():
        return False, f"目标文件不存在：{mut['file']}"
    text = p.read_text(encoding="utf-8")
    old, new = mut["old"], mut["new"]
    n = text.count(old)
    if n != 1:
        return False, f"old 片段在 {mut['file']} 中出现 {n} 次（必须恰好 1 次，否则变异不可控）"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True, "ok"


def run_mutation(mut: dict, workspace_digest: dict[str, str]) -> dict:
    rec = {
        "id": mut.get("id"),
        "file": mut.get("file"),
        "test": mut.get("test"),
        "note": mut.get("note", ""),
        "result": None,
        "detail": "",
    }
    with tempfile.TemporaryDirectory(prefix="bcui_mut_") as tmp:
        copy = Path(tmp) / "repo"
        _make_copy(copy)

        baseline_rc, baseline_out = _run_pytest(copy, mut["test"])
        rec["baseline_rc"] = baseline_rc
        if baseline_rc != 0:
            rec["result"] = "BASELINE_NOT_GREEN"
            rec["detail"] = (
                "变异前测试就不是绿的，无法判断变异是否被杀死。"
                f"输出尾部：{baseline_out[-800:]}"
            )
            return rec

        ok, detail = _apply_mutation(copy, mut)
        if not ok:
            rec["result"] = "MUTATION_NOT_APPLIED"
            rec["detail"] = detail
            return rec

        rc, out = _run_pytest(copy, mut["test"])
        if rc == 0:
            rec["result"] = "SURVIVED"  # 假通过风险
            rec["detail"] = f"改坏实现后测试仍然全绿 —— 该测试没有真正覆盖 {mut['file']} 的这条逻辑。输出尾部：{out[-800:]}"
        elif rc == 124:
            rec["result"] = "TIMEOUT"
            rec["detail"] = out
        else:
            rec["result"] = "KILLED"
            rec["detail"] = out.strip().splitlines()[-1] if out.strip() else ""

    # 安全证据：工作区必须字节级未变
    after = _digest_tree(REPO_ROOT)
    if after != workspace_digest:
        rec["result"] = "WORKSPACE_DIRTY"
        rec["detail"] = "变异过程改动了工作区文件（这绝不允许）"
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="变异测试：确认测试真的会失败")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--jobs", type=int, default=2)
    args = ap.parse_args(argv)

    if not MUTATIONS.exists():
        print(f"[mutation] 变异清单不存在：{MUTATIONS}")
        print("[mutation] 这是正常的（阶段一尚未有实现可变异）。请先补齐 mutations.json。")
        return 0

    muts = json.loads(MUTATIONS.read_text(encoding="utf-8"))
    if args.only:
        muts = [m for m in muts if m.get("id") in set(args.only)]

    if args.list:
        for m in muts:
            print(f"{m.get('id')}: {m.get('file')} :: {m.get('test')} :: {m.get('note','')}")
        return 0

    if not muts:
        print("[mutation] 没有可执行的变异（清单为空）。")
        return 0

    workspace_digest = _digest_tree(REPO_ROOT)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(run_mutation, m, workspace_digest): m for m in muts}
        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            results.append(rec)
            print(f"[{rec['result']}] {rec['id']} {rec['file']} :: {rec['test']}")
            if rec["detail"]:
                print("    " + rec["detail"].splitlines()[0][:200])

    survived = [r for r in results if r["result"] == "SURVIVED"]
    dirty = [r for r in results if r["result"] == "WORKSPACE_DIRTY"]
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "total": len(results),
        "killed": sum(1 for r in results if r["result"] == "KILLED"),
        "survived": len(survived),
        "other": [r for r in results if r["result"] not in ("KILLED", "SURVIVED")],
        "results": sorted(results, key=lambda r: str(r["id"])),
    }
    out = HERE / "_mutation_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"\n[mutation] 杀死={report['killed']} 存活={report['survived']} 其它={len(report['other'])}"
        f"（报告：{out}）"
    )
    if survived:
        print("[mutation] 以下变异存活 = 假通过风险，必须补强测试：")
        for r in survived:
            print(f"  - {r['id']} {r['file']} :: {r['test']}")
    if dirty:
        print("[mutation] 严重：变异过程改动了工作区文件！")
    return 1 if (survived or dirty) else 0


if __name__ == "__main__":
    raise SystemExit(main())
