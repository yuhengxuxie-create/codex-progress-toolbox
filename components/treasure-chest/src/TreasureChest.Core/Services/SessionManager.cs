using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using TreasureChest.Core.Models;

namespace TreasureChest.Core.Services;

public sealed class SessionStateChangedEventArgs : EventArgs
{
    public required SessionDefinition Session { get; init; }
    public required SessionSnapshot Previous { get; init; }
    public required SessionSnapshot Current { get; init; }
}

public sealed class SessionManager : IDisposable
{
    private sealed class Runtime
    {
        public Process? Process;
        public SessionSnapshot Snapshot = new(string.Empty, SessionStatus.Unknown, "尚未检查", 0, DateTimeOffset.MinValue);
        public bool ManualStop;
        public bool WasRunning;
        public int RestartAttempts;
    }

    private readonly ConcurrentDictionary<string, Runtime> _runtimes = new(StringComparer.OrdinalIgnoreCase);
    private readonly SemaphoreSlim _operationGate = new(1, 1);
    private readonly AppLogger _logger;
    private bool _disposed;

    public SessionManager(AppLogger logger) => _logger = logger;
    public bool IsPaused { get; set; }
    public event EventHandler<SessionStateChangedEventArgs>? StateChanged;

    public SessionSnapshot GetSnapshot(SessionDefinition session) =>
        GetRuntime(session).Snapshot with { SessionId = session.Id };

    public async Task RefreshAllAsync(IEnumerable<SessionDefinition> sessions, CancellationToken cancellationToken = default)
    {
        if (IsPaused) return;
        foreach (var session in sessions.ToArray())
        {
            cancellationToken.ThrowIfCancellationRequested();
            await RefreshOneAsync(session, allowAutoRestart: true, cancellationToken).ConfigureAwait(false);
        }
    }

    public async Task StartAutoStartSessionsAsync(IEnumerable<SessionDefinition> sessions, CancellationToken cancellationToken = default)
    {
        foreach (var session in sessions.Where(item => item.Enabled && item.AutoStart))
        {
            if ((await ProbeAsync(session, cancellationToken).ConfigureAwait(false)).Status != SessionStatus.Running)
                await StartAsync(session, automatic: false, cancellationToken).ConfigureAwait(false);
        }
    }

    public async Task<SessionSnapshot> StartAsync(SessionDefinition session, bool automatic = false, CancellationToken cancellationToken = default)
    {
        await _operationGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (!session.Enabled) throw new InvalidOperationException("会话已禁用，请先在编辑中启用。");
            var runtime = GetRuntime(session);
            runtime.ManualStop = false;
            var current = await ProbeAsync(session, cancellationToken).ConfigureAwait(false);
            if (current.Status == SessionStatus.Running) return SetSnapshot(session, current);
            runtime.Process?.Dispose();
            runtime.Process = CommandExecutor.StartManaged(session.StartCommand, session.WorkingDirectory, session.LogFilePath);
            session.LastExecutedAt = DateTimeOffset.Now;
            if (automatic) runtime.RestartAttempts++;
            _logger.Info($"启动会话：{session.Name}，自动={automatic}，重试={runtime.RestartAttempts}");
            await Task.Delay(700, cancellationToken).ConfigureAwait(false);
            return await RefreshOneAsync(session, allowAutoRestart: false, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _operationGate.Release();
        }
    }

    public async Task<SessionSnapshot> StopAsync(SessionDefinition session, CancellationToken cancellationToken = default)
    {
        await _operationGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var runtime = GetRuntime(session);
            runtime.ManualStop = true;
            if (!string.IsNullOrWhiteSpace(session.StopCommand))
            {
                var result = await CommandExecutor.RunAsync(session.StopCommand, session.WorkingDirectory, TimeSpan.FromSeconds(30), cancellationToken)
                    .ConfigureAwait(false);
                if (result.TimedOut || result.ExitCode > 1)
                    throw new InvalidOperationException($"停止命令失败，退出码 {result.ExitCode}。");
            }
            else if (runtime.Process is { HasExited: false })
            {
                runtime.Process.Kill(entireProcessTree: true);
                await runtime.Process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
            }
            runtime.RestartAttempts = 0;
            _logger.Info($"停止会话：{session.Name}");
            await Task.Delay(300, cancellationToken).ConfigureAwait(false);
            return await RefreshOneAsync(session, allowAutoRestart: false, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _operationGate.Release();
        }
    }

    public async Task<SessionSnapshot> RefreshOneAsync(SessionDefinition session, bool allowAutoRestart, CancellationToken cancellationToken = default)
    {
        var runtime = GetRuntime(session);
        var previous = runtime.Snapshot;
        var current = await ProbeAsync(session, cancellationToken).ConfigureAwait(false);
        if (current.Status == SessionStatus.Running)
        {
            runtime.WasRunning = true;
        }
        else if (allowAutoRestart && runtime.WasRunning && !runtime.ManualStop && session.Enabled && session.AutoRestart
                 && runtime.RestartAttempts < session.MaxRestartAttempts)
        {
            SetSnapshot(session, current);
            try
            {
                return await StartAsync(session, automatic: true, cancellationToken).ConfigureAwait(false);
            }
            catch (Exception error)
            {
                _logger.Error($"自动重启失败：{session.Name}，{error.GetType().Name}");
                current = new SessionSnapshot(session.Id, SessionStatus.Error, "自动重启失败：" + error.Message,
                    runtime.RestartAttempts, DateTimeOffset.Now);
            }
        }
        var result = SetSnapshot(session, current);
        if (previous.Status == SessionStatus.Unknown && result.Status == SessionStatus.Stopped)
            return result;
        return result;
    }

    private async Task<SessionSnapshot> ProbeAsync(SessionDefinition session, CancellationToken cancellationToken)
    {
        var runtime = GetRuntime(session);
        if (!session.Enabled)
            return new SessionSnapshot(session.Id, SessionStatus.Disabled, "已禁用", runtime.RestartAttempts, DateTimeOffset.Now);
        try
        {
            if (!string.IsNullOrWhiteSpace(session.StatusCommand))
            {
                var result = await CommandExecutor.RunAsync(session.StatusCommand, session.WorkingDirectory, TimeSpan.FromSeconds(8), cancellationToken)
                    .ConfigureAwait(false);
                var detail = SummarizeOutput(result.StandardOutput, result.StandardError);
                if (result.TimedOut) return new(session.Id, SessionStatus.Error, "状态检查超时", runtime.RestartAttempts, DateTimeOffset.Now);
                return result.ExitCode switch
                {
                    0 => new(session.Id, SessionStatus.Running, detail ?? "运行中", runtime.RestartAttempts, DateTimeOffset.Now),
                    1 => new(session.Id, SessionStatus.Stopped, detail ?? "已停止", runtime.RestartAttempts, DateTimeOffset.Now),
                    _ => new(session.Id, SessionStatus.Error, detail ?? $"状态命令退出码 {result.ExitCode}", runtime.RestartAttempts, DateTimeOffset.Now),
                };
            }
            if (runtime.Process is { } process)
            {
                if (!process.HasExited)
                    return new(session.Id, SessionStatus.Running, $"PID {process.Id}", runtime.RestartAttempts, DateTimeOffset.Now);
                return new(session.Id, process.ExitCode == 0 ? SessionStatus.Stopped : SessionStatus.Error,
                    $"进程已退出，代码 {process.ExitCode}", runtime.RestartAttempts, DateTimeOffset.Now);
            }
            if (!string.IsNullOrWhiteSpace(session.ProcessName))
            {
                var name = Path.GetFileNameWithoutExtension(session.ProcessName.Trim());
                var namedProcess = Process.GetProcessesByName(name).FirstOrDefault();
                if (namedProcess is not null)
                {
                    using (namedProcess)
                        return new(session.Id, SessionStatus.Running, $"PID {namedProcess.Id}", runtime.RestartAttempts, DateTimeOffset.Now);
                }
            }
            return new(session.Id, SessionStatus.Stopped, "未检测到运行实例", runtime.RestartAttempts, DateTimeOffset.Now);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            _logger.Error($"状态检查失败：{session.Name}，{error.GetType().Name}");
            return new(session.Id, SessionStatus.Error, error.Message, runtime.RestartAttempts, DateTimeOffset.Now);
        }
    }

    private SessionSnapshot SetSnapshot(SessionDefinition session, SessionSnapshot snapshot)
    {
        var runtime = GetRuntime(session);
        var previous = runtime.Snapshot with { SessionId = session.Id };
        var current = snapshot with { SessionId = session.Id, RestartAttempts = runtime.RestartAttempts };
        runtime.Snapshot = current;
        if (previous.Status != current.Status)
            StateChanged?.Invoke(this, new SessionStateChangedEventArgs { Session = session, Previous = previous, Current = current });
        return current;
    }

    private Runtime GetRuntime(SessionDefinition session) => _runtimes.GetOrAdd(session.Id, _ => new Runtime
    {
        Snapshot = new SessionSnapshot(session.Id, SessionStatus.Unknown, "尚未检查", 0, DateTimeOffset.MinValue),
    });

    private static string? SummarizeOutput(string standardOutput, string standardError)
    {
        var output = (standardOutput ?? string.Empty).Trim();
        if (output.StartsWith('{'))
        {
            try
            {
                using var document = JsonDocument.Parse(output);
                var root = document.RootElement;
                var parts = new List<string>();
                if (root.TryGetProperty("pid", out var pid) && pid.ValueKind == JsonValueKind.Number)
                    parts.Add("PID " + pid.GetRawText());
                if (root.TryGetProperty("state", out var state) && state.ValueKind == JsonValueKind.Object)
                {
                    if (state.TryGetProperty("notifications", out var notifications) && notifications.ValueKind == JsonValueKind.Number)
                        parts.Add("通知 " + notifications.GetRawText());
                    if (state.TryGetProperty("pending_hook_events", out var pending) && pending.ValueKind == JsonValueKind.Number)
                        parts.Add("待处理 " + pending.GetRawText());
                }
                if (parts.Count == 0 && root.TryGetProperty("reason", out var reason) && reason.ValueKind == JsonValueKind.String)
                    parts.Add(reason.GetString() ?? string.Empty);
                if (parts.Count > 0) return string.Join("，", parts);
            }
            catch (JsonException) { }
        }
        foreach (var value in new[] { standardOutput, standardError })
        {
            foreach (var line in (value ?? string.Empty).Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
            {
                var clean = line.Trim();
                if (clean.Length > 0)
                    return clean.Length > 160 ? clean[..160] : clean;
            }
        }
        return null;
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        foreach (var runtime in _runtimes.Values) runtime.Process?.Dispose();
        _operationGate.Dispose();
    }
}
