# AstrBot 接入指南

## 结论

“进度通知”**无需制作或安装 AstrBot 插件**。AstrBot >= 4.24.2 自带 HTTP API，本项目作为它的受信本地客户端：

```text
Codex agent-turn-complete
        ↓
Codex Progress Toolbox（四行进度文本）
        ↓  Bearer API Key / im scope
AstrBot HTTP API
        ↓
QQ 官方机器人或个人微信适配器
```

这条路线不需要公网 Webhook；当 AstrBot 和 Codex 在同一台电脑上时，API 只需监听本机回环地址。

AstrBot 的 `im` API Key 只负责把四行消息投递到 QQ/微信，不负责语义分析。要显示动态状态和 50 字摘要，Codex Progress Toolbox 还需要单独配置可访问的 Responses API 与 API Key；未配置时会发送“情况未知”的安全兜底，不会转发完整助手回复。

## 1. 准备 AstrBot

1. 安装并启动 AstrBot >= 4.24.2，确认 WebUI 可访问。
2. 在 WebUI 的“机器人”页面完成 QQ 官方机器人或个人微信适配器登录。
3. 在 WebUI “设置”中创建开发者 API Key，仅勾选 `im` scope。不要授予 `config`、`plugin`、`chat` 等无关权限。

默认 API 端点是：

```text
http://127.0.0.1:6185/api/v1/im/message
```

AstrBot 新版核心也可在 OpenAPI 中展示复数形式 `/api/v1/im/messages`；本项目默认使用兼容 AstrBot 4.24.2 的单数入口。若本机 OpenAPI 明确只显示其中一个，可用 `-BaseUrl` 传入该完整端点。本机交互文档通常为 `http://127.0.0.1:6185/api/v1/docs`。

## 2. 获取目标 UMO

UMO 是 AstrBot 的统一消息目标标识。请在**希望收到通知的私聊**里向机器人发送：

```text
/sid
```

复制 AstrBot 返回的 `UMO: 「...」` 中书名号内的完整值，不要把展示用的 `「」` 一起复制，也不要从 QQ 号、微信号或昵称手工拼接。配置加载器会兼容并自动去除一对误复制的 `「」`。

- **QQ 官方机器人：**本集成只使用 `FriendMessage` 私聊 UMO；不要填 `GroupMessage`。
- **个人微信：**必须先在私聊中给机器人发一条消息，让适配器获得并保存 `context_token`，然后再执行 `/sid`。不支持把它当作普通微信客户端去给任意联系人或群发消息。

## 3. 写入 Codex Progress Toolbox 配置

在项目根目录运行：

```powershell
.\scripts\configure-astrbot.ps1 -Umo '粘贴完整 UMO'
```

脚本会无回显请求 API Key，然后：

- 保留 `config.local.json` 中的线程列表、分类器和其他现有配置；
- 把 `notification.provider` 设为 `astrbot`；
- 写入 API 端点和 `target_umo`；
- 只将字面量 `${ASTRBOT_API_KEY}` 写入 `bearer_token`；
- 将真实 Key 存到当前 PowerShell 进程与 Windows User 级 `ASTRBOT_API_KEY` 环境变量，不在控制台回显。

也可先设当前进程环境变量，脚本会直接使用：

```powershell
$env:ASTRBOT_API_KEY = 'abk_xxx'
.\scripts\configure-astrbot.ps1 -Umo '粘贴完整 UMO'
```

自定义配置文件或 AstrBot 地址：

```powershell
.\scripts\configure-astrbot.ps1 `
  -ConfigPath (Join-Path $PWD 'config.local.json') `
  -BaseUrl 'https://astrbot.example.com/api/v1/im/message' `
  -Umo '粘贴完整 UMO'
```

如果 AstrBot 在远程服务器上，可先在一个单独且持续打开的终端中建立本地 SSH 转发：

```powershell
ssh -N -T -L 127.0.0.1:16185:127.0.0.1:6185 user@example.com
```

然后把 `-BaseUrl` 设为 `http://127.0.0.1:16185/api/v1/im/message`。该 SSH 命令退出后本地 `16185` 会立即停止监听，`send-test` 将报告端点不可达；发送测试和 Codex 自动通知期间必须保持隧道运行。

`-ApiKey` 参数可用于无交互自动化，但可能进入 shell 历史或进程命令行；日常使用建议采用无回显提示。
自动化测试可同时传入 `-NoPersistUserEnvironment`，避免修改 Windows User 级环境变量；该模式不适合桌面应用长期运行。

## 4. 验证

配置后完全退出并重启 Codex 桌面应用，再运行：

```powershell
.\scripts\validate.ps1
.\scripts\send-test.ps1
```

请同时在 AstrBot WebUI 或日志中确认目标适配器在线。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| HTTP 401 | API Key 无效；重新运行配置脚本，不要把 Key 粘贴到日志或 Issue |
| HTTP 403 / `Insufficient API key scope` | 确认 Key 包含 `im` scope；无需扩大到管理权限 |
| HTTP 404 | 打开 AstrBot 本机 OpenAPI 文档，用 `-BaseUrl` 选择其显示的单数或复数 IM message 端点 |
| 连接拒绝 | AstrBot 未启动、端口不正确，或远程 AstrBot 使用的 SSH 本地转发已经退出 |
| QQ 无消息 | 重新在目标私聊执行 `/sid`，确认 UMO 的消息类型为 `FriendMessage` |
| 微信无消息 | 先向机器人发一条新私聊消息，让适配器刷新 `context_token`，然后重新获取 UMO |

## 安全边界

- AstrBot API 默认使用 `127.0.0.1`；不要为了省事把 6185 端口直接暴露到公网。
- 如果必须跨机访问，使用 HTTPS、防火墙白名单和独立的最小权限 Key，并通过 `-BaseUrl` 配置 HTTPS 端点。
- API Key 会作为 Windows User 级环境变量保存，同一 Windows 用户下的进程可能读取它；请使用专用 `im` Key 并定期轮换。

官方依据：[AstrBot HTTP API](https://docs.astrbot.app/dev/openapi.html)、[AstrBot 消息平台接入](https://docs.astrbot.app/platform/start.html)。
