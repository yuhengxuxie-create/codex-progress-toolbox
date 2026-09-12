[CmdletBinding()]
param([string]$DotNetPath = '', [string]$Version = '1.5.0')

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$localDotNet = Join-Path $root ".dotnet\dotnet.exe"

function Test-DotNetSdk8([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    try {
        $sdks = & $Candidate --list-sdks 2>$null
        return ($LASTEXITCODE -eq 0 -and @($sdks | Where-Object { $_ -match '^8\.' }).Count -gt 0)
    }
    catch {
        return $false
    }
}

if ([string]::IsNullOrWhiteSpace($DotNetPath)) {
    if (Test-DotNetSdk8 -Candidate $localDotNet) {
        $DotNetPath = $localDotNet
    } else {
        $command = Get-Command dotnet -ErrorAction SilentlyContinue
        if ($null -ne $command -and (Test-DotNetSdk8 -Candidate $command.Source)) { $DotNetPath = $command.Source }
    }
}
if (-not (Test-DotNetSdk8 -Candidate $DotNetPath)) {
    throw '需要 .NET 8 SDK。请安装后重试，或通过 -DotNetPath 指定 dotnet.exe。'
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw '版本号必须是 x.y.z。' }
$FileVersion = $Version + '.0'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$expectedIconSourceSha256 = "2A60AD3825484022D9940C48DDD702BDC293A79292D777CD06899269830EB1F6"
function Assert-LastExitCode([string]$step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$step failed with exit code $LASTEXITCODE"
    }
}
$iconSource = Join-Path $root "resources\app.png"
$iconTarget = Join-Path $root "resources\app.ico"
$actualIconSourceSha256 = (Get-FileHash -LiteralPath $iconSource -Algorithm SHA256).Hash
if ($actualIconSourceSha256 -ne $expectedIconSourceSha256) {
    throw "Application icon source SHA-256 mismatch: $actualIconSourceSha256"
}
& $DotNetPath run --project (Join-Path $root "scripts\IconGenerator\IconGenerator.csproj") -- `
    $iconSource $iconTarget
Assert-LastExitCode "Icon generation"
& $DotNetPath build (Join-Path $root "tests\TreasureChest.SelfTest\TreasureChest.SelfTest.csproj") -c Release --nologo
Assert-LastExitCode "Self-test build"
& $DotNetPath run --project (Join-Path $root "tests\TreasureChest.SelfTest\TreasureChest.SelfTest.csproj") -c Release -r win-x64 --no-build
Assert-LastExitCode "Self-test execution"
$publish = Join-Path $root "build\publish"
& $DotNetPath publish (Join-Path $root "src\TreasureChest.App\TreasureChest.App.csproj") -c Release -r win-x64 `
    --self-contained true -p:PublishSingleFile=true -p:Version=$Version -p:AssemblyVersion=$FileVersion `
    -p:FileVersion=$FileVersion -p:InformationalVersion=$Version -o $publish --nologo
Assert-LastExitCode "Application publish"
$publishedExe = Join-Path $publish "TreasureChest.exe"
& $DotNetPath run --project (Join-Path $root "scripts\IconGenerator\IconGenerator.csproj") -- `
    --verify-exe $iconSource $iconTarget $publishedExe
Assert-LastExitCode "Published executable icon verification"
$updaterPublish = Join-Path $root "build\updater-publish"
& $DotNetPath publish (Join-Path $root "src\Ecosystem.Updater\Ecosystem.Updater.csproj") -c Release -r win-x64 `
    --self-contained true -p:PublishSingleFile=true -p:Version=$Version -p:AssemblyVersion=$FileVersion `
    -p:FileVersion=$FileVersion -p:InformationalVersion=$Version -o $updaterPublish --nologo
Assert-LastExitCode "Updater publish"
Copy-Item -LiteralPath (Join-Path $updaterPublish "Ecosystem.Updater.exe") -Destination (Join-Path $publish "Ecosystem.Updater.exe") -Force
$convenienceExe = Join-Path $root "build\TreasureChest.exe"
try {
    Copy-Item -LiteralPath $publishedExe -Destination $convenienceExe -Force
    Write-Host "Build complete: $convenienceExe"
}
catch [System.IO.IOException] {
    # 百宝箱本身可能正从 build\TreasureChest.exe 运行。发布目录中的独立产物已经完成，
    # 分享包构建器会直接使用它，因此不需要为了出包强行结束用户正在使用的程序。
    Write-Warning "当前运行中的 TreasureChest 锁定了便捷副本，未覆盖该文件。发布产物已就绪：$publishedExe"
}
Copy-Item -LiteralPath (Join-Path $publish "Ecosystem.Updater.exe") -Destination (Join-Path $root "build\Ecosystem.Updater.exe") -Force
