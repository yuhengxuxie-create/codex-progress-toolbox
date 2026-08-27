# TreasureChest 百宝箱

TreasureChest 是 Codex Feishu Ecosystem v1.5.0 的 Windows 桌面管理器。它提供后台服务管理、托盘单实例、插件中心、工具启动、开机自启，以及六列 Codex 项目监测列表和自动监测设置界面。

百宝箱通过后台稳定的 CLI/JSON 契约调用 `monitor-list`、`monitor-add`、`monitor-remove` 与 `monitor-settings`，不直接复制业务规则，也不直接改写 YAML。

## 构建

需要 Windows x64 与 .NET 8 SDK：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

可通过 `-DotNetPath` 指定 `dotnet.exe`。发布产物位于 `build\publish\TreasureChest.exe`。项目文件声明产品版本 1.5.0、文件版本 1.5.0.0；插件 SDK 主版本仍是独立契约 1。

普通用户请下载生态 full Release，不要只复制本组件源码或单个 EXE。
