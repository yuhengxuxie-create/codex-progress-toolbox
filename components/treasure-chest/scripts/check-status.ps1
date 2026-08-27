$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "build\TreasureChest.exe"
$config = Join-Path $root "config.json"
Write-Host "TreasureChest EXE: $(Test-Path -LiteralPath $exe)"
Write-Host "配置文件: $(Test-Path -LiteralPath $config)"
if (Test-Path -LiteralPath $exe) {
    $process = Get-Process -Name TreasureChest -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($process) { Write-Host "运行中，PID $($process.Id)，内存 $([math]::Round($process.WorkingSet64 / 1MB, 2)) MB" }
    else { Write-Host "当前未运行" }
}
