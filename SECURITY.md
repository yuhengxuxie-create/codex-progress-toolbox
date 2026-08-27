# Security Policy

## 支持版本

安全修复面向最新 v2.x。v1.2.0 仅作为升级来源保留，不再推荐新安装。

## 凭据与本地数据

- App Secret 使用 Windows DPAPI，仅当前 Windows 用户可解密。
- Secret 不进入 YAML、命令行、环境变量、日志、截图、Git 或 Release 包。
- `config.yaml`、`config.json`、`.secrets`、`.state`、数据库、日志和 Codex 会话不是公开源码。
- 唯一 open_id、App ID 和消息/任务 ID 均不得写入模板或测试夹具。

## 飞书边界

企业自建应用只申请发送机器人消息、读取 p2p 消息、读取消息图片与上传图片所需权限。群聊固定禁用；入站消息必须通过唯一 open_id、p2p、message_id、引用上下文、签名和幂等校验。

## Codex 集成边界

安装器只修改顶层 `notify` 和 `hooks.json` 中本工具的 `PermissionRequest` 条目。安装前快照，写入采用原子操作，卸载只在当前值仍可证明属于本工具时恢复。可复用审批规则只有在 Codex 给出 `prefix_rule` 且 `codex execpolicy check` 返回 allow 后才写入独立规则文件。

## 网络与进程

本项目不修改代理、DNS、路由、TUN、VPN，不启动公网回调服务器，不要求电脑飞书客户端。飞书 WebSocket 与 API 流量会到飞书官方服务；可选外部摘要默认不需要启用。

## 报告漏洞

请通过 GitHub Security Advisory 私密报告。不要在公开 Issue 中附带 Secret、open_id、任务 ID、日志、配置、对话截图或数据库。若凭据疑似泄露，先在飞书开放平台轮换 Secret，再继续排查。
