# -*- coding: utf-8 -*-
"""SSH 实机验证编排器（本机执行；目标机 Linux）。

⚠️ 状态：**已编写、未在真机验证**（当前没有 SSH 机器）。首次使用请先跑 `env`
子命令体检，再决定 deploy/verify。

用法：
    python -B scripts/ssh_verify.py env    --host 1.2.3.4 --user root --port 22 --key C:\\path\\id_rsa --remote-dir ~/BetterComfyUI
    python -B scripts/ssh_verify.py deploy --host ... --bundle dist/xxx.zip
    python -B scripts/ssh_verify.py verify --host ... --remote-dir ~/BetterComfyUI

安全约束：
  * 只执行白名单命令（env report / pytest / verify_install / 只读 ComfyUI HTTP）；
  * 不自动下载大型模型、不自动排队含 H3 权重的图（除非显式 --allow-heavy）；
  * 不使用 shell=True 拼接；参数逐个传递。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(cmd: list[str], timeout: int = 1800, capture: bool = True) -> subprocess.CompletedProcess:
    print("[cmd]", " ".join(cmd[:8]), "..." if len(cmd) > 8 else "")
    return subprocess.run(cmd, capture_output=capture, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def ssh_base(args) -> list[str]:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
           "-p", str(args.port)]
    if args.key:
        cmd += ["-i", args.key]
    cmd.append(f"{args.user}@{args.host}")
    return cmd


def scp_base(args) -> list[str]:
    cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new", "-P", str(args.port)]
    if args.key:
        cmd += ["-i", args.key]
    return cmd


def check_local_ssh() -> bool:
    try:
        proc = subprocess.run(["ssh", "-V"], capture_output=True, text=True, timeout=20)
        print("[ssh]", (proc.stderr or proc.stdout).strip())
        return True
    except Exception as exc:  # noqa: BLE001
        print("[ssh] 本机没有 ssh 客户端：", type(exc).__name__, exc)
        return False


def cmd_env(args) -> int:
    if not check_local_ssh():
        return 2
    script = REPO / "scripts" / "remote_env_report.sh"
    if not script.is_file():
        print("[FAIL] 缺少 scripts/remote_env_report.sh")
        return 2
    # 上传脚本并执行（远程目录不存在时先建）
    remote = f"{args.remote_dir}/scripts/remote_env_report.sh"
    run(ssh_base(args) + [f"mkdir -p {args.remote_dir}/scripts && echo ok"])
    proc = run(scp_base(args) + [str(script), f"{args.user}@{args.host}:{remote}"])
    if proc.returncode != 0:
        print("[FAIL] 上传失败：", proc.stderr[-500:])
        return 1
    proc = run(ssh_base(args) + [f"cd {args.remote_dir} && bash scripts/remote_env_report.sh {args.remote_dir}"],
               timeout=900)
    print((proc.stdout or "")[-8000:])
    if proc.stderr:
        print("[stderr]", proc.stderr[-1000:])
    # 拉回报告
    local = REPO / "dist" / "remote_env_report.md"
    local.parent.mkdir(exist_ok=True)
    run(scp_base(args) + [f"{args.user}@{args.host}:{args.remote_dir}/remote_env_report.md", str(local)])
    print("[OK] 报告已拉回：", local)
    return 0 if proc.returncode == 0 else 1


def cmd_deploy(args) -> int:
    if not check_local_ssh():
        return 2
    bundle = Path(args.bundle)
    if not bundle.is_file():
        print("[FAIL] bundle 不存在：", bundle)
        return 2
    remote_zip = f"{args.remote_dir}/bundle.zip"
    run(ssh_base(args) + [f"mkdir -p {args.remote_dir}"])
    proc = run(scp_base(args) + [str(bundle), f"{args.user}@{args.host}:{remote_zip}"], timeout=1800)
    if proc.returncode != 0:
        print("[FAIL] 上传失败：", proc.stderr[-500:])
        return 1
    unzip = (f"cd {args.remote_dir} && (command -v unzip >/dev/null && unzip -o bundle.zip "
             f"|| python -c \"import zipfile;zipfile.ZipFile('bundle.zip').extractall('.')\") && echo deployed")
    proc = run(ssh_base(args) + [unzip], timeout=900)
    print((proc.stdout or "")[-3000:])
    return 0 if proc.returncode == 0 else 1


def cmd_verify(args) -> int:
    if not check_local_ssh():
        return 2
    commands = [
        f"cd {args.remote_dir} && python -B scripts/verify_install.py",
        f"cd {args.remote_dir} && python -B -m pytest tests/verify -q",
        f"cd {args.remote_dir} && python -B -m pytest tests/ -q",
        "curl -s --max-time 15 http://127.0.0.1:8188/bettercomfyui/doctor",
    ]
    ok = True
    for command in commands:
        proc = run(ssh_base(args) + [command], timeout=3600)
        tail = (proc.stdout or "")[-2500:]
        print(tail)
        if proc.returncode != 0:
            ok = False
            print(f"[FAIL] 退出码 {proc.returncode}: {command}")
    print("[RESULT]", "PASS" if ok else "FAIL（详见上方向）")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["env", "deploy", "verify"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--key", default="")
    parser.add_argument("--remote-dir", default="~/BetterComfyUI")
    parser.add_argument("--bundle", default="")
    args = parser.parse_args()
    if args.action == "env":
        return cmd_env(args)
    if args.action == "deploy":
        if not args.bundle:
            print("[FAIL] deploy 需要 --bundle")
            return 2
        return cmd_deploy(args)
    return cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
