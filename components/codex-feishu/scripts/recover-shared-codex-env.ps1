[CmdletBinding()]
param(
    [switch]$Recover,
    [switch]$Watch,
    [string]$MarkerPath,
    [int]$ParentPid,
    [long]$ParentStartTimeUtcTicks,
    [string]$GenerationToken,
    [ValidateRange(100, 10000)]
    [int]$PollMilliseconds = 500,
    [ValidateRange(10, 5000)]
    [int]$GatewayRetryUnitMilliseconds = 1000
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# 恢复脚本只接受项目 .state 下的固定 marker，拒绝任何外部传入路径。
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$StateRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.state')).TrimEnd('\')
$ExpectedMarkerPath = [IO.Path]::GetFullPath((Join-Path $StateRoot 'codex-launch-environment.json'))
$RecoveryScriptPath = [IO.Path]::GetFullPath($PSCommandPath)
$RunOncePath = 'Software\Microsoft\Windows\CurrentVersion\RunOnce'
$RunOnceName = '!ProgressCheckingWX.CodexEnvironmentRecovery'
$EnvironmentOwnerVariable = 'PROGRESS_WX_CODEX_LAUNCH_TOKEN'

function Get-CodexEnvironmentMutexName {
    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ([String]::IsNullOrWhiteSpace($Sid)) {
        throw '无法读取当前 Windows 用户 SID；拒绝恢复临时环境。'
    }
    return ('Global\ProgressCheckingWX-CodexEnvironment-' + $Sid)
}

function Enter-CodexEnvironmentMutex {
    $Created = $false
    $Mutex = [Threading.Mutex]::new($false, (Get-CodexEnvironmentMutexName), [ref]$Created)
    try {
        try { $Acquired = $Mutex.WaitOne(30000) }
        catch [Threading.AbandonedMutexException] { $Acquired = $true }
        if (-not $Acquired) { throw '共享 Codex 环境事务锁超时，保留恢复状态等待重试。' }
        return $Mutex
    }
    catch {
        $Mutex.Dispose()
        throw
    }
}

function Exit-CodexEnvironmentMutex {
    param($Mutex)
    if ($null -eq $Mutex) { return }
    try { $Mutex.ReleaseMutex() } finally { $Mutex.Dispose() }
}

function Assert-LoopbackWebSocketUrl {
    param([Parameter(Mandatory)][string]$Value)
    try { $Uri = [Uri]::new($Value) }
    catch { throw '恢复 marker 的 URL 无效，拒绝清理。' }
    if ($Uri.Scheme -ne 'ws' -or $Uri.Host -ne '127.0.0.1' -or
        $Uri.Port -lt 1024 -or $Uri.Port -gt 65535 -or
        -not [String]::IsNullOrEmpty($Uri.UserInfo) -or
        -not [String]::IsNullOrEmpty($Uri.Query) -or
        -not [String]::IsNullOrEmpty($Uri.Fragment) -or
        $Uri.AbsolutePath -ne '/') {
        throw '恢复 marker 不是受支持的固定 IPv4 回环 URL。'
    }
    return $Uri.AbsoluteUri
}

function Assert-MarkerPath {
    param([string]$Candidate)
    if ([String]::IsNullOrWhiteSpace($Candidate)) {
        return $ExpectedMarkerPath
    }
    try {
        $FullPath = [IO.Path]::GetFullPath($Candidate)
    }
    catch {
        throw '恢复 marker 路径无法规范化，已拒绝操作。'
    }
    if (-not $FullPath.Equals($ExpectedMarkerPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw '恢复 marker 必须位于本项目 .state\codex-launch-environment.json，已拒绝操作。'
    }
    return $FullPath
}

function Get-WindowsPowerShellExecutable {
    $Candidate = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw '找不到 Windows PowerShell，无法验证恢复命令。'
    }
    return (Resolve-Path -LiteralPath $Candidate).Path
}

function Get-RecoveryRunOnceCommand {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Token
    )
    $PowerShellExe = Get-WindowsPowerShellExecutable
    $ScriptPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'recover-shared-codex-env.ps1'))
    if (-not $ScriptPath.Equals($RecoveryScriptPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw '恢复脚本路径校验失败，已拒绝生成 RunOnce 命令。'
    }
    return ('"' + $PowerShellExe + '" -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden ' +
        '-ExecutionPolicy Bypass -File "' + $ScriptPath + '" -Recover -MarkerPath "' +
        $Path + '" -GenerationToken "' + $Token + '"')
}

function Remove-RecoveryRunOnce {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Token
    )
    $ExpectedCommand = Get-RecoveryRunOnceCommand -Path $Path -Token $Token
    $Key = $null
    try {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($RunOncePath, $true)
        if ($null -eq $Key) { return }
        $Existing = $Key.GetValue($RunOnceName, $null)
        # 并发程序改写了固定名称时不碰它，避免覆盖别人的恢复动作。
        if ($null -eq $Existing -or [string]$Existing -ne $ExpectedCommand) { return }
        $Key.DeleteValue($RunOnceName, $false)
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
}

function Add-ProgressWxNativeMethods {
    if ('ProgressWx.NativeMethods' -as [type]) { return }
    $SourcePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'ProgressWxNativeMethods.cs'))
    $ScriptPrefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if (-not $SourcePath.StartsWith($ScriptPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw '固定环境广播源文件不存在或越出脚本目录。'
    }
    # 恢复进程同样使用仓库内固定源文件，避免系统 TEMP 清理竞争。
    Add-Type -Path $SourcePath
    if (-not ('ProgressWx.NativeMethods' -as [type])) {
        throw '环境广播本机方法加载失败。'
    }
}

function Send-EnvironmentChanged {
    Add-ProgressWxNativeMethods
    $Result = [UIntPtr]::Zero
    [void][ProgressWx.NativeMethods]::SendMessageTimeout(
        [IntPtr]0xffff,
        0x001A,
        [UIntPtr]::Zero,
        'Environment',
        0x0002,
        5000,
        [ref]$Result
    )
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Value)
    $Sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return (($Sha256.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $Sha256.Dispose()
    }
}

function Test-StrictPositiveInteger {
    param($Value)
    return (($Value -is [int]) -or ($Value -is [long])) -and
        -not ($Value -is [bool]) -and [long]$Value -gt 0
}

function Assert-LocalAbsoluteStatePath {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Label
    )
    if ([String]::IsNullOrWhiteSpace($Value) -or -not [IO.Path]::IsPathRooted($Value)) {
        throw ($Label + ' 必须是绝对本机路径。')
    }
    try { $FullPath = [IO.Path]::GetFullPath($Value) }
    catch { throw ($Label + ' 无法规范化。') }
    if ([Uri]::new($FullPath).IsUnc) {
        throw ($Label + ' 不能位于 UNC/网络路径。')
    }
    return $FullPath
}

function Request-OwnedGatewayStop {
    param(
        [Parameter(Mandatory)]$Marker,
        [Parameter(Mandatory)][int]$RetryUnitMilliseconds
    )

    if ($Marker.gateway_cleanup_enabled -ne $true) { return $true }
    $LaunchToken = [string]$Marker.gateway_launch_token
    if ($LaunchToken -notmatch '^[0-9a-f]{64}$') {
        throw '恢复 marker 的 gateway 启动归属令牌无效。'
    }
    $GatewayPidFile = [IO.Path]::GetFullPath((Join-Path $StateRoot 'codex-gateway.pid'))
    $ExpectedHash = Get-Sha256Hex -Value $LaunchToken
    # 第 1 次立即检查，之后按 1、3、6、12 个单位递增等待；默认覆盖约 22 秒。
    $RetryDelays = @(0, 1, 3, 6, 12)
    for ($Attempt = 0; $Attempt -lt $RetryDelays.Count; $Attempt++) {
        if ($RetryDelays[$Attempt] -gt 0) {
            Start-Sleep -Milliseconds ($RetryDelays[$Attempt] * $RetryUnitMilliseconds)
        }
        if (-not (Test-Path -LiteralPath $GatewayPidFile -PathType Leaf)) { continue }
        try {
            # 新版 PID 会原子发布；这里继续容忍旧版/磁盘瞬态读取失败，最终仍失败关闭。
            $Gateway = Get-Content -LiteralPath $GatewayPidFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $GatewayPid = [int]$Gateway.pid
            $GatewayCreationTime = [long]$Gateway.creation_time
            $GatewayProjectRoot = [IO.Path]::GetFullPath([string]$Gateway.project_root)
            $GatewayTokenHash = [string]$Gateway.launch_token_sha256
        }
        catch {
            continue
        }
        if ($GatewayPid -le 0 -or $GatewayCreationTime -le 0 -or
            -not $GatewayProjectRoot.Equals($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -or
            $GatewayTokenHash -ne $ExpectedHash) {
            # PID 文件已明确属于另一代或不是本项目；绝不停止替代实例。
            return $true
        }
        try {
            $Process = Get-Process -Id $GatewayPid -ErrorAction Stop
            $ActualCreationTime = $Process.StartTime.ToUniversalTime().ToFileTimeUtc()
        }
        catch {
            # 进程可能刚发布状态或暂时不可读；不能把未知误当成已经回收。
            continue
        }
        if ($ActualCreationTime -ne $GatewayCreationTime) { return $true }

        $StopPath = $GatewayPidFile + '.stop'
        if (Test-Path -LiteralPath $StopPath -PathType Leaf) {
            try { $Existing = Get-Content -LiteralPath $StopPath -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { throw '已有 gateway 停止标记损坏，拒绝覆盖。' }
            $ExistingVersion = $Existing.version
            $ValidVersion = (($ExistingVersion -is [int]) -or ($ExistingVersion -is [long])) -and
                [long]$ExistingVersion -eq 1
            if (-not $ValidVersion) {
                throw '已有 gateway 停止标记版本无效，拒绝覆盖。'
            }
            if (-not (Test-StrictPositiveInteger -Value $Existing.pid) -or
                -not (Test-StrictPositiveInteger -Value $Existing.creation_time)) {
                throw '已有 gateway 停止标记世代字段必须是正整数。'
            }
            if ([long]$Existing.pid -eq $GatewayPid -and
                [long]$Existing.creation_time -eq $GatewayCreationTime) {
                return $true
            }
            throw '已有 gateway 停止标记属于另一代，拒绝覆盖。'
        }
        $Temporary = $StopPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
        try {
            $Payload = [ordered]@{
                version = 1
                pid = $GatewayPid
                creation_time = $GatewayCreationTime
                requested_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            } | ConvertTo-Json -Compress
            [IO.File]::WriteAllText($Temporary, $Payload + "`r`n", [Text.UTF8Encoding]::new($false))
            try {
                [IO.File]::Move($Temporary, $StopPath)
            }
            catch {
                if (-not (Test-Path -LiteralPath $StopPath -PathType Leaf)) { throw }
                $Existing = Get-Content -LiteralPath $StopPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $ExistingVersion = $Existing.version
                $ValidVersion = (($ExistingVersion -is [int]) -or ($ExistingVersion -is [long])) -and
                    [long]$ExistingVersion -eq 1
                if (-not $ValidVersion) {
                    throw '并发 gateway 停止标记版本无效，拒绝覆盖。'
                }
                if (-not (Test-StrictPositiveInteger -Value $Existing.pid) -or
                    -not (Test-StrictPositiveInteger -Value $Existing.creation_time)) {
                    throw '并发 gateway 停止标记世代字段必须是正整数。'
                }
                if ([long]$Existing.pid -ne $GatewayPid -or
                    [long]$Existing.creation_time -ne $GatewayCreationTime) {
                    throw '并发 gateway 停止标记属于另一代，拒绝覆盖。'
                }
            }
        }
        finally {
            if (Test-Path -LiteralPath $Temporary -PathType Leaf) {
                Remove-Item -LiteralPath $Temporary -Force
            }
        }
        return $true
    }
    return $false
}

function Request-OwnedGatewayRecoveryV4 {
    param([Parameter(Mandatory)]$Marker)

    if ($Marker.gateway_cleanup_enabled -ne $true) { return $true }
    $LaunchToken = [string]$Marker.gateway_launch_token
    if ($LaunchToken -notmatch '^[0-9a-f]{64}$') {
        throw '恢复 marker 的 v4 gateway 启动归属令牌无效。'
    }
    $ExpectedPidFile = Assert-LocalAbsoluteStatePath `
        -Value ([string]$Marker.gateway_pid_file) -Label 'gateway PID 路径'
    $ExpectedStateFile = Assert-LocalAbsoluteStatePath `
        -Value ([string]$Marker.shared_desktop_state_file) -Label 'Desktop 状态路径'
    $ExpectedUrl = Assert-LoopbackWebSocketUrl -Value ([string]$Marker.websocket_url)
    # 项目路径固定在 Tool\AI Agent\Codex 下，可由项目根反推唯一隔离 Python。
    $ToolsRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot '..\..\..'))
    $PythonExe = [IO.Path]::GetFullPath((Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'))
    $EntryPoint = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'progress-wx.py'))
    $ConfigPath = [IO.Path]::GetFullPath((Join-Path $ProjectRoot 'config.yaml'))
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $EntryPoint -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw 'v4 gateway 恢复所需的固定 Python、入口或配置不存在。'
    }
    $Output = @(
        & $PythonExe $EntryPoint --config $ConfigPath gateway-recover-owned `
            --expected-launch-token $LaunchToken `
            --expected-pid-file $ExpectedPidFile `
            --expected-state-file $ExpectedStateFile `
            --expected-websocket-url $ExpectedUrl 2>&1
    )
    if ($LASTEXITCODE -ne 0) {
        throw 'v4 gateway 授权/世代恢复尚未确认；已保留 marker 和 RunOnce。'
    }
    try { $Result = (@($Output | ForEach-Object { [string]$_ }) -join "`n") | ConvertFrom-Json }
    catch { throw 'v4 gateway 恢复结果格式无效；已保留恢复状态。' }
    if ($Result.resolved -ne $true) {
        throw 'v4 gateway 恢复未返回 resolved=true；已保留恢复状态。'
    }
    return $true
}

function Remove-OwnedUserEnvironmentPair {
    param(
        [Parameter(Mandatory)][string]$Token,
        [Parameter(Mandatory)][string]$ExpectedUrl,
        [string]$OwnerName = $EnvironmentOwnerVariable,
        [string]$UrlName = 'CODEX_APP_SERVER_WS_URL'
    )
    foreach ($Name in @($OwnerName, $UrlName)) {
        if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$') {
            throw '待恢复的用户环境变量名称无效。'
        }
    }
    $Key = $null
    try {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        if ($null -eq $Key) { return $false }
        $Names = @($Key.GetValueNames())
        $ActualOwnerName = @($Names | Where-Object {
            [string]::Equals([string]$_, $OwnerName, [StringComparison]::OrdinalIgnoreCase)
        } | Select-Object -First 1)
        $ActualUrlName = @($Names | Where-Object {
            [string]::Equals([string]$_, $UrlName, [StringComparison]::OrdinalIgnoreCase)
        } | Select-Object -First 1)
        $OwnerPresent = $ActualOwnerName.Count -gt 0
        $UrlPresent = $ActualUrlName.Count -gt 0
        $CurrentOwner = if ($OwnerPresent) {
            [string]$Key.GetValue([string]$ActualOwnerName[0], $null)
        }
        else { $null }
        $CurrentUrl = if ($UrlPresent) {
            [string]$Key.GetValue([string]$ActualUrlName[0], $null)
        }
        else { $null }

        if ($OwnerPresent -and $CurrentOwner -eq $Token) {
            # 直接删除注册表值，确保恢复后名称本身不存在，并绕开用户环境枚举缓存差异。
            if ($UrlPresent -and $CurrentUrl -eq $ExpectedUrl) {
                $Key.DeleteValue([string]$ActualUrlName[0], $false)
            }
            $Key.DeleteValue([string]$ActualOwnerName[0], $false)
            $Key.Flush()
            return $true
        }
        if (-not $OwnerPresent) {
            if ($UrlPresent -and $CurrentUrl -eq $ExpectedUrl) {
                throw '临时 URL 存在但 owner token 缺失，拒绝猜测或删除。'
            }
            return $false
        }
        throw '环境 owner token 已属于另一代事务，拒绝清理。'
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
}

function Invoke-Recovery {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Token
    )
    $Mutex = Enter-CodexEnvironmentMutex
    try {
        $MarkerExists = Test-Path -LiteralPath $Path -PathType Leaf
        if (-not $MarkerExists) {
            Remove-RecoveryRunOnce -Path $Path -Token $Token
            return
        }
        try {
            $Marker = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            throw '恢复 marker 损坏，拒绝猜测或清理用户环境。'
        }
        $MarkerToken = [string]$Marker.generation_token
        if ($Marker.version -notin @(2, 3, 4) -or $MarkerToken -notmatch '^[0-9a-f]{32}$' -or
            $MarkerToken -ne $Token) {
            throw '恢复 marker generation token 不匹配，拒绝清理另一代事务。'
        }
        $ExpectedUrl = Assert-LoopbackWebSocketUrl -Value ([string]$Marker.websocket_url)
        if (Remove-OwnedUserEnvironmentPair -Token $Token -ExpectedUrl $ExpectedUrl) {
            Send-EnvironmentChanged
        }
        # 仅 watcher/RunOnce 会执行本脚本；正常流程由启动器内联恢复，不会走此回收。
        if ($Marker.version -eq 4) {
            $GatewayCleanupComplete = Request-OwnedGatewayRecoveryV4 -Marker $Marker
        }
        elseif ($Marker.version -eq 3) {
            $GatewayCleanupComplete = Request-OwnedGatewayStop -Marker $Marker `
                -RetryUnitMilliseconds $GatewayRetryUnitMilliseconds
        }
        else {
            # v2 只包含临时环境事务，不具有 gateway 清理语义。
            $GatewayCleanupComplete = $true
        }
        if ($GatewayCleanupComplete -ne $true) {
            throw 'gateway 归属状态暂不可确认；已保留 marker 和 RunOnce，等待下次恢复。'
        }
        Remove-RecoveryRunOnce -Path $Path -Token $Token
        Remove-Item -LiteralPath $Path -Force
    }
    finally {
        Exit-CodexEnvironmentMutex -Mutex $Mutex
    }
}

function Test-ParentAlive {
    param(
        [Parameter(Mandatory)][int]$Pid,
        [Parameter(Mandatory)][long]$StartTimeUtcTicks
    )
    try {
        $Process = Get-Process -Id $Pid -ErrorAction Stop
        return (-not $Process.HasExited) -and
            $Process.StartTime.ToUniversalTime().Ticks -eq $StartTimeUtcTicks
    }
    catch {
        return $false
    }
}

$SafeMarkerPath = Assert-MarkerPath -Candidate $MarkerPath
if ($GenerationToken -notmatch '^[0-9a-f]{32}$') {
    throw '恢复脚本缺少有效的 generation token，已拒绝运行。'
}
if ($Recover) {
    Invoke-Recovery -Path $SafeMarkerPath -Token $GenerationToken
    exit 0
}
if (-not $Watch) {
    throw '恢复脚本必须以 -Recover 或 -Watch 运行。'
}
if ($ParentPid -le 0) {
    throw 'watcher 缺少有效的启动器 PID，已拒绝运行。'
}
if ($ParentStartTimeUtcTicks -le 0) {
    throw 'watcher 缺少有效的启动器创建时间，已拒绝运行。'
}

while ($true) {
    if (-not (Test-Path -LiteralPath $SafeMarkerPath -PathType Leaf)) {
        # 正常 finally 已清理 marker；watcher 安静退出。
        exit 0
    }
    if (-not (Test-ParentAlive -Pid $ParentPid -StartTimeUtcTicks $ParentStartTimeUtcTicks)) {
        # 启动器异常退出或断电恢复后重新登录时，按 marker 值谨慎撤销 URL。
        Invoke-Recovery -Path $SafeMarkerPath -Token $GenerationToken
        exit 0
    }
    Start-Sleep -Milliseconds $PollMilliseconds
}
