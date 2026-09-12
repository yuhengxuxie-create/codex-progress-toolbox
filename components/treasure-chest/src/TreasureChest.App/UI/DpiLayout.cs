using System.Runtime.CompilerServices;

namespace TreasureChest.UI;

public enum CaptionButtonKind
{
    Minimize,
    Maximize,
    Restore,
    Close,
}

public enum WindowResizeHit
{
    Client = 1,
    Left = 10,
    Right = 11,
    Top = 12,
    TopLeft = 13,
    TopRight = 14,
    Bottom = 15,
    BottomLeft = 16,
    BottomRight = 17,
}

internal interface IManualDpiLayoutParticipant
{
    int LayoutDpi { get; set; }
}

public readonly record struct CaptionButtonRects(Rectangle Minimize, Rectangle Maximize, Rectangle Close);

public readonly record struct CaptionLine(PointF Start, PointF End);

public sealed record CaptionIconGeometry(
    IReadOnlyList<CaptionLine> Lines,
    IReadOnlyList<RectangleF> Rectangles);

public sealed record DpiMetrics(
    int Dpi,
    Size DefaultWindowSize,
    Size MinimumWindowSize,
    int TitleBarHeight,
    int CaptionButtonWidth,
    int NavigationWidth,
    Padding NavigationPadding,
    int BrandHeight,
    int NavigationButtonHeight,
    int NavigationButtonGap,
    Padding PagePadding,
    int ActionButtonHeight,
    int GridHeaderHeight,
    int GridRowHeight,
    int ResizeBorder,
    int ResizeCornerSpan,
    int ResizeGripSize);

public sealed record MonitorToolbarLayout(
    int Dpi,
    Size ToolbarSize,
    int MinimumWidth,
    IReadOnlyList<Rectangle> ItemBounds);

public sealed record MonitorGridColumnLayout(
    int Title,
    int Classification,
    int Origin,
    int LastActivity,
    int Remaining,
    int ThreadId)
{
    public int Total => Title + Classification + Origin + LastActivity + Remaining + ThreadId;
}

public readonly record struct SessionServiceColumnLayout(
    int Name,
    int Status,
    int Detail,
    int Auto,
    int Source,
    int Checked)
{
    public int Total => Name + Status + Detail + Auto + Source + Checked;
}

public readonly record struct MonitorCatalogColumnLayout(int Title, int LastActivity, int MonitoringStatus)
{
    public int Total => Title + LastActivity + MonitoringStatus;
}

public readonly record struct MonitorListColumnLayout(int Title, int LastActivity, int Remaining, int ThreadId)
{
    public int Total => Title + LastActivity + Remaining + ThreadId;
}

/// <summary>
/// Main-window geometry measured from the approved 1557x1010 composition after
/// aspect-preserving conversion to the product's 1180x760 logical canvas.
/// Keeping the reference measurements here prevents the page from drifting back
/// to unrelated percentage guesses when the shell or DPI changes.
/// </summary>
public static class ApprovedMainWindowLayout
{
    public static readonly Padding ShellPadding = new(16, 0, 16, 21);
    public static readonly Padding NavigationPadding = new(12, 34, 12, 28);
    public static readonly Padding MonitorSectionPadding = new(0, 8, 0, 10);
    public static readonly Padding SessionSectionPadding = new(0, 4, 0, 4);
    public static readonly MonitorGridColumnLayout MonitorColumns = new(240, 150, 82, 145, 125, 280);
    // The status cell reserves a semantic dot before the text.  90 logical px
    // clipped ResetAlertPresentation's longer, user-actionable states at the
    // approved 1180px canvas.  The approved lower well has enough previously
    // unused width for a 140px status column without taking space from name or
    // detail, and DpiLayoutStore scales this baseline exactly at each monitor DPI.
    public static readonly SessionServiceColumnLayout SessionColumns =
        new(Name: 190, Status: 140, Detail: 270, Auto: 120, Source: 100, Checked: 100);
    public const int PageHeaderHeight = 89;
    public const int SidebarToggleHeight = 48;

    // Group rows reserve the disclosure marker, chat glyph and their internal
    // spacing before the actual title begins in GridVisualStyler.PaintChatCell.
    public const int MonitorTitleDecorationWidth = 62;

    // The lower table begins at logical y=538.  With a 54px caption, the shell,
    // 24px page inset and the measured 89px header, this places the section split
    // at y=480 over a 548px body: (480 - 167) / 548 = 57.12%.  Reducing the
    // upper section's bottom inset by the same six logical pixels keeps the
    // approved 240px upper table height while moving the lower table to y=538.
    public const float MonitorSectionPercent = 57.12F;
}

/// <summary>
/// Approved 96-DPI logical canvas for the project-monitor manager. WinForms maps
/// this canvas to the current monitor DPI; native window rectangles must use
/// <see cref="DeviceWindowSize"/> rather than treating it as device pixels.
/// </summary>
public static class ApprovedMonitorManagerLayout
{
    public static readonly Size DefaultWindowSize = new(1260, 738);
    public static readonly Size MinimumWindowSize = new(1120, 680);
    public const int HeaderHeight = 76;
    public const int ToolbarHostHeight = 84;
    public const int ToolbarHeight = 74;
    public const int ToolbarControlHeight = 40;
    public const int FooterHeight = 55;
    public const int ContentHorizontalInset = 12;
    public const int ContentTopInset = 3;
    public const int ContentBottomInset = 8;
    public const int TransferColumnWidth = 98;
    public const float CatalogColumnPercent = 45.4F;
    public const float MonitorColumnPercent = 54.6F;

    // Approved logical pixel heights at 96 DPI. Their point equivalents preserve
    // the same user-visible density across 96/120/144/192 DPI.
    public const float BodyFontPixels = 16F;
    public const float SectionFontPixels = 18F;
    public const float PageTitleFontPixels = 32F;
    public const float BodyFontPoints = 12F;
    public const float SectionFontPoints = 13.5F;
    public const float PageTitleFontPoints = 24F;

    public static Font CreateBodyFont(FontStyle style = FontStyle.Regular) =>
        new(UiTheme.FontFamily, BodyFontPoints, style, GraphicsUnit.Point);

    public static Font CreateSectionFont() =>
        new(UiTheme.FontFamily, SectionFontPoints, FontStyle.Bold, GraphicsUnit.Point);

    public static Font CreatePageTitleFont() =>
        new(UiTheme.FontFamily, PageTitleFontPoints, FontStyle.Bold, GraphicsUnit.Point);

    public static Size DeviceWindowSize(int dpi) => DpiLayout.Scale(DefaultWindowSize, dpi);

    public static Size DeviceMinimumWindowSize(int dpi) => DpiLayout.Scale(MinimumWindowSize, dpi);

    public static Size LogicalVisibleSize(Size deviceSize, int dpi) => DpiLayout.Unscale(deviceSize, dpi);

    public static MonitorCatalogColumnLayout CatalogColumns(int viewportWidth, int dpi = DpiLayout.BaselineDpi)
    {
        // DataGridView headers reserve substantially more room than a bare
        // TextRenderer string (cell padding, border and header glyph space).
        // Keep those short columns at the measured safe widths and return all
        // remaining room to the decorated drawer title.
        var lastActivity = DpiLayout.Scale(92, dpi);
        var monitoringStatus = DpiLayout.Scale(93, dpi);
        var usable = Math.Max(DpiLayout.Scale(263, dpi) + lastActivity + monitoringStatus, viewportWidth);
        var title = usable - lastActivity - monitoringStatus;
        return new MonitorCatalogColumnLayout(title, lastActivity, monitoringStatus);
    }

    public static MonitorListColumnLayout MonitorColumns(int viewportWidth, int dpi = DpiLayout.BaselineDpi)
    {
        var lastActivity = DpiLayout.Scale(96, dpi);
        var remaining = DpiLayout.Scale(96, dpi);
        var threadId = DpiLayout.Scale(120, dpi);
        var usable = Math.Max(DpiLayout.Scale(230, dpi) + lastActivity + remaining + threadId, viewportWidth);
        var title = usable - lastActivity - remaining - threadId;
        return new MonitorListColumnLayout(title, lastActivity, remaining, threadId);
    }
}

/// <summary>
/// 所有窗口几何都以 96 DPI 逻辑像素保存，再直接换算到目标 DPI。
/// 绝不把已经缩放过的像素值作为下一次缩放的输入，避免跨显示器来回移动后累积放大或缩小。
/// </summary>
public static class DpiLayout
{
    public const int BaselineDpi = 96;

    public static int NormalizeDpi(int dpi) => dpi is >= 72 and <= 768 ? dpi : BaselineDpi;

    public static int Scale(int logicalPixels, int dpi) =>
        (int)Math.Round(logicalPixels * NormalizeDpi(dpi) / (double)BaselineDpi, MidpointRounding.AwayFromZero);

    public static int Unscale(int devicePixels, int dpi) =>
        (int)Math.Round(devicePixels * BaselineDpi / (double)NormalizeDpi(dpi), MidpointRounding.AwayFromZero);

    public static Size Scale(Size logicalSize, int dpi) =>
        new(Scale(logicalSize.Width, dpi), Scale(logicalSize.Height, dpi));

    public static Size Unscale(Size deviceSize, int dpi) =>
        new(Unscale(deviceSize.Width, dpi), Unscale(deviceSize.Height, dpi));

    public static Padding Scale(Padding logicalPadding, int dpi) => new(
        Scale(logicalPadding.Left, dpi),
        Scale(logicalPadding.Top, dpi),
        Scale(logicalPadding.Right, dpi),
        Scale(logicalPadding.Bottom, dpi));

    public static Padding Unscale(Padding devicePadding, int dpi) => new(
        Unscale(devicePadding.Left, dpi),
        Unscale(devicePadding.Top, dpi),
        Unscale(devicePadding.Right, dpi),
        Unscale(devicePadding.Bottom, dpi));

    public static int FontPixelHeight(float points, int dpi) =>
        (int)Math.Ceiling(points * NormalizeDpi(dpi) / 72F);

    public static DpiMetrics Metrics(int dpi)
    {
        dpi = NormalizeDpi(dpi);
        return new DpiMetrics(
            dpi,
            Scale(new Size(1180, 760), dpi),
            Scale(new Size(980, 650), dpi),
            Scale(50, dpi),
            Scale(50, dpi),
            Scale(210, dpi),
            Scale(new Padding(14, 28, 14, 14), dpi),
            Scale(82, dpi),
            Scale(46, dpi),
            Scale(8, dpi),
            Scale(new Padding(28), dpi),
            Scale(34, dpi),
            Scale(46, dpi),
            Scale(48, dpi),
            Math.Max(6, Scale(8, dpi)),
            Math.Max(14, Scale(20, dpi)),
            Math.Max(16, Scale(20, dpi)));
    }

    public static CaptionButtonRects CaptionButtons(Size titleBarSize, int dpi)
    {
        var width = Metrics(dpi).CaptionButtonWidth;
        var height = titleBarSize.Height;
        var close = new Rectangle(Math.Max(0, titleBarSize.Width - width), 0, width, height);
        var maximize = new Rectangle(Math.Max(0, close.Left - width), 0, width, height);
        var minimize = new Rectangle(Math.Max(0, maximize.Left - width), 0, width, height);
        return new CaptionButtonRects(minimize, maximize, close);
    }

    public static CaptionIconGeometry CaptionIcon(CaptionButtonKind kind, Size clientSize, int dpi)
    {
        dpi = NormalizeDpi(dpi);
        var center = new PointF(clientSize.Width / 2F, clientSize.Height / 2F);
        var span = Math.Max(8, Scale(10, dpi));
        var half = span / 2F;
        var lines = new List<CaptionLine>();
        var rectangles = new List<RectangleF>();
        switch (kind)
        {
            case CaptionButtonKind.Minimize:
                lines.Add(new CaptionLine(new PointF(center.X - half, center.Y), new PointF(center.X + half, center.Y)));
                break;
            case CaptionButtonKind.Maximize:
                rectangles.Add(new RectangleF(center.X - half, center.Y - half, span, span));
                break;
            case CaptionButtonKind.Restore:
                var offset = Math.Max(2, Scale(3, dpi));
                var restoreSpan = Math.Max(7, span - offset);
                rectangles.Add(new RectangleF(center.X - restoreSpan / 2F + offset / 2F,
                    center.Y - restoreSpan / 2F - offset / 2F, restoreSpan, restoreSpan));
                rectangles.Add(new RectangleF(center.X - restoreSpan / 2F - offset / 2F,
                    center.Y - restoreSpan / 2F + offset / 2F, restoreSpan, restoreSpan));
                break;
            case CaptionButtonKind.Close:
                lines.Add(new CaptionLine(new PointF(center.X - half, center.Y - half), new PointF(center.X + half, center.Y + half)));
                lines.Add(new CaptionLine(new PointF(center.X + half, center.Y - half), new PointF(center.X - half, center.Y + half)));
                break;
        }
        return new CaptionIconGeometry(lines, rectangles);
    }

    public static RectangleF CaptionVisualBounds(CaptionIconGeometry geometry, float strokeWidth)
    {
        var halfStroke = Math.Max(0.5F, strokeWidth / 2F);
        var bounds = new List<RectangleF>(geometry.Lines.Count + geometry.Rectangles.Count);
        bounds.AddRange(geometry.Lines.Select(line => RectangleF.FromLTRB(
            Math.Min(line.Start.X, line.End.X) - halfStroke,
            Math.Min(line.Start.Y, line.End.Y) - halfStroke,
            Math.Max(line.Start.X, line.End.X) + halfStroke,
            Math.Max(line.Start.Y, line.End.Y) + halfStroke)));
        bounds.AddRange(geometry.Rectangles.Select(rectangle => RectangleF.FromLTRB(
            rectangle.Left - halfStroke,
            rectangle.Top - halfStroke,
            rectangle.Right + halfStroke,
            rectangle.Bottom + halfStroke)));
        if (bounds.Count == 0) return RectangleF.Empty;
        var result = bounds[0];
        foreach (var rectangle in bounds.Skip(1)) result = RectangleF.Union(result, rectangle);
        return result;
    }

    public static WindowResizeHit HitTest(
        Rectangle windowBounds,
        Point screenPoint,
        int dpi,
        FormWindowState windowState)
    {
        if (windowState != FormWindowState.Normal || !windowBounds.Contains(screenPoint)) return WindowResizeHit.Client;
        var metrics = Metrics(dpi);
        var leftEdge = screenPoint.X < windowBounds.Left + metrics.ResizeBorder;
        var rightEdge = screenPoint.X >= windowBounds.Right - metrics.ResizeBorder;
        var topEdge = screenPoint.Y < windowBounds.Top + metrics.ResizeBorder;
        var bottomEdge = screenPoint.Y >= windowBounds.Bottom - metrics.ResizeBorder;
        var nearLeft = screenPoint.X < windowBounds.Left + metrics.ResizeCornerSpan;
        var nearRight = screenPoint.X >= windowBounds.Right - metrics.ResizeCornerSpan;
        var nearTop = screenPoint.Y < windowBounds.Top + metrics.ResizeCornerSpan;
        var nearBottom = screenPoint.Y >= windowBounds.Bottom - metrics.ResizeCornerSpan;

        if ((leftEdge && nearTop) || (topEdge && nearLeft)) return WindowResizeHit.TopLeft;
        if ((rightEdge && nearTop) || (topEdge && nearRight)) return WindowResizeHit.TopRight;
        if ((leftEdge && nearBottom) || (bottomEdge && nearLeft)) return WindowResizeHit.BottomLeft;
        if ((rightEdge && nearBottom) || (bottomEdge && nearRight)) return WindowResizeHit.BottomRight;
        if (leftEdge) return WindowResizeHit.Left;
        if (rightEdge) return WindowResizeHit.Right;
        if (topEdge) return WindowResizeHit.Top;
        if (bottomEdge) return WindowResizeHit.Bottom;
        return WindowResizeHit.Client;
    }

    public static Rectangle ConstrainToWorkingArea(Point preferredLocation, Size preferredSize, Rectangle workingArea, int dpi)
    {
        var margin = Scale(16, dpi);
        var maximum = new Size(
            Math.Max(1, workingArea.Width - margin * 2),
            Math.Max(1, workingArea.Height - margin * 2));
        // This method also repairs an already-open window after a display hot
        // unplug.  Never enlarge that existing physical window merely because
        // its current monitor reports a higher DPI; Form.MinimumSize continues
        // to enforce the interactive resize floor.  Only shrink when the new
        // working area truly cannot contain the preferred size.
        var size = new Size(
            Math.Min(Math.Max(1, preferredSize.Width), maximum.Width),
            Math.Min(Math.Max(1, preferredSize.Height), maximum.Height));
        var x = Math.Clamp(preferredLocation.X, workingArea.Left, Math.Max(workingArea.Left, workingArea.Right - size.Width));
        var y = Math.Clamp(preferredLocation.Y, workingArea.Top, Math.Max(workingArea.Top, workingArea.Bottom - size.Height));
        return new Rectangle(new Point(x, y), size);
    }

    public static MonitorToolbarLayout ProjectMonitorToolbar(int dpi, int availableWidth = 0)
    {
        dpi = NormalizeDpi(dpi);
        // Only the search/condition slot grows.  The segmented selector and the
        // four commands keep their approved compact geometry so a wider window
        // gives useful space back to long project names instead of opening a
        // blank strip at the right edge of the toolbar.
        // The archived capsule needs a little more than its rendered text width
        // once the check glyph and horizontal breathing room are included. Keep
        // that reservation explicit; the search slot alone absorbs the trade-off.
        var logicalWidths = new[] { 240, 190, 205, 165, 140, 165 };
        var logicalHeights = new[] { 40, 40, 40, 40, 40, 40 };
        var gap = Scale(8, dpi);
        var horizontalPadding = Scale(12, dpi);
        var toolbarHeight = Scale(ApprovedMonitorManagerLayout.ToolbarHeight, dpi);
        var minimumWidth = logicalWidths.Sum(width => Scale(width, dpi)) +
                           gap * (logicalWidths.Length - 1) + horizontalPadding * 2;
        var actualWidth = Math.Max(minimumWidth, availableWidth);
        logicalWidths[0] += Unscale(actualWidth - minimumWidth, dpi);
        var x = horizontalPadding;
        var bounds = new List<Rectangle>(logicalWidths.Length);
        for (var index = 0; index < logicalWidths.Length; index++)
        {
            var width = Scale(logicalWidths[index], dpi);
            var height = Scale(logicalHeights[index], dpi);
            var y = (toolbarHeight - height) / 2;
            bounds.Add(new Rectangle(x, y, width, height));
            x += width + gap;
        }
        return new MonitorToolbarLayout(dpi, new Size(actualWidth, toolbarHeight), minimumWidth, bounds);
    }

    public static Rectangle CenterControl(Rectangle host, Size preferredSize, bool fillWidth, bool fillHeight)
    {
        var width = fillWidth ? host.Width : Math.Min(preferredSize.Width, host.Width);
        var height = fillHeight ? host.Height : Math.Min(preferredSize.Height, host.Height);
        return new Rectangle(
            host.Left + (host.Width - width) / 2,
            host.Top + (host.Height - height) / 2,
            width,
            height);
    }
}

/// <summary>
/// 保存控件的 96-DPI 基线，只从基线生成目标像素。主窗使用 AutoScaleMode.None，
/// 因此 WinForms 不会再对同一组像素进行第二次递归缩放。
/// </summary>
internal interface IDpiLayoutExcluded
{
}

internal sealed class DpiLayoutStore
{
    private readonly ConditionalWeakTable<Control, ControlBaseline> _controls = new();
    private readonly ConditionalWeakTable<DataGridViewColumn, ColumnBaseline> _columns = new();
    private readonly ConditionalWeakTable<DataGridViewRow, RowBaseline> _rows = new();
    private readonly ConditionalWeakTable<TableLayoutPanel, TableBaseline> _tables = new();

    public void CaptureBaseTree(Control root) => CaptureTree(root, DpiLayout.BaselineDpi);

    public void CaptureTree(Control root, int sourceDpi)
    {
        if (root is IDpiLayoutExcluded) return;
        CaptureControl(root, sourceDpi);
        foreach (Control child in root.Controls) CaptureTree(child, sourceDpi);
    }

    public void RefreshMutableValues(Control root, int sourceDpi)
    {
        if (root is IDpiLayoutExcluded) return;
        if (root is DataGridView grid)
        {
            foreach (DataGridViewColumn column in grid.Columns)
            {
                if (!_columns.TryGetValue(column, out var baseline))
                {
                    baseline = new ColumnBaseline(
                        DpiLayout.Unscale(column.Width, sourceDpi),
                        DpiLayout.Unscale(column.MinimumWidth, sourceDpi));
                    _columns.Add(column, baseline);
                }
                baseline.Width = DpiLayout.Unscale(column.Width, sourceDpi);
                baseline.MinimumWidth = DpiLayout.Unscale(column.MinimumWidth, sourceDpi);
            }
            foreach (DataGridViewRow row in grid.Rows)
            {
                if (!_rows.TryGetValue(row, out _)) _rows.Add(row, new RowBaseline(DpiLayout.Unscale(row.Height, sourceDpi)));
            }
        }
        foreach (Control child in root.Controls) RefreshMutableValues(child, sourceDpi);
    }

    public void ApplyTree(Control root, int dpi)
    {
        if (root is IDpiLayoutExcluded) return;
        if (root is IManualDpiLayoutParticipant participant) participant.LayoutDpi = dpi;
        if (_controls.TryGetValue(root, out var baseline)) ApplyControl(root, baseline, dpi);
        if (root is TableLayoutPanel table && _tables.TryGetValue(table, out var tableBaseline)) ApplyTable(table, tableBaseline, dpi);
        if (root is DataGridView grid) ApplyGrid(grid, dpi);
        foreach (Control child in root.Controls) ApplyTree(child, dpi);
    }

    private void CaptureControl(Control control, int sourceDpi)
    {
        if (!_controls.TryGetValue(control, out _))
        {
            var dataGrid = control as DataGridView;
            _controls.Add(control, new ControlBaseline(
                control.Dock,
                control.AutoSize,
                DpiLayout.Unscale(control.Size, sourceDpi),
                DpiLayout.Unscale(control.MinimumSize, sourceDpi),
                DpiLayout.Unscale(control.MaximumSize, sourceDpi),
                DpiLayout.Unscale(control.Padding, sourceDpi),
                DpiLayout.Unscale(control.Margin, sourceDpi),
                dataGrid is null ? 0 : DpiLayout.Unscale(dataGrid.ColumnHeadersHeight, sourceDpi),
                dataGrid is null ? 0 : DpiLayout.Unscale(dataGrid.RowTemplate.Height, sourceDpi),
                dataGrid is null ? Padding.Empty : DpiLayout.Unscale(dataGrid.DefaultCellStyle.Padding, sourceDpi)));
        }
        if (control is TableLayoutPanel table && !_tables.TryGetValue(table, out _))
        {
            _tables.Add(table, new TableBaseline(
                table.RowStyles.Cast<RowStyle>().Select(style => style.SizeType == SizeType.Absolute
                    ? DpiLayout.Unscale((int)Math.Round(style.Height), sourceDpi)
                    : (int?)null).ToArray(),
                table.ColumnStyles.Cast<ColumnStyle>().Select(style => style.SizeType == SizeType.Absolute
                    ? DpiLayout.Unscale((int)Math.Round(style.Width), sourceDpi)
                    : (int?)null).ToArray()));
        }
        if (control is not DataGridView grid) return;
        foreach (DataGridViewColumn column in grid.Columns)
        {
            if (!_columns.TryGetValue(column, out _))
                _columns.Add(column, new ColumnBaseline(
                    DpiLayout.Unscale(column.Width, sourceDpi),
                    DpiLayout.Unscale(column.MinimumWidth, sourceDpi)));
        }
        foreach (DataGridViewRow row in grid.Rows)
        {
            if (!_rows.TryGetValue(row, out _))
                _rows.Add(row, new RowBaseline(DpiLayout.Unscale(row.Height, sourceDpi)));
        }
    }

    private static void ApplyControl(Control control, ControlBaseline baseline, int dpi)
    {
        control.Padding = DpiLayout.Scale(baseline.Padding, dpi);
        control.Margin = DpiLayout.Scale(baseline.Margin, dpi);
        control.MinimumSize = DpiLayout.Scale(baseline.MinimumSize, dpi);
        if (!baseline.MaximumSize.IsEmpty) control.MaximumSize = DpiLayout.Scale(baseline.MaximumSize, dpi);
        // A non-normal top-level window is sized by Windows. Applying the
        // saved normal bounds here can immediately undo maximize/minimize.
        if (baseline.AutoSize || control is Form { WindowState: not FormWindowState.Normal }) return;
        var target = DpiLayout.Scale(baseline.Size, dpi);
        switch (baseline.Dock)
        {
            case DockStyle.Top:
            case DockStyle.Bottom:
                control.Height = target.Height;
                break;
            case DockStyle.Left:
            case DockStyle.Right:
                control.Width = target.Width;
                break;
            case DockStyle.None:
                control.Size = target;
                break;
        }
    }

    private static void ApplyTable(TableLayoutPanel table, TableBaseline baseline, int dpi)
    {
        for (var index = 0; index < Math.Min(table.RowStyles.Count, baseline.Rows.Length); index++)
            if (baseline.Rows[index] is { } logical) table.RowStyles[index].Height = DpiLayout.Scale(logical, dpi);
        for (var index = 0; index < Math.Min(table.ColumnStyles.Count, baseline.Columns.Length); index++)
            if (baseline.Columns[index] is { } logical) table.ColumnStyles[index].Width = DpiLayout.Scale(logical, dpi);
    }

    private void ApplyGrid(DataGridView grid, int dpi)
    {
        if (_controls.TryGetValue(grid, out var baseline))
        {
            grid.ColumnHeadersHeight = DpiLayout.Scale(baseline.GridHeaderHeight, dpi);
            grid.RowTemplate.Height = DpiLayout.Scale(baseline.GridRowHeight, dpi);
            grid.DefaultCellStyle.Padding = DpiLayout.Scale(baseline.GridCellPadding, dpi);
        }
        foreach (DataGridViewColumn column in grid.Columns)
        {
            if (!_columns.TryGetValue(column, out var columnBaseline)) continue;
            column.MinimumWidth = Math.Max(2, DpiLayout.Scale(columnBaseline.MinimumWidth, dpi));
            column.Width = Math.Max(column.MinimumWidth, DpiLayout.Scale(columnBaseline.Width, dpi));
        }
        foreach (DataGridViewRow row in grid.Rows)
            if (_rows.TryGetValue(row, out var rowBaseline)) row.Height = DpiLayout.Scale(rowBaseline.Height, dpi);
    }

    private sealed class ControlBaseline
    {
        public ControlBaseline(DockStyle dock, bool autoSize, Size size, Size minimumSize, Size maximumSize,
            Padding padding, Padding margin, int gridHeaderHeight, int gridRowHeight, Padding gridCellPadding)
        {
            Dock = dock;
            AutoSize = autoSize;
            Size = size;
            MinimumSize = minimumSize;
            MaximumSize = maximumSize;
            Padding = padding;
            Margin = margin;
            GridHeaderHeight = gridHeaderHeight;
            GridRowHeight = gridRowHeight;
            GridCellPadding = gridCellPadding;
        }

        public DockStyle Dock { get; }
        public bool AutoSize { get; }
        public Size Size { get; }
        public Size MinimumSize { get; }
        public Size MaximumSize { get; }
        public Padding Padding { get; }
        public Padding Margin { get; }
        public int GridHeaderHeight { get; }
        public int GridRowHeight { get; }
        public Padding GridCellPadding { get; }
    }

    private sealed class ColumnBaseline
    {
        public ColumnBaseline(int width, int minimumWidth) { Width = width; MinimumWidth = minimumWidth; }
        public int Width { get; set; }
        public int MinimumWidth { get; set; }
    }

    private sealed record RowBaseline(int Height);
    private sealed record TableBaseline(int?[] Rows, int?[] Columns);
}
