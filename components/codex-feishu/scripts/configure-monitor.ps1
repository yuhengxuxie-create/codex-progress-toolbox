[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '运行时不存在，请先运行 scripts\install.ps1。'
}
# 只写入用户明确选择的精确 thread ID；服务会在下一轮配置热加载时采用。
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') `
    --config (Join-Path $ProjectRoot 'config.yaml') configure-monitor --force
if ($LASTEXITCODE -ne 0) { throw 'Codex 监控对象切换失败。' }
