namespace TreasureChest;

internal static class AppPaths
{
    public static string Root { get; } = FindRoot();
    public static string ConfigFile => Path.Combine(Root, "config.json");
    public static string LogFile => Path.Combine(Root, "logs", "TreasureChest.log");
    public static string ProgressNotificationRoot { get; } = FindProgressNotificationRoot();
    public static string ProgressPythonPath { get; } = FindProgressPythonPath();

    private static string FindRoot()
    {
        var configured = Environment.GetEnvironmentVariable("TREASURECHEST_ROOT");
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(Path.Combine(configured, "treasurechest.root")))
            return Path.GetFullPath(configured);
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        for (var depth = 0; current is not null && depth < 8; depth++, current = current.Parent)
            if (File.Exists(Path.Combine(current.FullName, "treasurechest.root"))) return current.FullName;
        return Path.GetFullPath(AppContext.BaseDirectory);
    }

    private static string FindProgressNotificationRoot()
    {
        var configured = Environment.GetEnvironmentVariable("PROGRESS_WX_ROOT");
        var bundled = Path.Combine(Root, "components", "codex-feishu");
        var legacyBundle = Path.Combine(Root, "components", "ProgressChecking(WX)");
        foreach (var candidate in new[] { configured, bundled, legacyBundle })
        {
            if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(Path.Combine(candidate, "progress-wx.py")))
                return Path.GetFullPath(candidate);
        }
        // 新安装在组件复制完成前也应获得稳定、可预期的目标路径。
        return Path.GetFullPath(bundled);
    }

    private static string FindProgressPythonPath()
    {
        var configured = Environment.GetEnvironmentVariable("PROGRESS_WX_PYTHON");
        var colocated = Path.Combine(ProgressNotificationRoot, "Python313-ProgressWX", "python.exe");
        var bundled = Path.Combine(Root, "components", "Python313-ProgressWX", "python.exe");
        foreach (var candidate in new[] { configured, colocated, bundled })
        {
            if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
                return Path.GetFullPath(candidate);
        }
        return Path.GetFullPath(bundled);
    }
}
