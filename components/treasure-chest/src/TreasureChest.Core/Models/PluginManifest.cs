using System.Text.Json.Serialization;

namespace TreasureChest.Core.Models;

public sealed class PluginManifest
{
    public string Id { get; set; } = string.Empty;
    public string Name { get; set; } = string.Empty;
    public string Version { get; set; } = string.Empty;
    public string Sdk { get; set; } = string.Empty;
    public string Author { get; set; } = string.Empty;
    public string Description { get; set; } = string.Empty;
    public string Icon { get; set; } = string.Empty;
    public bool EnabledByDefault { get; set; } = true;
    public PluginContributions Contributions { get; set; } = new();
}

public sealed class PluginContributions
{
    public List<PluginToolContribution> Tools { get; set; } = new();
    public List<PluginSessionContribution> Sessions { get; set; } = new();
}

public sealed class PluginToolContribution
{
    public string Id { get; set; } = string.Empty;
    public string Name { get; set; } = string.Empty;
    public string Entry { get; set; } = string.Empty;
    public string Arguments { get; set; } = string.Empty;
    public string WorkingDirectory { get; set; } = ".";
    public string Icon { get; set; } = string.Empty;
    public string Category { get; set; } = "插件工具";
}

public sealed class PluginSessionContribution
{
    public string Id { get; set; } = string.Empty;
    public string Name { get; set; } = string.Empty;
    public string Description { get; set; } = string.Empty;
    public string StartEntry { get; set; } = string.Empty;
    public string StartArguments { get; set; } = string.Empty;
    public string StopEntry { get; set; } = string.Empty;
    public string StopArguments { get; set; } = string.Empty;
    public string StatusEntry { get; set; } = string.Empty;
    public string StatusArguments { get; set; } = string.Empty;
    public string ProcessName { get; set; } = string.Empty;
    public string WorkingDirectory { get; set; } = ".";
    public string LogFile { get; set; } = string.Empty;
    public bool AutoStart { get; set; }
    public bool AutoRestart { get; set; }
    public int MaxRestartAttempts { get; set; } = 3;
}

public sealed class PluginDescriptor
{
    public required PluginManifest Manifest { get; init; }
    public required string RootDirectory { get; init; }
    public required string ManifestPath { get; init; }
    public required IReadOnlyList<ToolDefinition> Tools { get; init; }
    public required IReadOnlyList<SessionDefinition> Sessions { get; init; }
    public string? Error { get; init; }
}
