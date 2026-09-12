using System.Drawing.Drawing2D;

namespace TreasureChest.UI;

/// <summary>
/// A small owner-drawn progress control so progress surfaces follow the product
/// theme instead of inheriting the operating-system blue accent.
/// </summary>
public sealed class ThemedProgressBar : Control, IExplicitAnimationPaintSource
{
    private int _minimum;
    private int _maximum = 100;
    private int _value;
    private int _marqueeOffset;
    private ProgressBarStyle _style = ProgressBarStyle.Continuous;

    public ThemedProgressBar()
    {
        AccessibleRole = AccessibleRole.ProgressBar;
        SetStyle(ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw |
                 ControlStyles.UserPaint, true);
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

    public ProgressBarStyle Style
    {
        get => _style;
        set
        {
            if (_style == value) return;
            _style = value;
            UpdateAnimation();
            Invalidate();
        }
    }

    protected override void OnVisibleChanged(EventArgs e)
    {
        base.OnVisibleChanged(e);
        UpdateAnimation();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var border = Math.Max(1, DpiLayout.Scale(1, DeviceDpi));
        var bounds = Rectangle.Inflate(ClientRectangle, -border, -border);
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        var radius = Math.Min(bounds.Height / 2, DpiLayout.Scale(5, DeviceDpi));
        using var trackPath = RoundedRectangle(bounds, radius);
        using var trackBrush = new SolidBrush(UiTheme.ProgressTrack);
        e.Graphics.FillPath(trackBrush, trackPath);

        var fill = FillRectangle(bounds);
        if (fill.Width > 0)
        {
            var state = e.Graphics.Save();
            e.Graphics.SetClip(trackPath);
            using var fillBrush = new SolidBrush(Enabled ? UiTheme.Accent : UiTheme.DisabledText);
            e.Graphics.FillRectangle(fillBrush, fill);
            e.Graphics.Restore(state);
        }

        using var borderPen = new Pen(UiTheme.Border, border);
        e.Graphics.DrawPath(borderPen, trackPath);
        AnimationRunner.ReportPaint(this);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) AnimationRunner.Cancel(this);
        base.Dispose(disposing);
    }

    private Rectangle FillRectangle(Rectangle bounds)
    {
        if (_style == ProgressBarStyle.Marquee)
        {
            var blockWidth = Math.Max(DpiLayout.Scale(36, DeviceDpi), bounds.Width / 4);
            var travel = bounds.Width + blockWidth;
            var left = bounds.Left + (_marqueeOffset % travel) - blockWidth;
            return new Rectangle(left, bounds.Top, blockWidth, bounds.Height);
        }

        var ratio = (_value - _minimum) / (double)Math.Max(1, _maximum - _minimum);
        return new Rectangle(bounds.X, bounds.Y, (int)Math.Round(bounds.Width * ratio), bounds.Height);
    }

    private void UpdateAnimation()
    {
        AnimationRunner.Cancel(this, "marquee");
        if (Visible && _style == ProgressBarStyle.Marquee && !DesignMode) StartMarqueeCycle();
    }

    private void StartMarqueeCycle()
    {
        if (IsDisposed || !Visible || _style != ProgressBarStyle.Marquee) return;
        var travel = Math.Max(1, Width * 2);
        AnimationRunner.Start(this, "marquee", AnimationTokens.ComplexDurationMs, progress =>
        {
            _marqueeOffset = (int)Math.Round(travel * progress);
            Invalidate();
        }, StartMarqueeCycle);
    }

    private static GraphicsPath RoundedRectangle(Rectangle bounds, int radius)
    {
        var path = new GraphicsPath();
        if (radius <= 1)
        {
            path.AddRectangle(bounds);
            return path;
        }

        var diameter = radius * 2;
        var arc = new Rectangle(bounds.X, bounds.Y, diameter, diameter);
        path.AddArc(arc, 180, 90);
        arc.X = bounds.Right - diameter;
        path.AddArc(arc, 270, 90);
        arc.Y = bounds.Bottom - diameter;
        path.AddArc(arc, 0, 90);
        arc.X = bounds.Left;
        path.AddArc(arc, 90, 90);
        path.CloseFigure();
        return path;
    }
}
