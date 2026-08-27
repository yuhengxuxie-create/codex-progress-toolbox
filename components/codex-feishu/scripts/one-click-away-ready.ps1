[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$NoOpenReport,
    [switch]$NoDialog
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$ReportPath = Join-Path $Context.StateDir 'last-away-ready.txt'
$Report = [System.Collections.Generic.List[string]]::new()
$Report.Add('进度通知 - 离开前安全准备')
$Report.Add(('北京时间：' + (Get-BeijingTimestamp)))
$Report.Add('')

# 只请求协作停止，绝不按 PID 强杀；未运行时也会成功返回。
$Stop = Invoke-ProgressCli -Context $Context -Command 'stop' -Arguments @('--timeout', '30')
$Report.Add(('服务停止请求退出码：' + $Stop.ExitCode))
foreach ($Line in $Stop.Lines) { $Report.Add($Line) }
if ($Stop.ExitCode -ne 0) {
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    throw '服务未能正常停止；未执行强杀，请查看日志。'
}

# 只处理本项目唯一、固定名称的计划任务；以后可通过 enable-autostart.ps1 重建。
$Task = Get-ScheduledTask -TaskName 'ProgressCheckingWX' -ErrorAction SilentlyContinue
if ($null -ne $Task) {
    Unregister-ScheduledTask -TaskName 'ProgressCheckingWX' -Confirm:$false
    $Report.Add('开机自启：已移除固定任务 ProgressCheckingWX。')
}
else {
    $Report.Add('开机自启：固定任务 ProgressCheckingWX 原本就不存在。')
}

$Status = Invoke-ProgressCli -Context $Context -Command 'status'
$Report.Add(('停止后状态退出码：' + $Status.ExitCode))
foreach ($Line in $Status.Lines) { $Report.Add($Line) }
if ($Status.ExitCode -ne 1) {
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    throw '停止后状态不是“未运行”，已拒绝报告准备完成。'
}

$Report.Add('')
$Report.Add('[结论]')
$Report.Add('离开前安全准备已完成：服务未运行、没有开机自启。')
$Report.Add('脚本没有连接微信、没有读取聊天、没有发送消息，也没有切换账号。')
$Report.Add('你可以继续把当前小号当普通微信使用；本工具当前不会介入。')
$Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8

$Summary = @(
    '离开前安全准备已完成。',
    '服务未运行，开机自启不存在；没有连接或操作微信。',
    ('报告：' + $ReportPath)
) -join "`r`n"
Show-WizardMessage -Text $Summary -Title '进度通知 - 可以安全离开' -NoDialog:$NoDialog
Open-WizardReport -Path $ReportPath -NoOpenReport:$NoOpenReport
