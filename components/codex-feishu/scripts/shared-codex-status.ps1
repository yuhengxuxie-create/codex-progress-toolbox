[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$Result = Invoke-ProgressCli -Context $Context -Command 'gateway-status'
foreach ($Line in $Result.Lines) { Write-Host $Line }
exit $Result.ExitCode
