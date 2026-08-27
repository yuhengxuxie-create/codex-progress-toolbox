[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$PerformanceSeconds = 30,
    [switch]$NoDialog
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$ConfigHash = Get-WizardConfigHash -Context $Context
$TestMarkerPath = Join-Path $Context.StateDir 'wechat-test-sent.json'
$ReportPath = Join-Path $Context.StateDir 'last-production-enable.txt'
$Started = $false
$AutostartCreated = $false

function Invoke-Rollback {
    # 回滚只触及本项目固定计划任务并请求协作停止，不强杀任何进程。
    $DisablePath = Join-Path $PSScriptRoot 'disable-autostart.ps1'
    [void](Invoke-WizardPowerShellScript -Path $DisablePath)
    [void](Invoke-ProgressCli -Context $Context -Command 'stop' -Arguments @('--timeout', '30'))
}

try {
    $InitialStatus = Invoke-ProgressCli -Context $Context -Command 'status'
    if ($InitialStatus.ExitCode -ne 1) {
        throw '正式启用前服务必须处于“未运行”状态。'
    }

    $TestMarker = Read-WizardJson -Path $TestMarkerPath
    if (-not (Test-WizardMarker -Marker $TestMarker -ConfigHash $ConfigHash -MaxAgeSeconds 7200)) {
        throw '测试发送记录不存在、已超过两小时或配置已改变；请重新运行安全向导和单条白名单测试。'
    }

    # 正式启用前再次完整只读复核，不能只信任之前的阶段文件。
    foreach ($Command in @('validate', 'doctor', 'verify-wechat')) {
        $Result = Invoke-ProgressCli -Context $Context -Command $Command
        if ($Result.ExitCode -ne 0) {
            throw ('正式启用安全门未通过：' + $Command + "`r`n" + $Result.Text)
        }
    }

    $Received = Confirm-WizardAction -NoDialog:$NoDialog -Text @'
请先确认：你的大号已经收到刚才那 1 条“唯一白名单发送链路测试成功”消息，并且发送者确实是工具小号。

选择“是”后，向导才会处理启用前历史队列、启动服务、创建当前用户开机自启，并做 30 秒资源测量。

是否确认测试消息已正确收到？
'@
    if (-not $Received) {
        throw '你没有确认大号已正确收到测试消息；服务未启动。'
    }

    $QueueStatus = Invoke-ProgressCli -Context $Context -Command 'status'
    if ($QueueStatus.ExitCode -ne 1) {
        throw '建立启用前基线时服务状态发生变化，请重新开始。'
    }
    try {
        $QueueJson = $QueueStatus.Text | ConvertFrom-Json
        $PendingHooks = [int]$QueueJson.state.pending_hook_events
    }
    catch {
        throw '无法读取启用前待处理 hook 数量，已拒绝启动。'
    }

    if ($PendingHooks -gt 0) {
        $BaselineConfirmed = Confirm-WizardAction -NoDialog:$NoDialog -Text (@(
            ('检测到 ' + $PendingHooks + ' 条“服务尚未启用期间”积累的 Codex hook。'),
            '为避免第一次启动时连续推送旧消息，选择“是”会把这批精确数量原子标记为启用前基线，不发送到微信。',
            '确认期间如果又出现新事件，命令会自动拒绝，不会静默丢消息。',
            '',
            '是否忽略这批启用前历史 hook 并继续？'
        ) -join "`r`n")
        if (-not $BaselineConfirmed) {
            throw '你没有确认忽略启用前历史 hook；服务未启动。'
        }
    }

    # hook 为零时仍需登记所选对话当前已结束的最新轮次，避免首次启动补发旧进度。
    $Baseline = Invoke-ProgressCli -Context $Context `
        -Command 'baseline-pre-activation-hooks' `
        -Arguments @('--expected-count', [string]$PendingHooks)
    if ($Baseline.ExitCode -ne 0) {
        throw ('启用前历史基线未建立；服务未启动。' + "`r`n" + $Baseline.Text)
    }

    $Start = Invoke-ProgressCli -Context $Context -Command 'start'
    if ($Start.ExitCode -ne 0) {
        throw ('后台服务启动失败。' + "`r`n" + $Start.Text)
    }
    $Started = $true

    $EnableAutostartPath = Join-Path $PSScriptRoot 'enable-autostart.ps1'
    $Autostart = Invoke-WizardPowerShellScript -Path $EnableAutostartPath `
        -Arguments @('-ToolsRoot', $Context.ToolsRoot)
    if ($Autostart.ExitCode -ne 0) {
        throw ('创建开机自启失败，将回滚已启动服务。' + "`r`n" + $Autostart.Text)
    }
    $AutostartCreated = $true

    $MeasurePath = Join-Path $PSScriptRoot 'measure-running.ps1'
    $Measure = Invoke-WizardPowerShellScript -Path $MeasurePath `
        -Arguments @('-Seconds', [string]([Math]::Max(5, $PerformanceSeconds)))
    if ($Measure.ExitCode -ne 0) {
        throw ('真实服务资源测量未通过，将移除自启并停止服务。' + "`r`n" + $Measure.Text)
    }

    $FinalStatus = Invoke-ProgressCli -Context $Context -Command 'status'
    if ($FinalStatus.ExitCode -ne 0) {
        throw '最终状态不是“正在运行”，将回滚。'
    }

    $StartShortcut = New-ProjectDesktopShortcut -Context $Context `
        -Name '进度通知 - 启动.lnk' `
        -TargetName '启动进度通知.cmd' `
        -Description '仅在生产验收成功后手动启动进度通知'
    @(
        '进度通知 - 正式启用',
        ('北京时间：' + (Get-BeijingTimestamp)),
        ('启用前忽略的历史 hook：' + $PendingHooks),
        '服务：正在运行。',
        '开机自启：已创建。',
        ('性能测量：通过，时长 ' + [Math]::Max(5, $PerformanceSeconds) + ' 秒。'),
        ('手动启动入口：' + $StartShortcut),
        '',
        '说明：启动后可能为每个受监控对话发送一条当前结构化快照；之后只处理新变化。'
    ) | Set-Content -LiteralPath $ReportPath -Encoding UTF8

    Show-WizardMessage -Text (@(
        '正式启用完成：服务正在运行，开机自启已创建，资源测量通过。',
        '其他联系人、群聊和普通消息仍不会触发工具。',
        ('报告：' + $ReportPath)
    ) -join "`r`n") -Title '进度通知 - 正式启用成功' -NoDialog:$NoDialog
}
catch {
    if ($Started -or $AutostartCreated) {
        Invoke-Rollback
    }
    @(
        '进度通知 - 正式启用未完成',
        ('北京时间：' + (Get-BeijingTimestamp)),
        ('原因：' + $_.Exception.Message),
        '安全状态：若曾启动，已请求停止并移除固定开机自启；未执行强杀。'
    ) | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Show-WizardMessage -Text ($_.Exception.Message + "`r`n`r`n服务未启用或已回滚。") `
        -Title '进度通知 - 未启用' -NoDialog:$NoDialog -Icon Error
    throw
}
