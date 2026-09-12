using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Windows.Forms;

namespace TreasureChest.UI;

/// <summary>
/// Shared optical-radius family.  Values are logical pixels at 96 DPI; the
/// levels deliberately form a hierarchy instead of forcing every surface to
/// use the same radius.
/// </summary>
internal static class CornerRadiusTokens
{
    internal const int Window = 32;
    internal const int PrimarySurface = 24;
    internal const int SecondarySurface = 18;
    internal const int GridWell = 16;
    internal const int NavigationItem = 14;
    internal const int ActionButton = 12;
    internal const int Input = 11;

    internal static int Pill(int logicalHeight) => Math.Max(1, logicalHeight / 2);
}

/// <summary>
/// One rendering path for rounded theme chrome.  In particular this helper
/// never installs a binary Win32 Region: the caller clears to a known opaque
/// surface and lets GDI+ preserve the antialiased edge coverage.
/// </summary>
internal static class ThemePaint
{
    internal static void Configure(Graphics graphics)
    {
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        graphics.CompositingMode = CompositingMode.SourceOver;
        graphics.CompositingQuality = CompositingQuality.HighQuality;
        graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
    }

    // All owner-painted text uses the same native rasterizer and metrics.
    // Its alpha path handles fading navigation without returning to DrawString.
    internal static void DrawText(Graphics graphics, string? text, Font font,
        Rectangle bounds, Color color, TextFormatFlags flags) =>
        NativeButtonText.Draw(graphics, text, font, bounds, color, flags);

    internal static Size MeasureSingleLineText(Graphics graphics, string? text, Font font) =>
        NativeButtonText.Measure(graphics, text, font);

    internal static Color ResolveOpaqueBackground(Control control, Color fallback)
    {
        for (Control? current = control.Parent; current is not null; current = current.Parent)
        {
            var color = current.BackColor;
            if (!color.IsEmpty && color.A == byte.MaxValue) return color;
        }

        return !fallback.IsEmpty && fallback.A == byte.MaxValue
            ? fallback
            : UiTheme.Palette.Background;
    }

    internal static GraphicsPath RoundedPath(Rectangle bounds, int radius, float inset = 0.5F) =>
        RoundedPath(new RectangleF(bounds.X, bounds.Y, bounds.Width, bounds.Height), radius, inset);

    internal static GraphicsPath RoundedPath(RectangleF bounds, float radius, float inset = 0.5F)
        => RoundedPathExact(AlignedBounds(bounds, inset), radius);

    internal static RectangleF AlignedBounds(Rectangle bounds, float inset = 0.5F) =>
        AlignedBounds(new RectangleF(bounds.X, bounds.Y, bounds.Width, bounds.Height), inset);

    internal static RectangleF AlignedBounds(RectangleF bounds, float inset = 0.5F) => new(
        bounds.Left + inset,
        bounds.Top + inset,
        Math.Max(0F, bounds.Width - inset * 2F - 1F),
        Math.Max(0F, bounds.Height - inset * 2F - 1F));

    internal static GraphicsPath RoundedPathExact(RectangleF rectangle, float radius)
    {
        var path = new GraphicsPath();
        // GDI+ fills the right/bottom path boundary differently from the
        // left/top boundary.  Reserving the final device pixel keeps all four
        // antialiased contours optically mirrored instead of letting the fill
        // touch the last column and row.
        if (rectangle.Width <= 0F || rectangle.Height <= 0F) return path;

        var clampedRadius = Math.Min(Math.Max(0F, radius), Math.Min(rectangle.Width, rectangle.Height) / 2F);
        if (clampedRadius <= 1F)
        {
            path.AddRectangle(rectangle);
            path.CloseFigure();
            return path;
        }

        var diameter = clampedRadius * 2F;
        path.AddArc(rectangle.Left, rectangle.Top, diameter, diameter, 180F, 90F);
        path.AddArc(rectangle.Right - diameter, rectangle.Top, diameter, diameter, 270F, 90F);
        path.AddArc(rectangle.Right - diameter, rectangle.Bottom - diameter, diameter, diameter, 0F, 90F);
        path.AddArc(rectangle.Left, rectangle.Bottom - diameter, diameter, diameter, 90F, 90F);
        path.CloseFigure();
        return path;
    }

    internal static void DrawFocusRing(Graphics graphics, Rectangle bounds, int radius, int dpi, Color color)
    {
        var inset = Math.Max(2, DpiLayout.Scale(3, dpi));
        var focusBounds = Rectangle.Inflate(bounds, -inset, -inset);
        if (focusBounds.Width <= 2 || focusBounds.Height <= 2) return;
        Configure(graphics);
        using var path = RoundedPath(focusBounds, Math.Max(1, radius - inset));
        // Keep the geometric stroke opaque and let GDI+ anti-alias only the
        // coverage pixels.  Pre-blending the whole pen made the focus cue too
        // faint on the porcelain surface and prevented any fully resolved
        // Accent pixels from surviving at 96 DPI.
        using var pen = new Pen(color, Math.Max(1F, dpi / 96F))
        {
            Alignment = PenAlignment.Center,
            StartCap = LineCap.Round,
            EndCap = LineCap.Round,
            LineJoin = LineJoin.Round,
        };
        graphics.DrawPath(pen, path);
    }
}
