@echo off
setlocal
chcp 65001 >nul
title 进度通知 - 白名单测试（会发送1条）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\one-click-authorized-test.ps1"
set "RESULT=%ERRORLEVEL%"
if errorlevel 1 (
  echo.
  echo 测试未完成；后台服务没有启动。
  echo 按任意键关闭此窗口……
  pause >nul
)
exit /b %RESULT%
