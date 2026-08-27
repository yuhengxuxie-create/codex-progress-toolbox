# 升级到 v1.5.0

v1.5.0 是配置架构不兼容的生态升级，专用升级包支持两类旧用户。不要用 full 包直接覆盖旧目录。

## 支持的来源

### GitHub v1.2.0 单体模板

识别特征：`config.example.json`、`progress-notify.py`、`src/progress_notify`。旧版使用群自定义机器人 Webhook。

迁移规则：

- 只读取 `config.local.json` 中 `thread_ids` 的值；必须逐个通过完整任务 ID 格式校验。
- 合法 ID 写入新版模板的 `monitor.ids`。
- Webhook、签名密钥、Bearer token、headers、外部摘要 API Key、环境变量和日志都不迁移。
- 原目录及配置进入时间戳备份；升级后用户必须创建企业自建应用并重新绑定唯一 open_id。

### 2026-08-25 旧完整生态

识别特征：`PACKAGE_VERSION.txt`、`payload/ProgressChecking(WX)`，或已安装目录中的 `components/ProgressChecking(WX)` 与 `TreasureChest.exe`。

迁移规则：

- 原样保留后台 `config.yaml`、`.secrets`、`.state`、数据库和日志；不打印内容。
- 原样保留 TreasureChest `config.json`。
- 新代码安装到 `components/codex-feishu`；旧组件在成功健康检查前不删除。
- 状态库由程序按自身 schema 迁移，产品版本 1.5.0 不改写 `schema_version`。

## 标准命令

先完整解压 upgrade 包，再运行：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\installer\verify-package.ps1
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\installer\upgrade.ps1 -LegacyRoot '<旧安装或项目目录>'
```

若目标安装目录不同，增加 `-InstallRoot '<新目录>'`。升级器输出事务清单路径，出现失败会自动执行回滚；也可人工运行：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\installer\rollback.ps1 -TransactionManifest '<事务 manifest.json>'
```

## 升级顺序

1. 验证升级包哈希、结构、无路径穿越/重解析点，并验证 Python 官方签名。
2. 精确识别旧来源；未知目录拒绝继续。
3. 停止旧后台；无法安全停止时不覆盖。
4. 在安装目录外建立时间戳完整备份，并快照 Codex `config.toml` 与 `hooks.json` 的存在状态和内容。
5. 安装 v2 公共代码、TreasureChest 与锁定依赖。
6. 按来源迁移允许的数据，保留所有旧私人数据。
7. 安装/升级本工具的 notify 和 PermissionRequest Hook，不替换其他工具条目。
8. 运行配置校验、依赖导入、`status` 和 fail-closed 健康检查。
9. 仅在用户已完成企业应用配置和绑定时启动正式服务。
10. 任一步失败，恢复安装目录、notify/hooks 及原服务状态。

## 升级后的必要人工步骤

旧 v1.2.0 用户必须在飞书开放平台：创建企业自建应用、启用机器人、开通最小权限、配置长连接事件、发布版本、在本机隐藏输入 App ID/App Secret、绑定自己的 open_id。旧 Webhook 不能复用。

旧完整生态用户如原应用权限不足，至少补充 `im:message:readonly`、`im:resource` 和 `im.message.receive_v1`，发布新版本后重启后台。

## 旧版本保留

不要删除 GitHub v1.2.0 Release。它是可审计的升级来源。v1.5.0 发布后应在旧 Release 顶部标注“已被 v1.5.0 取代”，并链接本升级说明。
