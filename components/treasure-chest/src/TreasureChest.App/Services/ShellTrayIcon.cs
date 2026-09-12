using System.Runtime.InteropServices;

namespace TreasureChest.Services;

internal sealed class ShellTrayIcon : NativeWindow, IDisposable
{
    internal static readonly Guid ProductIdentityGuid =
        Guid.Parse("C49CFEA1-FD78-4F0D-8A32-1F68B9F10475");
    private const int WmApp = 0x8000;
    private const int CallbackMessage = WmApp + 0x42;
    private const int WmContextMenu = 0x007B;
    private const int WmNull = 0x0000;
    private const int WmLButtonUp = 0x0202;
    private const int WmLButtonDoubleClick = 0x0203;
    private const int WmRButtonUp = 0x0205;
    private const int NinSelect = 0x0400;
    private const int NinKeySelect = 0x0401;

    private const uint NimAdd = 0x00000000;
    private const uint NimModify = 0x00000001;
    private const uint NimDelete = 0x00000002;
    private const uint NimSetVersion = 0x00000004;
    private const uint NifMessage = 0x00000001;
    private const uint NifIcon = 0x00000002;
    private const uint NifTip = 0x00000004;
    private const uint NifInfo = 0x00000010;
    private const uint NifGuid = 0x00000020;
    private const uint NifShowTip = 0x00000080;
    private const uint NiifUser = 0x00000004;
    private const uint NiifLargeIcon = 0x00000020;
    private const uint NotifyIconVersion4 = 4;
    private const uint IconId = 1;

    private Icon? _icon;
    private Icon? _trayIcon;
    private Icon? _balloonIcon;
    private bool _visible;
    private bool _added;
    private bool _disposed;
    private bool _legacyIdentityCleaned;
    private string _text = string.Empty;
    private readonly int _taskbarCreatedMessage;

    public ShellTrayIcon()
    {
        _taskbarCreatedMessage = NativeMethods.RegisterWindowMessage("TaskbarCreated");
        CreateHandle(new CreateParams
        {
            Caption = "TreasureChest notification-area message window",
            Parent = IntPtr.Zero,
            ExStyle = 0x00000080, // WS_EX_TOOLWINDOW: hidden top-level window receives TaskbarCreated.
        });
    }

    public event EventHandler? DoubleClick;
    public event MouseEventHandler? MouseClick;

    public ContextMenuStrip? ContextMenuStrip { get; set; }

    public string Text
    {
        get => _text;
        set
        {
            _text = value ?? string.Empty;
            if (_added) ModifyIdentity();
        }
    }

    public Icon? Icon
    {
        get => _icon;
        set
        {
            if (ReferenceEquals(_icon, value)) return;
            _icon?.Dispose();
            _trayIcon?.Dispose();
            _balloonIcon?.Dispose();
            _icon = value is null ? null : (Icon)value.Clone();
            _trayIcon = value is null ? null : new Icon(value, SystemInformation.SmallIconSize);
            _balloonIcon = value is null ? null : new Icon(value, SystemInformation.IconSize);
            if (_added) ModifyIdentity();
        }
    }

    public bool Visible
    {
        get => _visible;
        set
        {
            if (_visible == value) return;
            _visible = value;
            if (_visible) Add();
            else Remove();
        }
    }

    internal bool UsesApplicationBalloonIcon => true;
    internal bool UsesGuidIdentity => true;
    internal int TaskbarCreatedMessage => _taskbarCreatedMessage;
    internal Size? TrayIconSize => _trayIcon?.Size;
    internal Size? BalloonIconSize => _balloonIcon?.Size;
    internal ShellNotifyResult LastShellResult { get; private set; } = ShellNotifyResult.NotAttempted;

    public ShellNotifyResult ShowBalloon(string title, string message)
    {
        if (!_added || _balloonIcon is null)
            return LastShellResult = ShellNotifyResult.NotReady;
        var data = CreateData(NifInfo | NifIcon);
        data.InfoTitle = Truncate(title, 63);
        data.Info = Truncate(message, 255);
        data.InfoFlags = NiifUser | NiifLargeIcon;
        data.BalloonIcon = _balloonIcon.Handle;
        return LastShellResult = InvokeShell(NimModify, ref data);
    }

    protected override void WndProc(ref Message message)
    {
        if (message.Msg == _taskbarCreatedMessage)
        {
            _added = false;
            if (_visible) Add();
            return;
        }
        if (message.Msg == CallbackMessage)
        {
            var code = unchecked((int)(long)message.LParam) & 0xffff;
            switch (code)
            {
                case WmLButtonDoubleClick:
                    DoubleClick?.Invoke(this, EventArgs.Empty);
                    break;
                case WmLButtonUp:
                case NinSelect:
                case NinKeySelect:
                    MouseClick?.Invoke(this, new MouseEventArgs(MouseButtons.Left, 1,
                        Cursor.Position.X, Cursor.Position.Y, 0));
                    break;
                case WmRButtonUp:
                case WmContextMenu:
                    ShowContextMenu();
                    break;
            }
            return;
        }
        base.WndProc(ref message);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        Remove();
        ContextMenuStrip?.Dispose();
        ContextMenuStrip = null;
        _icon?.Dispose();
        _icon = null;
        _trayIcon?.Dispose();
        _trayIcon = null;
        _balloonIcon?.Dispose();
        _balloonIcon = null;
        DestroyHandle();
        GC.SuppressFinalize(this);
    }

    private void Add()
    {
        if (_added || _trayIcon is null || Handle == IntPtr.Zero) return;
        CleanupLegacyIdentityOnce();
        var data = CreateData(NifMessage | NifIcon | NifTip | NifShowTip);
        var added = InvokeShell(NimAdd, ref data);
        LastShellResult = added;
        if (!added.Succeeded) return;
        _added = true;
        data.TimeoutOrVersion = NotifyIconVersion4;
        LastShellResult = InvokeShell(NimSetVersion, ref data);
    }

    private void Remove()
    {
        if (!_added || Handle == IntPtr.Zero) return;
        var data = CreateData(0);
        LastShellResult = InvokeShell(NimDelete, ref data);
        _added = false;
    }

    private void ModifyIdentity()
    {
        if (!_added || _trayIcon is null) return;
        var data = CreateData(NifIcon | NifTip | NifShowTip);
        LastShellResult = InvokeShell(NimModify, ref data);
    }

    private NotifyIconData CreateData(uint flags) => new()
    {
        Size = Marshal.SizeOf<NotifyIconData>(),
        Window = Handle,
        Id = IconId,
        Flags = flags | NifGuid,
        CallbackMessage = CallbackMessage,
        Icon = _trayIcon?.Handle ?? IntPtr.Zero,
        Tip = Truncate(_text, 127),
        Info = string.Empty,
        InfoTitle = string.Empty,
        ItemGuid = ProductIdentityGuid,
    };

    private void CleanupLegacyIdentityOnce()
    {
        if (_legacyIdentityCleaned || Handle == IntPtr.Zero) return;
        _legacyIdentityCleaned = true;
        var legacy = CreateData(0);
        legacy.Flags &= ~NifGuid;
        legacy.ItemGuid = Guid.Empty;
        // Absence of the legacy uID identity is expected on a clean install.
        _ = InvokeShell(NimDelete, ref legacy);
    }

    private static ShellNotifyResult InvokeShell(uint message, ref NotifyIconData data)
    {
        if (ShellNotifyIcon(message, ref data)) return ShellNotifyResult.Success;
        return new ShellNotifyResult(false, ShellNotifyFailure.ShellCallFailed, Marshal.GetLastWin32Error());
    }

    private void ShowContextMenu()
    {
        if (ContextMenuStrip is null) return;
        NativeMethods.SetForegroundWindow(Handle);
        ContextMenuStrip.Show(Cursor.Position);
        NativeMethods.PostMessage(Handle, WmNull, IntPtr.Zero, IntPtr.Zero);
    }

    private static string Truncate(string? value, int maximum) =>
        string.IsNullOrEmpty(value) || value.Length <= maximum ? value ?? string.Empty : value[..maximum];

    [DllImport("shell32.dll", EntryPoint = "Shell_NotifyIconW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShellNotifyIcon(uint message, ref NotifyIconData data);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NotifyIconData
    {
        public int Size;
        public IntPtr Window;
        public uint Id;
        public uint Flags;
        public uint CallbackMessage;
        public IntPtr Icon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Tip;
        public uint State;
        public uint StateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Info;
        public uint TimeoutOrVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string InfoTitle;
        public uint InfoFlags;
        public Guid ItemGuid;
        public IntPtr BalloonIcon;
    }
}

internal enum ShellNotifyFailure
{
    None,
    NotAttempted,
    NotReady,
    ShellCallFailed,
}

internal readonly record struct ShellNotifyResult(bool Succeeded, ShellNotifyFailure Failure, int NativeErrorCode)
{
    public static ShellNotifyResult Success => new(true, ShellNotifyFailure.None, 0);
    public static ShellNotifyResult NotAttempted => new(false, ShellNotifyFailure.NotAttempted, 0);
    public static ShellNotifyResult NotReady => new(false, ShellNotifyFailure.NotReady, 0);
}
