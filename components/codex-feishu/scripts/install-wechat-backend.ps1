[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '兼容运行时不存在，请先运行 install.ps1。'
}
& $PythonExe -m pip install --disable-pip-version-check --no-input -r (Join-Path $ProjectRoot 'requirements-wechat.txt')
if ($LASTEXITCODE -ne 0) { throw '安装 wxautox4 失败。' }
Write-Host 'wxautox4 已安装。请根据官方说明自行执行 wxautox4 auth activate，并避免把激活码写入共享文件。'
