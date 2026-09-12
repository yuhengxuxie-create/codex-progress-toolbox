using System.Globalization;
using System.Text;
using System.Text.Json;
using TreasureChest.Core.Services;

namespace TreasureChest.Integrations;

public sealed record ResetAlertEndpointState(string State, DateTimeOffset? RetryNotBefore,
    DateTimeOffset? NextAttemptAt, DateTimeOffset? LastAttemptAt, DateTimeOffset? LastSuccessAt, bool RetryUnrepresentable);
public sealed record ResetAlertFallback(string State, DateTimeOffset? CheckedAt);
public sealed record ResetAlertVerification(string State, bool Attempted);

public sealed record ResetAlertSourceState(
    string Source,
    string Health,
    DateTimeOffset? LastCheckAt,
    DateTimeOffset? LastSuccessAt,
    DateTimeOffset? LastItemAt,
    string? LastErrorCode,
    string? Coverage = null,
    string? DiscoveryErrorCode = null,
    IReadOnlyDictionary<string, ResetAlertEndpointState>? XEndpointStates = null,
    ResetAlertFallback? FallbackDiscovery = null,
    ResetAlertVerification? OfficialVerification = null);

public sealed record ResetAlertStatus(
    bool Available,
    bool Enabled,
    bool CanAlert,
    string State,
    string Timezone,
    IReadOnlyList<int> CheckHours,
    DateTimeOffset? LastCheckAt,
    DateTimeOffset? LastSuccessAt,
    DateTimeOffset? NextCheckAt,
    DateTimeOffset? WindowStartAt,
    DateTimeOffset? WindowEndAt,
    int Pending,
    int Uncertain,
    string? LastErrorCode,
    IReadOnlyList<ResetAlertSourceState> SourceStates,
    bool? WorkerRunning = null,
    DateTimeOffset? Heartbeat = null,
    string? Coverage = null);

public sealed record ResetAlertEvent(
    string? EventIdentity,
    string Level,
    string Evidence,
    string Window,
    string Advice,
    DateTimeOffset CreatedAt,
    DateTimeOffset ExpiresAt,
    DateTimeOffset? NotifiedAt,
    string DeliveryState,
    bool? NotificationEligible = null,
    string? EligibilityReason = null,
    string Phase = "legacy")
{
    public bool IsDesktopEligible(DateTimeOffset now) =>
        !string.IsNullOrWhiteSpace(EventIdentity) &&
        Level.Trim().ToUpperInvariant() is "A" or "B" &&
        (NotificationEligible ?? (DeliveryState.Trim().ToLowerInvariant() is "delivered" or "confirmed")) &&
        ExpiresAt > now;

    public string NotificationBody =>
        $"【{Level.Trim().ToUpperInvariant()}级{PhaseLabel}】\n证据：{Evidence}\n窗口：{Window}\n建议：{Advice}";

    private string PhaseLabel => Phase switch
    {
        "upcoming" => " · 未来预告",
        "announced_available" => " · 官方公告可用（个人账户未核实，请核对个人适用条件）",
        "watch" => " · 观察",
        _ => string.Empty,
    };
}

public sealed class ResetAlertCliService
{
    public const int SupportedSchemaVersion = 1;
    private readonly string _projectRoot;
    private readonly string _python;
    private readonly string _entry;
    private readonly string _config;

    public ResetAlertCliService(string projectRoot, string python)
    {
        _projectRoot = Path.GetFullPath(projectRoot);
        _python = Path.GetFullPath(python);
        _entry = Path.Combine(_projectRoot, "progress-wx.py");
        _config = Path.Combine(_projectRoot, "config.yaml");
    }

    public async Task<ResetAlertStatus> GetStatusAsync(CancellationToken cancellationToken = default) =>
        ParseStatusJson(await ExecuteAsync(["reset-alert-status", "--json"], "读取 Codex 重置预警状态", cancellationToken)
            .ConfigureAwait(false));

    public async Task<IReadOnlyList<ResetAlertEvent>> GetLatestAsync(
        int limit = 100,
        CancellationToken cancellationToken = default)
    {
        if (limit is < 1 or > 100) throw new ArgumentOutOfRangeException(nameof(limit));
        return ParseLatestJson(await ExecuteAsync(
                ["reset-alert-latest", "--limit", limit.ToString(CultureInfo.InvariantCulture), "--json"],
                "读取 Codex 重置预警事件", cancellationToken)
            .ConfigureAwait(false));
    }

    public static ResetAlertStatus ParseStatusJson(string json)
    {
        using var document = ParseRoot(json, "重置预警状态");
        var root = document.RootElement;
        ValidateSchema(root);
        var hours = RequiredArray(root, "check_hours").EnumerateArray().Select(value =>
        {
            if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var hour) || hour is < 0 or > 23)
                throw new InvalidDataException("重置预警 check_hours 必须是 0 至 23 的整数。");
            return hour;
        }).ToArray();
        var sources = RequiredArray(root, "source_states").EnumerateArray().Select(item =>
        {
            if (item.ValueKind != JsonValueKind.Object)
                throw new InvalidDataException("重置预警 source_states 包含非对象条目。");
            return new ResetAlertSourceState(
                RequiredString(item, "source"), RequiredString(item, "health"),
                OptionalTimestamp(item, "last_check_at"), OptionalTimestamp(item, "last_success_at"),
                OptionalTimestamp(item, "last_item_at"), OptionalString(item, "last_error_code"),
                OptionalString(item, "coverage"), OptionalString(item, "discovery_error_code"),
                ParseEndpoints(item), ParseFallback(item), ParseVerification(item));
        }).ToArray();
        return new ResetAlertStatus(
            RequiredBoolean(root, "available"), RequiredBoolean(root, "enabled"),
            RequiredBoolean(root, "can_alert"), RequiredString(root, "state"),
            RequiredString(root, "timezone"), hours,
            OptionalTimestamp(root, "last_check_at"), OptionalTimestamp(root, "last_success_at"),
            OptionalTimestamp(root, "next_check_at"), OptionalTimestamp(root, "window_start_at"),
            OptionalTimestamp(root, "window_end_at"), RequiredNonNegativeInt(root, "pending"),
            RequiredNonNegativeInt(root, "uncertain"), OptionalString(root, "last_error_code"), sources,
            root.TryGetProperty("worker_running", out _) ? RequiredBoolean(root, "worker_running") : null,
            OptionalTimestamp(root, "worker_heartbeat_at"), OptionalString(root, "coverage"));
    }

    private static JsonElement? OptionalObject(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null) return null;
        return value.ValueKind == JsonValueKind.Object ? value : throw new InvalidDataException($"重置预警字段 {name} 不是对象。");
    }
    private static IReadOnlyDictionary<string, ResetAlertEndpointState>? ParseEndpoints(JsonElement root)
    {
        if (OptionalObject(root, "x_endpoint_states") is not { } endpoints) return null;
        return endpoints.EnumerateObject().ToDictionary(item => item.Name, item =>
            new ResetAlertEndpointState(RequiredString(item.Value, "state"),
                OptionalTimestamp(item.Value, "retry_not_before"), OptionalTimestamp(item.Value, "next_attempt_at"),
                OptionalTimestamp(item.Value, "last_attempt_at"), OptionalTimestamp(item.Value, "last_success_at"),
                item.Value.TryGetProperty("retry_unrepresentable", out _) && RequiredBoolean(item.Value, "retry_unrepresentable")));
    }
    private static ResetAlertFallback? ParseFallback(JsonElement root) =>
        OptionalObject(root, "fallback_discovery") is { } value
            ? new(RequiredString(value, "state"), OptionalTimestamp(value, "checked_at")) : null;
    private static ResetAlertVerification? ParseVerification(JsonElement root) =>
        OptionalObject(root, "official_verification") is { } value
            ? new(RequiredString(value, "state"), RequiredBoolean(value, "attempted")) : null;

    public static IReadOnlyList<ResetAlertEvent> ParseLatestJson(string json)
    {
        using var document = ParseRoot(json, "重置预警事件");
        var root = document.RootElement;
        ValidateSchema(root);
        if (!RequiredBoolean(root, "available"))
            throw new InvalidDataException("重置预警事件暂不可读取；保留本地记录。");
        return RequiredArray(root, "items").EnumerateArray().Select(item =>
        {
            if (item.ValueKind != JsonValueKind.Object)
                throw new InvalidDataException("重置预警 items 包含非对象条目。");
            var delivery = RequiredObject(item, "delivery");
            var key = OptionalString(item, "event_key");
            var id = OptionalString(item, "event_id");
            if (key is not null && id is not null && key != id)
                throw new InvalidDataException("重置预警事件身份别名不一致。");
            var identity = key ?? id;
            return new ResetAlertEvent(
                identity, RequiredString(item, "level"), RequiredString(item, "evidence"),
                RequiredString(item, "window"), RequiredString(item, "advice"),
                RequiredTimestamp(item, "created_at"), RequiredTimestamp(item, "expires_at"),
                OptionalTimestamp(item, "notified_at"), RequiredString(delivery, "state"),
                item.TryGetProperty("notification_eligible", out _) ? RequiredBoolean(item, "notification_eligible") : null,
                OptionalString(item, "eligibility_reason"), OptionalString(item, "phase") ?? "legacy");
        }).ToArray();
    }

    private async Task<string> ExecuteAsync(
        IReadOnlyList<string> arguments,
        string action,
        CancellationToken cancellationToken)
    {
        EnsureRuntimeExists();
        var command = string.Join(" ", new[] { "call " + Quote(_python), Quote(_entry), "--config", Quote(_config) }
            .Concat(arguments.Select(Quote)));
        var result = await CommandExecutor.RunAsync(command, _projectRoot, TimeSpan.FromSeconds(15), cancellationToken)
            .ConfigureAwait(false);
        if (result.TimedOut) throw new TimeoutException($"{action}超过 15 秒。");
        if (result.ExitCode != 0)
        {
            var detail = LastLine(result.StandardError) ?? LastLine(result.StandardOutput) ?? $"退出码 {result.ExitCode}";
            throw new InvalidOperationException($"{action}失败：{detail}");
        }
        return result.StandardOutput.Trim();
    }

    private void EnsureRuntimeExists()
    {
        if (!File.Exists(_python)) throw new FileNotFoundException("FeiShuBOT Python 运行时不存在。", _python);
        if (!File.Exists(_entry)) throw new FileNotFoundException("FeiShuBOT CLI 入口不存在。", _entry);
        if (!File.Exists(_config)) throw new FileNotFoundException("FeiShuBOT 配置文件不存在。", _config);
    }

    private static JsonDocument ParseRoot(string json, string subject)
    {
        if (string.IsNullOrWhiteSpace(json)) throw new InvalidDataException($"{subject} CLI 返回了空 JSON。");
        var document = JsonDocument.Parse(json);
        if (document.RootElement.ValueKind == JsonValueKind.Object) return document;
        document.Dispose();
        throw new InvalidDataException($"{subject} JSON 顶层必须是对象。");
    }

    private static void ValidateSchema(JsonElement root)
    {
        if (!root.TryGetProperty("schema_version", out var value) || value.ValueKind != JsonValueKind.Number ||
            !value.TryGetInt32(out var version) || version != SupportedSchemaVersion)
            throw new InvalidDataException($"重置预警仅支持 schema_version={SupportedSchemaVersion}。");
    }

    private static JsonElement RequiredArray(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Array
            ? value : throw new InvalidDataException($"重置预警 JSON 缺少数组 {name}。");

    private static JsonElement RequiredObject(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Object
            ? value : throw new InvalidDataException($"重置预警 JSON 缺少对象 {name}。");

    private static string RequiredString(JsonElement root, string name) =>
        OptionalString(root, name) ?? throw new InvalidDataException($"重置预警 JSON 缺少字符串 {name}。");

    private static string? OptionalString(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String &&
        !string.IsNullOrWhiteSpace(value.GetString()) ? value.GetString()!.Trim() : null;

    private static bool RequiredBoolean(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? value.GetBoolean() : throw new InvalidDataException($"重置预警 JSON 缺少布尔字段 {name}。");

    private static int RequiredNonNegativeInt(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number &&
        value.TryGetInt32(out var number) && number >= 0 ? number :
        throw new InvalidDataException($"重置预警 JSON 字段 {name} 必须是非负整数。");

    private static DateTimeOffset RequiredTimestamp(JsonElement root, string name) =>
        OptionalTimestamp(root, name) ?? throw new InvalidDataException($"重置预警 JSON 缺少时间 {name}。");

    private static DateTimeOffset? OptionalTimestamp(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
            return null;
        if (value.ValueKind == JsonValueKind.String && DateTimeOffset.TryParse(
                value.GetString(), CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var parsed))
            return parsed;
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var unix))
            return Math.Abs(unix) >= 10_000_000_000
                ? DateTimeOffset.FromUnixTimeMilliseconds(unix)
                : DateTimeOffset.FromUnixTimeSeconds(unix);
        throw new InvalidDataException($"重置预警 JSON 字段 {name} 不是有效时间。");
    }

    private static string Quote(string value) => "\"" + value.Replace("\"", "\"\"", StringComparison.Ordinal) + "\"";
    private static string? LastLine(string value) => value.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
        .Select(line => line.Trim()).LastOrDefault(line => line.Length > 0);
}

public static class ResetAlertPresentation
{
    public static IReadOnlyList<string> StatusWidthSamples { get; } =
    [
        "运行中", "已停止", "连接中", "重连中", "不可用",
        "下次 23:00", "等待 08:00", "部分可用", "投递结果未知", "未就绪", "心跳过期",
    ];

    public static (string Status, string Detail) Describe(ResetAlertStatus status, DateTimeOffset now)
    {
        if (!status.Available) return ("不可用", status.LastErrorCode ?? "后端预警模块不可用");
        if (!status.Enabled) return ("已停止", "预警模块未启用；与飞书服务共用进程");
        if (status.WorkerRunning == false) return ("未就绪", "预警工作线程未运行；请检查飞书服务");
        if (status.WorkerRunning == true && status.Heartbeat is { } heartbeat && now - heartbeat > TimeSpan.FromMinutes(5))
            return ("心跳过期", "预警工作线程心跳超过 5 分钟未更新；当前运行状态待核验");
        if (status.Uncertain > 0) return ("投递结果未知", $"有 {status.Uncertain} 条投递结果待确认");
        string[] expected = ["forecast", "openai_status", "openai_codex_docs", "x_thsottiaux"];
        var missing = expected.Where(name => !status.SourceStates.Any(x => x.Source == name))
            .Select(name => new ResetAlertSourceState(name, "missing", null, null, null, "missing"));
        var unhealthySources = status.SourceStates.Where(item => !IsHealthy(item) || IsDegraded(item) ||
            HasEndpointFailure(item)).Concat(missing).ToArray();
        if (unhealthySources.Length > 0)
            return ("部分可用", DescribePartialAvailability(status, unhealthySources));
        if (string.Equals(status.Coverage, "degraded", StringComparison.OrdinalIgnoreCase))
            return ("部分可用", "后端报告监测覆盖不足；部分消息可能未被发现，等待后续检查");
        if (!status.CanAlert)
        {
            if (status.WorkerRunning != true) return ("未就绪", "后端当前不能预警；旧接口未提供工作线程状态");
            var firstHour = status.CheckHours.Count == 0 ? 8 : status.CheckHours.Min();
            if (now.ToOffset(TimeSpan.FromHours(8)).Hour >= firstHour)
                return ("未就绪", "已进入计划检查时段，但后端当前不能预警");
            return ($"等待 {firstHour:00}:00", $"北京时间 {firstHour:00}:00 后进入检查窗口");
        }
        if (status.NextCheckAt is { } next)
            return ($"下次 {next.ToOffset(TimeSpan.FromHours(8)):HH:mm}",
                HealthyDetail(status, now, $"北京时间 {next.ToOffset(TimeSpan.FromHours(8)):HH:mm} 再检查"));
        return ("运行中", HealthyDetail(status, now, "等待下一次计划检查"));
    }

    private static string FormatTime(DateTimeOffset? value, DateTimeOffset fallback) =>
        (value ?? fallback).ToOffset(TimeSpan.FromHours(8)).ToString("MM-dd HH:mm", CultureInfo.InvariantCulture);

    private static string HealthyDetail(ResetAlertStatus status, DateTimeOffset now, string schedule)
    {
        var sourceCount = status.SourceStates.Count;
        var sources = $"{sourceCount} 个来源均正常";
        var lastSuccess = status.LastSuccessAt is { } success ? FormatTime(success, now) : "尚无成功记录";
        return $"{sources}；最近成功：{lastSuccess}；{schedule}";
    }

    private static bool IsHealthy(ResetAlertSourceState source) =>
        source.Health.Equals("healthy", StringComparison.OrdinalIgnoreCase) ||
        source.Health.Equals("ok", StringComparison.OrdinalIgnoreCase);

    private static bool IsDegraded(ResetAlertSourceState source) =>
        string.Equals(source.Coverage, "degraded", StringComparison.OrdinalIgnoreCase);

    private static string DescribePartialAvailability(
        ResetAlertStatus status,
        IReadOnlyList<ResetAlertSourceState> unhealthySources)
    {
        var failures = unhealthySources.Select(source => source.Source == "x_thsottiaux" ? DescribeX(source) :
            $"{FriendlySourceName(source.Source)}{FriendlyFailure(source.LastErrorCode)}");
        var healthyCount = status.SourceStates.Count(source => IsHealthy(source) && !IsDegraded(source) && !HasEndpointFailure(source));
        var retry = status.NextCheckAt is { } next
            ? $"其他来源下次检查：北京时间 {next.ToOffset(TimeSpan.FromHours(8)):MM-dd HH:mm}"
            : "其他来源下次检查时间尚未提供";
        return $"{string.Join("；", failures)}；其他 {healthyCount} 个来源正常；{retry}";
    }

    private static string DescribeX(ResetAlertSourceState source)
    {
        var parts = new List<string>();
        var direct = source.XEndpointStates?.GetValueOrDefault("syndication");
        var limited = source.DiscoveryErrorCode?.Contains("429", StringComparison.Ordinal) == true || source.LastErrorCode == "source_http_429";
        parts.Add(direct?.State == "available" ? "X直接发现最近可用" :
            "X直接发现" + (limited ? "限流" : "受限") + RetryPlan(direct));
        foreach (var (key, label) in new[] { ("oembed", "正文核验"), ("x_parent", "父关系核验") })
            if (source.XEndpointStates?.GetValueOrDefault(key) is { } endpoint &&
                (endpoint.State is "cooldown" or "retry_due" or "error" || endpoint.RetryUnrepresentable))
                parts.Add(label + "也受限" + RetryPlan(endpoint));
        parts.Add(source.FallbackDiscovery switch
        {
            { State: "available", CheckedAt: not null } => source.OfficialVerification switch
            {
                { State: "verified", Attempted: true } => "备用来源本次可用",
                { State: "cached" } => "备用发现可用，正文沿用缓存",
                { State: "unavailable" } => "备用发现可用，正文核验失败",
                _ => "备用发现可用，正文暂无新核验",
            },
            { State: "unavailable" } => "备用来源本次失败",
            _ => "备用来源暂无新核验",
        });
        return string.Join("；", parts);
    }

    private static string RetryPlan(ResetAlertEndpointState? endpoint) =>
        endpoint?.RetryUnrepresentable == true ? "，暂无可用重试时间" :
        endpoint?.NextAttemptAt is { } next ? $"，预计北京时间 {next.ToOffset(TimeSpan.FromHours(8)):yyyy-MM-dd HH:mm} 重试" :
        "，重试计划尚未提供";

    private static bool HasEndpointFailure(ResetAlertSourceState source) =>
        source.XEndpointStates?.Values.Any(endpoint => endpoint.State is "cooldown" or "retry_due" or "error" || endpoint.RetryUnrepresentable) == true;

    private static string FriendlySourceName(string source) => source.Trim().ToLowerInvariant() switch
    {
        "x_thsottiaux" => "X（@thsottiaux）",
        "forecast" => "Will Codex Quota Reset 预测站",
        "openai_status" => "OpenAI Status",
        "openai_codex_docs" => "OpenAI 官方 Codex 额度页面",
        _ => "一个监测来源",
    };

    private static string FriendlyFailure(string? errorCode)
    {
        var code = errorCode?.Trim().ToLowerInvariant() ?? string.Empty;
        if (code == "missing") return "状态缺失";
        if (code == "source_http_429") return "被 HTTP 429 限流";
        if (code == "source_http_403") return "暂时拒绝访问（HTTP 403）";
        if (code == "source_http_401") return "身份校验失败（HTTP 401）";
        if (code.StartsWith("source_http_5", StringComparison.Ordinal)) return "服务端暂时异常";
        if (code.Contains("timeout", StringComparison.Ordinal)) return "请求超时";
        if (code.Contains("schema", StringComparison.Ordinal) ||
            code.Contains("parse", StringComparison.Ordinal) ||
            code.Contains("payload", StringComparison.Ordinal))
            return "返回内容暂时无法解析";
        if (code.Contains("unavailable", StringComparison.Ordinal) ||
            code.Contains("network", StringComparison.Ordinal) ||
            code.Contains("connection", StringComparison.Ordinal))
            return "暂时无法连接";
        return "暂时无法读取";
    }
}

/// <summary>
/// Bounds the read-only desktop consumer independently from the main one-second
/// status timer.  FeiShuBOT owns the hourly external scan; TreasureChest only
/// polls its frozen local status/latest views and never accesses those sources.
/// </summary>
internal sealed class ResetAlertRefreshGate(TimeSpan minimumInterval)
{
    private readonly object _sync = new();
    private DateTimeOffset _lastAttempt = DateTimeOffset.MinValue;

    public bool TryEnter(DateTimeOffset now, bool force = false)
    {
        lock (_sync)
        {
            if (!force && _lastAttempt != DateTimeOffset.MinValue &&
                now - _lastAttempt < minimumInterval)
                return false;
            _lastAttempt = now;
            return true;
        }
    }
}

public sealed class ResetAlertDedupStore
{
    private const int MaximumIdentities = 512;
    private readonly string _path;
    private readonly SemaphoreSlim _gate = new(1, 1);

    public ResetAlertDedupStore(string path) => _path = Path.GetFullPath(path);

    public async Task<IReadOnlyList<ResetAlertEvent>> SelectPendingAsync(
        IReadOnlyList<ResetAlertEvent> events,
        DateTimeOffset now,
        CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var state = await LoadAsync(cancellationToken).ConfigureAwait(false);
            var history = (state.History ?? []).ToDictionary(x => x.Event.EventIdentity!, StringComparer.Ordinal);
            foreach (var item in events.Where(x => !string.IsNullOrWhiteSpace(x.EventIdentity) &&
                         x.Level.Trim().ToUpperInvariant() is "A" or "B"))
            {
                if (history.TryGetValue(item.EventIdentity!, out var old))
                    history[item.EventIdentity!] = old with { Event = item };
                else
                    history[item.EventIdentity!] = new ResetAlertInboxItem(item, false,
                        state.EventIdentities.Contains(item.EventIdentity!, StringComparer.Ordinal));
            }
            // Persist evidence before attempting a transient shell notification.
            // Never discard an unread item simply because this is the first poll.
            var entries = history.Values.OrderByDescending(x => x.Event.CreatedAt).ToArray();
            state = state with { Initialized = true, History = entries };
            await SaveAsync(state, cancellationToken).ConfigureAwait(false);
            var seen = new HashSet<string>(state.EventIdentities, StringComparer.Ordinal);
            return entries.Where(x => !x.Read && !x.ShellSubmitted && !seen.Contains(x.Event.EventIdentity!) &&
                x.Event.IsDesktopEligible(now)).Select(x => x.Event).ToArray();
        }
        finally { _gate.Release(); }
    }

    public async Task<IReadOnlyList<ResetAlertInboxItem>> ReadHistoryAsync(CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try { return (await LoadAsync(cancellationToken).ConfigureAwait(false)).History ?? []; }
        finally { _gate.Release(); }
    }

    public async Task MarkReadAsync(IEnumerable<string> identities, CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var keys = identities.ToHashSet(StringComparer.Ordinal);
            var state = await LoadAsync(cancellationToken).ConfigureAwait(false);
            await SaveAsync(state with { History = (state.History ?? []).Select(x =>
                keys.Contains(x.Event.EventIdentity!) ? x with { Read = true } : x).ToArray() }, cancellationToken)
                .ConfigureAwait(false);
        }
        finally { _gate.Release(); }
    }

    public async Task MarkDeliveredAsync(string eventIdentity, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(eventIdentity))
            throw new ArgumentException("重置预警事件身份不能为空。", nameof(eventIdentity));
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var state = await LoadAsync(cancellationToken).ConfigureAwait(false);
            var seen = new HashSet<string>(state.EventIdentities, StringComparer.Ordinal) { eventIdentity };
            await SaveAsync(state with { Initialized = true, EventIdentities = seen.TakeLast(MaximumIdentities).ToArray(),
                History = (state.History ?? []).Select(x => x.Event.EventIdentity == eventIdentity ? x with { ShellSubmitted = true } : x).ToArray() }, cancellationToken)
                .ConfigureAwait(false);
        }
        finally { _gate.Release(); }
    }

    private async Task<State> LoadAsync(CancellationToken cancellationToken)
    {
        if (!File.Exists(_path)) return new State(false, []);
        await using var stream = File.OpenRead(_path);
        var state = await JsonSerializer.DeserializeAsync<State>(stream, cancellationToken: cancellationToken)
            .ConfigureAwait(false);
        if (state is null || state.EventIdentities is null ||
            (state.History ?? []).Any(x => x is null || x.Event is null || string.IsNullOrWhiteSpace(x.Event.EventIdentity)) ||
            (state.History ?? []).Select(x => x.Event.EventIdentity).Distinct(StringComparer.Ordinal).Count() != (state.History ?? []).Length)
            throw new InvalidDataException("本地预警记录损坏；文件已保留，请核验或从备份恢复。");
        return state;
    }

    private async Task SaveAsync(State state, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        var temporary = _path + ".tmp";
        try
        {
            await using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None,
                             4096, FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await JsonSerializer.SerializeAsync(stream, state, cancellationToken: cancellationToken)
                    .ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
            }
            File.Move(temporary, _path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporary)) File.Delete(temporary);
        }
    }

    private sealed record State(bool Initialized, string[] EventIdentities, ResetAlertInboxItem[]? History = null);
}

public sealed record ResetAlertInboxItem(ResetAlertEvent Event, bool Read, bool ShellSubmitted = false);
