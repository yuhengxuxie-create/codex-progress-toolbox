using System.Text.Json;
using System.Text.RegularExpressions;
using TreasureChest.Core.Models;

namespace TreasureChest.Core.Services;

public sealed class PluginCatalog
{
    public const string SupportedSdk = "1";
    private const int MaxPlugins = 64;
    private const long MaxManifestBytes = 256 * 1024;
    private static readonly Regex Identifier = new("^[a-z][a-z0-9_-]{1,47}$", RegexOptions.Compiled);
    private readonly JsonSerializerOptions _json = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    public PluginCatalog(string pluginsRoot) => PluginsRoot = Path.GetFullPath(pluginsRoot);
    public string PluginsRoot { get; }
    public IReadOnlyList<PluginDescriptor> Plugins { get; private set; } = [];

    public async Task<IReadOnlyList<PluginDescriptor>> ScanAsync(CancellationToken cancellationToken = default)
    {
        Directory.CreateDirectory(PluginsRoot);
        var results = new List<PluginDescriptor>();
        foreach (var directory in Directory.EnumerateDirectories(PluginsRoot).OrderBy(path => path, StringComparer.OrdinalIgnoreCase).Take(MaxPlugins))
        {
            cancellationToken.ThrowIfCancellationRequested();
            var manifestPath = Path.Combine(directory, "manifest.json");
            if (!File.Exists(manifestPath)) continue;
            PluginManifest? parsedManifest = null;
            try
            {
                var info = new FileInfo(manifestPath);
                if (info.Length > MaxManifestBytes) throw new InvalidDataException("manifest.json 超过 256 KiB。 ");
                await using var stream = new FileStream(manifestPath, FileMode.Open, FileAccess.Read, FileShare.Read);
                parsedManifest = await JsonSerializer.DeserializeAsync<PluginManifest>(stream, _json, cancellationToken)
                    .ConfigureAwait(false) ?? throw new InvalidDataException("manifest.json 内容为空。");
                results.Add(BuildDescriptor(parsedManifest, directory, manifestPath));
            }
            catch (Exception error) when (error is not OperationCanceledException)
            {
                results.Add(new PluginDescriptor
                {
                    Manifest = parsedManifest ?? new PluginManifest { Id = Path.GetFileName(directory), Name = Path.GetFileName(directory) },
                    RootDirectory = directory,
                    ManifestPath = manifestPath,
                    Tools = [], Sessions = [], Error = error.Message,
                });
            }
        }
        var duplicate = results.Where(item => item.Error is null)
            .GroupBy(item => item.Manifest.Id, StringComparer.OrdinalIgnoreCase).FirstOrDefault(group => group.Count() > 1);
        if (duplicate is not null)
        {
            results = results.Select(item => duplicate.Contains(item)
                ? new PluginDescriptor { Manifest = item.Manifest, RootDirectory = item.RootDirectory, ManifestPath = item.ManifestPath,
                    Tools = [], Sessions = [], Error = "插件 ID 重复。" }
                : item).ToList();
        }
        Plugins = results;
        return Plugins;
    }

    private PluginDescriptor BuildDescriptor(PluginManifest manifest, string root, string manifestPath)
    {
        if (!Identifier.IsMatch(manifest.Id)) throw new InvalidDataException("插件 id 格式无效。");
        if (string.IsNullOrWhiteSpace(manifest.Name)) throw new InvalidDataException("插件 name 不能为空。");
        if (string.IsNullOrWhiteSpace(manifest.Version)) throw new InvalidDataException("插件 version 不能为空。");
        if (manifest.Sdk != SupportedSdk) throw new InvalidDataException($"不支持 SDK {manifest.Sdk}；当前仅支持 {SupportedSdk}。");
        var toolIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var tools = new List<ToolDefinition>();
        foreach (var item in manifest.Contributions?.Tools ?? [])
        {
            ValidateContribution(item.Id, item.Name, item.Entry, toolIds, "工具");
            tools.Add(new ToolDefinition
            {
                Id = $"plugin:{manifest.Id}:tool:{item.Id}", Name = item.Name,
                TargetPath = ResolveInside(root, item.Entry, mustExist: true), Arguments = item.Arguments,
                WorkingDirectory = ResolveInside(root, item.WorkingDirectory, mustExist: true),
                IconPath = string.IsNullOrWhiteSpace(item.Icon) ? ResolveOptionalInside(root, manifest.Icon) : ResolveOptionalInside(root, item.Icon),
                Category = string.IsNullOrWhiteSpace(item.Category) ? "插件工具" : item.Category,
                Enabled = true, SourcePluginId = manifest.Id,
            });
        }
        var sessionIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var sessions = new List<SessionDefinition>();
        foreach (var item in manifest.Contributions?.Sessions ?? [])
        {
            ValidateContribution(item.Id, item.Name, item.StartEntry, sessionIds, "会话");
            sessions.Add(new SessionDefinition
            {
                Id = $"plugin:{manifest.Id}:session:{item.Id}", Name = item.Name, Description = item.Description,
                StartCommand = BuildCommand(ResolveInside(root, item.StartEntry, true), item.StartArguments),
                StopCommand = string.IsNullOrWhiteSpace(item.StopEntry) ? string.Empty : BuildCommand(ResolveInside(root, item.StopEntry, true), item.StopArguments),
                StatusCommand = string.IsNullOrWhiteSpace(item.StatusEntry) ? string.Empty : BuildCommand(ResolveInside(root, item.StatusEntry, true), item.StatusArguments),
                ProcessName = item.ProcessName, WorkingDirectory = ResolveInside(root, item.WorkingDirectory, true),
                LogFilePath = string.IsNullOrWhiteSpace(item.LogFile) ? string.Empty : ResolveInside(root, item.LogFile, false),
                Enabled = true, AutoStart = item.AutoStart, AutoRestart = item.AutoRestart,
                MaxRestartAttempts = Math.Clamp(item.MaxRestartAttempts, 0, 20), SourcePluginId = manifest.Id,
            });
        }
        return new PluginDescriptor { Manifest = manifest, RootDirectory = root, ManifestPath = manifestPath, Tools = tools, Sessions = sessions };
    }

    private static void ValidateContribution(string id, string name, string entry, HashSet<string> ids, string kind)
    {
        if (!Identifier.IsMatch(id)) throw new InvalidDataException($"{kind} id 格式无效。");
        if (!ids.Add(id)) throw new InvalidDataException($"{kind} id 重复：{id}");
        if (string.IsNullOrWhiteSpace(name) || string.IsNullOrWhiteSpace(entry)) throw new InvalidDataException($"{kind} name/entry 不能为空。");
    }

    private static string ResolveInside(string root, string relative, bool mustExist)
    {
        if (Path.IsPathRooted(relative)) throw new InvalidDataException("插件路径必须相对插件根目录。");
        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var full = Path.GetFullPath(Path.Combine(rootFull, string.IsNullOrWhiteSpace(relative) ? "." : relative));
        if (!full.StartsWith(rootFull, StringComparison.OrdinalIgnoreCase) && !full.Equals(rootFull.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("插件路径越出插件根目录。");
        if (mustExist && !File.Exists(full) && !Directory.Exists(full)) throw new FileNotFoundException("插件入口或目录不存在。", full);
        return full;
    }

    private static string ResolveOptionalInside(string root, string relative) =>
        string.IsNullOrWhiteSpace(relative) ? string.Empty : ResolveInside(root, relative, true);

    private static string BuildCommand(string entry, string arguments)
    {
        var extension = Path.GetExtension(entry).ToLowerInvariant();
        var quoted = $"\"{entry}\"";
        return extension switch
        {
            ".ps1" => $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {quoted} {arguments}".Trim(),
            ".bat" or ".cmd" => $"call {quoted} {arguments}".Trim(),
            _ => $"{quoted} {arguments}".Trim(),
        };
    }
}
