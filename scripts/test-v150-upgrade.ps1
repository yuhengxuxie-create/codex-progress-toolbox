[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PackageRoot,
    [Parameter(Mandatory=$true)][string]$LegacyPackageRoot,
    [Parameter(Mandatory=$true)][string]$TestRoot,
    [string]$RuntimeSeedPython = '',
    [string]$LegacyPowerShellPath = 'pwsh.exe'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Run this test with Windows PowerShell 5.1.' }
$PackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$LegacyPackageRoot = (Resolve-Path -LiteralPath $LegacyPackageRoot).Path
$TestRoot = [IO.Path]::GetFullPath($TestRoot)
if (Test-Path -LiteralPath $TestRoot) { throw 'Use a new test directory.' }
New-Item -ItemType Directory -Path $TestRoot | Out-Null
$InstallRoot = Join-Path $TestRoot 'installed'
$TestCodexHome = Join-Path $TestRoot 'codex-home'
New-Item -ItemType Directory -Path $TestCodexHome | Out-Null
Set-Content (Join-Path $TestCodexHome 'config.toml') "model = 'synthetic-upgrade-test'" -Encoding UTF8
Set-Content (Join-Path $TestCodexHome 'hooks.json') '{"hooks":{"Stop":[]}}' -Encoding UTF8
function Digest([string]$Root) {
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $lines = @(Get-ChildItem -LiteralPath $base -File -Recurse -Force | Sort-Object FullName | ForEach-Object {
        $_.FullName.Substring($base.Length) + ' ' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    })
    return ($lines -join "`n")
}
& $LegacyPowerShellPath -NoProfile -ExecutionPolicy Bypass -File (Join-Path $LegacyPackageRoot 'installer/install.ps1') -InstallRoot $InstallRoot -NonInteractive -NoLaunch -NoShortcuts -SkipCodexIntegration -CodexHomePath $TestCodexHome -RuntimeSeedPython $RuntimeSeedPython
if ($LASTEXITCODE -ne 0) { throw 'Actual v1.5.0 install failed.' }
$Python = Join-Path $InstallRoot 'components/Python313-ProgressWX/python.exe'
$Backend = Join-Path $InstallRoot 'components/codex-feishu'
$Fixture = Join-Path $PSScriptRoot 'test-upgrade-state.py'
& $Python $Fixture seed $InstallRoot
if ($LASTEXITCODE -ne 0) { throw 'Old schema fixture failed.' }
$Config = Join-Path $Backend 'config.yaml'
Add-Content -LiteralPath $Config -Value '# synthetic custom configuration retained' -Encoding UTF8
$PrivateFiles = @{
    'config.json' = '{"sessions":[],"synthetic":"keep"}'
    '.state/reset-alert-consumer.json' = '{"Initialized":true,"EventIdentities":["synthetic-event"],"History":[{"Read":true,"ShellSubmitted":true,"Message":"synthetic alert"}]}'
    'plugins/synthetic-user/content.txt' = 'synthetic custom plugin'
    'components/codex-feishu/.secrets/hmac.key' = 'synthetic hmac key bytes'
}
foreach ($relative in $PrivateFiles.Keys) {
    $target = Join-Path $InstallRoot $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Set-Content -LiteralPath $target -Value $PrivateFiles[$relative] -Encoding UTF8
}
Add-Type -AssemblyName System.Security
$SecretPath = Join-Path $Backend '.secrets/feishu-app-secret.dpapi'
$SecretPlain = [Text.Encoding]::UTF8.GetBytes('synthetic-only-secret')
[IO.File]::WriteAllBytes($SecretPath, [Security.Cryptography.ProtectedData]::Protect($SecretPlain,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))
$Conflict = Join-Path $InstallRoot 'plugins/progress-notification/custom-marker.txt'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Conflict) | Out-Null
Set-Content -LiteralPath $Conflict -Value 'synthetic modified bundled plugin' -Encoding UTF8
$PreserveHashes = @{}
foreach ($path in @($Config,$SecretPath) + @($PrivateFiles.Keys | ForEach-Object {Join-Path $InstallRoot $_})) { $PreserveHashes[$path] = (Get-FileHash -LiteralPath $path).Hash }
$Before = Digest $InstallRoot
[IO.File]::WriteAllText((Join-Path $TestRoot 'before-tree.txt'),$Before)
$CodexBefore = Digest $TestCodexHome
$ProgressFile = Join-Path $TestRoot 'progress.json'
& (Join-Path $PackageRoot 'installer/quick-upgrade.ps1') -LegacyRoot $InstallRoot -NonInteractive -NoLaunch -NoShortcuts -SkipCodexIntegration -CodexHomePath $TestCodexHome -RuntimeSeedPython $RuntimeSeedPython -ProgressFile $ProgressFile
if ($LASTEXITCODE -ne 0) { throw 'Actual v1.5.0 upgrade failed.' }
foreach ($path in $PreserveHashes.Keys) { if ((Get-FileHash -LiteralPath $path).Hash -ne $PreserveHashes[$path]) { throw 'Private data changed.' } }
$decrypted = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($SecretPath),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
if ([Text.Encoding]::UTF8.GetString($decrypted) -ne 'synthetic-only-secret') { throw 'Synthetic DPAPI round trip failed.' }
$Conflicts = @(Get-ChildItem -LiteralPath (Join-Path $InstallRoot '.state/plugin-backups') -File -Recurse -Filter custom-marker.txt)
if ($Conflicts.Count -ne 1 -or (Get-Content $Conflicts[0].FullName -Raw).Trim() -ne 'synthetic modified bundled plugin') { throw 'Same-name plugin not preserved.' }
$Progress = Get-Content -LiteralPath $ProgressFile -Raw | ConvertFrom-Json
if ($Progress.percent -ne 100 -or $Progress.state -ne 'completed') { throw 'Updater progress did not complete.' }
& $Python $Fixture check $InstallRoot
if ($LASTEXITCODE -ne 0) { throw 'Migrated database check failed.' }
$Metadata = Get-Content (Join-Path $InstallRoot '.ecosystem/installation.json') -Raw | ConvertFrom-Json
if ($Metadata.service_started -or $Metadata.ecosystem_version -ne (Get-Content (Join-Path $PackageRoot 'PACKAGE_VERSION.txt') -Raw).Trim()) { throw 'Unexpected service or version state.' }
& (Join-Path $PackageRoot 'installer/rollback.ps1') -TransactionManifest $Metadata.transaction_manifest
if ((Digest $InstallRoot) -ne $Before -or (Digest $TestCodexHome) -ne $CodexBefore) { throw 'Rollback is not byte-identical.' }
Write-Host 'PASS actual v1.5.0 install -> upgrade with ProgressFile -> private data/schema/DPAPI/plugin checks -> byte-identical rollback'
Write-Host 'Not exercised: real Feishu, GUI, Codex hook mutation, running production service; all data synthetic. Test directory retained.'
$global:LASTEXITCODE = 0
