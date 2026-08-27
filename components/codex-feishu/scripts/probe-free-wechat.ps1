[CmdletBinding()]
param(
    [string]$ToolAccountNickname = '',
    [switch]$SingleVisibleWindow,
    [switch]$DiagnosticUnverifiedIdentity,
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot)
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

# 默认按精确标题连接；显式单窗口模式只在唯一已展开的微信窗口上读取根身份。
$ProbeArguments = @(
    (Join-Path $ProjectRoot 'progress-wx.py'),
    '--config',
    (Join-Path $ProjectRoot 'config.yaml'),
    'probe-free-wechat'
)
if (-not [String]::IsNullOrWhiteSpace($ToolAccountNickname)) {
    $ProbeArguments += @('--nickname', $ToolAccountNickname)
}
if ($SingleVisibleWindow) {
    $ProbeArguments += '--single-visible-window'
}
if ($DiagnosticUnverifiedIdentity) {
    $ProbeArguments += '--diagnostic-unverified-identity'
}
& $ResolvedPython @ProbeArguments
if ($LASTEXITCODE -ne 0) {
    throw '免费微信 UIA 只读探针未通过。'
}
