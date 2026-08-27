[CmdletBinding()]
param([string]$DotNetPath = '')

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$localDotNet = Join-Path $root ".dotnet\dotnet.exe"
if ([string]::IsNullOrWhiteSpace($DotNetPath)) {
    if (Test-Path -LiteralPath $localDotNet -PathType Leaf) {
        $DotNetPath = $localDotNet
    } else {
        $command = Get-Command dotnet -ErrorAction SilentlyContinue
        if ($null -ne $command) { $DotNetPath = $command.Source }
    }
}
if ([string]::IsNullOrWhiteSpace($DotNetPath) -or -not (Test-Path -LiteralPath $DotNetPath -PathType Leaf)) {
    throw '需要 .NET 8 SDK。请安装后重试，或通过 -DotNetPath 指定 dotnet.exe。'
}
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
function Assert-LastExitCode([string]$step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$step failed with exit code $LASTEXITCODE"
    }
}
& $DotNetPath run --project (Join-Path $root "scripts\IconGenerator\IconGenerator.csproj") -- `
    (Join-Path $root "resources\app.png") (Join-Path $root "resources\app.ico")
Assert-LastExitCode "Icon generation"
& $DotNetPath build (Join-Path $root "tests\TreasureChest.SelfTest\TreasureChest.SelfTest.csproj") -c Release --nologo
Assert-LastExitCode "Self-test build"
& $DotNetPath run --project (Join-Path $root "tests\TreasureChest.SelfTest\TreasureChest.SelfTest.csproj") -c Release -r win-x64 --no-build
Assert-LastExitCode "Self-test execution"
$publish = Join-Path $root "build\publish"
& $DotNetPath publish (Join-Path $root "src\TreasureChest.App\TreasureChest.App.csproj") -c Release -r win-x64 `
    --self-contained true -p:PublishSingleFile=true -o $publish --nologo
Assert-LastExitCode "Application publish"
$publishedExe = Join-Path $publish "TreasureChest.exe"
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
