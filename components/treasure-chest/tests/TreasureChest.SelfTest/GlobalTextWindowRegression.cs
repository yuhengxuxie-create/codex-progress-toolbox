using System.Drawing;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using TreasureChest.UI;

internal static class GlobalTextWindowRegression
{
    internal static void WindowActions(Action<bool, string> assert)
    {
        using var window = new Form { ShowInTaskbar = false, Size = new Size(700, 500) };
        using var caption = new TreasureChest.UI.CaptionButton(CaptionButtonKind.Maximize, "测试") { Size = new Size(50, 50) };
        var clicks = 0;
        caption.Click += (_, _) => { clicks++; window.WindowState = WindowChromePresentation.ToggleMaximize(window.WindowState); };
        var handle = caption.Handle; // Hidden control; no Form.Show or user input.
        var point = new IntPtr(10 | 10 << 16);
        SendMessage(handle, 0x201, new IntPtr(1), point);
        SendMessage(handle, 0x202, IntPtr.Zero, point);
        assert(clicks == 1 && window.WindowState == FormWindowState.Maximized, "一次鼠标消息被派发多次Click，最大化立即还原");
        var layout = new DpiLayoutStore();
        layout.CaptureBaseTree(window);
        var size = window.Size;
        layout.ApplyTree(window, 144);
        assert(window.WindowState == FormWindowState.Maximized && window.Size == size, "DPI布局改写最大化窗口普通尺寸");
        SendMessage(handle, 0x201, new IntPtr(1), point);
        SendMessage(handle, 0x202, IntPtr.Zero, point);
        assert(clicks == 2 && window.WindowState == FormWindowState.Normal, "第二次单击没有明确还原");
        WindowChromePresentation.MinimizeToTaskbar(window);
        assert(window.WindowState == FormWindowState.Minimized && window.ShowInTaskbar, "最小化没有保留任务栏");
        size = window.Size;
        layout.ApplyTree(window, 192);
        assert(window.WindowState == FormWindowState.Minimized && window.Size == size, "DPI布局干扰最小化状态");
        SendMessage(handle, 0x201, new IntPtr(1), point);
        SendMessage(handle, 0x202, IntPtr.Zero, new IntPtr(200 | 200 << 16));
        assert(clicks == 2, "移出按钮后松开仍触发Click");
        SendMessage(handle, 0x201, new IntPtr(1), point);
        caption.Capture = false;
        SendMessage(handle, 0x202, IntPtr.Zero, point);
        assert(clicks == 2, "丢失捕获后仍触发Click");
        SendMessage(handle, 0x201, new IntPtr(1), point);
        SendMessage(handle, 0x202, IntPtr.Zero, point);
        assert(clicks == 3, "捕获丢失后下一次正常点击失效");
    }

    internal static void TextCoverage(Action<bool, string> assert)
    {
        using var font = UiTheme.CreateFont();
        foreach (var dpi in new[] { 96, 120, 144, 192 })
        foreach (var background in new[] { Color.White, Color.FromArgb(24, 28, 40) })
        {
            using var image = new Bitmap(500, 80);
            image.SetResolution(dpi, dpi);
            using var g = Graphics.FromImage(image);
            g.Clear(background);
            var area = new Rectangle(10, 10, 460, 60);
            ThemePaint.DrawText(g, "自动启动时最小化到托盘", font, area, Color.FromArgb(0, 120, 90, 200), TextFormatFlags.SingleLine);
            assert(image.GetPixel(20, 20).ToArgb() == background.ToArgb(), "透明度0仍绘制导航文字");
            ThemePaint.DrawText(g, "自动启动时最小化到托盘", font, area, Color.FromArgb(100, 120, 90, 200), TextFormatFlags.SingleLine);
            var changed = false;
            for (var y = 0; y < image.Height; y++)
            for (var x = 0; x < image.Width; x++)
            {
                var pixel = image.GetPixel(x, y);
                if (pixel.ToArgb() == background.ToArgb()) continue;
                changed = true;
                assert(area.Contains(x, y), "透明文字超出裁切范围");
                assert(Math.Abs(pixel.R - background.R) <= 102 && Math.Abs(pixel.G - background.G) <= 102 && Math.Abs(pixel.B - background.B) <= 102,
                    "透明文字被GDI当成不透明绘制");
            }
            assert(changed, "导航淡出文字没有原生字形覆盖");
        }
        // All current owner-painted consumers must route through the shared
        // dispatcher; no new DrawString bypass may silently reintroduce GDI+.
        var sourceRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", ".."));
        var ui = Path.Combine(sourceRoot, "src", "TreasureChest.App", "UI");
        assert(Directory.Exists(ui), "无法定位全局文字源码清单");
        foreach (var path in Directory.GetFiles(ui, "*.cs"))
        {
            var text = File.ReadAllText(path);
            assert(!text.Contains("graphics.DrawString(") && !text.Contains(".UseCompatibleTextRendering = true"), "发现GDI+文字旁路：" + Path.GetFileName(path));
        }
    }

    internal static void EditorSelection(Action<bool, string> assert)
    {
        UiTheme.Initialize("day");
        using var font = UiTheme.CreateFont();
        using var editor = new ThemedEmbeddedTextBox { Font = font, Text = "中文 Codex 编辑选区与光标 long text 123456789" };
        foreach (var dpi in new[] { 96, 120, 144, 192 })
        {
            using var image = new Bitmap(280, 70);
            image.SetResolution(dpi, dpi);
            using var g = Graphics.FromImage(image);
            var bounds = new Rectangle(20, 10, 200, 45);
            editor.SelectionStart = editor.TextLength;
            editor.SelectionLength = 0;
            g.Clear(Color.Magenta);
            editor.DrawVisual(g, bounds, focusedOverride: true);
            // A long input scrolls to its actual caret, rather than ellipsizing
            // under a caret pinned to a disconnected text fragment.
            var caret = image.GetPixel(bounds.Right - 2, bounds.Top + bounds.Height / 2);
            assert(caret.ToArgb() == UiTheme.Text.ToArgb(), "长文本光标未跟随水平滚动");
            editor.SelectionStart = 3;
            editor.SelectionLength = 7;
            g.Clear(Color.Magenta);
            editor.DrawVisual(g, bounds, focusedOverride: true);
            var selectionPixels = 0;
            for (var y = 0; y < image.Height; y++)
            for (var x = 0; x < image.Width; x++)
            {
                var pixel = image.GetPixel(x, y);
                if (!bounds.Contains(x, y)) assert(pixel.ToArgb() == Color.Magenta.ToArgb(), "编辑文字/选区越过输入裁切");
                else if (pixel.ToArgb() == UiTheme.AccentSoftStrong.ToArgb()) selectionPixels++;
            }
            assert(selectionPixels > 0, "混合中英文选区没有可见背景");
        }
    }

    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
}
