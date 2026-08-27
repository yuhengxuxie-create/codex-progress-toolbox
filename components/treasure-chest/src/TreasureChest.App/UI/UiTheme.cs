namespace TreasureChest.UI;

internal static class UiTheme
{
    public static readonly Color Background = Color.FromArgb(246, 248, 252);
    public static readonly Color Surface = Color.White;
    public static readonly Color Navigation = Color.FromArgb(31, 42, 68);
    public static readonly Color Accent = Color.FromArgb(76, 103, 210);
    public static readonly Color Text = Color.FromArgb(34, 42, 58);
    public static readonly Color Muted = Color.FromArgb(112, 122, 142);
    public static readonly Color Running = Color.FromArgb(42, 157, 89);
    public static readonly Color Stopped = Color.FromArgb(211, 66, 74);
    public static readonly Color Warning = Color.FromArgb(225, 161, 45);

    public static Button Button(string text, bool primary = false)
    {
        var button = new Button
        {
            Text = text,
            AutoSize = true,
            Height = 34,
            Padding = new Padding(12, 0, 12, 0),
            FlatStyle = FlatStyle.Flat,
            BackColor = primary ? Accent : Surface,
            ForeColor = primary ? Color.White : Text,
            Cursor = Cursors.Hand,
            Font = new Font("Microsoft YaHei UI", 9F),
        };
        button.FlatAppearance.BorderColor = primary ? Accent : Color.FromArgb(215, 220, 232);
        return button;
    }

    public static Label Heading(string text, float size = 18F) => new()
    {
        Text = text,
        AutoSize = true,
        Font = new Font("Microsoft YaHei UI", size, FontStyle.Bold),
        ForeColor = Text,
    };
}
