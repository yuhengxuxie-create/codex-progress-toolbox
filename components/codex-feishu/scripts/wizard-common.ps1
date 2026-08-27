Set-StrictMode -Version Latest

function Get-WizardContext {
    [CmdletBinding()]
    param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

    $ResolvedToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
    if ([Uri]::new($ResolvedToolsRoot).IsUnc) {
        throw 'ToolsRoot 必须是本机路径，拒绝从 UNC/网络位置加载程序。'
    }
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    if ([Uri]::new($ProjectRoot).IsUnc) {
        throw '项目必须位于本机路径，拒绝从 UNC/网络位置执行向导。'
    }
    $PythonExe = Join-Path $ResolvedToolsRoot 'Python313-ProgressWX\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw '项目隔离 Python 不存在，请先运行 scripts\install.ps1。'
    }
    $ResolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
    $RootPrefix = $ResolvedToolsRoot.TrimEnd('\') + '\'
    if (-not $ResolvedPython.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Python 解析路径越出 ToolsRoot，已拒绝执行。'
    }
    $EntryPoint = Join-Path $ProjectRoot 'progress-wx.py'
    $ConfigPath = Join-Path $ProjectRoot 'config.yaml'
    if (-not (Test-Path -LiteralPath $EntryPoint -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw '项目入口或 config.yaml 不存在。'
    }
    $StateDir = Join-Path $ProjectRoot '.state'
    if (-not (Test-Path -LiteralPath $StateDir -PathType Container)) {
        New-Item -ItemType Directory -Path $StateDir | Out-Null
    }
    return [pscustomobject]@{
        ToolsRoot = $ResolvedToolsRoot
        ProjectRoot = $ProjectRoot
        PythonExe = $ResolvedPython
        EntryPoint = $EntryPoint
        ConfigPath = $ConfigPath
        StateDir = $StateDir
    }
}

function Invoke-ProgressCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Context,
        [Parameter(Mandatory)][string]$Command,
        [string[]]$Arguments = @()
    )

    # Python CLI 固定输出 UTF-8。Windows PowerShell 5 会按当前控制台代码页解码
    # 原生程序输出，因此只在本次调用期间切换解码方式，并在 finally 中原样恢复。
    $OriginalConsoleOutputEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        # 捕获输出只用于本机报告；命令通过数组传参，不拼接 shell 字符串。
        $Output = @(
            & $Context.PythonExe $Context.EntryPoint --config $Context.ConfigPath `
                $Command @Arguments 2>&1
        )
        $ExitCode = $LASTEXITCODE
    }
    finally {
        [Console]::OutputEncoding = $OriginalConsoleOutputEncoding
    }
    $Lines = @($Output | ForEach-Object { [string]$_ })
    return [pscustomobject]@{
        ExitCode = $ExitCode
        Lines = $Lines
        Text = ($Lines -join "`n")
    }
}

function Invoke-WizardPowerShellScript {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [string[]]$Arguments = @()
    )

    # 复用当前已经启动的 PowerShell（Windows PowerShell 或 PowerShell 7），
    # 避免假定 $PSHOME 中一定存在旧版 powershell.exe。
    $PowerShellExe = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    if (-not (Test-Path -LiteralPath $PowerShellExe -PathType Leaf)) {
        throw '无法定位当前 PowerShell 可执行文件。'
    }
    $Output = @(
        & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments 2>&1
    )
    $ExitCode = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $ExitCode
        Lines = @($Output | ForEach-Object { [string]$_ })
        Text = (@($Output | ForEach-Object { [string]$_ }) -join "`n")
    }
}

function Get-WizardConfigHash {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Context)

    return (Get-FileHash -LiteralPath $Context.ConfigPath -Algorithm SHA256).Hash
}

function Get-BeijingTimestamp {
    [CmdletBinding()]
    param()

    return [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
        [DateTimeOffset]::UtcNow,
        'China Standard Time'
    ).ToString('yyyy-MM-dd HH:mm:ss')
}

function Write-WizardJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Value
    )

    $Value | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Read-WizardJson {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw ('向导状态文件损坏，拒绝继续：' + $Path)
    }
}

function Test-WizardMarker {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowNull()]$Marker,
        [Parameter(Mandatory)][string]$ConfigHash,
        [Parameter(Mandatory)][int]$MaxAgeSeconds
    )

    if ($null -eq $Marker -or $Marker.config_sha256 -ne $ConfigHash) {
        return $false
    }
    try {
        $Created = [DateTimeOffset]::Parse(
            [string]$Marker.created_utc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    catch {
        return $false
    }
    $Age = [DateTimeOffset]::UtcNow - $Created.ToUniversalTime()
    return ($Age.TotalSeconds -ge 0 -and $Age.TotalSeconds -le $MaxAgeSeconds)
}

function Show-WizardMessage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Text,
        [string]$Title = '进度通知',
        [switch]$NoDialog,
        [ValidateSet('Information', 'Warning', 'Error')][string]$Icon = 'Information'
    )

    Write-Host $Text
    if ($NoDialog) { return }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $MessageIcon = [System.Enum]::Parse(
            [System.Windows.Forms.MessageBoxIcon],
            $Icon
        )
        [void][System.Windows.Forms.MessageBox]::Show(
            $Text,
            $Title,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            $MessageIcon
        )
    }
    catch {
        Write-Warning '无法显示图形提示；请查看当前控制台和本机报告。'
    }
}

function Confirm-WizardAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Text,
        [string]$Title = '进度通知 - 请确认',
        [switch]$NoDialog
    )

    # 非交互模式永远拒绝有外部影响的动作，不能用参数绕过人工确认。
    if ($NoDialog) { return $false }
    Add-Type -AssemblyName System.Windows.Forms
    $Result = [System.Windows.Forms.MessageBox]::Show(
        $Text,
        $Title,
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button2
    )
    return $Result -eq [System.Windows.Forms.DialogResult]::Yes
}

function New-ProjectDesktopShortcut {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Context,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$TargetName,
        [Parameter(Mandatory)][string]$Description
    )

    $Target = [IO.Path]::GetFullPath((Join-Path $Context.ProjectRoot $TargetName))
    $ProjectPrefix = $Context.ProjectRoot.TrimEnd('\') + '\'
    if (-not $Target.StartsWith($ProjectPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $Target -PathType Leaf)) {
        throw '阶段快捷方式目标越出项目或不存在，已拒绝创建。'
    }
    $Desktop = [Environment]::GetFolderPath('Desktop')
    if ([String]::IsNullOrWhiteSpace($Desktop)) {
        throw '无法定位当前用户桌面目录。'
    }
    $ShortcutPath = Join-Path $Desktop $Name
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Target
    $Shortcut.WorkingDirectory = $Context.ProjectRoot
    $Shortcut.Description = $Description
    $Shortcut.WindowStyle = 1
    $Shortcut.Save()
    return $ShortcutPath
}

function Open-WizardReport {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$NoOpenReport
    )

    if ($NoOpenReport) { return }
    Start-Process -FilePath 'notepad.exe' -ArgumentList @($Path) | Out-Null
}
