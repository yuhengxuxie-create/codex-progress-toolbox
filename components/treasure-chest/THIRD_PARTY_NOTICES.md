# 第三方组件与许可证

TreasureChest 自身源码为本地项目代码。当前构建使用以下外部组件：

## Microsoft .NET 8

- 组件：.NET SDK 8.0.424、.NET Runtime、Windows Desktop Runtime
- 来源：Microsoft 官方 .NET 分发
- 许可证：MIT License（.NET 运行时与多数库）；分发包还包含各组件各自的第三方声明
- 项目：<https://github.com/dotnet/runtime>
- 许可证：<https://github.com/dotnet/runtime/blob/main/LICENSE.TXT>
- 第三方声明：<https://github.com/dotnet/runtime/blob/main/THIRD-PARTY-NOTICES.TXT>

发布的自包含 EXE 会携带运行所需的 .NET 组件。重新分发前应保留本文件，并按 Microsoft/.NET 的第三方声明要求一并提供相应信息。

## 示例插件

`plugins/example-hello` 仅使用 Windows PowerShell 和系统自带的 WPF `PresentationFramework`，没有额外下载的第三方库。

## 外部集成说明

TreasureChest 只通过用户配置的命令/快捷方式调用以下现有本地项目，不复制或重新分发其代码：

- `ProgressChecking(WX)` 进度通知项目
- 用户的 `修复tun.lnk`

这些外部项目/工具的许可证和使用条款由各自目录中的说明负责。

