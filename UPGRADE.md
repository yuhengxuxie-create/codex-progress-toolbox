# 升级到 1.6.0

本说明对应 v1.6.0，以下流程用于完整生态的原地升级；更早的 v1.2.0 模板迁移限制见后文。

## v1.5.0 用户首次快捷升级

旧版尚无v1.6.0的内置生态更新入口。下载 upgrade-from-v1.x ZIP，将它完整解压到旧安装目录之外，核对 Release 校验值。在百宝箱托盘菜单选择“退出”，双击升级包根目录“快捷升级.cmd”，选择旧安装目录；它会先校验包并检查旧百宝箱已经退出，再调用正式升级器，完成后显示飞书配置指南路径。取消目录选择不会修改旧安装。

高级用户也可运行以下命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\verify-package.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\upgrade.ps1 -LegacyRoot '<旧生态安装目录>' -InstallRoot '<同一旧生态安装目录>'
```

也可让 Codex 打开升级包目录，按照 AGENTS.md 引导执行。不要将 full 包直接覆盖旧安装，不要只复制 EXE，不要用模板替换 config.yaml。新版安装成功后，以后的正式更新可在百宝箱“设置 → 生态更新”完成。

## 保留与回滚

升级器先检查包、目录和数据库位置，停止原来运行的后台，再创建包含程序、配置及 Codex 集成文件的事务备份。随后安装新程序和离线依赖，恢复用户数据，迁移数据库并健康检查。原本停止的后台保持停止，原本运行的后台恢复运行。

保留后端 config.yaml、.secrets、完整 .state、数据库和日志；保留百宝箱 config.json、.state、日志与自定义插件。桌面 .state 包含预警正文、已读和去重状态。同名插件采用新版内置文件，旧副本另存到安装目录 `.state/plugin-backups`，供手动比较恢复，不随成功后的事务清理删除。

v1.5.0 数据库从 schema 10 迁移到 23。失败时恢复同一次备份中的程序、配置和数据库；只换回旧 EXE 无法读取新库。默认保留事务备份，命令输出清单路径，可手工回滚：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\installer\rollback.ps1 -TransactionManifest '<事务 manifest.json>'
```

独立更新器使用 `-CleanupBackupOnSuccess` 在成功后清理事务备份；同名插件副本仍保留。如需长期保留完整旧版，首次手工升级使用上面的默认命令。回滚会恢复备份时刻，升级后新增数据不会自动合并。

自动升级只保留后端 `.state` 内的数据库及配套守护状态。数据库位于安装根内的其他自定义目录、安装根外，或数据库/守护状态使用路径链接时，均会在停止服务或覆盖程序之前拒绝继续。请先备份并安排人工迁移，不要删除数据库或仅修改配置路径绕过检查。跨电脑、跨 Windows 用户的 DPAPI 凭据不能保证直接解密。

## 飞书需要补充的设置

新版将业务与通信守护分开。升级进入维护并备份 Windows 恢复任务，成功或回滚后恢复原意图；原来停止的业务继续停止，不因旧自动重启选项被拉起。新装监督默认关闭；已有恢复设置或旧默认飞书服务的自启偏好按 [守护迁移说明](docs/WINDOWS_GUARDIAN_RECOVERY.md) 保留。普通停止保留远程救援，完整退出会关闭救援。锁屏可恢复，注销或电脑断电时不能保证运行。

继续使用原有应用与绑定。四项权限 scope 不增加；新增 `card.action.trigger` 卡片回调，使用底部菜单时配置 `application.bot.menu_v6` 与 `progress_wx_feature_center` 菜单事件键。详细步骤见 [飞书配置增量](docs/FEISHU_UPGRADE_PERMISSIONS.md)。飞书平台保存、应用审核和真实点击需由应用管理员完成。

## 更早版本

GitHub v1.2.0 Webhook 模板只迁移通过格式检查的 monitor thread IDs，不复用 Webhook、签名或旧 config.local.json。2026-08-25 完整生态保留允许的配置与状态。旧 Webhook 用户需要创建企业自建应用。更早来源的兼容能力与本轮真实 v1.5.0 升级验收分别记录，不把一条路径的结果推广到所有版本。
