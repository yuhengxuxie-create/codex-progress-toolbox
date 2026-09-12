namespace TreasureChest.UI;

/// <summary>
/// Owns the closed-field and drop-down-row presentation in both themes. Keeping
/// the native ComboBox only for keyboard/IME/drop-down behavior prevents a white
/// Windows field or square arrow button from leaking through the rounded host.
/// </summary>
public sealed class ThemedComboBox : ComboBox
{
    private IntPtr _surfaceBrush;
    private uint _surfaceBrushColor;

    public ThemedComboBox()
    {
        DrawMode = DrawMode.OwnerDrawFixed;
        FlatStyle = FlatStyle.Flat;
        UiTheme.PaletteFrameChanged += ThemeFrameChanged;
    }

    protected override CreateParams CreateParams
    {
        get
        {
            var parameters = base.CreateParams;
            // The host owns the rounded border and this control paints its whole
            // closed field. Leaving WS_BORDER/WS_EX_CLIENTEDGE enabled creates a
            // separate native frame which is not covered by ClientRectangle and
            // appears as a square black arrow-side block in a real WGC frame.
            parameters.Style &= ~NativeMethods.WsBorder;
            parameters.ExStyle &= ~NativeMethods.WsExClientEdge;
            return parameters;
        }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        // Preserve the native ComboBox handle and drop-down behavior, but remove
        // every native closed-field theme painter. ToolbarItemHost owns the only
        // visible field and arrow; this HWND is clipped to an empty region.
        NativeMethods.SetWindowTheme(Handle, string.Empty, string.Empty);
        NativeMethods.SendMessage(
            Handle,
            NativeMethods.CbSetBackgroundColor,
            IntPtr.Zero,
            NativeMethods.ColorRef(UiTheme.Surface));
    }

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

    protected override void OnDrawItem(DrawItemEventArgs e)
    {
        if (e.Index < 0) return;
        var selected = (e.State & DrawItemState.Selected) != 0;
        var background = selected ? UiTheme.AccentSoftStrong : UiTheme.Surface;
        var foreground = Enabled ? UiTheme.Text : UiTheme.DisabledText;
        using (var fill = new SolidBrush(background)) e.Graphics.FillRectangle(fill, e.Bounds);
        ThemePaint.DrawText(e.Graphics, GetItemText(Items[e.Index]), Font,
            Rectangle.Inflate(e.Bounds, -DpiLayout.Scale(8, DeviceDpi), 0), foreground,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding |
            TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine);
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg is NativeMethods.WmReflect + NativeMethods.WmCtlColorEdit or
            NativeMethods.WmReflect + NativeMethods.WmCtlColorStatic or
            NativeMethods.WmReflect + NativeMethods.WmCtlColorListBox)
        {
            var background = Enabled ? UiTheme.Surface : UiTheme.DisabledBackground;
            if (m.WParam != IntPtr.Zero)
            {
                NativeMethods.SetBkColor(m.WParam, NativeMethods.ColorRefValue(background));
                NativeMethods.SetTextColor(m.WParam,
                    NativeMethods.ColorRefValue(Enabled ? UiTheme.Text : UiTheme.DisabledText));
            }
            m.Result = GetSurfaceBrush(NativeMethods.ColorRefValue(background));
            return;
        }
        if (m.Msg is NativeMethods.WmPrint or NativeMethods.WmPrintClient or
            NativeMethods.WmNcPaint or NativeMethods.WmEraseBackground)
        {
            // The host supplies the complete visible surface.  In particular,
            // do not draw to WM_PRINT/window DC because that bypasses the empty
            // native Region and produces a black arrow-side rectangle in WGC.
            m.Result = new IntPtr(1);
            return;
        }
        base.WndProc(ref m);
    }

    private void ThemeFrameChanged(object? sender, EventArgs e)
    {
        BackColor = UiTheme.Surface;
        ForeColor = UiTheme.Text;
        if (IsHandleCreated)
        {
            NativeMethods.SendMessage(
                Handle,
                NativeMethods.CbSetBackgroundColor,
                IntPtr.Zero,
                NativeMethods.ColorRef(UiTheme.Surface));
        }
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

}
