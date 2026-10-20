#!/usr/bin/env bash
# 剧核 DramaCore · Linux 一键安装入口（ROUND57）。
# 只是 install_core.py 的薄包装：找 Python、支持环境变量写法、转发参数。
#
# 用法：
#   ./install_linux.sh --mode attach --comfyui /opt/ComfyUI --dry-run
#   BACKEND=rocm ./install_linux.sh --mode bootstrap --comfyui "$HOME/ComfyUI" --yes
#
# 环境变量（都可被同名命令行参数覆盖）：
#   PYTHON  MODE  COMFYUI  BACKEND  TORCH_INDEX  DRY_RUN=1  YES=1  FORCE=1
#   SKIP_TORCH=1  SKIP_LAUNCHER=1  ALLOW_EXPERIMENTAL=1
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
    echo "找不到可用的 Python 3.9+。请安装 Python，或用 PYTHON=/path/to/python 指定。" >&2
    echo "（注意：部分系统里的 python3 只是 Microsoft Store 占位符，会直接退出。）" >&2
    exit 3
fi

args=(--mode "${MODE:-attach}" --backend "${BACKEND:-auto}")
[[ -n "${COMFYUI:-}" ]] && args+=(--comfyui "${COMFYUI}")
[[ -n "${TORCH_INDEX:-}" ]] && args+=(--torch-index "${TORCH_INDEX}")
[[ "${DRY_RUN:-0}" == "1" ]] && args+=(--dry-run)
[[ "${YES:-0}" == "1" ]] && args+=(--yes)
[[ "${FORCE:-0}" == "1" ]] && args+=(--force)
[[ "${SKIP_TORCH:-0}" == "1" ]] && args+=(--skip-torch)
[[ "${SKIP_LAUNCHER:-0}" == "1" ]] && args+=(--skip-launcher)
[[ "${ALLOW_EXPERIMENTAL:-0}" == "1" ]] && args+=(--allow-experimental)

echo "[剧核] 使用 Python：${python_bin}"
exec "${python_bin}" "${core}" "${args[@]}" "$@"
