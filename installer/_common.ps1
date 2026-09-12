Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'guardian-lifecycle.ps1')
$VersionFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'PACKAGE_VERSION.txt'
$script:EcosystemVersion = if (Test-Path -LiteralPath $VersionFile -PathType Leaf) {
    (Get-Content -LiteralPath $VersionFile -Raw).Trim()
} else { '1.5.0' }
if ($script:EcosystemVersion -notmatch '^\d+\.\d+\.\d+$') { throw '生态版本号无效。' }
$script:SentinelName = '.codex-feishu-ecosystem-root'

function Get-DefaultEcosystemRoot {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA 不可用，必须显式指定 -InstallRoot。'
    }
    return Join-Path $env:LOCALAPPDATA 'CodexFeishuEcosystem'
}

function Resolve-SafeLocalRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))
    if ([string]::IsNullOrWhiteSpace($Expanded)) { throw '目录不能为空。' }
    $Full = [IO.Path]::GetFullPath($Expanded)
    if ([Uri]::new($Full).IsUnc) { throw '目录必须位于本机磁盘，不能使用网络共享。' }
    $DriveRoot = [IO.Path]::GetPathRoot($Full)
    if ($Full.TrimEnd('\').Equals($DriveRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw '目录不能是磁盘根目录。'
    }
    foreach ($Forbidden in @($env:WINDIR, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not [string]::IsNullOrWhiteSpace($Forbidden) -and
            $Full.TrimEnd('\').Equals(([IO.Path]::GetFullPath($Forbidden)).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw '目录不能直接选择 Windows 或 Program Files 根目录。'
        }
    }
    return $Full
}

function Test-PathWithinRoot {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $Full = [IO.Path]::GetFullPath($Path)
    $RootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    return $Full.Equals($RootFull, [StringComparison]::OrdinalIgnoreCase) -or
        $Full.StartsWith($RootFull + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePoints {
    param([Parameter(Mandatory = $true)][string]$Root)
    $RootItem = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if (($RootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "拒绝重解析点目录：$Root"
    }
    foreach ($Item in Get-ChildItem -LiteralPath $Root -Recurse -Force) {
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝重解析点：$($Item.FullName)"
        }
    }
}

function Copy-TreeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string[]]$ExcludedSegments = @(),
        [string[]]$ExcludedLeaves = @()
    )
    $SourceFull = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Source).Path).TrimEnd('\')
    Assert-NoReparsePoints -Root $SourceFull
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($File in Get-ChildItem -LiteralPath $SourceFull -Recurse -File -Force) {
        $Relative = $File.FullName.Substring($SourceFull.Length).TrimStart('\')
        $Segments = @($Relative -split '[\\/]')
        if ($Segments -contains '..') { throw "复制路径越界：$Relative" }
        if (@($Segments | Where-Object { $_ -in $ExcludedSegments }).Count -gt 0) { continue }
        if ($File.Name -in $ExcludedLeaves) { continue }
        $Target = [IO.Path]::GetFullPath((Join-Path $Destination $Relative))
        if (-not (Test-PathWithinRoot -Path $Target -Root $Destination)) { throw "复制目标越界：$Relative" }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Target -Force
    }
}

function Write-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Value)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-CodexHomePath {
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($env:CODEX_HOME))
    }
    return Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
}

function New-EcosystemTransaction {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][ValidateSet('install', 'upgrade', 'uninstall')][string]$Kind,
        [string]$LegacyRoot = '',
        [string]$CodexHomePath = ''
    )
    $InstallFull = Resolve-SafeLocalRoot -Path $InstallRoot
    $Parent = Split-Path -Parent $InstallFull
    $Leaf = [IO.Path]::GetFileName($InstallFull)
    $Id = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N')
    $BackupRoot = Join-Path (Join-Path $Parent '.codex-feishu-backups') (Join-Path $Leaf $Id)
    New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

    $SourceExisted = Test-Path -LiteralPath $InstallFull -PathType Container
    if ($SourceExisted) {
        Copy-TreeChecked -Source $InstallFull -Destination (Join-Path $BackupRoot 'install-root')
    }
    if (-not [string]::IsNullOrWhiteSpace($LegacyRoot)) {
        $LegacyFull = Resolve-SafeLocalRoot -Path $LegacyRoot
        if (-not $LegacyFull.Equals($InstallFull, [StringComparison]::OrdinalIgnoreCase)) {
            Copy-TreeChecked -Source $LegacyFull -Destination (Join-Path $BackupRoot 'legacy-source')
        }
    }

    $CodexHome = if ([string]::IsNullOrWhiteSpace($CodexHomePath)) {
        Get-CodexHomePath
    } else {
        [IO.Path]::GetFullPath($CodexHomePath)
    }
    $CodexBackup = Join-Path $BackupRoot 'codex'
    New-Item -ItemType Directory -Force -Path $CodexBackup | Out-Null
    $CodexConfig = Join-Path $CodexHome 'config.toml'
    $CodexHooks = Join-Path $CodexHome 'hooks.json'
    $ConfigExisted = Test-Path -LiteralPath $CodexConfig -PathType Leaf
    $HooksExisted = Test-Path -LiteralPath $CodexHooks -PathType Leaf
    if ($ConfigExisted) { Copy-Item -LiteralPath $CodexConfig -Destination (Join-Path $CodexBackup 'config.toml') -Force }
    if ($HooksExisted) { Copy-Item -LiteralPath $CodexHooks -Destination (Join-Path $CodexBackup 'hooks.json') -Force }

    $ManifestPath = Join-Path $BackupRoot 'manifest.json'
    $Manifest = [ordered]@{
        schema_version = 1
        transaction_id = $Id
        kind = $Kind
        ecosystem_version = $script:EcosystemVersion
        created_at = (Get-Date).ToUniversalTime().ToString('o')
        install_root = $InstallFull
        source_existed = [bool]$SourceExisted
        backup_root = $BackupRoot
        legacy_root = $LegacyRoot
        codex_home = $CodexHome
        codex_config_existed = [bool]$ConfigExisted
        codex_hooks_existed = [bool]$HooksExisted
        legacy_service_was_running = $false
        status = 'prepared'
    }
    $TaskSnapshot = Join-Path $BackupRoot 'guardian-task.json'
    Export-GuardianTaskState -InstallRoot $InstallFull -SnapshotPath $TaskSnapshot
    $Manifest.guardian_task_snapshot = $TaskSnapshot
    Write-JsonFile -Path $ManifestPath -Value $Manifest
    return [pscustomobject]@{ Manifest = $Manifest; ManifestPath = $ManifestPath }
}

function Update-TransactionManifest {
    param([Parameter(Mandatory = $true)][string]$ManifestPath, [Parameter(Mandatory = $true)][hashtable]$Changes)
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    foreach ($Key in $Changes.Keys) { $Manifest | Add-Member -NotePropertyName $Key -NotePropertyValue $Changes[$Key] -Force }
    Write-JsonFile -Path $ManifestPath -Value $Manifest
}

function Remove-VerifiedInstallTree {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $Full = Resolve-SafeLocalRoot -Path $InstallRoot
    $Sentinel = Join-Path $Full $script:SentinelName
    if (-not (Test-Path -LiteralPath $Sentinel -PathType Leaf)) {
        throw "目标缺少生态根标记，拒绝递归删除：$Full"
    }
    Remove-Item -LiteralPath $Full -Recurse -Force
}

function Restore-EcosystemTransaction {
    param([Parameter(Mandatory = $true)][string]$ManifestPath, [switch]$AutomaticFailure)
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($Manifest.schema_version -ne 1) { throw '不支持的事务清单版本。' }
    $InstallRoot = Resolve-SafeLocalRoot -Path ([string]$Manifest.install_root)
    $BackupRoot = Resolve-SafeLocalRoot -Path ([string]$Manifest.backup_root)
    if (-not (Test-PathWithinRoot -Path $ManifestPath -Root $BackupRoot)) { throw '事务清单不在声明的备份根内。' }

    # Installed rollback replaces its own installer directory. Keep the exact
    # validated controller outside that tree for task restoration afterwards.
    $TaskController = Join-Path $BackupRoot 'guardian-task-controller.ps1'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'guardian-task.ps1') -Destination $TaskController -Force
    if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
        $TaskState = & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Status -InstallRoot $InstallRoot | ConvertFrom-Json
        # A failed first installation can have copied the CLI before Python
        # exists. Only this uninitialized transaction may restore without a
        # CLI query; any persisted state or recovery task remains fail-closed.
        $Uninitialized = Test-UninitializedTransactionRuntime -InstallRoot $InstallRoot -Manifest $Manifest -TaskInstalled ([bool]$TaskState.installed)
        $QuiescedUpgrade = $false
        if ($AutomaticFailure) { $QuiescedUpgrade = Test-QuiescedUpgradeRollback -InstallRoot $InstallRoot -BackupRoot $BackupRoot -Manifest $Manifest -TaskState $TaskState }
        if (-not $Uninitialized -and -not $QuiescedUpgrade) { $null = Enter-UpgradeGuardianMaintenance -InstallRoot $InstallRoot }
        if ($TaskState.installed) { & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Remove -InstallRoot $InstallRoot }
        Remove-VerifiedInstallTree -InstallRoot $InstallRoot
    }
    if ([bool]$Manifest.source_existed) {
        $Saved = Join-Path $BackupRoot 'install-root'
        if (-not (Test-Path -LiteralPath $Saved -PathType Container)) { throw '事务备份缺少原安装目录。' }
        Copy-TreeChecked -Source $Saved -Destination $InstallRoot
    }

    $CodexHome = [IO.Path]::GetFullPath([string]$Manifest.codex_home)
    New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
    foreach ($Entry in @(
        @{ Name = 'config.toml'; Existed = [bool]$Manifest.codex_config_existed },
        @{ Name = 'hooks.json'; Existed = [bool]$Manifest.codex_hooks_existed }
    )) {
        $Target = Join-Path $CodexHome $Entry.Name
        $Saved = Join-Path (Join-Path $BackupRoot 'codex') $Entry.Name
        if ($Entry.Existed) {
            if (-not (Test-Path -LiteralPath $Saved -PathType Leaf)) { throw "Codex 快照缺少 $($Entry.Name)。" }
            Copy-Item -LiteralPath $Saved -Destination $Target -Force
        } elseif (Test-Path -LiteralPath $Target -PathType Leaf) {
            Remove-Item -LiteralPath $Target -Force
        }
    }
    if ($Manifest.PSObject.Properties.Name -contains 'guardian_task_snapshot') {
        $TaskSnapshot = [IO.Path]::GetFullPath([string]$Manifest.guardian_task_snapshot)
        if (-not (Test-PathWithinRoot -Path $TaskSnapshot -Root $BackupRoot)) { throw 'Guardian snapshot is outside the transaction.' }
        & $TaskController -Mode Restore -InstallRoot $InstallRoot -SnapshotPath $TaskSnapshot
    }
    if ($Manifest.PSObject.Properties.Name -contains 'guardian_original_intent') {
        Leave-UpgradeGuardianMaintenance -InstallRoot $InstallRoot -Original $Manifest.guardian_original_intent
    }
    if ($Manifest.PSObject.Properties.Name -contains 'guardian_source_task_snapshot') {
        $SourceTaskSnapshot=[IO.Path]::GetFullPath([string]$Manifest.guardian_source_task_snapshot)
        if(-not (Test-PathWithinRoot -Path $SourceTaskSnapshot -Root $BackupRoot)){throw 'Source task snapshot is outside transaction.'}
        $SourceRoot=Resolve-SafeLocalRoot -Path ([string]$Manifest.legacy_root)
        & $TaskController -Mode Restore -InstallRoot $SourceRoot -SnapshotPath $SourceTaskSnapshot
    }
    Update-TransactionManifest -ManifestPath $ManifestPath -Changes @{ status = 'rolled_back'; rolled_back_at = (Get-Date).ToUniversalTime().ToString('o') }
}

function Test-UninitializedTransactionRuntime {
    param([string]$InstallRoot, $Manifest, [bool]$TaskInstalled)
    if ($Manifest.status -ne 'prepared' -or $TaskInstalled) { return $false }
    if ($Manifest.PSObject.Properties.Name -notcontains 'runtime_ready' -or $Manifest.runtime_ready -ne $false) { return $false }
    $FreshInstall = $Manifest.kind -eq 'install'
    $LegacyV12 = $Manifest.PSObject.Properties.Name -contains 'legacy_kind' -and $Manifest.legacy_kind -eq 'github-v1.2'
    if (-not $FreshInstall -and -not $LegacyV12) { return $false }
    if ($Manifest.PSObject.Properties.Name -contains 'guardian_original_intent' -and $null -ne $Manifest.guardian_original_intent) { return $false }
    if (Test-Path -LiteralPath (Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe')) { return $false }
    if (Test-Path -LiteralPath (Join-Path $InstallRoot '.ecosystem\installation.json')) { return $false }
    foreach ($Relative in @('components', 'components\Python313-ProgressWX', 'components\codex-feishu', 'components\codex-feishu\.state')) {
        $Path = Join-Path $InstallRoot $Relative
        if (Test-Path -LiteralPath $Path) {
            $Item = Get-Item -LiteralPath $Path -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $Item.PSIsContainer) { return $false }
        }
    }
    $State = Join-Path $InstallRoot 'components\codex-feishu\.state'
    if ((Test-Path -LiteralPath $State) -and @(Get-ChildItem -LiteralPath $State -Force).Count -gt 0) { return $false }
    $Config = Join-Path $InstallRoot 'components\codex-feishu\config.yaml'
    $Template = Join-Path $InstallRoot 'components\codex-feishu\config.example.yaml'
    if (Test-Path -LiteralPath $Config) {
        if (-not (Test-Path -LiteralPath $Template) -or (Get-FileHash -LiteralPath $Config).Hash -ne (Get-FileHash -LiteralPath $Template).Hash) { return $false }
    }
    return (Test-TransactionProcessesAbsent -InstallRoot $InstallRoot)
}

function Test-TransactionProcessesAbsent {
    param([string]$InstallRoot)
    $Prefix = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
    foreach ($Process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        if ($Process.ExecutablePath -and ([string]$Process.ExecutablePath).StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        if ($Process.Name -match '^python(?:w|[0-9.]*)?\.exe$' -and $Process.CommandLine -and ([string]$Process.CommandLine).IndexOf($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $false }
    }
    return $true
}

function Test-QuiescedUpgradeRollback {
    param([string]$InstallRoot, [string]$BackupRoot, $Manifest, $TaskState)
    if ($Manifest.status -ne 'prepared' -or $Manifest.kind -ne 'upgrade') { return $false }
    if ($Manifest.PSObject.Properties.Name -notcontains 'legacy_kind' -or $Manifest.legacy_kind -ne 'ecosystem-installed') { return $false }
    if ($Manifest.PSObject.Properties.Name -notcontains 'runtime_ready' -or $Manifest.runtime_ready -ne $false) { return $false }
    if ($Manifest.PSObject.Properties.Name -notcontains 'old_services_quiesced' -or $Manifest.old_services_quiesced -ne $true) { return $false }
    if ($Manifest.PSObject.Properties.Name -notcontains 'old_recovery_suspended' -or $Manifest.old_recovery_suspended -ne $true) { return $false }
    if ($TaskState.enabled -or $TaskState.legacy_enabled) { return $false }
    if (-not (Test-TransactionProcessesAbsent -InstallRoot $InstallRoot)) { return $false }
    $Saved = Join-Path $BackupRoot 'install-root'
    if (-not (Test-Path -LiteralPath $Saved -PathType Container)) { return $false }
    Assert-NoReparsePoints -Root $InstallRoot
    Assert-NoReparsePoints -Root $Saved
    # These are the exact preserved paths validated before the old service was
    # stopped. Any new state/configuration activity prevents the exception.
    foreach ($Relative in @('components\codex-feishu\config.yaml', 'components\codex-feishu\.state')) {
        $Current = Join-Path $InstallRoot $Relative
        $Original = Join-Path $Saved $Relative
        if ((Test-Path -LiteralPath $Current) -ne (Test-Path -LiteralPath $Original)) { return $false }
        if (-not (Test-Path -LiteralPath $Original)) { continue }
        foreach ($Root in @($Current, $Original)) {
            $Item = Get-Item -LiteralPath $Root -Force
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
            if ($Item.PSIsContainer) { Assert-NoReparsePoints -Root $Root }
        }
        $CurrentFiles = @(Get-ChildItem -LiteralPath $Current -Recurse -File -Force | Sort-Object FullName | ForEach-Object { $_.FullName.Substring($Current.Length) + ' ' + (Get-FileHash -LiteralPath $_.FullName).Hash })
        $OriginalFiles = @(Get-ChildItem -LiteralPath $Original -Recurse -File -Force | Sort-Object FullName | ForEach-Object { $_.FullName.Substring($Original.Length) + ' ' + (Get-FileHash -LiteralPath $_.FullName).Hash })
        if (($CurrentFiles -join "`n") -ne ($OriginalFiles -join "`n")) { return $false }
    }
    return $true
}

function Remove-EcosystemTransactionBackup {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { return }
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($Manifest.schema_version -ne 1) { throw '不支持的事务清单版本。' }
    $BackupRoot = Resolve-SafeLocalRoot -Path ([string]$Manifest.backup_root)
    if (-not (Test-PathWithinRoot -Path $ManifestPath -Root $BackupRoot)) { throw '事务清单不在声明的备份根内。' }
    if (-not [IO.Path]::GetFileName($BackupRoot).Equals([string]$Manifest.transaction_id, [StringComparison]::Ordinal)) {
        throw '事务备份目录与事务 ID 不一致，拒绝清理。'
    }
    $BackupFamily = Split-Path -Parent (Split-Path -Parent $BackupRoot)
    if (-not [IO.Path]::GetFileName($BackupFamily).Equals('.codex-feishu-backups', [StringComparison]::OrdinalIgnoreCase)) {
        throw '事务备份不在受控目录中，拒绝清理。'
    }
    Remove-Item -LiteralPath $BackupRoot -Recurse -Force
}

function Restore-EcosystemUserData {
    param([Parameter(Mandatory = $true)][string]$SavedInstallRoot, [Parameter(Mandatory = $true)][string]$InstallRoot)
    $Config = Join-Path $SavedInstallRoot 'config.json'
    if (Test-Path -LiteralPath $Config -PathType Leaf) {
        Copy-Item -LiteralPath $Config -Destination (Join-Path $InstallRoot 'config.json') -Force
    }
    $OldState = Join-Path $SavedInstallRoot '.state'
    if (Test-Path -LiteralPath $OldState -PathType Container) {
        Copy-TreeChecked -Source $OldState -Destination (Join-Path $InstallRoot '.state')
    }
    $OldLogs = Join-Path $SavedInstallRoot 'logs'
    if (Test-Path -LiteralPath $OldLogs -PathType Container) {
        Copy-TreeChecked -Source $OldLogs -Destination (Join-Path $InstallRoot 'logs')
    }
    $OldPlugins = Join-Path $SavedInstallRoot 'plugins'
    $NewPlugins = Join-Path $InstallRoot 'plugins'
    if (Test-Path -LiteralPath $OldPlugins -PathType Container) {
        New-Item -ItemType Directory -Force -Path $NewPlugins | Out-Null
        foreach ($Item in Get-ChildItem -LiteralPath $OldPlugins -Force) {
            $Target = Join-Path $NewPlugins $Item.Name
            if (Test-Path -LiteralPath $Target) {
                $ConflictRoot = Join-Path $InstallRoot ('.state\plugin-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N'))
                New-Item -ItemType Directory -Force -Path $ConflictRoot | Out-Null
                $Preserved = Join-Path $ConflictRoot $Item.Name
                if ($Item.PSIsContainer) { Copy-TreeChecked -Source $Item.FullName -Destination $Preserved }
                else { Copy-Item -LiteralPath $Item.FullName -Destination $Preserved -Force }
                Write-Host '同名旧插件已保存在安装目录 .state\plugin-backups，可手动比较恢复。'
                continue
            }
            if ($Item.PSIsContainer) { Copy-TreeChecked -Source $Item.FullName -Destination $Target }
            else { Copy-Item -LiteralPath $Item.FullName -Destination $Target -Force }
        }
    }
}

function Set-PrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$Directory)
    $Identity = "${env:USERDOMAIN}\${env:USERNAME}"
    if ($Directory) {
        & icacls.exe $Path /inheritance:r /grant:r "${Identity}:(OI)(CI)F" | Out-Null
    } else {
        & icacls.exe $Path /inheritance:r /grant:r "${Identity}:(F)" | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "无法收紧当前用户权限：$Path" }
}

function Get-RegisteredPython31314 {
    $Candidates = New-Object System.Collections.Generic.List[string]
    foreach ($RegistryPath in @(
        'HKCU:\Software\Python\PythonCore\3.13\InstallPath',
        'HKLM:\Software\Python\PythonCore\3.13\InstallPath',
        'HKLM:\Software\WOW6432Node\Python\PythonCore\3.13\InstallPath'
    )) {
        $Value = Get-ItemProperty -LiteralPath $RegistryPath -ErrorAction SilentlyContinue
        if ($null -ne $Value) {
            if (-not [string]::IsNullOrWhiteSpace($Value.ExecutablePath)) { $Candidates.Add([string]$Value.ExecutablePath) }
            $DefaultValue = $Value.'(default)'
            if (-not [string]::IsNullOrWhiteSpace($DefaultValue)) { $Candidates.Add((Join-Path ([string]$DefaultValue) 'python.exe')) }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $Candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'))
    }
    foreach ($Candidate in $Candidates | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
        $Version = (& $Candidate -c 'import platform; print(platform.python_version())' 2>$null).Trim()
        if ($LASTEXITCODE -eq 0 -and $Version -eq '3.13.14') { return [IO.Path]::GetFullPath($Candidate) }
    }
    return $null
}

function Copy-CleanPythonRuntime {
    param([Parameter(Mandatory = $true)][string]$SourcePython, [Parameter(Mandatory = $true)][string]$DestinationRoot)
    $SourceRoot = Split-Path -Parent $SourcePython
    Assert-NoReparsePoints -Root $SourceRoot
    New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
    foreach ($File in Get-ChildItem -LiteralPath $SourceRoot -Recurse -File -Force) {
        $Relative = $File.FullName.Substring($SourceRoot.TrimEnd('\').Length).TrimStart('\')
        $Segments = @($Relative -split '[\\/]')
        if ($Segments -contains 'site-packages' -or $Segments -contains 'Scripts' -or $Segments -contains '__pycache__' -or
            $File.Extension -in @('.pyc', '.pyo')) { continue }
        $Target = Join-Path $DestinationRoot $Relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Target -Force
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $DestinationRoot 'Lib\site-packages') | Out-Null
}

function Install-EcosystemFiles {
    param([Parameter(Mandatory = $true)][string]$PackageRoot, [Parameter(Mandatory = $true)][string]$InstallRoot)
    $ComponentSource = Join-Path $PackageRoot 'components\codex-feishu'
    $TreasurePayload = Join-Path $PackageRoot 'payload\treasure-chest'
    foreach ($Required in @($ComponentSource, $TreasurePayload, (Join-Path $PackageRoot 'plugins'), (Join-Path $PackageRoot 'docs'))) {
        if (-not (Test-Path -LiteralPath $Required -PathType Container)) { throw "安装包缺少：$Required" }
    }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Set-Content -LiteralPath (Join-Path $InstallRoot $script:SentinelName) -Value "version=$script:EcosystemVersion" -Encoding ASCII
    Copy-TreeChecked -Source $ComponentSource -Destination (Join-Path $InstallRoot 'components\codex-feishu')
    Copy-TreeChecked -Source $TreasurePayload -Destination $InstallRoot
    Copy-TreeChecked -Source (Join-Path $PackageRoot 'plugins') -Destination (Join-Path $InstallRoot 'plugins')
    Copy-TreeChecked -Source (Join-Path $PackageRoot 'docs') -Destination (Join-Path $InstallRoot 'docs')
    Copy-Item -LiteralPath (Join-Path $PackageRoot 'LICENSE') -Destination (Join-Path $InstallRoot 'LICENSE') -Force
    Copy-Item -LiteralPath (Join-Path $PackageRoot 'PACKAGE_VERSION.txt') -Destination (Join-Path $InstallRoot 'PACKAGE_VERSION.txt') -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'installer') | Out-Null
    foreach ($Name in @('_common.ps1', 'rollback.ps1', 'uninstall.ps1', 'guardian-lifecycle.ps1', 'guardian-task.ps1', 'guardian-supervisor.py', 'check-runtime.py')) {
        Copy-Item -LiteralPath (Join-Path $PackageRoot ('installer\' + $Name)) -Destination (Join-Path $InstallRoot ('installer\' + $Name)) -Force
    }
}

function Install-PythonRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [switch]$SkipRuntimeInstall,
        [string]$RuntimeSeedPython = ''
    )
    $PythonRoot = Join-Path $InstallRoot 'components\Python313-ProgressWX'
    $PythonExe = Join-Path $PythonRoot 'python.exe'
    if ($SkipRuntimeInstall) { return $PythonExe }
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        $Installer = Join-Path $PackageRoot 'payload\offline\python-3.13.14-amd64.exe'
        $Signature = Get-AuthenticodeSignature -LiteralPath $Installer
        if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
            throw 'Python 官方安装器签名无效。'
        }
        New-Item -ItemType Directory -Force -Path $PythonRoot | Out-Null
        $Arguments = @('/quiet', 'InstallAllUsers=0', "TargetDir=$PythonRoot", 'Include_pip=1', 'Include_launcher=0', 'Include_test=0', 'Shortcuts=0', 'PrependPath=0')
        $Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
        if ($Process.ExitCode -ne 0) { throw "Python 3.13.14 安装失败，退出码 $($Process.ExitCode)。" }
        if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
            $ExistingPython = Get-RegisteredPython31314
            if ($null -eq $ExistingPython -and -not [string]::IsNullOrWhiteSpace($RuntimeSeedPython) -and
                (Test-Path -LiteralPath $RuntimeSeedPython -PathType Leaf)) {
                $SeedVersion = (& $RuntimeSeedPython -c 'import platform; print(platform.python_version())').Trim()
                if ($LASTEXITCODE -eq 0 -and $SeedVersion -eq '3.13.14') {
                    $ExistingPython = [IO.Path]::GetFullPath($RuntimeSeedPython)
                }
            }
            if ($null -eq $ExistingPython) { throw 'Python 安装器未生成目标运行时，也未找到可验证的同版本注册安装。' }
            Copy-CleanPythonRuntime -SourcePython $ExistingPython -DestinationRoot $PythonRoot
        }
    }
    $Version = (& $PythonExe -c 'import platform; print(platform.python_version())').Trim()
    if ($LASTEXITCODE -ne 0 -or $Version -ne '3.13.14') { throw "项目 Python 版本异常：$Version" }
    $HasPip = (& $PythonExe (Join-Path $PSScriptRoot 'check-runtime.py') pip).Trim()
    if ($LASTEXITCODE -ne 0) { throw '无法检查项目 Python 的 pip 状态。' }
    if ($HasPip -ne '1') {
        & $PythonExe -m ensurepip --upgrade --default-pip | Out-Host
        if ($LASTEXITCODE -ne 0) { throw '无法从标准库初始化 pip。' }
    }
    $WheelRoot = Join-Path $PackageRoot 'payload\offline\wheels'
    $ProgressRoot = Join-Path $InstallRoot 'components\codex-feishu'
    & $PythonExe -m pip install --disable-pip-version-check --no-input --no-index --require-hashes --find-links $WheelRoot -r (Join-Path $ProgressRoot 'requirements-feishu.txt') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw '离线安装哈希锁定依赖失败。' }
    return $PythonExe
}

function Initialize-EcosystemConfig {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)
    $ProgressRoot = Join-Path $InstallRoot 'components\codex-feishu'
    $Config = Join-Path $ProgressRoot 'config.yaml'
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        Copy-Item -LiteralPath (Join-Path $ProgressRoot 'config.example.yaml') -Destination $Config
    }
    foreach ($Private in @('.secrets', '.state', 'logs')) {
        $Path = Join-Path $ProgressRoot $Private
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        Set-PrivateAcl -Path $Path -Directory
    }
    Set-PrivateAcl -Path $Config
    return $Config
}

function Install-CodexIntegration {
    param([Parameter(Mandatory = $true)][string]$InstallRoot, [Parameter(Mandatory = $true)][string]$PythonExe)
    $ProgressRoot = Join-Path $InstallRoot 'components\codex-feishu'
    $Config = Join-Path $ProgressRoot 'config.yaml'
    & $PythonExe (Join-Path $ProgressRoot 'progress-wx.py') --config $Config install-notify
    if ($LASTEXITCODE -ne 0) { throw '安装 Codex notify 失败。' }
    & $PythonExe (Join-Path $ProgressRoot 'progress-wx.py') --config $Config install-permission-hook
    if ($LASTEXITCODE -ne 0) { throw '安装 PermissionRequest Hook 失败。' }
}

function Test-EcosystemHealth {
    param([Parameter(Mandatory = $true)][string]$InstallRoot, [Parameter(Mandatory = $true)][string]$PythonExe, [switch]$SkipRuntimeCheck)
    foreach ($Required in @(
        (Join-Path $InstallRoot 'TreasureChest.exe'),
        (Join-Path $InstallRoot 'components\codex-feishu\progress-wx.py'),
        (Join-Path $InstallRoot 'components\codex-feishu\config.yaml')
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) { throw "健康检查缺少：$Required" }
    }
    if ($SkipRuntimeCheck) { return }
    $ProgressRoot = Join-Path $InstallRoot 'components\codex-feishu'
    & $PythonExe (Join-Path $PSScriptRoot 'check-runtime.py') health $ProgressRoot $script:EcosystemVersion
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖或产品版本健康检查失败。' }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProgressRoot 'scripts\status.ps1') -ToolsRoot (Join-Path $InstallRoot 'components') | Out-Null
    if ($LASTEXITCODE -notin @(0, 1)) { throw '后台 status 健康检查失败。' }
}
