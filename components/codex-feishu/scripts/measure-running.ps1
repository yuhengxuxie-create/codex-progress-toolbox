[CmdletBinding()]
param([double]$Seconds = 10)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PidPath = Join-Path $ProjectRoot '.state\progress-wx.pid'
if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) {
    throw '服务 PID 文件不存在，请先启动服务。'
}
$State = Get-Content -Raw -LiteralPath $PidPath | ConvertFrom-Json
$Before = Get-Process -Id $State.pid -ErrorAction Stop
$CpuBefore = $Before.TotalProcessorTime.TotalSeconds
$PeakBytes = [Math]::Max($Before.WorkingSet64, $Before.PeakWorkingSet64)
Start-Sleep -Milliseconds ([int]([Math]::Max(1, $Seconds) * 1000))
$After = Get-Process -Id $State.pid -ErrorAction Stop
$PeakBytes = [Math]::Max($PeakBytes, [Math]::Max($After.WorkingSet64, $After.PeakWorkingSet64))
$CpuPercent = (($After.TotalProcessorTime.TotalSeconds - $CpuBefore) / [Math]::Max(1, $Seconds) / [Environment]::ProcessorCount) * 100
$MemoryMb = $PeakBytes / 1MB
[pscustomobject]@{
    Pid = $State.pid
    DurationSeconds = $Seconds
    CpuTaskManagerPercent = [Math]::Round($CpuPercent, 3)
    PeakWorkingSetMb = [Math]::Round($MemoryMb, 3)
    CpuLimitPercent = 1
    MemoryLimitMb = 100
    Pass = ($CpuPercent -lt 1 -and $MemoryMb -lt 100)
} | Format-List
if ($CpuPercent -ge 1 -or $MemoryMb -ge 100) { exit 1 }
