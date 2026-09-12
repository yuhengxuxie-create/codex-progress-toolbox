using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;

namespace TreasureChest;

internal static class NativeMethods
{
    private enum PreferredAppMode
    {
        Default = 0,
        AllowDark = 1,
        ForceDark = 2,
        ForceLight = 3,
    }

    public const int HwndBroadcast = 0xffff;
    public const int WmNcHitTest = 0x0084;
    public const int WmPaint = 0x000F;
    public const int WmEraseBackground = 0x0014;
    public const int WmNcPaint = 0x0085;
    public const int WmCtlColorEdit = 0x0133;
    public const int WmCtlColorListBox = 0x0134;
    public const int WmCtlColorStatic = 0x0138;
    public const int WmReflect = 0x2000;
    public const int WmPrint = 0x0317;
    public const int WmPrintClient = 0x0318;
    public const int WmSetRedraw = 0x000B;
    public const int WmNcLButtonDown = 0x00A1;
    public const int WmDisplayChange = 0x007E;
    public const int WmSizing = 0x0214;
    public const int WmDpiChanged = 0x02E0;
    public const int HtClient = 0x0001;
    public const int HtTransparent = -1;
    public const int HtCaption = 0x0002;
    public const int HtLeft = 10;
    public const int HtRight = 11;
    public const int HtTop = 12;
    public const int HtTopLeft = 13;
    public const int HtTopRight = 14;
    public const int HtBottom = 15;
    public const int HtBottomLeft = 16;
    public const int HtBottomRight = 17;
    public const int DwmWindowCornerPreference = 33;
    public const int DwmBorderColor = 34;
    public const int DwmColorNone = unchecked((int)0xfffffffe);
    public const int DwmUseImmersiveDarkMode = 20;
    public const uint CreateWaitableTimerHighResolution = 0x00000002;
    public const uint TimerAllAccess = 0x001f0003;
    public const uint WaitObject0 = 0x00000000;
    public const uint WaitTimeout = 0x00000102;
    public const uint RdwInvalidate = 0x0001;
    public const uint RdwErase = 0x0004;
    public const uint RdwAllChildren = 0x0080;
    public const uint RdwUpdateNow = 0x0100;
    public const uint RdwFrame = 0x0400;
    public const uint PrintWindowClientOnly = 0x00000001;
    public const uint PrintWindowRenderFullContent = 0x00000002;
    public const int SourceCopyRasterOperation = 0x00CC0020;
    public const int WsBorder = 0x00800000;
    public const int WsExClientEdge = 0x00000200;
    public const int EmSetBackgroundColor = 0x0443;
    public const int CbSetBackgroundColor = 0x0166;

    [DllImport("user32.dll")]
    public static extern bool ReleaseCapture();
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int RegisterWindowMessage(string message);
    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")]
    public static extern bool RedrawWindow(IntPtr hWnd, IntPtr updateRectangle, IntPtr updateRegion, uint flags);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out NativeRect rectangle);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int GetClassName(IntPtr hWnd, StringBuilder className, int maxCount);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern int SetCurrentProcessExplicitAppUserModelID(string appUserModelId);
    [DllImport("gdi32.dll")]
    public static extern IntPtr CreateSolidBrush(uint colorRef);
    [DllImport("gdi32.dll")]
    public static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")]
    public static extern IntPtr SelectObject(IntPtr hdc, IntPtr drawingObject);
    [DllImport("gdi32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool DeleteDC(IntPtr hdc);
    [DllImport("gdi32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool BitBlt(
        IntPtr destination, int x, int y, int width, int height,
        IntPtr source, int sourceX, int sourceY, int rasterOperation);
    [DllImport("msimg32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool AlphaBlend(
        IntPtr destination, int x, int y, int width, int height,
        IntPtr source, int sourceX, int sourceY, int sourceWidth, int sourceHeight,
        BlendFunction blendFunction);
    [DllImport("gdi32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool DeleteObject(IntPtr objectHandle);
    [DllImport("gdi32.dll")]
    public static extern uint SetBkColor(IntPtr hdc, uint colorRef);
    [DllImport("gdi32.dll")]
    public static extern uint SetTextColor(IntPtr hdc, uint colorRef);
    [DllImport("user32.dll")]
    public static extern IntPtr GetWindowDC(IntPtr window);
    [DllImport("user32.dll")]
    public static extern int ReleaseDC(IntPtr window, IntPtr dc);
    [DllImport("user32.dll")]
    public static extern uint GetDpiForWindow(IntPtr hWnd);
    [DllImport("dwmapi.dll")]
    public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
    [DllImport("dwmapi.dll")]
    public static extern int DwmFlush();
    [DllImport("dwmapi.dll")]
    public static extern int DwmIsCompositionEnabled([MarshalAs(UnmanagedType.Bool)] out bool enabled);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateWaitableTimerEx(
        IntPtr timerAttributes,
        string? timerName,
        uint flags,
        uint desiredAccess);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetWaitableTimer(
        IntPtr timer,
        ref long dueTime,
        int period,
        IntPtr completionRoutine,
        IntPtr argument,
        [MarshalAs(UnmanagedType.Bool)] bool resume);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint WaitForMultipleObjects(
        uint count,
        IntPtr[] handles,
        [MarshalAs(UnmanagedType.Bool)] bool waitAll,
        uint milliseconds);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CancelWaitableTimer(IntPtr timer);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")]
    public static extern bool SetProcessWorkingSetSize(IntPtr process, IntPtr minimumWorkingSetSize, IntPtr maximumWorkingSetSize);
    [DllImport("uxtheme.dll", CharSet = CharSet.Unicode)]
    public static extern int SetWindowTheme(IntPtr hwnd, string? subAppName, string? subIdList);
    [DllImport("uxtheme.dll", EntryPoint = "#135")]
    private static extern PreferredAppMode SetPreferredAppMode(PreferredAppMode appMode);
    [DllImport("uxtheme.dll", EntryPoint = "#136")]
    private static extern void FlushMenuThemes();

    public static bool TrySetPreferredDarkMode(bool dark)
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 18362)) return false;
        try
        {
            SetPreferredAppMode(dark ? PreferredAppMode.AllowDark : PreferredAppMode.ForceLight);
            FlushMenuThemes();
            return true;
        }
        catch (DllNotFoundException) { return false; }
        catch (EntryPointNotFoundException) { return false; }
        catch (BadImageFormatException) { return false; }
    }

    public static bool TrySetWindowDarkMode(IntPtr handle, bool dark)
    {
        if (handle == IntPtr.Zero || !OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763)) return false;
        var enabled = dark ? 1 : 0;
        // 20 is the documented Windows 10 20H1+/Windows 11 attribute.  Builds 1809/1903
        // used 19; keep the bounded fallback so standard dialog captions follow the
        // application theme without replacing their native window behaviour.
        if (DwmSetWindowAttribute(handle, DwmUseImmersiveDarkMode, ref enabled, sizeof(int)) == 0) return true;
        return DwmSetWindowAttribute(handle, 19, ref enabled, sizeof(int)) == 0;
    }

    public static bool TrySetWindowCornerPreference(IntPtr handle, int preference)
    {
        if (handle == IntPtr.Zero || !OperatingSystem.IsWindowsVersionAtLeast(10, 0, 22000)) return false;
        try
        {
            var rounded = DwmSetWindowAttribute(handle, DwmWindowCornerPreference, ref preference, sizeof(int)) == 0;
            var noBorder = DwmColorNone;
            DwmSetWindowAttribute(handle, DwmBorderColor, ref noBorder, sizeof(int));
            return rounded;
        }
        catch (DllNotFoundException) { return false; }
        catch (EntryPointNotFoundException) { return false; }
        catch (BadImageFormatException) { return false; }
    }

    public static Point PointFromLParam(IntPtr value)
    {
        var packed = unchecked((uint)value.ToInt64());
        return new Point(unchecked((short)(packed & 0xffff)), unchecked((short)((packed >> 16) & 0xffff)));
    }

    public static IntPtr LParamFromScreenPoint(Point point)
    {
        var packed = unchecked((uint)(ushort)point.X | ((uint)(ushort)point.Y << 16));
        return (IntPtr)unchecked((int)packed);
    }

    public static Rectangle WindowRectangle(IntPtr handle) =>
        GetWindowRect(handle, out var rectangle)
            ? Rectangle.FromLTRB(rectangle.Left, rectangle.Top, rectangle.Right, rectangle.Bottom)
            : Rectangle.Empty;

    public static string WindowClassName(IntPtr handle)
    {
        if (handle == IntPtr.Zero) return string.Empty;
        var buffer = new StringBuilder(256);
        var length = GetClassName(handle, buffer, buffer.Capacity);
        return length > 0 ? buffer.ToString(0, length) : string.Empty;
    }

    public static IntPtr ColorRef(Color color) =>
        new(color.R | (color.G << 8) | (color.B << 16));

    public static uint ColorRefValue(Color color) =>
        unchecked((uint)(color.R | (color.G << 8) | (color.B << 16)));

    [StructLayout(LayoutKind.Sequential)]
    public struct NativeRect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    public struct BlendFunction
    {
        public byte BlendOperation;
        public byte BlendFlags;
        public byte SourceConstantAlpha;
        public byte AlphaFormat;
    }
}
