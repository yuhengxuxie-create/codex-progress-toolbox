Set-StrictMode -Version Latest
$script:EcosystemVersion = '1.5.0'
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
    param([Parameter(Mandatory = $true)][string]$ManifestPath)
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($Manifest.schema_version -ne 1) { throw '不支持的事务清单版本。' }
    $InstallRoot = Resolve-SafeLocalRoot -Path ([string]$Manifest.install_root)
    $BackupRoot = Resolve-SafeLocalRoot -Path ([string]$Manifest.backup_root)
    if (-not (Test-PathWithinRoot -Path $ManifestPath -Root $BackupRoot)) { throw '事务清单不在声明的备份根内。' }

    if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
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
    Update-TransactionManifest -ManifestPath $ManifestPath -Changes @{ status = 'rolled_back'; rolled_back_at = (Get-Date).ToUniversalTime().ToString('o') }
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
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'installer') | Out-Null
    foreach ($Name in @('_common.ps1', 'rollback.ps1', 'uninstall.ps1')) {
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
    $HasPip = (& $PythonExe -c 'import importlib.util; print(int(importlib.util.find_spec("pip") is not None))').Trim()
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
    & $PythonExe -c "import sys; sys.path.insert(0, r'$($ProgressRoot.Replace("'", "''"))\src'); import progress_wx, yaml, lark_channel; assert progress_wx.__version__ == '1.5.0'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖或产品版本健康检查失败。' }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProgressRoot 'scripts\status.ps1') -ToolsRoot (Join-Path $InstallRoot 'components') | Out-Null
    if ($LASTEXITCODE -notin @(0, 1)) { throw '后台 status 健康检查失败。' }
}
