# Codex 飞书后台组件

这是 Codex Feishu Ecosystem v1.5.0 的后台公共源码，同时承载两个逻辑组件：

- Codex 管理：从飞书查看项目与会话、新建或继续任务、传递图片、处理用户级权限审批、查询额度。
- Codex 进度监测：发现和登记任务，识别完成、失败、打断、待审批与待用户输入，并向飞书发送进度。

飞书通道使用企业自建应用机器人和官方 WebSocket 长连接，不使用 v1.2.0 的群自定义机器人 Webhook。它不要求公网回调服务器，也不依赖电脑飞书客户端。

## 运行与配置

面向普通用户时，请从仓库 Release 下载 `codex-feishu-ecosystem-v1.5.0-full.zip`，把整个解压目录交给 Codex，并遵循根目录 `AGENTS.md`。不要单独运行本组件的生产安装脚本。

开发者可在本目录运行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

运行配置从 `config.example.yaml` 生成。真实 `config.yaml`、`.secrets`、`.state`、数据库和日志永远不得提交或再次打包。App Secret 只能通过本机无回显窗口写入 Windows DPAPI，不能粘贴到 Codex 对话。

组件产品版本为 1.5.0；`schema_version` 是独立的协议/状态库版本，不随产品版本重写。
