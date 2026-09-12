@echo off
setlocal
chcp 65001 >nul
title 进度通知 - 单账号只读结构诊断
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\one-click-safe-check.ps1" -DiagnosticUnverifiedIdentity
set "RESULT=%ERRORLEVEL%"
echo.
if errorlevel 1 (
  echo 诊断未完成；未启动服务，也未发送任何微信消息。
) else (
  echo 只读诊断已完成；身份仍未达到生产核验要求。
)
echo 按任意键关闭此窗口……
pause >nul
exit /b %RESULT%
