using TreasureChest.Core.Models;
using TreasureChest.Core.Services;
using TreasureChest.Integrations;

var failures = new List<string>();
var total = 0;
async Task Check(string name, Func<Task> test)
{
    total++;
    try { await test(); Console.WriteLine($"PASS {name}"); }
    catch (Exception error) { failures.Add(name + ": " + error.Message); Console.WriteLine($"FAIL {name}: {error.Message}"); }
}
void Assert(bool condition, string message)
{
    if (!condition) throw new InvalidOperationException(message);
}

var root = Path.Combine(Path.GetTempPath(), "TreasureChest-SelfTest-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
try
{
    await Check("默认配置与路径", () =>
    {
        var config = AppConfiguration.CreateDefault(root);
        Assert(config.Sessions.Count == 1, "默认会话数量错误");
        Assert(!config.Tools.Any(tool => tool.Name.Contains("TUN")), "公共默认配置不得包含个人 TUN 工具");
        Assert(!config.Settings.AutoStartEnabled, "新安装不得默认开启开机自启");
        Assert(config.Sessions[0].WorkingDirectory == Path.Combine(root, "components", "FeiShuBOT"),
            "进度通知必须使用相对百宝箱根目录的组件路径");
        Assert(config.Sessions[0].StartCommand.Contains($" -ToolsRoot \"{Path.Combine(root, "components")}\""),
            "Codex 管理启动命令必须携带便携组件根目录");
        ConfigStore.Validate(config);
        return Task.CompletedTask;
    });

    await Check("配置原子保存与读取", async () =>
    {
        var store = new ConfigStore(Path.Combine(root, "config.json"), root);
        var config = await store.LoadAsync();
        config.Settings.RefreshIntervalSeconds = 7;
        await store.SaveAsync(config);
        var loaded = await store.LoadAsync();
        Assert(loaded.Settings.RefreshIntervalSeconds == 7, "配置未持久化");
        Assert(!File.Exists(Path.Combine(root, "config.json.tmp")), "残留临时文件");
    });

    await Check("工具路径引号与工作目录兼容", async () =>
    {
        var command = Path.Combine(root, "quoted tool.cmd");
        await File.WriteAllTextAsync(command, "@exit /b 0");
        Assert(ToolPath.NormalizeInput($"'{command}'") == command, "单引号路径未兼容");
        Assert(ToolPath.NormalizeInput($"'{root}'inner'{Path.DirectorySeparatorChar}tool.cmd'") ==
            $"{root}'inner'{Path.DirectorySeparatorChar}tool.cmd", "路径内部的引号被错误删除");
        Assert(ToolPath.NormalizeInput($"''{command}''") == $"'{command}'", "不应删除超过一对首尾引号");
        var store = new ConfigStore(Path.Combine(root, "quoted-tool-config.json"), root);
        var config = AppConfiguration.CreateDefault(root);
        config.Tools.Add(new ToolDefinition
        {
            Id = "quoted-tool",
            Name = "带引号工具",
            TargetPath = $"\"{command}\"",
            WorkingDirectory = $"\"{command}\"",
        });
        await store.SaveAsync(config);
        var loaded = await store.LoadAsync();
        var tool = loaded.Tools.Single(item => item.Id == "quoted-tool");
        Assert(tool.TargetPath == command, "目标路径两端的引号未清理");
        Assert(tool.WorkingDirectory == root, "脚本文件未自动转换为工作目录");

        var result = await ToolRunner.RunAsync(new ToolDefinition
        {
            Id = "legacy-quoted-tool",
            Name = "旧版带引号工具",
            TargetPath = $"\"{command}\"",
            WorkingDirectory = $"\"{command}\"",
        });
        Assert(result.Started && result.ExitCode == 0, "旧配置中的带引号脚本无法运行");
    });

    await Check("状态命令退出码", async () =>
    {
        var ok = await CommandExecutor.RunAsync("echo RUNNING & exit /b 0", root, TimeSpan.FromSeconds(3));
        var stopped = await CommandExecutor.RunAsync("echo STOPPED & exit /b 1", root, TimeSpan.FromSeconds(3));
        Assert(ok.ExitCode == 0 && ok.StandardOutput.Contains("RUNNING"), "运行状态命令异常");
        Assert(stopped.ExitCode == 1, "停止状态命令异常");
    });

    await Check("插件扫描与贡献", async () =>
    {
        var plugin = Path.Combine(root, "plugins", "valid");
        Directory.CreateDirectory(plugin);
        await File.WriteAllTextAsync(Path.Combine(plugin, "run.cmd"), "@exit /b 0");
        await File.WriteAllTextAsync(Path.Combine(plugin, "manifest.json"), """
        { "id":"valid-plugin", "name":"Valid", "version":"1.0.0", "sdk":"1",
          "contributions":{"tools":[{"id":"valid-tool","name":"Tool","entry":"run.cmd"}],"sessions":[]} }
        """);
        var catalog = new PluginCatalog(Path.Combine(root, "plugins"));
        var result = await catalog.ScanAsync();
        Assert(result.Count == 1 && result[0].Error is null && result[0].Tools.Count == 1, "有效插件未加载");
    });

    await Check("插件路径越界被拒绝", async () =>
    {
        var plugin = Path.Combine(root, "plugins", "escape");
        Directory.CreateDirectory(plugin);
        await File.WriteAllTextAsync(Path.Combine(plugin, "manifest.json"), """
        { "id":"escape-plugin", "name":"Escape", "version":"1.0.0", "sdk":"1",
          "contributions":{"tools":[{"id":"escape-tool","name":"Tool","entry":"../outside.cmd"}],"sessions":[]} }
        """);
        var catalog = new PluginCatalog(Path.Combine(root, "plugins"));
        var result = await catalog.ScanAsync();
        Assert(result.Single(item => item.Manifest.Id == "escape-plugin").Error is not null, "越界路径未被拒绝");
    });

    await Check("项目监测 CLI 契约与命令", async () =>
    {
        const string autoId = "01afffff-1111-4111-8111-111111111111";
        const string manualId = "01aeeeee-2222-4222-8222-222222222222";
        var cliRoot = Path.Combine(root, "project monitor cli");
        Directory.CreateDirectory(cliRoot);
        var fakePython = Path.Combine(cliRoot, "fake-python.cmd");
        var entry = Path.Combine(cliRoot, "progress-wx.py");
        var config = Path.Combine(cliRoot, "config.yaml");
        await File.WriteAllTextAsync(entry, "# fake entry");
        await File.WriteAllTextAsync(config, "version: 2");
        await File.WriteAllTextAsync(fakePython, """
        @echo off
        if /I "%~4"=="monitor-list" goto list
        if /I "%~4"=="monitor-add" goto mutate
        if /I "%~4"=="monitor-remove" goto mutate
        if /I "%~4"=="monitor-settings" goto settings
        exit /b 20
        :list
        echo {"schema_version":1,"items":[{"thread_id":"01afffff-1111-4111-8111-111111111111","title":"Auto task","group/project":"Test project","origin":"auto","last_activity_at":"2026-08-26T00:10:00+08:00","expires_at":"2026-08-27T00:10:00+08:00"},{"thread_id":"01aeeeee-2222-4222-8222-222222222222","title":"Manual task","project":{"name":"Personal tools"},"origin":"manual","last_activity_at":1787673600000,"expires_at":null}]}
        exit /b 0
        :mutate
        if /I not "%~5"=="--thread-id" exit /b 21
        if /I "%~6"=="" exit /b 22
        if /I not "%~7"=="--json" exit /b 23
        echo {"schema_version":1,"ok":true}
        exit /b 0
        :settings
        if /I "%~5"=="--json" (
          echo {"schema_version":1,"auto_monitoring_enabled":true,"effective_at":null}
          exit /b 0
        )
        if /I not "%~5"=="--auto-enabled" exit /b 24
        if /I not "%~7"=="--json" exit /b 25
        if "%~6"=="true" (
          echo {"schema_version":1,"auto_monitoring_enabled":true,"changed":true,"effective_at":1787727946}
          exit /b 0
        )
        if "%~6"=="false" (
          echo {"schema_version":1,"auto_monitoring_enabled":false,"changed":true,"effective_at":1787727941}
          exit /b 0
        )
        exit /b 26
        """);

        var service = new ProjectMonitorCliService(cliRoot, fakePython);
        var items = await service.ListAsync();
        Assert(items.Count == 2, "CLI 列表数量错误");
        Assert(items.Single(item => item.ThreadId == autoId).Origin == "auto", "自动来源未解析");
        Assert(items.Single(item => item.ThreadId == autoId).Classification == "Test project", "group/project 未解析");
        Assert(items.Single(item => item.ThreadId == autoId).ExpiresAt.HasValue, "自动项到期时间未解析");
        Assert(items.Single(item => item.ThreadId == manualId).IsManual, "手动来源未解析");
        Assert(items.Single(item => item.ThreadId == manualId).Classification == "Personal tools", "嵌套 project 未解析");
        await service.AddAsync("codex://threads/" + manualId);
        await service.RemoveAsync(autoId);
        var initialSettings = await service.GetSettingsAsync();
        Assert(initialSettings.AutoMonitoringEnabled && initialSettings.EffectiveAt is null, "自动监测默认设置未解析");
        var disabledSettings = await service.SetAutoMonitoringAsync(false);
        Assert(!disabledSettings.AutoMonitoringEnabled && disabledSettings.Changed == true && disabledSettings.EffectiveAt.HasValue,
            "关闭自动监测结果未解析");
        var enabledSettings = await service.SetAutoMonitoringAsync(true);
        Assert(enabledSettings.AutoMonitoringEnabled && enabledSettings.Changed == true, "开启自动监测结果未解析");
        Assert(ProjectMonitorCliService.NormalizeThreadId("'codex://threads/" + autoId + "'") == autoId, "任务 ID 规范化失败");

        var rejectedSchema = false;
        try { ProjectMonitorCliService.ParseListJson("{\"schema_version\":2,\"items\":[]}"); }
        catch (InvalidDataException) { rejectedSchema = true; }
        Assert(rejectedSchema, "未知 schema_version 未被拒绝");
        var rejectedSettings = false;
        try { ProjectMonitorCliService.ParseSettingsJson("{\"schema_version\":1,\"auto_monitoring_enabled\":\"true\"}", false); }
        catch (InvalidDataException) { rejectedSettings = true; }
        Assert(rejectedSettings, "非布尔自动监测状态未被拒绝");
    });

    await Check("可选生产项目监测 CLI 只读联调", async () =>
    {
        var progressRoot = Environment.GetEnvironmentVariable("CODEX_FEISHU_INTEGRATION_ROOT");
        if (string.IsNullOrWhiteSpace(progressRoot)) return;
        var python = Path.Combine(progressRoot, "Python313-ProgressWX", "python.exe");
        if (!File.Exists(python))
            python = Path.Combine(Path.GetDirectoryName(progressRoot) ?? progressRoot, "Python313-ProgressWX", "python.exe");
        Assert(File.Exists(python) && File.Exists(Path.Combine(progressRoot, "progress-wx.py")),
            "显式联调目录缺少后台入口或 Python");
        var service = new ProjectMonitorCliService(progressRoot, python);
        var items = await service.ListAsync();
        Assert(items.All(item => item.Origin is "manual" or "auto"), "监测列表包含未知来源");
        Assert(items.Select(item => item.ThreadId).Distinct(StringComparer.OrdinalIgnoreCase).Count() == items.Count,
            "监测列表包含重复任务 ID");
        _ = await service.GetSettingsAsync();
    });

    await Check("项目监测按来源与项目分组排序", () =>
    {
        var now = DateTimeOffset.Now;
        var records = new[]
        {
            new ProjectMonitorItem("01a00000-0000-0000-0000-000000000005", "自动个人", "个人对话", "auto", now, now.AddHours(2)),
            new ProjectMonitorItem("01a00000-0000-0000-0000-000000000003", "手动个人", "个人对话", "manual", now, null),
            new ProjectMonitorItem("01a00000-0000-0000-0000-000000000002", "项目甲较旧", "项目甲", "manual", now.AddMinutes(-5), null),
            new ProjectMonitorItem("01a00000-0000-0000-0000-000000000004", "自动项目乙", "项目乙", "auto", now, now.AddHours(2)),
            new ProjectMonitorItem("01a00000-0000-0000-0000-000000000001", "项目甲较新", "项目甲", "manual", now, null),
        };
        var ordered = ProjectMonitorPresentation.OrderMonitors(records);
        Assert(ordered.Select(item => item.Title).SequenceEqual(new[] { "项目甲较新", "项目甲较旧", "手动个人", "自动项目乙", "自动个人" }),
            "未按手动/自动、项目/个人、同项目连续规则排序");
        Assert(ProjectMonitorPresentation.GroupTitle("项目甲", 2) == "项目 · 项目甲（2）", "项目分组标题错误");
        Assert(ProjectMonitorPresentation.GroupTitle("个人对话", 3) == "个人对话（3）", "个人对话分组标题错误");
        Assert(ProjectMonitorPresentation.GroupTitle("个人会话", 4) == "个人对话（4）", "FeiShuBOT 个人会话别名未归入个人对话");
        Assert(ProjectMonitorPresentation.DrawerGroupTitle("项目甲", 2, false) == "▶  项目 · 项目甲（2）", "收起抽屉标题错误");
        Assert(ProjectMonitorPresentation.DrawerGroupTitle("个人会话", 4, true) == "▼  个人对话（4）", "展开抽屉标题错误");
        return Task.CompletedTask;
    });

    await Check("可选生产进度通知状态只读兼容", async () =>
    {
        var progressRoot = Environment.GetEnvironmentVariable("CODEX_FEISHU_INTEGRATION_ROOT");
        if (string.IsNullOrWhiteSpace(progressRoot)) return;
        var status = Path.Combine(progressRoot, "scripts", "status.ps1");
        Assert(File.Exists(status), "显式联调目录缺少状态脚本");
        var command = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{status}\"";
        var result = await CommandExecutor.RunAsync(command, progressRoot, TimeSpan.FromSeconds(12));
        Assert(!result.TimedOut && result.ExitCode is 0 or 1, "状态脚本通过命令封装后退出码异常");
        using var manager = new SessionManager(new AppLogger(Path.Combine(root, "session-test.log")));
        var session = new SessionDefinition { Name = "生产状态", StartCommand = command, StatusCommand = command, WorkingDirectory = progressRoot };
        var snapshot = await manager.RefreshOneAsync(session, allowAutoRestart: false);
        Assert(snapshot.Status == SessionStatus.Running && snapshot.Message.Contains("PID"), "结构化状态未生成可读摘要");
    });

    await Check("可选生产 Codex 会话目录只读兼容", async () =>
    {
        var progressRoot = Environment.GetEnvironmentVariable("CODEX_FEISHU_INTEGRATION_ROOT");
        if (string.IsNullOrWhiteSpace(progressRoot)) return;
        var python = Path.Combine(progressRoot, "Python313-ProgressWX", "python.exe");
        if (!File.Exists(python))
            python = Path.Combine(Path.GetDirectoryName(progressRoot) ?? progressRoot, "Python313-ProgressWX", "python.exe");
        Assert(File.Exists(python) && File.Exists(Path.Combine(progressRoot, "progress-wx.py")),
            "显式联调目录缺少后台入口或 Python");
        var catalog = await new CodexThreadCatalogService(progressRoot, python).LoadAsync();
        Assert(catalog.All(item => item.ThreadSource.Equals("user", StringComparison.OrdinalIgnoreCase)), "内部子任务被错误展示");
        Assert(catalog.Select(item => item.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() == catalog.Count, "会话目录存在重复任务 ID");
        Assert(catalog.All(item => item.UpdatedAtMs.HasValue), "会话目录缺少最近活动时间");
        Assert(catalog.Zip(catalog.Skip(1), (left, right) => left.UpdatedAtMs >= right.UpdatedAtMs).All(value => value),
            "会话目录未按最近活动时间倒序返回");
    });
}
finally
{
    try { Directory.Delete(root, true); } catch { }
}

Console.WriteLine($"RESULT {total - failures.Count}/{total} passed");
if (failures.Count > 0)
{
    foreach (var failure in failures) Console.Error.WriteLine(failure);
    Environment.ExitCode = 1;
}
