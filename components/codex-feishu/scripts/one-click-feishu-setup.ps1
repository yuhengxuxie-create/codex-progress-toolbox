[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$PerformanceSeconds = 15,
    [switch]$NoDialog,
    [switch]$IntegratedMode,
    [switch]$EnableServiceAutoStart
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$ReportPath = Join-Path $Context.StateDir 'last-feishu-setup.txt'
$Started = $false
$AutostartCreated = $false

function Invoke-FeishuSetupRollback {
    # 只撤销本轮实际创建的固定任务并协作停机；不强杀进程、不影响飞书客户端。
    if ($AutostartCreated) {
        [void](Invoke-WizardPowerShellScript -Path (Join-Path $PSScriptRoot 'disable-autostart.ps1'))
    }
    if ($Started) {
        [void](Invoke-ProgressCli -Context $Context -Command 'stop' -Arguments @('--timeout', '30'))
    }
}

try {
    $InitialStatus = Invoke-ProgressCli -Context $Context -Command 'status'
    if ($InitialStatus.ExitCode -ne 1) {
        throw '首次设置前进度通知必须处于未运行状态；请先点击“停止”。'
    }

    Write-Host '步骤 1/8：选择要监控的一个或多个 Codex 对话。'
    & $Context.PythonExe $Context.EntryPoint --config $Context.ConfigPath configure-monitor
    if ($LASTEXITCODE -ne 0) { throw 'Codex 监控对象配置失败。' }

    Write-Host '步骤 2/8：安全保存飞书 App ID 与 App Secret。'
    & (Join-Path $PSScriptRoot 'configure-feishu.ps1') -ToolsRoot $Context.ToolsRoot
    if ($LASTEXITCODE -ne 0) { throw '飞书凭证配置失败。' }

    Write-Host '步骤 3/8：请按屏幕提示，用手机向机器人发送一次性绑定码。'
    & (Join-Path $PSScriptRoot 'pair-feishu.ps1') -ToolsRoot $Context.ToolsRoot
    if ($LASTEXITCODE -ne 0) { throw '手机飞书绑定失败。' }

    Write-Host '步骤 4/8：向刚刚绑定的唯一用户发送一条真实测试消息。'
    & (Join-Path $PSScriptRoot 'test-feishu.ps1') -ToolsRoot $Context.ToolsRoot
    if ($LASTEXITCODE -ne 0) { throw '飞书测试消息发送失败。' }

    $Received = Confirm-WizardAction -NoDialog:$NoDialog -Text @'
请查看手机飞书，确认已经收到“进度通知飞书链路测试成功”。

选择“是”后，向导才会忽略你明确确认的启用前历史通知、启动后台并测量真实资源。

开机自启是可选设置：集成到 TreasureChest 时请在其“设置”和会话编辑界面随时开启或关闭。

是否确认测试消息已正确收到？
'@
    if (-not $Received) {
        throw '你尚未确认收到测试消息；凭证和绑定已保留，但后台不会启动。'
    }

    Write-Host '步骤 5/8：复核配置与依赖。'
    foreach ($Command in @('validate', 'doctor')) {
        $Check = Invoke-ProgressCli -Context $Context -Command $Command
        if ($Check.ExitCode -ne 0) {
            throw ('正式启用检查失败：' + $Command + "`r`n" + $Check.Text)
        }
    }

    $QueueStatus = Invoke-ProgressCli -Context $Context -Command 'status'
    if ($QueueStatus.ExitCode -ne 1) {
        throw '读取启用前队列时服务状态发生变化，已拒绝继续。'
    }
    $QueueJson = $QueueStatus.Text | ConvertFrom-Json
    $PendingHooks = 0
    $StateProperty = $QueueJson.PSObject.Properties['state']
    if ($null -ne $StateProperty -and $null -ne $StateProperty.Value) {
        $PendingProperty = $StateProperty.Value.PSObject.Properties['pending_hook_events']
        if ($null -ne $PendingProperty) {
            $PendingHooks = [int]$PendingProperty.Value
        }
    }
    if ($PendingHooks -gt 0) {
        $IgnoreHistory = Confirm-WizardAction -NoDialog:$NoDialog -Text (
            '检测到 ' + $PendingHooks + ' 条服务启用前积累的 Codex 通知。' + "`r`n`r`n" +
            '选择“是”会按这个精确数量建立基线，不向飞书补发旧消息；确认期间若数量变化，命令会自动拒绝。是否继续？'
        )
        if (-not $IgnoreHistory) {
            throw '你没有确认忽略启用前历史通知；后台不会启动。'
        }
    }

    # 即使没有待处理 hook，也要给所选对话当前已结束的最新轮次建立 SQLite 基线。
    $Baseline = Invoke-ProgressCli -Context $Context `
        -Command 'baseline-pre-activation-hooks' `
        -Arguments @('--expected-count', [string]$PendingHooks)
    if ($Baseline.ExitCode -ne 0) {
        throw ('启用前历史基线建立失败。' + "`r`n" + $Baseline.Text)
    }

    Write-Host '步骤 6/8：启动本地后台服务。'
    $Start = Invoke-ProgressCli -Context $Context -Command 'start'
    if ($Start.ExitCode -ne 0) {
        throw ('后台服务启动失败。' + "`r`n" + $Start.Text)
    }
    $Started = $true

    Write-Host '步骤 7/8：应用开机自启选择。'
    if ($EnableServiceAutoStart) {
        $Autostart = Invoke-WizardPowerShellScript `
            -Path (Join-Path $PSScriptRoot 'enable-autostart.ps1') `
            -Arguments @('-ToolsRoot', $Context.ToolsRoot)
        if ($Autostart.ExitCode -ne 0) {
            throw ('创建开机自启失败。' + "`r`n" + $Autostart.Text)
        }
        $AutostartCreated = $true
    }
    else {
        Write-Host '未创建独立服务自启；可稍后在 TreasureChest 中随时开启或关闭。'
    }

    Write-Host '步骤 8/8：测量真实后台资源并整理入口。'
    $MeasureSeconds = [Math]::Max(8, $PerformanceSeconds)
    $Measure = Invoke-WizardPowerShellScript `
        -Path (Join-Path $PSScriptRoot 'measure-running.ps1') `
        -Arguments @('-Seconds', [string]$MeasureSeconds)
    if ($Measure.ExitCode -ne 0) {
        throw ('真实后台资源超过限制或服务中途退出。' + "`r`n" + $Measure.Text)
    }
    if (-not $IntegratedMode) {
        $Shortcuts = Invoke-WizardPowerShellScript `
            -Path (Join-Path $PSScriptRoot 'create-desktop-shortcuts.ps1')
        if ($Shortcuts.ExitCode -ne 0) {
            throw ('桌面入口创建失败。' + "`r`n" + $Shortcuts.Text)
        }
    }
    $FinalStatus = Invoke-ProgressCli -Context $Context -Command 'status'
    if ($FinalStatus.ExitCode -ne 0) {
        throw '最终状态不是“正在运行”。'
    }

    @(
        '进度通知 - 飞书首次设置完成',
        ('北京时间：' + (Get-BeijingTimestamp)),
        ('启用前忽略的历史通知：' + $PendingHooks),
        '服务：正在运行。',
        ('独立服务开机自启：' + $(if ($AutostartCreated) { '已创建' } else { '未创建（可在 TreasureChest 设置）' }) + '。'),
        ('独立桌面入口：' + $(if ($IntegratedMode) { '未创建（统一使用 TreasureChest）' } else { '已创建' }) + '。'),
        ('真实性能测量：通过，时长 ' + $MeasureSeconds + ' 秒。'),
        '',
        $Measure.Text
    ) | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Show-WizardMessage -NoDialog:$NoDialog -Title '进度通知 - 设置成功' -Text (
        '飞书设置完成：后台正在运行。' + "`r`n" +
        '开机自启和入口均按本次安装模式处理，可在 TreasureChest 中随时调整。' + "`r`n" +
        '群聊、普通消息和其他联系人不会触发 Codex。' + "`r`n" +
        '报告：' + $ReportPath
    )
}
catch {
    if ($Started -or $AutostartCreated) {
        Invoke-FeishuSetupRollback
    }
    @(
        '进度通知 - 飞书首次设置未完成',
        ('北京时间：' + (Get-BeijingTimestamp)),
        ('原因：' + $_.Exception.Message),
        '若后台或自启由本轮创建，已请求正常停止并撤销本轮改动。'
    ) | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Show-WizardMessage -NoDialog:$NoDialog -Title '进度通知 - 设置未完成' `
        -Icon Error -Text ($_.Exception.Message + "`r`n`r`n服务未启用或已回滚。")
    throw
}
