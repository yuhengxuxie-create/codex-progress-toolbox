[CmdletBinding()]
param(
    [double]$Seconds = 12,
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$IncludeFeishuSdk
)

$ErrorActionPreference = 'Stop'
$ResolvedToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
if ([Uri]::new($ResolvedToolsRoot).IsUnc) {
    throw 'ToolsRoot 必须是本机路径，拒绝从 UNC/网络位置加载 Python。'
}
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ResolvedToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '项目隔离 Python 不存在，请先运行 install.ps1。'
}
$ResolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$RootPrefix = $ResolvedToolsRoot.TrimEnd('\') + '\'
if (-not $ResolvedPython.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Python 解析路径越出 ToolsRoot，已拒绝执行。'
}
$Arguments = @(
    (Join-Path $ProjectRoot 'scripts\perf-probe.py'),
    '--seconds',
    ([String]$Seconds)
)
if ($IncludeFeishuSdk) {
    $Arguments += '--include-feishu-sdk'
}
& $ResolvedPython @Arguments
exit $LASTEXITCODE
