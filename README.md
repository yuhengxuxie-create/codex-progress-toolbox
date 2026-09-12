# Codex 飞书生态

面向 Windows 的飞书任务管理与进度通知工具，配套桌面应用“百宝箱”。本目录对应 **v1.6.2** 正式版本。

本版本修复更新检查限流提示、Skills 直接浏览、会话名称保留和模型信息显示，详见 [更新说明](docs/RELEASE_NOTES_v1.6.2.md)。

## 本版本包含什么

- 飞书功能中心：创建和查询任务、搜索会话、引用消息继续图文对话，以及 Goal、Plan 等指令使用。
- 直接问题的答案、可审阅成果、重要变化、待操作和任务完成通知；图片与成果文件独立回传，保留历史关联和去重状态。
- Codex 额度重置消息预警与记录。预警不代表个人账户额度已经重置；来源故障会显示覆盖不完整。
- 百宝箱任务监测、工具与插件管理、主题设置、预警已读记录和整套生态更新。
- 四页图解使用说明、完整文字说明，以及旧用户飞书卡片回调和底部菜单设置指引。

## 安装与首次升级

从 [GitHub Releases](https://github.com/yuhengxuxie-create/codex-progress-toolbox/releases) 下载对应完整安装包或升级包。自动生成的 Source code ZIP 不含运行载荷。

新用户选 full 包，解压后用 Codex 打开目录并说“按照 AGENTS.md 安装并引导配置飞书”。先校验 SHA256SUMS.txt 和包内文件，再安装并输入凭据。App Secret 只能在本机隐藏输入窗口填写，不要发送到聊天。

**已有 v1.5.0 用户首次升级须下载 upgrade-from-v1.x 包**，解压到旧安装目录之外，双击根目录“快捷升级.cmd”，选择旧安装目录，按 [升级说明](UPGRADE.md) 完成操作。不要寻找旧版没有的内置更新按钮，也不要仅替换 EXE。以后可通过新版百宝箱“设置 → 生态更新”检查正式更新。

保留原机器人、配置和绑定，不必重新配对。权限 scope 没有新增；卡片操作需要补充回调，底部菜单需要相应事件和菜单项。按 [飞书升级设置](docs/FEISHU_UPGRADE_PERMISSIONS.md) 完成配置。

## 使用说明

- [飞书文字指南](components/codex-feishu/docs/FEISHU_USAGE.md)
- [图文使用入口与最新变化](docs/USAGE_CURRENT.md)
- [百宝箱指南](components/treasure-chest/README.md)
- [升级、备份和回滚](UPGRADE.md)
- [通信守护与 Windows 自动恢复](docs/WINDOWS_GUARDIAN_RECOVERY.md)
- [安全说明](SECURITY.md)

源码位于 components/codex-feishu 和 components/treasure-chest；installer 包含安装和回滚脚本，scripts 包含构建与验证工具。公开包不包含生产配置、凭据、数据库、日志或用户内容。安装后请完成自己的飞书应用配置和用户绑定，并确认消息能够正常收发。

许可证：[MIT](LICENSE)。
