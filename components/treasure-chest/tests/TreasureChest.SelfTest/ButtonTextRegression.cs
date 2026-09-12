using System.Drawing;
using System.Reflection;
using System.Windows.Forms;
using TreasureChest;
using TreasureChest.Core.Models;
using TreasureChest.UI;

internal static class ButtonTextRegression
{
    internal static void Run(Action<bool, string> assert)
    {
        const BindingFlags flags = BindingFlags.NonPublic | BindingFlags.Instance;
        var paint = typeof(RoundedButton).GetMethod("OnPaint", flags)!;
        var hover = typeof(RoundedButton).GetField("_hovered", flags)!;
        var pressed = typeof(RoundedButton).GetField("_pressed", flags)!;
        UiTheme.Initialize("day");
        foreach (var dpi in new[] { 96, 120, 144, 192 })
        foreach (var label in new[] { "刷新", "在 Codex 打开", "监测设置", "管理监测", "停止", "立即刷新", "预警记录(9+)", "编辑", "删除" })
        {
            using var button = (RoundedButton)UiTheme.Button(label, glyph: ButtonGlyph.Refresh);
            button.VisualDpiOverride = dpi;
            button.Padding = DpiLayout.Scale(new Padding(14, 0, 14, 0), dpi);
            button.Size = button.GetPreferredSize(Size.Empty);
            assert(button.Font.Unit == GraphicsUnit.Point && Math.Abs(button.Font.SizeInPoints - 12F) < 0.01,
                "按钮字体单位/字号被意外修改");
            using var actual = new Bitmap(button.Width, button.Height);
            actual.SetResolution(dpi, dpi);
            using var graphics = Graphics.FromImage(actual);
            var measured = NativeButtonText.Measure(graphics, label, button.Font);
            assert(measured.Width + button.Padding.Horizontal + DpiLayout.Scale(24, dpi) <= button.Width &&
                measured.Height <= button.Height, $"{dpi}/{label} 图文空间不足");
            foreach (var state in new[] { 0, 1, 2, 3, 0 })
            {
                button.Enabled = state != 3;
                hover.SetValue(button, state == 1);
                pressed.SetValue(button, state == 2);
                paint.Invoke(button, [new PaintEventArgs(graphics, button.ClientRectangle)]);
                using var clean = new Bitmap(button.Width, button.Height);
                clean.SetResolution(dpi, dpi);
                using var fresh = Graphics.FromImage(clean);
                paint.Invoke(button, [new PaintEventArgs(fresh, button.ClientRectangle)]);
                assert(Equal(actual, clean, button.ClientRectangle), $"{dpi}/{label}/{state} 状态重绘残留旧像素");
                var clip = new Rectangle(button.Width / 3, 0, Math.Max(1, button.Width / 3), button.Height);
                var saved = graphics.Save();
                graphics.SetClip(clip);
                paint.Invoke(button, [new PaintEventArgs(graphics, clip)]);
                graphics.Restore(saved);
                assert(Equal(actual, clean, clip), $"{dpi}/{label}/{state} 局部剪裁改变字形原点");
            }
        }
        var legacy = new SessionDefinition { Name = "Codex管理（飞书）", WorkingDirectory = AppPaths.ProgressNotificationRoot };
        assert(MainForm.SessionDisplayName(legacy) == "指令使用（飞书）" && legacy.Name == "Codex管理（飞书）",
            "默认服务显示映射修改了持久名称");
        legacy.Name = "我的机器人";
        assert(MainForm.SessionDisplayName(legacy) == "我的机器人", "用户自定义名称被覆盖");
        legacy.Name = "Codex管理（飞书）";
        legacy.WorkingDirectory = Path.GetTempPath();
        assert(MainForm.SessionDisplayName(legacy) == legacy.Name, "非飞书服务同名被误改");
    }

    private static bool Equal(Bitmap a, Bitmap b, Rectangle area)
    {
        for (var y = area.Top; y < area.Bottom; y++)
        for (var x = area.Left; x < area.Right; x++)
            if (a.GetPixel(x, y) != b.GetPixel(x, y)) return false;
        return true;
    }
}
