"""回归：验收脚本在非 UTF-8 控制台（Windows GBK）下必须能跑完并给出正确退出码。

## 为什么要有这个文件（审计 R1 发现）

`python -B tests/verify/check_contracts.py` 是任务要求的验收命令。
修复前，在默认 Windows 控制台（cp936/GBK）下，`render()` 里的 ✔/✘/∅ 触发
`UnicodeEncodeError`，脚本退出码为 1 —— 看起来像"契约检查不通过"，
实际一条 BLOCKER 都没有。只有 `PYTHONIOENCODING=utf-8` 时才是退出码 0。

本文件用 `PYTHONIOENCODING=gbk` 子进程复现最接近现场的默认控制台环境，
断言脚本仍能以正确退出码结束。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tests" / "verify" / "check_contracts.py"


def _run(encoding: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    env.pop("PYTHONUTF8", None)
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
    )


def test_check_contracts_survives_gbk_console():
    if not SCRIPT.is_file():  # pragma: no cover - 仓库结构保护
        import pytest

        pytest.skip(f"脚本不存在：{SCRIPT}")
    proc = _run("gbk")
    assert proc.returncode == 0, (
        "check_contracts.py 在 GBK 控制台下没有正常结束（退出码 "
        f"{proc.returncode}）。stderr 尾部：{(proc.stderr or '')[-1200:]}")
    # 确认它不是"什么都没打印就退出"：报告标题必须出现
    assert "契约一致性检查报告" in (proc.stdout or ""), (
        f"没有输出可读报告；stdout 尾部：{(proc.stdout or '')[-500:]}")
    assert "UnicodeEncodeError" not in (proc.stderr or ""), (
        f"仍然出现编码崩溃：{(proc.stderr or '')[-800:]}")
