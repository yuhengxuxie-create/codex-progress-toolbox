@echo off
setlocal
chcp 65001 >nul
title 进度通知 - 离开前安全准备
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\one-click-away-ready.ps1"
set "RESULT=%ERRORLEVEL%"
if errorlevel 1 (
  echo.
  echo 安全准备未完成；请保持电脑在线并查看错误。
  echo 按任意键关闭此窗口……
  pause >nul
)
exit /b %RESULT%
