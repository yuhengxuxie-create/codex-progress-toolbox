@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\shared-codex-status.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
echo 按任意键关闭此窗口……
pause >nul
exit /b %RESULT%
