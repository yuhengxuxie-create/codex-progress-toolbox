[CmdletBinding()]
param([string]$ToolsRoot = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'guardian-autostart-common.ps1')
$Location = Get-GuardianTaskLocation
& $Location.Manager -Mode Install -InstallRoot $Location.Root -Layout $Location.Layout -Enabled
if (-not $?) { throw '通信监督任务启用失败。' }
Write-Host '已启用当前安装、当前用户的通信监督；保留业务停止和完整退出意图。'