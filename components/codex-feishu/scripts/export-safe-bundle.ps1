[CmdletBinding()]
param(
    [string]$OutputRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'exports')
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$OutputFull = [System.IO.Path]::GetFullPath($OutputRoot)
$ProjectFull = [System.IO.Path]::GetFullPath($ProjectRoot)
$Comparison = [System.StringComparison]::OrdinalIgnoreCase

# 分享包只允许写入项目内固定的 exports 子目录；源码白名单不会遍历该目录。
$AllowedOutputRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectFull 'exports'))
if (-not ($OutputFull.Equals($AllowedOutputRoot, $Comparison) -or
    $OutputFull.StartsWith(($AllowedOutputRoot + [System.IO.Path]::DirectorySeparatorChar), $Comparison))) {
    throw "分享包输出目录必须位于：$AllowedOutputRoot"
}

New-Item -ItemType Directory -Force -Path $OutputFull | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$StagingRoot = Join-Path $OutputFull ('.staging-' + [Guid]::NewGuid().ToString('N'))
$BundleRoot = Join-Path $StagingRoot 'ProgressChecking(WX)'
$ZipPath = Join-Path $OutputFull ("ProgressCheckingWX-Source-$Stamp.zip")

function Test-SafeRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $Segments = $RelativePath -split '[\\/]'
    foreach ($Segment in $Segments) {
        if ($Segment -in @('__pycache__', '.pytest_cache', '.secrets', '.state', 'logs') -or
            $Segment -like '*.egg-info') {
            return $false
        }
    }
    $Leaf = [System.IO.Path]::GetFileName($RelativePath)
    if ($Leaf -in @('config.yaml') -or $Leaf -like '*.pyc' -or $Leaf -like '*.pyo' -or
        $Leaf -like '*.dpapi' -or $Leaf -like '*.key' -or $Leaf -like '*.db' -or
        $Leaf -like '*.sqlite' -or $Leaf -like '*.sqlite3' -or $Leaf -like '*.log') {
        return $false
    }
    return $true
}

function Copy-SafeFile {
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$File)

    if (($File.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "拒绝打包重解析点：$($File.FullName)"
    }
    $Relative = $File.FullName.Substring($ProjectFull.Length).TrimStart([char[]]@('\', '/'))
    if (-not (Test-SafeRelativePath -RelativePath $Relative)) {
        return
    }
    $Destination = Join-Path $BundleRoot $Relative
    $DestinationDirectory = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
    Copy-Item -LiteralPath $File.FullName -Destination $Destination
}

try {
    New-Item -ItemType Directory -Force -Path $BundleRoot | Out-Null

    # 顶层只允许明确列出的源码与分发文件，不使用“先全拷再删除”。
    $TopLevelNames = @(
        '.gitignore', 'LICENSE', 'README.md', 'pyproject.toml', 'config.example.yaml',
        'progress-wx.py', 'progress-wx-hook.py'
    )
    foreach ($Name in $TopLevelNames) {
        $Candidate = Get-Item -LiteralPath (Join-Path $ProjectFull $Name) -ErrorAction Stop
        Copy-SafeFile -File $Candidate
    }
    Get-ChildItem -LiteralPath $ProjectFull -File | Where-Object {
        $_.Name -like 'requirements-*.txt' -or $_.Name -like '*.cmd'
    } | ForEach-Object { Copy-SafeFile -File $_ }

    foreach ($DirectoryName in @('docs', 'scripts', 'src', 'tests')) {
        $Directory = Get-Item -LiteralPath (Join-Path $ProjectFull $DirectoryName) -ErrorAction Stop
        if (($Directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝打包重解析点目录：$($Directory.FullName)"
        }
        Get-ChildItem -LiteralPath $Directory.FullName -Recurse -File -Force |
            ForEach-Object { Copy-SafeFile -File $_ }
    }

    $Forbidden = @(Get-ChildItem -LiteralPath $BundleRoot -Recurse -File -Force | Where-Object {
        -not (Test-SafeRelativePath -RelativePath $_.FullName.Substring($BundleRoot.Length).TrimStart([char[]]@('\', '/')))
    })
    if ($Forbidden.Count -ne 0) {
        throw '分享包安全检查失败：检测到本机私有文件。'
    }

    $Manifest = @(
        '进度通知安全源码分享包',
        "生成时间：$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))",
        '已排除：config.yaml、.secrets、.state、logs、Python 缓存、egg-info。',
        '飞书 App Secret、HMAC、open_id、未投递队列和本机日志不在本包内。',
        '安装前请阅读 docs\REMOTE_INSTALL.md 和 docs\SECURITY.md。'
    )
    Set-Content -LiteralPath (Join-Path $BundleRoot 'SHARE_PACKAGE_CONTENTS.txt') -Value $Manifest -Encoding UTF8

    Compress-Archive -LiteralPath $BundleRoot -DestinationPath $ZipPath -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw '分享包压缩文件未生成。'
    }
    Write-Host "安全分享包已生成：$ZipPath"
}
finally {
    # 仅清理本次在固定输出根下创建的 GUID 临时目录。
    $ResolvedStaging = [System.IO.Path]::GetFullPath($StagingRoot)
    if ($ResolvedStaging.StartsWith(($OutputFull + [System.IO.Path]::DirectorySeparatorChar), $Comparison) -and
        [System.IO.Path]::GetFileName($ResolvedStaging).StartsWith('.staging-', [System.StringComparison]::Ordinal)) {
        if (Test-Path -LiteralPath $ResolvedStaging -PathType Container) {
            Remove-Item -LiteralPath $ResolvedStaging -Recurse -Force
        }
    }
}
