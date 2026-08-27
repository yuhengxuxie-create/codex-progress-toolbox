using System.Diagnostics;

namespace TreasureChest.Core.Services;

public sealed record CommandResult(int ExitCode, string StandardOutput, string StandardError, bool TimedOut);

public static class CommandExecutor
{
    public static async Task<CommandResult> RunAsync(
        string command,
        string? workingDirectory,
        TimeSpan timeout,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(command))
            throw new ArgumentException("命令不能为空。", nameof(command));
        using var process = new Process { StartInfo = CreateShellInfo(command, workingDirectory, capture: true) };
        if (!process.Start())
            throw new InvalidOperationException("命令进程未能启动。");
        var stdoutTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var stderrTask = process.StandardError.ReadToEndAsync(cancellationToken);
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeoutSource.CancelAfter(timeout);
        try
        {
            await process.WaitForExitAsync(timeoutSource.Token).ConfigureAwait(false);
            return new CommandResult(process.ExitCode, await stdoutTask.ConfigureAwait(false), await stderrTask.ConfigureAwait(false), false);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            try { process.Kill(entireProcessTree: true); } catch { }
            return new CommandResult(-1, await stdoutTask.ConfigureAwait(false), await stderrTask.ConfigureAwait(false), true);
        }
    }

    public static Process StartManaged(string command, string? workingDirectory, string? logFile)
    {
        if (string.IsNullOrWhiteSpace(command))
            throw new ArgumentException("启动命令不能为空。", nameof(command));
        var capture = !string.IsNullOrWhiteSpace(logFile);
        var process = new Process
        {
            StartInfo = CreateShellInfo(command, workingDirectory, capture),
            EnableRaisingEvents = true,
        };
        if (capture)
        {
            var fullLog = System.IO.Path.GetFullPath(logFile!);
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(fullLog)!);
            process.OutputDataReceived += (_, e) => AppendLine(fullLog, e.Data);
            process.ErrorDataReceived += (_, e) => AppendLine(fullLog, e.Data);
        }
        if (!process.Start())
            throw new InvalidOperationException("启动命令未创建进程。");
        if (capture)
        {
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        return process;
    }

    private static ProcessStartInfo CreateShellInfo(string command, string? workingDirectory, bool capture)
    {
        var info = new ProcessStartInfo
        {
            FileName = Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe",
            Arguments = "/d /c " + command,
            WorkingDirectory = ResolveWorkingDirectory(workingDirectory),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = capture,
            RedirectStandardError = capture,
            StandardOutputEncoding = capture ? System.Text.Encoding.UTF8 : null,
            StandardErrorEncoding = capture ? System.Text.Encoding.UTF8 : null,
        };
        return info;
    }

    private static string ResolveWorkingDirectory(string? directory) =>
        !string.IsNullOrWhiteSpace(directory) && Directory.Exists(directory)
            ? System.IO.Path.GetFullPath(directory)
            : Environment.CurrentDirectory;

    private static void AppendLine(string file, string? line)
    {
        if (line is null) return;
        try { File.AppendAllText(file, line + Environment.NewLine); } catch { }
    }
}
