Set-StrictMode -Version Latest

function Invoke-GuardianControl {
    param([string]$Python, [string]$Backend, [string[]]$Arguments, [switch]$ReadJson)
    $Info=New-Object Diagnostics.ProcessStartInfo
    $Info.FileName=$Python
    $All=@('-B',(Join-Path $Backend 'progress-wx.py'),'--config',(Join-Path $Backend 'config.yaml')) + $Arguments
    $Info.Arguments=($All | ForEach-Object { '"'+$_.Replace('"','\"')+'"' }) -join ' '
    $Info.UseShellExecute=$false; $Info.CreateNoWindow=$true
    $Info.RedirectStandardOutput=$true; $Info.RedirectStandardError=$true
    $Info.StandardOutputEncoding=[Text.Encoding]::UTF8
    $Info.StandardErrorEncoding=[Text.Encoding]::UTF8
    $Process=New-Object Diagnostics.Process
    $Process.StartInfo=$Info
    try {
        if(-not $Process.Start()){throw 'Cannot start guardian control.'}
        $Output=$Process.StandardOutput.ReadToEndAsync()
        $Errors=$Process.StandardError.ReadToEndAsync()
        if(-not $Process.WaitForExit(120000)) {
            $Process.Kill() # Only the short-lived command we just created.
            throw 'Guardian control timed out; actual state is not confirmed.'
        }
        $Text=$Output.GetAwaiter().GetResult()
        $null=$Errors.GetAwaiter().GetResult()
        if($Process.ExitCode -ne 0){throw ('Guardian control failed, exit '+$Process.ExitCode+'. Original installation is retained or restored.')}
        if($ReadJson) {
            if($Text.Length -gt 65536){throw 'Guardian status exceeds protocol limit.'}
            return $Text | ConvertFrom-Json
        }
    } finally {$Process.Dispose()}
}

function Get-InstalledGuardianStatus {
    param([string]$InstallRoot)
    $Backend=Join-Path $InstallRoot 'components\codex-feishu'
    $Cli=Join-Path $Backend 'src\progress_wx\cli.py'
    if(-not (Test-Path -LiteralPath $Cli)){return $null}
    if(-not ([IO.File]::ReadAllText($Cli).Contains('guardian-status'))){return $null}
    $Python=Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe'
    $Status=Invoke-GuardianControl -Python $Python -Backend $Backend -Arguments @('guardian-status','--json') -ReadJson
    if($Status.schema_version -ne 1 -or $Status.desired_state -notin @('running','stopped','maintenance','exited')){throw 'Unsupported guardian status.'}
    return $Status
}

function Enter-UpgradeGuardianMaintenance {
    param([string]$InstallRoot)
    $Status=Get-InstalledGuardianStatus $InstallRoot
    if($null -eq $Status){return $null}
    if(-not $Status.available) {
        $ControlRoot=[IO.Path]::GetFullPath([string]$Status.guardian.control_directory)
        $InstallFull=[IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
        if(-not $ControlRoot.StartsWith($InstallFull+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Guardian control directory is outside the installation.'}
        if((Test-Path -LiteralPath $ControlRoot) -and @(Get-ChildItem -LiteralPath $ControlRoot -Force).Count -gt 0) {
            throw 'Existing guardian state is unavailable; refusing to replace files or infer stopped state.'
        }
        return $null
    }
    $Original=[ordered]@{desired_state=$Status.desired_state;entered_here=($Status.desired_state -ne 'maintenance')}
    try {
        Invoke-GuardianControl -Python (Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe') -Backend (Join-Path $InstallRoot 'components\codex-feishu') -Arguments @('guardian-maintenance','--enter','--shutdown-guardian','--timeout','30')
        $Stopped=Get-InstalledGuardianStatus $InstallRoot
        if($Stopped.guardian.running -or $Stopped.worker.running -or $Stopped.desired_state -ne 'maintenance'){throw 'Maintenance did not stop both guardian and worker; refusing to replace files.'}
    } catch {
        $Failure=$_
        if($Original.entered_here) {
            try { Leave-UpgradeGuardianMaintenance -InstallRoot $InstallRoot -Original $Original }
            catch { Write-Warning 'Could not restore pre-maintenance intent; inspect guardian status before continuing.' }
        }
        throw $Failure
    }
    return $Original
}

function Leave-UpgradeGuardianMaintenance {
    param([string]$InstallRoot, $Original)
    if($null -ne $Original -and $Original.entered_here) {
        Invoke-GuardianControl -Python (Join-Path $InstallRoot 'components\Python313-ProgressWX\python.exe') -Backend (Join-Path $InstallRoot 'components\codex-feishu') -Arguments @('guardian-maintenance','--leave')
    }
}

function Export-GuardianTaskState {
    param([string]$InstallRoot,[string]$SnapshotPath)
    & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Export -InstallRoot $InstallRoot -SnapshotPath $SnapshotPath
}

function Disable-GuardianTaskForUpgrade {
    param([string]$InstallRoot,[string]$SnapshotPath)
    Export-GuardianTaskState -InstallRoot $InstallRoot -SnapshotPath $SnapshotPath
    $Snapshot=Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json
    if($Snapshot.xml -or ($Snapshot.PSObject.Properties.Name -contains 'legacy_xml' -and $Snapshot.legacy_xml)) {
        try { & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Suspend -InstallRoot $InstallRoot }
        catch {
            $Failure=$_
            try { & (Join-Path $PSScriptRoot 'guardian-task.ps1') -Mode Restore -InstallRoot $InstallRoot -SnapshotPath $SnapshotPath }
            catch { Write-Warning 'Could not restore recovery task after a failed disable.' }
            throw $Failure
        }
    }
    return $Snapshot
}

function Get-LegacyRecoveryPreference {
    param([string]$InstallRoot)
    $Config=Join-Path $InstallRoot 'config.json'
    if(-not (Test-Path -LiteralPath $Config)){return $false}
    $Value=Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
    if($Value.PSObject.Properties.Name -notcontains 'Sessions'){return $false}
    $Backend=Join-Path $InstallRoot 'components\codex-feishu'
    foreach($Session in $Value.Sessions) {
        if($Session.PSObject.Properties.Name -notcontains 'WorkingDirectory'){continue}
        if(([string]$Session.WorkingDirectory).TrimEnd('\') -ne $Backend){continue}
        if($Session.PSObject.Properties.Name -contains 'SourcePluginId' -and $Session.SourcePluginId){continue}
        $Matches=$true
        foreach($Pair in @(@('StartCommand','start'),@('StopCommand','stop'),@('StatusCommand','status'))) {
            $Expected='powershell.exe -NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $Backend ('scripts\'+$Pair[1]+'.ps1'))+'" -ToolsRoot "'+(Join-Path $InstallRoot 'components')+'"'
            if($Session.PSObject.Properties.Name -notcontains $Pair[0] -or ([string]$Session.($Pair[0])).Trim() -ne $Expected){$Matches=$false;break}
        }
        if(-not $Matches){continue}
        if(($Session.PSObject.Properties.Name -contains 'AutoStart' -and $Session.AutoStart) -or ($Session.PSObject.Properties.Name -contains 'AutoRestart' -and $Session.AutoRestart)){return $true}
    }
    return $false
}
