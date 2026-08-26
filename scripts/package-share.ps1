[CmdletBinding()]
param([string]$DestinationDirectory)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$OutputRoot = if ($DestinationDirectory) {
    [System.IO.Path]::GetFullPath($DestinationDirectory)
} else {
    Split-Path -Parent $ProjectRoot
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ZipPath = Join-Path $OutputRoot "codex-progress-toolbox-$stamp.zip"
$ChecksumPath = Join-Path $OutputRoot 'SHA256SUMS.txt'
if (Test-Path -LiteralPath $ZipPath) {
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $ZipPath = Join-Path $OutputRoot "codex-progress-toolbox-$stamp-$suffix.zip"
}

$TempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$StageRoot = Join-Path $TempBase ("progress-share-stage-" + [guid]::NewGuid().ToString('N'))
$VerifyRoot = Join-Path $TempBase ("progress-share-verify-" + [guid]::NewGuid().ToString('N'))
$StageProject = Join-Path $StageRoot 'codex-progress-toolbox'
$PackageCreated = $false

function Assert-SafeTempPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $prefix = $TempBase.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
        [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw '临时目录超出系统临时路径，已拒绝继续。'
    }
}

function Add-PrivateValue {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.HashSet[string]]$Values,
        [AllowNull()][object]$Value
    )

    if ($Value -isnot [string]) { return }
    $candidate = $Value.Trim()
    if ($candidate.Length -lt 3 -or $candidate.Contains('${')) { return }
    [void]$Values.Add($candidate)
    foreach ($match in [regex]::Matches(
        $candidate,
        '(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])'
    )) {
        $address = $match.Value
        if (
            $address -eq '0.0.0.0' -or
            $address.StartsWith('127.') -or
            $address.StartsWith('192.0.2.') -or
            $address.StartsWith('198.51.100.') -or
            $address.StartsWith('203.0.113.')
        ) {
            continue
        }
        [void]$Values.Add($address)
    }
}

function Add-PrivateConfigValues {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [System.Collections.Generic.HashSet[string]]$Values,
        [Parameter(Mandatory = $true)][string]$Path
    )

    try {
        $data = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return
    }
    $threadIdsProperty = $data.PSObject.Properties['thread_ids']
    if ($threadIdsProperty) {
        foreach ($value in @($threadIdsProperty.Value)) {
            Add-PrivateValue $Values $value
        }
    }
    $notificationProperty = $data.PSObject.Properties['notification']
    if ($notificationProperty -and $notificationProperty.Value) {
        $notification = $notificationProperty.Value
        foreach ($name in @('webhook_url', 'bearer_token', 'target_umo', 'hmac_secret')) {
            $property = $notification.PSObject.Properties[$name]
            if ($property) { Add-PrivateValue $Values $property.Value }
        }
        $feishuProperty = $notification.PSObject.Properties['feishu_signing_secret']
        if ($feishuProperty) { Add-PrivateValue $Values $feishuProperty.Value }
    }
    $classifierProperty = $data.PSObject.Properties['classifier']
    if ($classifierProperty -and $classifierProperty.Value) {
        $classifier = $classifierProperty.Value
        $apiKeyProperty = $classifier.PSObject.Properties['api_key']
        if ($apiKeyProperty) { Add-PrivateValue $Values $apiKeyProperty.Value }
        $baseUrlProperty = $classifier.PSObject.Properties['base_url']
        $baseUrl = if ($baseUrlProperty) { [string]$baseUrlProperty.Value } else { '' }
        if ($baseUrl -and $baseUrl -notmatch '^https://(api\.openai\.com|api\.deepseek\.com)(/|$)') {
            Add-PrivateValue $Values $baseUrl
        }
    }
    $logFileProperty = $data.PSObject.Properties['log_file']
    $logFile = if ($logFileProperty) { [string]$logFileProperty.Value } else { '' }
    if ([System.IO.Path]::IsPathRooted($logFile)) {
        Add-PrivateValue $Values $logFile
    }
    $codexProperty = $data.PSObject.Properties['codex']
    if ($codexProperty -and $codexProperty.Value) {
        $codex = $codexProperty.Value
        $overridesProperty = $codex.PSObject.Properties['title_overrides']
        if ($overridesProperty -and $overridesProperty.Value) {
            foreach ($property in $overridesProperty.Value.PSObject.Properties) {
                Add-PrivateValue $Values $property.Name
                Add-PrivateValue $Values $property.Value
            }
        }
        $codexCommandProperty = $codex.PSObject.Properties['command']
        $codexCommand = if ($codexCommandProperty) {
            [string]$codexCommandProperty.Value
        } else {
            ''
        }
        if ([System.IO.Path]::IsPathRooted($codexCommand)) {
            Add-PrivateValue $Values $codexCommand
        }
    }
}

function Assert-ShareTreeSafe {
    param([Parameter(Mandatory = $true)][string]$Root)

    $forbidden = Get-ChildItem -LiteralPath $Root -Recurse -Force | Where-Object {
        $_.Name -eq 'config.local.json' -or
        $_.Name -like 'config.local.json.*.bak' -or
        $_.Name -eq '.state' -or
        $_.Name -eq '__pycache__' -or
        $_.Name -eq '.pytest_cache' -or
        $_.Extension -in @('.pyc', '.pyo', '.log', '.bak', '.zip')
    }
    if ($forbidden) { throw '分享目录混入了本地配置、日志、缓存或备份。' }

    $privateValues = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    Get-ChildItem -LiteralPath $ProjectRoot -File -Filter 'config.local.json*' |
        ForEach-Object { Add-PrivateConfigValues $privateValues $_.FullName }

    foreach ($name in @(
        'PROGRESS_THREAD_IDS',
        'PROGRESS_WEBHOOK_URL',
        'PROGRESS_WEBHOOK_BEARER_TOKEN',
        'PROGRESS_WEBHOOK_HMAC_SECRET',
        'PROGRESS_FEISHU_SECRET',
        'PROGRESS_ASTRBOT_TARGET_UMO',
        'ASTRBOT_API_KEY',
        'OPENAI_API_KEY',
        'DEEPSEEK_API_KEY',
        'PROGRESS_THREAD_TITLES_JSON'
    )) {
        foreach ($scope in @('Process', 'User', 'Machine')) {
            $raw = [Environment]::GetEnvironmentVariable($name, $scope)
            Add-PrivateValue $privateValues $raw
            if ($name -eq 'PROGRESS_THREAD_IDS' -and $raw) {
                foreach ($value in $raw.Split(',')) {
                    Add-PrivateValue $privateValues $value
                }
            }
            if ($name -eq 'PROGRESS_THREAD_TITLES_JSON' -and $raw) {
                try {
                    $titles = $raw | ConvertFrom-Json
                    foreach ($property in $titles.PSObject.Properties) {
                        Add-PrivateValue $privateValues $property.Name
                        Add-PrivateValue $privateValues $property.Value
                    }
                } catch {}
            }
        }
    }
    Add-PrivateValue $privateValues ([Environment]::GetFolderPath('UserProfile'))
    Add-PrivateValue $privateValues (
        [Environment]::GetFolderPath('UserProfile').Replace('\', '/')
    )

    $uuidPattern = '(?i)\b0[0-9a-f]{7}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
    $unsafeFiles = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File) {
        try {
            $text = [System.IO.File]::ReadAllText($file.FullName)
        } catch {
            continue
        }
        if ([regex]::IsMatch($text, $uuidPattern)) {
            [void]$unsafeFiles.Add($file.FullName)
            continue
        }
        foreach ($value in $privateValues) {
            if ($text.Contains($value, [System.StringComparison]::Ordinal)) {
                [void]$unsafeFiles.Add($file.FullName)
                break
            }
        }
    }
    if ($unsafeFiles.Count -gt 0) {
        $relativeUnsafe = @(
            $unsafeFiles | ForEach-Object {
                [System.IO.Path]::GetRelativePath($Root, $_)
            } | Sort-Object
        )
        throw (
            "隐私扫描发现 $($unsafeFiles.Count) 个文件包含本机私密值或真实线程 ID：" +
            ($relativeUnsafe -join ', ')
        )
    }
}

function Find-CompatiblePython {
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($env:PROGRESS_PYTHON) { $candidates.Add($env:PROGRESS_PYTHON) }
    $toolsRoot = if ($env:PROGRESS_TOOLS_ROOT) {
        $env:PROGRESS_TOOLS_ROOT
    } else {
        Join-Path $env:LOCALAPPDATA 'CodexProgressToolbox'
    }
    $candidates.Add((Join-Path $toolsRoot 'Python\python.exe'))
    try { $candidates.Add((Get-Command python.exe -ErrorAction Stop).Source) } catch {}
    try {
        $launcher = Get-Command py.exe -ErrorAction Stop
        $resolved = @(& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved.Count -gt 0) {
            $candidates.Add([string]$resolved[-1])
        }
    } catch {}
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -eq 0) { return (Resolve-Path -LiteralPath $candidate).Path }
        } catch {}
    }
    return $null
}

try {
    Assert-SafeTempPath $StageRoot
    Assert-SafeTempPath $VerifyRoot
    New-Item -ItemType Directory -Path $StageProject -Force | Out-Null

    $files = [System.Collections.Generic.List[System.IO.FileInfo]]::new()
    foreach ($name in @(
        '.gitignore',
        'AGENTS.md',
        'CHANGELOG.md',
        'CONTRIBUTING.md',
        'INSTALL_WITH_CODEX.md',
        'LICENSE',
        'README.md',
        'SECURITY.md',
        'pyproject.toml',
        'progress-notify.py',
        'manage-threads.pyw',
        'config.example.json'
    )) {
        $files.Add((Get-Item -LiteralPath (Join-Path $ProjectRoot $name)))
    }
    foreach ($directory in @('docs', 'scripts', 'src', 'tests')) {
        Get-ChildItem -LiteralPath (Join-Path $ProjectRoot $directory) -Recurse -File |
            Where-Object { $_.Extension -in @('.py', '.ps1', '.md', '.json') } |
            ForEach-Object { $files.Add($_) }
    }
    $files.Add((Get-Item -LiteralPath (Join-Path $ProjectRoot 'scripts\manage-threads.cmd')))

    foreach ($file in $files) {
        $relative = [System.IO.Path]::GetRelativePath($ProjectRoot, $file.FullName)
        $destination = Join-Path $StageProject $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force |
            Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }

    Assert-ShareTreeSafe $StageProject
    Compress-Archive -LiteralPath $StageProject -DestinationPath $ZipPath -CompressionLevel Optimal
    $PackageCreated = $true

    New-Item -ItemType Directory -Path $VerifyRoot -Force | Out-Null
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $VerifyRoot
    $verifiedProject = Join-Path $VerifyRoot 'codex-progress-toolbox'
    if (-not (Test-Path -LiteralPath $verifiedProject -PathType Container)) {
        throw 'ZIP 根目录结构无效。'
    }
    Assert-ShareTreeSafe $verifiedProject
    $count = (Get-ChildItem -LiteralPath $verifiedProject -Recurse -File).Count

    $python = Find-CompatiblePython
    if ($python) {
        Push-Location $verifiedProject
        try {
            & $python -m unittest discover -s tests -q
            if ($LASTEXITCODE -ne 0) { throw '分享包内自动化测试失败。' }
        } finally {
            Pop-Location
        }
    } else {
        Write-Warning '未找到 Python >= 3.11，已跳过分享包测试。'
    }

    $hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumLine = "$hash *$([System.IO.Path]::GetFileName($ZipPath))"
    [System.IO.File]::WriteAllText(
        $ChecksumPath,
        $checksumLine + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "安全分享包：$ZipPath"
    Write-Output "校验文件：$ChecksumPath"
    Write-Output "文件数量：$count"
    Write-Output "SHA-256：$hash"
} catch {
    if ($PackageCreated -and (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        $resolvedZip = [System.IO.Path]::GetFullPath($ZipPath)
        $outputPrefix = $OutputRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if ($resolvedZip.StartsWith(
            $outputPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            Remove-Item -LiteralPath $resolvedZip -Force
        }
    }
    throw
} finally {
    foreach ($path in @($StageRoot, $VerifyRoot)) {
        Assert-SafeTempPath $path
        if (Test-Path -LiteralPath $path) {
            Get-ChildItem -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue |
                ForEach-Object { $_.Attributes = [System.IO.FileAttributes]::Normal }
            [System.IO.Directory]::Delete($path, $true)
        }
    }
}
