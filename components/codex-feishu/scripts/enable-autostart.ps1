[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '独立 Python 3.13 运行时不存在，请先安装。'
}
$ConsolePython = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $ConsolePython -PathType Leaf)) {
    throw '独立 Python 3.13 控制台运行时不存在，无法做自启预检。'
}
$EntryPoint = Join-Path $ProjectRoot 'progress-wx.py'
$ConfigPath = Join-Path $ProjectRoot 'config.yaml'
& $ConsolePython $EntryPoint --config $ConfigPath validate
if ($LASTEXITCODE -ne 0) {
    throw '配置或消息后端尚未就绪；未创建开机自启任务。'
}
$Arguments = '"' + $EntryPoint + '" --config "' + $ConfigPath + '" start'
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# 服务内部已有最多 5 次的递增重试；耗尽后必须停机求助，不能由计划任务无条件复活。
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'ProgressCheckingWX' -Action $Action -Trigger $Trigger -Settings $Settings -Description '当前用户登录后启动 Codex 飞书进度通知' -Force | Out-Null
Write-Host '已创建当前用户登录触发的计划任务 ProgressCheckingWX。'
