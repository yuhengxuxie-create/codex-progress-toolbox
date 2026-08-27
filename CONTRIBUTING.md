# Contributing

欢迎提交 Issue 和 Pull Request。请勿提交真实凭据、配置、open_id、任务 ID、数据库、日志、私人截图或本机绝对路径。

提交前至少运行：

```powershell
python -m pytest components\codex-feishu\tests
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\components\treasure-chest\scripts\build.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\privacy-scan.ps1
```

协议字段的 `schema_version` 与产品版本独立；不要为了发布 v2.x 任意修改 schema。改变 `monitor-*` JSON、安装路径、迁移规则或权限模型时，必须补充 E2E 和升级/回滚测试。

新增依赖必须固定版本与 SHA-256、记录许可证，并说明为什么现有标准库或依赖不能满足需求。
