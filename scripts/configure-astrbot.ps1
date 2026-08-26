[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$BaseUrl = 'http://127.0.0.1:6185/api/v1/im/message',
    [string]$Umo,
    [string]$ApiKey,
    [switch]$NoPersistUserEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectRoot 'config.local.json'
}
$ConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
$ConfigDirectory = Split-Path -Parent $ConfigPath
if (-not (Test-Path -LiteralPath $ConfigDirectory -PathType Container)) {
    throw "配置文件所在目录不存在：$ConfigDirectory"
}

$endpoint = $null
if (-not [System.Uri]::TryCreate($BaseUrl, [System.UriKind]::Absolute, [ref]$endpoint)) {
    throw '-BaseUrl 必须是完整的 HTTP(S) URL。'
}
if ($endpoint.Scheme -notin @('http', 'https')) {
    throw '-BaseUrl 只支持 http 或 https。'
}
if ($endpoint.UserInfo -or $endpoint.Fragment) {
    throw '-BaseUrl 不得包含嵌入式凭据或 URL fragment。'
}
if ($endpoint.Scheme -eq 'http' -and $endpoint.Host -notin @('127.0.0.1', 'localhost', '::1')) {
    throw '非本机 AstrBot 必须使用 HTTPS。'
}

if (-not $Umo) {
    $Umo = Read-Host '请粘贴目标私聊中 /sid 返回的完整 UMO'
}
$Umo = $Umo.Trim()
if (-not $Umo -or $Umo.Contains("`r") -or $Umo.Contains("`n")) {
    throw 'UMO 不能为空或包含换行。'
}
if ($Umo.StartsWith('「') -and $Umo.EndsWith('」')) {
    $Umo = $Umo.Substring(1, $Umo.Length - 2).Trim()
}
if ($Umo.Contains('「') -or $Umo.Contains('」')) {
    throw 'UMO 包含不成对的 AstrBot 展示书名号。'
}
if (@($Umo.ToCharArray() | Where-Object { [int]$_ -lt 32 -or [int]$_ -eq 127 }).Count -gt 0) {
    throw 'UMO 不得包含控制字符。'
}
$umoParts = $Umo -split ':', 3
if ($umoParts.Count -ne 3 -or @($umoParts | Where-Object { -not $_.Trim() }).Count -gt 0) {
    throw 'UMO 格式无效；请完整复制 AstrBot /sid 的输出。'
}
if ($umoParts[1] -notin @('FriendMessage', 'GroupMessage', 'OtherMessage')) {
    throw 'UMO 消息类型无效；请完整复制 AstrBot /sid 的输出。'
}

$resolvedApiKey = $ApiKey
if (-not $resolvedApiKey) {
    $resolvedApiKey = $env:ASTRBOT_API_KEY
}
if (-not $resolvedApiKey) {
    $secureApiKey = Read-Host '请输入 AstrBot API Key（输入不会回显）' -AsSecureString
    $secretPointer = [System.IntPtr]::Zero
    try {
        $secretPointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
        $resolvedApiKey = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    } finally {
        if ($secretPointer -ne [System.IntPtr]::Zero) {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
        }
    }
}
if (-not $resolvedApiKey -or -not $resolvedApiKey.Trim()) {
    throw 'AstrBot API Key 不能为空。'
}
$resolvedApiKey = $resolvedApiKey.Trim()
if (@($resolvedApiKey.ToCharArray() | Where-Object { [int]$_ -lt 32 -or [int]$_ -eq 127 }).Count -gt 0) {
    throw 'AstrBot API Key 不得包含控制字符。'
}

if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
    try {
        $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    } catch {
        throw "现有配置文件不是有效 JSON，未修改：$ConfigPath"
    }
} else {
    $templatePath = Join-Path $ProjectRoot 'config.example.json'
    $config = Get-Content -LiteralPath $templatePath -Raw | ConvertFrom-Json
}
if ($null -eq $config -or $config -isnot [psobject]) {
    throw '配置根节点必须是 JSON 对象。'
}

if ($null -eq $config.notification) {
    $config | Add-Member -NotePropertyName notification -NotePropertyValue ([pscustomobject]@{}) -Force
}
function Set-JsonProperty {
    param(
        [Parameter(Mandatory = $true)][psobject]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()]$Value
    )
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

Set-JsonProperty -Object $config.notification -Name provider -Value 'astrbot'
Set-JsonProperty -Object $config.notification -Name webhook_url -Value $endpoint.AbsoluteUri
Set-JsonProperty -Object $config.notification -Name auth_type -Value 'bearer'
Set-JsonProperty -Object $config.notification -Name bearer_token -Value '${ASTRBOT_API_KEY}'
Set-JsonProperty -Object $config.notification -Name target_umo -Value $Umo
Set-JsonProperty -Object $config.notification -Name allow_http_localhost -Value $true

$json = $config | ConvertTo-Json -Depth 100
$null = $json | ConvertFrom-Json
$temporaryPath = Join-Path $ConfigDirectory ('.progress-astrbot-' + [guid]::NewGuid().ToString('N') + '.tmp')
$backupPath = $null
try {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, $utf8NoBom)

    # Persist the secret outside JSON. Neither branch writes its value to stdout.
    $env:ASTRBOT_API_KEY = $resolvedApiKey
    if (-not $NoPersistUserEnvironment) {
        [System.Environment]::SetEnvironmentVariable('ASTRBOT_API_KEY', $resolvedApiKey, 'User')
    }

    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        $backupPath = $ConfigPath + '.before-astrbot-' + (Get-Date -Format 'yyyyMMddHHmmssfff') + '.bak'
        [System.IO.File]::Replace($temporaryPath, $ConfigPath, $backupPath, $true)
    } else {
        [System.IO.File]::Move($temporaryPath, $ConfigPath)
    }
} finally {
    $resolvedApiKey = $null
    $ApiKey = $null
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Host "AstrBot 通知配置已更新：$ConfigPath"
if ($backupPath) {
    Write-Host "原配置备份：$backupPath"
}
if ($NoPersistUserEnvironment) {
    Write-Host 'API Key 仅设置于当前 PowerShell 进程（密钥未显示）。'
} else {
    Write-Host 'API Key 已写入 Windows User 级 ASTRBOT_API_KEY（密钥未显示）。'
}
Write-Host '请完全退出并重启 Codex 桌面应用，再运行 scripts\validate.ps1 和 scripts\send-test.ps1。' -ForegroundColor Yellow
