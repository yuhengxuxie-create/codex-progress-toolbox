namespace TreasureChest.UI;

/// <summary>
/// 项目监测管理工具栏使用与自测相同的 DPI 几何模型。每项先占据稳定宿主矩形，
/// 再把真实控件整体垂直居中；不依赖控件各自的 Margin.Top 或字体基线。
/// </summary>
public sealed class MonitorToolbarPanel : SurfacePanel
{
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Control, ToolbarItemHost>
        BehaviorHosts = new();
    private readonly Control[] _items;
    private readonly ToolbarItemHost[] _hosts;
    private readonly HiddenBehaviorLayer _behaviorLayer = new();
    private bool _initializing;

    public IReadOnlyList<Rectangle> ItemBoundsSnapshot => _hosts.Select(host => host.Bounds).ToArray();

    internal static void SetSearchGlyph(TextBox input, ButtonGlyph glyph)
    {
        if (!BehaviorHosts.TryGetValue(input, out var host) || host.Chrome != ToolbarChrome.Search) return;
        host.SearchGlyph = glyph;
        host.Invalidate();
    }

    internal static bool TryGetSearchHost(TextBox input, out Control host, out Rectangle textBounds)
    {
        if (BehaviorHosts.TryGetValue(input, out var toolbarHost) &&
            toolbarHost.Chrome == ToolbarChrome.Search)
        {
            host = toolbarHost;
            textBounds = toolbarHost.VisualContentBounds;
            return true;
        }
        host = null!;
        textBounds = Rectangle.Empty;
        return false;
    }

    internal static bool TryGetChromeHost(Control behavior, out Control host)
    {
        if (BehaviorHosts.TryGetValue(behavior, out var toolbarHost))
        {
            host = toolbarHost;
            return true;
        }
        host = null!;
        return false;
    }

    public MonitorToolbarPanel(params Control[] items)
    {
        if (items.Length != 6) throw new ArgumentException("项目监测工具栏必须包含六项。", nameof(items));
        _items = items;
        _hosts = new ToolbarItemHost[items.Length];
        _initializing = true;
        SuspendLayout();
        Controls.Add(_behaviorLayer);
        for (var index = 0; index < items.Length; index++)
        {
            var item = items[index];
            var chrome = item switch
            {
                TextBox => ToolbarChrome.Search,
                ComboBox => ToolbarChrome.Combo,
                PillCheckBox => ToolbarChrome.Pill,
                _ => ToolbarChrome.None,
            };
            var host = new ToolbarItemHost(chrome) { Margin = Padding.Empty, BackColor = UiTheme.Surface, TabIndex = index };
            _hosts[index] = host;
            item.Margin = Padding.Empty;
            if (item is TextBox textBox)
            {
                textBox.BorderStyle = BorderStyle.None;
                textBox.BackColor = UiTheme.Surface;
            }
            if (item is ComboBox comboBox)
            {
                comboBox.FlatStyle = FlatStyle.Flat;
                comboBox.BackColor = UiTheme.Surface;
            }
            if (chrome is ToolbarChrome.Search or ToolbarChrome.Combo or ToolbarChrome.Pill)
            {
                _behaviorLayer.Controls.Add(item);
                host.AttachBehavior(item);
                BehaviorHosts.Remove(item);
                BehaviorHosts.Add(item, host);
            }
            else
            {
                host.Controls.Add(item);
            }
            Controls.Add(host);
        }
        _behaviorLayer.SendToBack();
        Dock = DockStyle.Top;
        BackColor = UiTheme.Surface;
        CornerRadiusLogical = 18;
        DrawBorder = true;
        SetStyle(ControlStyles.ResizeRedraw | ControlStyles.OptimizedDoubleBuffer, true);
        _initializing = false;
        ResumeLayout(performLayout: true);
    }

    protected override void OnLayout(LayoutEventArgs eventArgs)
    {
        if (_initializing)
        {
            base.OnLayout(eventArgs);
            return;
        }
        var visualDpi = DeviceDpi;
        var layout = DpiLayout.ProjectMonitorToolbar(visualDpi, ClientSize.Width);
        if (Height != layout.ToolbarSize.Height) Height = layout.ToolbarSize.Height;
        MinimumSize = new Size(layout.MinimumWidth, layout.ToolbarSize.Height);
        _behaviorLayer.Bounds = HiddenBehaviorLayer.ParkingBounds;
        for (var index = 0; index < _items.Length; index++)
        {
            var host = _hosts[index];
            var item = _items[index];
            host.Bounds = layout.ItemBounds[index];
            var fillWidth = item is TextBox or ComboBox or CheckBox or PillCheckBox or Button or FlowLayoutPanel;
            if (item is TextBox)
            {
                var inner = host.ClientRectangle;
                inner.X += DpiLayout.Scale(42, visualDpi);
                inner.Width -= DpiLayout.Scale(56, visualDpi);
                inner.Y += DpiLayout.Scale(7, visualDpi);
                inner.Height -= DpiLayout.Scale(14, visualDpi);
                host.VisualContentBounds = inner;
                item.Bounds = new Rectangle(0, 0, Math.Max(1, inner.Width), Math.Max(1, inner.Height));
            }
            else if (item is ComboBox)
            {
                var inner = Rectangle.Inflate(host.ClientRectangle,
                    -DpiLayout.Scale(8, visualDpi),
                    -DpiLayout.Scale(4, visualDpi));
                host.VisualContentBounds = inner;
                item.Bounds = new Rectangle(0, 0, Math.Max(1, inner.Width), Math.Max(1, inner.Height));
            }
            else if (item is PillCheckBox)
            {
                host.VisualContentBounds = host.ClientRectangle;
                item.Bounds = new Rectangle(0, 0, Math.Max(1, host.ClientSize.Width), Math.Max(1, host.ClientSize.Height));
            }
            else
            {
                item.Bounds = DpiLayout.CenterControl(host.ClientRectangle, item.PreferredSize, fillWidth, fillHeight: true);
            }
            // WinForms ComboBox may coerce the requested height after Bounds is assigned.
            // Re-center the actual resulting rectangle instead of assuming the requested height stuck.
            if (item.Parent == host && item.Height < host.ClientSize.Height)
                item.Top = (host.ClientSize.Height - item.Height) / 2;
        }
        base.OnLayout(eventArgs);
    }

    internal enum ToolbarChrome
    {
        None,
        Search,
        Combo,
        Pill,
    }

    private sealed class ToolbarItemHost : Panel, IExplicitAnimationPaintSource
    {
        private Control? _behavior;
        private ContextMenuStrip? _managedDropDown;
        private bool _searchPressed;
        private bool _hovered;
        private bool _pressed;

        internal ToolbarChrome Chrome { get; }
        public bool IncludeInThemePaintTelemetry => Chrome is ToolbarChrome.Search or ToolbarChrome.Combo or ToolbarChrome.Pill;
        internal ButtonGlyph SearchGlyph { get; set; } = ButtonGlyph.Search;
        internal Rectangle VisualContentBounds { get; set; }

        internal ToolbarItemHost(ToolbarChrome chrome)
        {
            Chrome = chrome;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
                     ControlStyles.ResizeRedraw | ControlStyles.UserPaint |
                     ControlStyles.Selectable, true);
            TabStop = chrome is ToolbarChrome.Search or ToolbarChrome.Combo or ToolbarChrome.Pill;
            Cursor = chrome == ToolbarChrome.Search ? Cursors.IBeam :
                chrome is ToolbarChrome.Combo or ToolbarChrome.Pill ? Cursors.Hand : Cursors.Default;
            AccessibleRole = chrome == ToolbarChrome.Search ? AccessibleRole.Text :
                chrome == ToolbarChrome.Combo ? AccessibleRole.ComboBox :
                chrome == ToolbarChrome.Pill ? AccessibleRole.CheckButton : AccessibleRole.Pane;
            AccessibleName = chrome == ToolbarChrome.Search ? "项目名称搜索" :
                chrome == ToolbarChrome.Combo ? "项目范围" :
                chrome == ToolbarChrome.Pill ? "包含已归档" : null;
            UiTheme.PaletteFrameChanged += ThemeFrameChanged;
        }

        internal void AttachBehavior(Control behavior)
        {
            if (Chrome is not (ToolbarChrome.Search or ToolbarChrome.Combo or ToolbarChrome.Pill)) return;
            _behavior = behavior;
            behavior.TabStop = false;
            behavior.AccessibleRole = AccessibleRole.None;
            behavior.AccessibleName = null;
            behavior.TextChanged += BehaviorVisualChanged;
            behavior.GotFocus += BehaviorVisualChanged;
            behavior.LostFocus += BehaviorVisualChanged;
            if (behavior is ComboBox combo)
            {
                combo.SelectedIndexChanged += BehaviorVisualChanged;
                combo.KeyDown += ComboBehaviorKeyDown;
            }
            if (behavior is PillCheckBox pill) pill.CheckedChanged += BehaviorVisualChanged;
            UpdateAccessibilityDescription();
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            // The visible field is one atomic managed composition.  In particular,
            // do not split the surface clear into OnPaintBackground: an Opaque/
            // buffered WinForms host may suppress that stage and WGC then captures
            // uninitialized black or pixels retained from the adjacent selector.
        }

        private void PaintManagedBackground(Graphics graphics)
        {
            ThemePaint.Configure(graphics);
            graphics.Clear(UiTheme.Surface);
            if (Chrome == ToolbarChrome.None) return;
            using var path = ThemePaint.RoundedPath(ClientRectangle,
                DpiLayout.Scale(CornerRadiusTokens.Input, DeviceDpi));
            using var fill = new SolidBrush(UiTheme.Surface);
            using var border = new Pen(_behavior?.Focused == true ? UiTheme.Accent : UiTheme.Border, 1F);
            graphics.FillPath(fill, path);
            graphics.DrawPath(border, path);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            RenderManagedLayer(e.Graphics);
            AnimationRunner.ReportPaint(this);
        }

        private void PaintManagedForeground(Graphics graphics)
        {
            if (_behavior is ThemedEmbeddedTextBox editor && Chrome == ToolbarChrome.Search)
            {
                editor.DrawVisual(graphics, VisualContentBounds);
                var bounds = SearchSlotAnimator.SearchGlyphBounds(ClientRectangle, DeviceDpi);
                SearchSlotAnimator.DrawGlyph(graphics, SearchGlyph, bounds, UiTheme.Muted, DeviceDpi);
            }
            else if (_behavior is ComboBox combo && Chrome == ToolbarChrome.Combo)
            {
                var arrowWidth = Math.Min(ClientSize.Width, DpiLayout.Scale(34, DeviceDpi));
                var textBounds = VisualContentBounds;
                textBounds.X += DpiLayout.Scale(6, DeviceDpi);
                textBounds.Width = Math.Max(1, textBounds.Width - arrowWidth - DpiLayout.Scale(8, DeviceDpi));
                ThemePaint.DrawText(graphics, combo.Text, combo.Font, textBounds,
                    combo.Enabled ? UiTheme.Text : UiTheme.DisabledText,
                    TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding |
                    TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine);
                var side = Math.Min(arrowWidth, DpiLayout.Scale(16, DeviceDpi));
                ThemeGlyphRenderer.Draw(graphics, ThemeGlyph.ChevronDown,
                    new RectangleF(ClientSize.Width - arrowWidth / 2F - side / 2F,
                        (ClientSize.Height - side) / 2F, side, side),
                    combo.Enabled ? UiTheme.Muted : UiTheme.DisabledText);
            }
        }

        private void RenderManagedLayer(Graphics graphics)
        {
            if (Chrome == ToolbarChrome.Pill && _behavior is PillCheckBox pill)
            {
                pill.DrawVisual(graphics, ClientRectangle, _hovered, _pressed,
                    Focused && ShowFocusCues, DeviceDpi);
                return;
            }
            PaintManagedBackground(graphics);
            PaintManagedForeground(graphics);
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            if (Chrome == ToolbarChrome.Search && e.Button == MouseButtons.Left)
            {
                _searchPressed = true;
                Capture = true;
            }
            if (Chrome == ToolbarChrome.Pill && e.Button == MouseButtons.Left)
            {
                _pressed = true;
                Capture = true;
                Invalidate();
            }
            ActivateBehavior(openCombo: Chrome == ToolbarChrome.Combo);
            base.OnMouseDown(e);
        }

        protected override void OnMouseUp(MouseEventArgs e)
        {
            var activateSearch = Chrome == ToolbarChrome.Search && _searchPressed &&
                                 e.Button == MouseButtons.Left && ClientRectangle.Contains(e.Location);
            var activatePill = Chrome == ToolbarChrome.Pill && _pressed &&
                               e.Button == MouseButtons.Left && ClientRectangle.Contains(e.Location);
            _searchPressed = false;
            _pressed = false;
            Capture = false;
            base.OnMouseUp(e);
            if (activateSearch && _behavior is ThemedEmbeddedTextBox editor)
                editor.InvokeManagedClick();
            if (activatePill && _behavior is PillCheckBox pill) pill.Checked = !pill.Checked;
            Invalidate();
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
            if (!Capture) _pressed = false;
            Invalidate();
            base.OnMouseLeave(e);
        }

        protected override void OnEnter(EventArgs e)
        {
            ActivateBehavior(openCombo: false);
            base.OnEnter(e);
        }

        protected override bool IsInputKey(Keys keyData) =>
            keyData is Keys.Enter or Keys.Space or Keys.F4 or Keys.Down or Keys.Up || base.IsInputKey(keyData);

        protected override void OnKeyDown(KeyEventArgs e)
        {
            if (Chrome == ToolbarChrome.Search && e.KeyCode is Keys.Enter or Keys.Space)
            {
                e.Handled = true;
                e.SuppressKeyPress = true;
                if (_behavior is ThemedEmbeddedTextBox editor) editor.InvokeManagedClick();
            }
            if (Chrome == ToolbarChrome.Pill && e.KeyCode == Keys.Space)
            {
                e.Handled = true;
                e.SuppressKeyPress = true;
                if (_behavior is PillCheckBox pill) pill.Checked = !pill.Checked;
            }
            if (Chrome == ToolbarChrome.Combo && e.KeyCode is Keys.Enter or Keys.Space or Keys.F4)
            {
                e.Handled = true;
                e.SuppressKeyPress = true;
                if (_behavior is ComboBox combo) ShowManagedDropDown(combo);
            }
            base.OnKeyDown(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                UiTheme.PaletteFrameChanged -= ThemeFrameChanged;
                var managedDropDown = _managedDropDown;
                _managedDropDown = null;
                if (managedDropDown is not null)
                {
                    managedDropDown.Closed -= ManagedDropDownClosed;
                    if (!managedDropDown.IsDisposed) managedDropDown.Dispose();
                }
                if (_behavior is not null)
                {
                    _behavior.TextChanged -= BehaviorVisualChanged;
                    _behavior.GotFocus -= BehaviorVisualChanged;
                    _behavior.LostFocus -= BehaviorVisualChanged;
                    if (_behavior is ComboBox combo)
                    {
                        combo.SelectedIndexChanged -= BehaviorVisualChanged;
                        combo.KeyDown -= ComboBehaviorKeyDown;
                    }
                    if (_behavior is PillCheckBox pill) pill.CheckedChanged -= BehaviorVisualChanged;
                    BehaviorHosts.Remove(_behavior);
                }
            }
            base.Dispose(disposing);
        }

        private void ActivateBehavior(bool openCombo)
        {
            if (_behavior is null || !_behavior.Enabled) return;
            if (_behavior is TextBox search)
            {
                // Fuzzy mode turns the native EDIT into a read-only, off-screen
                // behaviour object.  Giving that hidden HWND focus during
                // WM_LBUTTONDOWN releases the visible host's mouse capture on a
                // real desktop click, so the host never receives WM_LBUTTONUP
                // and the search workflow is not opened.  Direct SendMessage
                // tests used to miss this because they forced the up message
                // back to the host.  Keep focus/capture on the managed host for
                // the read-only action; editable exact-search mode still routes
                // keyboard/IME input to the native editor.
                if (search.ReadOnly)
                {
                    if (!Focused) Focus();
                    Invalidate();
                    return;
                }
                search.Focus();
                search.SelectionStart = search.TextLength;
                search.SelectionLength = 0;
            }
            else if (openCombo && _behavior is ComboBox combo)
            {
                combo.Focus();
                ShowManagedDropDown(combo);
            }
            else
            {
                _behavior.Focus();
            }
            Invalidate();
        }

        private void ComboBehaviorKeyDown(object? sender, KeyEventArgs e)
        {
            if (sender is not ComboBox combo) return;
            if (e.KeyCode == Keys.F4 || e.Alt && e.KeyCode == Keys.Down || e.KeyCode is Keys.Enter or Keys.Space)
            {
                e.Handled = true;
                e.SuppressKeyPress = true;
                BeginInvoke(() => ShowManagedDropDown(combo));
            }
        }

        private void ShowManagedDropDown(ComboBox combo)
        {
            if (!combo.Enabled || combo.Items.Count == 0) return;
            var menu = _managedDropDown;
            if (menu?.IsDisposed == true)
            {
                _managedDropDown = null;
                menu = null;
            }
            if (menu?.Visible == true)
            {
                menu.Close(ToolStripDropDownCloseReason.AppClicked);
                return;
            }
            if (menu is null)
            {
                menu = new ContextMenuStrip
                {
                    ShowImageMargin = false,
                    ShowCheckMargin = false,
                    AutoSize = true,
                };
                menu.Closed += ManagedDropDownClosed;
                _managedDropDown = menu;
            }
            menu.SuspendLayout();
            var previousItems = menu.Items.Cast<ToolStripItem>().ToArray();
            menu.Items.Clear();
            foreach (var existing in previousItems) existing.Dispose();
            menu.MinimumSize = new Size(Width, 0);
            menu.Font = combo.Font;
            UiTheme.StyleMenu(menu);
            for (var index = 0; index < combo.Items.Count; index++)
            {
                var itemIndex = index;
                var item = new ToolStripMenuItem(combo.GetItemText(combo.Items[index]))
                {
                    AutoSize = false,
                    Width = Math.Max(Width, combo.DropDownWidth),
                    Height = Math.Max(combo.ItemHeight, DpiLayout.Scale(32, DeviceDpi)),
                    Checked = index == combo.SelectedIndex,
                };
                item.Click += (_, _) => combo.SelectedIndex = itemIndex;
                menu.Items.Add(item);
            }
            menu.ResumeLayout(performLayout: true);
            menu.Show(this, new Point(0, Height + DpiLayout.Scale(2, DeviceDpi)),
                ToolStripDropDownDirection.BelowRight);
            Invalidate();
        }

        private void ManagedDropDownClosed(object? sender, ToolStripDropDownClosedEventArgs e)
        {
            // WinForms continues portions of its keyboard/app-click close pipeline after
            // Closed is raised.  Disposing here makes Esc race that pipeline and yields
            // ObjectDisposedException.  Keep the live popup for the next open and release
            // it exactly once with the owning toolbar host.
            Invalidate();
        }

        private void BehaviorVisualChanged(object? sender, EventArgs e)
        {
            UpdateAccessibilityDescription();
            Invalidate();
        }

        private void UpdateAccessibilityDescription()
        {
            AccessibleDescription = _behavior switch
            {
                TextBox text when text.TextLength > 0 => text.Text,
                TextBox text => text.PlaceholderText,
                ComboBox combo => combo.Text,
                PillCheckBox pill => pill.AccessibleName,
                _ => null,
            };
            AccessibilityNotifyClients(AccessibleEvents.ValueChange, -1);
        }

        private void ThemeFrameChanged(object? sender, EventArgs e)
        {
            BackColor = UiTheme.Surface;
            if (_managedDropDown is { IsDisposed: false } menu) UiTheme.StyleMenu(menu);
            Invalidate();
        }
    }

    private sealed class HiddenBehaviorLayer : Panel, IDpiLayoutExcluded
    {
        internal static readonly Rectangle ParkingBounds = new(-8192, -8192, 2, 2);

        internal HiddenBehaviorLayer()
        {
            Bounds = ParkingBounds;
            TabStop = false;
            AccessibleRole = AccessibleRole.None;
            BackColor = UiTheme.Surface;
        }
    }

}
