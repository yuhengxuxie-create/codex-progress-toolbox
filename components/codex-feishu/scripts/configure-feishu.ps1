[CmdletBinding()]
param([string]$ToolsRoot = $(if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) 'Python313-ProgressWX\python.exe') -PathType Leaf) { Split-Path -Parent $PSScriptRoot } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }))

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '运行时不存在，请先运行 scripts\install.ps1。'
}
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config (Join-Path $ProjectRoot 'config.yaml') configure-feishu
if ($LASTEXITCODE -ne 0) { throw '飞书凭证配置失败。' }
