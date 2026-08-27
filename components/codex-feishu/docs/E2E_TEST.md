# 后台组件验收

完整发布验收由仓库根目录 `scripts` 执行。本组件至少满足：

1. `python -m pytest` 全部通过。
2. `monitor-list` 与 `monitor-settings` 的 stdout 是纯 JSON，且 `schema_version=1`。
3. 未配置企业自建应用和 open_id 时，启动正式服务必须 fail closed。
4. 测试不读取或修改真实任务、生产监测列表、Codex 配置、飞书凭据或外部网络设置。
5. 只有显式提供隔离测试目录时，才执行安装、启动、停止、升级和回滚 E2E。

生产只读联调应单独执行，只允许 `monitor-list`、`monitor-settings` 和 `status`，禁止 `monitor-add`、`monitor-remove` 或设置变更。

