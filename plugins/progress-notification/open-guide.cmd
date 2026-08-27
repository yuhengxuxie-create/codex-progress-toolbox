@echo off
setlocal
for %%I in ("%~dp0..\..") do set "TREASURE_ROOT=%%~fI"
set "GUIDE=%TREASURE_ROOT%\docs\FEISHU_SETUP.md"
if not exist "%GUIDE%" exit /b 2
start "" "%GUIDE%"
exit /b 0
