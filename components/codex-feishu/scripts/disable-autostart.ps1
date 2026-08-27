$ErrorActionPreference = 'Stop'
$Task = Get-ScheduledTask -TaskName 'ProgressCheckingWX' -ErrorAction SilentlyContinue
if ($null -ne $Task) {
    Unregister-ScheduledTask -TaskName 'ProgressCheckingWX' -Confirm:$false
    Write-Host '已移除计划任务 ProgressCheckingWX。'
} else {
    Write-Host '计划任务不存在。'
}

