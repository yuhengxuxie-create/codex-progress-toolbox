# Codex Progress Toolbox

一套可直接交给 Codex 安装的 Windows 模板，用于管理需要关注的 Codex
对话，并在每轮正常完成后把四行中文进度摘要发送到飞书。

项目同时保留 AstrBot（QQ/个人微信）、企业微信群机器人和通用 HTTPS
Webhook 适配器。源码只依赖 Python 标准库，不捆绑账号、服务器、密钥、
对话记录或任何开发者电脑路径。

## 能做什么

- 使用 Codex 官方 `agent-turn-complete` 通知事件，不轮询聊天界面；
- 只监控用户明确选中的完整 `thread-id`，不做标题模糊匹配；
- 提供中文会话管理器，可搜索、多选并保存监控列表；
- 原生支持飞书自定义机器人 Webhook 和可选签名校验；
- 可选调用 Responses API，把本轮结果归纳为短状态和 50 字内详情；
- 安装器会备份并安全修改用户级 Codex `config.toml`，卸载时可恢复；
- 附带测试、隐私扫描和安全打包脚本。

每条消息固定四行：

```text
对话名称：修复支付回调
当前进度：待人工测试
进度详情：自动检查已通过，需要在真实环境验证回调。
本条消息时间：2026-08-20 21:08:35（北京时间）
```

> [!IMPORTANT]
> 当前保证的是“正常完成一轮”通知。Codex 的 `notify` 事件不保证覆盖
> `interrupted` 或 `failed`；本项目不会把失败或取消误报为完成。

## 最省事：把文件夹交给 Codex

1. 下载 Release ZIP 并解压到一个长期不移动的目录。
2. 在 Codex 桌面应用中把解压后的文件夹作为项目打开。
3. 新建任务，发送下面这段话：

   ```text
   请按照本项目 AGENTS.md 的“首次安装”流程安装 Codex Progress Toolbox。
   从创建我自己的飞书自定义机器人开始，一步一步引导我；任何 Webhook、
   签名密钥或 API Key 都不要让我粘贴到聊天中，请让我在本机无回显终端里输入。
   每完成一步先验证，再继续下一步。
   ```

Codex 会自动读取仓库根目录的 `AGENTS.md`。这符合
[OpenAI 官方 AGENTS.md 说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)：
项目级指令会在 Codex 开始工作前加载。

飞书机器人和密钥必须由用户本人在飞书及本机终端中完成。这里的“一键安装”
指 Codex 负责检查环境、执行脚本、解释界面、验证结果和排障，不代表绕过飞书
的人工授权步骤。完整的 Codex 协作流程见 [INSTALL_WITH_CODEX.md](INSTALL_WITH_CODEX.md)。

## 手动完整安装

### 0. 准备

- Windows 10/11；
- 已安装并能运行 Codex；
- Windows PowerShell 5.1 或 PowerShell 7；
- Python 3.11+。若未找到，安装脚本会下载经 SHA-256 校验的 Python.org
  官方嵌入版到
  `%LOCALAPPDATA%\CodexProgressToolbox\Python`。

项目代码没有第三方 Python 依赖。

### 1. 创建自己的飞书机器人

建议先建一个仅自己或可信成员可见的飞书群，再在群设置中添加“自定义机器人”：

1. 打开目标群，进入群设置中的“群机器人”；
2. 选择“添加机器人” → “自定义机器人”；
3. 填写名称与说明，例如“Codex 进度通知”；
4. 在安全设置中优先开启“签名校验”，保存签名密钥；
5. 如启用关键词校验，可使用消息固定包含的 `对话名称`；
6. 完成创建，复制 Webhook URL。

Webhook URL 和签名密钥都是凭据，不要发到聊天、Issue、截图或 Git 提交中。
界面变化、安全选项和常见错误见 [飞书机器人逐步指南](docs/FEISHU.md)。

### 2. 安装 Codex 通知入口

在项目根目录打开 PowerShell：

```powershell
.\scripts\install.ps1
```

脚本会创建 `config.local.json`、本地状态目录，并将本项目入口写入用户级
Codex `config.toml`。如果原来已经配置了 `notify`，安装器会创建恢复记录，
并先异步转发原命令。

> [!NOTE]
> 项目目录会写进 Codex 配置，因此安装后不要随意移动文件夹。确需移动时，
> 在新位置重新运行安装脚本。

### 3. 安全写入飞书配置

运行：

```powershell
.\scripts\configure-feishu.ps1
```

脚本会用无回显提示读取 Webhook 和可选签名密钥。真实值保存在 Windows
用户级环境变量中；`config.local.json` 只写
`${PROGRESS_WEBHOOK_URL}` 与 `${PROGRESS_FEISHU_SECRET}` 占位符。

如果机器人没有开启签名校验，在密钥提示处直接回车即可。

### 4. 选择要监控的 Codex 对话

推荐双击：

```text
scripts\manage-threads.cmd
```

也可以列出对话后手工填写完整 ID：

```powershell
.\scripts\list-threads.ps1
```

会话管理器只修改 `config.local.json` 顶层的 `thread_ids`，保存前创建
时间戳备份；不会改动通知密钥或分类器配置。

### 5. 可选：启用语义进度摘要

没有 OpenAI API Key 时通知仍会发送，但状态显示“情况未知”，并提示打开
Codex 查看本轮结果。若要自动生成短状态和详情，可在本机配置：

```powershell
[Environment]::SetEnvironmentVariable('OPENAI_API_KEY', '填入自己的API密钥', 'User')
```

Codex 产品登录与 OpenAI API Key 是两套独立凭据。启用分类意味着把有限的
对话内容发送到 `OPENAI_BASE_URL`；不接受这一隐私边界时，将
`PROGRESS_CLASSIFIER_MODE` 设为 `disabled`。

### 6. 验证并重启

```powershell
.\scripts\validate.ps1
.\scripts\send-test.ps1 -DryRun
```

确认本地预览正确后，再显式发送一条真实飞书测试消息：

```powershell
.\scripts\send-test.ps1
```

最后完全退出并重新启动 Codex 桌面应用，让它重新读取用户级配置和环境变量。

## 配置速查

| 环境变量 | 用途 |
| --- | --- |
| `PROGRESS_THREAD_IDS` | 逗号分隔的完整 Codex 对话 ID |
| `PROGRESS_NOTIFY_PROVIDER` | `feishu`、`astrbot`、`wecom` 或 `generic` |
| `PROGRESS_WEBHOOK_URL` | 目标 Webhook；等同发送凭据 |
| `PROGRESS_FEISHU_SECRET` | 飞书自定义机器人签名密钥，可选 |
| `OPENAI_API_KEY` | 可选的语义分类 API Key |
| `PROGRESS_CLASSIFIER_MODE` | `auto`、`openai` 或 `disabled` |
| `PROGRESS_CLASSIFIER_MODEL` | 分类模型，默认 `gpt-5-mini` |
| `OPENAI_BASE_URL` | Responses API 根地址 |
| `PROGRESS_TOOLS_ROOT` | 可选运行时根目录；默认在 `%LOCALAPPDATA%` 下 |
| `PROGRESS_NOTIFY_CONFIG` | 可选的其他配置文件绝对路径 |

全部字段与占位符见 [config.example.json](config.example.json)。其他通知通道见
[AstrBot 指南](docs/ASTRBOT.md) 和
[通用 Webhook 契约](docs/WEBHOOK_SCHEMA.md)。

## 隐私与安全

仓库和 Release 不应包含：

- `config.local.json` 及其 `.bak`；
- `.state/`、日志、缓存、字节码；
- Webhook、签名密钥、API Key、真实对话 ID；
- 用户名、用户目录、固定盘符、远程服务器 IP 或 SSH 密钥；
- 真实对话内容和截图。

启用 Responses 分类时，工具会发送事件类型、标题、最多最后 8 条输入消息
（每条尾部 4000 字符）和助手回复尾部 50000 字符到配置的 API。
关闭分类后不会把助手回复塞进通知。

安全报告请遵循 [SECURITY.md](SECURITY.md)，不要在公开 Issue 中披露凭据。

## 测试、打包与卸载

```powershell
python -m unittest discover -s tests -v
.\scripts\package-share.ps1
.\scripts\uninstall.ps1
```

打包脚本按白名单复制文件、扫描本地私密值、解压复检并运行测试。卸载会恢复
安装前的 Codex `notify`；卸载后同样需要完全重启 Codex。

## 项目结构

```text
AGENTS.md                    Codex 首次安装指令
INSTALL_WITH_CODEX.md        人机协作安装流程
config.example.json          无凭据配置模板
manage-threads.pyw           中文会话管理器入口
progress-notify.py           Codex notify 稳定入口
scripts/                     安装、配置、验证、打包和卸载脚本
src/progress_notify/         标准库实现
tests/                       无网络真实外发的自动化测试
docs/                        飞书、架构、Webhook 与发布说明
```

## 许可证

[MIT License](LICENSE)。发布前如需改成其他许可证，请先理解对再分发、修改和
专利条款的影响，再替换根目录许可证文件。
