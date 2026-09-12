@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\pair-feishu.ps1"
set "RESULT=%ERRORLEVEL%"
pause

exit /b %RESULT%
