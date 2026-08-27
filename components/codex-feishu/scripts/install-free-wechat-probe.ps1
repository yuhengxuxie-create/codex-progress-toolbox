[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ResolvedToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
if ([Uri]::new($ResolvedToolsRoot).IsUnc) {
    throw 'ToolsRoot 必须是本机路径，拒绝从 UNC/网络位置加载 Python。'
}
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ResolvedToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '兼容运行时不存在，请先运行 install.ps1。'
}
$ResolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$RootPrefix = $ResolvedToolsRoot.TrimEnd('\') + '\'
if (-not $ResolvedPython.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Python 解析路径越出 ToolsRoot，已拒绝执行。'
}
& $ResolvedPython -m pip install --disable-pip-version-check --no-input `
    -r (Join-Path $ProjectRoot 'requirements-wechat-free.txt')
if ($LASTEXITCODE -ne 0) { throw '安装免费微信 UIA 只读探针依赖失败。' }
Write-Host '免费 UIA 只读探针已安装；无需激活码，不会自动操作微信。'
