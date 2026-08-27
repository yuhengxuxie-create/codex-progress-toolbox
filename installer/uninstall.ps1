[CmdletBinding()]
param([string]$InstallRoot = '', [string]$CodexHomePath = '')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot '_common.ps1')
if ([string]::IsNullOrWhiteSpace($InstallRoot)) { $InstallRoot = Get-DefaultEcosystemRoot }
$InstallRoot = Resolve-SafeLocalRoot -Path $InstallRoot
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $script:SentinelName) -PathType Leaf)) {
    throw '目标不是可验证的 Codex Feishu Ecosystem 安装目录。'
}
$Transaction = New-EcosystemTransaction -InstallRoot $InstallRoot -Kind uninstall -CodexHomePath $CodexHomePath
try {
    $ProgressRoot = Join-Path $InstallRoot 'components\codex-feishu'
    $Python = Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe'
    $Config = Join-Path $ProgressRoot 'config.yaml'
    if ((Test-Path -LiteralPath $Python -PathType Leaf) -and (Test-Path -LiteralPath $Config -PathType Leaf)) {
        & $Python (Join-Path $ProgressRoot 'progress-wx.py') --config $Config stop --timeout 30
        & $Python (Join-Path $ProgressRoot 'progress-wx.py') --config $Config uninstall-permission-hook
        if ($LASTEXITCODE -ne 0) { throw 'PermissionRequest Hook 无法安全移除。' }
        & $Python (Join-Path $ProgressRoot 'progress-wx.py') --config $Config uninstall-notify
        if ($LASTEXITCODE -ne 0) { throw 'notify 无法安全恢复。' }
    }
    if (Test-Path -LiteralPath (Join-Path $ProgressRoot 'scripts\disable-autostart.ps1') -PathType Leaf) {
        & (Join-Path $ProgressRoot 'scripts\disable-autostart.ps1')
    }
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'TreasureChest' -ErrorAction SilentlyContinue
    Update-TransactionManifest -ManifestPath $Transaction.ManifestPath -Changes @{ status = 'completed'; completed_at = (Get-Date).ToUniversalTime().ToString('o') }
    Write-Host '后台、Codex notify/hooks 和自启已安全停用。' -ForegroundColor Green
    Write-Host "为保护配置、DPAPI、状态库与日志，程序目录未自动删除：$InstallRoot"
    Write-Host "卸载前备份：$($Transaction.ManifestPath)"
} catch {
    $Failure = $_
    try { Restore-EcosystemTransaction -ManifestPath $Transaction.ManifestPath } catch { Write-Warning "自动回滚失败：$($_.Exception.Message)" }
    throw $Failure
}
$global:LASTEXITCODE = 0
