#!/usr/bin/env python
"""一键配置 Better ComfyUI 的运行环境。

做完这几件事：

1. 在仓库内建 `.comfyenv` 虚拟环境（如果还没有）；
2. 安装 **CPU 版** torch（默认；有 NVIDIA 卡可加 `--cuda`）；
3. 下载 **ComfyUI 最新版**源码到 `.comfyui/`（用 GitHub zip，不走 `git clone` —— 实测
   `git clone` 会超时，而 HTTPS 下载 zip 正常）；
4. 安装 ComfyUI 的 `requirements.txt`；
5. 安装编排层额外需要的 `pyyaml` 与 `imageio-ffmpeg`；
6. 把插件与编排包同步到 `custom_nodes/`。

**可重复执行**：已存在的步骤会跳过，不会重复下载。

用法：
    python scripts/setup_comfyui.py              # 默认 CPU 版 torch
    python scripts/setup_comfyui.py --cuda       # 有 NVIDIA 卡时装 CUDA 版
    python scripts/setup_comfyui.py --skip-comfyui-download   # 已有源码，只装依赖
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV = REPO_ROOT / ".comfyenv"
COMFYUI = REPO_ROOT / ".comfyui"
COMFYUI_ZIP = "https://codeload.github.com/comfyanonymous/ComfyUI/zip/refs/heads/master"
PYPI_CPU = "https://download.pytorch.org/whl/cpu"


def venv_python(venv: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def run(cmd: list[str], *, what: str, cwd: Path | None = None) -> None:
    print(f"\n>>> {what}")
    print(f"    {' '.join(str(c) for c in cmd)}")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"[!] {what} 失败（exit {proc.returncode}）")
    print(f"    完成，用时 {time.time() - t0:.0f}s")


def step_venv() -> Path:
    py = venv_python(VENV)
    if py.is_file():
        print(f"[跳过] 虚拟环境已存在：{VENV}")
        return py
    run([sys.executable, "-m", "venv", str(VENV)], what="创建虚拟环境 .comfyenv")
    py = venv_python(VENV)
    if not py.is_file():
        raise SystemExit(f"[!] 虚拟环境创建后仍找不到 python：{py}")
    # 升级 pip，但失败不算致命（某些镜像源会抽风）
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"], check=False)
    return py


def step_torch(py: Path, *, cuda: bool) -> None:
    cmd = [str(py), "-m", "pip", "install", "torch", "torchvision", "torchaudio"]
    if not cuda:
        cmd += ["--index-url", PYPI_CPU]
    run(cmd, what=f"安装 {'CUDA' if cuda else 'CPU'} 版 torch（较大，请耐心）")


def _download(url: str, dest: Path) -> None:
    print(f"\n>>> 下载 {url}")
    t0 = time.time()

    def hook(block: int, size: int, total: int) -> None:
        if total > 0 and block % 200 == 0:
            pct = min(100, block * size * 100 // total)
            print(f"\r    {pct}%", end="", flush=True)

    urllib.request.urlretrieve(url, dest, reporthook=hook)  # noqa: S310 - 固定可信 URL
    print(f"\n    完成：{dest.stat().st_size / 1e6:.1f} MB，用时 {time.time() - t0:.0f}s")


def step_comfyui(*, skip_download: bool) -> None:
    if skip_download:
        print(f"[跳过] 不下载 ComfyUI（--skip-comfyui-download）；使用现有 {COMFYUI}")
        if not (COMFYUI / "main.py").is_file():
            raise SystemExit(f"[!] {COMFYUI} 下没有 main.py")
        return

    if (COMFYUI / "main.py").is_file():
        ver = "unknown"
        vf = COMFYUI / "comfyui_version.py"
        if vf.is_file():
            for line in vf.read_text(encoding="utf-8", errors="replace").splitlines():
                if "__version__" in line and "=" in line:
                    ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        print(f"[跳过] ComfyUI 已存在（版本 {ver}）：{COMFYUI}")
        print("       如需更新，先删除该目录再运行本脚本。")
        return

    tmp_zip = REPO_ROOT / "_comfyui_download.zip"
    _download(COMFYUI_ZIP, tmp_zip)

    print("\n>>> 解压")
    with zipfile.ZipFile(tmp_zip) as zf:
        names = zf.namelist()
        top = names[0].split("/")[0]
        zf.extractall(REPO_ROOT / "_comfyui_tmp")
    extracted = REPO_ROOT / "_comfyui_tmp" / top
    if COMFYUI.exists():
        shutil.rmtree(COMFYUI)
    shutil.move(str(extracted), str(COMFYUI))
    shutil.rmtree(REPO_ROOT / "_comfyui_tmp", ignore_errors=True)
    tmp_zip.unlink(missing_ok=True)

    ver = "unknown"
    vf = COMFYUI / "comfyui_version.py"
    if vf.is_file():
        for line in vf.read_text(encoding="utf-8", errors="replace").splitlines():
            if "__version__" in line and "=" in line:
                ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    print(f"    ComfyUI {ver} -> {COMFYUI}")


def step_requirements(py: Path) -> None:
    req = COMFYUI / "requirements.txt"
    if not req.is_file():
        raise SystemExit(f"[!] 找不到 {req}")
    run([str(py), "-m", "pip", "install", "--no-input", "-r", str(req)],
        what="安装 ComfyUI requirements.txt")
    run([str(py), "-m", "pip", "install", "--no-input", "pyyaml", "imageio-ffmpeg"],
        what="安装编排层额外依赖（pyyaml / imageio-ffmpeg）")
    run([str(py), "-m", "pip", "install", "--no-input", "pytest"],
        what="安装 pytest（用于运行测试）")


def step_sync() -> None:
    run([sys.executable, str(REPO_ROOT / "scripts" / "sync_to_comfyui.py")],
        what="同步插件与编排包到 custom_nodes")


def main() -> int:
    ap = argparse.ArgumentParser(description="一键配置 Better ComfyUI 运行环境")
    ap.add_argument("--cuda", action="store_true", help="安装 CUDA 版 torch（需 NVIDIA 卡）")
    ap.add_argument("--skip-comfyui-download", action="store_true",
                    help="不下载 ComfyUI，只用现有 .comfyui/")
    args = ap.parse_args()

    print("=" * 70)
    print("Better ComfyUI 环境配置")
    print("=" * 70)
    print(f"仓库根 : {REPO_ROOT}")
    print(f"虚拟环境: {VENV}")
    print(f"ComfyUI : {COMFYUI}")

    py = step_venv()
    step_torch(py, cuda=args.cuda)
    step_comfyui(skip_download=args.skip_comfyui_download)
    step_requirements(py)
    step_sync()

    print("\n" + "=" * 70)
    print("配置完成。启动方式：")
    print(f'  cd "{COMFYUI}"')
    print(f'  "{py}" main.py --cpu --listen 127.0.0.1 --port 8188 --disable-auto-launch')
    print("然后浏览器打开 http://127.0.0.1:8188 ，在节点搜索框输入 Better ComfyUI")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
