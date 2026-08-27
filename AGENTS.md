# Codex Feishu Ecosystem v1.5.0 安装指令

## 目标

当用户要求安装、升级、配置或验证本目录时，直接执行这里的流程并保持一个可见检查清单。不要只复述教程。一次只让用户完成一个飞书平台步骤，用户确认后再继续。

## 绝对安全边界

- 第一条命令必须是 `installer\verify-package.ps1`。校验失败立即停止。
- 不读取、显示、记录或要求用户在 Codex 对话中粘贴 App Secret、Webhook、签名密钥、open_id、API Key 或真实任务 ID。
- App Secret 只能由用户在 TreasureChest 本机隐藏输入窗口中输入并写入 Windows DPAPI。不要用命令行参数、环境变量、剪贴板回显或聊天传递 Secret。
- 不复制旧用户的凭据给新用户，不自动创建或冒用发布者的飞书应用。
- 不读取生产 `config.yaml`、`.secrets`、`.state`、数据库、日志或 Codex 会话正文，除非用户明确要求诊断且无更小权限方法。
- 不修改代理、WinHTTP、WinINET、路由、DNS、TUN、VPN 或系统网络环境变量。
- 不强制退出或重启 Codex，不接管桌面任务。安装 notify 后如需新任务生效，只说明这一点。
- 不在用户的飞书应用发布、唯一用户绑定和测试消息完成前启动正式通知服务。
- 新安装的开机自启默认关闭。
- 升级前必须备份；只在临时副本做破坏性测试。失败时执行自动回滚。

## 基础安装

1. 确认 Windows x64，当前目录同时包含 `components`、`payload`、`installer`、`SHA256SUMS.txt`。
2. 运行：

   ```powershell
   powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\installer\verify-package.ps1
   ```

3. 校验通过后运行：

   ```powershell
   powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\installer\install.ps1 -NonInteractive -NoLaunch
   ```

   用户指定目录时增加 `-InstallRoot "绝对路径"`。不要安装到解压目录内部、磁盘根、Windows 或 Program Files 根目录。
4. 验证安装结果：`TreasureChest.exe`、`components\codex-feishu\progress-wx.py`、`components\Python313-ProgressWX\python.exe`、`.ecosystem\installation.json` 均存在；Python 版本为 3.13.14；正式服务仍未启动。
5. 启动 `<安装目录>\TreasureChest.exe`，告知用户：“基础安装完成，等待创建你自己的飞书企业自建应用。”

## 逐步创建飞书机器人

每一小节完成后都等待用户确认，不要一次把所有步骤丢给用户。

### 第一步：创建企业自建应用

引导用户打开飞书开放平台开发者后台，在自己的企业/团队中创建“企业自建应用”。应用名称和图标由用户自行选择。强调：不是“群自定义机器人 Webhook”。

### 第二步：启用机器人能力

在应用能力中添加“机器人”。不要开启群聊能力；本项目只接受唯一绑定用户的一对一消息。

### 第三步：申请最小权限

只申请以下权限：

- `im:message:send_as_bot`：机器人主动发送消息；
- `im:message.p2p_msg:readonly`：接收一对一消息事件；
- `im:message:readonly`：读取用户消息中的图片资源；
- `im:resource`：上传并发送可预览图片。

不要申请通讯录、群管理或其他无关权限。

### 第四步：配置长连接事件

在事件与回调中选择“使用长连接接收事件”，订阅 `im.message.receive_v1`。不配置公网回调 URL。

### 第五步：创建并发布应用版本

创建版本、提交审核并在本企业发布。权限或事件修改后必须再发布一个新版本才会生效。

### 第六步：本机录入 App ID 与 Secret

让用户在 TreasureChest → 工具箱中双击“飞书进度通知首次设置”。App ID 可在本机界面输入；App Secret 必须使用隐藏输入框。禁止让用户把 Secret 发到聊天。向导用 DPAPI 保存 Secret。

### 第七步：绑定唯一飞书用户

向导生成短时一次性代码后，用户只在自己的飞书里给机器人发送该代码。后台只接受 p2p、非机器人、精确匹配的一次性代码，得到唯一 open_id 后立即结束绑定。

### 第八步：测试与启用

先发送测试消息并让用户确认手机收到。建立历史基线后再启动正式服务，检查 `status` 为 running，并确认至少一个用户选择的任务出现在监测列表。不要替用户执行真实 `monitor-add/remove`，除非用户明确选择了任务。

## 升级流程

检测到旧 v1.2.0 或旧完整生态时，不运行普通安装器，改用：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\installer\upgrade.ps1 -LegacyRoot "<旧安装目录>"
```

旧 v1.2.0 只迁移严格验证的 thread-id。Webhook、签名、API Key 和环境变量不迁移。旧完整生态保留 YAML、DPAPI、状态库、数据库和百宝箱配置。升级失败时运行安装器输出的精确 `rollback.ps1 -TransactionManifest ...` 命令。

## 完成标准

只有以下条件同时满足才报告“全部部署完成”：

- 包内哈希和 Python 官方签名通过；
- TreasureChest 能启动，后台组件与 Python 版本正确；
- 用户自己的企业自建应用已发布；
- App Secret 已通过本机隐藏输入保存且从未进入聊天/日志；
- 唯一用户完成绑定，测试消息到达；
- 后台服务运行，监测列表可读；
- Codex notify/hooks 的安装状态可恢复；
- 开机自启与用户选择一致。

若用户还没创建或发布应用，应报告“基础安装完成，等待用户创建自己的飞书机器人”，不要描述为程序故障。
