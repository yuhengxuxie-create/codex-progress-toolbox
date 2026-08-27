[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$PackageRoot, [string]$RuntimeSeedPython = '')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PackageRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('CodexFeishu-FullE2E-' + [Guid]::NewGuid().ToString('N'))
$InstallRoot = Join-Path $TestRoot 'install'
$TestCodexHome = Join-Path $TestRoot 'codex-home'
Write-Host "Full E2E temp root: $TestRoot"

function Remove-TestRootSafely {
    $TempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $Full = [IO.Path]::GetFullPath($TestRoot)
    if (-not $Full.StartsWith($TempPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($Full).StartsWith('CodexFeishu-FullE2E-', [StringComparison]::Ordinal)) {
        throw "拒绝清理无法确认的测试目录：$Full"
    }
    if (Test-Path -LiteralPath $Full) {
        for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
            try { Remove-Item -LiteralPath $Full -Recurse -Force -ErrorAction Stop; break }
            catch {
                if (-not (Test-Path -LiteralPath $Full)) { break }
                if ($Attempt -eq 10) { Write-Warning "临时目录仍被占用，保留供构建后复检：$Full"; break }
                Start-Sleep -Milliseconds (200 * $Attempt)
            }
        }
    }
}

try {
    New-Item -ItemType Directory -Force -Path $TestCodexHome | Out-Null
    Set-Content -LiteralPath (Join-Path $TestCodexHome 'config.toml') -Value "model = 'test-model'" -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $TestCodexHome 'hooks.json') -Value '{"hooks":{"Stop":[]}}' -Encoding UTF8
    $ConfigHash = (Get-FileHash -LiteralPath (Join-Path $TestCodexHome 'config.toml') -Algorithm SHA256).Hash
    $HooksHash = (Get-FileHash -LiteralPath (Join-Path $TestCodexHome 'hooks.json') -Algorithm SHA256).Hash

    & (Join-Path $PackageRoot 'installer\install.ps1') -InstallRoot $InstallRoot -NonInteractive -NoLaunch -NoShortcuts -SkipCodexIntegration -CodexHomePath $TestCodexHome -RuntimeSeedPython $RuntimeSeedPython
    if ($LASTEXITCODE -ne 0) { throw '临时全新安装失败。' }
    foreach ($Required in @(
        (Join-Path $InstallRoot 'TreasureChest.exe'),
        (Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe'),
        (Join-Path $InstallRoot 'components\codex-feishu\config.yaml'),
        (Join-Path $InstallRoot '.ecosystem\installation.json')
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) { throw "临时安装缺少：$Required" }
    }
    $Python = Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe'
    $Version = (& $Python -c 'import platform; print(platform.python_version())').Trim()
    if ($Version -ne '3.13.14') { throw "临时 Python 版本异常：$Version" }

    $ProgressRoot = Join-Path $InstallRoot 'components\codex-feishu'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProgressRoot 'scripts\status.ps1') -ToolsRoot (Join-Path $InstallRoot 'components') *> $null
    if ($LASTEXITCODE -notin @(0, 1)) { throw '临时 status 失败。' }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProgressRoot 'scripts\start.ps1') -ToolsRoot (Join-Path $InstallRoot 'components') *> $null
    if ($LASTEXITCODE -eq 0) {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProgressRoot 'scripts\stop.ps1') -ToolsRoot (Join-Path $InstallRoot 'components') *> $null
        throw '未配置飞书凭据时服务意外启动，fail-closed 验收失败。'
    }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProgressRoot 'scripts\stop.ps1') -ToolsRoot (Join-Path $InstallRoot 'components') *> $null
    if ($LASTEXITCODE -notin @(0, 1)) { throw '临时 stop 失败。' }

    $Metadata = Get-Content -LiteralPath (Join-Path $InstallRoot '.ecosystem\installation.json') -Raw | ConvertFrom-Json
    & (Join-Path $PackageRoot 'installer\rollback.ps1') -TransactionManifest ([string]$Metadata.transaction_manifest)
    if (Test-Path -LiteralPath $InstallRoot) { throw '全新安装回滚后目标目录仍存在。' }
    if ((Get-FileHash -LiteralPath (Join-Path $TestCodexHome 'config.toml') -Algorithm SHA256).Hash -ne $ConfigHash) { throw '测试 Codex config.toml 未精确恢复。' }
    if ((Get-FileHash -LiteralPath (Join-Path $TestCodexHome 'hooks.json') -Algorithm SHA256).Hash -ne $HooksHash) { throw '测试 Codex hooks.json 未精确恢复。' }
    Write-Host '全新安装 E2E 通过：install/status/start-fail-closed/stop/rollback。' -ForegroundColor Green
} finally {
    Remove-TestRootSafely
}
$global:LASTEXITCODE = 0
