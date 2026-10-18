#!/usr/bin/env bash
# Better ComfyUI —— 目标机（Linux）环境体检 + 实机验证脚本
# 用法：bash remote_env_report.sh [项目目录]
# 只读为主：安装任何东西之前先输出结论；不自动跑 GPU 队列（由调用方决定）。
set -u
PROJECT_DIR="${1:-$(pwd)}"
OUT="$PROJECT_DIR/remote_env_report.md"
exec > >(tee "$OUT") 2>&1

echo "# 目标机环境体检报告"
echo "- 时间：$(date -Is)"
echo "- 主机：$(hostname)"
echo

echo "## 1) 系统 / 磁盘 / 内存"
uname -a || true
if command -v lsb_release >/dev/null 2>&1; then lsb_release -a 2>/dev/null || true; fi
df -h "$PROJECT_DIR" 2>/dev/null | tail -2 || true
free -h || true
echo

echo "## 2) NVIDIA 驱动 / CUDA"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
else
  echo "❌ 没有 nvidia-smi（驱动未装或容器未透传 GPU）"
fi
nvcc --version 2>/dev/null || echo "nvcc 未装（用 conda/pip 包通常不影响）"
echo

echo "## 3) Python / PyTorch"
if command -v python >/dev/null 2>&1; then
  python -V
  python - <<'PY'
try:
    import torch
    print("torch:", torch.__version__, "| cuda:", torch.version.cuda,
          "| available:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("device:", torch.cuda.get_device_name(0),
              "| capability:", torch.cuda.get_device_capability(0),
              "| vram:", torch.cuda.get_device_properties(0).total_memory // (1024**3), "GB")
except Exception as exc:
    print("torch 不可用:", type(exc).__name__, exc)
PY
else
  echo "❌ 没有 python"
fi
echo

echo "## 4) ffmpeg / 编码器"
ffmpeg -version 2>/dev/null | head -1 || echo "❌ 没有 ffmpeg"
ffmpeg -hide_banner -encoders 2>/dev/null | grep -E "libx264|aac" || true
echo

echo "## 5) 项目与 ComfyUI"
echo "- 项目目录：$PROJECT_DIR"
ls -d "$PROJECT_DIR"/bettercomfyui "$PROJECT_DIR"/bettercomfyui_comfy 2>/dev/null || echo "⚠️ 项目代码不在该目录"
if command -v curl >/dev/null 2>&1; then
  echo "- ComfyUI /system_stats："
  curl -s --max-time 5 http://127.0.0.1:8188/system_stats | head -c 800; echo
  echo "- doctor："
  curl -s --max-time 10 http://127.0.0.1:8188/bettercomfyui/doctor | head -c 800; echo
fi
echo

echo "## 6) 模型文件清单（H3 + 图像模型）"
if [ -d "$PROJECT_DIR/.comfyui/models" ]; then
  find "$PROJECT_DIR/.comfyui/models" -type f \( -name "*.safetensors" -o -name "*.pt" -o -name "*.gguf" \) \
    -printf "%10s  %p\n" 2>/dev/null | sort -nr | head -25 || true
else
  echo "⚠️ 未找到 .comfyui/models（路径需确认）"
fi
echo

echo "## 7) 关键 Python 包"
python - <<'PY' 2>/dev/null || true
for name in ("torch","torchvision","numpy","PIL","yaml","imageio_ffmpeg","playwright"):
    try:
        mod = __import__(name)
        print(f"{name}: OK {getattr(mod,'__version__','')}")
    except Exception as exc:
        print(f"{name}: MISSING ({type(exc).__name__})")
PY
echo

echo "## 8) 项目自检（如代码在）"
if [ -f "$PROJECT_DIR/scripts/verify_install.py" ]; then
  ( cd "$PROJECT_DIR" && python -B scripts/verify_install.py 2>&1 | tail -12 ) || true
else
  echo "⚠️ 缺少 scripts/verify_install.py"
fi
echo

echo "## 9) 结论建议"
echo "- [ ] GPU 可用（nvidia-smi 有卡）"
echo "- [ ] torch.cuda.is_available()=True 且 capability 匹配（4090=(8,9) / 5090=(12,0)）"
echo "- [ ] ffmpeg 含 libx264+aac"
echo "- [ ] ComfyUI 在 8188、doctor real_generation_available=true"
echo "- [ ] 模型文件齐全（H3 五件 + 图像 checkpoint）"
echo "- 日志已保存：$OUT"
