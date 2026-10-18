# Better ComfyUI —— 目标机（Windows）环境体检脚本
# 用法（在目标机 PowerShell 里）：
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\remote_env_report.ps1 -ProjectDir "C:\BetterComfyUI"
# 只读为主：不做安装、不排队。输出报告到 $ProjectDir\remote_env_report.md

param(
    [string]$ProjectDir = "$PWD",
    [string]$ComfyUrl = "http://127.0.0.1:8188"
)

$ErrorActionPreference = "Continue"
$out = Join-Path $ProjectDir "remote_env_report.md"
$lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text) { $lines.Add($text) | Out-Null; Write-Output $text }

Add-Line "# 目标机环境体检报告（Windows）"
Add-Line ("- 时间：" + (Get-Date -Format o))
Add-Line ("- 主机：" + $env:COMPUTERNAME)
Add-Line ""

Add-Line "## 1) 系统 / 内存 / 磁盘"
Add-Line ("- OS：" + (Get-CimInstance Win32_OperatingSystem).Caption)
$mem = Get-CimInstance Win32_ComputerSystem
Add-Line ("- 内存：" + [math]::Round($mem.TotalPhysicalMemory / 1GB, 1) + " GB")
Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -eq "C" -or $_.Name -eq "D" } |
    ForEach-Object { Add-Line ("- 磁盘 " + $_.Name + ": 可用 " + [math]::Round($_.Free / 1GB, 1) + " GB") }
Add-Line ""

Add-Line "## 2) NVIDIA 驱动 / GPU"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Add-Line '```'
    Add-Line (nvidia-smi | Out-String)
    Add-Line '```'
    Add-Line (nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv | Out-String)
} else {
    Add-Line "❌ 没有 nvidia-smi（驱动未装或非 NVIDIA 机器）"
}
Add-Line ""

Add-Line "## 3) Python / PyTorch"
if (Get-Command python -ErrorAction SilentlyContinue) {
    Add-Line ("- python: " + (python -V 2>&1))
    Add-Line '```'
    Add-Line (python -c "import torch;print('torch:',torch.__version__,'cuda:',torch.version.cuda,'available:',torch.cuda.is_available());print('capability:',torch.cuda.get_device_capability(0) if torch.cuda.is_available() else None)" 2>&1 | Out-String)
    Add-Line '```'
} else { Add-Line "❌ 没有 python" }
Add-Line ""

Add-Line "## 4) ffmpeg"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Add-Line ("- " + ((ffmpeg -version 2>&1) | Select-Object -First 1))
} else { Add-Line "❌ 没有 ffmpeg" }
Add-Line ""

Add-Line "## 5) ComfyUI"
try {
    $stats = Invoke-RestMethod "$ComfyUrl/system_stats" -TimeoutSec 8
    Add-Line ("- /system_stats devices：" + ($stats.devices | ConvertTo-Json -Compress))
} catch { Add-Line ("- ❌ ComfyUI 不可达：" + $_.Exception.Message) }
try {
    $doc = Invoke-RestMethod "$ComfyUrl/bettercomfyui/doctor" -TimeoutSec 15
    Add-Line ("- doctor node_count=" + $doc.node_count + " real_generation_available=" + $doc.real_generation_available)
} catch { Add-Line "- doctor 不可达（插件可能未加载）" }
Add-Line ""

Add-Line "## 6) 项目与模型"
Add-Line ("- 项目目录：" + $ProjectDir)
Add-Line ("- 代码存在：" + (Test-Path (Join-Path $ProjectDir "bettercomfyui")) + " / " + (Test-Path (Join-Path $ProjectDir "bettercomfyui_comfy")))
$models = Join-Path $ProjectDir ".comfyui\models"
if (Test-Path $models) {
    Get-ChildItem $models -Recurse -File -Include *.safetensors,*.pt,*.gguf -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 20 |
        ForEach-Object { Add-Line ("- " + [math]::Round($_.Length / 1GB, 2) + " GB  " + $_.FullName) }
} else { Add-Line "⚠️ 未找到 .comfyui\models" }
Add-Line ""

Add-Line "## 7) 项目自检"
$verify = Join-Path $ProjectDir "scripts\verify_install.py"
if (Test-Path $verify) {
    Push-Location $ProjectDir
    Add-Line '```'
    Add-Line (python -B scripts/verify_install.py 2>&1 | Out-String)
    Add-Line '```'
    Pop-Location
} else { Add-Line "⚠️ 缺少 scripts/verify_install.py" }

Add-Line ""
Add-Line "## 8) 结论清单"
Add-Line "- [ ] nvidia-smi 有卡；torch.cuda.is_available()=True"
Add-Line "- [ ] capability 匹配（4090=(8,9) / 5090=(12,0)）"
Add-Line "- [ ] ffmpeg 可用"
Add-Line "- [ ] ComfyUI 8188 在线且 real_generation_available=true"
Add-Line "- [ ] 模型齐全（H3 五件 + 图像 checkpoint）"

$lines -join "`n" | Set-Content -Path $out -Encoding UTF8
Write-Output "`n[OK] 报告已保存：$out"
