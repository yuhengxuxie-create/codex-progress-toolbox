[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$TimeoutSeconds = 60,
    [switch]$DiagnosticUnverifiedIdentity
)

$ErrorActionPreference = 'Stop'
$ResolvedToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
if ([Uri]::new($ResolvedToolsRoot).IsUnc) {
    throw 'ToolsRoot 必须是本机路径，拒绝从 UNC/网络位置加载 Python。'
}
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ResolvedToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '项目隔离 Python 不存在，请先运行 scripts\install.ps1。'
}
$ResolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$RootPrefix = $ResolvedToolsRoot.TrimEnd('\') + '\'
if (-not $ResolvedPython.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Python 解析路径越出 ToolsRoot，已拒绝执行。'
}

$StateDir = Join-Path $ProjectRoot '.state'
if (-not (Test-Path -LiteralPath $StateDir -PathType Container)) {
    New-Item -ItemType Directory -Path $StateDir | Out-Null
}
$Token = [Guid]::NewGuid().ToString('N')
$StdOutPath = Join-Path $StateDir ('.probe-' + $Token + '.out')
$StdErrPath = Join-Path $StateDir ('.probe-' + $Token + '.err')
$ReportPath = Join-Path $StateDir 'last-safe-wechat-check.txt'
$ModeLabel = if ($DiagnosticUnverifiedIdentity) {
    '单账号只读结构诊断（身份未验证）'
} else {
    '严格账号身份安全检测'
}
$Arguments = @(
    ('"' + (Join-Path $ProjectRoot 'progress-wx.py') + '"'),
    '--config',
    ('"' + (Join-Path $ProjectRoot 'config.yaml') + '"'),
    'probe-free-wechat',
    '--single-visible-window'
)
if ($DiagnosticUnverifiedIdentity) {
    $Arguments += '--diagnostic-unverified-identity'
}

try {
    Write-Host ('正在执行' + $ModeLabel + '：不会点击、聚焦、读取聊天或发送消息……')
    $Process = Start-Process -FilePath $ResolvedPython -ArgumentList $Arguments `
        -PassThru -NoNewWindow -RedirectStandardOutput $StdOutPath `
        -RedirectStandardError $StdErrPath
    if (-not $Process.WaitForExit([Math]::Max(10, $TimeoutSeconds) * 1000)) {
        $Process.Kill()
        $Process.WaitForExit()
        throw '只读探针超过时限，已只终止本次探针进程；微信未被操作。'
    }
    $StandardOutput = @()
    $StandardError = @()
    if (Test-Path -LiteralPath $StdOutPath) {
        $StandardOutput += Get-Content -LiteralPath $StdOutPath -Encoding UTF8
    }
    if (Test-Path -LiteralPath $StdErrPath) {
        $StandardError += Get-Content -LiteralPath $StdErrPath -Encoding UTF8
    }
    $Output = $StandardOutput + $StandardError
    $Conclusion = @()
    if ($Process.ExitCode -eq 0) {
        try {
            $ProbeResult = ($StandardOutput -join "`n") | ConvertFrom-Json
        }
        catch {
            throw '探针成功退出但没有返回有效 JSON，已拒绝信任结果。'
        }
        if ($ProbeResult.production_ready -ne $false -or
            $ProbeResult.quote_reply_verified -ne $false) {
            throw '结构探针返回了越权的生产就绪标记，已拒绝信任结果。'
        }
        $CapabilityCount = @(
            $ProbeResult.capabilities.PSObject.Properties |
                Where-Object { $_.Value -eq $true }
        ).Count
        if ($CapabilityCount -eq 0) {
            $Conclusion += '结论：当前微信未公开所需 UIA 结构，免费 UIA 生产路线不可用。'
        } else {
            $Conclusion += ('结论：发现 ' + $CapabilityCount + ' 项基础结构；仍需单独验证账号、联系人和引用回复。')
        }
        if ($ProbeResult.account_nickname_verified -ne $true) {
            $Conclusion += '身份：未机器验证；本次结果只能用于单账号诊断。'
        }
        $Conclusion += '安全状态：后台服务保持禁用，未发送任何微信消息。'
    }
    $Report = @(
        ('进度通知 - ' + $ModeLabel),
        ('北京时间：' + [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
            [DateTimeOffset]::UtcNow,
            'China Standard Time'
        ).ToString('yyyy-MM-dd HH:mm:ss')),
        ('退出码：' + $Process.ExitCode),
        ''
    ) + $Output + @('') + $Conclusion
    $Report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    $Report | ForEach-Object { Write-Host $_ }
    Write-Host ('结果已保存：' + $ReportPath)
    if ($Process.ExitCode -ne 0) {
        throw '只读安全检测未通过；后台服务仍保持禁用，未发送任何消息。'
    }
}
finally {
    Remove-Item -LiteralPath $StdOutPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StdErrPath -Force -ErrorAction SilentlyContinue
}
