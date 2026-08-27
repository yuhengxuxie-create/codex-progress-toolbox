@echo off
setlocal
chcp 65001 >nul
title 飞书进度通知首次设置
for %%I in ("%~dp0..\..") do set "TREASURE_ROOT=%%~fI"
set "COMPONENTS=%TREASURE_ROOT%\components"
set "SETUP_SCRIPT=%COMPONENTS%\codex-feishu\scripts\one-click-feishu-setup.ps1"
if not exist "%SETUP_SCRIPT%" (
  echo 找不到首次设置脚本：%SETUP_SCRIPT%
  echo 请重新运行 Release 包中的 installer\install.ps1。
  pause
  exit /b 2
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SETUP_SCRIPT%" -ToolsRoot "%COMPONENTS%" -IntegratedMode
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo 首次设置没有完成，请保留本窗口信息并查看故障排除文档。
pause
exit /b %RESULT%
