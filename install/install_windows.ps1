#requires -Version 5.1
<#
.SYNOPSIS
    剧核 DramaCore · Windows 一键安装入口（ROUND57）。
.DESCRIPTION
    只是 install_core.py 的薄包装：负责找 Python、转发参数、把退出码原样带回。
    真正的探测/计划/执行/回滚都在 install_core.py 里（跨平台同一套逻辑）。
    默认 dry-run 不会发生任何写入；真实安装必须显式加 -Yes。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_windows.ps1 -Mode attach -ComfyUI D:\ComfyUI -DryRun
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_windows.ps1 -Mode bootstrap -ComfyUI D:\ComfyUI -Backend cuda -Yes
#>
[CmdletBinding()]
param(
    [ValidateSet('attach', 'bootstrap')]
    [string]$Mode = 'attach',

    [string]$ComfyUI = '',

    [ValidateSet('auto', 'cuda', 'rocm', 'xpu', 'mps', 'ascend', 'cpu')]
    [string]$Backend = 'auto',

    [switch]$DryRun,
    [switch]$Yes,
    [switch]$Force,
    [switch]$SkipTorch,
    [switch]$SkipLauncher,
    [switch]$KeepPartial,
    [switch]$AllowExperimental,
    [switch]$Json,

    [string]$TorchIndex = '',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$core = Join-Path $scriptDir 'install_core.py'
if (-not (Test-Path -LiteralPath $core -PathType Leaf)) {
    Write-Error "找不到安装器核心：$core（install 目录不完整？）"
    exit 3
}

$pythonExe = $Python
$pythonPrefix = @()
if (-not $pythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonExe = $cmd.Source
    }
    else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            $pythonExe = $py.Source
            $pythonPrefix = @('-3')
        }
    }
}
if (-not $pythonExe) {
    Write-Error "找不到 Python。请安装 Python 3.9+ 并加入 PATH，或用 -Python 指定解释器路径。"
    exit 3
}

$pyArgs = @()
$pyArgs += $pythonPrefix
$pyArgs += @($core, '--mode', $Mode, '--backend', $Backend)
if ($ComfyUI) { $pyArgs += @('--comfyui', $ComfyUI) }
if ($TorchIndex) { $pyArgs += @('--torch-index', $TorchIndex) }
if ($Python) { $pyArgs += @('--python', $Python) }
if ($DryRun) { $pyArgs += '--dry-run' }
if ($Yes) { $pyArgs += '--yes' }
if ($Force) { $pyArgs += '--force' }
if ($SkipTorch) { $pyArgs += '--skip-torch' }
if ($SkipLauncher) { $pyArgs += '--skip-launcher' }
if ($KeepPartial) { $pyArgs += '--keep-partial' }
if ($AllowExperimental) { $pyArgs += '--allow-experimental' }
if ($Json) { $pyArgs += '--json' }

Write-Host "[剧核] 使用 Python：$pythonExe"
Write-Host "[剧核] 参数：$($pyArgs -join ' ')"
& $pythonExe @pyArgs
exit $LASTEXITCODE
