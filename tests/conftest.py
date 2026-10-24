"""仓库根目录注入：所有测试共享的 sys.path 与临时项目夹具。

放在 tests/ 根而不是 conftest 的 hidden 目录，保证 `import bettercomfyui` 在
任何 pytest 调用方式下都可用（`pytest tests/`、`pytest tests/test_x.py`、
以及在 IDE 里单跑）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """一个最小的临时仓库根：含 config/ 与 projects/。

    用 tmp_path 而不是真实仓库，保证测试不污染真实项目数据。
    """
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def default_config(repo_root: Path):
    """加载仓库内置默认配置（含 config/default.yaml）。"""
    from bettercomfyui.config import load_config

    return load_config(repo_root=repo_root)


def assert_bytes_unchanged(path: Path, snapshot: bytes, *, why: str) -> None:
    """断言文件字节未变化 —— 用于证明「绝不覆盖」这类硬规则。

    这是本项目最重要的一类断言：证明某个操作**没有**产生副作用，
    比证明它返回了正确值更有价值。
    """
    now = path.read_bytes() if path.exists() else b""
    assert now == snapshot, f"{why}：文件 {path} 的字节发生了变化（{len(snapshot)} -> {len(now)}）"
