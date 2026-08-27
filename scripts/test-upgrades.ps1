[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$LegacyV12Zip,
    [Parameter(Mandatory = $true)][string]$LegacyEcosystemZip,
    [string]$RuntimeSeedPython = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$LegacyV12Zip = (Resolve-Path -LiteralPath $LegacyV12Zip).Path
$LegacyEcosystemZip = (Resolve-Path -LiteralPath $LegacyEcosystemZip).Path
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('CodexFeishu-UpgradeE2E-' + [Guid]::NewGuid().ToString('N'))
Write-Host "Upgrade E2E temp root: $TestRoot"
. (Join-Path $PackageRoot 'installer\_common.ps1')

function Resolve-ExpandedRoot {
    param([string]$Root)
    $Children = @(Get-ChildItem -LiteralPath $Root -Force)
    if ($Children.Count -eq 1 -and $Children[0].PSIsContainer) { return $Children[0].FullName }
    return $Root
}

function Get-TreeDigest {
    param([string]$Root)
    $Full = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $Lines = Get-ChildItem -LiteralPath $Full -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
        $Relative = $_.FullName.Substring($Full.Length).TrimStart('\').Replace('\', '/')
        '{0} {1}' -f $Relative, (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
    $Joined = $Lines -join "`n"
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Joined)
    $Sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Sha.ComputeHash($Bytes))).Replace('-', '') } finally { $Sha.Dispose() }
}

function Test-OneUpgrade {
    param([string]$Zip, [string]$Name, [switch]$V12)
    $Expanded = Join-Path $TestRoot ($Name + '-expanded')
    Expand-Archive -LiteralPath $Zip -DestinationPath $Expanded
    $LegacyRoot = Resolve-ExpandedRoot -Root $Expanded
    $TestCodexHome = Join-Path $TestRoot ($Name + '-codex')
    New-Item -ItemType Directory -Force -Path $TestCodexHome | Out-Null
    Set-Content -LiteralPath (Join-Path $TestCodexHome 'config.toml') -Value "model = 'upgrade-test'" -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $TestCodexHome 'hooks.json') -Value '{"hooks":{"Stop":[]}}' -Encoding UTF8

    if ($V12) {
        $SyntheticId = '01addddd-3333-4333-8333-333333333333'
        @{ thread_ids = $SyntheticId; notification = @{ webhook_url = 'not-migrated' } } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $LegacyRoot 'config.local.json') -Encoding UTF8
    } else {
        $OldProgress = Join-Path $LegacyRoot 'payload\ProgressChecking(WX)'
        Copy-Item -LiteralPath (Join-Path $OldProgress 'config.example.yaml') -Destination (Join-Path $OldProgress 'config.yaml') -Force
        New-Item -ItemType Directory -Force -Path (Join-Path $OldProgress '.state') | Out-Null
        Set-Content -LiteralPath (Join-Path $OldProgress '.state\preserve-marker.txt') -Value 'preserve-me' -Encoding ASCII
        Set-Content -LiteralPath (Join-Path $LegacyRoot 'config.json') -Value '{"sessions":[]}' -Encoding UTF8
    }
    if (-not [string]::IsNullOrWhiteSpace($RuntimeSeedPython) -and (Test-Path -LiteralPath $RuntimeSeedPython -PathType Leaf)) {
        $SeedVersion = (& $RuntimeSeedPython -c 'import platform; print(platform.python_version())').Trim()
        if ($SeedVersion -ne '3.13.14') { throw '升级 E2E 运行时种子版本错误。' }
        Copy-TreeChecked -Source (Split-Path -Parent $RuntimeSeedPython) -Destination (Join-Path $LegacyRoot 'components\Python313-ProgressWX')
    }
    $Before = Get-TreeDigest -Root $LegacyRoot

    & (Join-Path $PackageRoot 'installer\upgrade.ps1') -LegacyRoot $LegacyRoot -InstallRoot $LegacyRoot -NonInteractive -NoLaunch -NoShortcuts -SkipCodexIntegration -SkipServiceControl -CodexHomePath $TestCodexHome -RuntimeSeedPython $RuntimeSeedPython
    if ($LASTEXITCODE -ne 0) { throw "$Name 升级失败。" }
    $NewProgress = Join-Path $LegacyRoot 'components\codex-feishu'
    if (-not (Test-Path -LiteralPath (Join-Path $LegacyRoot 'TreasureChest.exe') -PathType Leaf)) { throw "$Name 升级缺少 TreasureChest。" }
    if ($V12) {
        $Python = Join-Path $LegacyRoot 'components\Python313-ProgressWX\python.exe'
        $Config = Join-Path $NewProgress 'config.yaml'
        $Found = (& $Python -c "import yaml; d=yaml.safe_load(open(r'$($Config.Replace("'", "''"))',encoding='utf-8-sig')); print(int('01addddd-3333-4333-8333-333333333333' in d['monitor']['ids']))").Trim()
        if ($Found -ne '1') { throw 'v1 合法 thread-id 未迁移。' }
        if ((Get-Content -LiteralPath $Config -Raw) -match 'not-migrated') { throw 'v1 Webhook 被错误迁移。' }
    } else {
        if (-not (Test-Path -LiteralPath (Join-Path $NewProgress '.state\preserve-marker.txt') -PathType Leaf)) { throw '旧状态标记未保留。' }
        if (-not (Test-Path -LiteralPath (Join-Path $LegacyRoot 'config.json') -PathType Leaf)) { throw 'TreasureChest config.json 未保留。' }
    }

    $Metadata = Get-Content -LiteralPath (Join-Path $LegacyRoot '.ecosystem\installation.json') -Raw | ConvertFrom-Json
    & (Join-Path $PackageRoot 'installer\rollback.ps1') -TransactionManifest ([string]$Metadata.transaction_manifest)
    $After = Get-TreeDigest -Root $LegacyRoot
    if ($After -ne $Before) { throw "$Name 回滚后文件树不等于升级前。" }
    Write-Host "$Name 升级与回滚 E2E 通过。" -ForegroundColor Green
}

function Remove-TestRootSafely {
    $TempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $Full = [IO.Path]::GetFullPath($TestRoot)
    if (-not $Full.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($Full).StartsWith('CodexFeishu-UpgradeE2E-', [StringComparison]::Ordinal)) {
        throw "拒绝清理无法确认的测试目录：$Full"
    }
    if (Test-Path -LiteralPath $Full) {
        for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
            try { Remove-Item -LiteralPath $Full -Recurse -Force -ErrorAction Stop; break }
            catch {
                if (-not (Test-Path -LiteralPath $Full)) { break }
                if ($Attempt -eq 10) { Write-Warning "临时目录仍被占用，保留供构建后复检：$Full"; break }
                Start-Sleep -Milliseconds (200 * $Attempt)
            }
        }
    }
}

try {
    New-Item -ItemType Directory -Force -Path $TestRoot | Out-Null
    Test-OneUpgrade -Zip $LegacyV12Zip -Name 'github-v1.2' -V12
    Test-OneUpgrade -Zip $LegacyEcosystemZip -Name 'ecosystem-2026.08.25'
} finally {
    Remove-TestRootSafely
}
$global:LASTEXITCODE = 0
