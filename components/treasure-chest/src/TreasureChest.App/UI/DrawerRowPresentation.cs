using System.Runtime.CompilerServices;

namespace TreasureChest.UI;

public enum DrawerRowKind
{
    CollapsedGroup,
    ExpandedGroup,
    Child,
}

public readonly record struct DrawerRowVisualStyle(
    Color BackColor,
    Color ForeColor,
    Color SelectionBackColor,
    Color SelectionForeColor,
    bool Bold,
    int NameIndentLogical);

public readonly record struct DrawerRowDecoration(
    Color DividerColor,
    int BottomThickness);

public readonly record struct DrawerVisualMetrics(
    int RowHeight,
    int LeftInset,
    int DisclosureSize,
    int DisclosureSlot,
    int ChatSize,
    int GlyphTextGap,
    int RightInset)
{
    public int ChatLeft => LeftInset + DisclosureSlot;
    public int TextLeft => ChatLeft + ChatSize + GlyphTextGap;

    public static DrawerVisualMetrics ForDpi(int dpi) => new(
        DpiLayout.Scale(40, dpi),
        DpiLayout.Scale(12, dpi),
        DpiLayout.Scale(13, dpi),
        DpiLayout.Scale(22, dpi),
        DpiLayout.Scale(12, dpi),
        DpiLayout.Scale(7, dpi),
        DpiLayout.Scale(8, dpi));
}

/// <summary>
/// Defines the visual priority of drawer rows shared by the main monitor list and
/// all three lists in the monitor manager.  Group state deliberately outranks the
/// DataGridView selection state so an expanded group cannot look like an ordinary
/// selected child row.
/// </summary>
public static class DrawerRowPresentation
{
    private static readonly Font RegularFont = UiTheme.CreateFont();
    private static readonly Font BoldFont = UiTheme.CreateFont(style: FontStyle.Bold);
    private static readonly ConditionalWeakTable<DataGridView, object> ConfiguredGrids = new();
    private static readonly ConditionalWeakTable<DataGridView, DrawerFonts> FontsByGrid = new();
    private static readonly ConditionalWeakTable<DataGridViewRow, RowMetadata> RowMetadataByRow = new();

    private sealed record RowMetadata(DrawerRowKind Kind, bool IsLastChild);
    private sealed record DrawerFonts(Font Regular, Font Bold);

    /// <summary>
    /// Actual text rectangle used by a grouped chat row after its disclosure
    /// triangle, chat glyph and spacing. Keeping this in the product path lets
    /// layout tests validate the same usable pixels that the renderer owns.
    /// </summary>
    public static Rectangle GroupTitleTextBounds(Rectangle cellBounds, int dpi)
    {
        var metrics = DrawerVisualMetrics.ForDpi(dpi);
        var left = cellBounds.Left + metrics.TextLeft;
        var rightPadding = metrics.RightInset;
        return new Rectangle(left, cellBounds.Top,
            Math.Max(1, cellBounds.Right - left - rightPadding), cellBounds.Height);
    }

    public static Rectangle ChildTitleTextBounds(Rectangle cellBounds, int dpi) =>
        GroupTitleTextBounds(cellBounds, dpi);

    public static DrawerRowVisualStyle Resolve(DrawerRowKind kind) => kind switch
    {
        DrawerRowKind.ExpandedGroup => new(
            UiTheme.Surface, UiTheme.Text, UiTheme.AccentSoft, UiTheme.Text, Bold: false, NameIndentLogical: 0),
        DrawerRowKind.CollapsedGroup => new(
            UiTheme.Surface, UiTheme.Text, UiTheme.AccentSoft, UiTheme.Text, Bold: false, NameIndentLogical: 0),
        _ => new(
            UiTheme.Surface, UiTheme.Text, UiTheme.AccentSoft, UiTheme.Text, Bold: false, NameIndentLogical: 0),
    };

    public static bool ConfigureGrid(DataGridView grid, Font? regularFont = null, Font? boldFont = null)
    {
        ArgumentNullException.ThrowIfNull(grid);
        if (regularFont is not null || boldFont is not null)
        {
            FontsByGrid.Remove(grid);
            FontsByGrid.Add(grid, new DrawerFonts(regularFont ?? RegularFont, boldFont ?? BoldFont));
        }
        if (ConfiguredGrids.TryGetValue(grid, out _)) return false;
        ConfiguredGrids.Add(grid, new object());
        grid.CellPainting += PaintDrawerDivider;
        grid.DpiChangedAfterParent += ReapplyRowsAfterDpiChange;
        return true;
    }

    public static DrawerRowDecoration ResolveDecoration(DrawerRowKind kind, bool isLastChild, int dpi)
    {
        _ = dpi;
        return kind == DrawerRowKind.Child && isLastChild
            ? new DrawerRowDecoration(UiTheme.Divider, 1)
            : new DrawerRowDecoration(UiTheme.Divider, 0);
    }

    public static DrawerRowDecoration DecorationFor(DataGridViewRow row, int dpi)
    {
        ArgumentNullException.ThrowIfNull(row);
        return RowMetadataByRow.TryGetValue(row, out var metadata)
            ? ResolveDecoration(metadata.Kind, metadata.IsLastChild, dpi)
            : new DrawerRowDecoration(UiTheme.Divider, 0);
    }

    internal static bool TryGetKind(DataGridViewRow row, out DrawerRowKind kind)
    {
        ArgumentNullException.ThrowIfNull(row);
        if (RowMetadataByRow.TryGetValue(row, out var metadata))
        {
            kind = metadata.Kind;
            return true;
        }

        kind = DrawerRowKind.Child;
        return false;
    }

    public static void Apply(DataGridViewRow row, DrawerRowKind kind, int dpi, bool isLastChild = false)
    {
        ArgumentNullException.ThrowIfNull(row);
        if (row.DataGridView is not null) ConfigureGrid(row.DataGridView);
        isLastChild = kind == DrawerRowKind.Child && isLastChild;
        RowMetadataByRow.Remove(row);
        RowMetadataByRow.Add(row, new RowMetadata(kind, isLastChild));
        var style = Resolve(kind);
        row.DefaultCellStyle.BackColor = style.BackColor;
        row.DefaultCellStyle.ForeColor = style.ForeColor;
        row.DefaultCellStyle.SelectionBackColor = style.SelectionBackColor;
        row.DefaultCellStyle.SelectionForeColor = style.SelectionForeColor;
        var fonts = row.DataGridView is not null && FontsByGrid.TryGetValue(row.DataGridView, out var configured)
            ? configured
            : new DrawerFonts(RegularFont, BoldFont);
        row.DefaultCellStyle.Font = style.Bold ? fonts.Bold : fonts.Regular;

        if (row.Cells.Count > 0)
        {
            var horizontalPadding = DpiLayout.Scale(6, dpi);
            row.Cells[0].Style.Padding = new Padding(
                DpiLayout.Scale(style.NameIndentLogical, dpi),
                0,
                horizontalPadding,
                0);
        }
    }

    private static void ReapplyRowsAfterDpiChange(object? sender, EventArgs e)
    {
        if (sender is not DataGridView grid) return;
        foreach (DataGridViewRow row in grid.Rows)
        {
            if (RowMetadataByRow.TryGetValue(row, out var metadata))
                Apply(row, metadata.Kind, grid.DeviceDpi, metadata.IsLastChild);
        }
        grid.Invalidate(true);
    }

    private static void PaintDrawerDivider(object? sender, DataGridViewCellPaintingEventArgs e)
    {
        if (sender is not DataGridView grid || e.RowIndex < 0 || e.ColumnIndex < 0) return;
        // The shared live grid owns the final, post-WinForms boundary layer so its
        // one-pixel drawer divider cannot be doubled by the native cell border.
        if (grid is LiveResizableDataGridView) return;
        var row = grid.Rows[e.RowIndex];
        if (!RowMetadataByRow.TryGetValue(row, out var metadata) || metadata.Kind != DrawerRowKind.Child) return;

        var decoration = ResolveDecoration(metadata.Kind, metadata.IsLastChild, grid.DeviceDpi);
        var graphics = e.Graphics;
        if (graphics is null) return;
        e.Paint(e.ClipBounds, e.PaintParts);
        if (decoration.BottomThickness > 0)
        {
            using var dividerBrush = new SolidBrush(decoration.DividerColor);
            graphics.FillRectangle(
                dividerBrush,
                e.CellBounds.Left,
                e.CellBounds.Bottom - decoration.BottomThickness,
                e.CellBounds.Width,
                decoration.BottomThickness);
        }
        e.Handled = true;
    }
}
