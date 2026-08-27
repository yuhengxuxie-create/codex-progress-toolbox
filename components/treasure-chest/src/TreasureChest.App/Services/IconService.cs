using System.Diagnostics;
using System.Reflection;

namespace TreasureChest.Services;

internal static class IconService
{
    public static Icon LoadAppIcon()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("TreasureChest.AppIcon")
            ?? throw new InvalidOperationException("应用图标资源缺失。");
        using var source = new Icon(stream);
        return (Icon)source.Clone();
    }

    public static Image LoadToolImage(string target, string customIcon)
    {
        try
        {
            var iconTarget = !string.IsNullOrWhiteSpace(customIcon) ? customIcon : target;
            if (File.Exists(iconTarget))
            {
                if (Path.GetExtension(iconTarget).Equals(".png", StringComparison.OrdinalIgnoreCase))
                {
                    using var source = Image.FromFile(iconTarget);
                    return new Bitmap(source, new Size(42, 42));
                }
                using var icon = Icon.ExtractAssociatedIcon(iconTarget);
                if (icon is not null) return icon.ToBitmap();
            }
        }
        catch { }
        using var fallback = LoadAppIcon();
        return fallback.ToBitmap();
    }
}
