# TreasureChest 公共组件本地验收记录

状态：**Unreleased / 未发布**  
核验日期：2026-08-29

本文件只记录公共组件可复核证据，不包含生产 PID、日志计数、备份目录、用户会话、机器绝对路径或私人截图。

## 应用图标冻结映射

- `resources/app.png`：用户提供的优化版 1254×1254、24bpp PNG 原图逐字节副本；大小 1,696,172 字节，SHA-256 `2A60AD3825484022D9940C48DDD702BDC293A79292D777CD06899269830EB1F6`。
- `resources/app.ico`：大小 160,393 字节，SHA-256 `CE9512E0DEBE03B8BF59CCF53D863D308FFC2EE20BB92AF3E413F191A579DE11`；固定包含 16、20、24、32、40、48、64、128、256 像素九层 32 位 PNG。
- `scripts/IconGenerator/Program.cs` 与生产冻结源逐字一致；生成时验证源图和 ICO，发布后只读检查候选 EXE 的 `RT_GROUP_ICON`。
- 公共 `scripts/build.ps1` 保留 `-DotNetPath`、版本参数和 Windows PowerShell 5.1 支持，同时映射生产的固定源图哈希及 EXE 图标反验门禁。
- 公共 SelfTest 保留合成数据、相对路径和可选生产只读包装，并新增原图哈希、尺寸、像素格式、ICO 九层、色深、PNG payload 与实际尺寸断言。

## 主题与搜索隔离映射

- `UiTheme`、`DrawerRowPresentation`、`CaptionButton` 和新增 `ThemedProgressBar` 映射生产冻结主题；公开组件只使用相对资源路径，不复制生产截图、备份或运行状态。
- 普通启动、`--startup` 和用户搜索显式 `persistent`；仅 `--qa-session-search-ephemeral` 首次启动链贯穿 `ephemeral`，标题显示“隔离 QA”。请求正文继续通过独立 request.json 传递。
- 2026-08-29 两次误用默认持久搜索曾刷新约 69 条证据缓存并由旧逻辑删除 79 条旧内容哈希判断；无运行前一致性备份，不能声称已经恢复，也没有部分恢复或 Luna 重建。消息、监测、会话与管理数据未丢失。
- 本轮公共验收只允许预置取消的 `ephemeral` 探针；必须在前后核对生产缓存表计数与确定性全表哈希完全相同、临时副本为 0、Luna 调用为 0。

## 本地公共构建证据

- Windows PowerShell 5.1 + .NET SDK 8.0.424：编译 0 warning / 0 error，源 PNG、九层 ICO 与发布 EXE `RT_GROUP_ICON` 反验通过。
- 默认匿名自测：25/25；未设置生产根目录的三项只读包装按设计显示 SKIP。显式生产只读兼容：25/25；只读取公开 CLI、服务状态和会话目录契约，不执行搜索、monitor add/remove 或配置写入。
- FeiShuBOT 公共组件强制导入公共 `src` 后，冻结相关定向测试 183/183、完整 pytest 443/443。
- `build/publish/TreasureChest.exe`：74,103,833 字节，SHA-256 `4421F9E320E03F537C07264E08C61E495A321776EB8182563B4DCB0421EE44D2`；`RT_GROUP_ICON` 九层反验通过。
- `build/updater-publish/Ecosystem.Updater.exe`：71,609,506 字节，SHA-256 `B9CF58B224D6D6FBFDD4476264005283E764D9A83B0684CDAA2656AED5AF78DD`。Updater 项目不声明主程序 `ApplicationIcon`，保持独立事务更新器身份。
- 预置取消 `ephemeral` 探针退出 3，progress 为“【隔离临时缓存】搜索已取消”；生产 cache 前后均为 69 行且确定性全表 SHA-256 均为 `C4B91BBC6609D7CB5BA399D2B30A9D8380469FCA9BBC40E6B97F47C607DFF239`，judgments 前后均为 0 行且 SHA-256 均为 `4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945`。没有新增临时副本；预取消发生在 Luna 调用前。

公共 EXE 使用可迁移 `AppPaths` 包装，因此不要求与生产机 EXE 字节相同；图标源、九层 ICO 和主 EXE 图标资源契约必须一致。

## 发布边界

以上均为本地公共工作树证据。尚未 commit、push、创建版本标签、GitHub Release 或上传附件；公开 Latest 仍是 v1.5.0。
