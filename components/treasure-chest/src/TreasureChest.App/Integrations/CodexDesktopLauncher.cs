using System.Diagnostics;

namespace TreasureChest.Integrations;

public sealed record CodexThreadOpenResult(bool Opened, string Message);

/// <summary>
/// 仅在 Codex Desktop 已运行时跳转任务，避免 codex:// URI 意外强行启动应用。
/// </summary>
public sealed class CodexDesktopLauncher
{
    private readonly Func<bool> _isDesktopRunning;
    private readonly Action<string> _openUri;

    public CodexDesktopLauncher(Func<bool>? isDesktopRunning = null, Action<string>? openUri = null)
    {
        _isDesktopRunning = isDesktopRunning ?? IsCodexDesktopRunning;
        _openUri = openUri ?? (uri => Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true }));
    }

    public CodexThreadOpenResult TryOpen(string threadId)
    {
        var id = ProjectMonitorCliService.NormalizeThreadId(threadId);
        if (!_isDesktopRunning())
            return new CodexThreadOpenResult(false, "Codex Desktop 当前没有运行，已取消跳转；请先打开 Codex，再双击会话。");
        try
        {
            _openUri("codex://threads/" + id);
            return new CodexThreadOpenResult(true, "已在正在运行的 Codex Desktop 中打开所选会话。");
        }
        catch (Exception error)
        {
            return new CodexThreadOpenResult(false, "无法在 Codex Desktop 打开会话：" + error.Message);
        }
    }

    public static bool IsCodexDesktopRunning()
    {
        foreach (var process in Process.GetProcesses())
        {
            using (process)
            {
                try
                {
                    if (process.ProcessName.Equals("ChatGPT", StringComparison.OrdinalIgnoreCase)) return true;
                    if (!process.ProcessName.Contains("Codex", StringComparison.OrdinalIgnoreCase)) continue;
                    if (process.MainWindowHandle != IntPtr.Zero &&
                        (process.MainWindowTitle.Contains("Codex", StringComparison.OrdinalIgnoreCase) ||
                         process.MainWindowTitle.Contains("ChatGPT", StringComparison.OrdinalIgnoreCase))) return true;
                }
                catch (InvalidOperationException) { }
                catch (System.ComponentModel.Win32Exception) { }
            }
        }
        return false;
    }
}
