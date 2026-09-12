namespace TreasureChest.UI;

public static class WindowChromePresentation
{
    internal static void MinimizeToTaskbar(Form window)
    {
        window.ShowInTaskbar = true;
        window.WindowState = FormWindowState.Minimized;
    }

    public static int DwmCornerPreference(FormWindowState state) =>
        state == FormWindowState.Maximized ? 1 : 2;

    public static int CornerRadius(int dpi, FormWindowState state) =>
        state == FormWindowState.Maximized ? 0 : DpiLayout.Scale(CornerRadiusTokens.Window, dpi);

    public static FormWindowState ToggleMaximize(FormWindowState current) =>
        current == FormWindowState.Maximized ? FormWindowState.Normal : FormWindowState.Maximized;

    public static CaptionButtonKind MaximizeKind(FormWindowState current) =>
        current == FormWindowState.Maximized ? CaptionButtonKind.Restore : CaptionButtonKind.Maximize;

    public static string MaximizeAccessibleName(FormWindowState current) =>
        current == FormWindowState.Maximized ? "还原窗口" : "最大化窗口";

    public static bool ShouldConstrainToWorkingArea(FormWindowState state) =>
        state == FormWindowState.Normal;

    public static Rectangle ResizeGripBounds(Rectangle clientRectangle, int dpi, FormWindowState state)
    {
        if (state != FormWindowState.Normal || clientRectangle.Width <= 0 || clientRectangle.Height <= 0)
            return Rectangle.Empty;
        var side = DpiLayout.Metrics(dpi).ResizeGripSize;
        return new Rectangle(
            Math.Max(clientRectangle.Left, clientRectangle.Right - side),
            Math.Max(clientRectangle.Top, clientRectangle.Bottom - side),
            Math.Min(side, clientRectangle.Width),
            Math.Min(side, clientRectangle.Height));
    }
}

internal sealed class ResizeGripOverlay : Control, IDpiLayoutExcluded, IExplicitAnimationPaintSource
{
    private int _visualDpi = DpiLayout.BaselineDpi;

    public ResizeGripOverlay()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.SupportsTransparentBackColor, true);
        AccessibleName = "拖拽调整窗口大小";
        AccessibleRole = AccessibleRole.Grip;
        Cursor = Cursors.SizeNWSE;
        TabStop = false;
        BackColor = Color.Transparent;
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public void Place(Rectangle clientRectangle, int dpi, FormWindowState state)
    {
        _visualDpi = DpiLayout.NormalizeDpi(dpi);
        var target = WindowChromePresentation.ResizeGripBounds(clientRectangle, _visualDpi, state);
        Visible = !target.IsEmpty;
        if (!target.IsEmpty && Bounds != target) Bounds = target;
        Invalidate();
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        // The chrome marker deliberately has no independent surface. Clearing to
        // the current parent material keeps it unobtrusive without a square tile.
        e.Graphics.Clear(Parent?.BackColor ?? UiTheme.Background);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var inset = Math.Max(1, DpiLayout.Scale(1, _visualDpi));
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.ResizeGrip,
            RectangleF.Inflate(ClientRectangle, -inset, -inset), UiTheme.Divider);
        AnimationRunner.ReportPaint(this);
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left && FindForm() is Form form && form.WindowState == FormWindowState.Normal)
        {
            NativeMethods.ReleaseCapture();
            NativeMethods.SendMessage(form.Handle, NativeMethods.WmNcLButtonDown,
                (IntPtr)NativeMethods.HtBottomRight,
                NativeMethods.LParamFromScreenPoint(PointToScreen(e.Location)));
        }
        base.OnMouseDown(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    private void ThemeFrameChanged(object? sender, EventArgs e) => Invalidate();
}
