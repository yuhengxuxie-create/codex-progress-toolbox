[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$LegacyRoot,
    [string]$InstallRoot = '',
    [switch]$NonInteractive,
    [switch]$NoLaunch,
    [switch]$NoShortcuts,
    [switch]$SkipCodexIntegration,
    [switch]$SkipRuntimeInstall,
    [switch]$SkipServiceControl,
    [string]$CodexHomePath = '',
    [string]$RuntimeSeedPython = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot '_common.ps1')

function Get-LegacyKind {
    param([string]$Root)
    if ((Test-Path -LiteralPath (Join-Path $Root 'TreasureChest.exe') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root 'components\ProgressChecking(WX)\progress-wx.py') -PathType Leaf)) {
        return 'ecosystem-installed'
    }
    if ((Test-Path -LiteralPath (Join-Path $Root 'PACKAGE_VERSION.txt') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root 'payload\ProgressChecking(WX)\progress-wx.py') -PathType Leaf)) {
        return 'ecosystem-package'
    }
    if ((Test-Path -LiteralPath (Join-Path $Root 'config.example.json') -PathType Leaf) -and
        ((Test-Path -LiteralPath (Join-Path $Root 'progress-notify.py') -PathType Leaf) -or
         (Test-Path -LiteralPath (Join-Path $Root 'src\progress_notify') -PathType Container))) {
        return 'github-v1.2'
    }
    throw '无法识别旧版来源；未执行升级。'
}

function Get-V1ThreadIds {
    param([string]$Root)
    $LocalConfig = Join-Path $Root 'config.local.json'
    if (-not (Test-Path -LiteralPath $LocalConfig -PathType Leaf)) { return @() }
    try { $Value = Get-Content -LiteralPath $LocalConfig -Raw | ConvertFrom-Json }
    catch { throw '旧 config.local.json 无法安全解析；只保留备份，不迁移。' }
    $Raw = $Value.thread_ids
    if ($null -eq $Raw) { return @() }
    $Candidates = @()
    if ($Raw -is [string]) { $Candidates = @($Raw -split '[,;\s]+' | Where-Object { $_ }) }
    elseif ($Raw -is [System.Collections.IEnumerable]) { $Candidates = @($Raw) }
    else { throw '旧 thread_ids 类型无效；拒绝迁移。' }
    $Ids = New-Object System.Collections.Generic.List[string]
    foreach ($Candidate in $Candidates) {
        $Id = ([string]$Candidate).Trim()
        if ($Id -notmatch '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$') {
            throw '旧 thread_ids 含不合法值；拒绝迁移并保留完整备份。'
        }
        if (-not $Ids.Contains($Id)) { $Ids.Add($Id) }
    }
    return @($Ids)
}

function Get-LegacyProgressRoot {
    param([string]$Root, [string]$Kind)
    if ($Kind -eq 'ecosystem-installed') { return Join-Path $Root 'components\ProgressChecking(WX)' }
    if ($Kind -eq 'ecosystem-package') { return Join-Path $Root 'payload\ProgressChecking(WX)' }
    return ''
}

function Get-LegacyPython {
    param([string]$Root, [string]$Kind)
    if ($Kind -eq 'ecosystem-installed') { return Join-Path $Root 'components\Python313-ProgressWX\python.exe' }
    return ''
}

function Copy-LegacyPrivateData {
    param([string]$SourceRoot, [string]$DestinationRoot)
    if ([string]::IsNullOrWhiteSpace($SourceRoot)) { return }
    $Config = Join-Path $SourceRoot 'config.yaml'
    if (Test-Path -LiteralPath $Config -PathType Leaf) {
        Copy-Item -LiteralPath $Config -Destination (Join-Path $DestinationRoot 'config.yaml') -Force
    }
    foreach ($Private in @('.secrets', '.state', 'logs')) {
        $Source = Join-Path $SourceRoot $Private
        if (Test-Path -LiteralPath $Source -PathType Container) {
            Copy-TreeChecked -Source $Source -Destination (Join-Path $DestinationRoot $Private)
        }
    }
}

& (Join-Path $PSScriptRoot 'verify-package.ps1') -PackageRoot $PackageRoot
$LegacyRoot = Resolve-SafeLocalRoot -Path $LegacyRoot
if (-not (Test-Path -LiteralPath $LegacyRoot -PathType Container)) { throw '旧版目录不存在。' }
$Kind = Get-LegacyKind -Root $LegacyRoot
if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $InstallRoot = if ($Kind -eq 'ecosystem-installed') { $LegacyRoot } else { Get-DefaultEcosystemRoot }
}
$InstallRoot = Resolve-SafeLocalRoot -Path $InstallRoot
if (Test-PathWithinRoot -Path $InstallRoot -Root $PackageRoot) { throw '安装目录不能位于升级包内部。' }
if (-not $InstallRoot.Equals($LegacyRoot, [StringComparison]::OrdinalIgnoreCase)) {
    if (Test-PathWithinRoot -Path $InstallRoot -Root $LegacyRoot) { throw '新安装目录不能位于旧版目录内部。' }
    if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
        if (@(Get-ChildItem -LiteralPath $InstallRoot -Force).Count -gt 0) {
            throw '目标安装目录非空；请选择空目录或旧完整生态原目录。'
        }
    }
}

$V1Ids = @()
if ($Kind -eq 'github-v1.2') { $V1Ids = @(Get-V1ThreadIds -Root $LegacyRoot) }
$Transaction = New-EcosystemTransaction -InstallRoot $InstallRoot -Kind upgrade -LegacyRoot $LegacyRoot -CodexHomePath $CodexHomePath
$WasRunning = $false
$LegacyProgress = Get-LegacyProgressRoot -Root $LegacyRoot -Kind $Kind
$LegacyPython = Get-LegacyPython -Root $LegacyRoot -Kind $Kind
try {
    if ($Kind -eq 'ecosystem-installed' -and -not $SkipServiceControl -and
        (Test-Path -LiteralPath $LegacyPython -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $LegacyProgress 'config.yaml') -PathType Leaf)) {
        & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') status *> $null
        $WasRunning = $LASTEXITCODE -eq 0
        if ($WasRunning) {
            & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') stop --timeout 30
            if ($LASTEXITCODE -ne 0) { throw '旧后台无法安全停止。' }
        }
    }
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ legacy_kind = $Kind; legacy_service_was_running = $WasRunning }

    if ($Kind -eq 'ecosystem-installed' -and -not $SkipCodexIntegration) {
        & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') uninstall-permission-hook
        if ($LASTEXITCODE -ne 0) { throw '旧 PermissionRequest Hook 无法安全移除。' }
        & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') uninstall-notify
        if ($LASTEXITCODE -ne 0) { throw '旧 notify 无法安全恢复。' }
    }

    Install-EcosystemFiles -PackageRoot $PackageRoot -InstallRoot $InstallRoot
    $NewProgress = Join-Path $InstallRoot 'components\codex-feishu'
    Copy-LegacyPrivateData -SourceRoot $LegacyProgress -DestinationRoot $NewProgress
    if (-not $LegacyRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $LegacyTreasureConfig = Join-Path $LegacyRoot 'config.json'
        if (Test-Path -LiteralPath $LegacyTreasureConfig -PathType Leaf) {
            Copy-Item -LiteralPath $LegacyTreasureConfig -Destination (Join-Path $InstallRoot 'config.json') -Force
        }
    }

    $Python = Install-PythonRuntime -PackageRoot $PackageRoot -InstallRoot $InstallRoot -SkipRuntimeInstall:$SkipRuntimeInstall -RuntimeSeedPython $RuntimeSeedPython
    $Config = Initialize-EcosystemConfig -InstallRoot $InstallRoot
    if ($V1Ids.Count -gt 0) {
        $Plan = Join-Path $InstallRoot '.ecosystem\v1-monitor-migration.json'
        Write-JsonFile -Path $Plan -Value @{ schema_version = 1; thread_ids = $V1Ids }
        if ($SkipRuntimeInstall) { throw '存在 v1 thread-id 迁移时不能跳过 Python 运行时。' }
        & $Python (Join-Path $PSScriptRoot 'migrate-v1.py') $Config $Plan
        if ($LASTEXITCODE -ne 0) { throw 'v1 thread-id 迁移失败。' }
    }
    if (-not $SkipCodexIntegration) { Install-CodexIntegration -InstallRoot $InstallRoot -PythonExe $Python }
    Test-EcosystemHealth -InstallRoot $InstallRoot -PythonExe $Python -SkipRuntimeCheck:$SkipRuntimeInstall

    if ($WasRunning -and -not $SkipServiceControl) {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $NewProgress 'scripts\start.ps1') -ToolsRoot (Join-Path $InstallRoot 'components')
        if ($LASTEXITCODE -ne 0) { throw '新后台未能恢复原运行状态。' }
    }
    $Metadata = [ordered]@{
        schema_version = 1
        ecosystem_version = $script:EcosystemVersion
        upgraded_at = (Get-Date).ToUniversalTime().ToString('o')
        legacy_kind = $Kind
        legacy_root = $LegacyRoot
        install_root = $InstallRoot
        migrated_thread_count = $V1Ids.Count
        transaction_manifest = $Transaction.ManifestPath
        service_started = $WasRunning
    }
    Write-JsonFile -Path (Join-Path $InstallRoot '.ecosystem\installation.json') -Value $Metadata
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ status = 'completed'; completed_at = (Get-Date).ToUniversalTime().ToString('o') }
    Write-Host "升级完成：$Kind -> v1.5.0" -ForegroundColor Green
    Write-Host "事务备份：$($Transaction.ManifestPath)"
    if ($Kind -eq 'github-v1.2') {
        Write-Host '旧 Webhook 与 config.local.json 未复用。下一步必须创建企业自建应用并重新绑定用户。'
    }
} catch {
    $Failure = $_
    try { Restore-EcosystemTransaction -ManifestPath $Transaction.ManifestPath } catch { Write-Warning "自动回滚失败：$($_.Exception.Message)" }
    throw $Failure
}

if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $InstallRoot 'TreasureChest.exe') -WorkingDirectory $InstallRoot
}
$global:LASTEXITCODE = 0
