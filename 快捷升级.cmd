@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\quick-upgrade.ps1" %*
if errorlevel 1 echo Upgrade failed. Review the message above.
pause
