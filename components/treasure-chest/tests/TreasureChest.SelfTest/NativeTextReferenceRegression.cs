using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using TreasureChest.UI;

internal static class NativeTextReferenceRegression
{
    internal static void Run(Action<bool, string> assert)
    {
        string[] labels = ["刷新", "在 Codex 打开", "监测设置", "管理监测", "停止", "预警记录",
            "上方管理飞书机器人正在监测的 Codex 项目与会话；下方管理后台服务。"];
        using var font = UiTheme.CreateFont();
        foreach (var dpi in new[] { 96, 120, 144, 192 })
        foreach (var dark in new[] { false, true })
        foreach (var text in labels)
        {
            var background = dark ? Color.FromArgb(24, 28, 40) : Color.FromArgb(250, 250, 252);
            var foreground = dark ? Color.FromArgb(238, 238, 244) : Color.FromArgb(32, 35, 48);
            using var actual = new Bitmap(1500, 80, PixelFormat.Format32bppRgb);
            using var reference = new Bitmap(1500, 80, PixelFormat.Format32bppRgb);
            using var previous = new Bitmap(1500, 80, PixelFormat.Format32bppRgb);
            actual.SetResolution(dpi, dpi); reference.SetResolution(dpi, dpi); previous.SetResolution(dpi, dpi);
            var bounds = new Rectangle(13, 7, 1460, 64);
            using var g = Graphics.FromImage(actual);
            using var r = Graphics.FromImage(reference);
            using var p = Graphics.FromImage(previous);
            g.Clear(background); r.Clear(background); p.Clear(background);
            NativeButtonText.Draw(g, text, font, bounds, foreground, TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
            var measured = NativeButtonText.Measure(g, text, font);
            var expected = DrawWin32(r, text, font, dpi, bounds, foreground);
            assert(measured == expected, $"{dpi}/{text} 原生量测不符合独立DrawTextW参考：{measured}/{expected}");
            assert(EqualRgb(actual, reference), $"{dpi}/{dark}/{text} 字形不符合独立DrawTextW参考");
            ThemePaint.DrawText(p, text, font, bounds, foreground, TextFormatFlags.NoPadding | TextFormatFlags.SingleLine);
            assert(EqualRgb(reference, previous), $"{dpi}/{dark}/{text} 全局ThemePaint未走独立Win32一致路径");
        }
        using var label = new Label { UseCompatibleTextRendering = true };
        typeof(UiTheme).GetMethod("ApplyInputTheme", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)!
            .Invoke(null, [label]);
        assert(!label.UseCompatibleTextRendering, "普通Label仍被强制兼容GDI+绘制");
    }

    private static Size DrawWin32(Graphics graphics, string text, Font font, int dpi, Rectangle bounds, Color color)
    {
        var hdc = graphics.GetHdc();
        var hfont = CreateFontW(-(int)Math.Round(font.SizeInPoints * dpi / 72F), 0, 0, 0, 400, 0, 0, 0,
            font.GdiCharSet, 4, 0, 0, 0, font.Name);
        if (hfont == IntPtr.Zero) throw new InvalidOperationException("CreateFontW failed");
        var old = SelectObject(hdc, hfont);
        try
        {
            SetBkMode(hdc, 1);
            SetTextColor(hdc, (uint)(color.R | color.G << 8 | color.B << 16));
            var rect = new Rect(bounds.Left, bounds.Top, bounds.Right, bounds.Bottom);
            const uint flags = 0x20 | 0x800; // DT_SINGLELINE | DT_NOPREFIX, zero margins.
            DrawTextW(hdc, text, text.Length, ref rect, flags);
            rect = new Rect(0, 0, int.MaxValue, int.MaxValue);
            DrawTextW(hdc, text, text.Length, ref rect, flags | 0x400); // DT_CALCRECT.
            return new Size(rect.Right - rect.Left, rect.Bottom - rect.Top);
        }
        finally { SelectObject(hdc, old); DeleteObject(hfont); graphics.ReleaseHdc(hdc); }
    }

    private static bool EqualRgb(Bitmap a, Bitmap b)
    {
        for (var y = 0; y < a.Height; y++)
        for (var x = 0; x < a.Width; x++)
            if ((a.GetPixel(x, y).ToArgb() & 0xffffff) != (b.GetPixel(x, y).ToArgb() & 0xffffff)) return false;
        return true;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Rect(int left, int top, int right, int bottom)
    { public int Left = left, Top = top, Right = right, Bottom = bottom; }
    [DllImport("gdi32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr CreateFontW(int height, int width, int escapement, int orientation, int weight, uint italic, uint underline, uint strikeOut, uint charSet, uint outputPrecision, uint clipPrecision, uint quality, uint pitch, string face);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
    [DllImport("gdi32.dll")] private static extern int SetBkMode(IntPtr dc, int mode);
    [DllImport("gdi32.dll")] private static extern uint SetTextColor(IntPtr dc, uint color);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int DrawTextW(IntPtr dc, string text, int count, ref Rect rect, uint flags);
}
