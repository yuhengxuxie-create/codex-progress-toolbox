[CmdletBinding()]
param(
    [string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$InstallDevDependencies
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RuntimeRoot = Join-Path $ToolsRoot 'Python313-ProgressWX'
$DownloadRoot = Join-Path $ToolsRoot 'ProgressCheckingWX-Downloads'
$FeishuCacheRoot = Join-Path $ToolsRoot 'FeishuSDK-ProgressNotify'
$PythonVersion = '3.13.14'
$InstallerPath = Join-Path $DownloadRoot "python-$PythonVersion-amd64.exe"
$PythonExe = Join-Path $RuntimeRoot 'python.exe'
$ConfigPath = Join-Path $ProjectRoot 'config.yaml'
$Identity = "${env:USERDOMAIN}\${env:USERNAME}"

# 使用独立 Python 3.13 运行时，避免污染系统 Python。
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot, $DownloadRoot | Out-Null
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        $Url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
        Invoke-WebRequest -Uri $Url -OutFile $InstallerPath -UseBasicParsing
    }
    $Signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
    if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
        throw 'Python 安装包数字签名无效，已拒绝执行。'
    }
    $Arguments = @(
        '/quiet', 'InstallAllUsers=0', "TargetDir=$RuntimeRoot", 'Include_pip=1',
        'Include_launcher=0', 'Include_test=0', 'Shortcuts=0', 'PrependPath=0'
    )
    $Process = Start-Process -FilePath $InstallerPath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($Process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "Python 3.13 安装失败，退出码 $($Process.ExitCode)。"
    }
}
$DetectedVersion = (& $PythonExe -c 'import platform; print(platform.python_version())').Trim()
if ($LASTEXITCODE -ne 0 -or $DetectedVersion -ne $PythonVersion) {
    throw "隔离运行时版本应为 $PythonVersion，实际为 $DetectedVersion。"
}

New-Item -ItemType Directory -Force -Path $FeishuCacheRoot | Out-Null
# 先下载完整、已锁版本且已锁哈希的运行时闭包，再从本地缓存安装。
# 项目命令入口会直接加载 src，无需在目标机上临时下载 setuptools 来构建可编辑包。
& $PythonExe -m pip download --disable-pip-version-check --only-binary=:all: --require-hashes --dest $FeishuCacheRoot -r (Join-Path $ProjectRoot 'requirements-feishu.txt')
if ($LASTEXITCODE -ne 0) { throw '下载完整运行时闭包到工具缓存目录失败。' }
& $PythonExe -m pip install --disable-pip-version-check --no-input --no-index --require-hashes --find-links $FeishuCacheRoot -r (Join-Path $ProjectRoot 'requirements-feishu.txt')
if ($LASTEXITCODE -ne 0) { throw '安装进度通知完整运行时失败。' }
if ($InstallDevDependencies) {
    & $PythonExe -m pip install --disable-pip-version-check --no-input -r (Join-Path $ProjectRoot 'requirements-dev.txt')
    if ($LASTEXITCODE -ne 0) { throw '安装开发测试依赖失败。' }
}
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'config.example.yaml') -Destination $ConfigPath
    Write-Host "已创建 $ConfigPath，请先运行一键配置飞书。"
}
# config.yaml 包含本机 open_id 白名单；与 HMAC 状态一样只授权当前 Windows 用户。
& icacls.exe $ConfigPath /inheritance:r /grant:r "${Identity}:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "收紧本机配置 ACL 失败：$ConfigPath" }

# 先生成本机 HMAC 密钥并安装 notify；该命令不要求账号占位值已填写。
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config $ConfigPath install-notify
if ($LASTEXITCODE -ne 0) { throw '安装 Codex notify 失败。' }
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config $ConfigPath install-permission-hook
if ($LASTEXITCODE -ne 0) { throw '安装 Codex 全局飞书审批 Hook 失败。' }
$SecretPath = Join-Path $ProjectRoot '.secrets\hmac.key'
foreach ($PrivateDirectory in @(
    (Join-Path $ProjectRoot '.secrets'),
    (Join-Path $ProjectRoot '.state'),
    (Join-Path $ProjectRoot 'logs')
)) {
    if (Test-Path -LiteralPath $PrivateDirectory -PathType Container) {
        & icacls.exe $PrivateDirectory /inheritance:r /grant:r "${Identity}:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "收紧私有目录 ACL 失败：$PrivateDirectory" }
        # 已有子项可能带显式 ACL，不能只依赖目录继承；逐项收紧并检查退出码。
        Get-ChildItem -LiteralPath $PrivateDirectory -Force -Recurse | ForEach-Object {
            $Grant = if ($_.PSIsContainer) { "${Identity}:(OI)(CI)F" } else { "${Identity}:(F)" }
            & icacls.exe $_.FullName /inheritance:r /grant:r $Grant | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "收紧私有子项 ACL 失败：$($_.FullName)" }
        }
    }
}

Write-Host '核心安装完成。'
Write-Host "已安装完整哈希锁定运行时，离线安装缓存位于：$FeishuCacheRoot"
Write-Host '下一步依次运行：一键配置飞书、一键绑定手机飞书、一键测试飞书。'
