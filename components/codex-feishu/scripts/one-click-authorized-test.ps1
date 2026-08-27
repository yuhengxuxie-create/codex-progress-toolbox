[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$NoDialog
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$ConfigHash = Get-WizardConfigHash -Context $Context
$PreflightPath = Join-Path $Context.StateDir 'production-preflight.json'
$TestMarkerPath = Join-Path $Context.StateDir 'wechat-test-sent.json'
$ReportPath = Join-Path $Context.StateDir 'last-authorized-test.txt'

$Status = Invoke-ProgressCli -Context $Context -Command 'status'
if ($Status.ExitCode -ne 1) {
    throw '服务必须处于“未运行”状态，才能执行单条白名单测试。'
}

$ExistingTest = Read-WizardJson -Path $TestMarkerPath
if (Test-WizardMarker -Marker $ExistingTest -ConfigHash $ConfigHash -MaxAgeSeconds 7200) {
    $ShortcutPath = New-ProjectDesktopShortcut -Context $Context `
        -Name '进度通知 - 确认收到后正式启用.lnk' `
        -TargetName '一键确认测试后正式启用.cmd' `
        -Description '确认唯一白名单好友已收到测试消息后，建立历史基线并正式启用'
    Show-WizardMessage -Text (@(
        '当前配置在两小时内已经成功发送过一条测试消息；为防误触，本次没有重复发送。',
        ('确认大号收到后，请使用：' + $ShortcutPath)
    ) -join "`r`n") -Title '进度通知 - 已阻止重复测试' -NoDialog:$NoDialog
    return
}

$Preflight = Read-WizardJson -Path $PreflightPath
if (-not (Test-WizardMarker -Marker $Preflight -ConfigHash $ConfigHash -MaxAgeSeconds 1800)) {
    throw '只读生产预检不存在、已超过 30 分钟或配置已改变；请先运行“一键下一步安全向导”。'
}

foreach ($Command in @('validate', 'doctor', 'verify-wechat')) {
    $Result = Invoke-ProgressCli -Context $Context -Command $Command
    if ($Result.ExitCode -ne 0) {
        throw ('发送前安全门未通过：' + $Command + "`r`n" + $Result.Text)
    }
}

$Confirmed = Confirm-WizardAction -NoDialog:$NoDialog -Text @'
只读核验已再次通过。

下一步只会从配置中的工具小号，向唯一白名单好友发送 1 条测试消息；不会读取或回复其他联系人，也不会启动后台服务。

是否现在发送这 1 条测试消息？
'@
if (-not $Confirmed) {
    throw '你没有确认发送；本次没有发送任何微信消息。'
}

$Send = Invoke-ProgressCli -Context $Context -Command 'test-wechat' -Arguments @(
    '--text',
    '进度通知：唯一白名单发送链路测试成功。请勿直接回复；正式启用后请引用 PCWX 通知回复。'
)
if ($Send.ExitCode -ne 0) {
    throw ('白名单测试发送失败；服务仍保持关闭。' + "`r`n" + $Send.Text)
}

Write-WizardJson -Path $TestMarkerPath -Value ([ordered]@{
    version = 1
    config_sha256 = $ConfigHash
    created_utc = [DateTimeOffset]::UtcNow.ToString('o')
    sent_count = 1
})
$ShortcutPath = New-ProjectDesktopShortcut -Context $Context `
    -Name '进度通知 - 确认收到后正式启用.lnk' `
    -TargetName '一键确认测试后正式启用.cmd' `
    -Description '确认唯一白名单好友已收到测试消息后，建立历史基线并正式启用'

@(
    '进度通知 - 单条白名单测试',
    ('北京时间：' + (Get-BeijingTimestamp)),
    '结果：只向配置中的唯一白名单好友发送了 1 条测试消息。',
    '后台服务：仍未运行。',
    ('下一步入口：' + $ShortcutPath)
) | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Show-WizardMessage -Text (@(
    '已发送且只发送了 1 条白名单测试消息，后台服务仍未启动。',
    '请先在大号确认确实收到，再双击：',
    $ShortcutPath
) -join "`r`n") -Title '进度通知 - 请核对大号' -NoDialog:$NoDialog
