# 图文使用入口（v1.6.0，2026-09-12）

本页提供四页操作图解及文字说明。图中的按钮可直接点击，手动输入指令以[最新版文字指南](../components/codex-feishu/docs/FEISHU_USAGE.md)为准，使用行首英文半角点，例如 `.功能中心`、`.查询会话`。

## 先完成自己的机器人配置

新用户按根目录 AGENTS.md 完成安装、企业自建应用、长连接、唯一用户绑定和测试。凭据只在本机隐藏窗口填写。旧 v1.5.0 用户按 [升级说明](../UPGRADE.md) 使用升级包，并检查[卡片回调与底部菜单](FEISHU_UPGRADE_PERMISSIONS.md)，不重新创建或配对已有机器人。

## 四页操作图

![开始前提醒](../components/codex-feishu/docs/assets/feishu-usage-classroom-20260908/01-important-reminder.png)

![开始任务](../components/codex-feishu/docs/assets/feishu-usage-classroom-20260908/02-start-task-approved-v7.png)

![找任务与发送图片](../components/codex-feishu/docs/assets/feishu-usage-classroom-20260908/03-find-task-and-send-image.png)

![监测、通知与额度预警](../components/codex-feishu/docs/assets/feishu-usage-classroom-20260908/04-monitor-notifications-and-alerts-v3.png)

## 图解之后增加或完善的行为

- 任务明确交付的本机成果文件会独立回传；普通附件须非空且不超过30MB。大型安装包应经正式发布下载，不通过机器人发送。
- 文件路径必须准确。程序会统一校验路径与身份，允许同一进程内尚未提交的本地失败在本轮文件准备好后恢复；不猜文件名、不自动补发旧拒绝或未知结果。
- 等待审批时可以收到提醒。无可信远程入口时须回原任务审批框操作，回复 A 不代表批准。
- 停止业务后若通信守护仍健康在线，可发送 `.启动飞书机器人` 请求恢复。电脑须开机、原用户登录且网络可用。
- 额度预警反映公开来源的核验结果，覆盖不完整会明确提示，不代表个人账户已重置。

完整变化见 [v1.6.0说明](RELEASE_NOTES_v1.6.0.md)；文件升级边界见[后端交付与升级](../components/codex-feishu/docs/BACKEND_DELIVERY_UPGRADE.md)。
