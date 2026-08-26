[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$EntryPoint = Join-Path $ProjectRoot 'progress-notify.py'
$toolsRoot = if ($env:PROGRESS_TOOLS_ROOT) {
    $env:PROGRESS_TOOLS_ROOT
} else {
    Join-Path $env:LOCALAPPDATA 'CodexProgressToolbox'
}
$candidates = @()
if ($env:PROGRESS_PYTHON) { $candidates += $env:PROGRESS_PYTHON }
$candidates += (Join-Path $toolsRoot 'Python\python.exe')
try { $candidates += (Get-Command python.exe -ErrorAction Stop).Source } catch {}
try {
    $launcher = Get-Command py.exe -ErrorAction Stop
    $resolved = @(& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null)
    if ($LASTEXITCODE -eq 0 -and $resolved.Count -gt 0) {
        $candidates += [string]$resolved[-1]
    }
} catch {}
$Python = $null
foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
        $result = @(& $candidate -c "import sys; print('OK' if sys.version_info >= (3, 11) else 'OLD')" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $result -contains 'OK') {
            $Python = (Resolve-Path -LiteralPath $candidate).Path
            break
        }
    } catch {}
}
if (-not $Python) { throw '找不到 Python >= 3.11；请先运行 scripts\install.ps1。' }

$arguments = @($EntryPoint, 'send-test')
if ($ConfigPath) { $arguments += @('--config', $ConfigPath) }
if ($DryRun) { $arguments += '--dry-run' }
& $Python @arguments
exit $LASTEXITCODE
