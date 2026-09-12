namespace TreasureChest.UI;

/// <summary>
/// 在拖动表头分隔线时立即更新列宽。WinForms DataGridView 默认只绘制拖动指示线，
/// 直到鼠标松开才应用新宽度；这里接管分隔线拖动，让后续列和单元格实时重排。
/// </summary>
internal sealed class LiveResizableDataGridView : DataGridView, IExplicitAnimationPaintSource
{
    private DataGridViewColumn? _resizingColumn;
    private int _resizeStartX;
    private int _resizeStartWidth;
    private readonly ThemedGridHorizontalScrollBar _horizontalBar;

    public LiveResizableDataGridView()
    {
        DoubleBuffered = true;
        ScrollBars = ScrollBars.Vertical;
        _horizontalBar = new ThemedGridHorizontalScrollBar(this)
        {
            AccessibleName = "水平滚动",
            TabStop = false,
        };
        Controls.Add(_horizontalBar);
        SizeChanged += (_, _) => PositionHorizontalBar();
        ColumnAdded += (_, _) => PositionHorizontalBar();
        ColumnRemoved += (_, _) => PositionHorizontalBar();
        ColumnWidthChanged += (_, _) => PositionHorizontalBar();
        Scroll += (_, _) => _horizontalBar.SyncFromGrid();
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        PositionHorizontalBar();
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        PositionHorizontalBar();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        PaintApprovedGridLines(e.Graphics);
        AnimationRunner.ReportPaint(this);
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left && FindDividerColumn(e.Location) is { } column)
        {
            _resizingColumn = column;
            _resizeStartX = e.X;
            _resizeStartWidth = column.Width;
            Capture = true;
            Cursor = Cursors.VSplit;
            return;
        }
        base.OnMouseDown(e);
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        if (_resizingColumn is { } column)
        {
            column.Width = LiveColumnResize.CalculateWidth(
                _resizeStartWidth,
                e.X - _resizeStartX,
                column.MinimumWidth);
            PositionHorizontalBar();
            PerformLayout();
            Invalidate(true);
            Update();
            return;
        }

        Cursor = FindDividerColumn(e.Location) is null ? Cursors.Default : Cursors.VSplit;
        base.OnMouseMove(e);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        if (_resizingColumn is not null)
        {
            EndResize();
            return;
        }
        base.OnMouseUp(e);
    }

    protected override void OnMouseLeave(EventArgs e)
    {
        if (_resizingColumn is null) Cursor = Cursors.Default;
        base.OnMouseLeave(e);
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        if (e.KeyCode == Keys.Escape && _resizingColumn is { } column)
        {
            column.Width = _resizeStartWidth;
            EndResize();
            e.Handled = true;
            e.SuppressKeyPress = true;
            return;
        }
        base.OnKeyDown(e);
    }

    protected override void OnMouseCaptureChanged(EventArgs e)
    {
        if (!Capture && _resizingColumn is not null) EndResize();
        base.OnMouseCaptureChanged(e);
    }

    private DataGridViewColumn? FindDividerColumn(Point point)
    {
        if (!ColumnHeadersVisible || point.Y < 0 || point.Y > ColumnHeadersHeight) return null;
        var dividerGrip = LiveColumnResize.DividerGrip(DeviceDpi);
        foreach (DataGridViewColumn column in Columns.Cast<DataGridViewColumn>()
                     .Where(item => item.Visible)
                     .OrderBy(item => item.DisplayIndex))
        {
            if (column.Resizable == DataGridViewTriState.False ||
                column.AutoSizeMode != DataGridViewAutoSizeColumnMode.None) continue;
            var rectangle = GetColumnDisplayRectangle(column.Index, cutOverflow: true);
            if (rectangle.Width <= 0) continue;
            if (Math.Abs(point.X - rectangle.Right) <= dividerGrip) return column;
        }
        return null;
    }

    private void EndResize()
    {
        _resizingColumn = null;
        Capture = false;
        Cursor = Cursors.Default;
        Invalidate(true);
    }

    private void PositionHorizontalBar()
    {
        if (_horizontalBar is null || ClientSize.Width <= 1 || ClientSize.Height <= 1) return;
        var height = DpiLayout.Scale(18, DeviceDpi);
        _horizontalBar.Bounds = new Rectangle(0, Math.Max(0, ClientSize.Height - height), ClientSize.Width, height);
        _horizontalBar.BringToFront();
        _horizontalBar.SyncFromGrid();
    }

    private void PaintApprovedGridLines(Graphics graphics)
    {
        if (Columns.Count == 0 || ClientSize.Width <= 1 || ClientSize.Height <= 1) return;

        var bottom = _horizontalBar.Visible ? _horizontalBar.Top : ClientSize.Height - 1;
        using var line = new Pen(UiTheme.GridLine, 1F);
        foreach (DataGridViewColumn column in Columns.Cast<DataGridViewColumn>()
                     .Where(item => item.Visible)
                     .OrderBy(item => item.DisplayIndex))
        {
            var rectangle = GetColumnDisplayRectangle(column.Index, cutOverflow: true);
            if (rectangle.Width <= 0 || rectangle.Right <= 0 || rectangle.Right >= ClientSize.Width) continue;
            graphics.DrawLine(line, rectangle.Right, 0, rectangle.Right, bottom);
        }

        if (ColumnHeadersVisible && ColumnHeadersHeight > 0)
            graphics.DrawLine(line, 0, ColumnHeadersHeight, ClientSize.Width - 1, ColumnHeadersHeight);

        foreach (DataGridViewRow row in Rows)
        {
            if (!row.Visible) continue;
            var rectangle = GetRowDisplayRectangle(row.Index, cutOverflow: true);
            if (rectangle.Height <= 0 || rectangle.Bottom <= ColumnHeadersHeight || rectangle.Bottom > bottom) continue;
            var decoration = DrawerRowPresentation.DecorationFor(row, DeviceDpi);
            if (decoration.BottomThickness > 0)
            {
                using var divider = new Pen(decoration.DividerColor, decoration.BottomThickness);
                graphics.DrawLine(divider, 0, rectangle.Bottom, ClientSize.Width - 1, rectangle.Bottom);
            }
            else
            {
                graphics.DrawLine(line, 0, rectangle.Bottom, ClientSize.Width - 1, rectangle.Bottom);
            }
        }

        _horizontalBar.BringToFront();
    }
}

internal sealed class ThemedGridHorizontalScrollBar : Control
{
    private readonly DataGridView _owner;
    private int _maximum;
    private bool _dragging;
    private int _dragStartX;
    private int _dragStartOffset;

    public ThemedGridHorizontalScrollBar(DataGridView owner)
    {
        _owner = owner;
        BackColor = UiTheme.SurfaceAlt;
        Cursor = Cursors.Hand;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint, true);
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public void SyncFromGrid()
    {
        var contentWidth = _owner.Columns.Cast<DataGridViewColumn>()
            .Where(column => column.Visible)
            .Sum(column => column.Width);
        var viewportWidth = Math.Max(1, _owner.ClientSize.Width - DpiLayout.Scale(4, _owner.DeviceDpi));
        _maximum = Math.Max(0, contentWidth - viewportWidth);
        Visible = _maximum > 0;
        Invalidate();
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left || _maximum <= 0) return;
        var thumb = ThumbRectangle();
        var arrowWidth = ArrowWidth();
        if (e.X < arrowWidth)
        {
            SetOffset(_owner.HorizontalScrollingOffset - DpiLayout.Scale(80, DeviceDpi));
        }
        else if (e.X >= Width - arrowWidth)
        {
            SetOffset(_owner.HorizontalScrollingOffset + DpiLayout.Scale(80, DeviceDpi));
        }
        else if (thumb.Contains(e.Location))
        {
            _dragging = true;
            _dragStartX = e.X;
            _dragStartOffset = _owner.HorizontalScrollingOffset;
            Capture = true;
        }
        else
        {
            SetOffset(_owner.HorizontalScrollingOffset + (e.X < thumb.Left ? -1 : 1) * Math.Max(80, _owner.ClientSize.Width / 2));
        }
        Invalidate();
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        if (_dragging)
        {
            var track = TrackRectangle();
            var thumb = ThumbRectangle();
            var travel = Math.Max(1, track.Width - thumb.Width);
            var delta = e.X - _dragStartX;
            SetOffset(_dragStartOffset + (int)Math.Round(delta * _maximum / (double)travel));
        }
        base.OnMouseMove(e);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        _dragging = false;
        Capture = false;
        base.OnMouseUp(e);
    }

    protected override void OnMouseCaptureChanged(EventArgs e)
    {
        if (!Capture) _dragging = false;
        base.OnMouseCaptureChanged(e);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
        e.Graphics.Clear(UiTheme.SurfaceAlt);
        if (_maximum <= 0) return;
        var track = TrackRectangle();
        using (var trackPath = RoundedButton.RoundedPath(track, Math.Max(2, track.Height / 2)))
        using (var trackBrush = new SolidBrush(UiTheme.ProgressTrack))
            e.Graphics.FillPath(trackBrush, trackPath);
        var thumb = ThumbRectangle();
        using (var thumbPath = RoundedButton.RoundedPath(thumb, Math.Max(2, thumb.Height / 2)))
        using (var thumbBrush = new SolidBrush(UiTheme.Divider))
            e.Graphics.FillPath(thumbBrush, thumbPath);

        var arrowWidth = ArrowWidth();
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.ChevronLeft,
            new RectangleF(0, 0, arrowWidth, Height), UiTheme.NavigationMuted);
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.ChevronRight,
            new RectangleF(Width - arrowWidth, 0, arrowWidth, Height), UiTheme.NavigationMuted);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    private int ArrowWidth() => DpiLayout.Scale(20, DeviceDpi);

    private Rectangle TrackRectangle()
    {
        var arrow = ArrowWidth();
        var height = Math.Max(5, DpiLayout.Scale(6, DeviceDpi));
        return new Rectangle(arrow, (Height - height) / 2, Math.Max(1, Width - arrow * 2), height);
    }

    private Rectangle ThumbRectangle()
    {
        var track = TrackRectangle();
        var contentWidth = Math.Max(1, _owner.Columns.Cast<DataGridViewColumn>()
            .Where(column => column.Visible)
            .Sum(column => column.Width));
        var viewport = Math.Max(1, contentWidth - _maximum);
        var thumbWidth = Math.Clamp((int)Math.Round(track.Width * viewport / (double)contentWidth),
            Math.Min(track.Width, DpiLayout.Scale(52, DeviceDpi)), track.Width);
        var travel = Math.Max(0, track.Width - thumbWidth);
        var left = track.Left + (_maximum == 0 ? 0 : (int)Math.Round(travel * _owner.HorizontalScrollingOffset / (double)_maximum));
        return new Rectangle(left, track.Top, thumbWidth, track.Height);
    }

    private void SetOffset(int value)
    {
        var offset = Math.Clamp(value, 0, _maximum);
        if (_owner.HorizontalScrollingOffset != offset) _owner.HorizontalScrollingOffset = offset;
        Invalidate();
    }

    private void ThemeFrameChanged(object? sender, EventArgs e)
    {
        BackColor = UiTheme.SurfaceAlt;
        Invalidate();
    }
}

public static class LiveColumnResize
{
    public static int DividerGrip(int deviceDpi) => Math.Max(5, Math.Max(96, deviceDpi) / 24);

    public static int CalculateWidth(int startWidth, int horizontalDelta, int minimumWidth) =>
        Math.Max(Math.Max(2, minimumWidth), startWidth + horizontalDelta);
}
