[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$DesktopStartWaitSeconds = 300,
    [int]$ExitWaitSeconds = 300,
    [switch]$NoOpenReport,
    [switch]$NoDialog
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'wizard-common.ps1')
$Context = Get-WizardContext -ToolsRoot $ToolsRoot
$ReportPath = Join-Path $Context.StateDir 'last-shared-codex-start.txt'
$Report = [Collections.Generic.List[string]]::new()
$Report.Add('进度通知 - 一键共享 Codex')
$Report.Add(('北京时间：' + (Get-BeijingTimestamp)))
$Report.Add('安全边界：不关闭现有 Codex、不写用户环境或代理、不发送消息。')
$Report.Add('')
# 入口一旦真正执行就立即留下新时间戳，避免窗口意外退出后仍只看到旧报告。
$Report.Add('[启动中]')
$Report.Add('共享启动器已进入 PowerShell 主体。')
$Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
$EnvironmentMarker = Join-Path $Context.StateDir 'codex-launch-environment.json'
$RecoveryScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'recover-shared-codex-env.ps1'))
$EnvironmentOwnerVariable = 'PROGRESS_WX_CODEX_LAUNCH_TOKEN'
$DiagnosticVariablePrefix = 'CODEX_APP_SERVER_PROGRESS_WX_CACHE_PROBE_'
$EnvironmentMutex = $null
$GenerationToken = [Guid]::NewGuid().ToString('N')
$GatewayStartedByThisRun = $false
$GatewayLaunchAttempted = $false
$GatewayRecoveryResolved = $false
$GatewayLaunchToken = ([Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N'))
$GatewayOwnedPid = $null
$GatewayOwnedCreationTime = $null
$ExpectedGatewayPid = $null
$ExpectedGatewayCreationTime = $null
$ProxyFingerprintBefore = $null
$GatewayPidFileAtLaunch = $null
$SharedDesktopStateFileAtLaunch = $null
$DesktopConnectionObserved = $false
$ConnectedPid = $null
$ActivationNotBeforeFileTime = [long]0

function Get-CodexEnvironmentMutexName {
    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ([String]::IsNullOrWhiteSpace($Sid)) {
        throw '无法读取当前 Windows 用户 SID；拒绝修改临时环境。'
    }
    return ('Global\ProgressCheckingWX-CodexEnvironment-' + $Sid)
}

function Enter-CodexEnvironmentMutex {
    $Created = $false
    $Mutex = [Threading.Mutex]::new($false, (Get-CodexEnvironmentMutexName), [ref]$Created)
    try {
        try { $Acquired = $Mutex.WaitOne(15000) }
        catch [Threading.AbandonedMutexException] { $Acquired = $true }
        if (-not $Acquired) { throw '另一个共享 Codex 启动/恢复事务仍在运行。' }
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
    catch { throw '共享 Codex 环境 marker 的 URL 无效，拒绝清理。' }
    if ($Uri.Scheme -ne 'ws' -or $Uri.Host -ne '127.0.0.1' -or
        $Uri.Port -lt 1024 -or $Uri.Port -gt 65535 -or
        -not [String]::IsNullOrEmpty($Uri.UserInfo) -or
        -not [String]::IsNullOrEmpty($Uri.Query) -or
        -not [String]::IsNullOrEmpty($Uri.Fragment) -or
        $Uri.AbsolutePath -ne '/') {
        throw '共享 Codex 环境 marker 不是受支持的固定 IPv4 回环 URL。'
    }
    return $Uri.AbsoluteUri
}

function Write-EnvironmentMarkerAtomic {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Value,
        [Parameter(Mandatory)][string]$Token
    )
    if (Test-Path -LiteralPath $Path) {
        throw '共享 Codex 环境 marker 已存在；拒绝覆盖另一代事务。'
    }
    if ($Token -notmatch '^[0-9a-f]{32}$') {
        throw '共享 Codex 环境 marker token 无效。'
    }
    # 临时文件名使用独立随机值，不把 environment generation token 带入异常路径。
    $Temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $Json = $Value | ConvertTo-Json -Depth 10
        [IO.File]::WriteAllText($Temporary, $Json + "`r`n", [Text.UTF8Encoding]::new($true))
        [IO.File]::Move($Temporary, $Path)
    }
    finally {
        if (Test-Path -LiteralPath $Temporary -PathType Leaf) {
            Remove-Item -LiteralPath $Temporary -Force
        }
    }
}

function Get-CodexPackage {
    $Package = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if ($null -eq $Package -or [String]::IsNullOrWhiteSpace($Package.InstallLocation)) {
        throw '未找到已安装的 Codex Desktop（OpenAI.Codex）。'
    }
    return $Package
}

function Get-CodexDesktopExecutable {
    param([Parameter(Mandatory)]$Package)
    $Manifest = Get-AppxPackageManifest -Package $Package -ErrorAction Stop
    $Applications = @($Manifest.Package.Applications.Application)
    $Application = @($Applications | Where-Object { [string]$_.Id -eq 'App' } | Select-Object -First 1)
    if ($Application.Count -ne 1) {
        throw 'Codex AppX manifest 中没有唯一的 App 入口。'
    }
    if ([string]$Application[0].EntryPoint -ne 'Windows.FullTrustApplication') {
        throw 'Codex AppX 入口不是 Windows.FullTrustApplication，拒绝使用进程级启动。'
    }
    $Relative = ([string]$Application[0].Executable).Replace('/', '\')
    if ([String]::IsNullOrWhiteSpace($Relative) -or [IO.Path]::IsPathRooted($Relative) -or
        @($Relative.Split('\') | Where-Object { $_ -eq '..' }).Count -gt 0) {
        throw 'Codex AppX manifest 的可执行路径无效。'
    }
    $InstallPrefix = [IO.Path]::GetFullPath([string]$Package.InstallLocation).TrimEnd('\') + '\'
    $Executable = [IO.Path]::GetFullPath((Join-Path $Package.InstallLocation $Relative))
    if (-not $Executable.StartsWith($InstallPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw 'Codex AppX 可执行文件不存在或越出包目录。'
    }
    return $Executable
}

function Assert-IndependentLauncherAncestry {
    # 共享入口必须由 Explorer/终端独立启动。若它是 Codex 的后代，正常退出
    # Codex 时 Windows 可能连带终止启动器，使环境恢复和后续等待都无法完成。
    $Seen = [Collections.Generic.HashSet[int]]::new()
    $CursorPid = [int]$PID
    for ($Depth = 0; $Depth -lt 32; $Depth++) {
        if (-not $Seen.Add($CursorPid)) {
            throw '启动器父进程链出现循环；为避免错误接管，已拒绝继续。'
        }
        $Current = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [string]$CursorPid) `
            -ErrorAction SilentlyContinue
        if ($null -eq $Current) { return }
        $ParentPid = [int]$Current.ParentProcessId
        if ($ParentPid -le 0) { return }
        $Parent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [string]$ParentPid) `
            -ErrorAction SilentlyContinue
        if ($null -eq $Parent) { return }
        $ParentName = [string]$Parent.Name
        if ($ParentName -in @('ChatGPT.exe', 'codex.exe')) {
            throw '共享入口不能由 Codex 代为启动。请从桌面手动双击“进度通知 - 一键共享 Codex”，否则关闭 Codex 会连带终止启动器。'
        }
        $CursorPid = $ParentPid
    }
    throw '启动器父进程链超过安全检查上限；为避免错误接管，已拒绝继续。'
}

function Get-CodexDesktopProcesses {
    param([Parameter(Mandatory)][string]$InstallLocation)
    try {
        $Prefix = [IO.Path]::GetFullPath($InstallLocation).TrimEnd('\') + '\'
    }
    catch {
        throw 'Codex 安装目录无法规范化；为避免误判，已拒绝继续。'
    }
    $Matches = [Collections.Generic.List[object]]::new()
    $CurrentSessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    foreach ($Process in @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'")) {
        $ProcessSessionId = $null
        try {
            if ($null -ne $Process.SessionId) {
                $ProcessSessionId = [int]$Process.SessionId
            }
        }
        catch { $ProcessSessionId = $null }
        $IsCurrentSession = $null -eq $ProcessSessionId -or $ProcessSessionId -eq $CurrentSessionId
        $RawPath = [string]$Process.ExecutablePath
        if ([String]::IsNullOrWhiteSpace($RawPath)) {
            if ($IsCurrentSession) {
                # Desktop 正常退出时可能短暂出现“CIM 记录仍在、映像路径已释放”的竞态。
                # 使用另外两种只读来源并做有限重查；PID 已退出应视为正常，而不是报错。
                $ProcessExited = $false
                for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
                    $DotNetProcess = Get-Process -Id ([int]$Process.ProcessId) -ErrorAction SilentlyContinue
                    if ($null -eq $DotNetProcess) {
                        $ProcessExited = $true
                        break
                    }
                    try { $RawPath = [string]$DotNetProcess.Path } catch { $RawPath = '' }
                    if ([String]::IsNullOrWhiteSpace($RawPath)) {
                        try { $RawPath = [string]$DotNetProcess.MainModule.FileName } catch { $RawPath = '' }
                    }
                    if (-not [String]::IsNullOrWhiteSpace($RawPath)) { break }
                    Start-Sleep -Milliseconds 100
                    $Refreshed = Get-CimInstance Win32_Process `
                        -Filter ('ProcessId=' + [string]$Process.ProcessId) -ErrorAction SilentlyContinue
                    if ($null -eq $Refreshed) {
                        $ProcessExited = $true
                        break
                    }
                    $RawPath = [string]$Refreshed.ExecutablePath
                    if (-not [String]::IsNullOrWhiteSpace($RawPath)) { break }
                }
                if ($ProcessExited) { continue }
                if ([String]::IsNullOrWhiteSpace($RawPath)) {
                    throw ('当前会话的 ChatGPT.exe 连续 5 次无法读取映像路径（PID ' +
                        [string]$Process.ProcessId + '）；进程仍存活，为避免误接入，已拒绝继续。')
                }
            }
            else {
                # 其它用户会话的同名进程不属于本启动器的控制范围，不能阻断当前用户。
                continue
            }
        }
        try {
            if (-not [IO.Path]::IsPathRooted($RawPath)) {
                throw '映像路径不是绝对路径。'
            }
            $ExecutablePath = [IO.Path]::GetFullPath($RawPath)
        }
        catch {
            if ($IsCurrentSession) {
                throw ('当前会话 ChatGPT.exe 映像路径无法规范化（PID ' + [string]$Process.ProcessId + '）；为避免误判，已拒绝继续。')
            }
            # 其它用户会话的不可读路径不作跨会话推断，继续检查当前会话。
            continue
        }
        # 其它可读且可规范化的 ChatGPT.exe 路径明确不属于当前 Codex 包，不计入结果。
        if ($ExecutablePath.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
            $Matches.Add($Process)
        }
    }
    return @($Matches)
}

function Get-CodexAppServerUserVariableNames {
    $Names = [Collections.Generic.List[string]]::new()
    $Key = $null
    try {
        # Environment.GetEnvironmentVariables(User) 在同一进程刚删除变量后可能仍返回旧枚举；
        # 直接重开 HKCU\Environment，确保恢复事务后的判定来自注册表当前状态。
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
        if ($null -eq $Key) { return @() }
        foreach ($Name in @($Key.GetValueNames())) {
            if ([string]$Name -and [string]$Name.StartsWith(
                    'CODEX_APP_SERVER_', [StringComparison]::OrdinalIgnoreCase
                ) -and -not [string]$Name.StartsWith(
                    $DiagnosticVariablePrefix, [StringComparison]::OrdinalIgnoreCase
                )) {
                $Names.Add([string]$Name)
            }
        }
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
    return @($Names)
}

function Get-UserEnvironmentEntry {
    param([Parameter(Mandatory)][string]$Name)
    if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$') {
        throw '用户环境变量名称无效。'
    }
    $Key = $null
    try {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
        if ($null -eq $Key) {
            return [pscustomobject]@{ Present = $false; Value = $null }
        }
        $ActualName = @($Key.GetValueNames() | Where-Object {
            [string]::Equals([string]$_, $Name, [StringComparison]::OrdinalIgnoreCase)
        } | Select-Object -First 1)
        if ($ActualName.Count -eq 0) {
            return [pscustomobject]@{ Present = $false; Value = $null }
        }
        return [pscustomobject]@{
            Present = $true
            Value = $Key.GetValue(
                [string]$ActualName[0], $null,
                [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
            )
        }
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
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
            # 直接删除注册表值，确保名称本身消失，并绕开用户环境 API 的枚举缓存差异。
            if ($UrlPresent -and $CurrentUrl -eq $ExpectedUrl) {
                $Key.DeleteValue([string]$ActualUrlName[0], $false)
            }
            $Key.DeleteValue([string]$ActualOwnerName[0], $false)
            $Key.Flush()
            return $true
        }
        if (-not $OwnerPresent) {
            if ($UrlPresent -and $CurrentUrl -eq $ExpectedUrl) {
                throw '临时 URL 存在但缺少本工具 owner token，拒绝猜测或删除。'
            }
            return $false
        }
        throw '临时环境 owner token 已属于另一代事务，拒绝清理。'
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
}

function Remove-ProgressWxDiagnosticUserVariables {
    $Removed = 0
    $Key = $null
    try {
        # 该前缀只属于本项目开发期间的无值探针，不是 Codex 支持的配置项。
        # 绝不删除其它 CODEX_APP_SERVER_* 或任何变量值未知的 owner。
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        if ($null -eq $Key) { return 0 }
        foreach ($Name in @($Key.GetValueNames())) {
            if ([string]$Name -and [string]$Name.StartsWith(
                    $DiagnosticVariablePrefix, [StringComparison]::OrdinalIgnoreCase
                )) {
                $Key.DeleteValue([string]$Name, $false)
                $Removed += 1
            }
        }
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
    return $Removed
}

function Get-ProxyStateFingerprint {
    # 只对会影响普通代理的现有用户设置做不可逆摘要；不记录或输出代理值。
    $Rows = [Collections.Generic.List[string]]::new()
    $Targets = @(
        [pscustomobject]@{
            Scope = 'internet'
            Path = 'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
            Names = @('ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL', 'AutoDetect')
        },
        [pscustomobject]@{
            Scope = 'environment'
            Path = 'Environment'
            Names = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')
        }
    )
    foreach ($Target in $Targets) {
        $Key = $null
        try {
            $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey([string]$Target.Path, $false)
            $ExistingNames = if ($null -eq $Key) { @() } else { @($Key.GetValueNames()) }
            foreach ($ExpectedName in @($Target.Names)) {
                $ActualName = @($ExistingNames | Where-Object {
                    [string]::Equals([string]$_, [string]$ExpectedName, [StringComparison]::OrdinalIgnoreCase)
                } | Select-Object -First 1)
                if ($ActualName.Count -eq 0) {
                    $Rows.Add(([string]$Target.Scope + '|' + [string]$ExpectedName + '|absent|'))
                    continue
                }
                $Name = [string]$ActualName[0]
                $Kind = [string]$Key.GetValueKind($Name)
                $Value = $Key.GetValue(
                    $Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
                )
                $Bytes = if ($Value -is [byte[]]) {
                    [byte[]]$Value
                }
                else {
                    [Text.Encoding]::UTF8.GetBytes([string]$Value)
                }
                $Rows.Add(([string]$Target.Scope + '|' + [string]$ExpectedName + '|' +
                    $Kind + '|' + [Convert]::ToBase64String($Bytes)))
            }
        }
        finally {
            if ($null -ne $Key) { $Key.Dispose() }
        }
    }
    $Sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $Payload = [Text.Encoding]::UTF8.GetBytes(($Rows -join "`n"))
        return (($Sha256.ComputeHash($Payload) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $Sha256.Dispose()
    }
}

function Assert-ProxyStateUnchanged {
    param(
        [Parameter(Mandatory)][string]$ExpectedFingerprint,
        [Parameter(Mandatory)][string]$Stage
    )
    if ((Get-ProxyStateFingerprint) -ne $ExpectedFingerprint) {
        throw ('检测到普通代理配置在共享启动期间发生变化（阶段：' + $Stage +
            '）。本工具未覆盖代理值，已停止并保留现场。')
    }
}

function ConvertTo-ProxyEnvironmentUri {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$DefaultScheme,
        [Parameter(Mandatory)][string[]]$AllowedSchemes
    )
    $Text = $Value.Trim()
    if ([String]::IsNullOrWhiteSpace($Text) -or $Text.IndexOf([char]0) -ge 0 -or
        $Text.Contains("`r") -or $Text.Contains("`n")) {
        throw 'WinINET 固定代理格式无效。'
    }
    if ($Text -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') {
        $Text = $DefaultScheme + '://' + $Text
    }
    try { $Uri = [Uri]::new($Text) }
    catch { throw 'WinINET 固定代理无法转换为子进程代理 URI。' }
    if (-not $Uri.IsAbsoluteUri -or $AllowedSchemes -notcontains $Uri.Scheme.ToLowerInvariant() -or
        [String]::IsNullOrWhiteSpace($Uri.Host) -or $Uri.Port -le 0 -or $Uri.Port -gt 65535 -or
        -not [String]::IsNullOrEmpty($Uri.Query) -or
        -not [String]::IsNullOrEmpty($Uri.Fragment) -or
        $Uri.AbsolutePath -notin @('', '/')) {
        throw 'WinINET 固定代理包含不受支持的协议、端口或路径。'
    }
    return $Uri.AbsoluteUri.TrimEnd('/')
}

function Get-WinInetFixedProxyEnvironment {
    # 只读取当前用户已经启用的固定代理；PAC/自动检测不在本地猜测或展开。
    $Key = $null
    try {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
            'Software\Microsoft\Windows\CurrentVersion\Internet Settings', $false
        )
        if ($null -eq $Key -or [int]$Key.GetValue('ProxyEnable', 0) -ne 1) { return @{} }
        $Raw = [string]$Key.GetValue(
            'ProxyServer', $null,
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
        )
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
    if ([String]::IsNullOrWhiteSpace($Raw) -or $Raw.IndexOf([char]0) -ge 0 -or
        $Raw.Contains("`r") -or $Raw.Contains("`n")) {
        return @{}
    }

    $ProtocolMap = @{}
    if ($Raw.Contains('=')) {
        foreach ($Segment in @($Raw.Split(';'))) {
            $Index = $Segment.IndexOf('=')
            if ($Index -le 0) { continue }
            $Protocol = $Segment.Substring(0, $Index).Trim().ToLowerInvariant()
            $Endpoint = $Segment.Substring($Index + 1).Trim()
            if ($Protocol -in @('http', 'https', 'socks') -and
                -not [String]::IsNullOrWhiteSpace($Endpoint)) {
                $ProtocolMap[$Protocol] = $Endpoint
            }
        }
    }
    else {
        $ProtocolMap['all'] = $Raw.Trim()
    }

    $Result = @{}
    if ($ProtocolMap.ContainsKey('all')) {
        $Endpoint = ConvertTo-ProxyEnvironmentUri -Value ([string]$ProtocolMap['all']) `
            -DefaultScheme 'http' -AllowedSchemes @('http', 'https')
        $Result['HTTP_PROXY'] = $Endpoint
        $Result['HTTPS_PROXY'] = $Endpoint
    }
    else {
        if ($ProtocolMap.ContainsKey('http')) {
            $Result['HTTP_PROXY'] = ConvertTo-ProxyEnvironmentUri `
                -Value ([string]$ProtocolMap['http']) -DefaultScheme 'http' `
                -AllowedSchemes @('http', 'https')
        }
        if ($ProtocolMap.ContainsKey('https')) {
            $Result['HTTPS_PROXY'] = ConvertTo-ProxyEnvironmentUri `
                -Value ([string]$ProtocolMap['https']) -DefaultScheme 'http' `
                -AllowedSchemes @('http', 'https')
        }
        if ($ProtocolMap.ContainsKey('socks')) {
            $Result['ALL_PROXY'] = ConvertTo-ProxyEnvironmentUri `
                -Value ([string]$ProtocolMap['socks']) -DefaultScheme 'socks5' `
                -AllowedSchemes @('socks', 'socks5', 'socks5h')
        }
    }
    return $Result
}

function Invoke-ProgressCliWithFreshUserProxyEnvironment {
    param(
        [Parameter(Mandatory)]$Context,
        [Parameter(Mandatory)][string]$Command,
        [string[]]$Arguments = @()
    )
    $Names = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')
    $Original = @{}
    $OriginalPresent = @{}
    $UserValues = @{}
    $Key = $null
    try {
        $ProcessEnvironment = [Environment]::GetEnvironmentVariables(
            [EnvironmentVariableTarget]::Process
        )
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $false)
        foreach ($Name in $Names) {
            $Original[$Name] = [Environment]::GetEnvironmentVariable(
                $Name, [EnvironmentVariableTarget]::Process
            )
            $OriginalPresent[$Name] = @($ProcessEnvironment.Keys | Where-Object {
                [string]::Equals([string]$_, $Name, [StringComparison]::OrdinalIgnoreCase)
            }).Count -gt 0
            if ($null -eq $Key) { continue }
            $Value = $Key.GetValue($Name, $null)
            if ($null -ne $Value) {
                $Text = [string]$Value
                if ($Text.IndexOf([char]0) -ge 0 -or $Text.Contains("`r") -or $Text.Contains("`n")) {
                    throw ('用户代理环境变量格式无效：' + $Name)
                }
                $UserValues[$Name] = $Text
            }
        }
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
    $EffectiveValues = @{}
    foreach ($Name in $UserValues.Keys) {
        $EffectiveValues[[string]$Name] = [string]$UserValues[$Name]
    }
    $WinInetValues = Get-WinInetFixedProxyEnvironment
    foreach ($Name in $WinInetValues.Keys) {
        if (-not $EffectiveValues.ContainsKey([string]$Name)) {
            $EffectiveValues[[string]$Name] = [string]$WinInetValues[$Name]
        }
    }
    if ($WinInetValues.Count -gt 0 -and -not $EffectiveValues.ContainsKey('NO_PROXY')) {
        $EffectiveValues['NO_PROXY'] = 'localhost,127.0.0.1,::1'
    }
    try {
        # 只刷新当前启动器进程；gateway 子进程会继承，用户/系统环境保持不变。
        # 显式用户变量优先；缺失时只读投影已启用的固定 WinINET 代理。
        foreach ($Name in $Names) {
            # 用户环境中不存在的代理变量也必须在 gateway 启动期间临时清空，
            # 否则旧 Explorer/终端进程里的陈旧值仍会被子进程继承。
            if ($EffectiveValues.ContainsKey($Name)) {
                [Environment]::SetEnvironmentVariable(
                    [string]$Name, [string]$EffectiveValues[$Name], [EnvironmentVariableTarget]::Process
                )
            }
            else {
                # 通过 Env: provider 明确删除名称，避免不同宿主的空值绑定/枚举差异。
                Remove-Item -LiteralPath ('Env:' + $Name) -ErrorAction SilentlyContinue
            }
        }
        return Invoke-ProgressCli -Context $Context -Command $Command -Arguments $Arguments
    }
    finally {
        foreach ($Name in $Names) {
            if ($OriginalPresent[$Name]) {
                [Environment]::SetEnvironmentVariable(
                    $Name, [string]$Original[$Name], [EnvironmentVariableTarget]::Process
                )
            }
            else {
                Remove-Item -LiteralPath ('Env:' + $Name) -ErrorAction SilentlyContinue
            }
        }
    }
}

function Get-WindowsPowerShellExecutable {
    $Candidate = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw '找不到 Windows PowerShell；无法安装异常恢复钩子。'
    }
    return (Resolve-Path -LiteralPath $Candidate).Path
}

function Get-RecoveryScriptPath {
    $Path = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'recover-shared-codex-env.ps1'))
    $Prefix = [IO.Path]::GetFullPath($Context.ProjectRoot).TrimEnd('\') + '\'
    if (-not $Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw '异常恢复脚本不存在或越出项目目录，已拒绝继续。'
    }
    return $Path
}

function Get-RecoveryRunOnceCommand {
    param(
        [Parameter(Mandatory)][string]$RecoveryScript,
        [Parameter(Mandatory)][string]$MarkerPath,
        [Parameter(Mandatory)][string]$Token
    )
    $PowerShellExe = Get-WindowsPowerShellExecutable
    # Windows 合法路径不能包含双引号；完整路径加引号以保留空格和括号。
    return ('"' + $PowerShellExe + '" -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden ' +
        '-ExecutionPolicy Bypass -File "' + $RecoveryScript + '" -Recover -MarkerPath "' +
        $MarkerPath + '" -GenerationToken "' + $Token + '"')
}

function Set-RecoveryRunOnce {
    param(
        [Parameter(Mandatory)][string]$RecoveryScript,
        [Parameter(Mandatory)][string]$MarkerPath,
        [Parameter(Mandatory)][string]$Token
    )
    # ! 前缀要求 Windows 仅在命令成功后消费该值；恢复失败时下次登录会重试。
    $RunOnceName = '!ProgressCheckingWX.CodexEnvironmentRecovery'
    $RunOncePath = 'Software\Microsoft\Windows\CurrentVersion\RunOnce'
    $ExpectedCommand = Get-RecoveryRunOnceCommand -RecoveryScript $RecoveryScript -MarkerPath $MarkerPath -Token $Token
    $Key = $null
    try {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($RunOncePath)
        $Existing = $Key.GetValue($RunOnceName, $null)
        if ($null -ne $Existing -and [string]$Existing -ne $ExpectedCommand) {
            throw 'HKCU RunOnce 的固定恢复项已被其他值占用；为避免覆盖，已拒绝继续。'
        }
        $Key.SetValue($RunOnceName, $ExpectedCommand, [Microsoft.Win32.RegistryValueKind]::String)
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
}

function Remove-RecoveryRunOnce {
    param(
        [Parameter(Mandatory)][string]$RecoveryScript,
        [Parameter(Mandatory)][string]$MarkerPath,
        [Parameter(Mandatory)][string]$Token
    )
    $RunOnceName = '!ProgressCheckingWX.CodexEnvironmentRecovery'
    $RunOncePath = 'Software\Microsoft\Windows\CurrentVersion\RunOnce'
    $ExpectedCommand = Get-RecoveryRunOnceCommand -RecoveryScript $RecoveryScript -MarkerPath $MarkerPath -Token $Token
    $Key = $null
    try {
        $Key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($RunOncePath, $true)
        if ($null -eq $Key) { return }
        $Existing = $Key.GetValue($RunOnceName, $null)
        # 并发值不是本工具写入的，绝不删除或覆盖。
        if ($null -eq $Existing -or [string]$Existing -ne $ExpectedCommand) { return }
        $Key.DeleteValue($RunOnceName, $false)
    }
    finally {
        if ($null -ne $Key) { $Key.Dispose() }
    }
}

function Start-RecoveryWatcher {
    param(
        [Parameter(Mandatory)][string]$RecoveryScript,
        [Parameter(Mandatory)][string]$MarkerPath,
        [Parameter(Mandatory)][string]$Token
    )
    $PowerShellExe = Get-WindowsPowerShellExecutable
    $ParentStartTimeUtcTicks = [Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks
    # Start-Process 会把 ArgumentList 拼成命令行；路径参数必须自行保留双引号。
    $QuotedRecoveryScript = '"' + $RecoveryScript + '"'
    $QuotedMarkerPath = '"' + $MarkerPath + '"'
    $Watcher = Start-Process -FilePath $PowerShellExe -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File', $QuotedRecoveryScript, '-Watch',
        '-MarkerPath', $QuotedMarkerPath, '-ParentPid', [string]$PID,
        '-ParentStartTimeUtcTicks', [string]$ParentStartTimeUtcTicks,
        '-GenerationToken', $Token
    )
    if ($null -eq $Watcher -or $Watcher.HasExited) {
        throw '无法启动隐藏的异常恢复 watcher；已拒绝设置临时环境。'
    }
    return $Watcher
}

function Add-ProgressWxNativeMethods {
    if ('ProgressWx.NativeMethods' -as [type]) { return }
    $SourcePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'ProgressWxNativeMethods.cs'))
    $ScriptPrefix = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if (-not $SourcePath.StartsWith($ScriptPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw '固定环境广播源文件不存在或越出脚本目录。'
    }
    # 使用仓库内固定源文件，避免 TypeDefinition 在系统 TEMP 中生成随机 .cs，
    # 从而消除杀毒/清理程序与 CodeDom 之间的临时文件竞争。
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

function Invoke-GatewayRecoveryV4 {
    param(
        [Parameter(Mandatory)][string]$LaunchToken,
        [Parameter(Mandatory)][string]$GatewayPidFile,
        [Parameter(Mandatory)][string]$SharedDesktopStateFile,
        [Parameter(Mandatory)][string]$WebSocketUrl
    )
    if ($LaunchToken -notmatch '^[0-9a-f]{64}$') {
        throw 'v4 gateway 恢复令牌无效。'
    }
    $ExpectedPidFile = Assert-LocalAbsoluteStatePath -Value $GatewayPidFile -Label 'gateway PID 路径'
    $ExpectedStateFile = Assert-LocalAbsoluteStatePath `
        -Value $SharedDesktopStateFile -Label 'Desktop 状态路径'
    $ExpectedUrl = Assert-LoopbackWebSocketUrl -Value $WebSocketUrl
    $Recovery = Invoke-ProgressCli -Context $Context `
        -Command 'gateway-recover-owned' `
        -Arguments @(
            '--expected-launch-token', $LaunchToken,
            '--expected-pid-file', $ExpectedPidFile,
            '--expected-state-file', $ExpectedStateFile,
            '--expected-websocket-url', $ExpectedUrl
        )
    if ($Recovery.ExitCode -ne 0) {
        throw 'v4 gateway 授权/世代仍未确认恢复。'
    }
    try { $RecoveryJson = $Recovery.Text | ConvertFrom-Json }
    catch { throw 'v4 gateway 恢复结果格式无效。' }
    if ($RecoveryJson.resolved -ne $true) {
        throw 'v4 gateway 恢复未返回 resolved=true。'
    }
    return $RecoveryJson
}

function Restore-ToolEnvironment {
    param(
        [Parameter(Mandatory)][string]$MarkerPath,
        [Parameter(Mandatory)][string]$RecoveryScript,
        [string]$ExpectedGenerationToken,
        [switch]$PreserveRecoveryState
    )
    $MarkerExists = Test-Path -LiteralPath $MarkerPath -PathType Leaf
    if (-not $MarkerExists) { return }
    $Marker = Read-WizardJson -Path $MarkerPath
    $Token = [string]$Marker.generation_token
    if ($Marker.version -notin @(2, 3, 4) -or $Token -notmatch '^[0-9a-f]{32}$') {
        throw '共享 Codex 环境 marker 缺少可信 generation token，拒绝清理。'
    }
    if (-not [String]::IsNullOrWhiteSpace($ExpectedGenerationToken) -and
        $Token -ne $ExpectedGenerationToken) {
        throw '共享 Codex 环境 marker 已属于另一代启动器，拒绝清理。'
    }
    if ([String]::IsNullOrWhiteSpace($ExpectedGenerationToken) -and
        $Marker.version -in @(3, 4) -and $Marker.gateway_cleanup_enabled -eq $true) {
        if ($Marker.version -eq 3) {
            throw '检测到旧版共享 Codex 的 gateway 异常回收尚未完成；已保留恢复状态，拒绝猜测。'
        }
        [void](Invoke-GatewayRecoveryV4 `
            -LaunchToken ([string]$Marker.gateway_launch_token) `
            -GatewayPidFile ([string]$Marker.gateway_pid_file) `
            -SharedDesktopStateFile ([string]$Marker.shared_desktop_state_file) `
            -WebSocketUrl ([string]$Marker.websocket_url))
    }
    $ExpectedUrl = Assert-LoopbackWebSocketUrl -Value ([string]$Marker.websocket_url)
    if (Remove-OwnedUserEnvironmentPair -Token $Token -ExpectedUrl $ExpectedUrl) {
        Send-EnvironmentChanged
    }
    if (-not $PreserveRecoveryState) {
        Remove-RecoveryRunOnce -RecoveryScript $RecoveryScript -MarkerPath $MarkerPath -Token $Token
        Remove-Item -LiteralPath $MarkerPath -Force
    }
}

$RecoveryScript = Get-RecoveryScriptPath

try {
    Assert-IndependentLauncherAncestry
    if ($DesktopStartWaitSeconds -lt 30 -or $DesktopStartWaitSeconds -gt 1800) {
        throw 'DesktopStartWaitSeconds 必须在 30 到 1800 秒之间。'
    }
    if ($ExitWaitSeconds -lt 30 -or $ExitWaitSeconds -gt 1800) {
        throw 'ExitWaitSeconds 必须在 30 到 1800 秒之间。'
    }
    $Package = Get-CodexPackage
    $DesktopExecutable = Get-CodexDesktopExecutable -Package $Package
    # PowerShell 会把空管道折叠为 $null；严格模式下必须显式保留为数组。
    $Existing = @(Get-CodexDesktopProcesses -InstallLocation $Package.InstallLocation)
    $WaitForNormalExit = $Existing.Count -gt 0
    $Report.Add('即将显示本机回环共享确认框。')
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    $ConfirmationLines = [Collections.Generic.List[string]]::new()
    $ConfirmationLines.Add('即将准备仅绑定 127.0.0.1 的共享 Codex app-server。')
    $ConfirmationLines.Add('Desktop 退出后会显示第二个确认框；只有你点击“是”，才用进程级临时环境打开 Codex。')
    $ConfirmationLines.Add('新方式不写用户环境变量，也不广播 Environment，不会触发代理客户端重载。')
    $ConfirmationLines.Add('这不会改变进度通知服务状态，也不会发送任何消息。')
    if ($WaitForNormalExit) {
        $ConfirmationLines.Add(('点击“是”后，请在 ' + [string]$ExitWaitSeconds + ' 秒内正常退出所有 Codex 窗口。'))
        $ConfirmationLines.Add('本工具只等待进程自行退出，绝不会强制结束它。')
    }
    $ConfirmationLines.Add('是否继续？')
    $Confirmed = Confirm-WizardAction -NoDialog:$NoDialog -Text ($ConfirmationLines -join "`r`n")
    if (-not $Confirmed) {
        throw '用户未确认；未启动共享 Codex。'
    }
    if ($WaitForNormalExit) {
        if ($NoDialog) {
            throw 'NoDialog 模式检测到 Codex Desktop 仍在运行，拒绝自动等待或关闭。'
        }
        $Report.Add(('正在等待 Codex Desktop 正常退出，最长 ' + [string]$ExitWaitSeconds + ' 秒。'))
        $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
        $ExitDeadline = [DateTime]::UtcNow.AddSeconds($ExitWaitSeconds)
        while ($Existing.Count -gt 0 -and [DateTime]::UtcNow -lt $ExitDeadline) {
            Start-Sleep -Milliseconds 500
            $Existing = @(Get-CodexDesktopProcesses -InstallLocation $Package.InstallLocation)
        }
        if ($Existing.Count -gt 0) {
            throw '等待 Codex Desktop 正常退出超时；未强制关闭任何进程。请确认所有窗口已退出后重试。'
        }
        $Report.Add('已检测到 Codex Desktop 完全退出；下一步准备共享 gateway。')
    }

    # 等待/取消阶段到此为止仅做进程和文件只读检查。
    $EnvironmentMutex = Enter-CodexEnvironmentMutex
    $Existing = @(Get-CodexDesktopProcesses -InstallLocation $Package.InstallLocation)
    if ($Existing.Count -gt 0) {
        throw '准备共享环境前检测到 Codex Desktop 已重新运行；未修改代理或用户环境，请退出后重试。'
    }
    $ProxyFingerprintBefore = Get-ProxyStateFingerprint
    $Report.Add('普通代理只读基线已建立；后续不写代理，也不把代理变化作为中止条件。')
    if (Test-Path -LiteralPath $EnvironmentMarker -PathType Leaf) {
        throw '检测到旧版共享启动环境 marker；为避免触发旧版环境广播，已拒绝继续。请先报告给开发者处理。'
    }
    $ExistingVariables = @(Get-CodexAppServerUserVariableNames)
    $ExistingOwner = Get-UserEnvironmentEntry -Name $EnvironmentOwnerVariable
    if ($ExistingVariables.Count -gt 0 -or $ExistingOwner.Present) {
        $BlockedNames = [Collections.Generic.List[string]]::new()
        foreach ($Name in $ExistingVariables) { $BlockedNames.Add([string]$Name) }
        if ($ExistingOwner.Present) {
            $BlockedNames.Add($EnvironmentOwnerVariable)
        }
        throw ('用户环境中已有 Codex app-server 或进度通知 owner 配置；新方式不会覆盖或清理它。检测项：' +
            (($BlockedNames | Sort-Object -Unique) -join ', '))
    }
    $Report.Add('进程级启动预检通过：不需要写入或恢复用户环境。')

    $GatewayBefore = Invoke-ProgressCli -Context $Context -Command 'gateway-status'
    if ($GatewayBefore.ExitCode -notin @(0, 1)) {
        throw '无法确认共享 Codex gateway 启动前状态。'
    }
    try {
        $GatewayBeforeJson = $GatewayBefore.Text | ConvertFrom-Json
    }
    catch {
        throw '共享 Codex gateway 启动前状态格式无效。'
    }
    $WebSocketUrl = Assert-LoopbackWebSocketUrl -Value ([string]$GatewayBeforeJson.websocket_url)
    $GatewayPidFileAtLaunch = Assert-LocalAbsoluteStatePath `
        -Value ([string]$GatewayBeforeJson.gateway_pid_file) -Label 'gateway PID 路径'
    $SharedDesktopStateFileAtLaunch = Assert-LocalAbsoluteStatePath `
        -Value ([string]$GatewayBeforeJson.shared_desktop_state_file) -Label 'Desktop 状态路径'
    $Uri = [Uri]::new($WebSocketUrl)
    $GatewayLaunchAttempted = ($GatewayBefore.ExitCode -eq 1)

    if ($GatewayBefore.ExitCode -eq 1) {
        $GatewayStart = Invoke-ProgressCliWithFreshUserProxyEnvironment `
            -Context $Context -Command 'gateway-start' -Arguments @(
                '--launch-token', $GatewayLaunchToken
            )
        if ($GatewayStart.ExitCode -ne 0) {
            throw '共享 Codex gateway 启动失败。'
        }
        try {
            $GatewayStartJson = $GatewayStart.Text | ConvertFrom-Json
        }
        catch {
            throw '共享 Codex gateway 启动结果格式无效。'
        }
        $GatewayStartedByThisRun = ($GatewayStartJson.started_by_request -eq $true)
        if (-not $GatewayStartedByThisRun) {
            throw '共享 Codex gateway 并非由本次启动器创建；拒绝继续或回滚他人实例。'
        }
        $GatewayOwnedPid = [int]$GatewayStartJson.pid
        $GatewayOwnedCreationTime = [long]$GatewayStartJson.creation_time
        if ($GatewayOwnedPid -le 0 -or $GatewayOwnedCreationTime -le 0) {
            throw '共享 Codex gateway 缺少可信进程世代；拒绝继续。'
        }
    }
    $GatewayStatusArguments = @()
    if ($GatewayStartedByThisRun) {
        $GatewayStatusArguments = @(
            '--expected-launch-token', $GatewayLaunchToken,
            '--expected-pid', [string]$GatewayOwnedPid,
            '--expected-creation-time', [string]$GatewayOwnedCreationTime
        )
    }
    $GatewayStatus = Invoke-ProgressCli -Context $Context -Command 'gateway-status' `
        -Arguments $GatewayStatusArguments
    if ($GatewayStatus.ExitCode -ne 0) {
        throw '共享 Codex gateway 健康检查失败。'
    }
    $GatewayJson = $GatewayStatus.Text | ConvertFrom-Json
    $ExpectedGatewayPid = [int]$GatewayJson.pid
    $ExpectedGatewayCreationTime = [long]$GatewayJson.creation_time
    if ($ExpectedGatewayPid -le 0 -or $ExpectedGatewayCreationTime -le 0) {
        throw '共享 Codex gateway 状态缺少可信进程世代。'
    }
    $StatusWebSocketUrl = Assert-LoopbackWebSocketUrl -Value ([string]$GatewayJson.websocket_url)
    if (-not $StatusWebSocketUrl.Equals($WebSocketUrl, [StringComparison]::Ordinal)) {
        throw '共享 Codex gateway URL 在启动事务中发生变化。'
    }
    $Report.Add('gateway 已准备；等待用户确认使用进程级临时环境打开 Codex。')
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    $LaunchConfirmed = Confirm-WizardAction -NoDialog:$NoDialog -Text @'
共享 gateway 已准备完成。

点击“是”后，启动器会直接运行 Codex AppX 包内的 ChatGPT.exe，并且只在这个新进程的环境副本中设置回环地址。
不会写用户环境变量，不会广播 Environment，也不会修改代理、路由或 TUN。
'@
    if (-not $LaunchConfirmed) {
        throw '用户未确认进程级启动；已取消打开 Codex。'
    }
    $ActivationNotBeforeFileTime = [DateTime]::UtcNow.ToFileTimeUtc()
    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $DesktopExecutable
    $StartInfo.WorkingDirectory = Split-Path -Parent $DesktopExecutable
    $StartInfo.UseShellExecute = $false
    # EnvironmentVariables 是当前进程环境的独立副本；这里只改新 Codex 子进程。
    $StartInfo.EnvironmentVariables['CODEX_APP_SERVER_WS_URL'] = $WebSocketUrl
    [void]$StartInfo.EnvironmentVariables.Remove($EnvironmentOwnerVariable)
    $StartedDesktop = [Diagnostics.Process]::Start($StartInfo)
    if ($null -eq $StartedDesktop) {
        throw 'Codex AppX 进程级启动没有返回进程对象。'
    }
    $Report.Add(('已按用户确认启动 Codex；正在验证共享回环连接，最长 ' +
        [string]$DesktopStartWaitSeconds + ' 秒。'))
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    $Deadline = [DateTimeOffset]::UtcNow.AddSeconds($DesktopStartWaitSeconds)
    while ([DateTimeOffset]::UtcNow -lt $Deadline) {
        Start-Sleep -Milliseconds 250
        $Processes = Get-CodexDesktopProcesses -InstallLocation $Package.InstallLocation
        $PackagePids = @($Processes | ForEach-Object { [int]$_.ProcessId })
        if ($PackagePids.Count -eq 0) { continue }
        $Connections = @(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
            Where-Object {
                $_.RemoteAddress -eq '127.0.0.1' -and
                [int]$_.RemotePort -eq $Uri.Port -and
                $PackagePids -contains [int]$_.OwningProcess
            })
        if ($Connections.Count -gt 0) {
            $ConnectedPid = [int]$Connections[0].OwningProcess
            $DesktopConnectionObserved = $true
            break
        }
    }
    if ($null -eq $ConnectedPid) {
        throw '进程级启动的 Codex 未在时限内建立共享回环连接；请正常退出 Desktop 后查看报告。'
    }

    $RegisterArguments = @(
        '--pid', [string]$ConnectedPid,
        '--install-location', [string]$Package.InstallLocation,
        '--not-before-filetime', [string]$ActivationNotBeforeFileTime,
        '--expected-gateway-pid', [string]$ExpectedGatewayPid,
        '--expected-gateway-creation-time', [string]$ExpectedGatewayCreationTime
    )
    if ($GatewayStartedByThisRun) {
        $RegisterArguments += @('--expected-gateway-launch-token', $GatewayLaunchToken)
    }
    $Register = Invoke-ProgressCli -Context $Context -Command 'register-shared-desktop' `
        -Arguments $RegisterArguments
    foreach ($Line in $Register.Lines) { $Report.Add($Line) }
    if ($Register.ExitCode -ne 0) {
        throw 'Desktop 已连接，但共享状态登记失败；请勿启动通知服务。'
    }
    $FinalArguments = @(
        '--expected-pid', [string]$ExpectedGatewayPid,
        '--expected-creation-time', [string]$ExpectedGatewayCreationTime
    )
    if ($GatewayStartedByThisRun) {
        $FinalArguments += @('--expected-launch-token', $GatewayLaunchToken)
    }
    $Final = Invoke-ProgressCli -Context $Context -Command 'gateway-status' `
        -Arguments $FinalArguments
    if ($Final.ExitCode -ne 0) { throw '共享 Codex 最终健康检查失败。' }
    $FinalJson = $Final.Text | ConvertFrom-Json
    if ($FinalJson.desktop_shared -ne $true) {
        throw '最终状态未确认 desktop_shared=true。'
    }
    $Report.Add('')
    $Report.Add('[结果]')
    $Report.Add('共享 Codex 已就绪；进度通知服务保持原状态，也未发送消息。')
    if ((Get-ProxyStateFingerprint) -eq $ProxyFingerprintBefore) {
        $Report.Add('普通代理配置与启动前完全一致；本流程未写用户环境或广播 Environment。')
    }
    else {
        $Report.Add('运行期间代理客户端自行更新了状态；本流程未写代理或用户环境，也未广播 Environment。')
    }
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Show-WizardMessage -NoDialog:$NoDialog -Title '进度通知 - 共享 Codex' -Text (
        '共享 Codex 已就绪。进度通知服务保持原状态。' + "`r`n`r`n报告：" + $ReportPath
    )
    Open-WizardReport -Path $ReportPath -NoOpenReport:$NoOpenReport
    exit 0
}
catch {
    if ($GatewayLaunchAttempted -and -not $DesktopConnectionObserved) {
        try {
            [void](Invoke-GatewayRecoveryV4 -LaunchToken $GatewayLaunchToken `
                -GatewayPidFile $GatewayPidFileAtLaunch `
                -SharedDesktopStateFile $SharedDesktopStateFileAtLaunch `
                -WebSocketUrl $WebSocketUrl)
            $GatewayRecoveryResolved = $true
            $Report.Add('[失败回滚 gateway]')
            $Report.Add('本代待启动授权已撤销，或精确 gateway 世代已确认退出。')
        }
        catch {
            $Report.Add('gateway 回滚状态未知；已保留 gateway 状态，拒绝开始替代世代。')
        }
    }
    elseif ($DesktopConnectionObserved) {
        $Report.Add('已观察到 Desktop 连接；为避免切断仍在线客户端，没有自动停止 gateway。')
    }
    if (-not [String]::IsNullOrWhiteSpace($ProxyFingerprintBefore)) {
        try {
            if ((Get-ProxyStateFingerprint) -eq $ProxyFingerprintBefore) {
                $Report.Add('失败前后普通代理配置完全一致；本工具未改代理、端口、路由或 TUN。')
            }
            else {
                $Report.Add('检测到普通代理状态发生外部变化；本工具未回写代理值，已保留现场。')
            }
        }
        catch {
            $Report.Add('失败收尾时无法复核普通代理指纹；本工具未尝试覆盖任何代理设置。')
        }
    }
    $Report.Add('')
    $Report.Add('[未完成]')
    $Report.Add($_.Exception.Message)
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    Show-WizardMessage -NoDialog:$NoDialog -Icon Error -Title '进度通知 - 共享 Codex 未完成' -Text (
        $_.Exception.Message + "`r`n`r`n未强制关闭任何程序。报告：" + $ReportPath
    )
    Open-WizardReport -Path $ReportPath -NoOpenReport:$NoOpenReport
    exit 2
}
finally {
    if ($null -ne $EnvironmentMutex) {
        try {
            if ($GatewayLaunchAttempted -and -not $DesktopConnectionObserved -and
                -not $GatewayRecoveryResolved) {
                [void](Invoke-GatewayRecoveryV4 -LaunchToken $GatewayLaunchToken `
                    -GatewayPidFile $GatewayPidFileAtLaunch `
                    -SharedDesktopStateFile $SharedDesktopStateFileAtLaunch `
                    -WebSocketUrl $WebSocketUrl)
            }
        }
        catch {
            Write-Warning ('gateway 异常回收未完成，请保留状态供人工检查：' + $_.Exception.Message)
        }
        finally {
            Exit-CodexEnvironmentMutex -Mutex $EnvironmentMutex
            $EnvironmentMutex = $null
        }
    }
}
