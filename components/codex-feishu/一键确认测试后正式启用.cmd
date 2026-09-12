@echo off
chcp 65001 >nul
title 进度通知 - 确认测试后正式启用
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\one-click-enable-after-test.ps1"
if errorlevel 1 (
  echo.
  echo 正式启用未完成；服务未启动或已安全回滚。
  echo 按任意键关闭此窗口……
  pause >nul
)
