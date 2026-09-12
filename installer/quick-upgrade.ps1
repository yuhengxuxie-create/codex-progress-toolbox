[CmdletBinding()]
param(
    [string]$LegacyRoot = '',
    [switch]$NonInteractive,
    [switch]$NoLaunch,
    [switch]$NoShortcuts,
    [switch]$SkipCodexIntegration,
    [string]$CodexHomePath = '',
    [string]$RuntimeSeedPython = '',
    [string]$ProgressFile = ''
)
$ErrorActionPreference = 'Stop'
$PackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& (Join-Path $PSScriptRoot 'verify-package.ps1') -PackageRoot $PackageRoot
if ([string]::IsNullOrWhiteSpace($LegacyRoot)) {
    if ($NonInteractive) { throw 'LegacyRoot is required in non-interactive mode.' }
    Add-Type -AssemblyName System.Windows.Forms
    $picker = New-Object System.Windows.Forms.FolderBrowserDialog
    try {
        $picker.Description = '请选择已有生态安装目录（内含 TreasureChest.exe），不要选择升级包目录。取消不会修改安装。'
        $picker.ShowNewFolderButton = $false
        if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            Write-Host '已取消，未修改旧安装。'
            return
        }
        $LegacyRoot = $picker.SelectedPath
    } finally { $picker.Dispose() }
}
$LegacyRoot = (Resolve-Path -LiteralPath $LegacyRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $LegacyRoot '.codex-feishu-ecosystem-root')) -or
    -not (Test-Path -LiteralPath (Join-Path $LegacyRoot 'TreasureChest.exe'))) {
    throw '所选目录不是已安装的完整生态。旧 Webhook 模板请按 UPGRADE.md 迁移。'
}
$AppPath = Join-Path $LegacyRoot 'TreasureChest.exe'
$Running = @(Get-Process TreasureChest -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $AppPath })
if ($Running.Count) { throw '请先在旧百宝箱托盘菜单选择“退出”，然后重新双击快捷升级。不会强制结束程序。' }
$Arguments = @{
    LegacyRoot=$LegacyRoot; InstallRoot=$LegacyRoot; NonInteractive=$true
    NoLaunch=$NoLaunch; NoShortcuts=$NoShortcuts; SkipCodexIntegration=$SkipCodexIntegration
    CodexHomePath=$CodexHomePath; RuntimeSeedPython=$RuntimeSeedPython; ProgressFile=$ProgressFile
}
& (Join-Path $PSScriptRoot 'upgrade.ps1') @Arguments
if ($LASTEXITCODE -ne 0) { throw '升级未成功，请查看上方原因。' }
Write-Host ('下一步飞书配置说明：' + (Join-Path $PackageRoot 'docs/FEISHU_UPGRADE_PERMISSIONS.md'))
$global:LASTEXITCODE = 0
