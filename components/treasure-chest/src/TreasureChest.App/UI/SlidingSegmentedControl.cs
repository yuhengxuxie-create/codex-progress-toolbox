namespace TreasureChest.UI;

/// <summary>
/// Shared two-way selector used by both search mode and day/night mode.  Geometry,
/// rendering and the reversible timeline intentionally live in one implementation.
/// </summary>
public sealed class SlidingSegmentedControl : Control, IExplicitAnimationPaintSource
{
    private readonly string[] _labels;
    private double _position;
    private int _selectedIndex;

    public SlidingSegmentedControl(string leftText, string rightText)
    {
        _labels = [leftText, rightText];
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Selectable |
                 ControlStyles.Opaque, true);
        Size = new Size(220, 40);
        MinimumSize = new Size(160, 34);
        Cursor = Cursors.Hand;
        Font = UiTheme.CreateFont(9F, FontStyle.Bold);
        AccessibleRole = AccessibleRole.RadioButton;
        TabStop = true;
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public event EventHandler? SelectedIndexChanged;

    public int SelectedIndex
    {
        get => _selectedIndex;
        set => Select(value, animate: false);
    }

    public double VisualPosition => _position;

    internal bool SynchronizeWithThemeTransition { get; set; }

    public void Select(int index, bool animate = true)
    {
        if (index is < 0 or > 1) throw new ArgumentOutOfRangeException(nameof(index));
        var changed = _selectedIndex != index;
        _selectedIndex = index;
        AccessibleName = $"{_labels[0]} / {_labels[1]}：{_labels[index]}";
        var start = _position;
        var target = (double)index;
        if (SynchronizeWithThemeTransition && animate && changed)
        {
            // UiTheme owns the one shared Stopwatch/quintic timeline.  Keep the
            // current visual position until that same palette frame advances it.
            AnimationRunner.Cancel(this, "selection");
        }
        else if (!animate || !IsHandleCreated || Math.Abs(start - target) < 0.0001D)
        {
            AnimationRunner.Cancel(this, "selection");
            _position = target;
            Invalidate();
        }
        else
        {
            AnimationRunner.Start(this, "selection", AnimationTokens.StandardDurationMs, progress =>
            {
                _position = start + (target - start) * progress;
                Invalidate();
            });
        }
        if (changed) SelectedIndexChanged?.Invoke(this, EventArgs.Empty);
    }

    internal double BeginSynchronizedTransition(int index)
    {
        if (index is < 0 or > 1) throw new ArgumentOutOfRangeException(nameof(index));
        AnimationRunner.Cancel(this, "selection");
        _selectedIndex = index;
        AccessibleName = $"{_labels[0]} / {_labels[1]}：{_labels[index]}";
        return _position;
    }

    internal void ApplySynchronizedFrame(double position)
    {
        _position = Math.Clamp(position, 0D, 1D);
        Invalidate();
        if (Visible && IsHandleCreated) Update();
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        if (e.Button == MouseButtons.Left) Select(e.X < ClientSize.Width / 2 ? 0 : 1);
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        base.OnKeyDown(e);
        if (e.KeyCode is Keys.Left or Keys.Up) Select(0);
        else if (e.KeyCode is Keys.Right or Keys.Down or Keys.Space) Select(1);
        else return;
        e.Handled = true;
        e.SuppressKeyPress = true;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var client = ClientRectangle;
        if (client.Width <= 1 || client.Height <= 1) return;
        // The control already owns a WinForms double-buffered surface.  Draw the
        // complete capsule and its two labels exactly once on that opaque surface.
        // TextRenderer uses GDI/ClearType; drawing it into a PArgb bitmap and then
        // compositing that bitmap a second time produced a visible dark duplicate
        // around the inactive day-theme label after a live theme transition.
        var graphics = e.Graphics;
        ThemePaint.Configure(graphics);
        graphics.Clear(UiTheme.Palette.Surface);
        var state = graphics.Save();
        try
        {
            var bounds = ThemePaint.AlignedBounds(client);
            var radius = bounds.Height / 2F;
            using (var track = ThemePaint.RoundedPathExact(bounds, radius))
            using (var brush = new SolidBrush(UiTheme.SurfaceAlt))
                graphics.FillPath(brush, track);

            var half = bounds.Width / 2F;
            var selected = new RectangleF(bounds.Left + (float)(_position * half), bounds.Top, half, bounds.Height);
            using (var pill = ThemePaint.RoundedPathExact(selected, radius))
            using (var brush = new SolidBrush(UiTheme.Accent))
                graphics.FillPath(brush, pill);

            var textHalf = client.Width / 2F;
            DrawText(graphics, _labels[0], new RectangleF(0F, 0F, textHalf, client.Height), 1D - _position);
            DrawText(graphics, _labels[1], new RectangleF(textHalf, 0F, textHalf, client.Height), _position);
        }
        finally
        {
            graphics.Restore(state);
        }
        if (Focused && ShowFocusCues)
            ThemePaint.DrawFocusRing(e.Graphics, client,
                CornerRadiusTokens.Pill(client.Height), DeviceDpi, UiTheme.Accent);
        AnimationRunner.ReportPaint(this);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        // The rounded capsule is intentionally inset by half a device pixel for
        // anti-aliasing.  Never let the native Control background (SystemColors.Window
        // on some night-theme handle creation paths) show through that outer sample or
        // an endpoint seam as a stable white strip.
        e.Graphics.Clear(UiTheme.Palette.Surface);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            AnimationRunner.Cancel(this);
            UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        }
        base.Dispose(disposing);
    }

    private void DrawText(Graphics graphics, string text, RectangleF bounds, double selectedAmount)
    {
        var color = Blend(UiTheme.Text, UiTheme.OnAccent, selectedAmount);
        ThemePaint.DrawText(graphics, text, Font, Rectangle.Round(bounds), color,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter |
            TextFormatFlags.NoPadding | TextFormatFlags.NoPrefix |
            TextFormatFlags.PreserveGraphicsClipping | TextFormatFlags.PreserveGraphicsTranslateTransform);
    }

    private static Color Blend(Color from, Color to, double amount) => Color.FromArgb(
        (int)Math.Round(from.R + (to.R - from.R) * amount),
        (int)Math.Round(from.G + (to.G - from.G) * amount),
        (int)Math.Round(from.B + (to.B - from.B) * amount));

    private void ThemeFrameChanged(object? sender, EventArgs e) => Invalidate();
}
