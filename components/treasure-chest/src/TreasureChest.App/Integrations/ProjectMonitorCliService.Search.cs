using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TreasureChest.Integrations;

public enum SessionSearchCacheMode
{
    Persistent,
    Ephemeral,
}

public static class SessionSearchExecutionPolicy
{
    public const string EphemeralQaSwitch = "--qa-session-search-ephemeral";

    public static SessionSearchCacheMode FromApplicationArguments(IEnumerable<string> arguments) =>
        arguments.Any(value => value.Equals(EphemeralQaSwitch, StringComparison.OrdinalIgnoreCase))
            ? SessionSearchCacheMode.Ephemeral
            : SessionSearchCacheMode.Persistent;

    public static bool SuppressSystemStateWrites(IEnumerable<string> arguments) =>
        FromApplicationArguments(arguments) == SessionSearchCacheMode.Ephemeral;

    public static string ToCliValue(SessionSearchCacheMode mode) => mode switch
    {
        SessionSearchCacheMode.Persistent => "persistent",
        SessionSearchCacheMode.Ephemeral => "ephemeral",
        _ => throw new ArgumentOutOfRangeException(nameof(mode), mode, "不支持的会话搜索缓存模式。"),
    };
}

public sealed record SessionSearchRequest(
    string Name,
    string Description,
    string LastActivity,
    string Scope = "auto")
{
    public SessionSearchRequest WithScope(string scope) => this with { Scope = scope };
}

public sealed record SessionSearchProgress(
    string Phase,
    int Current,
    int Total,
    string Message);

public sealed record SessionSearchMonitor(
    bool Monitored,
    string? Origin,
    long? ExpiresAt);

public sealed record SessionSearchMatch(
    string ThreadId,
    string Title,
    string Description,
    string LastResult,
    string LastActivityAtBeijing,
    double Score,
    string Confidence,
    string Classification,
    string Reason,
    string ProjectId,
    string ProjectName,
    bool Archived,
    string HostId,
    SessionSearchMonitor Monitor);

public sealed record SessionSearchWarning(
    string Code,
    string? ThreadId,
    IReadOnlyList<string>? Details);

public sealed record SessionSearchResult(
    string SearchId,
    string Status,
    string Scope,
    string ScopeLabel,
    bool CanExpand,
    string? NextScope,
    string CostWarning,
    int ExaminedCount,
    int SemanticCandidateCount,
    int ModelCallCount,
    IReadOnlyList<SessionSearchMatch> Matches,
    IReadOnlyList<SessionSearchWarning> Warnings);

public sealed partial class ProjectMonitorCliService
{
    public static readonly IReadOnlySet<string> SearchScopes = new HashSet<string>(
        ["auto", "hint", "recent_30d", "recent_180d", "all"],
        StringComparer.Ordinal);
    public static readonly IReadOnlySet<string> SearchPhases = new HashSet<string>(
        ["starting", "collecting", "reading", "narrowing", "scoring", "completed", "cancelled", "failed"],
        StringComparer.Ordinal);
    private static readonly TimeSpan SearchTimeout = TimeSpan.FromMinutes(45);
    private static readonly TimeSpan CancelGracePeriod = TimeSpan.FromSeconds(30);

    public async Task<SessionSearchResult> SearchSessionsAsync(
        SessionSearchRequest request,
        IProgress<SessionSearchProgress>? progress = null,
        CancellationToken cancellationToken = default,
        SessionSearchCacheMode cacheMode = SessionSearchCacheMode.Persistent)
    {
        EnsureRuntimeExists();
        ValidateSearchRequest(request);
        var workspace = SessionSearchWorkspace.Create();
        Process? process = null;
        try
        {
            await File.WriteAllTextAsync(
                workspace.RequestFile,
                SerializeSearchRequest(request),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
                cancellationToken).ConfigureAwait(false);

            process = new Process
            {
                StartInfo = CreateSearchStartInfo(workspace, cacheMode),
                EnableRaisingEvents = true,
            };
            if (!process.Start()) throw new InvalidOperationException("FeiShuBOT 会话搜索进程未能启动。");
            var stdoutTask = process.StandardOutput.ReadToEndAsync();
            var stderrTask = process.StandardError.ReadToEndAsync();
            using var cancellationRegistration = cancellationToken.Register(() => workspace.RequestCancel());
            var startedAt = DateTimeOffset.UtcNow;
            DateTimeOffset? cancelRequestedAt = null;
            string? lastProgressJson = null;

            while (!process.HasExited)
            {
                if (cancellationToken.IsCancellationRequested)
                {
                    cancelRequestedAt ??= DateTimeOffset.UtcNow;
                    workspace.RequestCancel();
                }
                if (DateTimeOffset.UtcNow - startedAt > SearchTimeout)
                {
                    workspace.RequestCancel();
                    await WaitForExitOrKillAsync(process, CancelGracePeriod).ConfigureAwait(false);
                    throw new TimeoutException($"会话搜索超过 {SearchTimeout.TotalMinutes:0} 分钟，已请求安全取消。");
                }
                if (cancelRequestedAt.HasValue && DateTimeOffset.UtcNow - cancelRequestedAt.Value > CancelGracePeriod)
                {
                    TryKill(process);
                    throw new OperationCanceledException("会话搜索取消等待超时，已结束搜索进程。", cancellationToken);
                }
                lastProgressJson = ReportProgressIfChanged(workspace.ProgressFile, lastProgressJson, progress);
                await Task.Delay(200).ConfigureAwait(false);
            }

            await process.WaitForExitAsync().ConfigureAwait(false);
            lastProgressJson = ReportProgressIfChanged(workspace.ProgressFile, lastProgressJson, progress);
            var stdout = (await stdoutTask.ConfigureAwait(false)).Trim();
            var stderr = (await stderrTask.ConfigureAwait(false)).Trim();
            if (process.ExitCode == 3 || cancellationToken.IsCancellationRequested)
                throw new OperationCanceledException(LastNonEmptyLine(stderr) ?? "会话搜索已取消。", cancellationToken);
            if (process.ExitCode != 0)
                throw new InvalidOperationException($"会话搜索失败：{LastNonEmptyLine(stderr) ?? $"退出码 {process.ExitCode}"}");
            if (string.IsNullOrWhiteSpace(stdout))
                throw new InvalidDataException("会话搜索成功退出，但 stdout 没有最终 JSON。");
            return ParseSearchResultJson(stdout);
        }
        catch
        {
            if (process is { HasExited: false })
            {
                workspace.RequestCancel();
                await WaitForExitOrKillAsync(process, CancelGracePeriod).ConfigureAwait(false);
            }
            throw;
        }
        finally
        {
            process?.Dispose();
            workspace.Dispose();
        }
    }

    public static void ValidateSearchRequest(SessionSearchRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        ValidateClue(request.Name, "会话名称");
        ValidateClue(request.Description, "会话描述");
        ValidateClue(request.LastActivity, "会话最后活动时间");
        if (!SearchScopes.Contains(request.Scope ?? string.Empty))
            throw new ArgumentException("搜索范围必须是 auto、hint、recent_30d、recent_180d 或 all。", nameof(request));
    }

    public static string SerializeSearchRequest(SessionSearchRequest request)
    {
        ValidateSearchRequest(request);
        var payload = new SearchRequestPayload
        {
            SchemaVersion = SupportedSchemaVersion,
            Name = request.Name ?? string.Empty,
            Description = request.Description ?? string.Empty,
            LastActivity = request.LastActivity ?? string.Empty,
            Scope = request.Scope,
        };
        var json = JsonSerializer.Serialize(payload);
        if (Encoding.UTF8.GetByteCount(json) > 64 * 1024)
            throw new ArgumentException("会话搜索请求超过 64 KiB。", nameof(request));
        return json;
    }

    public static SessionSearchProgress ParseSearchProgressJson(string json)
    {
        using var document = ParseJsonObject(json, "搜索进度");
        var root = document.RootElement;
        ValidateSchemaVersion(root);
        var phase = RequiredExactString(root, "phase");
        if (!SearchPhases.Contains(phase)) throw new InvalidDataException($"搜索进度 phase={phase} 不受支持。");
        var current = RequiredNonNegativeInt(root, "current");
        var total = RequiredNonNegativeInt(root, "total");
        var message = RequiredExactString(root, "message");
        if (message.Length > 300) throw new InvalidDataException("搜索进度 message 超过 300 字符。");
        return new SessionSearchProgress(phase, current, total, message);
    }

    public static SessionSearchResult ParseSearchResultJson(string json)
    {
        using var document = ParseJsonObject(json, "搜索结果");
        var root = document.RootElement;
        ValidateSchemaVersion(root);
        var searchId = RequiredNonEmptyString(root, "search_id");
        var status = RequiredExactString(root, "status");
        if (status is not ("found" or "ambiguous" or "not_found"))
            throw new InvalidDataException($"搜索结果 status={status} 不受支持。");
        var scope = RequiredExactString(root, "scope");
        if (!SearchScopes.Contains(scope)) throw new InvalidDataException($"搜索结果 scope={scope} 不受支持。");
        var scopeLabel = RequiredNonEmptyString(root, "scope_label");
        var canExpand = RequiredJsonBoolean(root, "can_expand");
        var nextScope = RequiredNullableString(root, "next_scope");
        if (nextScope is not null && !SearchScopes.Contains(nextScope))
            throw new InvalidDataException($"搜索结果 next_scope={nextScope} 不受支持。");
        if (canExpand != (nextScope is not null))
            throw new InvalidDataException("搜索结果 can_expand 与 next_scope 不一致。");
        var matchesElement = RequiredArray(root, "matches");
        var matches = matchesElement.EnumerateArray().Select(ParseSearchMatch).ToArray();
        if (matches.Select(item => item.ThreadId).Distinct(StringComparer.OrdinalIgnoreCase).Count() != matches.Length)
            throw new InvalidDataException("搜索结果 matches 包含重复任务 ID。");
        if (status == "not_found" && matches.Length != 0)
            throw new InvalidDataException("not_found 搜索结果的 matches 必须为空。");
        if (status != "not_found" && matches.Length == 0)
            throw new InvalidDataException($"{status} 搜索结果缺少候选。");
        var warnings = RequiredArray(root, "warnings").EnumerateArray().Select(ParseSearchWarning).ToArray();
        return new SessionSearchResult(
            searchId,
            status,
            scope,
            scopeLabel,
            canExpand,
            nextScope,
            RequiredNonEmptyString(root, "cost_warning"),
            RequiredNonNegativeInt(root, "examined_count"),
            RequiredNonNegativeInt(root, "semantic_candidate_count"),
            RequiredNonNegativeInt(root, "model_call_count"),
            matches,
            warnings);
    }

    public static IReadOnlyList<string> BuildSearchCommandArguments(
        string entry,
        string config,
        string requestFile,
        string progressFile,
        string cancelFile,
        SessionSearchCacheMode cacheMode)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(entry);
        ArgumentException.ThrowIfNullOrWhiteSpace(config);
        ArgumentException.ThrowIfNullOrWhiteSpace(requestFile);
        ArgumentException.ThrowIfNullOrWhiteSpace(progressFile);
        ArgumentException.ThrowIfNullOrWhiteSpace(cancelFile);
        return
        [
            entry, "--config", config, "session-search",
            "--request-file", requestFile,
            "--progress-file", progressFile,
            "--cancel-file", cancelFile,
            "--cache-mode", SessionSearchExecutionPolicy.ToCliValue(cacheMode),
            "--json",
        ];
    }

    private ProcessStartInfo CreateSearchStartInfo(SessionSearchWorkspace workspace, SessionSearchCacheMode cacheMode)
    {
        var info = new ProcessStartInfo
        {
            FileName = _python,
            WorkingDirectory = _projectRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var argument in BuildSearchCommandArguments(
                     _entry,
                     _config,
                     workspace.RequestFile,
                     workspace.ProgressFile,
                     workspace.CancelFile,
                     cacheMode))
            info.ArgumentList.Add(argument);
        return info;
    }

    private static string? ReportProgressIfChanged(
        string path,
        string? previousJson,
        IProgress<SessionSearchProgress>? progress)
    {
        if (!File.Exists(path)) return previousJson;
        string json;
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
            json = reader.ReadToEnd();
        }
        catch (FileNotFoundException) { return previousJson; }
        if (json == previousJson) return previousJson;
        var parsed = ParseSearchProgressJson(json);
        progress?.Report(parsed);
        return json;
    }

    private static SessionSearchMatch ParseSearchMatch(JsonElement item)
    {
        if (item.ValueKind != JsonValueKind.Object) throw new InvalidDataException("搜索结果 matches 包含非对象条目。");
        var monitorElement = RequiredObject(item, "monitor");
        var origin = RequiredNullableString(monitorElement, "origin");
        if (origin is not null && origin is not ("manual" or "auto"))
            throw new InvalidDataException("搜索结果 monitor.origin 必须是 manual、auto 或 null。");
        var monitored = RequiredJsonBoolean(monitorElement, "monitored");
        var expiresAt = RequiredNullableInt64(monitorElement, "expires_at");
        if (!monitored && (origin is not null || expiresAt is not null))
            throw new InvalidDataException("未监测候选的 monitor.origin/expires_at 必须为 null。");
        if (monitored && origin is null)
            throw new InvalidDataException("已监测候选的 monitor.origin 不能为 null。");
        var threadId = NormalizeThreadId(RequiredNonEmptyString(item, "thread_id"));
        var score = RequiredDouble(item, "score");
        if (score is < 0 or > 1) throw new InvalidDataException("搜索结果 score 必须在 0 到 1 之间。");
        var confidence = RequiredNonEmptyString(item, "confidence");
        if (confidence is not ("high" or "medium" or "low"))
            throw new InvalidDataException("搜索结果 confidence 必须是 high、medium 或 low。");
        var classification = RequiredNonEmptyString(item, "classification");
        if (classification is not ("strong_match" or "possible_match" or "unlikely"))
            throw new InvalidDataException("搜索结果 classification 不受支持。");
        var lastActivity = RequiredNonEmptyString(item, "last_activity_at_beijing");
        if (!DateTime.TryParseExact(lastActivity, "yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture,
                DateTimeStyles.None, out _))
            throw new InvalidDataException("搜索结果 last_activity_at_beijing 必须使用 yyyy-MM-dd HH:mm。");
        return new SessionSearchMatch(
            threadId,
            RequiredExactString(item, "title"),
            RequiredExactString(item, "description"),
            RequiredExactString(item, "last_result"),
            lastActivity,
            score,
            confidence,
            classification,
            RequiredNonEmptyString(item, "reason"),
            RequiredExactString(item, "project_id"),
            RequiredNonEmptyString(item, "project_name"),
            RequiredJsonBoolean(item, "archived"),
            RequiredNonEmptyString(item, "host_id"),
            new SessionSearchMonitor(monitored, origin, expiresAt));
    }

    private static SessionSearchWarning ParseSearchWarning(JsonElement item)
    {
        if (item.ValueKind != JsonValueKind.Object) throw new InvalidDataException("搜索结果 warnings 包含非对象条目。");
        var code = RequiredNonEmptyString(item, "code");
        var threadId = OptionalExactString(item, "thread_id");
        if (!string.IsNullOrWhiteSpace(threadId)) threadId = NormalizeThreadId(threadId);
        IReadOnlyList<string>? details = null;
        if (TryGetProperty(item, "details", out var value) && value.ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined))
        {
            if (value.ValueKind != JsonValueKind.Array || value.EnumerateArray().Any(entry => entry.ValueKind != JsonValueKind.String))
                throw new InvalidDataException("搜索结果 warnings.details 必须是字符串数组或 null。");
            details = value.EnumerateArray().Select(entry => entry.GetString() ?? string.Empty).ToArray();
        }
        return new SessionSearchWarning(code, threadId, details);
    }

    private static JsonDocument ParseJsonObject(string json, string label)
    {
        if (string.IsNullOrWhiteSpace(json)) throw new InvalidDataException($"{label} JSON 为空。");
        JsonDocument document;
        try { document = JsonDocument.Parse(json); }
        catch (JsonException error) { throw new InvalidDataException($"{label} JSON 损坏：{error.Message}", error); }
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            document.Dispose();
            throw new InvalidDataException($"{label} JSON 顶层必须是对象。");
        }
        return document;
    }

    private static JsonElement RequiredArray(JsonElement item, string name) =>
        TryGetProperty(item, name, out var value) && value.ValueKind == JsonValueKind.Array
            ? value
            : throw new InvalidDataException($"搜索 JSON 缺少数组字段 {name}。");

    private static JsonElement RequiredObject(JsonElement item, string name) =>
        TryGetProperty(item, name, out var value) && value.ValueKind == JsonValueKind.Object
            ? value
            : throw new InvalidDataException($"搜索 JSON 缺少对象字段 {name}。");

    private static string RequiredExactString(JsonElement item, string name) =>
        TryGetProperty(item, name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? string.Empty
            : throw new InvalidDataException($"搜索 JSON 缺少字符串字段 {name}。");

    private static string RequiredNonEmptyString(JsonElement item, string name)
    {
        var value = RequiredExactString(item, name);
        if (string.IsNullOrWhiteSpace(value)) throw new InvalidDataException($"搜索 JSON 字段 {name} 不能为空。");
        return value;
    }

    private static string? OptionalExactString(JsonElement item, string name) =>
        TryGetProperty(item, name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    private static string? RequiredNullableString(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value)) throw new InvalidDataException($"搜索 JSON 缺少字段 {name}。");
        return value.ValueKind switch
        {
            JsonValueKind.Null => null,
            JsonValueKind.String => value.GetString(),
            _ => throw new InvalidDataException($"搜索 JSON 字段 {name} 必须是字符串或 null。"),
        };
    }

    private static bool RequiredJsonBoolean(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value)) throw new InvalidDataException($"搜索 JSON 缺少布尔字段 {name}。");
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => throw new InvalidDataException($"搜索 JSON 字段 {name} 必须是布尔值。"),
        };
    }

    private static int RequiredNonNegativeInt(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value) || value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var number) || number < 0)
            throw new InvalidDataException($"搜索 JSON 字段 {name} 必须是非负整数。");
        return number;
    }

    private static long? RequiredNullableInt64(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value)) throw new InvalidDataException($"搜索 JSON 缺少字段 {name}。");
        if (value.ValueKind == JsonValueKind.Null) return null;
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var number)) return number;
        throw new InvalidDataException($"搜索 JSON 字段 {name} 必须是整数或 null。");
    }

    private static double RequiredDouble(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value) || value.ValueKind != JsonValueKind.Number || !value.TryGetDouble(out var number) || double.IsNaN(number) || double.IsInfinity(number))
            throw new InvalidDataException($"搜索 JSON 字段 {name} 必须是有限数值。");
        return number;
    }

    private static void ValidateClue(string? value, string label)
    {
        if ((value ?? string.Empty).Length > 1000) throw new ArgumentException($"{label}不能超过 1000 字符。");
    }

    private static async Task WaitForExitOrKillAsync(Process process, TimeSpan grace)
    {
        if (process.HasExited) return;
        using var timeout = new CancellationTokenSource(grace);
        try { await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false); }
        catch (OperationCanceledException)
        {
            TryKill(process);
            try { await process.WaitForExitAsync().ConfigureAwait(false); } catch { }
        }
    }

    private static void TryKill(Process process)
    {
        try { if (!process.HasExited) process.Kill(entireProcessTree: true); } catch { }
    }

    private static string? LastNonEmptyLine(string value) => value
        .Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
        .Select(line => line.Trim())
        .LastOrDefault(line => line.Length > 0);

    private sealed class SearchRequestPayload
    {
        [JsonPropertyName("schema_version")]
        public int SchemaVersion { get; init; }
        [JsonPropertyName("name")]
        public string Name { get; init; } = string.Empty;
        [JsonPropertyName("description")]
        public string Description { get; init; } = string.Empty;
        [JsonPropertyName("last_activity")]
        public string LastActivity { get; init; } = string.Empty;
        [JsonPropertyName("scope")]
        public string Scope { get; init; } = "auto";
    }
}

public sealed class SessionSearchWorkspace : IDisposable
{
    private static readonly string Root = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "TreasureChest",
        "session-search");
    private bool _disposed;

    private SessionSearchWorkspace(string directory)
    {
        Directory = directory;
        RequestFile = Path.Combine(directory, "request.json");
        ProgressFile = Path.Combine(directory, "progress.json");
        CancelFile = Path.Combine(directory, "cancel.flag");
    }

    public string Directory { get; }
    public string RequestFile { get; }
    public string ProgressFile { get; }
    public string CancelFile { get; }

    public static SessionSearchWorkspace Create()
    {
        System.IO.Directory.CreateDirectory(Root);
        var directory = Path.Combine(Root, Guid.NewGuid().ToString("N"));
        System.IO.Directory.CreateDirectory(directory);
        return new SessionSearchWorkspace(directory);
    }

    public void RequestCancel()
    {
        if (_disposed) return;
        try { File.WriteAllBytes(CancelFile, []); } catch { }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        var fullRoot = Path.GetFullPath(Root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var fullDirectory = Path.GetFullPath(Directory).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!fullDirectory.StartsWith(fullRoot, StringComparison.OrdinalIgnoreCase) ||
            Path.GetFileName(Directory).Length != 32)
            throw new InvalidOperationException("拒绝清理不属于 TreasureChest 会话搜索目录的路径。");
        if (System.IO.Directory.Exists(Directory)) System.IO.Directory.Delete(Directory, recursive: true);
    }
}
