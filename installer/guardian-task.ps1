[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Status','Enable','Disable','Suspend','Remove','Export','Restore','ResetCircuit')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [string]$SnapshotPath = '',
    [switch]$Enabled,
    [ValidateSet('ecosystem','standalone')][string]$Layout='ecosystem'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$Marker=if($Layout -eq 'standalone'){'.codex-feishu-guardian-root'}else{'.codex-feishu-ecosystem-root'}
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot $Marker) -PathType Leaf) -and $Mode -notin @('Status','Export','Restore') -and -not ($Mode -in @('Install','Suspend','Disable') -and $Layout -eq 'standalone')) {
    throw 'Not a managed ecosystem installation.'
}
$Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Hasher = [Security.Cryptography.SHA256]::Create()
try { $Id = ([BitConverter]::ToString($Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallRoot.ToLowerInvariant()+'|'+$Sid)))).Replace('-','').Substring(0,20) }
finally { $Hasher.Dispose() }
$TaskName = 'CodexFeishuGuardian-' + $Id
$Description = 'CodexFeishuGuardian:v1:' + $Id
$ConfigPath = Join-Path $InstallRoot '.ecosystem\guardian-supervision.json'
$RuntimeRoot=if($Layout -eq 'standalone'){Join-Path $InstallRoot 'Python313-ProgressWX'}else{Join-Path $InstallRoot 'components\Python313-ProgressWX'}
$Python = Join-Path $RuntimeRoot 'python.exe'
$PythonWindowless = Join-Path $RuntimeRoot 'pythonw.exe'
$Backend = if($Layout -eq 'standalone'){$InstallRoot}else{Join-Path $InstallRoot 'components\codex-feishu'}
$Supervisor = Join-Path $InstallRoot 'installer\guardian-supervisor.py'
$TaskArguments = '-B "' + $Supervisor + '" --root "' + $InstallRoot + '"'
$Service = New-Object -ComObject Schedule.Service
$Service.Connect()
$Folder = $Service.GetFolder('\')
$Existing = $null
try { $Existing = $Folder.GetTask($TaskName) }
catch {
    $Cause=$_.Exception
    while ($null -ne $Cause.InnerException) { $Cause=$Cause.InnerException }
    if ($Cause.HResult -ne -2147024894) { throw }
}
function Assert-OwnedTask($Task) {
    $PrincipalIdentity = [string]$Task.Definition.Principal.UserId
    try {
        $PrincipalSid = if ($PrincipalIdentity -match '^S-1-') {
            ([Security.Principal.SecurityIdentifier]::new($PrincipalIdentity)).Value
        } else {
            ([Security.Principal.NTAccount]::new($PrincipalIdentity)).Translate([Security.Principal.SecurityIdentifier]).Value
        }
    } catch { throw 'Cannot resolve recovery task principal.' }
    if ($Task.Definition.RegistrationInfo.Description -ne $Description -or
        $PrincipalSid -ne $Sid -or
        $Task.Definition.Principal.LogonType -ne 3 -or
        $Task.Definition.Principal.RunLevel -ne 0 -or
        $Task.Definition.Actions.Count -ne 1 -or
        $Task.Definition.Actions.Item(1).Path -ne $PythonWindowless -or
        $Task.Definition.Actions.Item(1).Arguments -ne $TaskArguments) {
        throw 'Existing task ownership or action does not match this installation.'
    }
}
if ($null -ne $Existing) { Assert-OwnedTask $Existing }
$LegacyTaskName = 'ProgressCheckingWX'
$Legacy = $null
try { $Legacy = $Folder.GetTask($LegacyTaskName) }
catch {
    $Cause=$_.Exception
    while($null -ne $Cause.InnerException){$Cause=$Cause.InnerException}
    if($Cause.HResult -ne -2147024894){throw}
}
function Test-OwnedLegacyTask($Task) {
    if($null -eq $Task){return $false}
    try {
        $Identity=[string]$Task.Definition.Principal.UserId
        $Owner=if($Identity -match '^S-1-'){([Security.Principal.SecurityIdentifier]::new($Identity)).Value}else{([Security.Principal.NTAccount]::new($Identity)).Translate([Security.Principal.SecurityIdentifier]).Value}
    } catch {return $false}
    $Expected='"'+(Join-Path $Backend 'progress-wx.py')+'" --config "'+(Join-Path $Backend 'config.yaml')+'" start'
    return $Owner -eq $Sid -and $Task.Definition.Principal.LogonType -eq 3 -and
        $Task.Definition.Principal.RunLevel -eq 0 -and $Task.Definition.Actions.Count -eq 1 -and
        $Task.Definition.Actions.Item(1).Path -eq $PythonWindowless -and
        $Task.Definition.Actions.Item(1).Arguments -eq $Expected -and
        $Task.Definition.Actions.Item(1).WorkingDirectory -eq $Backend
}
$OwnedLegacy = Test-OwnedLegacyTask $Legacy
function Save-Config($Value) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigPath) | Out-Null
    $Temporary = $ConfigPath + '.tmp-' + [Guid]::NewGuid().ToString('N')
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Temporary -Encoding UTF8
        Move-Item -LiteralPath $Temporary -Destination $ConfigPath -Force
    } finally { if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force } }
}
if ($Mode -eq 'Status') {
    [ordered]@{schema_version=1; installed=($null -ne $Existing); enabled=($null -ne $Existing -and $Existing.Enabled); task_name=$TaskName; logon_type='InteractiveToken'; same_user=$true; owned_legacy_autostart=$OwnedLegacy; legacy_enabled=($OwnedLegacy -and $Legacy.Enabled)} | ConvertTo-Json
    return
}
if ($Mode -eq 'Export') {
    if ([string]::IsNullOrWhiteSpace($SnapshotPath)) { throw 'SnapshotPath required.' }
    $ConfigText = if (Test-Path -LiteralPath $ConfigPath) { [IO.File]::ReadAllText($ConfigPath) } else { $null }
    $Xml = if ($null -ne $Existing) { $Existing.Xml } else { $null }
    $LegacyXml=if($OwnedLegacy){$Legacy.Xml}else{$null}
    [ordered]@{schema_version=1;task_name=$TaskName;owner_sid=$Sid;install_root=$InstallRoot;xml=$Xml;config=$ConfigText;legacy_xml=$LegacyXml} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SnapshotPath -Encoding UTF8
    return
}
if ($Mode -in @('Remove','Suspend','Disable')) {
    if (Test-Path -LiteralPath $ConfigPath) { $Config=Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json; $Config.enabled=$false; Save-Config $Config }
    if ($null -ne $Existing) { $Existing.Enabled=$false; if($Mode -eq 'Remove'){$Folder.DeleteTask($TaskName,0)} }
    if ($OwnedLegacy) { $Legacy.Enabled=$false; if($Mode -eq 'Remove'){$Folder.DeleteTask($LegacyTaskName,0)} }
    return
}
if ($Mode -eq 'Restore') {
    $Snapshot = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json
    if ($Snapshot.schema_version -ne 1 -or $Snapshot.task_name -ne $TaskName -or $Snapshot.owner_sid -ne $Sid -or $Snapshot.install_root -ne $InstallRoot) { throw 'Snapshot does not belong to this user and installation.' }
    if ($Snapshot.xml) {
        $Definition = $Service.NewTask(0)
        $Definition.XmlText = $Snapshot.xml
        # Validate before registering: a snapshot must never inject another action.
        $Wrapper = [pscustomobject]@{Definition=$Definition}
        Assert-OwnedTask $Wrapper
        $null = $Folder.RegisterTaskDefinition($TaskName,$Definition,6,$Sid,$null,3)
    } elseif ($null -ne $Existing) { $Folder.DeleteTask($TaskName,0) }
    if ($Snapshot.config) { Save-Config ($Snapshot.config | ConvertFrom-Json) }
    elseif (Test-Path -LiteralPath $ConfigPath) { Remove-Item -LiteralPath $ConfigPath -Force }
    if($Snapshot.PSObject.Properties.Name -contains 'legacy_xml') {
        if($Snapshot.legacy_xml) {
            if($null -ne $Legacy -and -not $OwnedLegacy){throw 'Legacy task name now belongs to another installation; refusing to replace it.'}
            $LegacyDefinition=$Service.NewTask(0);$LegacyDefinition.XmlText=$Snapshot.legacy_xml
            if(-not (Test-OwnedLegacyTask ([pscustomobject]@{Definition=$LegacyDefinition}))){throw 'Invalid legacy task snapshot.'}
            $null=$Folder.RegisterTaskDefinition($LegacyTaskName,$LegacyDefinition,6,$Sid,$null,3)
        } elseif($OwnedLegacy){$Folder.DeleteTask($LegacyTaskName,0)}
    }
    return
}
if ($Mode -in @('Enable','Disable','ResetCircuit')) {
    if ($null -eq $Existing -or -not (Test-Path -LiteralPath $ConfigPath)) { throw 'Recovery supervision is not installed.' }
    $Config=Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if ($Mode -eq 'ResetCircuit') {
        $LockPath = Join-Path $InstallRoot '.ecosystem\guardian-supervisor.lock'
        $Lock = [IO.File]::Open($LockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
        $Locked = $false
        try {
        $Lock.Lock(0,1); $Locked=$true
        $StatePath = [IO.Path]::GetFullPath([string]$Config.state_file)
        if (-not $StatePath.StartsWith($InstallRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'State file is outside the installation.' }
        $State = if (Test-Path -LiteralPath $StatePath) { Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json } else { [pscustomobject]@{schema_version=1} }
        foreach ($Entry in @{circuit_open=$false;attempts=@();next_attempt_at=0;suspect_since=$null;last_error_code=$null}.GetEnumerator()) { $State | Add-Member -NotePropertyName $Entry.Key -NotePropertyValue $Entry.Value -Force }
        $Temporary = $StatePath + '.tmp-' + [Guid]::NewGuid().ToString('N')
        try {
            $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Temporary -Encoding UTF8
            Move-Item -LiteralPath $Temporary -Destination $StatePath -Force
        } finally { if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force } }
        } finally { if($Locked){$Lock.Unlock(0,1)}; $Lock.Dispose() }
    } else {
        $Config.enabled=($Mode -eq 'Enable'); Save-Config $Config
        $Existing.Enabled=($Mode -eq 'Enable')
        if($OwnedLegacy){$Legacy.Enabled=$false}
    }
    return
}
foreach ($Path in @($Python,$PythonWindowless,$Supervisor,(Join-Path $Backend 'progress-wx.py'))) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Recovery component missing.' }
}
$StatusText = & $Python -B (Join-Path $Backend 'progress-wx.py') --config (Join-Path $Backend 'config.yaml') guardian-status --json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read guardian protocol.' }
$Status = ($StatusText -join "`n") | ConvertFrom-Json
$StatePath = [IO.Path]::GetFullPath([string]$Status.supervisor_state_path)
if ($Status.schema_version -ne 1 -or -not $StatePath.StartsWith($InstallRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsupported guardian protocol or state outside installation.' }
$Definition = $Service.NewTask(0)
$Definition.RegistrationInfo.Description=$Description
$Definition.Principal.UserId=$Sid
$Definition.Principal.LogonType=3
$Definition.Principal.RunLevel=0
$Definition.Settings.Enabled=[bool]$Enabled
$Definition.Settings.Hidden=$true
$Definition.Settings.MultipleInstances=2
$Definition.Settings.ExecutionTimeLimit='PT50S'
$Definition.Settings.DisallowStartIfOnBatteries=$false
$Definition.Settings.StopIfGoingOnBatteries=$false
$Definition.Settings.StartWhenAvailable=$true
$Definition.Settings.AllowDemandStart=$true
$Definition.Settings.RestartCount=0
$Trigger=$Definition.Triggers.Create(1)
$Trigger.StartBoundary=(Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
$Trigger.Repetition.Interval='PT1M'
$Trigger.Enabled=$true
$Logon=$Definition.Triggers.Create(9)
$Logon.UserId=$Sid
$Logon.Delay='PT15S'
$Action=$Definition.Actions.Create(0)
$Action.Path=$PythonWindowless
$Action.Arguments=$TaskArguments
$Action.WorkingDirectory=$InstallRoot
Save-Config ([ordered]@{schema_version=1;enabled=[bool]$Enabled;owner_sid=$Sid;python=$Python;backend=$Backend;state_file=$StatePath;task_name=$TaskName;layout=$Layout})
$null=$Folder.RegisterTaskDefinition($TaskName,$Definition,6,$Sid,$null,3)
if($OwnedLegacy){$Legacy.Enabled=$false}
if ($Layout -eq 'standalone') { Set-Content -LiteralPath (Join-Path $InstallRoot $Marker) -Value $Description -Encoding ASCII }
Write-Output ('Installed same-user recovery task; enabled=' + [bool]$Enabled)
