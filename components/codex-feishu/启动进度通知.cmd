@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
set "RESULT=%ERRORLEVEL%"
pause

exit /b %RESULT%
