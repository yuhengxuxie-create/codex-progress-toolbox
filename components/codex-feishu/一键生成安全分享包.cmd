@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0.") do set "PROJECT=%%~fI"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PROJECT%\scripts\export-safe-bundle.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo 生成失败，请保留本窗口的错误信息。
pause
exit /b %RESULT%
