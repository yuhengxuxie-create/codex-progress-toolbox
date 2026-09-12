param([Parameter(Mandatory=$true)][string]$Root)
$ErrorActionPreference='Stop'
$Root=(Resolve-Path -LiteralPath $Root).Path
$Count=0
foreach($File in Get-ChildItem -LiteralPath $Root -Recurse -File -Force){
 if($File.Extension -notin @('.cmd','.bat')){continue}
 $Bytes=[IO.File]::ReadAllBytes($File.FullName)
 if($Bytes.Length -ge 2 -and (($Bytes[0] -eq 255 -and $Bytes[1] -eq 254) -or ($Bytes[0] -eq 254 -and $Bytes[1] -eq 255))){throw "Launcher BOM is unsupported: $($File.FullName)"}
 if($Bytes.Length -ge 3 -and $Bytes[0] -eq 239 -and $Bytes[1] -eq 187 -and $Bytes[2] -eq 191){throw "Launcher BOM is unsupported: $($File.FullName)"}
 for($i=0;$i -lt $Bytes.Length;$i++){
  if($Bytes[$i] -eq 10 -and ($i -eq 0 -or $Bytes[$i-1] -ne 13)){throw "Launcher requires CRLF, found bare LF: $($File.FullName)"}
  if($Bytes[$i] -eq 13 -and ($i+1 -eq $Bytes.Length -or $Bytes[$i+1] -ne 10)){throw "Launcher contains bare CR: $($File.FullName)"}
 }
 $Count++
}
if($Count -eq 0){throw 'No CMD/BAT launchers found'}
Write-Output "PASS $Count CMD/BAT launchers use CRLF without BOM"
