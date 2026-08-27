namespace TreasureChest.Core.Models;

public sealed class AppConfiguration
{
    public int Version { get; set; } = 1;
    public GeneralSettings Settings { get; set; } = new();
    public List<SessionDefinition> Sessions { get; set; } = new();
    public List<ToolDefinition> Tools { get; set; } = new();
    public Dictionary<string, bool> PluginEnabled { get; set; } = new(StringComparer.OrdinalIgnoreCase);

    public static AppConfiguration CreateDefault(string treasureChestRoot)
    {
        var progressRoot = Path.Combine(treasureChestRoot, "components", "FeiShuBOT");
        string Ps(string script) =>
            $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{Path.Combine(progressRoot, "scripts", script)}\" -ToolsRoot \"{Path.Combine(treasureChestRoot, "components")}\"";

        return new AppConfiguration
        {
            Settings = new GeneralSettings
            {
                NotificationsEnabled = true,
                AutoStartEnabled = false,
                StartMinimizedToTray = true,
                RefreshIntervalSeconds = 5,
            },
            Sessions =
            [
                new SessionDefinition
                {
                    Name = "Codex管理（飞书）",
                    Description = "通过飞书监控、查询、新建和续接 Codex 项目与会话。",
                    StartCommand = Ps("start.ps1"),
                    StopCommand = Ps("stop.ps1"),
                    StatusCommand = Ps("status.ps1"),
                    WorkingDirectory = progressRoot,
                    LogFilePath = Path.Combine(progressRoot, "logs", "progress-wx.log"),
                    Enabled = true,
                    AutoStart = false,
                    AutoRestart = false,
                    MaxRestartAttempts = 5,
                },
            ],
            Tools =
            [
                new ToolDefinition
                {
                    Name = "飞书机器人项目",
                    TargetPath = progressRoot,
                    Category = "开发工具",
                },
                new ToolDefinition
                {
                    Name = "TreasureChest 目录",
                    TargetPath = treasureChestRoot,
                    Category = "管理工具",
                },
            ],
        };
    }
}

public sealed class GeneralSettings
{
    public bool NotificationsEnabled { get; set; } = true;
    public bool AutoStartEnabled { get; set; } = true;
    public bool StartMinimizedToTray { get; set; } = true;
    public int RefreshIntervalSeconds { get; set; } = 5;
}

public sealed class SessionDefinition
{
    public string Id { get; set; } = Guid.NewGuid().ToString("N");
    public string Name { get; set; } = string.Empty;
    public string Description { get; set; } = string.Empty;
    public bool Enabled { get; set; } = true;
    public string StartCommand { get; set; } = string.Empty;
    public string StopCommand { get; set; } = string.Empty;
    public string StatusCommand { get; set; } = string.Empty;
    public string ProcessName { get; set; } = string.Empty;
    public string WorkingDirectory { get; set; } = string.Empty;
    public string LogFilePath { get; set; } = string.Empty;
    public bool AutoStart { get; set; }
    public bool AutoRestart { get; set; }
    public int MaxRestartAttempts { get; set; } = 3;
    public DateTimeOffset? LastExecutedAt { get; set; }
    public string SourcePluginId { get; set; } = string.Empty;
}

public sealed class ToolDefinition
{
    public string Id { get; set; } = Guid.NewGuid().ToString("N");
    public string Name { get; set; } = string.Empty;
    public string TargetPath { get; set; } = string.Empty;
    public string Arguments { get; set; } = string.Empty;
    public string WorkingDirectory { get; set; } = string.Empty;
    public string IconPath { get; set; } = string.Empty;
    public string Category { get; set; } = "常用工具";
    public bool Enabled { get; set; } = true;
    public string SourcePluginId { get; set; } = string.Empty;
}

public enum SessionStatus
{
    Unknown,
    Running,
    Stopped,
    Error,
    Disabled,
}

public sealed record SessionSnapshot(
    string SessionId,
    SessionStatus Status,
    string Message,
    int RestartAttempts,
    DateTimeOffset CheckedAt);
