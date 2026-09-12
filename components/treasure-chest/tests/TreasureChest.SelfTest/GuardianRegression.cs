using TreasureChest.Core.Models;
using TreasureChest.Core.Services;
using TreasureChest.Integrations;
using System.Text.Json;

internal static class GuardianRegression
{
    public static void VerifyStatus(Action<bool, string> assert)
    {
        var now = DateTimeOffset.UtcNow;
        string Payload(string desired = "running", bool ready = true, bool online = true, int age = 0, bool running = true) =>
            JsonSerializer.Serialize(new { schema_version = 1, available = true, desired_state = desired,
                guardian = new { running = true, healthy = true, heartbeat_at = now.ToUnixTimeSeconds() - age },
                worker = new { running, ready, state = running ? "ready" : "stopped", heartbeat_at = now.ToUnixTimeSeconds() - age },
                channel = new { online }, maintenance = new { active = desired == "maintenance" }, last_error_code = (string?)null });
        assert(GuardianCliService.Parse(Payload(), now).Status == SessionStatus.Running, "就绪在线未识别");
        assert(GuardianCliService.Parse(Payload(ready: false), now).Status != SessionStatus.Running, "仅PID不得报启动成功");
        assert(GuardianCliService.Parse(Payload(online: false), now).Status != SessionStatus.Running, "通信离线不得报成功");
        assert(GuardianCliService.Parse(Payload(age: 40), now).Status == SessionStatus.Error, "过期心跳未拒绝");
        assert(GuardianCliService.Parse(Payload("stopped", running: false), now).Description.Contains("保留远程救援"), "主动停止救援说明缺失");
        assert(GuardianCliService.Parse(Payload("exited", running: false), now).Description.Contains("正在完整退出"), "守护仍运行时不能误报完整退出成功");
        assert(GuardianCliService.Parse(Payload("maintenance", running: false), now).Status == SessionStatus.Stopped, "维护不能显示运行");
        foreach (var invalid in new[] { Payload().Replace("\"schema_version\":1", "\"schema_version\":2"), Payload("unexpected") })
        {
            var rejected = false;
            try { GuardianCliService.Parse(invalid, now); } catch (InvalidDataException) { rejected = true; }
            assert(rejected, "不兼容守护状态未拒绝");
        }
        var service = new GuardianCliService(Path.Combine(Path.GetTempPath(), "guardian-fixture"), "unused");
        assert(!service.Handles(new SessionDefinition { WorkingDirectory = Path.Combine(Path.GetTempPath(), "guardian-fixture"), SourcePluginId = "custom" }), "插件不得被劫持为机器人");
        var backend = Path.Combine(Path.GetTempPath(), "guardian-fixture");
        var shipped = new SessionDefinition { WorkingDirectory = backend, Name = "用户自定义的机器人名称" };
        string Script(string verb) => $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{Path.Combine(backend, "scripts", verb + ".ps1")}\" -ToolsRoot \"{backend}\"";
        shipped.StartCommand = Script("start"); shipped.StopCommand = Script("stop"); shipped.StatusCommand = Script("status");
        assert(service.Handles(shipped), "已知机器人命令应支持自定义显示名称");
        shipped.StartCommand = "echo custom";
        assert(!service.Handles(shipped), "同目录自定义服务不能被守护接管");
        var absent = Payload("exited", running: false).Replace("\"available\":true", "\"available\":false")
            .Replace("\"guardian\":{\"running\":true", "\"guardian\":{\"running\":false");
        assert(GuardianCliService.Parse(absent, now).Status == SessionStatus.Stopped &&
            GuardianCliService.Parse(absent, now).Description.Contains("可点击启动"), "首次未初始化状态必须允许明确启动");
        var backendFixture = Environment.GetEnvironmentVariable("TREASURECHEST_GUARDIAN_FIXTURE_PATH");
        var circuitJson = Payload()[..^1] + ",\"windows_supervisor\":{\"circuit_open\":true,\"attempts\":3,\"last_error_code\":\"recovery_budget_exhausted\",\"next_attempt_at\":2000000000}}";
        var circuitStatus = GuardianCliService.Parse(circuitJson, now);
        assert(circuitStatus.Description.Contains("自动恢复已暂停") && circuitStatus.Description.Contains("手动恢复") &&
            circuitStatus.Description.Contains("recovery_budget_exhausted") && !circuitStatus.Description.Contains("下次恢复检查"),
            "熔断必须明确人工维护且不能许诺下一次自动恢复");
        assert(circuitStatus.Status == SessionStatus.Running, "恢复熔断不应掩盖仍就绪的实际业务状态");
        assert(GuardianCliService.Parse(Payload()[..^1] + ",\"windows_supervisor\":null}", now).SupervisorDescription == "", "旧监督字段null未兼容");
        assert(GuardianCliService.Parse(Payload()[..^1] + ",\"windows_supervisor\":{}}", now).Status == SessionStatus.Running, "监督可选字段缺失不得破坏业务状态");
        if (!string.IsNullOrWhiteSpace(backendFixture))
        {
            var actual = GuardianCliService.Parse(File.ReadAllText(backendFixture), now);
            assert(!actual.Available && actual.Status == SessionStatus.Stopped && actual.Description.Contains("可点击启动"),
                "真实后端未初始化JSON与桌面解析不兼容");
        }
    }
    public static async Task VerifyOwnershipAsync(string root, Action<bool, string> assert)
    {
        var controller = new FakeController();
        var session = new SessionDefinition { Id = "guardian-test", Name = "机器人", Enabled = true,
            AutoStart = true, AutoRestart = true, MaxRestartAttempts = 20,
            StartCommand = "exit /b 91", StopCommand = "exit /b 92", StatusCommand = "exit /b 93" };
        var logger = new AppLogger(Path.Combine(root, "guardian-test.log"));
        using (var manager = new SessionManager(logger, controller))
        {
            await manager.StartAutoStartSessionsAsync([session]);
            assert(controller.Starts == 0, "桌面自启不能覆盖守护的停止意图");
            await manager.StartAsync(session);
            assert(controller.Starts == 1 && manager.GetSnapshot(session).Status == SessionStatus.Running,
                "显式启动没有交给控制器");
            controller.Status = SessionStatus.Stopped;
            await manager.RefreshAllAsync([session]);
            await manager.StartAsync(session, automatic: true);
            assert(controller.Starts == 1, "桌面不得竞争自动恢复权");
            await manager.StopAsync(session);
            assert(controller.Stops == 1 && manager.GetSnapshot(session).Status == SessionStatus.Stopped,
                "显式停止没有交给控制器");
        }
        using (var reopened = new SessionManager(logger, controller))
        {
            await reopened.StartAutoStartSessionsAsync([session]);
            await reopened.RefreshAllAsync([session]);
            assert(controller.Starts == 1, "桌面重建后重新拉起了已停止业务");
            var ordinary = new SessionDefinition { Id = "ordinary", Name = "普通服务", StatusCommand = "exit /b 0" };
            await reopened.RefreshAllAsync([ordinary]);
            assert(!reopened.IsExternallyControlled(ordinary) && reopened.GetSnapshot(ordinary).Status == SessionStatus.Running,
                "普通服务状态命令受到守护接入影响");
        }
    }

    private sealed class FakeController : IExternalSessionController
    {
        public int Starts, Stops;
        public SessionStatus Status = SessionStatus.Stopped;
        public bool Handles(SessionDefinition session) => session.Id == "guardian-test";
        public bool ShouldStop(SessionDefinition session) => Status == SessionStatus.Running;
        public Task<SessionSnapshot> ProbeAsync(SessionDefinition session, CancellationToken cancellationToken) =>
            Task.FromResult(new SessionSnapshot(session.Id, Status, "模拟守护状态", 0, DateTimeOffset.Now));
        public Task StartAsync(SessionDefinition session, CancellationToken cancellationToken)
        { Starts++; Status = SessionStatus.Running; return Task.CompletedTask; }
        public Task StopAsync(SessionDefinition session, CancellationToken cancellationToken)
        { Stops++; Status = SessionStatus.Stopped; return Task.CompletedTask; }
        public Task ExitAsync(CancellationToken cancellationToken) { Status = SessionStatus.Stopped; return Task.CompletedTask; }
    }
}
