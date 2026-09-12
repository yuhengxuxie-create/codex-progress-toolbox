$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../installer/_common.ps1')
$TestRoot=Join-Path ([IO.Path]::GetTempPath()) ('CodexRollbackBoundary-'+[Guid]::NewGuid().ToString('N'))
$Backend=Join-Path $TestRoot 'components/codex-feishu'
$State=Join-Path $Backend '.state'
New-Item -ItemType Directory -Path $State -Force|Out-Null
$script:TestProcesses=@()
function Get-CimInstance { param($ClassName,$ErrorAction) return $script:TestProcesses }
$Base=@{status='prepared';kind='upgrade';legacy_kind='github-v1.2';runtime_ready=$false;guardian_original_intent=$null}
$Count=0
function Assert-Boundary([bool]$Expected,$Data=$Base,[bool]$Task=$false){
 $actual=Test-UninitializedTransactionRuntime -InstallRoot $TestRoot -Manifest ([pscustomobject]$Data) -TaskInstalled $Task
 if($actual -ne $Expected){throw "Boundary assertion $script:Count failed"}
 $script:Count++
}
Assert-Boundary $true
foreach($Change in @(@{status='completed'},@{runtime_ready=$true},@{legacy_kind='ecosystem-installed'},@{guardian_original_intent=@{desired_state='running'}})){
 $Data=$Base.Clone();foreach($Key in $Change.Keys){$Data[$Key]=$Change[$Key]};Assert-Boundary $false $Data
}
$Data=$Base.Clone();$Data.Remove('runtime_ready');Assert-Boundary $false $Data
Assert-Boundary $false $Base $true
$Marker=Join-Path $State 'persisted.json';Set-Content $Marker '{}';Assert-Boundary $false;Remove-Item -LiteralPath $Marker
$Runtime=Join-Path $TestRoot 'components/Python313-ProgressWX';New-Item -ItemType Directory $Runtime|Out-Null
$Exe=Join-Path $Runtime 'python.exe';Set-Content $Exe 'synthetic';Assert-Boundary $false;Remove-Item -LiteralPath $Exe
$Metadata=Join-Path $TestRoot '.ecosystem/installation.json';New-Item -ItemType Directory (Split-Path $Metadata)|Out-Null;Set-Content $Metadata '{}';Assert-Boundary $false;Remove-Item -LiteralPath $Metadata
$Template=Join-Path $Backend 'config.example.yaml';$Config=Join-Path $Backend 'config.yaml';Set-Content $Template 'default';Copy-Item $Template $Config;Assert-Boundary $true
Set-Content $Config 'custom control directory';Assert-Boundary $false;Remove-Item -LiteralPath $Config
$script:TestProcesses=@([pscustomobject]@{Name='helper.exe';ExecutablePath=(Join-Path $TestRoot 'helper.exe');CommandLine=''});Assert-Boundary $false
$script:TestProcesses=@([pscustomobject]@{Name='python.exe';ExecutablePath='C:\synthetic\python.exe';CommandLine=('python '+$TestRoot+'\progress-wx.py')});Assert-Boundary $false
$script:TestProcesses=@()
Remove-Item -LiteralPath $Runtime
New-Item -ItemType Junction -Path $Runtime -Target $State|Out-Null
try{Assert-Boundary $false}finally{Remove-Item -LiteralPath $Runtime}
$Backup=Join-Path $TestRoot 'backup'
$SavedBackend=Join-Path $Backup 'install-root/components/codex-feishu'
New-Item -ItemType Directory (Join-Path $SavedBackend '.state') -Force|Out-Null
Set-Content (Join-Path $State 'history.sqlite') 'synthetic old state'
Copy-Item (Join-Path $State 'history.sqlite') (Join-Path $SavedBackend '.state/history.sqlite')
Copy-Item $Template $Config;Copy-Item $Config (Join-Path $SavedBackend 'config.yaml')
$Upgrade=@{status='prepared';kind='upgrade';legacy_kind='ecosystem-installed';runtime_ready=$false;old_services_quiesced=$true;old_recovery_suspended=$true}
$Task=[pscustomobject]@{enabled=$false;legacy_enabled=$false}
function Assert-Upgrade([bool]$Expected,$Data=$Upgrade,$TaskData=$Task){
 $Actual=Test-QuiescedUpgradeRollback -InstallRoot $TestRoot -BackupRoot $Backup -Manifest ([pscustomobject]$Data) -TaskState $TaskData
 if($Actual -ne $Expected){throw "Upgrade boundary $script:Count failed"};$script:Count++
}
Assert-Upgrade $true
foreach($Change in @(@{old_services_quiesced=$false},@{old_recovery_suspended=$false},@{runtime_ready=$true},@{status='completed'})){
 $Data=$Upgrade.Clone();foreach($Key in $Change.Keys){$Data[$Key]=$Change[$Key]};Assert-Upgrade $false $Data
}
$Data=$Upgrade.Clone();$Data.Remove('old_services_quiesced');Assert-Upgrade $false $Data
Assert-Upgrade $false $Upgrade ([pscustomobject]@{enabled=$true;legacy_enabled=$false})
Assert-Upgrade $false $Upgrade ([pscustomobject]@{enabled=$false;legacy_enabled=$true})
Set-Content $Config 'changed configuration';Assert-Upgrade $false;Copy-Item $Template $Config -Force
Set-Content (Join-Path $State 'history.sqlite') 'new state activity';Assert-Upgrade $false
Copy-Item (Join-Path $SavedBackend '.state/history.sqlite') (Join-Path $State 'history.sqlite') -Force
$script:TestProcesses=@([pscustomobject]@{Name='python.exe';ExecutablePath='C:\synthetic\python.exe';CommandLine=('python '+$TestRoot+'\progress-wx.py')});Assert-Upgrade $false
$script:TestProcesses=@();Assert-Upgrade $true
Write-Output "PASS $Count rollback initialization and quiesced upgrade boundaries; isolated fixture retained: $TestRoot"
