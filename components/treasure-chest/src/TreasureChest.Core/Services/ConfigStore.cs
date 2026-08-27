using System.Text.Json;
using TreasureChest.Core.Models;

namespace TreasureChest.Core.Services;

public sealed class ConfigStore
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly JsonSerializerOptions _options = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        AllowTrailingCommas = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
    };

    public ConfigStore(string path, string root)
    {
        Path = System.IO.Path.GetFullPath(path);
        Root = System.IO.Path.GetFullPath(root);
    }

    public string Path { get; }
    public string Root { get; }

    public async Task<AppConfiguration> LoadAsync(CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (!File.Exists(Path))
            {
                var initial = AppConfiguration.CreateDefault(Root);
                Validate(initial);
                await SaveUnsafeAsync(initial, cancellationToken).ConfigureAwait(false);
                return initial;
            }

            await using var stream = new FileStream(Path, FileMode.Open, FileAccess.Read, FileShare.Read);
            var config = await JsonSerializer.DeserializeAsync<AppConfiguration>(stream, _options, cancellationToken)
                .ConfigureAwait(false) ?? throw new InvalidDataException("config.json 内容为空。");
            Normalize(config);
            Validate(config);
            return config;
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task SaveAsync(AppConfiguration config, CancellationToken cancellationToken = default)
    {
        Normalize(config);
        Validate(config);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await SaveUnsafeAsync(config, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task ExportAsync(AppConfiguration config, string destination, CancellationToken cancellationToken = default)
    {
        Normalize(config);
        Validate(config);
        var fullPath = System.IO.Path.GetFullPath(destination);
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(fullPath)!);
        await using var stream = new FileStream(fullPath, FileMode.Create, FileAccess.Write, FileShare.None);
        await JsonSerializer.SerializeAsync(stream, config, _options, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    public async Task<AppConfiguration> ImportAsync(string source, CancellationToken cancellationToken = default)
    {
        var fullPath = System.IO.Path.GetFullPath(source);
        await using var stream = new FileStream(fullPath, FileMode.Open, FileAccess.Read, FileShare.Read);
        var config = await JsonSerializer.DeserializeAsync<AppConfiguration>(stream, _options, cancellationToken)
            .ConfigureAwait(false) ?? throw new InvalidDataException("导入文件内容为空。");
        Normalize(config);
        Validate(config);
        await SaveAsync(config, cancellationToken).ConfigureAwait(false);
        return config;
    }

    public async Task<AppConfiguration> ResetAsync(CancellationToken cancellationToken = default)
    {
        var config = AppConfiguration.CreateDefault(Root);
        await SaveAsync(config, cancellationToken).ConfigureAwait(false);
        return config;
    }

    public static void Validate(AppConfiguration config)
    {
        if (config.Settings.RefreshIntervalSeconds is < 1 or > 300)
            throw new InvalidDataException("刷新间隔必须为 1 至 300 秒。");
        if (config.Sessions.Any(item => string.IsNullOrWhiteSpace(item.Id) || string.IsNullOrWhiteSpace(item.Name)))
            throw new InvalidDataException("会话 ID 和名称不能为空。");
        if (config.Tools.Any(item => string.IsNullOrWhiteSpace(item.Id) || string.IsNullOrWhiteSpace(item.Name)))
            throw new InvalidDataException("工具 ID 和名称不能为空。");
        if (config.Sessions.GroupBy(item => item.Name.Trim(), StringComparer.OrdinalIgnoreCase).Any(group => group.Count() > 1))
            throw new InvalidDataException("会话名称必须唯一。");
        if (config.Tools.GroupBy(item => item.Name.Trim(), StringComparer.OrdinalIgnoreCase).Any(group => group.Count() > 1))
            throw new InvalidDataException("工具名称必须唯一。");
        if (config.Sessions.Any(item => item.MaxRestartAttempts is < 0 or > 20))
            throw new InvalidDataException("自动重启次数必须为 0 至 20。");
    }

    private async Task SaveUnsafeAsync(AppConfiguration config, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
        var temporary = Path + ".tmp-" + Guid.NewGuid().ToString("N");
        try
        {
            await using (var stream = new FileStream(
                temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096,
                FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await JsonSerializer.SerializeAsync(stream, config, _options, cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
            }
            File.Move(temporary, Path, true);
        }
        finally
        {
            File.Delete(temporary);
        }
    }

    private static void Normalize(AppConfiguration config)
    {
        config.Settings ??= new GeneralSettings();
        config.Sessions ??= [];
        config.Tools ??= [];
        config.PluginEnabled ??= new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        foreach (var session in config.Sessions)
        {
            session.Id = string.IsNullOrWhiteSpace(session.Id) ? Guid.NewGuid().ToString("N") : session.Id.Trim();
            session.Name = session.Name.Trim();
        }
        foreach (var tool in config.Tools)
        {
            tool.Id = string.IsNullOrWhiteSpace(tool.Id) ? Guid.NewGuid().ToString("N") : tool.Id.Trim();
            tool.Name = tool.Name.Trim();
            tool.TargetPath = ToolPath.NormalizeInput(tool.TargetPath);
            tool.WorkingDirectory = ToolPath.NormalizeWorkingDirectoryInput(tool.WorkingDirectory, tool.TargetPath);
            tool.IconPath = ToolPath.NormalizeInput(tool.IconPath);
        }
    }
}
