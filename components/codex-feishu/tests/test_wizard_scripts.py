from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from progress_wx.process_control import process_creation_time


PROJECT = Path(__file__).resolve().parents[1]


def _script(name: str) -> str:
    return (PROJECT / "scripts" / name).read_text(encoding="utf-8-sig")


def test_all_powershell_scripts_have_utf8_bom_for_windows_powershell_5() -> None:
    scripts = sorted((PROJECT / "scripts").glob("*.ps1"))
    assert scripts
    assert all(path.read_bytes().startswith(b"\xef\xbb\xbf") for path in scripts)


def test_progress_cli_temporarily_decodes_native_output_as_utf8() -> None:
    text = _script("wizard-common.ps1")
    function_start = text.index("function Invoke-ProgressCli")
    function_end = text.index("function Invoke-WizardPowerShellScript", function_start)
    function = text[function_start:function_end]

    # Windows PowerShell 5 必须按 UTF-8 解码 Python 输出，且不能永久修改用户控制台。
    save = function.index("$OriginalConsoleOutputEncoding = [Console]::OutputEncoding")
    switch = function.index("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)")
    invoke = function.index("& $Context.PythonExe")
    restore = function.index("[Console]::OutputEncoding = $OriginalConsoleOutputEncoding")
    assert save < switch < invoke < restore
    assert "finally" in function


@pytest.mark.parametrize(
    "script_name", ["start-shared-codex.ps1", "recover-shared-codex-env.ps1"]
)
def test_environment_broadcast_compiles_from_committed_source(
    script_name: str,
) -> None:
    text = _script(script_name)
    function_start = text.index("function Add-ProgressWxNativeMethods")
    function_end = text.index("function Send-EnvironmentChanged", function_start)
    function = text[function_start:function_end]

    assert "ProgressWxNativeMethods.cs" in function
    assert "Add-Type -Path $SourcePath" in function
    assert "Add-Type -TypeDefinition" not in function
    assert "powershell-compiler-temp" not in text
    assert "SetEnvironmentVariable('TEMP'" not in function
    assert "SetEnvironmentVariable('TMP'" not in function


def test_next_step_wizard_never_sends_or_starts_directly() -> None:
    text = _script("one-click-next-step.ps1")
    assert "-Command 'test-wechat'" not in text
    assert "-Command 'start'" not in text
    assert "enable-autostart.ps1" not in text
    assert "activate-wechat.ps1" not in text
    assert "verify-wechat" in text
    assert "production-preflight.json" in text


def test_away_ready_only_stops_and_removes_exact_task() -> None:
    text = _script("one-click-away-ready.ps1")
    assert "-Command 'stop'" in text
    assert "-Command 'status'" in text
    assert "-TaskName 'ProgressCheckingWX'" in text
    assert "-Command 'start'" not in text
    assert "test-wechat" not in text
    assert "wxautox4" not in text


def test_authorized_test_requires_fresh_preflight_reverification_and_confirmation() -> None:
    text = _script("one-click-authorized-test.ps1")
    preflight = text.index("production-preflight.json")
    validate = text.index("@('validate', 'doctor', 'verify-wechat')")
    confirm = text.index("Confirm-WizardAction")
    send = text.index("-Command 'test-wechat'")
    marker = text.index("Write-WizardJson -Path $TestMarkerPath")
    assert preflight < validate < confirm < send < marker
    assert "NoDialog" in text
    assert "两小时内已经成功发送过一条" in text


def test_enable_stage_requires_test_receipt_and_exact_backlog_baseline() -> None:
    text = _script("one-click-enable-after-test.ps1")
    test_marker = text.index("wechat-test-sent.json")
    reverify = text.index("@('validate', 'doctor', 'verify-wechat')")
    receipt = text.index("大号已经收到刚才那 1 条")
    backlog = text.index("pending_hook_events")
    baseline = text.index("baseline-pre-activation-hooks")
    start = text.index("-Command 'start'")
    autostart = text.index("enable-autostart.ps1")
    performance = text.index("measure-running.ps1")
    assert test_marker < reverify < receipt < backlog < baseline < start < autostart < performance
    assert "--expected-count" in text
    block_end = text.index("\n    }\n", text.index("if ($PendingHooks -gt 0)"))
    assert baseline > block_end
    assert "Invoke-Rollback" in text


def test_noninteractive_mode_cannot_approve_external_action() -> None:
    text = _script("wizard-common.ps1")
    function = text[text.index("function Confirm-WizardAction") :]
    assert "if ($NoDialog) { return $false }" in function
    assert "MessageBoxDefaultButton]::Button2" in function


def test_default_desktop_shortcuts_are_reduced_to_four_production_entries() -> None:
    text = _script("create-desktop-shortcuts.ps1")
    assert "飞书首次设置" in text
    assert "启动进度通知.cmd" in text
    assert "查看进度通知状态.cmd" in text
    assert "停止进度通知.cmd" in text
    assert "一键共享Codex.cmd" not in text
    assert text.count("Name = '进度通知 - ") == 4
    assert "微信" not in text
    assert "$Shortcut.Arguments = ''" in text


def test_legacy_shared_desktop_wrapper_is_neutralized() -> None:
    text = (PROJECT / "一键共享Codex.cmd").read_text(encoding="utf-8-sig")
    assert "start-shared-codex.ps1" not in text
    assert "入口已经停用" in text
    assert "gateway" in text


def test_feishu_first_setup_orders_external_actions_and_has_rollback() -> None:
    text = _script("one-click-feishu-setup.ps1")
    monitor = text.index("configure-monitor")
    configure = text.index("configure-feishu.ps1")
    pair = text.index("pair-feishu.ps1")
    send = text.index("test-feishu.ps1")
    confirm = text.index("Confirm-WizardAction")
    baseline = text.index("baseline-pre-activation-hooks")
    start = text.index("-Command 'start'")
    autostart = text.index("enable-autostart.ps1")
    performance = text.index("measure-running.ps1")
    shortcuts = text.index("create-desktop-shortcuts.ps1")
    assert monitor < configure < pair < send < confirm < baseline < start < autostart < performance < shortcuts
    block_end = text.index("\n    }\n", text.index("if ($PendingHooks -gt 0)"))
    assert baseline > block_end
    assert "Invoke-FeishuSetupRollback" in text
    assert "普通消息和其他联系人不会触发 Codex" in text


def test_monitor_switch_wrapper_uses_force_but_is_not_added_to_desktop() -> None:
    text = _script("configure-monitor.ps1")
    shortcuts = _script("create-desktop-shortcuts.ps1")
    assert "configure-monitor --force" in text
    assert "切换监控" not in shortcuts


def test_activation_code_is_passed_via_stdin_not_child_argv() -> None:
    text = _script("activate-wechat.ps1")
    assert "$PlainCode | & $PythonExe -c $PythonCode" in text
    assert "wxautox4 import authenticate" in text
    assert "auth activate $PlainCode" not in text


def test_installer_protects_local_config_before_installing_notify() -> None:
    text = _script("install.ps1")
    protect = text.index("icacls.exe $ConfigPath /inheritance:r /grant:r")
    notify = text.index("install-notify")
    assert protect < notify
    assert '"${Identity}:(F)"' in text
    assert "FeishuSDK-ProgressNotify" in text
    assert "requirements-feishu.txt" in text
    assert "--require-hashes" in text
    assert "--only-binary=:all:" in text


def test_autostart_does_not_bypass_five_attempt_fail_stop() -> None:
    """Windows 不得在服务已停机求助后无条件复活它。"""

    text = _script("enable-autostart.ps1")
    assert "-RestartCount 0" in text
    assert "-AtLogOn" in text
    assert "-MultipleInstances IgnoreNew" in text


def test_feishu_runtime_lock_is_hashed_and_excludes_unused_openapi_package() -> None:
    text = (PROJECT / "requirements-feishu.txt").read_text(encoding="utf-8")
    requirements = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert requirements
    # 飞书锁先引入已独立校验的核心锁，其余每个包仍必须定版本并带哈希。
    assert requirements[0] == "-r requirements-core.txt"
    assert all(
        "==" in line and "--hash=sha256:" in line for line in requirements[1:]
    )
    assert "lark-channel-sdk==1.2.0" in text
    assert "lark-oapi" not in text


def test_shared_codex_launcher_is_staged_and_never_forces_existing_desktop() -> None:
    text = _script("start-shared-codex.ps1")
    existing_check = text.index("$Existing = @(Get-CodexDesktopProcesses")
    confirmation = text.index("$Confirmed = Confirm-WizardAction")
    gateway_start = text.index("-Command 'gateway-start'")
    launch_confirm = text.index("$LaunchConfirmed = Confirm-WizardAction")
    child_environment = text.index("$StartInfo.EnvironmentVariables['CODEX_APP_SERVER_WS_URL']")
    process_start = text.index("[Diagnostics.Process]::Start($StartInfo)")
    tcp_check = text.index("Get-NetTCPConnection")
    register = text.index("-Command 'register-shared-desktop'")
    assert (
        existing_check
        < confirmation
        < gateway_start
        < launch_confirm
        < child_environment
        < process_start
        < tcp_check
        < register
    )
    assert "Stop-Process" not in text
    assert "taskkill" not in text.casefold()
    assert "shell:AppsFolder\\" not in text
    assert "Start-Process -FilePath 'explorer.exe'" not in text
    assert "-Command 'start'" not in text
    assert "CODEX_APP_SERVER_WS_URL" in text
    assert "started_by_request" in text
    assert "$GatewayStartedByThisRun = $true" not in text
    assert "--expected-pid" in text
    assert "--expected-creation-time" in text
    assert "--expected-launch-token" in text
    assert "--expected-gateway-pid" in text
    assert "--expected-gateway-creation-time" in text
    assert "--expected-gateway-launch-token" in text
    assert "$FinalArguments" in text
    assert "$GatewayLaunchAttempted" in text
    assert "$Existing = @(Get-CodexDesktopProcesses" in text
    assert "$ExitWaitSeconds = 300" in text
    assert "$DesktopStartWaitSeconds = 300" in text
    assert "$WaitForNormalExit = $Existing.Count -gt 0" in text
    assert "while ($Existing.Count -gt 0" in text
    assert "未强制关闭任何进程" in text
    assert "只有你点击“是”" in text
    assert "进程级临时环境打开 Codex" in text
    assert "foreach ($Line in $GatewayStart.Lines)" not in text


def test_shared_codex_wait_and_cancel_stage_is_read_only_for_environment_and_proxy() -> None:
    text = _script("start-shared-codex.ps1")
    main = text.index("$RecoveryScript = Get-RecoveryScriptPath")
    guard = text.index("Assert-IndependentLauncherAncestry", main)
    desktop_check = text.index("$Existing = @(Get-CodexDesktopProcesses", main)
    confirmation = text.index("$Confirmed = Confirm-WizardAction", desktop_check)
    wait_loop = text.index("while ($Existing.Count -gt 0", confirmation)
    mutex = text.index("$EnvironmentMutex = Enter-CodexEnvironmentMutex", wait_loop)
    proxy_baseline = text.index("$ProxyFingerprintBefore = Get-ProxyStateFingerprint", mutex)
    launch_confirm = text.index("$LaunchConfirmed = Confirm-WizardAction", proxy_baseline)
    main_text = text[main:]

    assert guard < desktop_check < confirmation < wait_loop < mutex
    assert mutex < proxy_baseline < launch_confirm
    assert "Remove-ProgressWxDiagnosticUserVariables" not in main_text
    assert "Restore-ToolEnvironment" not in main_text
    assert "Send-EnvironmentChanged" not in main_text
    assert "Write-EnvironmentMarkerAtomic" not in main_text
    assert "Set-RecoveryRunOnce" not in main_text
    assert "[EnvironmentVariableTarget]::User" not in main_text
    assert "共享入口不能由 Codex 代为启动" in text
    assert "准备共享环境前检测到 Codex Desktop 已重新运行" in text


def test_shared_codex_visible_entry_is_harmless_and_keeps_notice_open() -> None:
    command = (PROJECT / "一键共享Codex.cmd").read_text(encoding="utf-8-sig")

    assert "powershell.exe" not in command.casefold()
    assert "start-shared-codex.ps1" not in command
    assert "入口已经停用" in command
    assert command.count("pause >nul") == 1


def test_legacy_environment_broadcast_is_not_called_by_shared_launcher() -> None:
    launcher = _script("start-shared-codex.ps1")
    source = (PROJECT / "scripts" / "ProgressWxNativeMethods.cs").read_text(encoding="utf-8")
    main = launcher[launcher.index("$RecoveryScript = Get-RecoveryScriptPath") :]
    assert "Add-Type -Path $SourcePath" in launcher
    assert "ProgressWxNativeMethods.cs" in launcher
    assert "Add-Type -TypeDefinition" not in launcher
    assert "CompilerTempRoot" not in launcher
    assert "SendMessageTimeout" in source
    assert "不读写代理、路由或网络设置" in source
    assert "Send-EnvironmentChanged" not in main


def test_shared_codex_success_has_no_user_environment_cleanup_transaction() -> None:
    text = _script("start-shared-codex.ps1")
    main = text[text.index("$RecoveryScript = Get-RecoveryScriptPath") :]
    final_check = text.index("if ($FinalJson.desktop_shared -ne $true)")
    result = text.index("$Report.Add('[结果]')", final_check)
    success_block = text[final_check:result]
    assert "Restore-ToolEnvironment" not in success_block
    assert "Invoke-GatewayRecoveryV4" not in success_block
    assert "[EnvironmentVariableTarget]::User" not in main
    assert "Send-EnvironmentChanged" not in main


def test_shared_codex_launcher_rejects_any_existing_app_server_user_variable() -> None:
    text = _script("start-shared-codex.ps1")
    assert "CODEX_APP_SERVER_FORCE_CLI" not in text
    variable_scan = text.index("Get-CodexAppServerUserVariableNames")
    gateway_start = text.index("-Command 'gateway-start'")
    assert variable_scan < gateway_start
    assert "StartsWith(" in text
    assert "'CODEX_APP_SERVER_'" in text
    assert "已有 Codex app-server 或进度通知 owner 配置" in text

    # 恢复刚删除变量后必须重新读取注册表，不能使用同进程可能缓存的用户枚举。
    scan_start = text.index("function Get-CodexAppServerUserVariableNames")
    scan_end = text.index("function Remove-ProgressWxDiagnosticUserVariables", scan_start)
    scan = text[scan_start:scan_end]
    assert "CurrentUser.OpenSubKey('Environment', $false)" in scan
    assert "GetValueNames()" in scan
    assert "$Key.Dispose()" in scan
    assert "[Environment]::GetEnvironmentVariables" not in scan

    main = text.index("$RecoveryScript = Get-RecoveryScriptPath", scan_end)
    desktop_check = text.index("$Existing = @(Get-CodexDesktopProcesses", main)
    variable_check = text.index("$ExistingVariables = @(Get-CodexAppServerUserVariableNames)", desktop_check)
    gateway_start = text.index("-Command 'gateway-start'", variable_check)
    main_text = text[main:]
    assert desktop_check < variable_check < gateway_start
    assert "Remove-ProgressWxDiagnosticUserVariables" not in main_text
    assert "Restore-ToolEnvironment" not in main_text
    assert "进程级启动预检通过" in text
    assert "检测项：" in text


def test_shared_codex_launcher_keeps_proxy_neutral_and_refreshes_child_scope() -> None:
    text = _script("start-shared-codex.ps1")
    proxy_start = text.index("function Invoke-ProgressCliWithFreshUserProxyEnvironment")
    proxy_end = text.index("function Get-WindowsPowerShellExecutable", proxy_start)
    proxy_function = text[proxy_start:proxy_end]
    gateway_start = text.index("-Command 'gateway-start'")

    assert "Get-ProxyStateFingerprint" in text
    assert "Assert-ProxyStateUnchanged" in text
    assert "Get-WinInetFixedProxyEnvironment" in text
    assert "ConvertTo-ProxyEnvironmentUri" in text
    assert "Invoke-ProgressCliWithFreshUserProxyEnvironment" in text[:gateway_start]
    assert "HTTP_PROXY" in proxy_function
    assert "HTTPS_PROXY" in proxy_function
    assert "ALL_PROXY" in proxy_function
    assert "NO_PROXY" in proxy_function
    assert "[EnvironmentVariableTarget]::Process" in proxy_function
    assert "[EnvironmentVariableTarget]::User" not in proxy_function
    assert "[EnvironmentVariableTarget]::Machine" not in proxy_function
    assert "foreach ($Name in $Names)" in proxy_function
    assert "$EffectiveValues.ContainsKey($Name)" in proxy_function
    assert "$WinInetValues = Get-WinInetFixedProxyEnvironment" in proxy_function
    assert "localhost,127.0.0.1,::1" in proxy_function
    assert "$OriginalPresent" in proxy_function
    assert "Remove-Item -LiteralPath ('Env:' + $Name)" in proxy_function
    assert "ProxyEnable /" not in text
    assert "ProxyServer /" not in text
    assert "setx" not in text.casefold()
    assert "netsh" not in text.casefold()
    assert "普通代理配置与启动前完全一致" in text


def test_shared_codex_launcher_uses_child_only_environment_and_no_marker_hooks() -> None:
    launcher = _script("start-shared-codex.ps1")
    main = launcher[launcher.index("$RecoveryScript = Get-RecoveryScriptPath") :]
    manifest = main.index("Get-CodexDesktopExecutable -Package $Package")
    set_url = main.index("$StartInfo.EnvironmentVariables['CODEX_APP_SERVER_WS_URL']")
    process_start = main.index("[Diagnostics.Process]::Start($StartInfo)")
    assert manifest < set_url < process_start
    assert "Get-AppxPackageManifest" in launcher
    assert "Windows.FullTrustApplication" in launcher
    assert "$StartInfo.UseShellExecute = $false" in main
    assert "$StartInfo.EnvironmentVariables.Remove($EnvironmentOwnerVariable)" in main
    assert "--install-location" in launcher
    assert "--not-before-filetime" in launcher
    assert "Global\\ProgressCheckingWX-CodexEnvironment-" in launcher
    assert "-Command 'gateway-recover-owned'" in launcher
    assert "Write-EnvironmentMarkerAtomic" not in main
    assert "Set-RecoveryRunOnce" not in main
    assert "Start-RecoveryWatcher" not in main
    assert "Restore-ToolEnvironment" not in main
    assert "Send-EnvironmentChanged" not in main
    assert "[EnvironmentVariableTarget]::User" not in main


def test_process_start_info_environment_is_child_only_on_windows() -> None:
    powershell_script = r"""
$ErrorActionPreference = 'Stop'
$Name = 'PROGRESS_WX_CHILD_SCOPE_' + [Guid]::NewGuid().ToString('N')
if ($null -ne [Environment]::GetEnvironmentVariable($Name, 'User')) { exit 10 }
if ($null -ne [Environment]::GetEnvironmentVariable($Name, 'Process')) { exit 11 }
$StartInfo = [Diagnostics.ProcessStartInfo]::new()
$StartInfo.FileName = $env:ComSpec
$StartInfo.Arguments = '/d /c exit 0'
$StartInfo.UseShellExecute = $false
$StartInfo.CreateNoWindow = $true
$StartInfo.EnvironmentVariables[$Name] = 'child-only'
$Child = [Diagnostics.Process]::Start($StartInfo)
if ($null -eq $Child -or -not $Child.WaitForExit(5000) -or $Child.ExitCode -ne 0) { exit 12 }
if ($null -ne [Environment]::GetEnvironmentVariable($Name, 'Process')) { exit 13 }
if ($null -ne [Environment]::GetEnvironmentVariable($Name, 'User')) { exit 14 }
Write-Output 'child-scope-ok'
"""
    encoded = base64.b64encode(powershell_script.encode("utf-16-le")).decode("ascii")
    powershell = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = subprocess.run(
        [
            os.fspath(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "child-scope-ok" in result.stdout


def test_shared_codex_stop_holds_same_environment_transaction_mutex() -> None:
    launcher = _script("start-shared-codex.ps1")
    stop = _script("stop-shared-codex.ps1")
    identity = "Global\\ProgressCheckingWX-CodexEnvironment-"
    assert identity in launcher
    assert identity in stop
    assert stop.index("Enter-CodexEnvironmentMutex") < stop.index("gateway-stop")
    assert stop.index("gateway-stop") < stop.index("ReleaseMutex")


def test_shared_codex_launcher_fails_closed_for_unreadable_current_session_processes() -> None:
    text = _script("start-shared-codex.ps1")
    assert "$Process.SessionId" in text
    assert "$CurrentSessionId" in text
    assert "$IsCurrentSession" in text
    assert "Path]::IsPathRooted" in text
    assert "for ($Attempt = 1; $Attempt -le 5; $Attempt++)" in text
    assert "Get-Process -Id ([int]$Process.ProcessId)" in text
    assert "$DotNetProcess.Path" in text
    assert "$DotNetProcess.MainModule.FileName" in text
    assert "$Refreshed = Get-CimInstance Win32_Process" in text
    assert "if ($ProcessExited) { continue }" in text
    assert "连续 5 次无法读取映像路径" in text
    assert "其它用户会话的同名进程" in text
    assert "其它可读且可规范化的 ChatGPT.exe 路径" in text


def test_shared_codex_process_scan_tolerates_exit_race_but_not_persistent_unknown() -> None:
    text = _script("start-shared-codex.ps1")
    function_start = text.index("function Get-CodexDesktopProcesses")
    function_end = text.index("function Get-CodexAppServerUserVariableNames", function_start)
    function = text[function_start:function_end]
    powershell_script = f"""
$ErrorActionPreference = 'Stop'
$script:Mode = 'exits'
function New-FakeCodexProcess {{
    return [pscustomobject]@{{
        ProcessId = 424242
        SessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
        ExecutablePath = $null
    }}
}}
function Get-CimInstance {{
    [CmdletBinding()]
    param([Parameter(Position = 0)][string]$ClassName, [string]$Filter)
    if ($Filter -like 'Name=*') {{ return (New-FakeCodexProcess) }}
    if ($script:Mode -eq 'persistent') {{ return (New-FakeCodexProcess) }}
    return $null
}}
function Get-Process {{
    [CmdletBinding()]
    param([int]$Id)
    return [pscustomobject]@{{
        Path = $null
        MainModule = [pscustomobject]@{{ FileName = $null }}
    }}
}}
function Start-Sleep {{
    [CmdletBinding()]
    param([int]$Milliseconds)
}}
{function}
$Exited = @(Get-CodexDesktopProcesses -InstallLocation 'C:\\Program Files\\WindowsApps\\OpenAI.Codex_Test')
if ($Exited.Count -ne 0) {{ exit 10 }}
$script:Mode = 'persistent'
$FailedClosed = $false
try {{
    [void](Get-CodexDesktopProcesses -InstallLocation 'C:\\Program Files\\WindowsApps\\OpenAI.Codex_Test')
}}
catch {{
    $FailedClosed = $_.Exception.Message -like '*连续 5 次无法读取映像路径*'
}}
if (-not $FailedClosed) {{ exit 11 }}
Write-Output 'exit-race-ok'
"""
    encoded = base64.b64encode(powershell_script.encode("utf-16-le")).decode("ascii")
    powershell = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = subprocess.run(
        [
            os.fspath(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "exit-race-ok" in result.stdout


def _run_temp_recovery(
    tmp_path: Path,
    marker_token: str,
    argument_token: str,
    *,
    gateway_launch_token: str | None = None,
    gateway_state_token: str | None = None,
    gateway_state_available: bool = True,
    gateway_state_delay_seconds: float = 0.0,
    existing_stop_version: int | None = None,
    retry_unit_milliseconds: int = 10,
):
    scripts = tmp_path / "scripts"
    state = tmp_path / ".state"
    scripts.mkdir()
    state.mkdir()
    recovery = scripts / "recover-shared-codex-env.ps1"
    shutil.copyfile(PROJECT / "scripts" / recovery.name, recovery)
    native_source = scripts / "ProgressWxNativeMethods.cs"
    shutil.copyfile(PROJECT / "scripts" / native_source.name, native_source)
    marker = state / "codex-launch-environment.json"
    marker.write_text(
        json.dumps(
            {
                "version": 3,
                "generation_token": marker_token,
                "websocket_url": "ws://127.0.0.1:6230/",
                "gateway_cleanup_enabled": gateway_launch_token is not None,
                "gateway_launch_token": gateway_launch_token or "",
            }
        ),
        encoding="utf-8",
    )
    gateway_pid = state / "codex-gateway.pid"
    gateway_stop = Path(os.fspath(gateway_pid) + ".stop")
    writer: threading.Thread | None = None
    if gateway_launch_token is not None:
        state_token = gateway_state_token or gateway_launch_token
        gateway_payload = {
            "pid": os.getpid(),
            "creation_time": process_creation_time(os.getpid()),
            "project_root": os.fspath(tmp_path),
            "launch_token_sha256": hashlib.sha256(
                state_token.encode("utf-8")
            ).hexdigest(),
        }

        def publish_gateway_state() -> None:
            if gateway_state_delay_seconds > 0:
                time.sleep(gateway_state_delay_seconds)
            gateway_pid.write_text(json.dumps(gateway_payload), encoding="utf-8")

        if gateway_state_available:
            if gateway_state_delay_seconds > 0:
                writer = threading.Thread(target=publish_gateway_state, daemon=True)
                writer.start()
            else:
                publish_gateway_state()
        if existing_stop_version is not None:
            gateway_stop.write_text(
                json.dumps(
                    {
                        "version": existing_stop_version,
                        "pid": os.getpid(),
                        "creation_time": process_creation_time(os.getpid()),
                        "requested_at": 1,
                    }
                ),
                encoding="utf-8",
            )
    powershell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [
                os.fspath(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.fspath(recovery),
                "-Recover",
                "-MarkerPath",
                os.fspath(marker),
                "-GenerationToken",
                argument_token,
                "-GatewayRetryUnitMilliseconds",
                str(retry_unit_milliseconds),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    finally:
        if writer is not None:
            writer.join(timeout=2)
    return result, marker, gateway_stop


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows RunOnce 恢复脚本")
def test_recovery_generation_token_allows_own_marker_only(tmp_path: Path) -> None:
    token = "1" * 32
    result, marker, _stop = _run_temp_recovery(tmp_path, token, token)
    assert result.returncode == 0
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows RunOnce 恢复脚本")
def test_recovery_generation_mismatch_preserves_marker(tmp_path: Path) -> None:
    result, marker, _stop = _run_temp_recovery(tmp_path, "2" * 32, "3" * 32)
    assert result.returncode != 0
    assert marker.is_file()


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows watcher 异常回收")
def test_recovery_requests_stop_for_exact_owned_gateway(tmp_path: Path) -> None:
    marker_token = "4" * 32
    launch_token = "5" * 64
    result, marker, stop = _run_temp_recovery(
        tmp_path,
        marker_token,
        marker_token,
        gateway_launch_token=launch_token,
    )
    assert result.returncode == 0
    assert not marker.exists()
    payload = json.loads(stop.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["pid"] == os.getpid()
    assert payload["creation_time"] == process_creation_time(os.getpid())


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows watcher 异常回收")
def test_recovery_never_stops_gateway_with_different_nonce(tmp_path: Path) -> None:
    marker_token = "6" * 32
    result, marker, stop = _run_temp_recovery(
        tmp_path,
        marker_token,
        marker_token,
        gateway_launch_token="7" * 64,
        gateway_state_token="8" * 64,
    )
    assert result.returncode == 0
    assert not marker.exists()
    assert not stop.exists()


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows watcher 异常回收")
def test_recovery_waits_for_owned_gateway_pid_publish(tmp_path: Path) -> None:
    marker_token = "9" * 32
    launch_token = "a" * 64
    result, marker, stop = _run_temp_recovery(
        tmp_path,
        marker_token,
        marker_token,
        gateway_launch_token=launch_token,
        gateway_state_delay_seconds=0.05,
    )
    assert result.returncode == 0
    assert not marker.exists()
    assert json.loads(stop.read_text(encoding="utf-8"))["version"] == 1


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows watcher 异常回收")
def test_recovery_preserves_marker_when_gateway_pid_never_published(tmp_path: Path) -> None:
    marker_token = "b" * 32
    result, marker, stop = _run_temp_recovery(
        tmp_path,
        marker_token,
        marker_token,
        gateway_launch_token="c" * 64,
        gateway_state_available=False,
    )
    assert result.returncode != 0
    assert marker.is_file()
    assert not stop.exists()


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows watcher 异常回收")
def test_recovery_rejects_same_generation_stop_with_wrong_version(tmp_path: Path) -> None:
    marker_token = "d" * 32
    result, marker, stop = _run_temp_recovery(
        tmp_path,
        marker_token,
        marker_token,
        gateway_launch_token="e" * 64,
        existing_stop_version=2,
    )
    assert result.returncode != 0
    assert marker.is_file()
    assert json.loads(stop.read_text(encoding="utf-8"))["version"] == 2


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows watcher 异常回收")
def test_recovery_accepts_existing_same_generation_v1_stop(tmp_path: Path) -> None:
    marker_token = "f" * 32
    result, marker, stop = _run_temp_recovery(
        tmp_path,
        marker_token,
        marker_token,
        gateway_launch_token="0" * 64,
        existing_stop_version=1,
    )
    assert result.returncode == 0
    assert not marker.exists()
    assert json.loads(stop.read_text(encoding="utf-8"))["version"] == 1
