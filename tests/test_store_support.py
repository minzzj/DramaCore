"""store 测试的共享工具（本文件**不含**测试用例，只是被其他 test_store_*.py 导入）。

放在 ``tests/`` 下而不是 conftest.py：``tests/conftest.py`` 不是本任务的独占写入范围，
避免与其他队友的 fixture 冲突。每个测试文件自己把仓库根与 tests 目录加入 sys.path，
因此无论 ``tests/`` 是否是 package 都能工作。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(REPO_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from bettercomfyui.store import AssetStore, ProjectStore  # noqa: E402


def child_env() -> dict:
    """子进程环境：PYTHONPATH 指向仓库根，强制 UTF-8 输出。"""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def spawn_child(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        cwd=str(REPO_ROOT),
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def run_child(code: str, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
    proc = spawn_child(code, *args)
    out, err = proc.communicate(timeout=timeout)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def assert_child_ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (
        f"子进程失败（exit={result.returncode}）：\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def make_project(tmp_path: Path, display_name: str = "测试项目") -> dict:
    return ProjectStore(tmp_path).create(display_name)


def make_asset_store(tmp_path: Path, display_name: str = "测试项目"):
    """返回 (ProjectPaths, AssetStore)。"""
    paths = make_project(tmp_path, display_name)["paths"]
    return paths, AssetStore(paths)


def add_bytes_version(store, asset_id: str, payload: bytes, *, status: str = "DRAFT", extra=None):
    """用临时文件给素材追加一个版本（测试便捷函数）。"""
    tmp = store.paths.project_dir / f"_src_{len(payload)}_{abs(hash(payload)) & 0xFFFFFF}.bin"
    tmp.write_bytes(payload)
    try:
        return store.add_version(
            asset_id, source="AI", file_path=tmp, status=status, extra=extra
        )
    finally:
        if tmp.exists():
            tmp.unlink()
