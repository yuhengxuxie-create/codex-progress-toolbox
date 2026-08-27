[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [switch]$NonInteractive,
    [switch]$NoLaunch,
    [switch]$NoShortcuts,
    [switch]$SkipCodexIntegration,
    [switch]$SkipRuntimeInstall,
    [string]$CodexHomePath = '',
    [string]$RuntimeSeedPython = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot '_common.ps1')

& (Join-Path $PSScriptRoot 'verify-package.ps1') -PackageRoot $PackageRoot
if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $InstallRoot = Get-DefaultEcosystemRoot
    if (-not $NonInteractive) {
        $Typed = Read-Host "安装目录（直接回车使用 $InstallRoot）"
        if (-not [string]::IsNullOrWhiteSpace($Typed)) { $InstallRoot = $Typed }
    }
}
$InstallRoot = Resolve-SafeLocalRoot -Path $InstallRoot
if (Test-PathWithinRoot -Path $InstallRoot -Root $PackageRoot) { throw '安装目录不能位于解压包内部。' }
if (Test-Path -LiteralPath $InstallRoot) {
    $Existing = @(Get-ChildItem -LiteralPath $InstallRoot -Force -ErrorAction Stop)
    if ($Existing.Count -gt 0) { throw '安装目录非空；旧用户请使用 upgrade.ps1。' }
}

$Transaction = New-EcosystemTransaction -InstallRoot $InstallRoot -Kind install -CodexHomePath $CodexHomePath
try {
    Install-EcosystemFiles -PackageRoot $PackageRoot -InstallRoot $InstallRoot
    $Python = Install-PythonRuntime -PackageRoot $PackageRoot -InstallRoot $InstallRoot -SkipRuntimeInstall:$SkipRuntimeInstall -RuntimeSeedPython $RuntimeSeedPython
    $Config = Initialize-EcosystemConfig -InstallRoot $InstallRoot
    if (-not $SkipCodexIntegration) { Install-CodexIntegration -InstallRoot $InstallRoot -PythonExe $Python }
    Test-EcosystemHealth -InstallRoot $InstallRoot -PythonExe $Python -SkipRuntimeCheck:$SkipRuntimeInstall

    $Metadata = [ordered]@{
        schema_version = 1
        ecosystem_version = $script:EcosystemVersion
        installed_at = (Get-Date).ToUniversalTime().ToString('o')
        install_root = $InstallRoot
        python_version = if ($SkipRuntimeInstall) { $null } else { '3.13.14' }
        codex_integration_installed = (-not $SkipCodexIntegration)
        transaction_manifest = $Transaction.ManifestPath
        service_started = $false
    }
    Write-JsonFile -Path (Join-Path $InstallRoot '.ecosystem\installation.json') -Value $Metadata
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ status = 'completed'; completed_at = (Get-Date).ToUniversalTime().ToString('o') }

    if (-not $NoShortcuts) {
        $Shell = New-Object -ComObject WScript.Shell
        $Desktop = [Environment]::GetFolderPath('Desktop')
        if (-not [string]::IsNullOrWhiteSpace($Desktop)) {
            $Shortcut = $Shell.CreateShortcut((Join-Path $Desktop 'TreasureChest.lnk'))
            $Shortcut.TargetPath = Join-Path $InstallRoot 'TreasureChest.exe'
            $Shortcut.WorkingDirectory = $InstallRoot
            $Shortcut.Save()
        }
    }
    Write-Host "基础安装完成：$InstallRoot" -ForegroundColor Green
    Write-Host '正式飞书服务尚未启动；下一步请按照 AGENTS.md 创建并绑定自己的企业自建应用。'
    Write-Host "事务备份：$($Transaction.ManifestPath)"
} catch {
    $Failure = $_
    try { Restore-EcosystemTransaction -ManifestPath $Transaction.ManifestPath } catch { Write-Warning "自动回滚失败：$($_.Exception.Message)" }
    throw $Failure
}

if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $InstallRoot 'TreasureChest.exe') -WorkingDirectory $InstallRoot
}
$global:LASTEXITCODE = 0
