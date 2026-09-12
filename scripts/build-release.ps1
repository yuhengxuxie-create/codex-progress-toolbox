[CmdletBinding()]
param(
    [string]$OutputRoot = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'artifacts'),
    [string]$Version = '1.5.0',
    [string]$DotNetPath = '',
    [string]$TreasureChestExe = '',
    [string]$UpdaterExe = '',
    [string]$PythonInstaller = '',
    [string]$WheelSource = '',
    [string]$SensitiveYaml = '',
    [switch]$SkipBuild,
    [switch]$SkipE2E,
    [string]$LegacyV12Zip = '',
    [string]$LegacyEcosystemZip = '',
    [string]$RuntimeSeedPython = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $RepoRoot 'installer\_common.ps1')
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw '版本号必须是 x.y.z。' }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if ([Uri]::new($OutputRoot).IsUnc) { throw '输出必须位于本机磁盘。' }
$DriveRoot = [IO.Path]::GetPathRoot($OutputRoot)
if ($OutputRoot.TrimEnd('\').Equals($DriveRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw '拒绝直接输出到磁盘根目录。'
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$StagingRoot = Join-Path $OutputRoot ('.staging-release-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $StagingRoot | Out-Null

function Remove-StagingSafely {
    param([string]$Path)
    $Full = [IO.Path]::GetFullPath($Path)
    if (-not (Test-PathWithinRoot -Path $Full -Root $OutputRoot) -or
        -not [IO.Path]::GetFileName($Full).StartsWith('.staging-release-', [StringComparison]::Ordinal)) {
        throw "拒绝清理非本轮临时目录：$Full"
    }
    if (Test-Path -LiteralPath $Full -PathType Container) { Remove-Item -LiteralPath $Full -Recurse -Force }
}

function Copy-RepositorySnapshot {
    param([string]$Destination)
    Copy-TreeChecked -Source $RepoRoot -Destination $Destination `
        -ExcludedSegments @('.git', 'payload', 'artifacts', 'release-local', '.dotnet', 'bin', 'obj', 'build', 'dist', 'exports', '__pycache__', '.pytest_cache', 'logs', '.secrets', '.state') `
        -ExcludedLeaves @('config.yaml', 'config.local.json', 'config.json', 'SHA256SUMS.txt')
}

function Write-InternalChecksums {
    param([string]$Root)
    $Checksum = Join-Path $Root 'SHA256SUMS.txt'
    if (Test-Path -LiteralPath $Checksum) { Remove-Item -LiteralPath $Checksum -Force }
    $RootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $Lines = Get-ChildItem -LiteralPath $RootFull -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
        $Relative = $_.FullName.Substring($RootFull.Length).TrimStart('\').Replace('\', '/')
        '{0} *{1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $Relative
    }
    Set-Content -LiteralPath $Checksum -Value $Lines -Encoding UTF8
}

function New-PackageTree {
    param([string]$Type, [string]$Destination)
    Copy-RepositorySnapshot -Destination $Destination
    New-Item -ItemType Directory -Force -Path (Join-Path $Destination 'payload\treasure-chest\resources'), (Join-Path $Destination 'payload\offline\wheels') | Out-Null
    Copy-Item -LiteralPath $TreasureChestExe -Destination (Join-Path $Destination 'payload\treasure-chest\TreasureChest.exe') -Force
    Copy-Item -LiteralPath $UpdaterExe -Destination (Join-Path $Destination 'payload\treasure-chest\Ecosystem.Updater.exe') -Force
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'components\treasure-chest\treasurechest.root') -Destination (Join-Path $Destination 'payload\treasure-chest\treasurechest.root') -Force
    Copy-TreeChecked -Source (Join-Path $RepoRoot 'components\treasure-chest\resources') -Destination (Join-Path $Destination 'payload\treasure-chest\resources')
    Copy-Item -LiteralPath $PythonInstaller -Destination (Join-Path $Destination 'payload\offline\python-3.13.14-amd64.exe') -Force
    Copy-TreeChecked -Source $WheelSource -Destination (Join-Path $Destination 'payload\offline\wheels')
    Set-Content -LiteralPath (Join-Path $Destination 'PACKAGE_TYPE.txt') -Value $Type -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $Destination 'PACKAGE_VERSION.txt') -Value $Version -Encoding ASCII
    & (Join-Path $Destination 'scripts\privacy-scan.ps1') -Root $Destination -SensitiveYaml $SensitiveYaml
    Write-InternalChecksums -Root $Destination
    & (Join-Path $Destination 'scripts\validate-package-structure.ps1') -Root $Destination
}

try {
    # 隐私扫描在每个待发布包目录上执行；被 .gitignore 排除的本机运行状态不会进入包。
    & (Join-Path $RepoRoot 'scripts\validate-package-structure.ps1') -Root $RepoRoot

    if (-not $SkipBuild) {
        & (Join-Path $RepoRoot 'components\treasure-chest\scripts\build.ps1') -DotNetPath $DotNetPath -Version $Version
        if ($LASTEXITCODE -ne 0) { throw 'TreasureChest 构建失败。' }
    }
    if ([string]::IsNullOrWhiteSpace($TreasureChestExe)) {
        $TreasureChestExe = Join-Path $RepoRoot 'components\treasure-chest\build\publish\TreasureChest.exe'
    }
    if ([string]::IsNullOrWhiteSpace($UpdaterExe)) {
        $UpdaterExe = Join-Path $RepoRoot 'components\treasure-chest\build\publish\Ecosystem.Updater.exe'
    }
    if (-not (Test-Path -LiteralPath $TreasureChestExe -PathType Leaf)) { throw '缺少 TreasureChest 发布 EXE。' }
    if (-not (Test-Path -LiteralPath $UpdaterExe -PathType Leaf)) { throw '缺少独立生态更新器 EXE。' }
    $VersionInfo = [Diagnostics.FileVersionInfo]::GetVersionInfo((Resolve-Path -LiteralPath $TreasureChestExe).Path)
    if ($VersionInfo.FileVersion -ne ($Version + '.0') -or $VersionInfo.ProductVersion -notlike ($Version + '*')) {
        throw "TreasureChest 版本异常：File=$($VersionInfo.FileVersion), Product=$($VersionInfo.ProductVersion)"
    }
    $UpdaterVersionInfo = [Diagnostics.FileVersionInfo]::GetVersionInfo((Resolve-Path -LiteralPath $UpdaterExe).Path)
    if ($UpdaterVersionInfo.FileVersion -ne ($Version + '.0') -or $UpdaterVersionInfo.ProductVersion -notlike ($Version + '*')) {
        throw "生态更新器版本异常：File=$($UpdaterVersionInfo.FileVersion), Product=$($UpdaterVersionInfo.ProductVersion)"
    }

    if ([string]::IsNullOrWhiteSpace($PythonInstaller)) {
        $CacheRoot = Join-Path $OutputRoot '.release-cache'
        New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
        $PythonInstaller = Join-Path $CacheRoot 'python-3.13.14-amd64.exe'
        if (-not (Test-Path -LiteralPath $PythonInstaller -PathType Leaf)) {
            Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe' -OutFile $PythonInstaller -UseBasicParsing
        }
    }
    $PythonSignature = Get-AuthenticodeSignature -LiteralPath $PythonInstaller
    if ($PythonSignature.Status -ne 'Valid' -or $PythonSignature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
        throw 'Python 官方安装器签名无效。'
    }

    if ([string]::IsNullOrWhiteSpace($WheelSource)) {
        $WheelSource = Join-Path $OutputRoot '.release-cache\wheels'
        New-Item -ItemType Directory -Force -Path $WheelSource | Out-Null
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $PythonCommand) { throw '自动准备 wheel 需要可用的 python；也可通过 -WheelSource 显式指定。' }
        & $PythonCommand.Source -m pip download --disable-pip-version-check --only-binary=:all: --platform win_amd64 --python-version 313 --implementation cp --require-hashes --dest $WheelSource -r (Join-Path $RepoRoot 'components\codex-feishu\requirements-feishu.txt')
        if ($LASTEXITCODE -ne 0) { throw '下载哈希锁定 wheel 失败。' }
    }
    if (@(Get-ChildItem -LiteralPath $WheelSource -Filter '*.whl' -File).Count -lt 10) { throw 'wheel 缓存不完整。' }

    $FullRoot = Join-Path $StagingRoot "codex-feishu-ecosystem-v$Version-full"
    $UpgradeRoot = Join-Path $StagingRoot "codex-feishu-ecosystem-v$Version-upgrade-from-v1.x"
    New-PackageTree -Type full -Destination $FullRoot
    New-PackageTree -Type 'upgrade-from-v1.x' -Destination $UpgradeRoot

    if (-not $SkipE2E) {
        & (Join-Path $FullRoot 'scripts\test-full-install.ps1') -PackageRoot $FullRoot -RuntimeSeedPython $RuntimeSeedPython
        if ($LASTEXITCODE -ne 0) { throw '全新安装 E2E 失败。' }
        if (-not [string]::IsNullOrWhiteSpace($LegacyV12Zip) -and -not [string]::IsNullOrWhiteSpace($LegacyEcosystemZip)) {
            & (Join-Path $FullRoot 'scripts\test-upgrades.ps1') -PackageRoot $FullRoot -LegacyV12Zip $LegacyV12Zip -LegacyEcosystemZip $LegacyEcosystemZip -RuntimeSeedPython $RuntimeSeedPython
            if ($LASTEXITCODE -ne 0) { throw '升级/回滚 E2E 失败。' }
        } else {
            throw '未提供两类旧版 ZIP，不能完成强制升级 E2E。'
        }
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $FullZip = Join-Path $OutputRoot "codex-feishu-ecosystem-v$Version-full.zip"
    $UpgradeZip = Join-Path $OutputRoot "codex-feishu-ecosystem-v$Version-upgrade-from-v1.x.zip"
    foreach ($Zip in @($FullZip, $UpgradeZip)) { if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force } }
    [IO.Compression.ZipFile]::CreateFromDirectory($FullRoot, $FullZip, [IO.Compression.CompressionLevel]::Optimal, $true)
    [IO.Compression.ZipFile]::CreateFromDirectory($UpgradeRoot, $UpgradeZip, [IO.Compression.CompressionLevel]::Optimal, $true)
    & (Join-Path $RepoRoot 'scripts\validate-archive.ps1') -ZipPath $FullZip
    & (Join-Path $RepoRoot 'scripts\validate-archive.ps1') -ZipPath $UpgradeZip

    $FullHash = (Get-FileHash -LiteralPath $FullZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $UpgradeHash = (Get-FileHash -LiteralPath $UpgradeZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $UpdateManifest = Join-Path $OutputRoot 'ecosystem-update.json'
    [ordered]@{
        schema_version = 1
        ecosystem_version = $Version
        minimum_upgradable_version = '1.5.0'
        release_tag = "v$Version"
        release_notes_url = "https://github.com/yuhengxuxie-create/codex-progress-toolbox/releases/tag/v$Version"
        packages = [ordered]@{
            upgrade = [ordered]@{
                name = [IO.Path]::GetFileName($UpgradeZip)
                url = "https://github.com/yuhengxuxie-create/codex-progress-toolbox/releases/download/v$Version/$([IO.Path]::GetFileName($UpgradeZip))"
                sha256 = $UpgradeHash
                size = (Get-Item -LiteralPath $UpgradeZip).Length
            }
            full = [ordered]@{
                name = [IO.Path]::GetFileName($FullZip)
                url = "https://github.com/yuhengxuxie-create/codex-progress-toolbox/releases/download/v$Version/$([IO.Path]::GetFileName($FullZip))"
                sha256 = $FullHash
                size = (Get-Item -LiteralPath $FullZip).Length
            }
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $UpdateManifest -Encoding UTF8
    $Outer = @($FullZip, $UpgradeZip, $UpdateManifest) | ForEach-Object {
        '{0} *{1}' -f (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant(), [IO.Path]::GetFileName($_)
    }
    Set-Content -LiteralPath (Join-Path $OutputRoot 'SHA256SUMS.txt') -Value $Outer -Encoding UTF8
    Write-Host "Release 资产已生成：$OutputRoot" -ForegroundColor Green
} finally {
    Remove-StagingSafely -Path $StagingRoot
}
$global:LASTEXITCODE = 0
