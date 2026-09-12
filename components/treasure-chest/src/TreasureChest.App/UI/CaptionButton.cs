namespace TreasureChest.UI;

internal sealed class CaptionButton : Control, IExplicitAnimationPaintSource
{
    private bool _hovered;
    private bool _pressed;
    private CaptionButtonKind _kind;

    public CaptionButton(CaptionButtonKind kind, string accessibleName)
    {
        _kind = kind;
        AccessibleName = accessibleName;
        AccessibleRole = AccessibleRole.PushButton;
        Dock = DockStyle.Right;
        Size = new Size(50, 50);
        Margin = Padding.Empty;
        Cursor = Cursors.Hand;
        TabStop = false;
        // Own the click once. Control's StandardClick pipeline otherwise emits
        // a second Click before this control's mouse-up handler.
        SetStyle(ControlStyles.StandardClick | ControlStyles.StandardDoubleClick, false);
        SetStyle(ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw |
                 ControlStyles.UserPaint, true);
    }

    public CaptionButtonKind Kind
    {
        get => _kind;
        set
        {
            if (_kind == value) return;
            _kind = value;
            Invalidate();
            AccessibilityNotifyClients(AccessibleEvents.NameChange, -1);
        }
    }

    protected override void OnMouseEnter(EventArgs e) { _hovered = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { _hovered = false; _pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left) { _pressed = true; Capture = true; Invalidate(); }
        base.OnMouseDown(e);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        var invoke = _pressed && e.Button == MouseButtons.Left && ClientRectangle.Contains(e.Location);
        _pressed = false;
        Capture = false;
        Invalidate();
        base.OnMouseUp(e);
        if (invoke) OnClick(EventArgs.Empty);
    }

    protected override void OnMouseCaptureChanged(EventArgs e)
    {
        if (!Capture) _pressed = false;
        Invalidate();
        base.OnMouseCaptureChanged(e);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        var normal = UiTheme.TitleBar;
        var hover = Kind == CaptionButtonKind.Close ? UiTheme.CloseHover : UiTheme.NavigationHover;
        var pressed = Kind == CaptionButtonKind.Close ? UiTheme.ClosePressed : UiTheme.NavigationPressed;
        e.Graphics.Clear(_pressed ? pressed : _hovered ? hover : normal);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        ThemePaint.Configure(e.Graphics);
        var iconSize = DpiLayout.Scale(20, DeviceDpi);
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyphMap.From(Kind),
            new RectangleF((Width - iconSize) / 2F, (Height - iconSize) / 2F, iconSize, iconSize),
            UiTheme.NavigationText);
        AnimationRunner.ReportPaint(this);
    }
}
