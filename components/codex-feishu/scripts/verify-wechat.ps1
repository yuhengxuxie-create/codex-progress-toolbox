[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config (Join-Path $ProjectRoot 'config.yaml') verify-wechat
exit $LASTEXITCODE
