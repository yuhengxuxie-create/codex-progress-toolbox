namespace TreasureChest.Services;

internal sealed class NotificationService
{
    private readonly NotifyIcon _trayIcon;
    private readonly Func<bool> _enabled;
    public NotificationService(NotifyIcon trayIcon, Func<bool> enabled)
    {
        _trayIcon = trayIcon;
        _enabled = enabled;
    }

    public void Show(string title, string message, ToolTipIcon icon = ToolTipIcon.Info)
    {
        if (!_enabled()) return;
        _trayIcon.BalloonTipTitle = title;
        _trayIcon.BalloonTipText = message.Length > 240 ? message[..240] : message;
        _trayIcon.BalloonTipIcon = icon;
        _trayIcon.ShowBalloonTip(3500);
    }
}
