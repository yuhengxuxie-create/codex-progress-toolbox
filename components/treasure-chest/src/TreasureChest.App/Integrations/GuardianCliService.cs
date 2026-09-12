using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using TreasureChest.Core.Models;
using TreasureChest.Core.Services;

namespace TreasureChest.Integrations;

public sealed record GuardianStatus(string DesiredState, bool GuardianHealthy, bool WorkerReady,
    bool WorkerRunning, bool ChannelOnline, bool Maintenance, string WorkerState, string? ErrorCode,
    bool Available = true, bool GuardianRunning = true, string SupervisorDescription = "")
{
    public SessionStatus Status => !Available && !WorkerRunning && !GuardianRunning ? SessionStatus.Stopped
        : DesiredState == "exited" && !WorkerRunning && !GuardianRunning ? SessionStatus.Stopped
        : !GuardianHealthy ? SessionStatus.Error
        : DesiredState is "stopped" or "maintenance" && !WorkerRunning ? SessionStatus.Stopped
        : DesiredState == "running" && WorkerReady && ChannelOnline ? SessionStatus.Running
        : WorkerState == "starting" ? SessionStatus.Unknown : SessionStatus.Error;
    public string Description => (!Available ? "通信守护状态尚未初始化或读取失败；可点击启动，若失败请检查后端日志。" :
        $"通信守护：{(GuardianHealthy ? "健康" : "离线或心跳异常")}；飞书：{(ChannelOnline ? "在线" : "离线")}；业务：{WorkerState switch { "ready" => WorkerReady ? "就绪" : "就绪未确认或心跳过期", "starting" => "启动中", "stopped" => "已停止", "unresponsive" => "无响应", _ => "故障" }}；" +
        (DesiredState switch { "stopped" => WorkerRunning ? "正在停止，保留远程救援" : "已按要求停止，保留远程救援", "maintenance" => "维护中，暂停远程启动", "exited" => GuardianRunning || WorkerRunning ? "正在完整退出，远程救援关闭中" : "已完整退出，远程救援不可用", _ => "请求运行" }) +
        (string.IsNullOrWhiteSpace(ErrorCode) ? "" : "；错误：" + ErrorCode)) + SupervisorDescription;
}

public sealed class GuardianCliService : IExternalSessionController
{
    private readonly string _root, _python;
    private readonly ConcurrentDictionary<string, GuardianStatus> _states = new();
    public GuardianCliService(string root, string python) { _root = Path.GetFullPath(root); _python = python; }
    public bool Handles(SessionDefinition session)
    {
        if (!string.IsNullOrEmpty(session.SourcePluginId) || string.IsNullOrWhiteSpace(session.WorkingDirectory)) return false;
        try { return Path.TrimEndingDirectorySeparator(Path.GetFullPath(session.WorkingDirectory))
            .Equals(Path.TrimEndingDirectorySeparator(_root), StringComparison.OrdinalIgnoreCase) &&
            KnownCommand(session.StartCommand, "start") && KnownCommand(session.StopCommand, "stop") && KnownCommand(session.StatusCommand, "status"); }
        catch (Exception e) when (e is ArgumentException or NotSupportedException or PathTooLongException) { return false; }
    }
    private bool KnownCommand(string command, string verb)
    {
        // Recognize the shipped script contract, never arbitrary services sharing this directory.
        foreach (var toolsRoot in new[] { _root, Path.GetDirectoryName(_root)! })
        {
            var expected = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{Path.Combine(_root, "scripts", verb + ".ps1")}\" -ToolsRoot \"{toolsRoot}\"";
            if (command.Trim().Equals(expected, StringComparison.OrdinalIgnoreCase)) return true;
        }
        return false;
    }
    public bool ShouldStop(SessionDefinition session) => _states.TryGetValue(session.Id, out var state) &&
        (state.DesiredState == "running" || state.WorkerRunning);
    public async Task<SessionSnapshot> ProbeAsync(SessionDefinition session, CancellationToken cancellationToken)
    {
        var result = await RunAsync("guardian-status", true, TimeSpan.FromSeconds(10), cancellationToken);
        if (result.ExitCode != 0) throw new InvalidOperationException("无法读取通信守护状态，请确认后端已升级并查看日志。");
        var state = Parse(result.StandardOutput, DateTimeOffset.UtcNow);
        _states[session.Id] = state;
        return new(session.Id, state.Status, state.Description, 0, DateTimeOffset.Now);
    }
    public Task StartAsync(SessionDefinition session, CancellationToken cancellationToken) => ControlAsync("start", cancellationToken);
    public Task StopAsync(SessionDefinition session, CancellationToken cancellationToken) => ControlAsync("stop", cancellationToken);
    public Task ExitAsync(CancellationToken cancellationToken = default) => ControlAsync("guardian-stop", cancellationToken);
    private async Task ControlAsync(string command, CancellationToken cancellationToken)
    {
        var result = await RunAsync(command, false, TimeSpan.FromSeconds(120), cancellationToken);
        if (result.ExitCode != 0) throw new InvalidOperationException($"守护控制未完成（{command}，退出码 {result.ExitCode}）。请刷新状态并查看后端日志；不会改用旧命令或强制结束业务。");
    }
    private async Task<CommandResult> RunAsync(string command, bool json, TimeSpan timeout, CancellationToken token)
    {
        var info = new ProcessStartInfo(_python) { WorkingDirectory = _root, UseShellExecute = false,
            CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8 };
        foreach (var arg in new[] { Path.Combine(_root, "progress-wx.py"), "--config", Path.Combine(_root, "config.yaml"), command })
            info.ArgumentList.Add(arg);
        if (json) info.ArgumentList.Add("--json");
        using var process = Process.Start(info) ?? throw new IOException("无法启动守护控制命令。");
        var stdout = process.StandardOutput.ReadToEndAsync(token);
        var stderr = process.StandardError.ReadToEndAsync(token);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(timeout);
        try { await process.WaitForExitAsync(deadline.Token); }
        catch (OperationCanceledException)
        {
            // Only cancel this short-lived CLI. Never kill its guardian/worker descendants.
            try { process.Kill(entireProcessTree: false); } catch (InvalidOperationException) { }
            throw new IOException("守护命令等待已结束，实际结果尚未确认，请刷新状态。");
        }
        return new(process.ExitCode, await stdout, await stderr, false);
    }
    public static GuardianStatus Parse(string json, DateTimeOffset now)
    {
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        if (root.GetProperty("schema_version").GetInt32() != 1)
            throw new InvalidDataException("通信守护状态版本不支持。");
        var available = root.GetProperty("available").GetBoolean();
        var desired = root.GetProperty("desired_state").GetString();
        if (desired is not ("running" or "stopped" or "maintenance" or "exited")) throw new InvalidDataException("未知守护运行意图。");
        var guardian = root.GetProperty("guardian"); var worker = root.GetProperty("worker");
        var state = worker.GetProperty("state").GetString();
        if (state is not ("starting" or "ready" or "stopped" or "failed" or "unresponsive")) throw new InvalidDataException("未知业务状态。");
        static bool Fresh(JsonElement element, DateTimeOffset current, double maxAge) =>
            element.TryGetProperty("heartbeat_at", out var heartbeat) && heartbeat.ValueKind == JsonValueKind.Number && heartbeat.TryGetDouble(out var unix) &&
            double.IsFinite(unix) && current.ToUnixTimeMilliseconds() / 1000d - unix is var age && age >= -10 && age <= maxAge;
        var running = worker.GetProperty("running").GetBoolean();
        var healthy = guardian.GetProperty("running").GetBoolean() && guardian.GetProperty("healthy").GetBoolean() && Fresh(guardian, now, 15);
        var ready = running && worker.GetProperty("ready").GetBoolean() && state == "ready" && Fresh(worker, now, 30);
        var maintenance = root.GetProperty("maintenance").GetProperty("active").GetBoolean();
        if (maintenance && desired != "maintenance") throw new InvalidDataException("维护状态与运行意图不一致。");
        return new(desired, healthy, ready, running, root.GetProperty("channel").GetProperty("online").GetBoolean(),
            maintenance, state, root.TryGetProperty("last_error_code", out var error) && error.ValueKind == JsonValueKind.String ? error.GetString() : null,
            available, guardian.GetProperty("running").GetBoolean(), DescribeSupervisor(root));
    }
    private static string DescribeSupervisor(JsonElement root)
    {
        if (!root.TryGetProperty("windows_supervisor", out var supervisor) || supervisor.ValueKind == JsonValueKind.Null)
            return string.Empty;
        if (supervisor.ValueKind != JsonValueKind.Object) return "；Windows 自动恢复状态不可读，请检查日志";
        var parts = new List<string>();
        if (supervisor.TryGetProperty("circuit_open", out var circuit) && circuit.ValueKind == JsonValueKind.True)
            parts.Add("Windows 自动恢复已暂停（熔断）；请检查日志后手动恢复，启动业务不会解除熔断");
        if (supervisor.TryGetProperty("attempts", out var attempts) && attempts.ValueKind == JsonValueKind.Number &&
            attempts.TryGetInt32(out var count) && count >= 0) parts.Add($"恢复尝试 {count} 次");
        if (supervisor.TryGetProperty("last_error_code", out var error) && error.ValueKind == JsonValueKind.String &&
            !string.IsNullOrWhiteSpace(error.GetString())) parts.Add("监督错误：" + error.GetString());
        if (supervisor.TryGetProperty("next_attempt_at", out var next) && next.ValueKind == JsonValueKind.Number &&
            next.TryGetDouble(out var unix) && double.IsFinite(unix) && unix >= 0 && unix <= 253402300799 &&
            !(supervisor.TryGetProperty("circuit_open", out var open) && open.ValueKind == JsonValueKind.True))
            parts.Add("下次恢复检查 " + DateTimeOffset.FromUnixTimeSeconds((long)unix).ToLocalTime().ToString("MM-dd HH:mm:ss"));
        return parts.Count == 0 ? string.Empty : "；" + string.Join("；", parts);
    }
}
