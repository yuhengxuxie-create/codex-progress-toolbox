[CmdletBinding()]
param([string]$ToolsRoot = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$PythonExe = Join-Path $ToolsRoot 'Python313-ProgressWX\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'wxautox4 不存在，请先运行 install-wechat-backend.ps1。'
}
# SecureString 避免激活码进入历史；stdin 避免把明文放入子进程命令行或环境变量。
$SecureCode = Read-Host '请输入 wxautox4 Plus 激活码' -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureCode)
try {
    $PlainCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    $PythonCode = @'
import sys
from wxautox4 import authenticate

code = sys.stdin.readline().rstrip("\r\n")
if not code:
    raise SystemExit("激活码不能为空")
result = authenticate(code)
raise SystemExit(1 if result is False else 0)
'@
    $PlainCode | & $PythonExe -c $PythonCode
    if ($LASTEXITCODE -ne 0) { throw 'wxautox4 激活失败。' }
} finally {
    if ($null -ne $PlainCode) { Remove-Variable PlainCode -ErrorAction SilentlyContinue }
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}
