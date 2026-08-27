[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$EnvironmentMutex = $null
$ExitCode = 2

function Get-CodexEnvironmentMutexName {
    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ([String]::IsNullOrWhiteSpace($Sid)) {
        throw '无法读取当前 Windows 用户 SID；拒绝停止共享 gateway。'
    }
    return ('Global\ProgressCheckingWX-CodexEnvironment-' + $Sid)
}

function Enter-CodexEnvironmentMutex {
    $Created = $false
    $Mutex = [Threading.Mutex]::new($false, (Get-CodexEnvironmentMutexName), [ref]$Created)
    try {
        try { $Acquired = $Mutex.WaitOne(15000) }
        catch [Threading.AbandonedMutexException] { $Acquired = $true }
        if (-not $Acquired) { throw '共享 Codex 启动或环境恢复事务仍在运行，请稍后重试。' }
        return $Mutex
    }
    catch {
        $Mutex.Dispose()
        throw
    }
}

try {
    # 与启动/恢复流程共用同一锁；持锁到 gateway 完全退出，阻止受支持路径晚到连接。
    $EnvironmentMutex = Enter-CodexEnvironmentMutex
    $Result = Invoke-ProgressCli -Context $Context -Command 'gateway-stop'
    foreach ($Line in $Result.Lines) { Write-Host $Line }
    $ExitCode = $Result.ExitCode
    if ($ExitCode -ne 0) {
        Write-Host '请先在 Codex 界面中正常退出 Desktop，再重试；脚本不会强制关闭它。'
    }
}
catch {
    Write-Host ('停止共享 Codex 失败：' + $_.Exception.Message)
    $ExitCode = 2
}
finally {
    if ($null -ne $EnvironmentMutex) {
        try { $EnvironmentMutex.ReleaseMutex() } catch { }
        $EnvironmentMutex.Dispose()
    }
}

exit $ExitCode
