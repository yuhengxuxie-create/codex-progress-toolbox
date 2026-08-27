[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$TransactionManifest)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot '_common.ps1')

$Before = Get-Content -LiteralPath $TransactionManifest -Raw | ConvertFrom-Json
Restore-EcosystemTransaction -ManifestPath $TransactionManifest

if ([bool]$Before.legacy_service_was_running -and [bool]$Before.source_existed) {
    $InstallRoot = [string]$Before.install_root
    $ProgressRoot = Join-Path $InstallRoot 'components\ProgressChecking(WX)'
    $Python = Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe'
    $Config = Join-Path $ProgressRoot 'config.yaml'
    if ((Test-Path -LiteralPath $Python -PathType Leaf) -and (Test-Path -LiteralPath $Config -PathType Leaf)) {
        & $Python (Join-Path $ProgressRoot 'progress-wx.py') --config $Config start
        if ($LASTEXITCODE -ne 0) { Write-Warning '文件和 Codex 配置已回滚，但旧服务需要人工启动。' }
    }
}
Write-Host '事务已回滚；安装目录和 Codex notify/hooks 已恢复。' -ForegroundColor Green
$global:LASTEXITCODE = 0
