namespace TreasureChest.Services;

using TreasureChest.Core.Services;

internal sealed class NotificationService
{
    private readonly ShellTrayIcon _trayIcon;
    private readonly Func<bool> _enabled;
    private readonly AppLogger _logger;
    public NotificationService(ShellTrayIcon trayIcon, Func<bool> enabled, AppLogger logger)
    {
        _trayIcon = trayIcon;
        _enabled = enabled;
        _logger = logger;
    }

    public ShellNotifyResult Show(string title, string message)
    {
        if (!_enabled()) return ShellNotifyResult.NotAttempted;
        var result = _trayIcon.ShowBalloon(title, message.Length > 240 ? message[..240] : message);
        if (!result.Succeeded)
            _logger.Error($"Windows 通知提交失败：{result.Failure}，nativeError={result.NativeErrorCode}");
        return result;
    }
}
