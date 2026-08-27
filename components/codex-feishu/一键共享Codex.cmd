@echo off
chcp 65001 >nul
setlocal
title 进度通知 - 旧共享入口已停用
echo.
echo 此入口已经停用，不会启动 gateway，也不会重启 Codex。
echo 进度通知现在默认使用独立 stdio app-server；请直接正常使用 Codex。
echo 后台服务请使用桌面的“进度通知 - 启动/停止/查看状态”。
echo.
echo 按任意键关闭此窗口……
pause >nul
exit /b 0
