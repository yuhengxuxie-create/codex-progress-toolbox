@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\shared-codex-status.ps1"
echo.
echo 按任意键关闭此窗口……
pause >nul
