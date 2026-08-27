[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$ProbeTimeoutSeconds = 60,
    [switch]$SkipSafeProbe,
    [switch]$NoOpenReport,
    [switch]$NoDialog
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$ReportPath = Join-Path $Context.StateDir 'last-next-step.txt'
$Report = [System.Collections.Generic.List[string]]::new()
$Report.Add('进度通知 - 远控下一步安全向导')
$Report.Add(('北京时间：' + (Get-BeijingTimestamp)))
$Report.Add('安全承诺：本向导不激活、不发送微信、不启动服务、不创建开机自启。')
$Report.Add('')

function Add-CommandResult {
    param([string]$Title, $Result)
    $Report.Add(('[' + $Title + '] 退出码：' + $Result.ExitCode))
    foreach ($Line in $Result.Lines) { $Report.Add($Line) }
    $Report.Add('')
}

$Status = Invoke-ProgressCli -Context $Context -Command 'status'
Add-CommandResult -Title '服务状态' -Result $Status
if ($Status.ExitCode -notin @(0, 1)) {
    throw '无法可靠读取服务状态。'
}

$StatusJson = $null
try { $StatusJson = $Status.Text | ConvertFrom-Json } catch { }
$ServiceRunning = ($Status.ExitCode -eq 0)

$Validate = Invoke-ProgressCli -Context $Context -Command 'validate'
Add-CommandResult -Title '配置与依赖' -Result $Validate

$Doctor = Invoke-ProgressCli -Context $Context -Command 'doctor'
Add-CommandResult -Title 'Codex 与后端只读诊断' -Result $Doctor
$DoctorJson = $null
try { $DoctorJson = $Doctor.Text | ConvertFrom-Json } catch { }
$Backend = if ($null -ne $DoctorJson) { [string]$DoctorJson.wechat_backend } else { 'unknown' }

$ProbeExit = $null
if ($Backend -eq 'probe_only' -and -not $SkipSafeProbe) {
    # 复用带 60 秒进程超时的现有探针；严格模式不接受用户口头身份代替机器校验。
    $ProbeScript = Join-Path $PSScriptRoot 'one-click-safe-check.ps1'
    $Probe = Invoke-WizardPowerShellScript -Path $ProbeScript -Arguments @(
        '-ToolsRoot', $Context.ToolsRoot,
        '-TimeoutSeconds', [string]([Math]::Max(10, $ProbeTimeoutSeconds))
    )
    $ProbeExit = $Probe.ExitCode
    $Report.Add(('[免费微信严格只读探针] 退出码：' + $Probe.ExitCode))
    $Report.Add('探针不点击、不聚焦、不读取聊天正文；详细结果见 .state\last-safe-wechat-check.txt。')
    $Report.Add('')
}

$Conclusion = ''
if ($ServiceRunning) {
    $Conclusion = '服务当前正在运行。请先使用桌面“停止”入口，再重新运行本向导；本向导没有改变运行状态。'
}
elseif ($Backend -eq 'probe_only') {
    $Conclusion = @(
        '当前为 probe_only 安全模式，服务已保持关闭。',
        '你无需为了本工具登录、退出或切换微信账号；已登录的小号可照常手工使用。',
        '微信 4.1.12.55 目前没有满足安全约束的免费生产后端，因此暂时不能真实通知或回传。',
        'Codex 共享连接是独立功能：从桌面双击“进度通知 - 一键共享 Codex”，按提示退出并进行第二次确认；启动器只给新 Codex 子进程注入回环地址。',
        '不要运行激活、测试发送、正式启动或开机自启；等待生产后端方案发生变化后，再点本向导即可。'
    ) -join "`r`n"
}
elseif ($Validate.ExitCode -eq 0 -and $Doctor.ExitCode -eq 0) {
    $Verify = Invoke-ProgressCli -Context $Context -Command 'verify-wechat'
    Add-CommandResult -Title '工具小号与唯一联系人只读核验' -Result $Verify
    if ($Verify.ExitCode -eq 0) {
        $MarkerPath = Join-Path $Context.StateDir 'production-preflight.json'
        Write-WizardJson -Path $MarkerPath -Value ([ordered]@{
            version = 1
            config_sha256 = Get-WizardConfigHash -Context $Context
            created_utc = [DateTimeOffset]::UtcNow.ToString('o')
            sends_message = $false
        })
        $ShortcutPath = New-ProjectDesktopShortcut -Context $Context `
            -Name '进度通知 - 白名单测试（会发送1条）.lnk' `
            -TargetName '一键白名单测试（会发送微信）.cmd' `
            -Description '只在新鲜只读预检后，确认并向唯一白名单好友发送一条测试消息'
        $Conclusion = @(
            '生产只读预检已通过；本向导仍未发送任何消息。',
            ('已临时创建下一阶段入口：' + $ShortcutPath),
            '只有你明确双击该入口并在默认“否”的确认框选择“是”，才会发送一条白名单测试消息。'
        ) -join "`r`n"
    }
    else {
        $Conclusion = @(
            '生产依赖已安装，但工具小号或唯一联系人只读核验未通过。',
            '现在才需要登录并展开配置中的工具小号；不要同时展开其它微信主窗口，然后重新点本向导。',
            '本次未监听、未发送、未启动。'
        ) -join "`r`n"
    }
}
else {
    $Conclusion = @(
        '配置、Codex app-server 或生产微信依赖尚未就绪。',
        '请查看本报告中的“配置与依赖”和“只读诊断”；本次未监听、未发送、未启动。'
    ) -join "`r`n"
}

$Report.Add('[结论与唯一下一步]')
foreach ($Line in ($Conclusion -split "`r?`n")) { $Report.Add($Line) }
$Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8

$Summary = $Conclusion + "`r`n`r`n报告：" + $ReportPath
Show-WizardMessage -Text $Summary -Title '进度通知 - 下一步安全向导' -NoDialog:$NoDialog
Open-WizardReport -Path $ReportPath -NoOpenReport:$NoOpenReport
