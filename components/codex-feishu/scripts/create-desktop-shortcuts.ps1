[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Desktop = [Environment]::GetFolderPath('Desktop')
if ([String]::IsNullOrWhiteSpace($Desktop)) {
    throw '无法定位当前用户桌面目录。'
}
$Entries = @(
    @{
        Name = '进度通知 - 飞书首次设置.lnk'
        Target = (Join-Path $ProjectRoot '飞书首次设置.cmd')
        Description = '安全保存凭证、绑定手机用户并发送测试消息'
    },
    @{
        Name = '进度通知 - 启动.lnk'
        Target = (Join-Path $ProjectRoot '启动进度通知.cmd')
        Description = '在后台启动飞书进度通知服务'
    },
    @{
        Name = '进度通知 - 查看状态.lnk'
        Target = (Join-Path $ProjectRoot '查看进度通知状态.cmd')
        Description = '查看进度通知后台服务状态'
    },
    @{
        Name = '进度通知 - 停止.lnk'
        Target = (Join-Path $ProjectRoot '停止进度通知.cmd')
        Description = '请求进度通知后台服务正常停止'
    }
)
$Shell = New-Object -ComObject WScript.Shell
foreach ($Entry in $Entries) {
    if (-not (Test-Path -LiteralPath $Entry.Target -PathType Leaf)) {
        throw ('快捷方式目标不存在：' + $Entry.Target)
    }
    $ShortcutPath = Join-Path $Desktop $Entry.Name
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Entry.Target
    # 覆盖同名快捷方式时 WScript 会保留旧参数，必须显式清空。
    $Shortcut.Arguments = ''
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.Description = $Entry.Description
    $Shortcut.WindowStyle = 1
    $Shortcut.Save()
    Write-Host ('已创建：' + $ShortcutPath)
}
