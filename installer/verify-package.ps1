[CmdletBinding()]
param([string]$PackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot '_common.ps1')

$PackageRoot = [IO.Path]::GetFullPath($PackageRoot)
if ([Uri]::new($PackageRoot).IsUnc) { throw '请先把包完整复制到本机磁盘。' }
Assert-NoReparsePoints -Root $PackageRoot

$ChecksumFile = Join-Path $PackageRoot 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $ChecksumFile -PathType Leaf)) { throw '缺少 SHA256SUMS.txt。' }
$Required = @(
    'PACKAGE_TYPE.txt', 'AGENTS.md', 'README.md', 'LICENSE',
    'components\codex-feishu\progress-wx.py',
    'components\treasure-chest\src\TreasureChest.App\TreasureChest.App.csproj',
    'payload\treasure-chest\TreasureChest.exe',
    'payload\offline\python-3.13.14-amd64.exe',
    'payload\offline\wheels',
    'installer\install.ps1', 'installer\upgrade.ps1', 'installer\rollback.ps1'
)
foreach ($Relative in $Required) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot $Relative))) { throw "包缺少必要内容：$Relative" }
}

$PackageType = (Get-Content -LiteralPath (Join-Path $PackageRoot 'PACKAGE_TYPE.txt') -Raw).Trim()
if ($PackageType -notin @('full', 'upgrade-from-v1.x')) { throw "未知包类型：$PackageType" }

$ExpectedPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$Verified = 0
foreach ($Line in Get-Content -LiteralPath $ChecksumFile -Encoding UTF8) {
    if ([string]::IsNullOrWhiteSpace($Line)) { continue }
    if ($Line -notmatch '^([0-9A-Fa-f]{64})\s+\*(.+)$') { throw "哈希清单格式错误：$Line" }
    $Expected = $Matches[1].ToUpperInvariant()
    $Relative = $Matches[2].Replace('/', '\')
    if ([IO.Path]::IsPathRooted($Relative) -or @($Relative -split '[\\/]') -contains '..') {
        throw "哈希清单路径越界：$Relative"
    }
    $File = [IO.Path]::GetFullPath((Join-Path $PackageRoot $Relative))
    if (-not (Test-PathWithinRoot -Path $File -Root $PackageRoot)) { throw "哈希清单路径越界：$Relative" }
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "文件缺失：$Relative" }
    $Actual = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash
    if ($Actual -ne $Expected) { throw "文件校验失败：$Relative" }
    [void]$ExpectedPaths.Add($Relative)
    $Verified++
}
if ($Verified -lt 100) { throw "哈希条目异常少：$Verified" }

$Unlisted = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force | Where-Object {
    $Relative = $_.FullName.Substring($PackageRoot.TrimEnd('\').Length).TrimStart('\')
    $_.Name -ne 'SHA256SUMS.txt' -and -not $ExpectedPaths.Contains($Relative)
})
if ($Unlisted.Count -gt 0) { throw "包中存在未列入哈希的文件：$($Unlisted[0].FullName)" }

$PythonInstaller = Join-Path $PackageRoot 'payload\offline\python-3.13.14-amd64.exe'
$Signature = Get-AuthenticodeSignature -LiteralPath $PythonInstaller
if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
    throw 'Python 官方安装器签名无效。'
}

Write-Host "安装包校验通过：类型 $PackageType，$Verified 个文件，Python 官方签名有效。" -ForegroundColor Green
$global:LASTEXITCODE = 0
