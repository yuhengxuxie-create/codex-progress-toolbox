@echo off
setlocal
chcp 65001 >nul
title 进度通知 - 一键安全检测
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\one-click-safe-check.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if errorlevel 1 (
  echo 检测未通过；未启动服务，也未发送任何微信消息。
) else (
  echo 检测命令已完成。
)
echo 按任意键关闭此窗口……
pause >nul
exit /b %RESULT%
