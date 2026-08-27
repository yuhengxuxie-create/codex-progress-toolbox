# 故障排除

## 校验失败

不要绕过 `verify-package.ps1`。删除当前副本，从官方 Release 重新下载 full/upgrade 附件，并核对 SHA-256。不要从第三方补文件。

## 机器人不回消息

确认应用已发布；事件接收方式是长连接；订阅 `im.message.receive_v1`；消息来自绑定用户的一对一聊天；后台状态为 running。不要在群里测试。

## 图片失败

确认 `im:message:readonly` 与 `im:resource` 已开通并发布新版本。权限修改后重启后台再重发；不要把原图或日志上传到公开 Issue。

## Codex 没有通知

运行后台 `scripts\validate.ps1` 和 `scripts\status.ps1`。检查安装状态，不手工覆盖 Codex `config.toml` 或 `hooks.json`。外部工具修改过 notify/hooks 时，卸载器会拒绝猜测恢复，应先人工核对备份。

## 升级失败

不要删除事务备份。使用错误信息中给出的 `rollback.ps1 -TransactionManifest` 命令恢复。回滚后确认旧服务状态、Codex notify/hooks 和私人配置均与升级前一致。

## 凭据疑似泄露

立即在飞书开放平台轮换 App Secret，停止后台，重新运行首次设置并绑定；不要继续发送旧日志或截图。

