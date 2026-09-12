[CmdletBinding()]
param([string]$ToolsRoot = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'guardian-autostart-common.ps1')
$Location = Get-GuardianTaskLocation
& $Location.Manager -Mode Disable -InstallRoot $Location.Root -Layout $Location.Layout
if (-not $?) { throw '通信监督任务停用失败。' }
Write-Host '已停用当前安装、当前用户的通信监督任务。'