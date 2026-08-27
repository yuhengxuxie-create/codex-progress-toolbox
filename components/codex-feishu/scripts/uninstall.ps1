[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '运行时不存在，无法安全停止服务并恢复 Codex notify；未执行卸载。'
}
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config (Join-Path $ProjectRoot 'config.yaml') stop
if ($LASTEXITCODE -ne 0) {
    throw "服务未能正常停止（退出码 $LASTEXITCODE）；未修改 Codex notify 或开机自启。"
}
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config (Join-Path $ProjectRoot 'config.yaml') uninstall-notify
if ($LASTEXITCODE -ne 0) {
    throw "Codex notify 恢复失败（退出码 $LASTEXITCODE）；保留开机自启供人工核对。"
}
& $PythonExe (Join-Path $ProjectRoot 'progress-wx.py') --config (Join-Path $ProjectRoot 'config.yaml') uninstall-permission-hook
if ($LASTEXITCODE -ne 0) {
    throw "Codex 全局飞书审批 Hook 移除失败（退出码 $LASTEXITCODE）；保留开机自启供人工核对。"
}
& (Join-Path $PSScriptRoot 'disable-autostart.ps1')
Write-Host '已恢复 Codex notify、移除本工具审批 Hook 并移除开机自启；配置、日志、密钥和外部运行时均保留，可人工备份后删除。'
