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
    [string]$RuntimeSeedPython = '',
    [string]$ProgressFile = '',
    [switch]$CleanupBackupOnSuccess
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot '_common.ps1')

function Write-UpgradeProgress {
    param([int]$Percent, [string]$Stage, [string]$Message, [string]$State = 'running')
    if ([string]::IsNullOrWhiteSpace($ProgressFile)) { return }
    $Temporary = $null
    try {
        $Full = [IO.Path]::GetFullPath($ProgressFile)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Full) | Out-Null
        $Temporary = $Full + '.tmp-' + [Guid]::NewGuid().ToString('N')
        [ordered]@{
            schema_version = 1
            percent = [Math]::Min(100, [Math]::Max(0, $Percent))
            stage = $Stage
            message = $Message
            state = $State
            updated_at = (Get-Date).ToUniversalTime().ToString('o')
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Temporary -Encoding UTF8
        Move-Item -LiteralPath $Temporary -Destination $Full -Force
    } catch {
        Write-Warning 'Unable to update progress display; the installation transaction remains authoritative.'
    } finally {
        if ($Temporary -and (Test-Path -LiteralPath $Temporary -PathType Leaf)) {
            Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-LegacyKind {
    param([string]$Root)
    if ((Test-Path -LiteralPath (Join-Path $Root 'TreasureChest.exe') -PathType Leaf) -and
        ((Test-Path -LiteralPath (Join-Path $Root 'components\codex-feishu\progress-wx.py') -PathType Leaf) -or
         (Test-Path -LiteralPath (Join-Path $Root 'components\ProgressChecking(WX)\progress-wx.py') -PathType Leaf))) {
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
    if ($Kind -eq 'ecosystem-installed') {
        $Current = Join-Path $Root 'components\codex-feishu'
        if (Test-Path -LiteralPath (Join-Path $Current 'progress-wx.py') -PathType Leaf) { return $Current }
        return Join-Path $Root 'components\ProgressChecking(WX)'
    }
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

Write-UpgradeProgress 3 '校验升级包' '正在校验包内文件与运行环境签名。'
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
$LegacyProgress = Get-LegacyProgressRoot -Root $LegacyRoot -Kind $Kind
$LegacyPython = Get-LegacyPython -Root $LegacyRoot -Kind $Kind
if ($Kind -eq 'ecosystem-installed' -and (Test-Path -LiteralPath (Join-Path $LegacyProgress 'config.yaml'))) {
    if (-not (Test-Path -LiteralPath $LegacyPython -PathType Leaf)) { throw '旧安装缺少 Python，无法安全检查数据库位置；请先修复运行环境。' }
    & $LegacyPython -B (Join-Path $PSScriptRoot 'migrate-state.py') $LegacyProgress --check-only
    if ($LASTEXITCODE -ne 0) { throw '旧数据库或守护状态不在受支持的 .state 保留目录、使用路径链接或配置无法读取；尚未停止服务或覆盖程序，请先备份并安排人工迁移。' }
}
$WasRunning = $false
$OldServicesQuiesced = $false
$OldRecoverySuspended = $false
$GuardianOriginal = $null
$GuardianWasRunning = $false
$GuardianTaskBefore = $null
$GuardianSnapshotPath = Join-Path ([IO.Path]::GetTempPath()) ('codex-guardian-upgrade-'+[Guid]::NewGuid().ToString('N')+'.json')
$RecoveryEnabled = $false
if ($Kind -eq 'ecosystem-installed') {
    $GuardianStatus = Get-InstalledGuardianStatus -InstallRoot $LegacyRoot
    if ($null -ne $GuardianStatus) { $GuardianWasRunning = [bool]$GuardianStatus.worker.running }
    if ($null -ne $GuardianStatus -and -not $InstallRoot.Equals($LegacyRoot,[StringComparison]::OrdinalIgnoreCase)) {
        throw 'An installation with guardian state must be upgraded in place; relocation requires a separate migration.'
    }
    if ($null -ne $GuardianStatus -and $SkipServiceControl -and ($GuardianStatus.guardian.running -or $GuardianStatus.worker.running)) {
        throw 'Cannot skip service control while guardian or worker is running.'
    }
    $LegacyRecoveryPreference = Get-LegacyRecoveryPreference -InstallRoot $LegacyRoot
    $GuardianTaskBefore = Disable-GuardianTaskForUpgrade -InstallRoot $LegacyRoot -SnapshotPath $GuardianSnapshotPath
    $RecoveryEnabled = if ($GuardianTaskBefore.xml) {
        ([xml]$GuardianTaskBefore.xml).Task.Settings.Enabled -eq 'true'
    } elseif($GuardianTaskBefore.legacy_xml) {
        ([xml]$GuardianTaskBefore.legacy_xml).Task.Settings.Enabled -eq 'true'
    } else { $LegacyRecoveryPreference }
    try {
        $RecoveryStatus = & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Status -InstallRoot $LegacyRoot | ConvertFrom-Json
        if ($RecoveryStatus.enabled -or $RecoveryStatus.legacy_enabled) { throw 'Old recovery task is still enabled; refusing to replace files.' }
        $OldRecoverySuspended = $true
        $GuardianOriginal = Enter-UpgradeGuardianMaintenance -InstallRoot $LegacyRoot
    }
    catch {
        & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Restore -InstallRoot $LegacyRoot -SnapshotPath $GuardianSnapshotPath
        throw
    }
}
if ($Kind -eq 'ecosystem-installed' -and -not $SkipServiceControl -and
    (Test-Path -LiteralPath $LegacyPython -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $LegacyProgress 'config.yaml') -PathType Leaf)) {
    try {
    if ($null -ne $GuardianOriginal) {
        $WasRunning = $GuardianWasRunning
    } else {
        $RunningState = & $LegacyPython -B (Join-Path $PSScriptRoot 'check-runtime.py') service-state $LegacyProgress
        if ($LASTEXITCODE -ne 0 -or [string]$RunningState -notin @('0','1')) { throw 'Cannot safely determine the old service state.' }
        $WasRunning = [string]$RunningState -eq '1'
    }
    if ($WasRunning -and $null -eq $GuardianOriginal) {
        Write-UpgradeProgress 8 '停止后台服务' '正在停止后台，随后创建一致的升级备份。'
        & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') stop --timeout 30
        if ($LASTEXITCODE -ne 0) { throw '旧后台无法安全停止；未创建备份或覆盖程序。' }
    }
    $OldServicesQuiesced = $true
    } catch {
        if ($null -ne $GuardianTaskBefore) { & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Restore -InstallRoot $LegacyRoot -SnapshotPath $GuardianSnapshotPath }
        Leave-UpgradeGuardianMaintenance -InstallRoot $LegacyRoot -Original $GuardianOriginal
        throw
    }
}
try {
    $Transaction = New-EcosystemTransaction -InstallRoot $InstallRoot -Kind upgrade -LegacyRoot $LegacyRoot -CodexHomePath $CodexHomePath
    if ($null -ne $GuardianTaskBefore -and $LegacyRoot.Equals($InstallRoot,[StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $GuardianSnapshotPath -Destination $Transaction.Manifest.guardian_task_snapshot -Force
    } elseif ($null -ne $GuardianTaskBefore) {
        $SourceTaskSnapshot=Join-Path ([string]$Transaction.Manifest.backup_root) 'guardian-source-task.json'
        Copy-Item -LiteralPath $GuardianSnapshotPath -Destination $SourceTaskSnapshot -Force
        Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{guardian_source_task_snapshot=$SourceTaskSnapshot}
    }
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{guardian_original_intent=$GuardianOriginal;old_services_quiesced=$OldServicesQuiesced;old_recovery_suspended=$OldRecoverySuspended}
} catch {
    if ($null -ne $GuardianTaskBefore) {
        & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Restore -InstallRoot $LegacyRoot -SnapshotPath $GuardianSnapshotPath
    }
    Leave-UpgradeGuardianMaintenance -InstallRoot $LegacyRoot -Original $GuardianOriginal
    if ($WasRunning -and $null -eq $GuardianOriginal) {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $LegacyProgress 'scripts\start.ps1') -ToolsRoot (Join-Path $LegacyRoot 'components')
        if ($LASTEXITCODE -ne 0) { Write-Warning '备份失败，旧后台需要手动启动。' }
    }
    throw
}
$SavedInstallRoot = Join-Path ([string]$Transaction.Manifest.backup_root) 'install-root'
$PreservedProgress = if ($Kind -eq 'ecosystem-installed' -and
    $LegacyRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Get-LegacyProgressRoot -Root $SavedInstallRoot -Kind $Kind
} else { $LegacyProgress }
try {
    Write-UpgradeProgress 12 '创建安全备份' '旧版程序和用户配置已进入事务备份。'
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ legacy_kind = $Kind; legacy_service_was_running = $WasRunning }

    if ($Kind -eq 'ecosystem-installed' -and -not $SkipCodexIntegration) {
        Write-UpgradeProgress 31 '暂时解除 Codex 集成' '更新完成后会自动恢复通知与权限处理。'
        & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') uninstall-permission-hook
        if ($LASTEXITCODE -ne 0) { throw '旧 PermissionRequest Hook 无法安全移除。' }
        & $LegacyPython (Join-Path $LegacyProgress 'progress-wx.py') --config (Join-Path $LegacyProgress 'config.yaml') uninstall-notify
        if ($LASTEXITCODE -ne 0) { throw '旧 notify 无法安全恢复。' }
    }

    if ($Kind -eq 'ecosystem-installed' -and $LegacyRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Write-UpgradeProgress 42 '清理旧版程序' '只保留事务备份中的配置与私人数据，移除旧程序文件。'
        Remove-VerifiedInstallTree -InstallRoot $InstallRoot
    }
    Write-UpgradeProgress 52 '安装新版生态' '正在同时更新 Codex 管理、进度监测和百宝箱。'
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ runtime_ready = $false }
    Install-EcosystemFiles -PackageRoot $PackageRoot -InstallRoot $InstallRoot
    $NewProgress = Join-Path $InstallRoot 'components\codex-feishu'
    Copy-LegacyPrivateData -SourceRoot $PreservedProgress -DestinationRoot $NewProgress
    if (Test-Path -LiteralPath $SavedInstallRoot -PathType Container) {
        Restore-EcosystemUserData -SavedInstallRoot $SavedInstallRoot -InstallRoot $InstallRoot
    } elseif (-not $LegacyRoot.Equals($InstallRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $LegacyTreasureConfig = Join-Path $LegacyRoot 'config.json'
        if (Test-Path -LiteralPath $LegacyTreasureConfig -PathType Leaf) {
            Copy-Item -LiteralPath $LegacyTreasureConfig -Destination (Join-Path $InstallRoot 'config.json') -Force
        }
    }

    Write-UpgradeProgress 67 '恢复运行环境' '正在安装并核对离线 Python 运行环境。'
    $Python = Install-PythonRuntime -PackageRoot $PackageRoot -InstallRoot $InstallRoot -SkipRuntimeInstall:$SkipRuntimeInstall -RuntimeSeedPython $RuntimeSeedPython
    if (-not $SkipRuntimeInstall) { Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ runtime_ready = $true } }
    $Config = Initialize-EcosystemConfig -InstallRoot $InstallRoot
    if ($V1Ids.Count -gt 0) {
        $Plan = Join-Path $InstallRoot '.ecosystem\v1-monitor-migration.json'
        Write-JsonFile -Path $Plan -Value @{ schema_version = 1; thread_ids = $V1Ids }
        if ($SkipRuntimeInstall) { throw '存在 v1 thread-id 迁移时不能跳过 Python 运行时。' }
        & $Python (Join-Path $PSScriptRoot 'migrate-v1.py') $Config $Plan
        if ($LASTEXITCODE -ne 0) { throw 'v1 thread-id 迁移失败。' }
    }
    if (-not $SkipRuntimeInstall) {
        Write-UpgradeProgress 74 '迁移历史状态' '正在保留原有记录并升级数据库。'
        & $Python (Join-Path $PSScriptRoot 'migrate-state.py') $NewProgress
        if ($LASTEXITCODE -ne 0) { throw '数据库迁移失败，将恢复升级前程序和数据。' }
    }
    if (-not $SkipCodexIntegration) {
        Write-UpgradeProgress 79 '恢复 Codex 集成' '正在重新安装通知与权限处理入口。'
        Install-CodexIntegration -InstallRoot $InstallRoot -PythonExe $Python
    }
    Write-UpgradeProgress 88 '执行健康检查' '正在确认三个组件和后台命令均可正常运行。'
    Test-EcosystemHealth -InstallRoot $InstallRoot -PythonExe $Python -SkipRuntimeCheck:$SkipRuntimeInstall

    if (-not $SkipRuntimeInstall -and $null -ne (Get-InstalledGuardianStatus -InstallRoot $InstallRoot)) {
        if ($null -eq $GuardianOriginal -and -not $WasRunning) {
            Invoke-GuardianControl -Python $Python -Backend $NewProgress -Arguments @('stop','--timeout','30')
        }
        & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Install -InstallRoot $InstallRoot -Enabled:$RecoveryEnabled
        Leave-UpgradeGuardianMaintenance -InstallRoot $InstallRoot -Original $GuardianOriginal
    }

    if ($WasRunning -and $null -eq $GuardianOriginal -and -not $SkipServiceControl) {
        Write-UpgradeProgress 94 '恢复后台服务' '正在重新启动飞书机器人和进度监测。'
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
        transaction_manifest = if ($CleanupBackupOnSuccess) { $null } else { $Transaction.ManifestPath }
        service_started = $WasRunning
    }
    Write-JsonFile -Path (Join-Path $InstallRoot '.ecosystem\installation.json') -Value $Metadata
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ status = 'completed'; completed_at = (Get-Date).ToUniversalTime().ToString('o') }
    Write-UpgradeProgress 98 '清理更新残留' '正在删除旧程序备份和不再使用的临时文件。'
    if ($CleanupBackupOnSuccess) { Remove-EcosystemTransactionBackup -ManifestPath $Transaction.ManifestPath }
    Write-UpgradeProgress 100 '更新完成' '整套生态已经升级并通过健康检查。' 'completed'
    Write-Host "升级完成：$Kind -> v$script:EcosystemVersion" -ForegroundColor Green
    if (-not $CleanupBackupOnSuccess) { Write-Host "事务备份：$($Transaction.ManifestPath)" }
    if ($Kind -eq 'github-v1.2') {
        Write-Host '旧 Webhook 与 config.local.json 未复用。下一步必须创建企业自建应用并重新绑定用户。'
    }
} catch {
    $Failure = $_
    Write-UpgradeProgress 40 '更新失败，正在自动恢复' '正在把程序和 Codex 配置恢复到更新前状态。' 'rollback'
    try {
        Restore-EcosystemTransaction -ManifestPath $Transaction.ManifestPath -AutomaticFailure
        if ($WasRunning -and $null -eq $GuardianOriginal -and -not $SkipServiceControl) {
            $RestoredProgress = Get-LegacyProgressRoot -Root $InstallRoot -Kind 'ecosystem-installed'
            $RestoredStart = Join-Path $RestoredProgress 'scripts\start.ps1'
            if (Test-Path -LiteralPath $RestoredStart -PathType Leaf) {
                & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RestoredStart -ToolsRoot (Join-Path $InstallRoot 'components')
                if ($LASTEXITCODE -ne 0) { Write-Warning '旧版文件已恢复，但后台服务需要人工启动。' }
            }
        }
        Write-UpgradeProgress 40 '已恢复旧版本' $Failure.Exception.Message 'failed'
    } catch {
        Write-Warning "自动回滚失败：$($_.Exception.Message)"
        Write-UpgradeProgress 40 '自动恢复失败' $_.Exception.Message 'failed'
    }
    throw $Failure
}

if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $InstallRoot 'TreasureChest.exe') -WorkingDirectory $InstallRoot
}
$global:LASTEXITCODE = 0
