using System.Drawing.Drawing2D;

namespace TreasureChest.UI;

internal enum ThemeGlyph
{
    None,
    Refresh,
    OpenExternal,
    Settings,
    Monitor,
    Play,
    Stop,
    Add,
    Edit,
    Delete,
    Paste,
    Search,
    Filter,
    ArrowRight,
    ArrowLeft,
    ArrowUp,
    ChevronUp,
    ChevronDown,
    ChevronLeft,
    ChevronRight,
    DisclosureCollapsed,
    DisclosureExpanded,
    Check,
    Warning,
    Close,
    NavigationSessions,
    NavigationTools,
    NavigationPlugins,
    NavigationSettings,
    CollapseLeft,
    ExpandRight,
    Chat,
    WindowMinimize,
    WindowMaximize,
    WindowRestore,
    WindowClose,
    ResizeGrip,
}

/// <summary>
/// The single vector-glyph system used by buttons, navigation, drawers and
/// window chrome. Every glyph lives in the same 24x24 optical view box and
/// therefore shares stroke weight, caps, joins and centering at every DPI.
/// </summary>
internal static class ThemeGlyphRenderer
{
    private const float ViewSize = 24F;

    public static void Draw(Graphics graphics, ThemeGlyph glyph, RectangleF bounds, Color color)
    {
        if (glyph == ThemeGlyph.None || bounds.Width <= 1F || bounds.Height <= 1F) return;

        var side = Math.Min(bounds.Width, bounds.Height);
        var square = new RectangleF(
            bounds.Left + (bounds.Width - side) / 2F,
            bounds.Top + (bounds.Height - side) / 2F,
            side,
            side);
        var state = graphics.Save();
        try
        {
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
            graphics.SetClip(bounds, CombineMode.Intersect);
            using var pen = new Pen(color, OpticalStrokeWidth(side))
            {
                StartCap = LineCap.Round,
                EndCap = LineCap.Round,
                LineJoin = LineJoin.Round,
                Alignment = PenAlignment.Center,
            };
            using var fill = new SolidBrush(color);

            PointF P(float x, float y) => new(
                square.Left + x / ViewSize * square.Width,
                square.Top + y / ViewSize * square.Height);
            RectangleF R(float x, float y, float width, float height) => new(
                square.Left + x / ViewSize * square.Width,
                square.Top + y / ViewSize * square.Height,
                width / ViewSize * square.Width,
                height / ViewSize * square.Height);
            void Lines(params PointF[] points) => graphics.DrawLines(pen, points);

            switch (glyph)
            {
                case ThemeGlyph.Refresh:
                    DrawRefresh(graphics, pen, fill, square);
                    break;
                case ThemeGlyph.OpenExternal:
                    graphics.DrawRoundedRectangle(pen, R(4, 7, 13, 13), side * 0.07F);
                    graphics.DrawLine(pen, P(10, 14), P(20, 4));
                    Lines(P(13.5F, 4), P(20, 4), P(20, 10.5F));
                    break;
                case ThemeGlyph.Settings:
                case ThemeGlyph.NavigationSettings:
                    DrawGear(graphics, pen, square);
                    break;
                case ThemeGlyph.Monitor:
                    Lines(P(2.5F, 12), P(7, 12), P(9, 6), P(12.5F, 18), P(15, 12), P(21.5F, 12));
                    break;
                case ThemeGlyph.Play:
                    Lines(P(8, 5), P(19, 12), P(8, 19), P(8, 5));
                    break;
                case ThemeGlyph.Stop:
                    graphics.DrawRoundedRectangle(pen, R(6, 6, 12, 12), side * 0.05F);
                    break;
                case ThemeGlyph.Add:
                    graphics.DrawLine(pen, P(4.5F, 12), P(19.5F, 12));
                    graphics.DrawLine(pen, P(12, 4.5F), P(12, 19.5F));
                    break;
                case ThemeGlyph.Edit:
                    Lines(P(5, 18.5F), P(6.5F, 13.5F), P(15.5F, 4.5F), P(19.5F, 8.5F), P(10.5F, 17.5F), P(5, 18.5F));
                    graphics.DrawLine(pen, P(13.5F, 6.5F), P(17.5F, 10.5F));
                    break;
                case ThemeGlyph.Delete:
                    graphics.DrawLine(pen, P(4.5F, 7), P(19.5F, 7));
                    graphics.DrawLine(pen, P(9, 4.5F), P(15, 4.5F));
                    graphics.DrawRoundedRectangle(pen, R(6.5F, 7, 11, 13), side * 0.05F);
                    graphics.DrawLine(pen, P(10, 10), P(10, 17));
                    graphics.DrawLine(pen, P(14, 10), P(14, 17));
                    break;
                case ThemeGlyph.Paste:
                    graphics.DrawRoundedRectangle(pen, R(5.5F, 6.5F, 13, 14), side * 0.05F);
                    graphics.DrawRoundedRectangle(pen, R(8, 3.5F, 8, 5), side * 0.07F);
                    break;
                case ThemeGlyph.Search:
                    graphics.DrawEllipse(pen, R(4, 4, 12.5F, 12.5F));
                    graphics.DrawLine(pen, P(15, 15), P(20, 20));
                    break;
                case ThemeGlyph.Filter:
                    Lines(P(4, 5), P(20, 5), P(14.5F, 12), P(14.5F, 18.5F), P(9.5F, 21), P(9.5F, 12), P(4, 5));
                    break;
                case ThemeGlyph.ArrowRight:
                    graphics.DrawLine(pen, P(4, 12), P(20, 12));
                    Lines(P(14, 6), P(20, 12), P(14, 18));
                    break;
                case ThemeGlyph.ArrowLeft:
                    graphics.DrawLine(pen, P(20, 12), P(4, 12));
                    Lines(P(10, 6), P(4, 12), P(10, 18));
                    break;
                case ThemeGlyph.ArrowUp:
                    graphics.DrawLine(pen, P(12, 20), P(12, 4));
                    Lines(P(6, 10), P(12, 4), P(18, 10));
                    break;
                case ThemeGlyph.ChevronUp:
                    Lines(P(5, 15.5F), P(12, 8.5F), P(19, 15.5F));
                    break;
                case ThemeGlyph.ChevronDown:
                    Lines(P(5, 8.5F), P(12, 15.5F), P(19, 8.5F));
                    break;
                case ThemeGlyph.ChevronLeft:
                    Lines(P(15.5F, 5), P(8.5F, 12), P(15.5F, 19));
                    break;
                case ThemeGlyph.ChevronRight:
                    Lines(P(8.5F, 5), P(15.5F, 12), P(8.5F, 19));
                    break;
                case ThemeGlyph.DisclosureCollapsed:
                    // Disclosure markers intentionally occupy more of the shared
                    // view box than action chevrons. At the 12-13 logical-pixel
                    // drawer size the former 7..19 geometry produced only five
                    // to six pixels of visible ink and disappeared beside the
                    // chat glyph. Keep the optical centre while restoring the
                    // approved drawer hierarchy.
                    graphics.FillPolygon(fill, [P(4, 3), P(21, 12), P(4, 21)]);
                    break;
                case ThemeGlyph.DisclosureExpanded:
                    graphics.FillPolygon(fill, [P(3, 4), P(21, 4), P(12, 21)]);
                    break;
                case ThemeGlyph.Check:
                    Lines(P(4.5F, 12.5F), P(9.5F, 17.5F), P(19.5F, 6.5F));
                    break;
                case ThemeGlyph.Warning:
                    graphics.FillEllipse(fill, R(2, 2, 20, 20));
                    using (var warningPen = new Pen(UiTheme.WarningSurface, Math.Max(1.6F, side * 0.12F))
                    {
                        StartCap = LineCap.Round,
                        EndCap = LineCap.Round,
                    })
                    {
                        graphics.DrawLine(warningPen, P(12, 6.5F), P(12, 13.5F));
                        graphics.DrawLine(warningPen, P(12, 17.5F), P(12, 17.7F));
                    }
                    break;
                case ThemeGlyph.Close:
                case ThemeGlyph.WindowClose:
                    graphics.DrawLine(pen, P(5.5F, 5.5F), P(18.5F, 18.5F));
                    graphics.DrawLine(pen, P(18.5F, 5.5F), P(5.5F, 18.5F));
                    break;
                case ThemeGlyph.NavigationSessions:
                case ThemeGlyph.Chat:
                    DrawChat(graphics, pen, fill, square, glyph == ThemeGlyph.NavigationSessions);
                    break;
                case ThemeGlyph.NavigationTools:
                    graphics.DrawRoundedRectangle(pen, R(3, 7, 18, 13), side * 0.08F);
                    graphics.DrawRoundedRectangle(pen, R(8.5F, 4, 7, 5), side * 0.06F);
                    graphics.DrawLine(pen, P(3, 11), P(21, 11));
                    break;
                case ThemeGlyph.NavigationPlugins:
                    DrawPlugin(graphics, pen, square);
                    break;
                case ThemeGlyph.CollapseLeft:
                    Lines(P(13, 5), P(6, 12), P(13, 19));
                    Lines(P(20, 5), P(13, 12), P(20, 19));
                    break;
                case ThemeGlyph.ExpandRight:
                    Lines(P(4, 5), P(11, 12), P(4, 19));
                    Lines(P(11, 5), P(18, 12), P(11, 19));
                    break;
                case ThemeGlyph.WindowMinimize:
                    graphics.DrawLine(pen, P(6, 12), P(18, 12));
                    break;
                case ThemeGlyph.WindowMaximize:
                    graphics.DrawRectangle(pen, R(6, 6, 12, 12));
                    break;
                case ThemeGlyph.WindowRestore:
                    graphics.DrawRectangle(pen, R(7.5F, 8.5F, 10, 10));
                    Lines(P(9.5F, 8.5F), P(9.5F, 5.5F), P(19.5F, 5.5F), P(19.5F, 15.5F), P(17.5F, 15.5F));
                    break;
                case ThemeGlyph.ResizeGrip:
                    graphics.DrawLine(pen, P(17.5F, 7.5F), P(7.5F, 17.5F));
                    graphics.DrawLine(pen, P(20, 11), P(11, 20));
                    graphics.DrawLine(pen, P(20, 16), P(16, 20));
                    break;
            }
        }
        finally
        {
            graphics.Restore(state);
        }
    }

    internal static float OpticalStrokeWidth(float side)
    {
        if (side <= 0F) return 0F;
        // Small 14–16 logical-pixel actions are commonly 18–24 device pixels on
        // 125–150% displays.  Their former 8.5% stroke rasterized to a weak one-pixel
        // hairline.  Give that range a deliberate optical correction while keeping
        // larger navigation/caption glyphs restrained.
        var ratio = side <= 24F ? 0.115F : 0.09F;
        return Math.Max(1.75F, side * ratio);
    }

    private static void DrawRefresh(Graphics graphics, Pen pen, Brush fill, RectangleF square)
    {
        // One conventional clockwise circular arrow.  The circle remains
        // optically centred and the filled head overlaps its tangent, so the
        // real 14–16px action glyph cannot degrade into the old hook/open-C.
        square.Offset(square.Width * 0.035F, square.Height * 0.06F);
        var inset = square.Width * 0.20F;
        var arc = RectangleF.Inflate(square, -inset, -inset);
        const float startAngle = 45F;
        const float sweepAngle = 270F;
        graphics.DrawArc(pen, arc, startAngle, sweepAngle);

        var endRadians = (startAngle + sweepAngle) * MathF.PI / 180F;
        var tip = new PointF(
            arc.Left + arc.Width / 2F + MathF.Cos(endRadians) * arc.Width / 2F,
            arc.Top + arc.Height / 2F + MathF.Sin(endRadians) * arc.Height / 2F);
        var tangent = new PointF(-MathF.Sin(endRadians), MathF.Cos(endRadians));
        var normal = new PointF(-tangent.Y, tangent.X);
        var headLength = square.Width * 0.255F;
        var halfWidth = square.Width * 0.14F;
        var basePoint = new PointF(tip.X - tangent.X * headLength, tip.Y - tangent.Y * headLength);
        graphics.FillPolygon(fill,
        [
            new PointF(basePoint.X + normal.X * halfWidth, basePoint.Y + normal.Y * halfWidth),
            tip,
            new PointF(basePoint.X - normal.X * halfWidth, basePoint.Y - normal.Y * halfWidth),
        ]);
    }

    private static void DrawGear(Graphics graphics, Pen pen, RectangleF square)
    {
        var center = new PointF(square.Left + square.Width / 2F, square.Top + square.Height / 2F);
        var outer = square.Width * 0.34F;
        var inner = square.Width * 0.21F;
        graphics.DrawEllipse(pen, center.X - inner, center.Y - inner, inner * 2F, inner * 2F);
        for (var i = 0; i < 8; i++)
        {
            var angle = -MathF.PI / 2F + i * MathF.PI / 4F;
            var a = new PointF(center.X + MathF.Cos(angle) * outer * 0.70F, center.Y + MathF.Sin(angle) * outer * 0.70F);
            var b = new PointF(center.X + MathF.Cos(angle) * outer, center.Y + MathF.Sin(angle) * outer);
            graphics.DrawLine(pen, a, b);
        }
        graphics.DrawEllipse(pen, center.X - outer * 0.72F, center.Y - outer * 0.72F, outer * 1.44F, outer * 1.44F);
    }

    private static void DrawChat(Graphics graphics, Pen pen, Brush fill, RectangleF square, bool navigation)
    {
        var bubble = new RectangleF(square.Left + square.Width * 0.13F, square.Top + square.Height * 0.16F,
            square.Width * 0.74F, square.Height * 0.62F);
        graphics.DrawRoundedRectangle(pen, bubble, square.Width * 0.16F);
        graphics.DrawLines(pen,
        [
            new PointF(bubble.Left + bubble.Width * 0.26F, bubble.Bottom),
            new PointF(bubble.Left + bubble.Width * 0.18F, bubble.Bottom + square.Height * 0.12F),
            new PointF(bubble.Left + bubble.Width * 0.44F, bubble.Bottom),
        ]);
        var dotSize = Math.Max(1.5F, square.Width * (navigation ? 0.08F : 0.075F));
        for (var i = 0; i < 3; i++)
        {
            var x = bubble.Left + bubble.Width * (0.29F + i * 0.21F) - dotSize / 2F;
            graphics.FillEllipse(fill, x, bubble.Top + bubble.Height * 0.47F - dotSize / 2F, dotSize, dotSize);
        }
    }

    private static void DrawPlugin(Graphics graphics, Pen pen, RectangleF square)
    {
        PointF P(float x, float y) => new(
            square.Left + x / ViewSize * square.Width,
            square.Top + y / ViewSize * square.Height);

        // A single continuous puzzle outline.  The earlier negative-sweep arcs
        // changed topology after raster rounding and looked like a document or
        // hook at sidebar size.
        using var path = new GraphicsPath();
        path.StartFigure();
        path.AddLine(P(4, 6), P(8.5F, 6));
        path.AddBezier(P(8.5F, 6), P(8.3F, 4), P(9.8F, 2.8F), P(12, 2.8F));
        path.AddBezier(P(12, 2.8F), P(14.2F, 2.8F), P(15.7F, 4), P(15.5F, 6));
        path.AddLine(P(15.5F, 6), P(20, 6));
        path.AddLine(P(20, 6), P(20, 10));
        path.AddBezier(P(20, 10), P(18, 9.8F), P(16.8F, 11.2F), P(16.8F, 12.25F));
        path.AddBezier(P(16.8F, 12.25F), P(16.8F, 13.3F), P(18, 14.7F), P(20, 14.5F));
        path.AddLine(P(20, 14.5F), P(20, 20));
        path.AddLine(P(20, 20), P(4, 20));
        path.AddLine(P(4, 20), P(4, 15.5F));
        path.AddBezier(P(4, 15.5F), P(6, 15.7F), P(7.2F, 14.2F), P(7.2F, 13));
        path.AddBezier(P(7.2F, 13), P(7.2F, 11.8F), P(6, 10.3F), P(4, 10.5F));
        path.AddLine(P(4, 10.5F), P(4, 6));
        path.CloseFigure();
        graphics.DrawPath(pen, path);
    }
}

internal static class ThemeGlyphMap
{
    public static ThemeGlyph From(ButtonGlyph glyph) => glyph switch
    {
        ButtonGlyph.Refresh => ThemeGlyph.Refresh,
        ButtonGlyph.OpenExternal => ThemeGlyph.OpenExternal,
        ButtonGlyph.Settings => ThemeGlyph.Settings,
        ButtonGlyph.Monitor => ThemeGlyph.Monitor,
        ButtonGlyph.Play => ThemeGlyph.Play,
        ButtonGlyph.Stop => ThemeGlyph.Stop,
        ButtonGlyph.Add => ThemeGlyph.Add,
        ButtonGlyph.Edit => ThemeGlyph.Edit,
        ButtonGlyph.Delete => ThemeGlyph.Delete,
        ButtonGlyph.Paste => ThemeGlyph.Paste,
        ButtonGlyph.Search => ThemeGlyph.Search,
        ButtonGlyph.Filter => ThemeGlyph.Filter,
        ButtonGlyph.ArrowRight => ThemeGlyph.ArrowRight,
        ButtonGlyph.ArrowLeft => ThemeGlyph.ArrowLeft,
        ButtonGlyph.ArrowUp => ThemeGlyph.ArrowUp,
        ButtonGlyph.Close => ThemeGlyph.Close,
        _ => ThemeGlyph.None,
    };

    public static ThemeGlyph From(NavigationGlyph glyph, bool expandsSidebar) => glyph switch
    {
        NavigationGlyph.Sessions => ThemeGlyph.NavigationSessions,
        NavigationGlyph.Tools => ThemeGlyph.NavigationTools,
        NavigationGlyph.Plugins => ThemeGlyph.NavigationPlugins,
        NavigationGlyph.Settings => ThemeGlyph.NavigationSettings,
        NavigationGlyph.Collapse when expandsSidebar => ThemeGlyph.ExpandRight,
        NavigationGlyph.Collapse => ThemeGlyph.CollapseLeft,
        _ => ThemeGlyph.None,
    };

    public static ThemeGlyph From(CaptionButtonKind kind) => kind switch
    {
        CaptionButtonKind.Minimize => ThemeGlyph.WindowMinimize,
        CaptionButtonKind.Maximize => ThemeGlyph.WindowMaximize,
        CaptionButtonKind.Restore => ThemeGlyph.WindowRestore,
        CaptionButtonKind.Close => ThemeGlyph.WindowClose,
        _ => ThemeGlyph.None,
    };
}
