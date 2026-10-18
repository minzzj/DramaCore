# -*- coding: utf-8 -*-
"""打包「远程部署包」：只含代码/工作流/配置/文档/测试，不含模型与项目数据。

用法：python -B scripts/prepare_remote_bundle.py
产出：dist/bettercomfyui_bundle_<时间戳>.zip（约几 MB），上传目标机后按
      docs/早上开机操作单.md 操作。
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"

INCLUDE_DIRS = [
    "bettercomfyui",
    "bettercomfyui_comfy",
    "scripts",
    "tests",
    "config",
    "docs",
    "文档",
]
INCLUDE_FILES = [
    "__init__.py",  # 如果根目录有
    "README.md",
    "requirements.txt",
    ".gitignore",
]
EXCLUDE_NAMES = {"user.yaml", "user.yml", "secrets.yaml"}  # 含 API Key，不随源码分发

EXCLUDE_PARTS = {
    "__pycache__", ".pytest_cache", ".comfyui", "projects", "dist",
    "保留备用的节点", "_stray_custom_nodes_projects", ".comfyenv",
    "node_modules", ".git",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".mp4", ".mp3", ".wav", ".png", ".jpg",
                  ".jpeg", ".webp", ".safetensors", ".pt", ".pth", ".gguf", ".zip"}


def main() -> int:
    DIST.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = DIST / f"bettercomfyui_bundle_{stamp}.zip"
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in INCLUDE_DIRS:
            base = REPO / name
            if not base.exists():
                continue
            for file in base.rglob("*"):
                if not file.is_file():
                    continue
                parts = set(file.relative_to(REPO).parts)
                if file.name in EXCLUDE_NAMES:
                    continue
                if parts & EXCLUDE_PARTS:
                    continue
                if file.suffix.lower() in EXCLUDE_SUFFIX:
                    continue
                zf.write(file, file.relative_to(REPO))
                count += 1
        for name in INCLUDE_FILES:
            file = REPO / name
            if file.is_file():
                zf.write(file, file.relative_to(REPO))
                count += 1
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"[OK] {out} | {count} 个文件 | {size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
