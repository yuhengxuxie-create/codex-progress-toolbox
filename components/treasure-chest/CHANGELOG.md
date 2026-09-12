# TreasureChest 变更日志

## Unreleased / 未发布

- 以应用图标真实色彩为参考建立瓷白、深海军蓝、薰衣草紫和灰紫集中主题；新增 `ThemedProgressBar`，并覆盖主窗、管理、设置、搜索、更新、编辑和托盘界面，不重新绘制或修改原图。
- 抽屉继续保持展开组头、未展开组头、普通子行、选中子行和末子行 1 设备像素分隔的既有语义；主题化不改变 DPI、标题按钮、排序、滚动、双击或窗口框架契约。
- 普通启动、开机启动和真实用户搜索显式使用 `persistent`；只有 `--qa-session-search-ephemeral` 首次启动链把 MainForm、管理窗、搜索窗与扩大范围切换到 `ephemeral`，并明确显示“隔离 QA”。查询正文仍只进入 request.json，不进入命令行。
- 记录 2026-08-29 首次流程错误：两次 UI 验收误用默认持久模式，约 69 条证据缓存被刷新，旧逻辑删除 79 条旧内容哈希判断；运行前没有一致性数据库备份，无法完整安全恢复，没有部分恢复或 Luna 重建。消息、监测、会话与管理数据未丢失。
- 公共自测新增主题颜色、禁用态、嵌套输入框、按钮对比度、自绘进度条，以及 persistent/ephemeral 启动链、参数隔离、进度前缀和未知模式失败关闭回归。
- 使用用户提供的优化版 1254×1254、24bpp 原始 PNG 更新 TreasureChest 主程序图标；公共 `resources/app.png` 与冻结源逐字节一致，大小 1,696,172 字节，SHA-256 为 `2A60AD3825484022D9940C48DDD702BDC293A79292D777CD06899269830EB1F6`，没有 AI 重绘、裁切或改色；生成的 `resources/app.ico` SHA-256 为 `CE9512E0DEBE03B8BF59CCF53D863D308FFC2EE20BB92AF3E413F191A579DE11`。
- 图标生成器固定输出 16、20、24、32、40、48、64、128、256 像素九层 32 位 PNG ICO，并校验目录、色深、payload 与实际尺寸。
- 公共构建入口保留 Windows PowerShell 5.1、`-DotNetPath` 和版本参数支持，同时增加源 PNG SHA-256 失败关闭门禁及发布 EXE `RT_GROUP_ICON` 九层反验。
- 主程序 PE、WinForms 窗口、Alt-Tab/任务栏、托盘和安装快捷方式继续共用 `resources/app.ico`；分享/升级载荷复制完整 `resources`。独立 `Ecosystem.Updater.exe` 保持独立身份，没有套用主程序图标。
- 新增公开自测，验证嵌入 PNG 的哈希、1254×1254 尺寸与 24bpp 像素格式，以及 ICO 九层顺序、32 位色深、PNG 压缩和每层实际尺寸。

本节仅记录本地公共工作树成果；尚未 commit、push、打标签或发布 GitHub Release。
