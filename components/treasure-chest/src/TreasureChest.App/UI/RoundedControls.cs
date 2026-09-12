using System.Drawing.Drawing2D;
using TreasureChest.Services;

namespace TreasureChest.UI;

public enum ButtonGlyph
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
    Close,
}

public sealed class RoundedButton : Button, IExplicitAnimationPaintSource
{
    private bool _hovered;
    private bool _pressed;

    public int CornerRadiusLogical { get; set; } = CornerRadiusTokens.ActionButton;
    public ButtonGlyph Glyph { get; set; }
    public int GlyphSizeLogical { get; set; } = 16;
    public int VisualDpiOverride { get; set; }
    private int VisualDpi => VisualDpiOverride > 0 ? DpiLayout.NormalizeDpi(VisualDpiOverride) : DeviceDpi;

    public RoundedButton()
    {
        FlatStyle = FlatStyle.Flat;
        UseVisualStyleBackColor = false;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Opaque, true);
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
        _pressed = false;
        Capture = false;
        Invalidate();
        base.OnMouseUp(e);
    }

    protected override void OnMouseCaptureChanged(EventArgs e)
    {
        if (!Capture) _pressed = false;
        Invalidate();
        base.OnMouseCaptureChanged(e);
    }

    protected override void OnEnabledChanged(EventArgs e)
    {
        _pressed = false;
        _hovered = false;
        base.OnEnabledChanged(e);
        Invalidate();
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        RemoveBinaryRegion();
    }

    private void RemoveBinaryRegion()
    {
        var old = Region;
        Region = null;
        old?.Dispose();
    }

    protected override void OnPaintBackground(PaintEventArgs pevent)
    {
        // The complete button, including its antialiased corner coverage, is
        // composed once in OnPaint.  A separate background pass can expose a
        // stale square between the parent and the rounded fill.
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        ThemePaint.Configure(e.Graphics);
        e.Graphics.Clear(ThemePaint.ResolveOpaqueBackground(this, UiTheme.Palette.Background));
        var background = !Enabled
            ? UiTheme.DisabledBackground
            : _pressed ? FlatAppearance.MouseDownBackColor
            : _hovered ? FlatAppearance.MouseOverBackColor
            : BackColor;
        if (background == Color.Empty) background = BackColor;
        using var path = ThemePaint.RoundedPath(ClientRectangle, DpiLayout.Scale(CornerRadiusLogical, VisualDpi));
        using (var fill = new SolidBrush(background)) e.Graphics.FillPath(fill, path);
        if (FlatAppearance.BorderSize > 0)
        {
            using var border = new Pen(FlatAppearance.BorderColor, Math.Max(1F, FlatAppearance.BorderSize));
            e.Graphics.DrawPath(border, path);
        }

        var foreground = Enabled ? ForeColor : UiTheme.DisabledText;
        var textSize = NativeButtonText.Measure(e.Graphics, Text, Font);
        var iconSize = Glyph == ButtonGlyph.None ? 0 : DpiLayout.Scale(GlyphSizeLogical, VisualDpi);
        var gap = iconSize == 0 || string.IsNullOrEmpty(Text) ? 0 : DpiLayout.Scale(8, VisualDpi);
        var contentWidth = iconSize + gap + textSize.Width;
        var left = Math.Max(Padding.Left, (Width - contentWidth) / 2);
        if (iconSize > 0)
        {
            var bounds = new RectangleF(left, (Height - iconSize) / 2F, iconSize, iconSize);
            ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyphMap.From(Glyph), bounds, foreground);
            left += iconSize + gap;
        }
        var textBounds = new Rectangle(left, 0, Math.Max(1, Width - left - Padding.Right), Height);
        NativeButtonText.Draw(e.Graphics, Text, Font, textBounds, foreground,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine |
            TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
        if (Focused && ShowFocusCues)
            ThemePaint.DrawFocusRing(e.Graphics, ClientRectangle,
                DpiLayout.Scale(CornerRadiusLogical, VisualDpi), VisualDpi,
                UiTheme.ContrastRatio(UiTheme.Accent, background) >= 3D ? UiTheme.Accent : UiTheme.OnAccent);
        AnimationRunner.ReportPaint(this);
    }

    public override Size GetPreferredSize(Size proposedSize)
    {
        using var measurement = new Bitmap(1, 1);
        measurement.SetResolution(VisualDpi, VisualDpi);
        using var graphics = Graphics.FromImage(measurement);
        var textSize = NativeButtonText.Measure(graphics, Text, Font);
        var iconSize = Glyph == ButtonGlyph.None ? 0 : DpiLayout.Scale(GlyphSizeLogical, VisualDpi);
        var gap = iconSize == 0 || string.IsNullOrEmpty(Text) ? 0 : DpiLayout.Scale(8, VisualDpi);
        var width = Padding.Horizontal + iconSize + gap + textSize.Width + DpiLayout.Scale(4, VisualDpi);
        var height = Padding.Vertical + Math.Max(textSize.Height, iconSize) + DpiLayout.Scale(8, VisualDpi);
        return new Size(Math.Max(MinimumSize.Width, width), Math.Max(MinimumSize.Height, height));
    }

    internal static GraphicsPath RoundedPath(Rectangle bounds, int radius)
        => ThemePaint.RoundedPath(bounds, radius);
}

internal interface IThemedEmbeddedEditor
{
}

internal sealed class ThemedEmbeddedTextBox : TextBox, IThemedEmbeddedEditor
{
    private IntPtr _surfaceBrush;
    private uint _surfaceBrushColor;

    public ThemedEmbeddedTextBox()
    {
        BorderStyle = BorderStyle.None;
        AutoSize = false;
        BackColor = UiTheme.Surface;
        ForeColor = UiTheme.Text;
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    protected override CreateParams CreateParams
    {
        get
        {
            var parameters = base.CreateParams;
            // The editor is embedded in a rounded, owner-painted host. Keep the
            // native EDIT window client-only so an uninitialized non-client edge
            // cannot survive at the right edge of the host on Windows dark mode.
            parameters.Style &= ~NativeMethods.WsBorder;
            parameters.ExStyle &= ~NativeMethods.WsExClientEdge;
            return parameters;
        }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        BorderStyle = BorderStyle.None;
        // The rounded host is the only visible field.  Keep the native EDIT
        // solely for input/IME/UIA and prevent Windows theme painters from
        // re-introducing a dark closed-field surface after the host renders.
        NativeMethods.SetWindowTheme(Handle, string.Empty, string.Empty);
        ApplyNativeSurface();
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg is NativeMethods.WmReflect + NativeMethods.WmCtlColorEdit or
            NativeMethods.WmReflect + NativeMethods.WmCtlColorStatic or
            NativeMethods.WmReflect + NativeMethods.WmCtlColorListBox)
        {
            var background = Enabled ? UiTheme.Surface : UiTheme.DisabledBackground;
            var foreground = Enabled ? UiTheme.Text : UiTheme.DisabledText;
            var color = NativeMethods.ColorRefValue(background);
            if (m.WParam != IntPtr.Zero)
            {
                NativeMethods.SetBkColor(m.WParam, color);
                NativeMethods.SetTextColor(m.WParam, NativeMethods.ColorRefValue(foreground));
            }
            m.Result = GetSurfaceBrush(color);
            return;
        }
        // The native HWND is clipped to an empty region by ToolbarItemHost.
        // Never bypass that clip by painting directly to a supplied HDC or a
        // window DC: real WGC captures showed those pixels as a black field.
        if (m.Msg is NativeMethods.WmPrint or NativeMethods.WmPrintClient)
        {
            m.Result = new IntPtr(1);
            return;
        }
        if (m.Msg == NativeMethods.WmNcPaint)
        {
            // The rounded toolbar host owns the only border. A native dark-mode
            // TextBox non-client pass can otherwise leave a stable black tail at
            // the editor's right edge even with BorderStyle.None.
            m.Result = new IntPtr(1);
            return;
        }
        if (m.Msg == NativeMethods.WmEraseBackground)
        {
            if (m.WParam != IntPtr.Zero)
            {
                using var graphics = Graphics.FromHdc(m.WParam);
                graphics.Clear(Enabled ? UiTheme.Surface : UiTheme.DisabledBackground);
            }
            m.Result = new IntPtr(1);
            return;
        }
        base.WndProc(ref m);
    }

    // The project-monitor toolbar keeps this native EDIT in an off-screen
    // behavior layer so it can retain IME/selection/UIA without contributing
    // pixels to WGC. Its visible managed host forwards the semantic activation
    // here; otherwise Click handlers on the hidden behavior never run.
    internal void InvokeManagedClick() => OnClick(EventArgs.Empty);

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
            if (_surfaceBrush != IntPtr.Zero)
            {
                NativeMethods.DeleteObject(_surfaceBrush);
                _surfaceBrush = IntPtr.Zero;
            }
        }
        base.Dispose(disposing);
    }

    private void ApplyNativeSurface()
    {
        if (IsDisposed || !IsHandleCreated) return;
        // This EDIT is still a real native control for text input, caret, IME and
        // accessibility, but it must not use the DarkMode_CFD visual painter.
        // That painter owns a right-hand fill which is outside the rounded host's
        // surface and is the source of the black tail seen in WGC captures.
        NativeMethods.SendMessage(
            Handle,
            NativeMethods.EmSetBackgroundColor,
            IntPtr.Zero,
            NativeMethods.ColorRef(Enabled ? UiTheme.Surface : UiTheme.DisabledBackground));
    }

    private void ThemeFrameChanged(object? sender, EventArgs e)
    {
        ApplyNativeSurface();
        BackColor = UiTheme.Surface;
        ForeColor = Enabled ? UiTheme.Text : UiTheme.DisabledText;
        Invalidate();
    }

    private IntPtr GetSurfaceBrush(uint color)
    {
        if (_surfaceBrush != IntPtr.Zero && _surfaceBrushColor == color) return _surfaceBrush;
        var replacement = NativeMethods.CreateSolidBrush(color);
        if (replacement == IntPtr.Zero) return IntPtr.Zero;
        var previous = _surfaceBrush;
        _surfaceBrush = replacement;
        _surfaceBrushColor = color;
        if (previous != IntPtr.Zero) NativeMethods.DeleteObject(previous);
        return replacement;
    }

    internal void DrawVisual(Graphics graphics, Rectangle bounds, bool? focusedOverride = null)
    {
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        var background = Enabled ? UiTheme.Surface : UiTheme.DisabledBackground;
        using (var fill = new SolidBrush(background)) graphics.FillRectangle(fill, bounds);
        var placeholder = TextLength == 0;
        var focused = focusedOverride ?? Focused;
        var display = placeholder ? PlaceholderText : Text;
        if (string.IsNullOrEmpty(display)) return;
        var color = !Enabled ? UiTheme.DisabledText : placeholder ? UiTheme.Muted : UiTheme.Text;
        var flags = TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding |
                    TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine;
        var saved = graphics.Save();
        try
        {
            graphics.SetClip(bounds, CombineMode.Intersect);
            if (placeholder || !focused)
            {
                ThemePaint.DrawText(graphics, display, Font, bounds, color, flags);
                return;
            }
            var selectionStart = Math.Clamp(SelectionStart, 0, TextLength);
            var selectionEnd = Math.Clamp(selectionStart + SelectionLength, selectionStart, TextLength);
            var prefixWidth = NativeButtonText.Measure(graphics, Text[..selectionStart], Font).Width;
            var endWidth = NativeButtonText.Measure(graphics, Text[..selectionEnd], Font).Width;
            var fullWidth = NativeButtonText.Measure(graphics, Text, Font).Width;
            var scroll = Math.Max(0, endWidth - bounds.Width + 2);
            var textBounds = new Rectangle(bounds.Left - scroll, bounds.Top,
                Math.Max(bounds.Width + scroll, fullWidth + 2), bounds.Height);
            flags &= ~TextFormatFlags.EndEllipsis;
            ThemePaint.DrawText(graphics, Text, Font, textBounds, color, flags);
            if (selectionEnd > selectionStart)
            {
                var selectionBounds = Rectangle.Intersect(bounds, new Rectangle(
                    textBounds.Left + prefixWidth, bounds.Top, Math.Max(1, endWidth - prefixWidth), bounds.Height));
                if (!selectionBounds.IsEmpty)
                {
                    using var selection = new SolidBrush(UiTheme.AccentSoftStrong);
                    graphics.FillRectangle(selection, selectionBounds);
                    graphics.SetClip(selectionBounds, CombineMode.Intersect);
                    // Repaint the complete run at the same origin. Splitting it
                    // into three strings changes glyph positioning at boundaries.
                    ThemePaint.DrawText(graphics, Text, Font, textBounds, UiTheme.Text, flags);
                }
            }
            else
            {
                var x = Math.Clamp(textBounds.Left + prefixWidth, bounds.Left, bounds.Right - 1);
                var inset = Math.Max(2, (bounds.Height - NativeButtonText.Measure(graphics, "中", Font).Height) / 2);
                using var caret = new Pen(UiTheme.Text, 1F);
                graphics.DrawLine(caret, x, bounds.Top + inset, x, bounds.Bottom - inset);
            }
        }
        finally { graphics.Restore(saved); }
    }
}

public sealed class ThemedInputHost : Panel, IExplicitAnimationPaintSource
{
    private readonly TextBoxBase _editor;
    private IntPtr _surfaceBrush;
    private uint _surfaceBrushColor;

    public ThemedInputHost(TextBoxBase editor)
    {
        ArgumentNullException.ThrowIfNull(editor);
        _editor = editor;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint, true);
        BackColor = UiTheme.Surface;
        Padding = new Padding(10, editor.Multiline ? 8 : 9, 10, editor.Multiline ? 8 : 7);
        TabStop = false;
        editor.BorderStyle = BorderStyle.None;
        editor.Dock = DockStyle.Fill;
        editor.Margin = Padding.Empty;
        editor.BackColor = UiTheme.Surface;
        editor.ForeColor = UiTheme.Text;
        editor.GotFocus += EditorFocusChanged;
        editor.LostFocus += EditorFocusChanged;
        Controls.Add(editor);
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public TextBoxBase Editor => _editor;

    public static ThemedInputHost Wrap(TextBoxBase editor)
    {
        var host = new ThemedInputHost(editor)
        {
            Dock = editor.Dock,
            Margin = editor.Margin,
            Size = editor.Size,
            MinimumSize = editor.MinimumSize,
            MaximumSize = editor.MaximumSize,
        };
        return host;
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        ThemePaint.Configure(e.Graphics);
        e.Graphics.Clear(ThemePaint.ResolveOpaqueBackground(this, UiTheme.Palette.Background));
        using var path = ThemePaint.RoundedPath(ClientRectangle, DpiLayout.Scale(CornerRadiusTokens.Input, DeviceDpi));
        using var fill = new SolidBrush(Enabled ? UiTheme.Surface : UiTheme.DisabledBackground);
        e.Graphics.FillPath(fill, path);
        using var border = new Pen(_editor.Focused ? UiTheme.Accent : UiTheme.Border, 1F);
        e.Graphics.DrawPath(border, path);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        AnimationRunner.ReportPaint(this);
    }

    protected override void WndProc(ref Message m)
    {
        if ((m.Msg is NativeMethods.WmCtlColorEdit or NativeMethods.WmCtlColorStatic) &&
            _editor.IsHandleCreated && m.LParam == _editor.Handle)
        {
            var background = Enabled ? UiTheme.Surface : UiTheme.DisabledBackground;
            var foreground = Enabled ? UiTheme.Text : UiTheme.DisabledText;
            var color = NativeMethods.ColorRefValue(background);
            if (m.WParam != IntPtr.Zero)
            {
                NativeMethods.SetBkColor(m.WParam, color);
                NativeMethods.SetTextColor(m.WParam, NativeMethods.ColorRefValue(foreground));
            }
            m.Result = GetSurfaceBrush(color);
            return;
        }
        base.WndProc(ref m);
    }

    protected override void OnEnabledChanged(EventArgs e)
    {
        _editor.Enabled = Enabled;
        Invalidate();
        base.OnEnabledChanged(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
            _editor.GotFocus -= EditorFocusChanged;
            _editor.LostFocus -= EditorFocusChanged;
            if (_surfaceBrush != IntPtr.Zero)
            {
                NativeMethods.DeleteObject(_surfaceBrush);
                _surfaceBrush = IntPtr.Zero;
            }
        }
        base.Dispose(disposing);
    }

    private void EditorFocusChanged(object? sender, EventArgs e) => Invalidate();

    private void ThemeFrameChanged(object? sender, EventArgs e)
    {
        BackColor = UiTheme.Surface;
        _editor.BorderStyle = BorderStyle.None;
        _editor.BackColor = UiTheme.Surface;
        _editor.ForeColor = Enabled ? UiTheme.Text : UiTheme.DisabledText;
        Invalidate(true);
    }

    private IntPtr GetSurfaceBrush(uint color)
    {
        if (_surfaceBrush != IntPtr.Zero && _surfaceBrushColor == color) return _surfaceBrush;
        var replacement = NativeMethods.CreateSolidBrush(color);
        if (replacement == IntPtr.Zero) return IntPtr.Zero;
        var previous = _surfaceBrush;
        _surfaceBrush = replacement;
        _surfaceBrushColor = color;
        if (previous != IntPtr.Zero) NativeMethods.DeleteObject(previous);
        return replacement;
    }
}

public sealed class PillCheckBox : Control, IExplicitAnimationPaintSource
{
    private bool _hovered;
    private bool _pressed;
    private bool _checked;
    public int VisualDpiOverride { get; set; }
    private int VisualDpi => VisualDpiOverride > 0 ? DpiLayout.NormalizeDpi(VisualDpiOverride) : DeviceDpi;
    internal Rectangle TextContentBounds => CalculateTextContentBounds(ClientRectangle);

    public PillCheckBox()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Selectable |
                 ControlStyles.Opaque, true);
        AccessibleRole = AccessibleRole.CheckButton;
        TabStop = true;
        Cursor = Cursors.Hand;
        AutoSize = false;
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public event EventHandler? CheckedChanged;

    public bool Checked
    {
        get => _checked;
        set
        {
            if (_checked == value) return;
            _checked = value;
            AccessibleName = $"{Text}：{(_checked ? "已选中" : "未选中")}";
            AccessibilityNotifyClients(AccessibleEvents.StateChange, -1);
            Invalidate();
            CheckedChanged?.Invoke(this, EventArgs.Empty);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(UiTheme.Palette.Surface);
        DrawVisual(e.Graphics, ClientRectangle, _hovered, _pressed, Focused && ShowFocusCues, VisualDpi);
        AnimationRunner.ReportPaint(this);
    }

    internal void DrawVisual(Graphics graphics, Rectangle bounds, bool hovered, bool pressed, bool focused, int visualDpi)
    {
        visualDpi = DpiLayout.NormalizeDpi(visualDpi);
        ThemePaint.Configure(graphics);
        // DrawVisual is also called by MonitorToolbarPanel on a different
        // control's Graphics.  Clear the complete destination here so stale
        // accent/focus pixels can never survive outside the rounded path.
        using (var surface = new SolidBrush(UiTheme.Surface)) graphics.FillRectangle(surface, bounds);
        var chromeInset = Math.Max(1, DpiLayout.Scale(1, visualDpi));
        var chromeBounds = Rectangle.Inflate(bounds, -chromeInset, -chromeInset);
        using var path = ThemePaint.RoundedPath(chromeBounds,
            Math.Max(1, DpiLayout.Scale(CornerRadiusTokens.Input, visualDpi) - chromeInset));
        var background = !Enabled
            ? UiTheme.DisabledBackground
            : pressed ? UiTheme.AccentSoftStrong
            : hovered ? UiTheme.SurfaceHover
            : UiTheme.SurfaceAlt;
        using (var fill = new SolidBrush(background)) graphics.FillPath(fill, path);
        using (var border = new Pen(UiTheme.Border, Math.Max(1F, visualDpi / 96F))) graphics.DrawPath(border, path);
        var side = DpiLayout.Scale(15, visualDpi);
        var box = new Rectangle(bounds.Left + DpiLayout.Scale(10, visualDpi),
            bounds.Top + (bounds.Height - side) / 2, side, side);
        using var boxPath = ThemePaint.RoundedPath(box, DpiLayout.Scale(4, visualDpi));
        using (var boxFill = new SolidBrush(Checked ? UiTheme.Accent : UiTheme.Surface)) graphics.FillPath(boxFill, boxPath);
        using (var boxBorder = new Pen(Checked ? UiTheme.Accent : UiTheme.Border, 1F)) graphics.DrawPath(boxBorder, boxPath);
        if (Checked)
            ThemeGlyphRenderer.Draw(graphics, ThemeGlyph.Check, RectangleF.Inflate(box, -1F, -1F), UiTheme.OnAccent);
        var textBounds = CalculateTextContentBounds(bounds, visualDpi);
        ThemePaint.DrawText(graphics, Text, Font, textBounds, Enabled ? UiTheme.Text : UiTheme.DisabledText,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding | TextFormatFlags.EndEllipsis);
        if (focused)
            ThemePaint.DrawFocusRing(graphics, chromeBounds,
                Math.Max(1, DpiLayout.Scale(CornerRadiusTokens.Input, visualDpi) - chromeInset),
                visualDpi, UiTheme.Accent);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        // OnPaint composes the complete opaque pill from one immutable palette
        // frame. A separate native background pass can expose a stale black or
        // accent strip at the host boundary while themes are transitioning.
    }

    public override Size GetPreferredSize(Size proposedSize)
    {
        var text = NativeButtonText.MeasureAtDpi(Text, Font, VisualDpi);
        var side = DpiLayout.Scale(15, VisualDpi);
        var width = DpiLayout.Scale(10, VisualDpi) + side + DpiLayout.Scale(7, VisualDpi) +
                    text.Width + DpiLayout.Scale(10, VisualDpi);
        var height = Math.Max(DpiLayout.Scale(40, VisualDpi), text.Height + DpiLayout.Scale(12, VisualDpi));
        return new Size(Math.Max(MinimumSize.Width, width), Math.Max(MinimumSize.Height, height));
    }

    private Rectangle CalculateTextContentBounds(Rectangle bounds) => CalculateTextContentBounds(bounds, VisualDpi);

    private static Rectangle CalculateTextContentBounds(Rectangle bounds, int visualDpi)
    {
        var side = DpiLayout.Scale(15, visualDpi);
        var boxRight = bounds.Left + DpiLayout.Scale(10, visualDpi) + side;
        var left = boxRight + DpiLayout.Scale(7, visualDpi);
        return new Rectangle(left, bounds.Top,
            Math.Max(1, bounds.Right - left - DpiLayout.Scale(10, visualDpi)), bounds.Height);
    }

    protected override void OnMouseEnter(EventArgs e)
    {
        _hovered = true;
        Invalidate();
        base.OnMouseEnter(e);
    }

    protected override void OnMouseLeave(EventArgs e)
    {
        _hovered = false;
        _pressed = false;
        Invalidate();
        base.OnMouseLeave(e);
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (Enabled && e.Button == MouseButtons.Left) { _pressed = true; Capture = true; Invalidate(); }
        base.OnMouseDown(e);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        var toggle = Enabled && _pressed && e.Button == MouseButtons.Left && ClientRectangle.Contains(e.Location);
        _pressed = false;
        Capture = false;
        Invalidate();
        base.OnMouseUp(e);
        if (toggle) Checked = !Checked;
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        if (Enabled && e.KeyCode == Keys.Space)
        {
            Checked = !Checked;
            e.Handled = true;
            e.SuppressKeyPress = true;
        }
        base.OnKeyDown(e);
    }

    private void ThemeFrameChanged(object? sender, EventArgs e) => Invalidate();
}

public sealed class ThemeCheckBox : Control, IExplicitAnimationPaintSource
{
    private bool _hovered;
    private bool _checked;
    public int VisualDpiOverride { get; set; }
    private int VisualDpi => VisualDpiOverride > 0 ? DpiLayout.NormalizeDpi(VisualDpiOverride) : DeviceDpi;

    public ThemeCheckBox()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Selectable, true);
        AutoSize = true;
        TabStop = true;
        Cursor = Cursors.Hand;
        BackColor = UiTheme.Surface;
        MouseEnter += (_, _) => { _hovered = true; Invalidate(); };
        MouseLeave += (_, _) => { _hovered = false; Invalidate(); };
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public event EventHandler? CheckedChanged;

    public bool Checked
    {
        get => _checked;
        set
        {
            if (_checked == value) return;
            _checked = value;
            Invalidate();
            CheckedChanged?.Invoke(this, EventArgs.Empty);
        }
    }

    public override Size GetPreferredSize(Size proposedSize)
    {
        var text = NativeButtonText.MeasureAtDpi(Text, Font, VisualDpi);
        var side = DpiLayout.Scale(16, VisualDpi);
        var gap = DpiLayout.Scale(7, VisualDpi);
        var horizontalSafety = DpiLayout.Scale(11, VisualDpi);
        return new Size(side + gap + horizontalSafety + text.Width,
            Math.Max(DpiLayout.Scale(22, VisualDpi), text.Height));
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        e.Graphics.Clear(ThemePaint.ResolveOpaqueBackground(this, UiTheme.Surface));
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        ThemePaint.Configure(e.Graphics);
        var side = DpiLayout.Scale(16, VisualDpi);
        var box = new Rectangle(1, (Height - side) / 2, side, side);
        using var boxPath = ThemePaint.RoundedPath(box, DpiLayout.Scale(4, VisualDpi));
        using (var background = new SolidBrush(Checked ? UiTheme.Accent : _hovered ? UiTheme.AccentSoft : UiTheme.Surface))
            e.Graphics.FillPath(background, boxPath);
        using (var border = new Pen(Checked ? UiTheme.Accent : UiTheme.Border, 1F))
            e.Graphics.DrawPath(border, boxPath);
        if (Checked)
            ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.Check, RectangleF.Inflate(box, -1F, -1F), UiTheme.OnAccent);
        var textBounds = new Rectangle(box.Right + DpiLayout.Scale(7, VisualDpi), 0,
            Math.Max(1, Width - box.Right - DpiLayout.Scale(7, VisualDpi)), Height);
        ThemePaint.DrawText(e.Graphics, Text, Font, textBounds, Enabled ? UiTheme.Text : UiTheme.DisabledText,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
        if (Focused && ShowFocusCues)
            ThemePaint.DrawFocusRing(e.Graphics, ClientRectangle,
                DpiLayout.Scale(CornerRadiusTokens.Input, VisualDpi), VisualDpi, UiTheme.Accent);
        AnimationRunner.ReportPaint(this);
    }

    protected override void OnClick(EventArgs e)
    {
        if (Enabled) Checked = !Checked;
        base.OnClick(e);
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        if (Enabled && e.KeyCode == Keys.Space)
        {
            Checked = !Checked;
            e.Handled = true;
            e.SuppressKeyPress = true;
        }
        base.OnKeyDown(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    private void ThemeFrameChanged(object? sender, EventArgs e) => Invalidate();
}

public sealed class ThemeNumericUpDown : UserControl, IExplicitAnimationPaintSource
{
    private readonly TextBox _editor = new ThemedEmbeddedTextBox
    {
        BorderStyle = BorderStyle.None,
        TextAlign = HorizontalAlignment.Left,
    };
    private decimal _minimum;
    private decimal _maximum = 100;
    private decimal _value;
    private bool _updatingText;
    public int VisualDpiOverride { get; set; }
    private int VisualDpi => VisualDpiOverride > 0 ? DpiLayout.NormalizeDpi(VisualDpiOverride) : DeviceDpi;

    public ThemeNumericUpDown()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint, true);
        Height = 30;
        Padding = new Padding(8, 6, 25, 4);
        BackColor = UiTheme.Surface;
        ForeColor = UiTheme.Text;
        _editor.Dock = DockStyle.Fill;
        _editor.BackColor = UiTheme.Surface;
        _editor.ForeColor = UiTheme.Text;
        _editor.Text = "0";
        _editor.TextChanged += EditorTextChanged;
        _editor.KeyDown += EditorKeyDown;
        _editor.LostFocus += (_, _) => SynchronizeText();
        Controls.Add(_editor);
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    public event EventHandler? ValueChanged;

    public decimal Minimum
    {
        get => _minimum;
        set { _minimum = value; if (_maximum < value) _maximum = value; Value = _value; }
    }

    public decimal Maximum
    {
        get => _maximum;
        set { _maximum = value; if (_minimum > value) _minimum = value; Value = _value; }
    }

    public decimal Value
    {
        get => _value;
        set
        {
            var next = Math.Clamp(value, _minimum, _maximum);
            if (_value == next) { SynchronizeText(); return; }
            _value = next;
            SynchronizeText();
            ValueChanged?.Invoke(this, EventArgs.Empty);
            Invalidate();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var fill = new SolidBrush(Enabled ? UiTheme.Surface : UiTheme.DisabledBackground);
        using var border = new Pen(UiTheme.Border, 1F);
        using var path = ThemePaint.RoundedPath(ClientRectangle, DpiLayout.Scale(CornerRadiusTokens.Input, VisualDpi));
        e.Graphics.FillPath(fill, path);
        e.Graphics.DrawPath(border, path);
        var buttonLeft = Width - DpiLayout.Scale(23, VisualDpi);
        e.Graphics.DrawLine(border, buttonLeft, 1, buttonLeft, Height - 2);
        var centerX = buttonLeft + (Width - buttonLeft) / 2F;
        var topY = Height * 0.31F;
        var bottomY = Height * 0.69F;
        var glyphSide = DpiLayout.Scale(9, VisualDpi);
        var glyphColor = Enabled ? UiTheme.Muted : UiTheme.DisabledText;
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.ChevronUp,
            new RectangleF(centerX - glyphSide / 2F, topY - glyphSide / 2F, glyphSide, glyphSide), glyphColor);
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.ChevronDown,
            new RectangleF(centerX - glyphSide / 2F, bottomY - glyphSide / 2F, glyphSide, glyphSide), glyphColor);
        AnimationRunner.ReportPaint(this);
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        if (!Enabled) return;
        var buttonLeft = Width - DpiLayout.Scale(23, VisualDpi);
        if (e.X < buttonLeft)
        {
            _editor.Focus();
            return;
        }
        Value += e.Y < Height / 2 ? 1 : -1;
        _editor.Focus();
    }

    protected override void OnMouseWheel(MouseEventArgs e)
    {
        if (Enabled) Value += e.Delta > 0 ? 1 : -1;
        base.OnMouseWheel(e);
    }

    protected override void OnFontChanged(EventArgs e)
    {
        base.OnFontChanged(e);
        _editor.Font = Font;
    }

    protected override void OnEnabledChanged(EventArgs e)
    {
        base.OnEnabledChanged(e);
        _editor.Enabled = Enabled;
        ApplyTheme();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    private void EditorTextChanged(object? sender, EventArgs e)
    {
        if (_updatingText || !decimal.TryParse(_editor.Text, out var parsed)) return;
        var next = Math.Clamp(parsed, _minimum, _maximum);
        if (_value == next) return;
        _value = next;
        ValueChanged?.Invoke(this, EventArgs.Empty);
    }

    private void EditorKeyDown(object? sender, KeyEventArgs e)
    {
        if (e.KeyCode is not (Keys.Up or Keys.Down)) return;
        Value += e.KeyCode == Keys.Up ? 1 : -1;
        e.Handled = true;
        e.SuppressKeyPress = true;
    }

    private void SynchronizeText()
    {
        var text = decimal.Truncate(_value) == _value ? decimal.Truncate(_value).ToString() : _value.ToString();
        if (_editor.Text == text) return;
        _updatingText = true;
        _editor.Text = text;
        _updatingText = false;
    }

    private void ThemeFrameChanged(object? sender, EventArgs e) => ApplyTheme();

    private void ApplyTheme()
    {
        BackColor = Enabled ? UiTheme.Surface : UiTheme.DisabledBackground;
        ForeColor = Enabled ? UiTheme.Text : UiTheme.DisabledText;
        _editor.BackColor = BackColor;
        _editor.ForeColor = ForeColor;
        Invalidate();
    }
}

public sealed class AppAvatarControl : Control, IExplicitAnimationPaintSource
{
    private Image? _source;

    internal Rectangle StableAnimationContentBounds => Rectangle.Inflate(ClientRectangle, -2, -2);

    public AppAvatarControl()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.SupportsTransparentBackColor, true);
        BackColor = Color.Transparent;
        AccessibleName = "TreasureChest 头像";
        try
        {
            var path = Path.Combine(AppPaths.Root, "resources", "app.png");
            if (File.Exists(path))
            {
                using var loaded = Image.FromFile(path);
                _source = new Bitmap(loaded);
            }
        }
        catch
        {
            _source = IconService.LoadAppIcon().ToBitmap();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        e.Graphics.CompositingQuality = CompositingQuality.HighQuality;
        e.Graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
        var bounds = Rectangle.Inflate(ClientRectangle, -2, -2);
        using var clip = new GraphicsPath();
        clip.AddEllipse(bounds);
        var state = e.Graphics.Save();
        e.Graphics.SetClip(clip);
        e.Graphics.Clear(UiTheme.AccentSoft);
        if (_source is not null)
        {
            var crop = new RectangleF(
                _source.Width * 0.30F,
                _source.Height * 0.39F,
                _source.Width * 0.54F,
                _source.Width * 0.54F);
            e.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
            e.Graphics.DrawImage(_source, bounds, crop, GraphicsUnit.Pixel);
        }
        e.Graphics.Restore(state);
        using var border = new Pen(UiTheme.AccentSoftStrong, Math.Max(1F, DeviceDpi / 96F));
        e.Graphics.DrawEllipse(border, bounds);
        AnimationRunner.ReportPaint(this);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) _source?.Dispose();
        base.Dispose(disposing);
    }
}

public class SurfacePanel : Panel, IExplicitAnimationPaintSource
{
    public int CornerRadiusLogical { get; set; } = CornerRadiusTokens.SecondarySurface;
    public bool DrawBorder { get; set; } = true;
    public bool DrawShadow { get; set; }
    protected virtual Color PanelBorderColor => UiTheme.Border;

    public SurfacePanel()
    {
        BackColor = UiTheme.Surface;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint, true);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        e.Graphics.Clear(Parent?.BackColor ?? UiTheme.Background);
        var surfaceBounds = ClientRectangle;
        if (DrawShadow && Width > 8 && Height > 8)
        {
            surfaceBounds = new Rectangle(2, 1, Width - 5, Height - 6);
            // Shadows participate in the same immutable palette frame as the
            // surface itself.  Reading the target Mode here used to make the
            // shadow jump to its endpoint while the rest of the window was
            // still interpolating.
            var nightAmount = UiTheme.NightAmount;
            var shadowAlpha = (int)Math.Round(5D + ((12D - 5D) * nightAmount));
            var shadowRed = (int)Math.Round(135D + ((8D - 135D) * nightAmount));
            var shadowGreen = (int)Math.Round(132D + ((10D - 132D) * nightAmount));
            var shadowBlue = (int)Math.Round(160D + ((18D - 160D) * nightAmount));
            for (var spread = 6; spread >= 1; spread--)
            {
                var shadowBounds = new Rectangle(0, spread, Width - 1, Height - spread);
                using var shadowPath = RoundedButton.RoundedPath(shadowBounds,
                    DpiLayout.Scale(CornerRadiusLogical + spread, DeviceDpi));
                using var shadow = new SolidBrush(Color.FromArgb(
                    Math.Max(1, shadowAlpha - spread + 1), shadowRed, shadowGreen, shadowBlue));
                e.Graphics.FillPath(shadow, shadowPath);
            }
        }
        using var path = RoundedButton.RoundedPath(surfaceBounds, DpiLayout.Scale(CornerRadiusLogical, DeviceDpi));
        using var fill = new SolidBrush(BackColor);
        e.Graphics.FillPath(fill, path);
        if (!DrawBorder) return;
        using var border = new Pen(PanelBorderColor, Math.Max(1F, DeviceDpi / 120F));
        e.Graphics.DrawPath(border, path);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        AnimationRunner.ReportPaint(this);
    }
}

public sealed class GridWellPanel : SurfacePanel
{
    protected override Color PanelBorderColor => UiTheme.GridWellBorder;

    public GridWellPanel()
    {
        CornerRadiusLogical = CornerRadiusTokens.GridWell;
        DrawBorder = true;
        DrawShadow = true;
        BackColor = UiTheme.SurfaceAlt;
        // Keep the child grid far enough inside the painted surface that its
        // header, blank body and custom scrollbar cannot cover the single,
        // continuous rounded outline.
        Padding = new Padding(4);
    }

    public static GridWellPanel Wrap(Control content)
    {
        var well = new GridWellPanel { Dock = DockStyle.Fill };
        content.Dock = DockStyle.Fill;
        well.Controls.Add(content);
        return well;
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        // Padding keeps the child grid away from the rounded corners.  A binary
        // Region would only clip away the surface's antialiased edge coverage.
        var previous = Region;
        Region = null;
        previous?.Dispose();
    }
}

/// <summary>
/// Scrollable content well that keeps the native WinForms scrollbar out of the
/// approved surface card. Mouse wheel/AutoScroll semantics remain native; only
/// the inset track and thumb are painted with shared theme tokens.
/// </summary>
public sealed class ThemedAutoScrollPanel : Panel, IExplicitAnimationPaintSource
{
    private bool _draggingThumb;
    private int _dragStartY;
    private int _dragStartOffset;

    public ThemedAutoScrollPanel()
    {
        AutoScroll = true;
        BackColor = UiTheme.Surface;
        Padding = new Padding(8, 8, 26, 8);
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint, true);
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        HideNativeBars();
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        HideNativeBars();
        Invalidate();
    }

    protected override void OnScroll(ScrollEventArgs se)
    {
        base.OnScroll(se);
        HideNativeBars();
        Invalidate();
    }

    protected override void OnMouseWheel(MouseEventArgs e)
    {
        base.OnMouseWheel(e);
        HideNativeBars();
        Invalidate();
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        var (track, thumb) = ScrollGeometry();
        if (thumb.IsEmpty || !track.Contains(e.Location)) return;
        if (thumb.Contains(e.Location))
        {
            _draggingThumb = true;
            _dragStartY = e.Y;
            _dragStartOffset = -AutoScrollPosition.Y;
            Capture = true;
            return;
        }
        ScrollToOffset(-AutoScrollPosition.Y + (e.Y < thumb.Top ? -1 : 1) * Math.Max(80, ClientSize.Height / 2));
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        if (!_draggingThumb) return;
        var (track, thumb) = ScrollGeometry();
        var travel = Math.Max(1, track.Height - thumb.Height);
        var maximum = MaximumOffset();
        ScrollToOffset(_dragStartOffset + (int)Math.Round((e.Y - _dragStartY) * maximum / (double)travel));
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        _draggingThumb = false;
        Capture = false;
        base.OnMouseUp(e);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var (track, thumb) = ScrollGeometry();
        if (!track.IsEmpty && !thumb.IsEmpty)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using var trackPath = RoundedButton.RoundedPath(track, Math.Max(2, track.Width / 2));
            using var trackBrush = new SolidBrush(UiTheme.ProgressTrack);
            e.Graphics.FillPath(trackBrush, trackPath);
            using var thumbPath = RoundedButton.RoundedPath(thumb, Math.Max(2, thumb.Width / 2));
            using var thumbBrush = new SolidBrush(UiTheme.Divider);
            e.Graphics.FillPath(thumbBrush, thumbPath);
        }
        AnimationRunner.ReportPaint(this);
    }

    private void ThemeFrameChanged(object? sender, EventArgs e)
    {
        BackColor = UiTheme.Surface;
        Invalidate();
    }

    private (Rectangle Track, Rectangle Thumb) ScrollGeometry()
    {
        var maximum = MaximumOffset();
        if (maximum <= 0 || ClientSize.Height <= 1) return (Rectangle.Empty, Rectangle.Empty);
        var width = DpiLayout.Scale(7, DeviceDpi);
        var inset = DpiLayout.Scale(8, DeviceDpi);
        var track = new Rectangle(ClientSize.Width - inset - width, inset,
            width, Math.Max(1, ClientSize.Height - inset * 2));
        var contentHeight = ClientSize.Height + maximum;
        var thumbHeight = Math.Clamp(
            (int)Math.Round(track.Height * ClientSize.Height / (double)contentHeight),
            DpiLayout.Scale(34, DeviceDpi), track.Height);
        var travel = Math.Max(0, track.Height - thumbHeight);
        var offset = Math.Clamp(-AutoScrollPosition.Y, 0, maximum);
        var top = track.Top + (maximum == 0 ? 0 : (int)Math.Round(travel * offset / (double)maximum));
        return (track, new Rectangle(track.Left, top, track.Width, thumbHeight));
    }

    private int MaximumOffset() => Math.Max(0, AutoScrollMinSize.Height + Padding.Vertical - ClientSize.Height);

    private void ScrollToOffset(int offset)
    {
        AutoScrollPosition = new Point(0, Math.Clamp(offset, 0, MaximumOffset()));
        HideNativeBars();
        Invalidate();
    }

    private void HideNativeBars()
    {
        if (!IsHandleCreated) return;
        Native.ShowScrollBar(Handle, 0, false);
        Native.ShowScrollBar(Handle, 1, false);
    }

    private static class Native
    {
        [System.Runtime.InteropServices.DllImport("user32.dll")]
        [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
        internal static extern bool ShowScrollBar(IntPtr hwnd, int bar, bool show);
    }
}

public sealed class ScrollSurfacePage : Panel
{
    public SurfacePanel SurfaceCard { get; }
    public ThemedAutoScrollPanel ScrollHost { get; }
    public Control Content { get; }

    public ScrollSurfacePage(Control content)
    {
        Content = content;
        Dock = DockStyle.Fill;
        BackColor = UiTheme.Background;
        SurfaceCard = new SurfacePanel
        {
            Dock = DockStyle.Fill,
            BackColor = UiTheme.Surface,
            Padding = new Padding(24),
            CornerRadiusLogical = CornerRadiusTokens.PrimarySurface,
            DrawShadow = true,
        };
        ScrollHost = new ThemedAutoScrollPanel { Dock = DockStyle.Fill };
        content.Dock = DockStyle.Top;
        ScrollHost.Controls.Add(content);
        SurfaceCard.Controls.Add(ScrollHost);
        Controls.Add(SurfaceCard);
        SizeChanged += (_, _) => FitContent();
        ScrollHost.SizeChanged += (_, _) => FitContent();
        content.SizeChanged += (_, _) => UpdateScrollExtent();
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
        FitContent();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
        base.Dispose(disposing);
    }

    private void ThemeFrameChanged(object? sender, EventArgs e)
    {
        BackColor = UiTheme.Background;
        SurfaceCard.BackColor = UiTheme.Surface;
        ScrollHost.BackColor = UiTheme.Surface;
        Invalidate(true);
    }

    private void FitContent()
    {
        Content.Width = Math.Max(1, ScrollHost.ClientSize.Width - ScrollHost.Padding.Horizontal);
        UpdateScrollExtent();
    }

    private void UpdateScrollExtent() =>
        ScrollHost.AutoScrollMinSize = new Size(0, Content.PreferredSize.Height + ScrollHost.Padding.Vertical);
}

public enum NavigationGlyph
{
    Sessions,
    Tools,
    Plugins,
    Settings,
    Collapse,
}

public sealed class SidebarNavigationButton : Button, IExplicitAnimationPaintSource
{
    private string _label;
    private readonly NavigationGlyph _glyph;
    private readonly ToolTip _actionToolTip = new() { ShowAlways = true };
    private double _collapseProgress;
    private bool _selected;
    private bool _hovered;
    private bool _pressed;
    public int VisualDpiOverride { get; set; }
    private int VisualDpi => VisualDpiOverride > 0 ? DpiLayout.NormalizeDpi(VisualDpiOverride) : DeviceDpi;

    public SidebarNavigationButton(string label, NavigationGlyph glyph)
    {
        _label = label;
        _glyph = glyph;
        Text = string.Empty;
        AccessibleName = label;
        if (_glyph == NavigationGlyph.Collapse) UpdateSidebarAction(expandsSidebar: false);
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        TextAlign = ContentAlignment.MiddleLeft;
        UseVisualStyleBackColor = false;
        BackColor = UiTheme.Navigation;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                 ControlStyles.ResizeRedraw | ControlStyles.UserPaint | ControlStyles.Opaque, true);
    }

    public bool IsSelected
    {
        get => _selected;
        set { if (_selected == value) return; _selected = value; Invalidate(); }
    }

    public bool ShowContainer { get; set; }

    public bool ExpandsSidebar { get; private set; }

    public string ActionLabel => _label;

    public string ToolTipText => _actionToolTip.GetToolTip(this) ?? string.Empty;

    public double CollapseProgress
    {
        get => _collapseProgress;
        set
        {
            _collapseProgress = Math.Clamp(value, 0D, 1D);
            if (_glyph == NavigationGlyph.Collapse)
                UpdateSidebarAction(_collapseProgress >= 0.5D);
            Invalidate();
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) _actionToolTip.Dispose();
        base.Dispose(disposing);
    }

    protected override void OnMouseEnter(EventArgs e) { _hovered = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { _hovered = false; _pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left) { _pressed = true; Invalidate(); }
        base.OnMouseDown(e);
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        _pressed = false;
        Invalidate();
        base.OnMouseUp(e);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        // OnPaint composes the complete opaque navigation item from one
        // palette frame. Letting ButtonBase paint BackColor first leaves a
        // rectangular child-window background behind the idle item when the
        // parent host uses a different default surface.
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        ThemePaint.Configure(e.Graphics);
        e.Graphics.Clear(ThemePaint.ResolveOpaqueBackground(this, UiTheme.Palette.Navigation));
        var itemBounds = Rectangle.Inflate(ClientRectangle, -1, -2);
        var drawStateSurface = _selected || _hovered || _pressed || ShowContainer;
        if (drawStateSurface)
        {
            var backgroundColor = _pressed || _selected ? UiTheme.NavigationPressed
                : _hovered ? UiTheme.NavigationHover
                : UiTheme.NavigationHover;
            using var itemPath = ThemePaint.RoundedPath(itemBounds,
                DpiLayout.Scale(CornerRadiusTokens.NavigationItem, VisualDpi));
            using (var background = new SolidBrush(backgroundColor)) e.Graphics.FillPath(background, itemPath);
            if (_selected || ShowContainer)
            {
                using var border = new Pen(_selected ? UiTheme.AccentSoftStrong : UiTheme.Border, 1F);
                e.Graphics.DrawPath(border, itemPath);
            }
        }
        if (_selected)
        {
            var indicator = new RectangleF(DpiLayout.Scale(2, VisualDpi), Height * 0.22F,
                DpiLayout.Scale(6, VisualDpi), Height * 0.56F);
            using var indicatorPath = RoundedButton.RoundedPath(Rectangle.Round(indicator), DpiLayout.Scale(2, VisualDpi));
            using var indicatorBrush = new SolidBrush(UiTheme.Accent);
            e.Graphics.FillPath(indicatorBrush, indicatorPath);
        }
        var iconCenter = new PointF(DpiLayout.Scale(27, VisualDpi), Height / 2F);
        var foreground = Enabled ? UiTheme.NavigationText : UiTheme.DisabledText;
        var iconSize = DpiLayout.Scale(24, VisualDpi);
        ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyphMap.From(_glyph, ExpandsSidebar),
            new RectangleF(iconCenter.X - iconSize / 2F, iconCenter.Y - iconSize / 2F, iconSize, iconSize),
            foreground);
        var alpha = (int)Math.Round(255D * (1D - _collapseProgress));
        if (alpha > 0)
        {
            var x = DpiLayout.Scale(52, VisualDpi) - (int)Math.Round(DpiLayout.Scale(20, VisualDpi) * _collapseProgress);
            var bounds = new Rectangle(x, 0, Math.Max(1, Width - x - 6), Height);
            ThemePaint.DrawText(e.Graphics, _label, Font, bounds, Color.FromArgb(alpha, foreground),
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
        }
        AnimationRunner.ReportPaint(this);
    }

    private void UpdateSidebarAction(bool expandsSidebar)
    {
        ExpandsSidebar = expandsSidebar;
        _label = expandsSidebar ? "展开侧栏" : "收起侧栏";
        Text = _label;
        AccessibleName = _label;
        AccessibleDescription = _label;
        _actionToolTip.SetToolTip(this, _label);
    }
}

internal static class GraphicsExtensions
{
    public static void DrawRoundedRectangle(this Graphics graphics, Pen pen, RectangleF bounds, float radius)
    {
        using var path = new GraphicsPath();
        var diameter = radius * 2F;
        path.AddArc(bounds.Left, bounds.Top, diameter, diameter, 180, 90);
        path.AddArc(bounds.Right - diameter, bounds.Top, diameter, diameter, 270, 90);
        path.AddArc(bounds.Right - diameter, bounds.Bottom - diameter, diameter, diameter, 0, 90);
        path.AddArc(bounds.Left, bounds.Bottom - diameter, diameter, diameter, 90, 90);
        path.CloseFigure();
        graphics.DrawPath(pen, path);
    }
}
