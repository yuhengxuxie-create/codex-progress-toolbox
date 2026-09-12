using System.Runtime.CompilerServices;

namespace TreasureChest.UI;

internal static class GridVisualStyler
{
    private static readonly ConditionalWeakTable<DataGridView, Subscription> Subscriptions = new();

    internal static Rectangle StatusTextBounds(Rectangle cellBounds, int geometryDpi)
    {
        var dot = Math.Max(6, DpiLayout.Scale(7, geometryDpi));
        var warning = Math.Max(dot, DpiLayout.Scale(13, geometryDpi));
        var left = cellBounds.Left + 12;
        return new Rectangle(left + warning + 8, cellBounds.Top,
            Math.Max(1, cellBounds.Width - warning - 28), cellBounds.Height);
    }

    public static void Configure(DataGridView grid, string? chatColumn = null, string? statusColumn = null,
        int? geometryDpi = null)
    {
        grid.BorderStyle = BorderStyle.None;
        grid.CellBorderStyle = DataGridViewCellBorderStyle.None;
        grid.ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None;
        grid.GridColor = UiTheme.GridLine;
        grid.BackgroundColor = UiTheme.Surface;
        grid.ColumnHeadersHeight = Math.Max(grid.ColumnHeadersHeight, 42);
        grid.RowTemplate.Height = chatColumn is null
            ? Math.Max(grid.RowTemplate.Height, 42)
            : DrawerVisualMetrics.ForDpi(geometryDpi ?? DpiLayout.BaselineDpi).RowHeight;
        grid.DefaultCellStyle.Padding = new Padding(10, 0, 10, 0);
        if (Subscriptions.TryGetValue(grid, out var existing))
        {
            existing.Merge(chatColumn, statusColumn, geometryDpi);
            grid.Invalidate();
            return;
        }
        var subscription = new Subscription(chatColumn, statusColumn, geometryDpi);
        Subscriptions.Add(grid, subscription);
        grid.CellPainting += subscription.Paint;
    }

    private sealed class Subscription(string? chatColumn, string? statusColumn, int? geometryDpi)
    {
        private string? _chatColumn = chatColumn;
        private string? _statusColumn = statusColumn;
        private int? _geometryDpi = geometryDpi;

        public void Merge(string? chatColumn, string? statusColumn, int? geometryDpi)
        {
            if (!string.IsNullOrWhiteSpace(chatColumn)) _chatColumn = chatColumn;
            if (!string.IsNullOrWhiteSpace(statusColumn)) _statusColumn = statusColumn;
            if (geometryDpi is not null) _geometryDpi = geometryDpi;
        }

        public void Paint(object? sender, DataGridViewCellPaintingEventArgs e)
        {
            if (sender is not DataGridView grid || e.ColumnIndex < 0) return;
            if (e.RowIndex < 0)
            {
                PaintStandardCell(e);
                return;
            }
            var columnName = grid.Columns[e.ColumnIndex].Name;
            if (_chatColumn is not null && string.Equals(columnName, _chatColumn, StringComparison.OrdinalIgnoreCase))
            {
                PaintChatCell(grid, e, _geometryDpi ?? grid.DeviceDpi);
                return;
            }
            if (_statusColumn is not null && string.Equals(columnName, _statusColumn, StringComparison.OrdinalIgnoreCase))
            {
                PaintStatusCell(grid, e, _geometryDpi ?? grid.DeviceDpi);
                return;
            }
            PaintStandardCell(e);
        }

        private static void PaintStandardCell(DataGridViewCellPaintingEventArgs e)
        {
            if (e.Graphics is null) return;
            e.Paint(e.CellBounds, e.PaintParts & ~DataGridViewPaintParts.Border);
            DrawLightBorders(e);
            e.Handled = true;
        }

        private static void PaintChatCell(DataGridView grid, DataGridViewCellPaintingEventArgs e, int geometryDpi)
        {
            if (e.Graphics is null) return;
            var style = e.CellStyle ?? grid.DefaultCellStyle;
            e.PaintBackground(e.CellBounds, true);
            var value = Convert.ToString(e.FormattedValue) ?? string.Empty;
            var hasKind = DrawerRowPresentation.TryGetKind(grid.Rows[e.RowIndex], out var rowKind);
            var isGroup = hasKind && rowKind is DrawerRowKind.CollapsedGroup or DrawerRowKind.ExpandedGroup;
            var selected = (e.State & DataGridViewElementStates.Selected) != 0;
            var foreground = selected ? style.SelectionForeColor : style.ForeColor;
            var iconColor = UiTheme.Accent;
            var metrics = DrawerVisualMetrics.ForDpi(geometryDpi);
            var disclosureLeft = e.CellBounds.Left + metrics.LeftInset;
            if (isGroup)
            {
                var arrowSize = metrics.DisclosureSize;
                var cy = e.CellBounds.Top + e.CellBounds.Height / 2F;
                ThemeGlyphRenderer.Draw(
                    e.Graphics,
                    rowKind == DrawerRowKind.ExpandedGroup
                        ? ThemeGlyph.DisclosureExpanded
                        : ThemeGlyph.DisclosureCollapsed,
                    new RectangleF(disclosureLeft, cy - arrowSize / 2F, arrowSize, arrowSize),
                    UiTheme.Text);
            }
            var iconSize = metrics.ChatSize;
            var x = e.CellBounds.Left + metrics.ChatLeft;
            ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.Chat,
                new RectangleF(x, e.CellBounds.Top + (e.CellBounds.Height - iconSize) / 2F, iconSize, iconSize),
                iconColor);
            x += iconSize + metrics.GlyphTextGap;
            var textBounds = isGroup
                ? DrawerRowPresentation.GroupTitleTextBounds(e.CellBounds, geometryDpi)
                : DrawerRowPresentation.ChildTitleTextBounds(e.CellBounds, geometryDpi);
            ThemePaint.DrawText(e.Graphics, value, style.Font ?? grid.Font, textBounds, foreground,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
            DrawLightBorders(e);
            e.Handled = true;
        }

        private static void PaintStatusCell(DataGridView grid, DataGridViewCellPaintingEventArgs e, int geometryDpi)
        {
            if (e.Graphics is null) return;
            var style = e.CellStyle ?? grid.DefaultCellStyle;
            e.PaintBackground(e.CellBounds, true);
            var value = Convert.ToString(e.FormattedValue) ?? string.Empty;
            var selected = (e.State & DataGridViewElementStates.Selected) != 0;
            var semantic = StatusColor(value);
            var dot = Math.Max(6, DpiLayout.Scale(7, geometryDpi));
            var left = e.CellBounds.Left + 12;
            if (value.Equals("部分可用", StringComparison.CurrentCulture))
            {
                var warning = Math.Max(dot, DpiLayout.Scale(13, geometryDpi));
                var top = e.CellBounds.Top + (e.CellBounds.Height - warning) / 2;
                ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.Warning,
                    new RectangleF(left, top, warning, warning), semantic);
            }
            else
            {
                var top = e.CellBounds.Top + (e.CellBounds.Height - dot) / 2;
                using var brush = new SolidBrush(semantic);
                e.Graphics.FillEllipse(brush, left, top, dot, dot);
            }
            var textBounds = GridVisualStyler.StatusTextBounds(e.CellBounds, geometryDpi);
            ThemePaint.DrawText(e.Graphics, value, style.Font ?? Control.DefaultFont, textBounds,
                selected ? style.SelectionForeColor : semantic,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
            DrawLightBorders(e);
            e.Handled = true;
        }

        private static Color StatusColor(string value)
        {
            if (value.Contains("运行", StringComparison.CurrentCultureIgnoreCase) || value.Contains("长期", StringComparison.CurrentCultureIgnoreCase))
                return UiTheme.Running;
            if (value.Contains("临时", StringComparison.CurrentCultureIgnoreCase)) return UiTheme.Accent;
            if (value.Contains("部分可用", StringComparison.CurrentCultureIgnoreCase)) return UiTheme.Warning;
            if (value.Contains("异常", StringComparison.CurrentCultureIgnoreCase) || value.Contains("失败", StringComparison.CurrentCultureIgnoreCase))
                return UiTheme.Stopped;
            return UiTheme.Muted;
        }

        private static void DrawLightBorders(DataGridViewCellPaintingEventArgs e)
        {
            if (e.Graphics is null) return;
            using var border = new Pen(UiTheme.GridLine, 1F);
            e.Graphics.DrawLine(border, e.CellBounds.Left, e.CellBounds.Bottom, e.CellBounds.Right, e.CellBounds.Bottom);
            e.Graphics.DrawLine(border, e.CellBounds.Right, e.CellBounds.Top, e.CellBounds.Right, e.CellBounds.Bottom);
        }
    }
}
