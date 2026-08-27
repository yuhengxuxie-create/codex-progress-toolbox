[CmdletBinding()]
param([string]$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root).Path)
. (Join-Path (Join-Path $Root 'installer') '_common.ps1')
Assert-NoReparsePoints -Root $Root

$Required = @(
    'AGENTS.md', 'README.md', 'CHANGELOG.md', 'UPGRADE.md', 'SECURITY.md',
    'components\codex-feishu\src\progress_wx\codex_management.py',
    'components\codex-feishu\src\progress_wx\service.py',
    'components\codex-feishu\src\progress_wx\approval_bridge.py',
    'components\codex-feishu\src\progress_wx\codex_account.py',
    'components\codex-feishu\docs\assets\feishu-usage-classroom\06-manage-monitoring.png',
    'components\treasure-chest\src\TreasureChest.App\Integrations\ProjectMonitorCliService.cs',
    'components\treasure-chest\src\TreasureChest.App\UI\ProjectMonitorSettingsDialog.cs',
    'installer\install.ps1', 'installer\upgrade.ps1', 'installer\rollback.ps1',
    'scripts\privacy-scan.ps1'
)
foreach ($Relative in $Required) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $Relative))) { throw "结构缺少：$Relative" }
}
$FeishuTests = @(Get-ChildItem -LiteralPath (Join-Path $Root 'components\codex-feishu\tests') -Filter 'test_*.py' -File)
if ($FeishuTests.Count -lt 20) { throw "后台测试数量异常：$($FeishuTests.Count)" }
$Classroom = @(Get-ChildItem -LiteralPath (Join-Path $Root 'components\codex-feishu\docs\assets\feishu-usage-classroom') -Filter '*.png' -File)
if ($Classroom.Count -ne 6) { throw "飞书课堂图片数量应为 6，实际 $($Classroom.Count)。" }

if (Test-Path -LiteralPath (Join-Path $Root 'PACKAGE_TYPE.txt') -PathType Leaf) {
    & (Join-Path $Root 'installer\verify-package.ps1') -PackageRoot $Root
}
Write-Host "包结构验证通过：后台测试 $($FeishuTests.Count) 个，课堂图片 6 张。" -ForegroundColor Green

