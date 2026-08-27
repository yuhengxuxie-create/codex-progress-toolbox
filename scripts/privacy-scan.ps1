[CmdletBinding()]
param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$SensitiveYaml = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Root).Path).TrimEnd('\')
$IsPackage = Test-Path -LiteralPath (Join-Path $Root 'PACKAGE_TYPE.txt') -PathType Leaf
$Violations = New-Object System.Collections.Generic.List[string]
$PrivateSegments = @('.secrets', '.state', 'logs', 'Share', 'Archive', 'release-local')
$GeneratedSegments = @('__pycache__', '.pytest_cache', '.dotnet', 'bin', 'obj', 'build', 'dist', 'exports')
$ForbiddenLeaves = @('config.yaml', 'config.local.json', 'config.json')
$ForbiddenExtensions = @('.pyc', '.pyo', '.dpapi', '.key', '.db', '.sqlite', '.sqlite3', '.log', '.pdb')

foreach ($Item in Get-ChildItem -LiteralPath $Root -Recurse -Force) {
    $Relative = $Item.FullName.Substring($Root.Length).TrimStart('\')
    $Segments = @($Relative -split '[\\/]')
    if (-not $IsPackage -and $Segments -contains '.git') { continue }
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        $Violations.Add("reparse-point`t$Relative")
        continue
    }
    if (@($Segments | Where-Object { $_ -in $PrivateSegments }).Count -gt 0) {
        $Violations.Add("forbidden-segment`t$Relative")
    }
    if ($IsPackage -and @($Segments | Where-Object { $_ -in $GeneratedSegments }).Count -gt 0) {
        $Violations.Add("generated-segment`t$Relative")
    }
    if ($IsPackage -and $Segments -contains '.git') { $Violations.Add("git-metadata`t$Relative") }
    if (-not $Item.PSIsContainer) {
        if ($Item.Name -in $ForbiddenLeaves -or $Item.Name -like 'config.yaml.*.bak') {
            $Violations.Add("private-config`t$Relative")
        }
        if ($IsPackage -and $Item.Extension.ToLowerInvariant() -in $ForbiddenExtensions) {
            $Violations.Add("private-extension`t$Relative")
        }
    }
}

$Needles = New-Object System.Collections.Generic.List[string]
foreach ($Known in @([Environment]::UserName, ('yu' + 'hengxuxie' + '-create'))) {
    if (-not [string]::IsNullOrWhiteSpace($Known) -and $Known.Length -ge 5) { $Needles.Add($Known) }
}
if (-not [string]::IsNullOrWhiteSpace($SensitiveYaml) -and (Test-Path -LiteralPath $SensitiveYaml -PathType Leaf)) {
    $SensitiveText = Get-Content -LiteralPath $SensitiveYaml -Raw
    foreach ($Pattern in @('(?m)^\s*app_id\s*:\s*["'']?([^\s#"'']+)', '(?m)^\s*target_open_id\s*:\s*["'']?([^\s#"'']+)')) {
        $Match = [regex]::Match($SensitiveText, $Pattern)
        if ($Match.Success -and $Match.Groups[1].Value.Length -ge 8) { $Needles.Add($Match.Groups[1].Value) }
    }
}

$TextExtensions = @('.txt', '.md', '.json', '.yaml', '.yml', '.ps1', '.cmd', '.bat', '.py', '.toml', '.cs', '.csproj', '.xml', '.config')
foreach ($File in Get-ChildItem -LiteralPath $Root -Recurse -File -Force | Where-Object {
    $Relative = $_.FullName.Substring($Root.Length).TrimStart('\')
    $Segments = @($Relative -split '[\\/]')
    $_.Extension.ToLowerInvariant() -in $TextExtensions -and
        $Segments -notcontains '.git' -and
        @($Segments | Where-Object { $_ -in $GeneratedSegments }).Count -eq 0
}) {
    $Relative = $File.FullName.Substring($Root.Length).TrimStart('\')
    $Text = Get-Content -LiteralPath $File.FullName -Raw -ErrorAction Stop
    foreach ($Needle in $Needles | Select-Object -Unique) {
        if ($Text.IndexOf($Needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $Violations.Add("known-private-identity`t$Relative")
            break
        }
    }
    foreach ($Rule in @(
        @{ Name = 'machine-path'; Pattern = '(?i)(?:[A-Z]:\\Software\\Tool\\|C:\\Users\\[^\\\s]+)' },
        @{ Name = 'feishu-app-id'; Pattern = '(?i)\bcli_[a-z0-9]{10,}\b' },
        @{ Name = 'feishu-open-id'; Pattern = '(?i)\bou_[a-z0-9]{10,}\b' },
        @{ Name = 'legacy-webhook'; Pattern = '(?i)open\.feishu\.cn/open-apis/bot/v2/hook/[a-z0-9-]+' }
    )) {
        if ([regex]::IsMatch($Text, $Rule.Pattern)) { $Violations.Add("$($Rule.Name)`t$Relative") }
    }
    foreach ($Match in [regex]::Matches($Text, '(?i)\b01a[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')) {
        if ($Match.Value -notmatch '^01a(?:00000|fffff|eeeee|ddddd)-') {
            $Violations.Add("thread-id`t$Relative")
        }
    }
}

$Unique = @($Violations | Sort-Object -Unique)
if ($Unique.Count -gt 0) {
    $Preview = ($Unique | Select-Object -First 30) -join [Environment]::NewLine
    throw "隐私扫描失败（不回显命中值）：$([Environment]::NewLine)$Preview"
}
Write-Host "隐私扫描通过：$Root" -ForegroundColor Green
