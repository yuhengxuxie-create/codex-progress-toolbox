# Codex Feishu Ecosystem

一套面向 Windows 的完整开源生态：飞书端 Codex 管理、Codex 进度监测，以及配套桌面软件“百宝箱”。当前整体版本为 **v1.5.0**。

v1.5.0 将旧 v1.2.0 Webhook 通知脚本升级为完整生态，且配置架构不兼容。飞书端改用企业自建应用机器人和官方 WebSocket 长连接；用户必须创建自己的应用，发布包不含任何开发者账号、App ID、App Secret、open_id、任务 ID、日志或本机路径。

## 三个组成部分

- **Codex 管理**：从飞书查看项目与个人会话、新建或继续任务、双向传图、等待用户选择提醒、用户级权限审批和额度查询。
- **Codex 进度监测**：识别完成、失败、打断、待审批、待用户输入，自动发现任务并提供稳定 `monitor-*` CLI。
- **TreasureChest 百宝箱**：桌面服务管理、六列项目监测列表、批量添加/移除、在 Codex 中打开任务、自动监测设置、插件中心与托盘管理。

## 下载与安装

1. 在 GitHub Releases 下载 **`codex-feishu-ecosystem-v1.5.0-full.zip`**。不要把 GitHub 自动生成的 `Source code (zip)` 当作完整安装包；它不含自包含百宝箱、Python 安装器和离线依赖。
2. 校验 Release 页面和 `SHA256SUMS.txt` 中的 SHA-256。
3. 解压到长期稳定、非系统目录。
4. 使用 Codex 打开整个解压目录，并说：“请按照 AGENTS.md 安装并引导我创建飞书机器人。”

Codex 会先校验安装包，再完成不含凭据的基础安装，然后一次一步引导创建企业自建应用。App Secret 只在本机无回显窗口输入，绝不能粘贴进聊天。

## 升级

- GitHub v1.2.0 单体模板或 2026-08-25 旧完整生态用户，请下载 **`codex-feishu-ecosystem-v1.5.0-upgrade-from-v1.x.zip`**。
- 旧 Webhook 不能作为新版 App ID/App Secret；`config.local.json` 也不能覆盖新版 YAML。
- 升级器会先停止旧服务、建立时间戳备份、迁移或保留允许的数据、验证并健康检查；失败会自动回滚。

详情见 [UPGRADE.md](UPGRADE.md)。

## 源码结构

```text
components/codex-feishu/     Codex 管理与进度监测后台
components/treasure-chest/   TreasureChest 桌面应用
installer/                   安装、升级、回滚、校验、卸载
docs/                        飞书设置、日常使用、架构和故障处理
scripts/                     可重复构建、隐私扫描和 E2E
```

源码仓库不提交 Python 安装器、wheel 缓存、`.NET bin/obj` 或生成的 EXE。完整二进制载荷只存在于 GitHub Release 附件。

## 安全

安装器不会修改代理、路由、DNS、TUN 或 VPN。它只对本工具的 Codex `notify` 和 `PermissionRequest` Hook 做可验证、可回滚的修改，并在变更前备份。安全报告见 [SECURITY.md](SECURITY.md)。

许可证：[MIT](LICENSE)。
