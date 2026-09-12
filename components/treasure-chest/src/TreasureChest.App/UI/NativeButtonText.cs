using System.Drawing.Text;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace TreasureChest.UI;

// Shared native text rasterizer. The historical type name is kept for source
// compatibility; ThemePaint routes every owner-painted text consumer here.
internal static class NativeButtonText
{
    private const TextFormatFlags Preserve = TextFormatFlags.PreserveGraphicsClipping |
        TextFormatFlags.PreserveGraphicsTranslateTransform | TextFormatFlags.NoPrefix;

    internal static Font PixelFont(Font font, float dpi) => new(font.FontFamily,
        font.Unit == GraphicsUnit.Pixel ? font.Size : font.SizeInPoints * dpi / 72F,
        font.Style, GraphicsUnit.Pixel, font.GdiCharSet);

    internal static Size Measure(Graphics graphics, string? text, Font font, Size? proposed = null,
        TextFormatFlags flags = TextFormatFlags.NoPadding | TextFormatFlags.SingleLine)
    {
        if (string.IsNullOrEmpty(text)) return Size.Empty;
        var state = graphics.Save();
        try
        {
            graphics.TextRenderingHint = TextRenderingHint.SystemDefault;
            using var pixels = PixelFont(font, graphics.DpiY);
            return TextRenderer.MeasureText(graphics, text, pixels, proposed ?? new Size(int.MaxValue, int.MaxValue), flags | Preserve);
        }
        finally { graphics.Restore(state); }
    }

    internal static Size MeasureAtDpi(string? text, Font font, int dpi, Size? proposed = null,
        TextFormatFlags flags = TextFormatFlags.NoPadding | TextFormatFlags.SingleLine)
    {
        using var bitmap = new Bitmap(1, 1);
        bitmap.SetResolution(dpi, dpi);
        using var graphics = Graphics.FromImage(bitmap);
        return Measure(graphics, text, font, proposed, flags);
    }

    internal static void Draw(Graphics graphics, string? text, Font font, Rectangle bounds, Color color, TextFormatFlags flags)
    {
        if (string.IsNullOrEmpty(text) || bounds.Width <= 0 || bounds.Height <= 0 || color.A == 0) return;
        if (color.A < 255)
        {
            DrawFadingText(graphics, text, font, bounds, color, flags);
            return;
        }
        var state = graphics.Save();
        try
        {
            graphics.TextRenderingHint = TextRenderingHint.SystemDefault;
            using var pixels = PixelFont(font, graphics.DpiY);
            TextRenderer.DrawText(graphics, text, pixels, bounds, color, flags | Preserve);
        }
        finally { graphics.Restore(state); }
    }

    private static void DrawFadingText(Graphics graphics, string text, Font font, Rectangle bounds, Color color, TextFormatFlags flags)
    {
        // GDI ignores Color.A. Build native grayscale glyph coverage on an
        // opaque mask, then apply the requested opacity exactly once.
        using var mask = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppRgb);
        mask.SetResolution(graphics.DpiX, graphics.DpiY);
        using (var maskGraphics = Graphics.FromImage(mask))
        using (var pixels = PixelFont(font, graphics.DpiY))
        {
            maskGraphics.Clear(Color.Black);
            maskGraphics.TextRenderingHint = TextRenderingHint.AntiAlias;
            TextRenderer.DrawText(maskGraphics, text, pixels, new Rectangle(Point.Empty, bounds.Size), Color.White,
                flags | TextFormatFlags.NoPrefix);
        }
        using var layer = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppArgb);
        var area = new Rectangle(Point.Empty, bounds.Size);
        var source = mask.LockBits(area, ImageLockMode.ReadOnly, PixelFormat.Format32bppRgb);
        var destination = layer.LockBits(area, ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
        try
        {
            var sourceBytes = new byte[source.Stride * bounds.Height];
            var result = new byte[destination.Stride * bounds.Height];
            Marshal.Copy(source.Scan0, sourceBytes, 0, sourceBytes.Length);
            for (var y = 0; y < bounds.Height; y++)
            for (var x = 0; x < bounds.Width; x++)
            {
                var index = y * destination.Stride + x * 4;
                result[index] = color.B; result[index + 1] = color.G; result[index + 2] = color.R;
                result[index + 3] = (byte)((sourceBytes[y * source.Stride + x * 4] * color.A + 127) / 255);
            }
            Marshal.Copy(result, 0, destination.Scan0, result.Length);
        }
        finally { mask.UnlockBits(source); layer.UnlockBits(destination); }
        var state = graphics.Save();
        try
        {
            graphics.SetClip(bounds, System.Drawing.Drawing2D.CombineMode.Intersect);
            graphics.PixelOffsetMode = System.Drawing.Drawing2D.PixelOffsetMode.None;
            graphics.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;
            graphics.DrawImageUnscaled(layer, bounds.Location);
        }
        finally { graphics.Restore(state); }
    }
}
