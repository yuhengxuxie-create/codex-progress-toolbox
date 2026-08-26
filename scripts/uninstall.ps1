[CmdletBinding()]
param([string]$CodexHome)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$EntryPoint = Join-Path $ProjectRoot 'progress-notify.py'

function Find-CompatiblePython {
    $candidates = @()
    if ($env:PROGRESS_PYTHON) { $candidates += $env:PROGRESS_PYTHON }
    $toolsRoot = if ($env:PROGRESS_TOOLS_ROOT) {
        $env:PROGRESS_TOOLS_ROOT
    } else {
        Join-Path $env:LOCALAPPDATA 'CodexProgressToolbox'
    }
    $candidates += (Join-Path $toolsRoot 'Python\python.exe')
    try { $candidates += (Get-Command python.exe -ErrorAction Stop).Source } catch {}
    try {
        $launcher = Get-Command py.exe -ErrorAction Stop
        $resolved = @(& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved.Count -gt 0) {
            $candidates += [string]$resolved[-1]
        }
    } catch {}
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $result = @(& $candidate -c "import sys; print('OK' if sys.version_info >= (3, 11) else 'OLD')" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $result -contains 'OK') {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        } catch {}
    }
    throw '找不到 Python >= 3.11；可设置 PROGRESS_PYTHON。'
}

$Python = Find-CompatiblePython
$arguments = @($EntryPoint, 'uninstall')
if ($CodexHome) { $arguments += @('--codex-home', $CodexHome) }
& $Python @arguments
if ($LASTEXITCODE -ne 0) {
    throw '卸载未修改配置：Codex notify 可能已被其他程序更改。'
}
Write-Host '卸载完成；原 notify 命令已恢复。请重启 Codex 桌面应用。' -ForegroundColor Yellow
