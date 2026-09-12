using System.Drawing.Drawing2D;

namespace Ecosystem.Updater;

/// <summary>
/// The updater is intentionally a separate executable, so it cannot reference
/// TreasureChest.App's WinForms assembly.  These tokens mirror the product's
/// approved day surface and are kept here as the updater's small, dependency
/// free visual contract.
/// </summary>
internal static class UpdaterTheme
{
    internal static readonly Color Background = Color.FromArgb(241, 240, 249);
    internal static readonly Color Surface = Color.FromArgb(253, 252, 255);
    internal static readonly Color SurfaceAlt = Color.FromArgb(247, 247, 251);
    internal static readonly Color SurfaceHover = Color.FromArgb(246, 244, 252);
    internal static readonly Color Text = Color.FromArgb(24, 29, 45);
    internal static readonly Color Muted = Color.FromArgb(96, 99, 116);
    internal static readonly Color Border = Color.FromArgb(228, 229, 235);
    internal static readonly Color Accent = Color.FromArgb(101, 76, 244);
    internal static readonly Color AccentHover = Color.FromArgb(87, 62, 226);
    internal static readonly Color AccentPressed = Color.FromArgb(74, 50, 207);
    internal static readonly Color OnAccent = Color.White;
    internal static readonly Color DisabledBackground = Color.FromArgb(236, 235, 241);
    internal static readonly Color DisabledText = Color.FromArgb(146, 145, 155);
    internal static readonly Color Error = Color.FromArgb(170, 50, 74);
    internal static readonly Color ProgressTrack = Color.FromArgb(225, 225, 234);
    internal static readonly Color FocusRing = Color.FromArgb(210, 198, 255);

    internal const int ActionRadius = 12;
    internal const int InputRadius = 11;

    internal static int Scale(Control control, int logicalPixels) =>
        Scale(logicalPixels, control.DeviceDpi);

    internal static int Scale(int logicalPixels, int dpi) =>
        Math.Max(1, (int)Math.Round(logicalPixels * Math.Max(96, dpi) / 96D));

    internal static void Configure(Graphics graphics)
    {
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        graphics.CompositingMode = CompositingMode.SourceOver;
        graphics.CompositingQuality = CompositingQuality.HighQuality;
        graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
    }

    internal static Color OpaqueParent(Control control, Color fallback)
    {
        for (Control? current = control.Parent; current is not null; current = current.Parent)
        {
            if (!current.BackColor.IsEmpty && current.BackColor.A == byte.MaxValue)
                return current.BackColor;
        }

        return fallback.A == byte.MaxValue ? fallback : Background;
    }

    internal static GraphicsPath RoundedPath(Rectangle bounds, int logicalRadius, Control control)
        => RoundedPath(bounds, logicalRadius, control.DeviceDpi);

    internal static GraphicsPath RoundedPath(Rectangle bounds, int logicalRadius, int dpi)
    {
        var scale = Math.Max(96, dpi) / 96F;
        var inset = Math.Max(0.5F, scale * 0.5F);
        var rectangle = new RectangleF(
            bounds.Left + inset,
            bounds.Top + inset,
            // Match the shared product renderer: GDI+ treats the right/bottom
            // fill boundary differently, so reserving the final device pixel
            // is what makes the coverage classes mirror on all four sides.
            Math.Max(0F, bounds.Width - inset * 2F - 1F),
            Math.Max(0F, bounds.Height - inset * 2F - 1F));
        var radius = Math.Min(logicalRadius * scale, Math.Min(rectangle.Width, rectangle.Height) / 2F);
        return RoundedPathExact(rectangle, radius);
    }

    private static GraphicsPath RoundedPathExact(RectangleF rectangle, float radius)
    {
        var path = new GraphicsPath();
        if (rectangle.Width <= 0F || rectangle.Height <= 0F) return path;
        if (radius <= 1F)
        {
            path.AddRectangle(rectangle);
            return path;
        }

        var diameter = radius * 2F;
        path.AddArc(rectangle.Left, rectangle.Top, diameter, diameter, 180F, 90F);
        path.AddArc(rectangle.Right - diameter, rectangle.Top, diameter, diameter, 270F, 90F);
        path.AddArc(rectangle.Right - diameter, rectangle.Bottom - diameter, diameter, diameter, 0F, 90F);
        path.AddArc(rectangle.Left, rectangle.Bottom - diameter, diameter, diameter, 90F, 90F);
        path.CloseFigure();
        return path;
    }
}

/// <summary>
/// Owner-drawn action button.  The native Button contract remains intact for
/// keyboard/UIA/click handling, while UserPaint owns the complete visual.
/// </summary>
internal sealed class UpdaterButton : Control
{
    private bool _primary;
    private bool _hot;
    private bool _pressed;

    internal bool Primary
    {
        get => _primary;
        set
        {
            if (_primary == value) return;
            _primary = value;
            Invalidate();
        }
    }

    public UpdaterButton()
    {
        SetStyle(ControlStyles.UserPaint |
                 ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw, true);
        AccessibleRole = AccessibleRole.PushButton;
        AccessibleName = Text;
        TabStop = true;
        Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);
        Cursor = Cursors.Default;
    }

    protected override void OnMouseEnter(EventArgs e)
    {
        _hot = true;
        base.OnMouseEnter(e);
        Invalidate();
    }

    protected override void OnMouseLeave(EventArgs e)
    {
        _hot = false;
        if (!Capture) _pressed = false;
        base.OnMouseLeave(e);
        Invalidate();
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (Enabled && e.Button == MouseButtons.Left)
        {
            _pressed = true;
            Capture = true;
        }
        base.OnMouseDown(e);
        Invalidate();
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        _pressed = false;
        Capture = false;
        base.OnMouseUp(e);
        Invalidate();
    }

    protected override void OnEnabledChanged(EventArgs e)
    {
        base.OnEnabledChanged(e);
        Invalidate();
    }

    protected override void OnTextChanged(EventArgs e)
    {
        base.OnTextChanged(e);
        AccessibleName = Text;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var parent = UpdaterTheme.OpaqueParent(this, UpdaterTheme.Background);
        e.Graphics.Clear(parent);
        UpdaterTheme.Configure(e.Graphics);

        var bounds = Rectangle.Inflate(ClientRectangle, -1, -1);
        if (bounds.Width <= 0 || bounds.Height <= 0) return;

        var fill = !Enabled
            ? UpdaterTheme.DisabledBackground
            : (_pressed && _hot
                ? (_primary ? UpdaterTheme.AccentPressed : UpdaterTheme.SurfaceHover)
                : (_hot ? (_primary ? UpdaterTheme.AccentHover : UpdaterTheme.SurfaceHover)
                    : (_primary ? UpdaterTheme.Accent : UpdaterTheme.Surface)));
        var text = !Enabled ? UpdaterTheme.DisabledText : (_primary ? UpdaterTheme.OnAccent : UpdaterTheme.Text);
        var path = UpdaterTheme.RoundedPath(bounds, UpdaterTheme.ActionRadius, this);
        using (path)
        using (var brush = new SolidBrush(fill)) e.Graphics.FillPath(brush, path);

        if (Focused && ShowFocusCues && Enabled)
        {
            using var focusPath = UpdaterTheme.RoundedPath(Rectangle.Inflate(bounds, -2, -2), UpdaterTheme.ActionRadius - 2, this);
            using var focusPen = new Pen(_primary ? UpdaterTheme.FocusRing : UpdaterTheme.Accent, Math.Max(1F, DeviceDpi / 96F));
            e.Graphics.DrawPath(focusPen, focusPath);
        }

        var flags = TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter |
                    TextFormatFlags.NoPadding | TextFormatFlags.NoPrefix;
        TextRenderer.DrawText(e.Graphics, Text, Font, bounds, text, flags);
    }

    protected override bool IsInputKey(Keys keyData) =>
        keyData is Keys.Enter or Keys.Space || base.IsInputKey(keyData);

    protected override void OnKeyDown(KeyEventArgs e)
    {
        if (Enabled && e.KeyCode is Keys.Enter or Keys.Space)
        {
            OnClick(EventArgs.Empty);
            e.Handled = true;
            e.SuppressKeyPress = true;
        }
        base.OnKeyDown(e);
    }

    internal void PerformClick()
    {
        if (Enabled) OnClick(EventArgs.Empty);
    }
}

/// <summary>Owner-drawn progress bar with the same rounded surface language.</summary>
internal sealed class UpdaterProgressBar : Control
{
    private int _minimum;
    private int _maximum = 100;
    private int _value;
    private ProgressBarStyle _style = ProgressBarStyle.Continuous;

    public UpdaterProgressBar()
    {
        SetStyle(ControlStyles.UserPaint |
                 ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw, true);
        AccessibleRole = AccessibleRole.ProgressBar;
        BackColor = UpdaterTheme.Background;
        TabStop = false;
    }

    public int Minimum
    {
        get => _minimum;
        set
        {
            _minimum = Math.Min(value, _maximum - 1);
            Value = _value;
            Invalidate();
        }
    }

    public int Maximum
    {
        get => _maximum;
        set
        {
            _maximum = Math.Max(value, _minimum + 1);
            Value = _value;
            Invalidate();
        }
    }

    public int Value
    {
        get => _value;
        set
        {
            var next = Math.Clamp(value, _minimum, _maximum);
            if (_value == next) return;
            _value = next;
            Invalidate();
            AccessibilityNotifyClients(AccessibleEvents.ValueChange, -1);
        }
    }

    // Kept for source-level compatibility with the updater workflow.  The
    // owner-drawn control intentionally renders a deterministic continuous
    // bar; marquee was never used by the update pipeline.
    public ProgressBarStyle Style
    {
        get => _style;
        set
        {
            if (value == ProgressBarStyle.Marquee)
                throw new NotSupportedException("更新器进度条不支持不确定进度。");
            _style = value;
            Invalidate();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(UpdaterTheme.OpaqueParent(this, UpdaterTheme.Background));
        UpdaterTheme.Configure(e.Graphics);
        var edge = Math.Max(1, UpdaterTheme.Scale(this, 1));
        var bounds = Rectangle.Inflate(ClientRectangle, -edge, -edge);
        if (bounds.Width <= 0 || bounds.Height <= 0) return;

        using var path = UpdaterTheme.RoundedPath(bounds, 6, this);
        using (var track = new SolidBrush(UpdaterTheme.ProgressTrack)) e.Graphics.FillPath(track, path);
        var ratio = (_value - _minimum) / (double)Math.Max(1, _maximum - _minimum);
        var fillWidth = (int)Math.Round(bounds.Width * ratio);
        if (fillWidth > 0)
        {
            var state = e.Graphics.Save();
            e.Graphics.SetClip(path);
            using var fill = new SolidBrush(Enabled ? UpdaterTheme.Accent : UpdaterTheme.DisabledText);
            e.Graphics.FillRectangle(fill, bounds.X, bounds.Y, fillWidth, bounds.Height);
            e.Graphics.Restore(state);
        }

        using var border = new Pen(UpdaterTheme.Border, edge);
        e.Graphics.DrawPath(border, path);
    }
}

/// <summary>
/// Read-only owner-drawn update log.  It deliberately avoids the native edit
/// border and native scrollbar buttons that otherwise form a visual island in
/// the updater. Mouse wheel, keyboard scrolling and Ctrl+C remain available.
/// </summary>
internal sealed class UpdaterLogView : Control
{
    private int _scrollOffset;
    private int _contentHeight;

    public UpdaterLogView()
    {
        SetStyle(ControlStyles.UserPaint |
                 ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw |
                 ControlStyles.Opaque |
                 ControlStyles.Selectable, true);
        BackColor = UpdaterTheme.Surface;
        ForeColor = UpdaterTheme.Text;
        Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);
        AccessibleRole = AccessibleRole.StaticText;
        AccessibleName = "更新日志";
        TabStop = true;
    }

    internal void AppendText(string value)
    {
        if (string.IsNullOrEmpty(value)) return;
        Text += value;
        RecalculateContent(scrollToEnd: true);
        Invalidate();
    }

    protected override void OnTextChanged(EventArgs e)
    {
        base.OnTextChanged(e);
        AccessibleDescription = Text;
        RecalculateContent(scrollToEnd: true);
        Invalidate();
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        RecalculateContent(scrollToEnd: false);
    }

    protected override void OnMouseWheel(MouseEventArgs e)
    {
        var lines = Math.Max(1, SystemInformation.MouseWheelScrollLines);
        ScrollBy(-(e.Delta / Math.Max(1, SystemInformation.MouseWheelScrollDelta)) * Font.Height * lines);
        base.OnMouseWheel(e);
    }

    protected override bool IsInputKey(Keys keyData) =>
        keyData is Keys.Up or Keys.Down or Keys.PageUp or Keys.PageDown or Keys.Home or Keys.End ||
        base.IsInputKey(keyData);

    protected override void OnKeyDown(KeyEventArgs e)
    {
        var page = Math.Max(Font.Height, ClientSize.Height - Font.Height);
        switch (e.KeyCode)
        {
            case Keys.Up: ScrollBy(-Font.Height); break;
            case Keys.Down: ScrollBy(Font.Height); break;
            case Keys.PageUp: ScrollBy(-page); break;
            case Keys.PageDown: ScrollBy(page); break;
            case Keys.Home: SetScrollOffset(0); break;
            case Keys.End: SetScrollOffset(MaxScrollOffset); break;
            case Keys.C when e.Control:
                if (!string.IsNullOrEmpty(Text)) Clipboard.SetText(Text);
                break;
            default:
                base.OnKeyDown(e);
                return;
        }
        e.Handled = true;
        e.SuppressKeyPress = true;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(UpdaterTheme.Surface);
        var scrollbarWidth = MaxScrollOffset > 0 ? UpdaterTheme.Scale(this, 8) : 0;
        var textBounds = new Rectangle(0, -_scrollOffset,
            Math.Max(1, ClientSize.Width - scrollbarWidth - UpdaterTheme.Scale(this, 4)),
            Math.Max(ClientSize.Height + _scrollOffset, _contentHeight));
        TextRenderer.DrawText(e.Graphics, Text, Font, textBounds, ForeColor,
            TextFormatFlags.Left | TextFormatFlags.Top | TextFormatFlags.WordBreak |
            TextFormatFlags.NoPadding | TextFormatFlags.NoPrefix);

        if (scrollbarWidth <= 0) return;
        var track = new Rectangle(ClientSize.Width - scrollbarWidth, UpdaterTheme.Scale(this, 2),
            UpdaterTheme.Scale(this, 4), Math.Max(1, ClientSize.Height - UpdaterTheme.Scale(this, 4)));
        using (var trackPath = UpdaterTheme.RoundedPath(track, 2, this))
        using (var trackBrush = new SolidBrush(UpdaterTheme.ProgressTrack))
            e.Graphics.FillPath(trackBrush, trackPath);
        var thumbHeight = Math.Max(UpdaterTheme.Scale(this, 20),
            (int)Math.Round(track.Height * Math.Min(1D, ClientSize.Height / (double)Math.Max(1, _contentHeight))));
        var travel = Math.Max(0, track.Height - thumbHeight);
        var thumbTop = track.Top + (int)Math.Round(travel * (_scrollOffset / (double)Math.Max(1, MaxScrollOffset)));
        var thumb = new Rectangle(track.Left, thumbTop, track.Width, thumbHeight);
        using var thumbPath = UpdaterTheme.RoundedPath(thumb, 2, this);
        using var thumbBrush = new SolidBrush(UpdaterTheme.Accent);
        e.Graphics.FillPath(thumbBrush, thumbPath);
    }

    private int MaxScrollOffset => Math.Max(0, _contentHeight - ClientSize.Height);

    private void RecalculateContent(bool scrollToEnd)
    {
        var width = Math.Max(1, ClientSize.Width - UpdaterTheme.Scale(this, 12));
        _contentHeight = string.IsNullOrEmpty(Text) ? 0 : TextRenderer.MeasureText(Text, Font,
            new Size(width, int.MaxValue), TextFormatFlags.Left | TextFormatFlags.Top |
            TextFormatFlags.WordBreak | TextFormatFlags.NoPadding | TextFormatFlags.NoPrefix).Height;
        SetScrollOffset(scrollToEnd ? MaxScrollOffset : Math.Min(_scrollOffset, MaxScrollOffset));
    }

    private void ScrollBy(int delta) => SetScrollOffset(_scrollOffset + delta);

    private void SetScrollOffset(int value)
    {
        var next = Math.Clamp(value, 0, MaxScrollOffset);
        if (_scrollOffset == next) return;
        _scrollOffset = next;
        Invalidate();
    }
}

internal sealed class UpdaterSurfaceHost : Panel
{
    internal UpdaterSurfaceHost(Control child)
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw |
                 ControlStyles.UserPaint, true);
        var horizontalPadding = UpdaterTheme.Scale(this, 10);
        var verticalPadding = UpdaterTheme.Scale(this, 8);
        Padding = new Padding(horizontalPadding, verticalPadding, horizontalPadding, verticalPadding);
        BackColor = UpdaterTheme.Background;
        child.Enter += ChildFocusChanged;
        child.Leave += ChildFocusChanged;
        child.Dock = DockStyle.Fill;
        child.Margin = Padding.Empty;
        Controls.Add(child);
    }

    protected override void OnDpiChangedAfterParent(EventArgs e)
    {
        var horizontalPadding = UpdaterTheme.Scale(this, 10);
        var verticalPadding = UpdaterTheme.Scale(this, 8);
        Padding = new Padding(horizontalPadding, verticalPadding, horizontalPadding, verticalPadding);
        base.OnDpiChangedAfterParent(e);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        e.Graphics.Clear(UpdaterTheme.OpaqueParent(this, UpdaterTheme.Background));
        UpdaterTheme.Configure(e.Graphics);
        var bounds = Rectangle.Inflate(ClientRectangle, -1, -1);
        using var path = UpdaterTheme.RoundedPath(bounds, UpdaterTheme.InputRadius, this);
        using (var fill = new SolidBrush(UpdaterTheme.Surface)) e.Graphics.FillPath(fill, path);
        var focused = Controls.Count > 0 && Controls[0].Focused;
        using var border = new Pen(focused ? UpdaterTheme.Accent : UpdaterTheme.Border,
            Math.Max(1, UpdaterTheme.Scale(this, 1)));
        e.Graphics.DrawPath(border, path);
    }

    private void ChildFocusChanged(object? sender, EventArgs e) => Invalidate();
}

internal sealed class UpdaterErrorDialog : Form
{
    public UpdaterErrorDialog(string message)
    {
        Text = "生态更新器无法启动";
        AccessibleName = Text;
        Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);
        AutoScaleMode = AutoScaleMode.Dpi;
        AutoScaleDimensions = new SizeF(96F, 96F);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        ControlBox = false;
        MaximizeBox = false;
        MinimizeBox = false;
        ShowInTaskbar = false;
        KeyPreview = true;
        ClientSize = new Size(520, 230);
        MinimumSize = Size;
        MaximumSize = Size;
        BackColor = UpdaterTheme.Background;

        var title = new Label
        {
            AutoSize = true,
            Location = new Point(28, 24),
            Text = "更新器无法启动",
            Font = new Font("Microsoft YaHei UI", 15F, FontStyle.Bold, GraphicsUnit.Point),
            ForeColor = UpdaterTheme.Error,
        };
        var detail = new Label
        {
            AutoEllipsis = true,
            Location = new Point(28, 72),
            Size = new Size(464, 96),
            Text = string.IsNullOrWhiteSpace(message) ? "发生未知错误。" : message.Trim(),
            ForeColor = UpdaterTheme.Text,
            AccessibleName = "错误详情",
        };
        var close = new UpdaterButton
        {
            Text = "关闭",
            Primary = true,
            Location = new Point(394, 180),
            Size = new Size(98, 38),
        };
        close.Click += (_, _) => Close();
        Controls.AddRange([title, detail, close]);
        ActiveControl = close;
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        if (keyData == Keys.Escape)
        {
            Close();
            return true;
        }
        return base.ProcessCmdKey(ref msg, keyData);
    }
}
