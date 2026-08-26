[CmdletBinding()]
param([string]$CodexHome)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$EntryPoint = Join-Path $ProjectRoot 'progress-notify.py'
$PythonUrl = 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip'
$PythonSha256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'

function Test-CompatiblePython {
    param([Parameter(Mandatory = $true)][string]$Executable)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    try {
        $result = @(& $Executable -c "import sys; print('OK' if sys.version_info >= (3, 11) else 'OLD')" 2>$null)
        return ($LASTEXITCODE -eq 0 -and $result -contains 'OK')
    } catch {
        return $false
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
    try {
        $command = Get-Command python.exe -ErrorAction Stop
        $candidates.Add($command.Source)
    } catch {}
    try {
        $launcher = Get-Command py.exe -ErrorAction Stop
        $resolved = @(& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $resolved.Count -gt 0) {
            $candidates.Add([string]$resolved[-1])
        }
    } catch {}
    foreach ($candidate in $candidates) {
        if (Test-CompatiblePython -Executable $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Install-EmbeddedPython {
    $toolsRoot = if ($env:PROGRESS_TOOLS_ROOT) {
        $env:PROGRESS_TOOLS_ROOT
    } else {
        Join-Path $env:LOCALAPPDATA 'CodexProgressToolbox'
    }
    $destination = [System.IO.Path]::GetFullPath((Join-Path $toolsRoot 'Python'))
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) ("progress-python-" + [guid]::NewGuid().ToString('N') + '.zip')
    try {
        Write-Host '未找到 Python >= 3.11，正在下载 Python.org 官方 3.13.15 x64 嵌入版...'
        Invoke-WebRequest -Uri $PythonUrl -OutFile $zipPath -UseBasicParsing
        $actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $PythonSha256) {
            throw "Python 下载包 SHA-256 校验失败。"
        }
        Expand-Archive -LiteralPath $zipPath -DestinationPath $destination -Force
    } finally {
        if (Test-Path -LiteralPath $zipPath -PathType Leaf) {
            Remove-Item -LiteralPath $zipPath -Force
        }
    }
    $python = Join-Path $destination 'python.exe'
    if (-not (Test-CompatiblePython -Executable $python)) {
        throw "嵌入式 Python 安装后无法运行标准库。"
    }
    return $python
}

if (-not (Test-Path -LiteralPath $EntryPoint -PathType Leaf)) {
    throw "入口脚本不存在：$EntryPoint"
}

$Python = Find-CompatiblePython
if (-not $Python) { $Python = Install-EmbeddedPython }

$ConfigPath = Join-Path $ProjectRoot 'config.local.json'
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'config.example.json') -Destination $ConfigPath
    Write-Host "已创建配置模板：$ConfigPath"
}

& $Python $EntryPoint validate --installation-only
if ($LASTEXITCODE -ne 0) { throw '安装前自检失败。' }

$installArgs = @($EntryPoint, 'install', '--python', $Python)
if ($CodexHome) { $installArgs += @('--codex-home', $CodexHome) }
& $Python @installArgs
if ($LASTEXITCODE -ne 0) { throw '写入 Codex notify 配置失败。' }

Write-Host ''
Write-Host '安装完成。请在 config.local.json 或环境变量中设置线程 ID 与通知通道。'
Write-Host '必须完全退出并重新启动 Codex 桌面应用，新的 notify 配置才会生效。' -ForegroundColor Yellow
