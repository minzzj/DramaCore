#!/usr/bin/env bash
# 剧核 DramaCore · macOS 一键安装入口（ROUND57）。
# Apple Silicon 默认走 MPS（PyPI 默认 wheel）；Intel Mac 只能 CPU。
#
# 用法：
#   ./install_macos.sh --mode attach --comfyui "$HOME/ComfyUI" --dry-run
#   BACKEND=mps ./install_macos.sh --mode bootstrap --comfyui "$HOME/ComfyUI" --yes
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
core="${script_dir}/install_core.py"

if [[ ! -f "${core}" ]]; then
    echo "找不到安装器核心：${core}（install 目录不完整？）" >&2
    exit 3
fi

python_bin=""
for candidate in "${PYTHON:-}" python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    [[ -z "${candidate}" ]] && continue
    if command -v "${candidate}" >/dev/null 2>&1 \
        && "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        python_bin="${candidate}"
        break
    fi
done
if [[ -z "${python_bin}" ]]; then
    echo "找不到可用的 Python 3.9+。建议 brew install python，或用 PYTHON=/path/to/python 指定。" >&2
    exit 3
fi

if [[ "$(uname -m)" != "arm64" ]]; then
    echo "[剧核] 提示：当前不是 Apple Silicon（$(uname -m)），MPS 不可用；安装器会按 CPU 处理。" >&2
fi

args=(--mode "${MODE:-attach}" --backend "${BACKEND:-auto}")
[[ -n "${COMFYUI:-}" ]] && args+=(--comfyui "${COMFYUI}")
[[ "${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run)
[[ "${YES:-0}" == "1" ]] && args+=(--yes)
[[ "${FORCE:-0}" == "1" ]] && args+=(--force)
[[ "${SKIP_TORCH:-0}" == "1" ]] && args+=(--skip-torch)
[[ "${SKIP_LAUNCHER:-0}" == "1" ]] && args+=(--skip-launcher)

echo "[剧核] 使用 Python：${python_bin}"
echo "[剧核] 说明：MPS 不承诺 H3 48GB 级全流程，优先素材/轻流程（能力分级见 --dry-run 输出）。"
exec "${python_bin}" "${core}" "${args[@]}" "$@"
