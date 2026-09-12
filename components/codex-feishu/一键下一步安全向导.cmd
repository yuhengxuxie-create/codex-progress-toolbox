@echo off
setlocal
chcp 65001 >nul
title 进度通知 - 下一步安全向导
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\one-click-next-step.ps1"
set "RESULT=%ERRORLEVEL%"
if errorlevel 1 (
  echo.
  echo "向导未能安全完成；没有发送微信，也没有启动服务。"
  echo "按任意键关闭此窗口……"
  pause >nul
)
exit /b %RESULT%
