[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '运行时不存在，请先运行 scripts\install.ps1。'
}
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config (Join-Path $ProjectRoot 'config.yaml') test-feishu
if ($LASTEXITCODE -ne 0) { throw '飞书测试消息发送失败。' }
