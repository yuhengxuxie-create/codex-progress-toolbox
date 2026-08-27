using System.Text.Json;
using System.Text.Json.Serialization;
using TreasureChest.Core.Services;

namespace TreasureChest.Integrations;

public sealed record CodexThreadInfo(
    string Id,
    string Title,
    string Cwd,
    bool Archived,
    string ThreadSource = "user",
    string? ProjectId = null,
    string? ProjectName = null,
    long? UpdatedAtMs = null)
{
    public string DisplayTitle => string.IsNullOrWhiteSpace(Title) ? "未命名会话" : Title;
    public string Classification => string.IsNullOrWhiteSpace(ProjectName) ? "个人对话" : ProjectName;
}

public sealed class CodexThreadCatalogService
{
    private readonly string _projectRoot;
    private readonly string _python;

    public CodexThreadCatalogService(string projectRoot, string python)
    {
        _projectRoot = Path.GetFullPath(projectRoot);
        _python = Path.GetFullPath(python);
    }

    public async Task<IReadOnlyList<CodexThreadInfo>> LoadAsync(CancellationToken cancellationToken = default)
    {
        var entry = Path.Combine(_projectRoot, "progress-wx.py");
        var config = Path.Combine(_projectRoot, "config.yaml");
        if (!File.Exists(_python) || !File.Exists(entry) || !File.Exists(config))
            throw new FileNotFoundException("进度通知的 Python、入口或配置文件缺失。");
        var command = $"call \"{_python}\" \"{entry}\" --config \"{config}\" list-threads --json";
        var result = await CommandExecutor.RunAsync(command, _projectRoot, TimeSpan.FromSeconds(30), cancellationToken).ConfigureAwait(false);
        if (result.TimedOut) throw new TimeoutException("读取 Codex 会话列表超过 30 秒。");
        if (result.ExitCode != 0) throw new InvalidOperationException(FirstLine(result.StandardError) ?? $"会话列表命令退出码 {result.ExitCode}。");
        var json = result.StandardOutput.Trim();
        var records = JsonSerializer.Deserialize<List<CatalogRecord>>(json, new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
            ?? throw new InvalidDataException("Codex 会话列表为空。");
        return records
            .Where(item => !string.IsNullOrWhiteSpace(item.Id)
                && string.Equals(item.ThreadSource, "user", StringComparison.OrdinalIgnoreCase))
            .Select(item => new CodexThreadInfo(
                item.Id!, Clean(item.Title), Clean(item.Cwd), item.Archived,
                Clean(item.ThreadSource), CleanOrNull(item.ProjectId), CleanOrNull(item.ProjectName),
                item.UpdatedAtMs))
            .DistinctBy(item => item.Id, StringComparer.OrdinalIgnoreCase)
            .ToArray();
    }

    private static string Clean(string? value) => string.Join(" ", (value ?? string.Empty).Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
    private static string? CleanOrNull(string? value) => Clean(value) is { Length: > 0 } result ? result : null;
    private static string? FirstLine(string value) => value.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries).Select(line => line.Trim()).FirstOrDefault();

    private sealed class CatalogRecord
    {
        [JsonPropertyName("id")]
        public string? Id { get; set; }
        [JsonPropertyName("title")]
        public string? Title { get; set; }
        [JsonPropertyName("cwd")]
        public string? Cwd { get; set; }
        [JsonPropertyName("archived")]
        public bool Archived { get; set; }
        [JsonPropertyName("thread_source")]
        public string? ThreadSource { get; set; }
        [JsonPropertyName("project_id")]
        public string? ProjectId { get; set; }
        [JsonPropertyName("project_name")]
        public string? ProjectName { get; set; }
        [JsonPropertyName("updated_at_ms")]
        public long? UpdatedAtMs { get; set; }
    }
}
