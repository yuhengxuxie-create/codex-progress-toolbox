[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ZipPath)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $ZipPath).Path)
try {
    if ($Archive.Entries.Count -lt 100) { throw 'ZIP 条目异常少。' }
    $Seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($Entry in $Archive.Entries) {
        $Name = $Entry.FullName.Replace('\', '/')
        if ([string]::IsNullOrWhiteSpace($Name)) { throw 'ZIP 含空路径。' }
        if ($Name.StartsWith('/') -or $Name -match '^[A-Za-z]:' -or @($Name -split '/') -contains '..') {
            throw "ZIP 路径越界：$Name"
        }
        if (-not $Seen.Add($Name)) { throw "ZIP 路径重复：$Name" }
    }
} finally {
    $Archive.Dispose()
}
Write-Host "ZIP 路径检查通过：$ZipPath" -ForegroundColor Green

