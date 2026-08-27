namespace TreasureChest.Core.Services;

public static class ToolPath
{
    public static string NormalizeInput(string? value)
    {
        var normalized = (value ?? string.Empty).Trim();
        if (normalized.Length >= 2 && IsMatchingQuotePair(normalized[0], normalized[^1]))
            normalized = normalized[1..^1].Trim();
        return normalized;
    }

    public static string NormalizeWorkingDirectoryInput(string? value, string? targetPath)
    {
        var workingDirectory = NormalizeInput(value);
        if (workingDirectory.Length == 0) return string.Empty;

        var resolvedWorkingDirectory = Resolve(workingDirectory);
        if (File.Exists(resolvedWorkingDirectory))
            return Path.GetDirectoryName(workingDirectory) ?? string.Empty;

        var target = NormalizeInput(targetPath);
        if (workingDirectory.Equals(target, StringComparison.OrdinalIgnoreCase) &&
            !Directory.Exists(Resolve(target)) && Path.HasExtension(target))
            return Path.GetDirectoryName(target) ?? string.Empty;

        return workingDirectory;
    }

    public static string Resolve(string? value) =>
        Environment.ExpandEnvironmentVariables(NormalizeInput(value));

    public static string ResolveWorkingDirectory(string? value, string resolvedTarget)
    {
        var workingDirectory = Resolve(value);
        if (Directory.Exists(workingDirectory)) return workingDirectory;
        if (File.Exists(workingDirectory))
            return Path.GetDirectoryName(workingDirectory) ?? Environment.CurrentDirectory;
        return Directory.Exists(resolvedTarget)
            ? resolvedTarget
            : Path.GetDirectoryName(resolvedTarget) ?? Environment.CurrentDirectory;
    }

    private static bool IsMatchingQuotePair(char first, char last) =>
        (first, last) is ('"', '"') or ('\'', '\'') or ('“', '”') or ('‘', '’');
}
