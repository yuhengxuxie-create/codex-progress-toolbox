using System.Diagnostics;
using TreasureChest.Core.Models;

namespace TreasureChest.Core.Services;

public sealed record ToolRunResult(bool Started, int? ExitCode, string Message);

public static class ToolRunner
{
    public static async Task<ToolRunResult> RunAsync(ToolDefinition tool, CancellationToken cancellationToken = default)
    {
        if (!tool.Enabled) throw new InvalidOperationException("工具已禁用。");
        if (string.IsNullOrWhiteSpace(tool.TargetPath)) throw new InvalidOperationException("工具目标路径为空。");
        var target = ToolPath.Resolve(tool.TargetPath);
        if (!File.Exists(target) && !Directory.Exists(target))
            throw new FileNotFoundException("工具目标不存在。", target);

        var info = BuildStartInfo(target, tool.Arguments, tool.WorkingDirectory);
        var process = Process.Start(info);
        if (process is null) return new ToolRunResult(true, null, "已交给 Windows 打开。");
        using (process)
        {
            if (Directory.Exists(target) || Path.GetExtension(target).Equals(".lnk", StringComparison.OrdinalIgnoreCase))
                return new ToolRunResult(true, null, "已启动。");
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
            return new ToolRunResult(true, process.ExitCode, $"已结束，退出码 {process.ExitCode}。");
        }
    }

    private static ProcessStartInfo BuildStartInfo(string target, string arguments, string workingDirectory)
    {
        var extension = Path.GetExtension(target).ToLowerInvariant();
        var directory = ToolPath.ResolveWorkingDirectory(workingDirectory, target);
        if (extension == ".ps1")
            return new ProcessStartInfo("powershell.exe", $"-NoProfile -ExecutionPolicy Bypass -File \"{target}\" {arguments}")
            { UseShellExecute = true, WorkingDirectory = directory };
        if (extension is ".cmd" or ".bat")
            return new ProcessStartInfo(Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe", $"/d /s /c call \"{target}\" {arguments}")
            { UseShellExecute = true, WorkingDirectory = directory };
        return new ProcessStartInfo(target, arguments ?? string.Empty)
        {
            UseShellExecute = true,
            WorkingDirectory = directory,
        };
    }
}
