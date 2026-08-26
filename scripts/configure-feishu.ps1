[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$WebhookUrl,
    [string]$SigningSecret,
    [switch]$NoSigning,
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

function ConvertFrom-SecureInput {
    param([Parameter(Mandatory = $true)][securestring]$Value)

    $pointer = [System.IntPtr]::Zero
    try {
        $pointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        if ($pointer -ne [System.IntPtr]::Zero) {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
}

$resolvedWebhookUrl = $WebhookUrl
if (-not $resolvedWebhookUrl) {
    $secureUrl = Read-Host '请粘贴飞书自定义机器人的 Webhook URL（输入不会回显）' -AsSecureString
    $resolvedWebhookUrl = ConvertFrom-SecureInput $secureUrl
}
$resolvedWebhookUrl = $resolvedWebhookUrl.Trim()
$endpoint = $null
if (-not [System.Uri]::TryCreate(
    $resolvedWebhookUrl,
    [System.UriKind]::Absolute,
    [ref]$endpoint
)) {
    throw '飞书 Webhook 必须是完整 URL。'
}
if ($endpoint.Scheme -ne 'https') {
    throw '飞书 Webhook 必须使用 HTTPS。'
}
if ($endpoint.Host -notin @('open.feishu.cn', 'open.larksuite.com')) {
    throw 'Webhook 主机必须是 open.feishu.cn 或 open.larksuite.com。'
}
if ($endpoint.UserInfo -or $endpoint.Fragment) {
    throw 'Webhook URL 不得包含嵌入式凭据或 fragment。'
}
if (-not $endpoint.AbsolutePath.StartsWith(
    '/open-apis/bot/v2/hook/',
    [System.StringComparison]::Ordinal
)) {
    throw 'Webhook 路径不是飞书自定义机器人地址。'
}
$hookId = $endpoint.AbsolutePath.Substring('/open-apis/bot/v2/hook/'.Length)
if (-not $hookId -or $hookId.Contains('/')) {
    throw 'Webhook 中缺少有效的机器人 ID。'
}

$resolvedSigningSecret = ''
if (-not $NoSigning) {
    $resolvedSigningSecret = $SigningSecret
    if (-not $resolvedSigningSecret) {
        $secureSecret = Read-Host '如已开启“签名校验”，请输入密钥；未开启直接回车（输入不会回显）' -AsSecureString
        $resolvedSigningSecret = ConvertFrom-SecureInput $secureSecret
    }
    $resolvedSigningSecret = $resolvedSigningSecret.Trim()
}
foreach ($value in @($resolvedWebhookUrl, $resolvedSigningSecret)) {
    if (@($value.ToCharArray() | Where-Object {
        [int]$_ -lt 32 -or [int]$_ -eq 127
    }).Count -gt 0) {
        throw 'Webhook 或签名密钥不得包含控制字符。'
    }
}

if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
    try {
        $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    } catch {
        throw "现有配置不是有效 JSON，未修改：$ConfigPath"
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

Set-JsonProperty $config.notification provider 'feishu'
Set-JsonProperty $config.notification webhook_url '${PROGRESS_WEBHOOK_URL}'
Set-JsonProperty $config.notification auth_type 'none'
Set-JsonProperty $config.notification bearer_token ''
Set-JsonProperty $config.notification target_umo ''
Set-JsonProperty $config.notification hmac_secret ''
Set-JsonProperty $config.notification feishu_signing_secret '${PROGRESS_FEISHU_SECRET}'
Set-JsonProperty $config.notification allow_http_localhost $false

$json = $config | ConvertTo-Json -Depth 100
$null = $json | ConvertFrom-Json
$temporaryPath = Join-Path $ConfigDirectory (
    '.progress-feishu-' + [guid]::NewGuid().ToString('N') + '.tmp'
)
$backupPath = $null
try {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        $temporaryPath,
        $json + [Environment]::NewLine,
        $utf8NoBom
    )

    $env:PROGRESS_WEBHOOK_URL = $resolvedWebhookUrl
    if ($resolvedSigningSecret) {
        $env:PROGRESS_FEISHU_SECRET = $resolvedSigningSecret
    } else {
        $env:PROGRESS_FEISHU_SECRET = $null
    }
    if (-not $NoPersistUserEnvironment) {
        [System.Environment]::SetEnvironmentVariable(
            'PROGRESS_WEBHOOK_URL',
            $resolvedWebhookUrl,
            'User'
        )
        $secretValue = if ($resolvedSigningSecret) {
            $resolvedSigningSecret
        } else {
            $null
        }
        [System.Environment]::SetEnvironmentVariable(
            'PROGRESS_FEISHU_SECRET',
            $secretValue,
            'User'
        )
    }

    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        $backupPath = $ConfigPath + '.before-feishu-' + (
            Get-Date -Format 'yyyyMMddHHmmssfff'
        ) + '.bak'
        [System.IO.File]::Replace(
            $temporaryPath,
            $ConfigPath,
            $backupPath,
            $true
        )
    } else {
        [System.IO.File]::Move($temporaryPath, $ConfigPath)
    }
} finally {
    $resolvedWebhookUrl = $null
    $resolvedSigningSecret = $null
    $WebhookUrl = $null
    $SigningSecret = $null
    if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Host "飞书通知配置已更新：$ConfigPath"
if ($backupPath) {
    Write-Host "原配置备份：$backupPath"
}
if ($NoPersistUserEnvironment) {
    Write-Host 'Webhook 与可选签名密钥仅设置于当前 PowerShell 进程。'
} else {
    Write-Host 'Webhook 与可选签名密钥已保存到 Windows User 环境变量（值未显示）。'
}
Write-Host '请运行 scripts\validate.ps1 和 scripts\send-test.ps1；成功后完全退出并重启 Codex。' -ForegroundColor Yellow
