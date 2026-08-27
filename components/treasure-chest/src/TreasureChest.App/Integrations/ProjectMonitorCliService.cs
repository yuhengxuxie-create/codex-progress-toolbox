using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;
using TreasureChest.Core.Services;

namespace TreasureChest.Integrations;

public sealed record ProjectMonitorItem(
    string ThreadId,
    string Title,
    string Classification,
    string Origin,
    DateTimeOffset? LastActivityAt,
    DateTimeOffset? ExpiresAt)
{
    public bool IsManual => Origin.Equals("manual", StringComparison.OrdinalIgnoreCase);
}

public sealed record ProjectMonitorSettings(
    bool AutoMonitoringEnabled,
    bool? Changed,
    DateTimeOffset? EffectiveAt);

/// <summary>
/// FeiShuBOT 项目监测 CLI 的唯一适配入口。监测来源、过期和抑制规则均由 FeiShuBOT 负责。
/// </summary>
public sealed class ProjectMonitorCliService
{
    public const int SupportedSchemaVersion = 1;
    private static readonly Regex ThreadIdPattern = new(
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        RegexOptions.Compiled | RegexOptions.IgnoreCase);

    private readonly string _projectRoot;
    private readonly string _python;
    private readonly string _entry;
    private readonly string _config;

    public ProjectMonitorCliService(string projectRoot, string python)
    {
        _projectRoot = Path.GetFullPath(projectRoot);
        _python = Path.GetFullPath(python);
        _entry = Path.Combine(_projectRoot, "progress-wx.py");
        _config = Path.Combine(_projectRoot, "config.yaml");
    }

    public async Task<IReadOnlyList<ProjectMonitorItem>> ListAsync(CancellationToken cancellationToken = default)
    {
        var json = await ExecuteAsync(["monitor-list", "--json"], "读取项目监测列表", cancellationToken)
            .ConfigureAwait(false);
        return ParseListJson(json);
    }

    public async Task AddAsync(string threadId, CancellationToken cancellationToken = default)
    {
        var id = NormalizeThreadId(threadId);
        var json = await ExecuteAsync(["monitor-add", "--thread-id", id, "--json"], "手动添加项目监测", cancellationToken)
            .ConfigureAwait(false);
        ValidateMutationJson(json);
    }

    public async Task RemoveAsync(string threadId, CancellationToken cancellationToken = default)
    {
        var id = NormalizeThreadId(threadId);
        var json = await ExecuteAsync(["monitor-remove", "--thread-id", id, "--json"], "明确移除项目监测", cancellationToken)
            .ConfigureAwait(false);
        ValidateMutationJson(json);
    }

    public async Task<ProjectMonitorSettings> GetSettingsAsync(CancellationToken cancellationToken = default)
    {
        var json = await ExecuteAsync(["monitor-settings", "--json"], "读取自动监测设置", cancellationToken)
            .ConfigureAwait(false);
        return ParseSettingsJson(json, requireChanged: false);
    }

    public async Task<ProjectMonitorSettings> SetAutoMonitoringAsync(bool enabled, CancellationToken cancellationToken = default)
    {
        var json = await ExecuteAsync(
                ["monitor-settings", "--auto-enabled", enabled ? "true" : "false", "--json"],
                enabled ? "开启自动监测" : "关闭自动监测",
                cancellationToken)
            .ConfigureAwait(false);
        return ParseSettingsJson(json, requireChanged: true);
    }

    public static string NormalizeThreadId(string value)
    {
        var clean = (value ?? string.Empty).Trim().Trim('"', '\'');
        const string prefix = "codex://threads/";
        if (clean.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) clean = clean[prefix.Length..];
        clean = clean.TrimEnd('/');
        if (!ThreadIdPattern.IsMatch(clean))
            throw new FormatException("请输入完整的 Codex 任务 ID，或 codex://threads/<任务 ID>。");
        return clean.ToLowerInvariant();
    }

    public static IReadOnlyList<ProjectMonitorItem> ParseListJson(string json)
    {
        if (string.IsNullOrWhiteSpace(json)) throw new InvalidDataException("项目监测 CLI 返回了空 JSON。");
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("项目监测列表 JSON 顶层必须是对象。");
        ValidateSchemaVersion(root);
        var itemsElement = FindItemsArray(root);
        var items = new List<ProjectMonitorItem>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var element in itemsElement.EnumerateArray())
        {
            if (element.ValueKind != JsonValueKind.Object)
                throw new InvalidDataException("项目监测列表包含非对象条目。");
            var id = NormalizeThreadId(RequiredString(element, "thread_id"));
            if (!seen.Add(id)) throw new InvalidDataException($"项目监测列表包含重复任务 ID：{id}");
            var origin = RequiredString(element, "origin").Trim().ToLowerInvariant();
            if (origin is not ("manual" or "auto"))
                throw new InvalidDataException($"任务 {id} 的 origin 必须是 manual 或 auto。");
            items.Add(new ProjectMonitorItem(
                id,
                OptionalString(element, "title") ?? "未命名会话",
                ReadClassification(element),
                origin,
                ReadTimestamp(element, "last_activity_at"),
                ReadTimestamp(element, "expires_at")));
        }
        return items;
    }

    public static ProjectMonitorSettings ParseSettingsJson(string json, bool requireChanged)
    {
        if (string.IsNullOrWhiteSpace(json)) throw new InvalidDataException("项目监测设置 CLI 返回了空 JSON。");
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("项目监测设置 JSON 顶层必须是对象。");
        ValidateSchemaVersion(root);
        var enabled = RequiredBoolean(root, "auto_monitoring_enabled");
        var changed = OptionalBoolean(root, "changed");
        if (requireChanged && !changed.HasValue)
            throw new InvalidDataException("项目监测设置变更结果缺少 changed。");
        return new ProjectMonitorSettings(enabled, changed, ReadTimestamp(root, "effective_at"));
    }

    private async Task<string> ExecuteAsync(IReadOnlyList<string> arguments, string action, CancellationToken cancellationToken)
    {
        EnsureRuntimeExists();
        // CommandExecutor 通过 cmd.exe /c 执行；首个令牌若直接以引号开头，cmd 会对引号进行特殊解析。
        // call 同时兼容 EXE、CMD 和 BAT，并可安全启动包含空格的完整路径。
        var launcher = "call " + Quote(_python);
        var command = string.Join(" ", new[] { launcher, Quote(_entry), "--config", Quote(_config) }
            .Concat(arguments.Select(Quote)));
        var result = await CommandExecutor.RunAsync(command, _projectRoot, TimeSpan.FromSeconds(30), cancellationToken)
            .ConfigureAwait(false);
        if (result.TimedOut) throw new TimeoutException($"{action}超过 30 秒。");
        if (result.ExitCode != 0)
        {
            var detail = LastLine(result.StandardError) ?? LastLine(result.StandardOutput) ?? $"退出码 {result.ExitCode}";
            if (result.StandardError.Contains("invalid choice", StringComparison.OrdinalIgnoreCase) ||
                result.StandardError.Contains("unrecognized arguments", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException($"FeiShuBOT 项目监测 CLI 尚未就绪：{detail}");
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

    private static JsonElement FindItemsArray(JsonElement root)
    {
        foreach (var name in new[] { "items", "monitors", "entries" })
            if (TryGetProperty(root, name, out var value) && value.ValueKind == JsonValueKind.Array) return value;
        if (TryGetProperty(root, "data", out var data))
        {
            if (data.ValueKind == JsonValueKind.Array) return data;
            if (data.ValueKind == JsonValueKind.Object)
                foreach (var name in new[] { "items", "monitors", "entries" })
                    if (TryGetProperty(data, name, out var value) && value.ValueKind == JsonValueKind.Array) return value;
        }
        throw new InvalidDataException("项目监测列表 JSON 缺少 items 数组。");
    }

    private static void ValidateMutationJson(string json)
    {
        if (string.IsNullOrWhiteSpace(json)) throw new InvalidDataException("项目监测 CLI 变更命令返回了空 JSON。");
        using var document = JsonDocument.Parse(json);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("项目监测 CLI 变更结果必须是 JSON 对象。");
        if (TryGetProperty(document.RootElement, "schema_version", out _)) ValidateSchemaVersion(document.RootElement);
    }

    private static void ValidateSchemaVersion(JsonElement root)
    {
        if (!TryGetProperty(root, "schema_version", out var value))
            throw new InvalidDataException("项目监测 JSON 缺少 schema_version。");
        var version = value.ValueKind switch
        {
            JsonValueKind.Number when value.TryGetInt32(out var number) => number,
            JsonValueKind.String when int.TryParse(value.GetString(), out var number) => number,
            _ => -1,
        };
        if (version != SupportedSchemaVersion)
            throw new InvalidDataException($"不支持项目监测 JSON schema_version={value}，百宝箱当前支持版本 {SupportedSchemaVersion}。");
    }

    private static string ReadClassification(JsonElement item)
    {
        foreach (var name in new[] { "group/project", "project", "group" })
        {
            if (!TryGetProperty(item, name, out var value)) continue;
            if (value.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(value.GetString())) return value.GetString()!.Trim();
            if (value.ValueKind == JsonValueKind.Object)
                foreach (var nested in new[] { "name", "title", "id" })
                    if (TryGetProperty(value, nested, out var text) && text.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(text.GetString()))
                        return text.GetString()!.Trim();
        }
        return "个人对话";
    }

    private static DateTimeOffset? ReadTimestamp(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined) return null;
        if (value.ValueKind == JsonValueKind.String && DateTimeOffset.TryParse(
                value.GetString(), CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var timestamp))
            return timestamp;
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out var raw))
            return Math.Abs(raw) >= 10_000_000_000
                ? DateTimeOffset.FromUnixTimeMilliseconds(raw)
                : DateTimeOffset.FromUnixTimeSeconds(raw);
        throw new InvalidDataException($"字段 {name} 不是有效的 ISO-8601 或 Unix 时间戳。");
    }

    private static string RequiredString(JsonElement item, string name) =>
        OptionalString(item, name) ?? throw new InvalidDataException($"项目监测条目缺少 {name}。");

    private static bool RequiredBoolean(JsonElement item, string name) =>
        OptionalBoolean(item, name) ?? throw new InvalidDataException($"项目监测设置缺少布尔字段 {name}。");

    private static bool? OptionalBoolean(JsonElement item, string name)
    {
        if (!TryGetProperty(item, name, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined) return null;
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => throw new InvalidDataException($"字段 {name} 必须是 JSON 布尔值。"),
        };
    }

    private static string? OptionalString(JsonElement item, string name) =>
        TryGetProperty(item, name, out var value) && value.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(value.GetString())
            ? value.GetString()!.Trim()
            : null;

    private static bool TryGetProperty(JsonElement element, string name, out JsonElement value)
    {
        foreach (var property in element.EnumerateObject())
            if (property.Name.Equals(name, StringComparison.OrdinalIgnoreCase))
            {
                value = property.Value;
                return true;
            }
        value = default;
        return false;
    }

    private static string Quote(string value) => "\"" + value.Replace("\"", "\"\"", StringComparison.Ordinal) + "\"";
    private static string? LastLine(string value) => value.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
        .Select(line => line.Trim()).LastOrDefault(line => line.Length > 0);
}
