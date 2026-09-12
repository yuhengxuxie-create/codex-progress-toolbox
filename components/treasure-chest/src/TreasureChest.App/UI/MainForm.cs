using System.Diagnostics;
using TreasureChest.Core.Models;
using TreasureChest.Core.Services;
using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class MainForm : Form, IExplicitAnimationPaintSource
{
    private sealed class ResetAlertLogicalRow
    {
        public static readonly ResetAlertLogicalRow Instance = new();
        private ResetAlertLogicalRow() { }
    }

    private sealed class ResetAlertHoverCard : Control
    {
        public ResetAlertHoverCard()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint |
                     ControlStyles.OptimizedDoubleBuffer | ControlStyles.Opaque, true);
            TabStop = false;
            Visible = false;
            Font = UiTheme.CreateFont();
            AccessibleRole = AccessibleRole.HelpBalloon;
        }

        public void ShowDetail(string detail, Rectangle bounds)
        {
            Text = detail;
            AccessibleName = "Codex 重置预警来源说明";
            AccessibleDescription = detail;
            Bounds = bounds;
            Visible = true;
            BringToFront();
            Invalidate();
        }

        public void HideDetail()
        {
            Visible = false;
            Text = string.Empty;
            AccessibleDescription = string.Empty;
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            e.Graphics.Clear(ThemePaint.ResolveOpaqueBackground(this, UiTheme.Palette.Surface));
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            if (ClientRectangle.Width <= 2 || ClientRectangle.Height <= 2) return;

            ThemePaint.Configure(e.Graphics);
            var dpi = DeviceDpi > 0 ? DeviceDpi : DpiLayout.BaselineDpi;
            using var path = ThemePaint.RoundedPath(ClientRectangle,
                DpiLayout.Scale(CornerRadiusTokens.Input, dpi));
            using var fill = new SolidBrush(UiTheme.Palette.SurfaceAlt);
            using var border = new Pen(UiTheme.Palette.Warning, Math.Max(1F, dpi / 96F));
            e.Graphics.FillPath(fill, path);
            e.Graphics.DrawPath(border, path);

            var padding = DpiLayout.Scale(12, dpi);
            var iconSize = DpiLayout.Scale(14, dpi);
            var iconBounds = new RectangleF(
                padding,
                (ClientSize.Height - iconSize) / 2F,
                iconSize,
                iconSize);
            ThemeGlyphRenderer.Draw(e.Graphics, ThemeGlyph.Warning, iconBounds, UiTheme.Palette.Warning);

            var textBounds = new Rectangle(
                padding * 2 + iconSize,
                padding / 2,
                Math.Max(1, ClientSize.Width - padding * 3 - iconSize),
                Math.Max(1, ClientSize.Height - padding));
            ThemePaint.DrawText(e.Graphics, Text, Font, textBounds, UiTheme.Palette.Text,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.WordBreak |
                TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == NativeMethods.WmNcHitTest)
            {
                m.Result = new IntPtr(NativeMethods.HtTransparent);
                return;
            }

            base.WndProc(ref m);
        }
    }

    private readonly ConfigStore _configStore;
    private readonly AppLogger _logger;
    private readonly SessionManager _sessionManager;
    private readonly PluginCatalog _pluginCatalog;
    private readonly ProjectMonitorCliService _projectMonitorCli;
    private readonly ResetAlertCliService _resetAlertCli;
    private readonly ResetAlertDedupStore _resetAlertDedup;
    private Button? _resetAlertHistoryButton;
    private readonly ResetAlertRefreshGate _resetAlertRefreshGate = new(TimeSpan.FromSeconds(30));
    private readonly CodexThreadCatalogService _codexThreadCatalog;
    private readonly CodexDesktopLauncher _codexDesktopLauncher = new();
    private readonly AutoStartService _autoStart;
    private readonly bool _startupLaunch;
    private readonly ShellTrayIcon _trayIcon;
    private readonly NotificationService _notifications;
    private readonly System.Windows.Forms.Timer _timer = new();
    private readonly Panel _content = new() { Dock = DockStyle.Fill, BackColor = UiTheme.Background, Padding = Padding.Empty };
    private readonly DataGridView _sessionGrid = CreateGrid();
    private readonly DataGridView _monitorThreadGrid = CreateGrid();
    private readonly DataGridView _toolGrid = CreateGrid();
    private readonly DataGridView _pluginGrid = CreateGrid();
    private readonly ThemeCheckBox _notificationsSetting = new() { Text = "启用系统托盘通知", AutoSize = true };
    private readonly ThemeCheckBox _autoStartSetting = new() { Text = "登录 Windows 后自动启动", AutoSize = true };
    private readonly ThemeCheckBox _startMinimizedSetting = new() { Text = "自动启动时最小化到托盘", AutoSize = true };
    private readonly ThemeNumericUpDown _refreshSetting = new() { Minimum = 1, Maximum = 300, Width = 90 };
    private readonly ThemeCheckBox _autoUpdateCheckSetting = new() { Text = "自动检查整套生态更新", AutoSize = true };
    private readonly ThemeNumericUpDown _updateIntervalSetting = new() { Minimum = 1, Maximum = 168, Width = 90 };
    private readonly SlidingSegmentedControl _themeModeSetting = new("日间模式", "夜间模式")
    {
        Width = 220,
        Height = 40,
        SynchronizeWithThemeTransition = true,
    };
    private readonly Label _updateStatusLabel = new() { AutoSize = false, Width = 680, Height = 42, ForeColor = UiTheme.Muted };
    private readonly Button _checkForUpdatesButton;
    private readonly HashSet<string> _expandedMainMonitorGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private readonly ToolStripMenuItem _pauseMenu = new("暂停状态监控");
    private readonly ToolTip _windowChromeTips = new();
    private readonly ResetAlertHoverCard _resetAlertHoverTip = new();
    private readonly DpiLayoutStore _dpiLayout = new();
    private AppConfiguration _config;
    private IReadOnlyList<SessionDefinition> _sessions = [];
    private IReadOnlyList<ToolDefinition> _tools = [];
    private IReadOnlyList<ProjectMonitorItem> _projectMonitors = [];
    private bool _loadingPlugins;
    private bool _loadingSettings;
    private bool _refreshing;
    private bool _refreshingResetAlert;
    private bool _refreshingProjectMonitors;
    private bool _checkingUpdates;
    private DateTimeOffset _lastProjectMonitorRefresh = DateTimeOffset.MinValue;
    private bool _allowExit;
    private SidebarNavigationButton? _activeNav;
    private Panel _navigation = null!;
    private Panel _shellBody = null!;
    private Panel _navigationGap = null!;
    private SidebarTransitionOverlay _sidebarTransition = null!;
    private readonly List<SidebarNavigationButton> _navigationButtons = [];
    private SidebarNavigationButton _sidebarToggle = null!;
    private Button? _sessionPowerButton;
    private bool _populatingSessionGrid;
    private ResetAlertStatus? _resetAlertStatus;
    private string? _resetAlertFailure;
    private DateTimeOffset _resetAlertCheckedAt = DateTimeOffset.MinValue;
    private int _expandedNavigationLogicalWidth = 218;
    private double _sidebarCollapseProgress;
    private double _sidebarAnimationTarget;
    private int _sidebarTimelineGeneration;
    private bool _sidebarTransitionActive;
    private bool _sidebarShellWasEnabled = true;
    private bool _transitionPrimeScheduled;
    private bool _settingsPageVisible;
    private CaptionButton _minimizeButton = null!;
    private CaptionButton _maximizeButton = null!;
    private CaptionButton _closeButton = null!;
    private ResizeGripOverlay _resizeGrip = null!;
    private int _layoutDpi = DpiLayout.BaselineDpi;
    private Size _normalLogicalSize = new(1180, 760);
    private bool _dpiLayoutReady;
    private bool _initialDpiApplied;
    private bool _handlingDpiChange;
    private bool _applyingDpiLayout;
    private bool _workingAreaConstraintPending;
    private bool _dwmRoundedCornersActive;
    private bool _ownedResourcesDisposed;
    private bool _themeEventHandlersAttached;
    private FormWindowState _lastChromeState = FormWindowState.Normal;
    private FormWindowState _windowStateBeforeHide = FormWindowState.Normal;
    private readonly EcosystemUpdateService _ecosystemUpdates;
    private readonly EcosystemInstallation _ecosystemInstallation;
    private readonly SessionSearchCacheMode _sessionSearchCacheMode;

    public MainForm(
        AppConfiguration config,
        ConfigStore configStore,
        AppLogger logger,
        SessionManager sessionManager,
        PluginCatalog pluginCatalog,
        AutoStartService autoStart,
        bool startupLaunch,
        SessionSearchCacheMode sessionSearchCacheMode = SessionSearchCacheMode.Persistent,
        bool suppressInitialLoadForTests = false)
    {
        UiTheme.ConfigureDpiAwareForm(this, manuallyManaged: true);
        SetStyle(ControlStyles.ResizeRedraw | ControlStyles.OptimizedDoubleBuffer, true);
        _config = config;
        _configStore = configStore;
        _logger = logger;
        _sessionManager = sessionManager;
        _pluginCatalog = pluginCatalog;
        _autoStart = autoStart;
        _startupLaunch = startupLaunch;
        _sessionSearchCacheMode = sessionSearchCacheMode;
        _projectMonitorCli = new ProjectMonitorCliService(AppPaths.ProgressNotificationRoot, AppPaths.ProgressPythonPath);
        _resetAlertCli = new ResetAlertCliService(AppPaths.ProgressNotificationRoot, AppPaths.ProgressPythonPath);
        _resetAlertDedup = new ResetAlertDedupStore(AppPaths.ResetAlertConsumerStateFile);
        _codexThreadCatalog = new CodexThreadCatalogService(AppPaths.ProgressNotificationRoot, AppPaths.ProgressPythonPath);
        _ecosystemUpdates = new EcosystemUpdateService();
        _ecosystemInstallation = EcosystemInstallation.Load(AppPaths.Root);
        _checkForUpdatesButton = ActionButton("立即检查更新", async () => await CheckForUpdatesAsync(manual: true), true);

        Text = "TreasureChest";
        Font = UiTheme.CreateFont();
        BackColor = UiTheme.Background;
        MinimumSize = new Size(980, 650);
        Size = new Size(1180, 760);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.None;
        Icon = IconService.LoadAppIcon();
        if (_startupLaunch && _config.Settings.StartMinimizedToTray)
        {
            Opacity = 0;
            ShowInTaskbar = false;
        }

        _trayIcon = new ShellTrayIcon
        {
            Text = "TreasureChest 管理中心",
            Icon = Icon,
            Visible = true,
            ContextMenuStrip = BuildTrayMenu(),
        };
        _notifications = new NotificationService(_trayIcon, () => _config.Settings.NotificationsEnabled, _logger);
        _trayIcon.DoubleClick += (_, _) => RestoreFromTray();
        _trayIcon.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) RestoreFromTray(); };

        BuildShell();
        BuildSessionGrid();
        BuildMonitorThreadGrid();
        BuildToolGrid();
        BuildPluginGrid();
        WireEvents();

        _dpiLayout.CaptureBaseTree(this);
        _dpiLayoutReady = true;
        if (IsHandleCreated && !_initialDpiApplied)
            ApplyDpiLayout((int)NativeMethods.GetDpiForWindow(Handle), suggestedLocation: null, initial: true);

        _timer.Interval = Math.Clamp(_config.Settings.RefreshIntervalSeconds, 1, 300) * 1000;
        _timer.Tick += async (_, _) => await RefreshTimerDataAsync();
        if (!suppressInitialLoadForTests)
            Shown += async (_, _) => await InitializeAsync();
        Shown += (_, _) => ScheduleTransitionPrimes(_settingsPageVisible);
    }

    public static int ActivateMessage { get; set; }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        _dwmRoundedCornersActive = NativeMethods.TrySetWindowCornerPreference(
            Handle, WindowChromePresentation.DwmCornerPreference(WindowState));
        if (_dpiLayoutReady && !_initialDpiApplied)
            ApplyDpiLayout((int)NativeMethods.GetDpiForWindow(Handle), suggestedLocation: null, initial: true);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        AnimationRunner.ReportPaint(this);
        UiTheme.ReportThemePaint();
    }

    protected override void WndProc(ref Message m)
    {
        if (ActivateMessage != 0 && m.Msg == ActivateMessage)
        {
            RestoreFromTray();
            return;
        }
        if (m.Msg == NativeMethods.WmNcHitTest)
        {
            var windowBounds = NativeMethods.WindowRectangle(Handle);
            var dpi = (int)NativeMethods.GetDpiForWindow(Handle);
            var hit = DpiLayout.HitTest(windowBounds, NativeMethods.PointFromLParam(m.LParam), dpi, WindowState);
            if (hit != WindowResizeHit.Client)
            {
                m.Result = (IntPtr)(int)hit;
                return;
            }
        }

        var dpiChange = m.Msg == NativeMethods.WmDpiChanged;
        if (dpiChange) _handlingDpiChange = true;
        try
        {
            base.WndProc(ref m);
        }
        finally
        {
            if (dpiChange) _handlingDpiChange = false;
        }
        if (m.Msg == NativeMethods.WmSizing)
        {
            PerformLayout();
            Invalidate(true);
            Update();
        }
        if (m.Msg == NativeMethods.WmDisplayChange)
        {
            AnimationRunner.InvalidateDisplayRefreshRate();
            ScheduleWorkingAreaConstraint();
        }
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        if (_sidebarTransitionActive)
        {
            AnimationRunner.Cancel(_sidebarTransition, "sidebar");
            FinishSidebarTransition(_sidebarAnimationTarget);
        }
        base.OnDpiChanged(e);
        ApplyDpiLayout(e.DeviceDpiNew, e.SuggestedRectangle.Location, initial: false);
        ScheduleWorkingAreaConstraint();
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (_sidebarTransitionActive)
        {
            AnimationRunner.Cancel(_sidebarTransition, "sidebar");
            FinishSidebarTransition(_sidebarAnimationTarget);
        }
        UpdateWindowChrome();
        if (!_dpiLayoutReady || _applyingDpiLayout || _handlingDpiChange) return;
        if (_lastChromeState == FormWindowState.Maximized && WindowState == FormWindowState.Normal)
        {
            _applyingDpiLayout = true;
            try { Size = DpiLayout.Scale(_normalLogicalSize, _layoutDpi); }
            finally { _applyingDpiLayout = false; }
        }
        else if (WindowState == FormWindowState.Normal)
        {
            _normalLogicalSize = DpiLayout.Unscale(Size, _layoutDpi);
        }
        _lastChromeState = WindowState;
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!_allowExit && e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            HideToTray();
            return;
        }
        DisposeOwnedResources();
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) DisposeOwnedResources();
        base.Dispose(disposing);
    }

    private void DisposeOwnedResources()
    {
        if (_ownedResourcesDisposed) return;
        _ownedResourcesDisposed = true;
        _timer.Stop();
        AnimationRunner.Cancel(this);
        if (_sidebarTransition is not null) AnimationRunner.Cancel(_sidebarTransition);
        if (_themeEventHandlersAttached)
        {
            UiTheme.PaletteFrameChanged -= ThemeFrameChangedForSidebar;
            UiTheme.ModeChanged -= ThemeModeChangingForSidebar;
            _themeEventHandlersAttached = false;
        }
        _trayIcon.Visible = false;
        _trayIcon.Dispose();
        _ecosystemUpdates.Dispose();
        _sessionManager.Dispose();
        foreach (var control in new Control[]
                 {
                     _sessionGrid, _monitorThreadGrid, _toolGrid, _pluginGrid,
                     _notificationsSetting, _autoStartSetting, _startMinimizedSetting,
                     _refreshSetting, _autoUpdateCheckSetting, _updateIntervalSetting,
                     _themeModeSetting, _updateStatusLabel, _checkForUpdatesButton,
                 })
            if (!control.IsDisposed) control.Dispose();
        _windowChromeTips.Dispose();
        _timer.Dispose();
    }

    private void BuildShell()
    {
        var titleFont = UiTheme.CreateFont(10.5F, FontStyle.Bold);
        var titleBar = new Panel { Dock = DockStyle.Top, Height = 54, BackColor = UiTheme.TitleBar };
        var avatarHost = new Panel { Dock = DockStyle.Left, Width = 62, Padding = new Padding(14, 7, 8, 7), BackColor = UiTheme.TitleBar };
        avatarHost.Controls.Add(new AppAvatarControl { Dock = DockStyle.Fill });
        var title = new Label
        {
            Text = "TreasureChest  ·  本地工具管理中心",
            ForeColor = UiTheme.NavigationText,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft,
            Font = titleFont,
        };
        title.MouseDown += DragWindow;
        titleBar.MouseDown += DragWindow;
        _closeButton = new CaptionButton(CaptionButtonKind.Close, "关闭到托盘");
        _closeButton.Click += (_, _) => HideToTray();
        _maximizeButton = new CaptionButton(CaptionButtonKind.Maximize, "最大化窗口");
        _maximizeButton.Click += (_, _) => ToggleMaximize();
        _minimizeButton = new CaptionButton(CaptionButtonKind.Minimize, "最小化到任务栏");
        _minimizeButton.Click += (_, _) => MinimizeToTaskbar();
        title.DoubleClick += (_, _) => ToggleMaximize();
        titleBar.DoubleClick += (_, _) => ToggleMaximize();
        titleBar.Controls.Add(title);
        titleBar.Controls.Add(_minimizeButton);
        titleBar.Controls.Add(_maximizeButton);
        titleBar.Controls.Add(_closeButton);
        titleBar.Controls.Add(avatarHost);

        const int navigationWidth = 218;
        _expandedNavigationLogicalWidth = navigationWidth;
        _navigation = new SurfacePanel
        {
            Dock = DockStyle.Left,
            Width = navigationWidth,
            BackColor = UiTheme.Navigation,
            Padding = ApprovedMainWindowLayout.NavigationPadding,
            CornerRadiusLogical = 22,
            DrawBorder = true,
            DrawShadow = true,
        };
        var navFlow = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
            // The navigation host is an opaque part of the rounded sidebar. A
            // default FlowLayoutPanel background (SystemColors.Control) would
            // make every child button's rectangular client area visible even
            // when its own idle state has no plate.
            BackColor = UiTheme.Navigation,
        };
        navFlow.Controls.Add(NavButton("会话管理", NavigationGlyph.Sessions, BuildSessionsPage, navigationWidth - _navigation.Padding.Horizontal));
        navFlow.Controls.Add(NavButton("工具箱", NavigationGlyph.Tools, BuildToolsPage, navigationWidth - _navigation.Padding.Horizontal));
        navFlow.Controls.Add(NavButton("插件中心", NavigationGlyph.Plugins, BuildPluginsPage, navigationWidth - _navigation.Padding.Horizontal));
        navFlow.Controls.Add(NavButton("设置", NavigationGlyph.Settings, BuildSettingsPage, navigationWidth - _navigation.Padding.Horizontal));
        _sidebarToggle = new SidebarNavigationButton("收起侧栏", NavigationGlyph.Collapse)
        {
            Dock = DockStyle.Bottom,
            Height = ApprovedMainWindowLayout.SidebarToggleHeight,
            BackColor = UiTheme.Navigation,
            ForeColor = UiTheme.NavigationText,
            Cursor = Cursors.Hand,
            Font = UiTheme.CreateFont(UiTheme.NavigationFontSize),
            ShowContainer = true,
        };
        _sidebarToggle.Click += (_, _) => ToggleSidebar();
        _navigation.Controls.Add(navFlow);
        _navigation.Controls.Add(_sidebarToggle);

        _shellBody = new Panel
        {
            Dock = DockStyle.Fill,
            BackColor = UiTheme.Background,
            // The approved 1557x1010 composition scales to a 1180x760 shell whose
            // secondary cards begin directly below the 54px title bar and finish
            // around y=735.  Keep the horizontal gutter while reserving the soft
            // shadow room at the bottom.
            Padding = ApprovedMainWindowLayout.ShellPadding,
        };
        _navigationGap = new Panel { Dock = DockStyle.Left, Width = 14, BackColor = UiTheme.Background };
        _shellBody.Controls.Add(_content);
        _shellBody.Controls.Add(_navigationGap);
        _shellBody.Controls.Add(_navigation);
        Controls.Add(_shellBody);
        Controls.Add(titleBar);

        _sidebarTransition = new SidebarTransitionOverlay();
        _sidebarTransition.ToggleRequested += (_, _) => ToggleSidebar();
        Controls.Add(_sidebarTransition);
        UiTheme.PaletteFrameChanged += ThemeFrameChangedForSidebar;
        UiTheme.ModeChanged += ThemeModeChangingForSidebar;
        _themeEventHandlersAttached = true;

        _resizeGrip = new ResizeGripOverlay();
        Controls.Add(_resizeGrip);
        _resizeGrip.BringToFront();
        UpdateWindowChrome();
    }

    private Button NavButton(string text, NavigationGlyph glyph, Func<Control> pageFactory, int width)
    {
        Control? page = null;
        var button = new SidebarNavigationButton(text, glyph)
        {
            Width = width,
            Height = 60,
            Margin = new Padding(0, 0, 0, 8),
            FlatStyle = FlatStyle.Flat,
            FlatAppearance = { BorderSize = 0 },
            BackColor = UiTheme.Navigation,
            ForeColor = UiTheme.NavigationText,
            TextAlign = ContentAlignment.MiddleLeft,
            Cursor = Cursors.Hand,
            Font = UiTheme.CreateFont(UiTheme.NavigationFontSize),
        };
        _navigationButtons.Add(button);
        void ShowPage()
        {
            foreach (Control existing in _content.Controls) existing.Visible = false;
            if (page is null || page.IsDisposed)
            {
                page = pageFactory();
                page.Dock = DockStyle.Fill;
                PreparePageForCurrentDpi(page);
                _content.Controls.Add(page);
            }
            else
            {
                // Hidden cached pages are excluded from intermediate palette
                // painting.  Reapply the immutable current endpoint immediately
                // before exposing one; geometry was already captured and follows
                // the form's DPI tree while hidden.
                UiTheme.ApplyCurrentTheme(page);
            }
            page.Visible = true;
            page.BringToFront();
            _settingsPageVisible = string.Equals(text, "设置", StringComparison.Ordinal);
            ScheduleTransitionPrimes(_settingsPageVisible);
        }
        button.Click += (_, _) =>
        {
            if (_activeNav is not null) _activeNav.IsSelected = false;
            _activeNav = button;
            button.IsSelected = true;
            ShowPage();
        };
        if (_activeNav is null)
        {
            _activeNav = button;
            button.IsSelected = true;
            ShowPage();
        }
        return button;
    }

    private void ScheduleTransitionPrimes(bool includeTheme, bool includeSidebar = true)
    {
        _settingsPageVisible = includeTheme;
        if (_transitionPrimeScheduled || IsDisposed || !IsHandleCreated) return;
        _transitionPrimeScheduled = true;
        try
        {
            BeginInvoke(() =>
            {
                _transitionPrimeScheduled = false;
                if (IsDisposed || !Visible || _sidebarTransitionActive || UiTheme.IsTransitioning) return;
                if (includeSidebar) PrimeSidebarTransitionFrames();
                if (_settingsPageVisible) UiTheme.PrimeTransitionFrames(force: true);
            });
        }
        catch (InvalidOperationException)
        {
            _transitionPrimeScheduled = false;
        }
    }

    private void PrimeSidebarTransitionFrames()
    {
        if (IsDisposed || !_shellBody.Visible || !_shellBody.IsHandleCreated ||
            _sidebarTransitionActive || UiTheme.IsTransitioning) return;
        var stableProgress = _sidebarCollapseProgress >= 0.5D ? 1D : 0D;
        if (Math.Abs(_sidebarCollapseProgress - stableProgress) > 0.000001D) return;

        Bitmap? currentFrame = null;
        Bitmap? oppositeFrame = null;
        Bitmap? navigationMotionFrame = null;
        var currentGeometry = CurrentSidebarGeometry();
        var oppositeGeometry = currentGeometry;
        var redrawSuspended = false;
        try
        {
            currentFrame = SidebarTransitionOverlay.CaptureVisibleSurfaceForTransition(_shellBody);
            NativeMethods.SendMessage(_shellBody.Handle, NativeMethods.WmSetRedraw, IntPtr.Zero, IntPtr.Zero);
            redrawSuspended = true;
            ApplySidebarEndpointLayout(1D - stableProgress);
            oppositeGeometry = CurrentSidebarGeometry();
            oppositeFrame = SidebarTransitionOverlay.CaptureWholeSurfaceSnapshot(_shellBody);
            // Intermediate geometry must never crop ClearType labels into half
            // glyphs. Capture one expanded-width navigation surface with the
            // labels already hidden; the overlay preserves its icons, rounded
            // state surfaces and right edge while only its width moves.
            ApplySidebarEndpointLayout(0D);
            foreach (var button in _navigationButtons) button.CollapseProgress = 1D;
            _sidebarToggle.CollapseProgress = 1D;
            _shellBody.PerformLayout();
            navigationMotionFrame = SidebarTransitionOverlay.CaptureWholeSurfaceSnapshot(_shellBody);
            ApplySidebarEndpointLayout(stableProgress);

            if (stableProgress <= 0D)
                _sidebarTransition.Prime(_shellBody, currentFrame, oppositeFrame, navigationMotionFrame,
                    currentGeometry, oppositeGeometry, UiTheme.VisualFrameId);
            else
                _sidebarTransition.Prime(_shellBody, oppositeFrame, currentFrame, navigationMotionFrame,
                    oppositeGeometry, currentGeometry, UiTheme.VisualFrameId);
            currentFrame = null;
            oppositeFrame = null;
            navigationMotionFrame = null;
        }
        finally
        {
            if (Math.Abs(_sidebarCollapseProgress - stableProgress) <= 0.000001D)
                ApplySidebarEndpointLayout(stableProgress);
            if (redrawSuspended && _shellBody.IsHandleCreated)
                NativeMethods.SendMessage(_shellBody.Handle, NativeMethods.WmSetRedraw, (IntPtr)1, IntPtr.Zero);
            currentFrame?.Dispose();
            oppositeFrame?.Dispose();
            navigationMotionFrame?.Dispose();
            _shellBody.Invalidate(true);
        }
    }

    private void ToggleSidebar()
    {
        var start = _sidebarCollapseProgress;
        var target = _sidebarTransitionActive
            ? 1D - _sidebarAnimationTarget
            : start >= 0.5D ? 0D : 1D;
        _sidebarAnimationTarget = target;
        var startingNewTransition = !_sidebarTransitionActive;
        if (startingNewTransition) BeginSidebarTransition(start);
        // Do not make the user wait for the composition worker's first pulse.
        // Commit a tiny geometry delta synchronously after the immutable source
        // surface is visible.  It is large enough to be observable (roughly
        // 3 logical pixels at the navigation edge), but small enough to remain
        // on the same 280 ms curve.  A rapid reversal already has a visible
        // timeline, so it keeps its exact current progress with no jump.
        var timelineStart = start;
        if (startingNewTransition && Math.Abs(target - start) > 0.000001D &&
            Math.Abs(_sidebarTransition.Progress - start) <= 0.000001D)
        {
            var immediateDistance = Math.Min(Math.Abs(target - start), 0.025D);
            timelineStart = start + Math.Sign(target - start) * immediateDistance;
            _sidebarCollapseProgress = timelineStart;
            _sidebarTransition.SetProgress(timelineStart, commitSynchronously: false);
        }
        // The compositor is the only control that paints during the transition,
        // therefore it also owns the timeline and its actual OnPaint telemetry.
        var remainingDuration = Math.Max(1, (int)Math.Round(
            AnimationTokens.ComplexDurationMs * Math.Abs(target - timelineStart)));
        var generation = ++_sidebarTimelineGeneration;
        if (!startingNewTransition)
        {
            // A direction change can be requested from inside ProgressChanged.
            // Freeze and publish that exact visible origin before the replacement
            // callback is posted.  The incremented generation below makes every
            // later pulse from the previous leg a no-op, so it cannot advance one
            // extra compositor frame while BeginInvoke is still queued.
            _sidebarCollapseProgress = timelineStart;
            _sidebarTransition.SetProgress(timelineStart);
        }
        void StartTimeline()
        {
            if (generation != _sidebarTimelineGeneration || !_sidebarTransitionActive) return;
            AnimationRunner.Start(_sidebarTransition, "sidebar", remainingDuration, progress =>
            {
                if (generation != _sidebarTimelineGeneration || !_sidebarTransitionActive) return;
                _sidebarCollapseProgress = timelineStart + (target - timelineStart) * progress;
                _sidebarTransition.SetProgress(_sidebarCollapseProgress);
            }, () =>
            {
                if (generation == _sidebarTimelineGeneration) FinishSidebarTransition(target);
            });
            // Start() replaces the previous direction without restarting the
            // compositor worker.  Commit the captured origin once before its
            // first timed pulse so a rapid reversal exposes the exact last
            // painted progress rather than appearing to jump by one refresh
            // interval.  The disabled-animation path completes synchronously
            // above, so it must not restore this intermediate origin.
            if (AnimationTokens.Enabled && generation == _sidebarTimelineGeneration &&
                _sidebarTransitionActive)
            {
                _sidebarCollapseProgress = timelineStart;
                _sidebarTransition.SetProgress(timelineStart);
            }
        }
        try { BeginInvoke(StartTimeline); }
        catch (InvalidOperationException) { StartTimeline(); }
    }

    private void BeginSidebarTransition(double sourceProgress)
    {
        if (!_sidebarTransition.HasPrimedEndpoints(_shellBody.ClientSize, UiTheme.VisualFrameId))
            PrimeSidebarTransitionFrames();
        var shellBounds = _shellBody.Bounds;
        _sidebarTransition.Begin(
            _shellBody, shellBounds, CurrentSidebarGeometry(), sourceProgress,
            UiTheme.Background, UiTheme.VisualFrameId);
        var targetProgress = sourceProgress <= 0.000001D ? 1D : 0D;
        _sidebarTransition.SetEndpointGeometry(targetProgress >= 0.5D,
            SidebarGeometryAt(targetProgress));
        _sidebarTransition.SealSource(SidebarToggleBoundsInShell());
        _sidebarShellWasEnabled = _shellBody.Enabled;
        // The opaque compositor is already the top-most input surface for the
        // 280 ms transition. Disabling the complete shell recursively sends
        // WM_ENABLE/repaint work to every child HWND before the first frame and
        // was the remaining source of the visible click delay.
        _sidebarTransitionActive = true;
        _shellBody.Visible = false;
        _resizeGrip.BringToFront();
    }

    private void ApplySidebarEndpointLayout(double progress)
    {
        progress = Math.Clamp(progress, 0D, 1D);
        var expandedWidth = DpiLayout.Scale(_expandedNavigationLogicalWidth, _layoutDpi);
        var collapsedWidth = DpiLayout.Scale(82, _layoutDpi);
        _shellBody.SuspendLayout();
        try
        {
            _navigation.Width = (int)Math.Round(expandedWidth + (collapsedWidth - expandedWidth) * progress);
            foreach (var button in _navigationButtons) button.CollapseProgress = progress;
            _sidebarToggle.CollapseProgress = progress;
        }
        finally
        {
            _shellBody.ResumeLayout(performLayout: true);
        }
        _shellBody.PerformLayout();
    }

    private SidebarFrameGeometry CurrentSidebarGeometry() => new(
        _navigation.Bounds, _navigationGap.Bounds, _content.Bounds);

    private SidebarFrameGeometry SidebarGeometryAt(double progress)
    {
        progress = Math.Clamp(progress, 0D, 1D);
        var current = CurrentSidebarGeometry();
        var expandedWidth = DpiLayout.Scale(_expandedNavigationLogicalWidth, _layoutDpi);
        var collapsedWidth = DpiLayout.Scale(82, _layoutDpi);
        var navigation = current.Navigation with
        {
            Width = (int)Math.Round(expandedWidth + (collapsedWidth - expandedWidth) * progress),
        };
        var gap = current.Gap with { X = navigation.Right };
        var content = current.Content with
        {
            X = gap.Right,
            Width = Math.Max(1, _shellBody.ClientSize.Width - gap.Right),
        };
        return new SidebarFrameGeometry(navigation, gap, content);
    }

    private Rectangle SidebarToggleBoundsInShell()
    {
        var screen = _sidebarToggle.RectangleToScreen(_sidebarToggle.ClientRectangle);
        var shellScreen = _shellBody.PointToScreen(Point.Empty);
        return new Rectangle(screen.X - shellScreen.X, screen.Y - shellScreen.Y, screen.Width, screen.Height);
    }

    private void FinishSidebarTransition(double target)
    {
        _sidebarTimelineGeneration++;
        _sidebarCollapseProgress = target;
        ApplySidebarEndpointLayout(target);
        _shellBody.Enabled = _sidebarShellWasEnabled;
        // Repaint the endpoint underneath the still-visible compositor, then
        // reveal it in one commit instead of exposing a partially repainted tree.
        _shellBody.Visible = true;
        _sidebarTransition.BringToFront();
        _resizeGrip.BringToFront();
        _shellBody.Refresh();
        _sidebarTransition.Finish();
        _sidebarTransitionActive = false;
        _shellBody.Invalidate(true);
        // Finish() preserves the exact two immutable endpoint frames that were
        // just shown. Re-running the live-tree endpoint capture immediately
        // after the animation is both redundant and observable to PrintWindow
        // or native children despite WM_SETREDRAW. Only refresh the independent
        // theme cache here; page/theme changes still request a full sidebar
        // re-prime through their existing call sites.
        ScheduleTransitionPrimes(_settingsPageVisible, includeSidebar: false);
    }

    private void ThemeFrameChangedForSidebar(object? sender, EventArgs e)
    {
        // ModeChanged commits the geometry before the first new palette frame;
        // a visible transition surface therefore never retains a stale palette.
        if (_sidebarTransitionActive) FinishSidebarTransition(_sidebarAnimationTarget);
        if (!UiTheme.IsTransitioning) ScheduleTransitionPrimes(_settingsPageVisible);
    }

    private void ThemeModeChangingForSidebar(object? sender, EventArgs e)
    {
        if (!_sidebarTransitionActive) return;
        AnimationRunner.Cancel(_sidebarTransition, "sidebar");
        FinishSidebarTransition(_sidebarAnimationTarget);
    }

    private Control BuildSessionsPage()
    {
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.Percent, ApprovedMainWindowLayout.MonitorSectionPercent));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 100F - ApprovedMainWindowLayout.MonitorSectionPercent));
        body.Controls.Add(BuildMonitorThreadPanel(), 0, 0);
        body.Controls.Add(BuildSessionPanel(), 0, 1);

        var noPageActions = new Panel { Dock = DockStyle.Top, Height = 0, Visible = false };
        return Page("会话管理", "上方管理飞书机器人正在监测的 Codex 项目与会话；下方管理后台服务。", noPageActions, body);
    }

    private Control BuildMonitorThreadPanel()
    {
        var panel = new SurfacePanel { Dock = DockStyle.Fill, Padding = ApprovedMainWindowLayout.MonitorSectionPadding, Margin = new Padding(0, 0, 0, 2), DrawBorder = false };
        var heading = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            MinimumSize = new Size(0, 44),
            ColumnCount = 2,
            RowCount = 1,
        };
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        heading.Controls.Add(new Label
        {
            Text = "项目监测列表",
            AutoSize = true,
            Font = UiTheme.CreateFont(UiTheme.BodyFontSize, FontStyle.Bold),
            ForeColor = UiTheme.Text,
            Anchor = AnchorStyles.Left,
            Margin = new Padding(2, 10, 8, 8),
        }, 0, 0);
        var monitorActions = new FlowLayoutPanel
        {
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            WrapContents = false,
            FlowDirection = FlowDirection.RightToLeft,
            Anchor = AnchorStyles.Right,
            Margin = Padding.Empty,
        };
        monitorActions.Controls.Add(MinimumWidth(ActionButton("管理监测", ManageProjectMonitors, true, ButtonGlyph.Monitor), 108));
        monitorActions.Controls.Add(MinimumWidth(ActionButton("监测设置", ManageProjectMonitorSettingsAsync, false, ButtonGlyph.Settings), 126));
        monitorActions.Controls.Add(MinimumWidth(ActionButton("在 Codex 打开", OpenMonitorThread, false, ButtonGlyph.OpenExternal), 150));
        monitorActions.Controls.Add(MinimumWidth(ActionButton("刷新", PopulateMonitorThreadGridAsync, false, ButtonGlyph.Refresh), 92));
        heading.Controls.Add(monitorActions, 1, 0);
        var monitorGridWell = new GridWellPanel { Dock = DockStyle.Fill };
        monitorGridWell.Controls.Add(_monitorThreadGrid);
        panel.Controls.Add(monitorGridWell);
        panel.Controls.Add(heading);
        return panel;
    }

    private Control BuildSessionPanel()
    {
        var panel = new SurfacePanel { Dock = DockStyle.Fill, Padding = ApprovedMainWindowLayout.SessionSectionPadding, Margin = new Padding(0, 2, 0, 0), DrawBorder = false };
        var heading = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            MinimumSize = new Size(0, 44),
            ColumnCount = 2,
            RowCount = 1,
        };
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        heading.Controls.Add(new Label
        {
            Text = "后台服务",
            AutoSize = true,
            Font = UiTheme.CreateFont(UiTheme.BodyFontSize, FontStyle.Bold),
            ForeColor = UiTheme.Text,
            Anchor = AnchorStyles.Left,
            Margin = new Padding(2, 10, 8, 8),
        }, 0, 0);

        var sessionActions = new FlowLayoutPanel
        {
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            WrapContents = false,
            FlowDirection = FlowDirection.RightToLeft,
            Anchor = AnchorStyles.Right,
            Margin = Padding.Empty,
        };
        sessionActions.Controls.Add(MinimumWidth(ActionButton("删除", DeleteSession, false, ButtonGlyph.Delete), 82));
        sessionActions.Controls.Add(MinimumWidth(ActionButton("编辑", EditSession, false, ButtonGlyph.Edit), 82));
        _resetAlertHistoryButton = MinimumWidth(ActionButton("预警记录", ShowResetAlertHistoryAsync, true, ButtonGlyph.Monitor), 132);
        sessionActions.Controls.Add(_resetAlertHistoryButton);
        _ = UpdateResetAlertHistoryLinkAsync();
        sessionActions.Controls.Add(MinimumWidth(ActionButton("立即刷新", async () => await RefreshSessionsAsync(force: true), false, ButtonGlyph.Refresh), 112));
        _sessionPowerButton = MinimumWidth(ActionButton("启动", ToggleSelectedSessionsAsync, false, ButtonGlyph.Play), 90);
        sessionActions.Controls.Add(_sessionPowerButton);
        // Returning to this page creates a fresh button while the existing grid
        // already contains the authoritative session state. Synchronize in the
        // same construction frame instead of showing a transient default “启动”.
        UpdateSessionPowerButton();
        heading.Controls.Add(sessionActions, 1, 0);

        var sessionGridWell = new GridWellPanel { Dock = DockStyle.Fill };
        sessionGridWell.Controls.Add(_sessionGrid);
        sessionGridWell.Controls.Add(_resetAlertHoverTip);
        _resetAlertHoverTip.BringToFront();
        panel.Controls.Add(sessionGridWell);
        panel.Controls.Add(heading);
        return panel;
    }

    private Control BuildToolsPage()
    {
        var actions = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 48 };
        actions.Controls.Add(ActionButton("运行工具", async () => await RunSelectedToolAsync(), true, ButtonGlyph.Play));
        actions.Controls.Add(ActionButton("添加工具", AddTool, false, ButtonGlyph.Add));
        actions.Controls.Add(ActionButton("编辑", EditTool, false, ButtonGlyph.Edit));
        actions.Controls.Add(ActionButton("删除", DeleteTool, false, ButtonGlyph.Delete));
        actions.Controls.Add(ActionButton("打开工具目录", OpenSelectedToolDirectory, false, ButtonGlyph.OpenExternal));
        var gridWell = GridWellPanel.Wrap(_toolGrid);
        return Page("工具箱", "把常用脚本、快捷方式、程序和目录集中到一个入口。", actions, gridWell);
    }

    private Control BuildPluginsPage()
    {
        var actions = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 48 };
        actions.Controls.Add(ActionButton("重新扫描", async () => await ScanPluginsAsync(), true, ButtonGlyph.Refresh));
        actions.Controls.Add(ActionButton("打开插件目录", () => OpenPath(_pluginCatalog.PluginsRoot), false, ButtonGlyph.OpenExternal));
        actions.Controls.Add(ActionButton("打开开发说明", () => OpenPath(Path.Combine(AppPaths.Root, "PLUGIN_DEVELOPMENT.md")), false, ButtonGlyph.OpenExternal));
        var gridWell = GridWellPanel.Wrap(_pluginGrid);
        return Page("插件中心", "每个插件都是独立目录；取消勾选即可停用，不修改插件自身文件。", actions, gridWell);
    }

    private Control BuildSettingsPage()
    {
        var stack = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
        };
        stack.Controls.Add(UiTheme.Heading("设置"));
        stack.Controls.Add(new Label { Text = "所有配置均保存在 TreasureChest 目录，不上传到外部服务。", AutoSize = true, ForeColor = UiTheme.Muted, Margin = new Padding(0, 6, 0, 24) });
        var appearanceHeading = UiTheme.Heading("外观", 13F);
        appearanceHeading.Margin = new Padding(0, 0, 0, 8);
        stack.Controls.Add(appearanceHeading);
        stack.Controls.Add(new Label
        {
            Text = "日间与夜间使用同一套布局；切换会立即应用到当前所有窗口，并在重启后保持。",
            AutoSize = true,
            ForeColor = UiTheme.Muted,
            Margin = new Padding(0, 0, 0, 8),
        });
        _themeModeSetting.Margin = new Padding(0, 0, 0, 18);
        stack.Controls.Add(_themeModeSetting);
        foreach (var check in new[] { _notificationsSetting, _autoStartSetting, _startMinimizedSetting })
        {
            check.Font = UiTheme.CreateFont(10F);
            check.Margin = new Padding(0, 8, 0, 8);
            stack.Controls.Add(check);
        }
        var refresh = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, 10, 0, 20) };
        refresh.Controls.Add(new Label { Text = "状态刷新间隔（秒）", AutoSize = true, Margin = new Padding(0, 7, 12, 0) });
        refresh.Controls.Add(_refreshSetting);
        stack.Controls.Add(refresh);
        var updateHeading = UiTheme.Heading("生态更新", 13F);
        updateHeading.Margin = new Padding(0, 16, 0, 4);
        stack.Controls.Add(updateHeading);
        stack.Controls.Add(new Label
        {
            Text = "一次更新会同时升级指令使用、进度监测／飞书机器人、百宝箱及配套文件。",
            AutoSize = true,
            ForeColor = UiTheme.Muted,
            Margin = new Padding(0, 4, 0, 10),
        });
        _autoUpdateCheckSetting.Font = UiTheme.CreateFont(10F);
        _autoUpdateCheckSetting.Margin = new Padding(0, 6, 0, 8);
        stack.Controls.Add(_autoUpdateCheckSetting);
        var updateInterval = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, 4, 0, 8) };
        updateInterval.Controls.Add(new Label { Text = "自动检查间隔（小时）", AutoSize = true, Margin = new Padding(0, 7, 12, 0) });
        updateInterval.Controls.Add(_updateIntervalSetting);
        stack.Controls.Add(updateInterval);
        _updateStatusLabel.Text = $"当前生态版本：{_ecosystemInstallation.DisplayVersion}\n{_ecosystemInstallation.ManagementMessage}";
        _updateStatusLabel.Margin = new Padding(0, 2, 0, 6);
        stack.Controls.Add(_updateStatusLabel);
        var updateActions = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, 0, 0, 18) };
        updateActions.Controls.Add(_checkForUpdatesButton);
        updateActions.Controls.Add(ActionButton("打开官方发布页", () =>
            Process.Start(new ProcessStartInfo(EcosystemUpdateService.ManualReleasePage) { UseShellExecute = true })));
        stack.Controls.Add(updateActions);
        var actions = new FlowLayoutPanel { AutoSize = true };
        actions.Controls.Add(ActionButton("导出配置", ExportConfig));
        actions.Controls.Add(ActionButton("导入配置", ImportConfig));
        actions.Controls.Add(ActionButton("恢复默认", ResetConfig));
        actions.Controls.Add(ActionButton("打开日志", () => OpenPath(_logger.Path)));
        actions.Controls.Add(ActionButton("打开应用目录", () => OpenPath(AppPaths.Root)));
        stack.Controls.Add(actions);
        return new ScrollSurfacePage(stack);
    }

    private static Control Page(string heading, string subheading, Control actions, Control body)
    {
        var page = new Panel { Dock = DockStyle.Fill, BackColor = UiTheme.Background, Padding = Padding.Empty };
        var card = new SurfacePanel
        {
            Dock = DockStyle.Fill,
            BackColor = UiTheme.Surface,
            Padding = new Padding(24),
            CornerRadiusLogical = 24,
            DrawShadow = true,
        };
        var header = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            MinimumSize = new Size(0, ApprovedMainWindowLayout.PageHeaderHeight),
            ColumnCount = 1,
            RowCount = 2,
            Padding = new Padding(0, 0, 0, 16),
        };
        var title = UiTheme.Heading(heading, UiTheme.PageTitleFontSize);
        title.Margin = Padding.Empty;
        var subtitle = new Label { Text = subheading, AutoSize = true, ForeColor = UiTheme.Muted, Margin = new Padding(2, 8, 0, 0) };
        header.Controls.Add(title, 0, 0);
        header.Controls.Add(subtitle, 0, 1);
        body.Dock = DockStyle.Fill;
        card.Controls.Add(body);
        card.Controls.Add(actions);
        card.Controls.Add(header);
        page.Controls.Add(card);
        return page;
    }

    private static Button ActionButton(string text, Action action, bool primary = false, ButtonGlyph glyph = ButtonGlyph.None)
    {
        var button = UiTheme.Button(text, primary, glyph);
        button.Click += (_, _) => action();
        return button;
    }

    private static Button ActionButton(string text, Func<Task> action, bool primary = false, ButtonGlyph glyph = ButtonGlyph.None)
    {
        var button = UiTheme.Button(text, primary, glyph);
        button.Click += async (_, _) =>
        {
            button.Enabled = false;
            try { await action(); }
            finally { button.Enabled = true; }
        };
        return button;
    }

    private static Button MinimumWidth(Button button, int logicalWidth)
    {
        button.MinimumSize = new Size(logicalWidth, UiTheme.ButtonHeight);
        return button;
    }

    private static DataGridView CreateGrid()
    {
        var grid = new LiveResizableDataGridView
        {
            Dock = DockStyle.Fill,
            BackgroundColor = UiTheme.Surface,
            BorderStyle = BorderStyle.None,
            AllowUserToAddRows = false,
            AllowUserToDeleteRows = false,
            AllowUserToResizeColumns = true,
            AllowUserToResizeRows = false,
            AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.None,
            AutoGenerateColumns = false,
            MultiSelect = true,
            SelectionMode = DataGridViewSelectionMode.FullRowSelect,
            RowHeadersVisible = false,
            ReadOnly = true,
            EnableHeadersVisualStyles = false,
            ColumnHeadersHeight = 46,
            RowTemplate = { Height = DrawerVisualMetrics.ForDpi(DpiLayout.BaselineDpi).RowHeight },
            GridColor = UiTheme.GridLine,
            ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = UiTheme.Surface, ForeColor = UiTheme.Text,
                SelectionBackColor = UiTheme.Surface, SelectionForeColor = UiTheme.Text,
                Font = UiTheme.CreateFont(style: FontStyle.Bold), Alignment = DataGridViewContentAlignment.MiddleLeft,
                Padding = new Padding(10, 0, 10, 0),
            },
            DefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = UiTheme.Surface, ForeColor = UiTheme.Text, SelectionBackColor = UiTheme.AccentSoft,
                SelectionForeColor = UiTheme.Text, Padding = new Padding(10, 0, 10, 0),
            },
        };
        GridVisualStyler.Configure(grid);
        return grid;
    }

    private void BuildSessionGrid()
    {
        var columns = ApprovedMainWindowLayout.SessionColumns;
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "会话", Width = columns.Name });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn
        {
            Name = "Status", HeaderText = "状态", Width = columns.Status, MinimumWidth = columns.Status,
        });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Detail", HeaderText = "详情", Width = columns.Detail });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Auto", HeaderText = "自动策略", Width = columns.Auto });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Source", HeaderText = "来源", Width = columns.Source });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Checked", HeaderText = "检查时间", Width = columns.Checked });
        GridVisualStyler.Configure(_sessionGrid, statusColumn: "Status");
        _sessionGrid.ShowCellToolTips = true;
        _sessionGrid.SelectionChanged += (_, _) =>
        {
            if (!_populatingSessionGrid) UpdateSessionPowerButton();
        };
        _sessionGrid.CellMouseEnter += (_, e) => ShowResetAlertStatusTip(e.RowIndex, e.ColumnIndex);
        _sessionGrid.CellMouseLeave += (_, _) => HideResetAlertStatusTip();
        _sessionGrid.CellDoubleClick += async (_, e) =>
        {
            if (e.RowIndex < 0) return;
            if (_sessionGrid.Rows[e.RowIndex].Tag is ResetAlertLogicalRow) await ShowResetAlertHistoryAsync();
            else await StartSelectedSessionsAsync();
        };
    }

    private void ShowResetAlertStatusTip(int rowIndex, int columnIndex)
    {
        HideResetAlertStatusTip();
        if (rowIndex < 0 || rowIndex >= _sessionGrid.Rows.Count ||
            columnIndex < 0 || columnIndex >= _sessionGrid.Columns.Count) return;
        var row = _sessionGrid.Rows[rowIndex];
        if (row.Tag is not ResetAlertLogicalRow) return;
        var detail = Convert.ToString(row.Cells[2].Value)?.Trim();
        if (string.IsNullOrWhiteSpace(detail)) return;
        var parent = _resetAlertHoverTip.Parent;
        if (parent is null || !parent.IsHandleCreated) return;
        var rowBounds = _sessionGrid.GetRowDisplayRectangle(rowIndex, cutOverflow: true);
        if (rowBounds.Width <= 0 || rowBounds.Height <= 0) return;

        var dpi = _layoutDpi > 0 ? _layoutDpi : DpiLayout.BaselineDpi;
        var outerPadding = DpiLayout.Scale(12, dpi);
        var cardWidth = Math.Min(DpiLayout.Scale(560, dpi),
            Math.Max(DpiLayout.Scale(260, dpi), parent.ClientSize.Width - outerPadding * 2));
        var textWidth = Math.Max(1, cardWidth - DpiLayout.Scale(58, dpi));
        var measured = NativeButtonText.MeasureAtDpi(detail, _resetAlertHoverTip.Font, dpi,
            new Size(textWidth, DpiLayout.Scale(100, dpi)),
            TextFormatFlags.Left | TextFormatFlags.WordBreak | TextFormatFlags.NoPadding);
        var cardHeight = Math.Max(DpiLayout.Scale(44, dpi),
            measured.Height + DpiLayout.Scale(18, dpi));
        var rowTop = parent.PointToClient(_sessionGrid.PointToScreen(rowBounds.Location));
        var x = Math.Clamp(rowTop.X + outerPadding, outerPadding,
            Math.Max(outerPadding, parent.ClientSize.Width - cardWidth - outerPadding));
        var y = rowTop.Y - cardHeight - DpiLayout.Scale(5, dpi);
        if (y < outerPadding)
            y = Math.Min(parent.ClientSize.Height - cardHeight - outerPadding,
                rowTop.Y + rowBounds.Height + DpiLayout.Scale(5, dpi));
        if (y < 0) return;

        _resetAlertHoverTip.ShowDetail(detail, new Rectangle(x, y, cardWidth, cardHeight));
    }

    private void HideResetAlertStatusTip()
    {
        _resetAlertHoverTip.HideDetail();
    }

    private void BuildMonitorThreadGrid()
    {
        _monitorThreadGrid.MultiSelect = true;
        DrawerRowPresentation.ConfigureGrid(_monitorThreadGrid);
        var columns = ApprovedMainWindowLayout.MonitorColumns;
        _monitorThreadGrid.Columns.Add(ResizableTextColumn("Title", "会话名称", columns.Title, 120));
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Classification", HeaderText = "项目 / 个人归属", Width = columns.Classification });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Origin", HeaderText = "来源", Width = columns.Origin });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "LastActivity", HeaderText = "最后活动时间", Width = columns.LastActivity });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Remaining", HeaderText = "剩余有效时间", Width = columns.Remaining });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "ThreadId", HeaderText = "完整任务 ID", Width = columns.ThreadId });
        GridVisualStyler.Configure(_monitorThreadGrid, chatColumn: "Title");
        _monitorThreadGrid.CellDoubleClick += (_, e) =>
        {
            if (e.RowIndex >= 0 && _monitorThreadGrid.Rows[e.RowIndex].Tag is ProjectMonitorItem) OpenMonitorThread();
        };
        _monitorThreadGrid.CellMouseDown += (_, e) =>
        {
            if (DrawerAnimationController.IsToggleGesture(e.Button, e.Clicks)) ToggleMainMonitorDrawer(e.RowIndex);
        };
        _monitorThreadGrid.CellMouseEnter += (_, e) =>
            _monitorThreadGrid.Cursor = e.RowIndex >= 0 && _monitorThreadGrid.Rows[e.RowIndex].Tag is MainMonitorDrawerGroup
                ? Cursors.Hand : Cursors.Default;
        _monitorThreadGrid.CellMouseLeave += (_, _) => _monitorThreadGrid.Cursor = Cursors.Default;
        _monitorThreadGrid.KeyDown += (_, e) =>
        {
            if (e.KeyCode is not (Keys.Enter or Keys.Space) || _monitorThreadGrid.CurrentRow?.Tag is not MainMonitorDrawerGroup) return;
            ToggleMainMonitorDrawer(_monitorThreadGrid.CurrentRow.Index);
            e.Handled = true;
            e.SuppressKeyPress = true;
        };
    }

    private static DataGridViewTextBoxColumn ResizableTextColumn(string name, string headerText, int width, int minimumWidth) => new()
    {
        Name = name,
        HeaderText = headerText,
        AutoSizeMode = DataGridViewAutoSizeColumnMode.None,
        Width = width,
        MinimumWidth = minimumWidth,
        Resizable = DataGridViewTriState.True,
        SortMode = DataGridViewColumnSortMode.NotSortable,
    };

    private void BuildToolGrid()
    {
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "工具", Width = 190 });
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Category", HeaderText = "分类", Width = 130 });
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Target", HeaderText = "目标", Width = 480 });
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Source", HeaderText = "来源", Width = 110 });
        _toolGrid.CellDoubleClick += async (_, e) => { if (e.RowIndex >= 0) await RunSelectedToolAsync(); };
    }

    private void BuildPluginGrid()
    {
        _pluginGrid.ReadOnly = false;
        _pluginGrid.MultiSelect = false;
        _pluginGrid.Columns.Add(new DataGridViewCheckBoxColumn { Name = "Enabled", HeaderText = "启用", Width = 65 });
        _pluginGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "插件", Width = 190, ReadOnly = true });
        _pluginGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Version", HeaderText = "版本", Width = 80, ReadOnly = true });
        _pluginGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Author", HeaderText = "作者", Width = 120, ReadOnly = true });
        _pluginGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Contributions", HeaderText = "贡献", Width = 150, ReadOnly = true });
        _pluginGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Description", HeaderText = "说明 / 状态", Width = 480, ReadOnly = true });
        _pluginGrid.CurrentCellDirtyStateChanged += (_, _) => { if (_pluginGrid.IsCurrentCellDirty) _pluginGrid.CommitEdit(DataGridViewDataErrorContexts.Commit); };
        _pluginGrid.CellValueChanged += async (_, e) => await PluginValueChangedAsync(e.RowIndex, e.ColumnIndex);
    }

    private void WireEvents()
    {
        _sessionManager.StateChanged += (_, e) =>
        {
            if (IsDisposed) return;
            BeginInvoke(() =>
            {
                PopulateSessionGrid();
                if (e.Previous.Status != SessionStatus.Unknown)
                    _notifications.Show(e.Session.Name, $"状态变为：{StatusText(e.Current.Status)}\n{e.Current.Message}");
            });
        };
        _notificationsSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync();
        _autoStartSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync(applyAutoStart: true);
        _startMinimizedSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync();
        _refreshSetting.ValueChanged += async (_, _) => await SaveSettingsAsync();
        _autoUpdateCheckSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync();
        _updateIntervalSetting.ValueChanged += async (_, _) => await SaveSettingsAsync();
        _themeModeSetting.SelectedIndexChanged += async (_, _) => await SaveSettingsAsync();
        _themeModeSetting.MouseEnter += (_, _) => UiTheme.PrimeTransitionFrames();
    }

    private ContextMenuStrip BuildTrayMenu()
    {
        var menu = new ContextMenuStrip();
        UiTheme.StyleMenu(menu);
        var show = new ToolStripMenuItem("打开 TreasureChest");
        show.Click += (_, _) => RestoreFromTray();
        _pauseMenu.Click += (_, _) =>
        {
            _sessionManager.IsPaused = !_sessionManager.IsPaused;
            _pauseMenu.Text = _sessionManager.IsPaused ? "恢复状态监控" : "暂停状态监控";
            _notifications.Show("TreasureChest", _sessionManager.IsPaused ? "状态监控已暂停。" : "状态监控已恢复。");
        };
        var exit = new ToolStripMenuItem("退出管理应用");
        exit.Click += (_, _) => ExitApplication();
        var exitGuardian = new ToolStripMenuItem("完整退出飞书守护（关闭远程救援）");
        exitGuardian.Click += async (_, _) =>
        {
            if (MessageBox.Show(this, "这会停止飞书业务和通信守护，并禁止 Windows 自动恢复。远程启动命令将无法接收。\n仅想暂停业务，请使用后台服务的停止按钮。确定完整退出？",
                "关闭远程救援", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
            exitGuardian.Enabled = false;
            try
            {
                await _sessionManager.ExitExternalControllerAsync();
                await RefreshSessionsAsync(force: true);
            }
            catch (Exception error) { ShowError("完整退出守护未完成", error); }
            finally { if (!IsDisposed) exitGuardian.Enabled = true; }
        };
        menu.Items.Add(show); menu.Items.Add(_pauseMenu); menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(exitGuardian); menu.Items.Add(exit);
        return menu;
    }

    private async Task InitializeAsync()
    {
        try
        {
            EcosystemUpdateService.CleanupStaleCache();
            LoadSettingsControls();
            await ScanPluginsAsync();
            await PopulateMonitorThreadGridAsync();
            await RefreshSessionsAsync(force: true);
            await _sessionManager.StartAutoStartSessionsAsync(_sessions);
            await RefreshResetAlertAsync(force: true);
            _timer.Start();
            if (_startupLaunch && _config.Settings.StartMinimizedToTray) HideToTray();
            else RestoreFromTray();
            _logger.Info("TreasureChest 主窗口初始化完成。");
            await CheckForUpdatesAsync(manual: false);
        }
        catch (Exception error)
        {
            _logger.Error("初始化失败：" + error);
            MessageBox.Show(this, error.Message, "TreasureChest 初始化失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private async Task ScanPluginsAsync()
    {
        await _pluginCatalog.ScanAsync();
        foreach (var plugin in _pluginCatalog.Plugins)
            if (!_config.PluginEnabled.ContainsKey(plugin.Manifest.Id))
                _config.PluginEnabled[plugin.Manifest.Id] = plugin.Manifest.EnabledByDefault;
        await _configStore.SaveAsync(_config);
        RebuildEffectiveItems();
        PopulateAllGrids();
    }

    private void RebuildEffectiveItems()
    {
        var enabledPlugins = _pluginCatalog.Plugins.Where(plugin => plugin.Error is null && IsPluginEnabled(plugin));
        _sessions = _config.Sessions.Concat(enabledPlugins.SelectMany(plugin => plugin.Sessions)).ToArray();
        _tools = _config.Tools.Concat(enabledPlugins.SelectMany(plugin => plugin.Tools)).ToArray();
    }

    private bool IsPluginEnabled(PluginDescriptor plugin) =>
        _config.PluginEnabled.TryGetValue(plugin.Manifest.Id, out var enabled) ? enabled : plugin.Manifest.EnabledByDefault;

    private void PopulateAllGrids()
    {
        PopulateSessionGrid(); PopulateToolGrid(); PopulatePluginGrid();
    }

    private async Task PopulateMonitorThreadGridAsync()
    {
        if (_refreshingProjectMonitors) return;
        _refreshingProjectMonitors = true;
        try
        {
            var selected = SelectedRows<ProjectMonitorItem>(_monitorThreadGrid)
                .Select(item => item.ThreadId).ToHashSet(StringComparer.OrdinalIgnoreCase);
            _projectMonitors = await _projectMonitorCli.ListAsync();
            RenderMonitorThreadGrid(selected);
            _lastProjectMonitorRefresh = DateTimeOffset.UtcNow;
        }
        catch (Exception error)
        {
            _logger.Error("读取项目监测列表失败：" + error);
            _projectMonitors = [];
            _monitorThreadGrid.Rows.Clear();
            var index = _monitorThreadGrid.Rows.Add("项目监测接口暂不可用", error.Message, "—", "—", "—", "—");
            var row = _monitorThreadGrid.Rows[index];
            row.DefaultCellStyle.ForeColor = UiTheme.Stopped;
            foreach (DataGridViewCell cell in row.Cells) cell.ToolTipText = error.Message;
        }
        finally { _refreshingProjectMonitors = false; }
    }

    private void RenderMonitorThreadGrid(IReadOnlySet<string>? selected = null)
    {
        selected ??= SelectedRows<ProjectMonitorItem>(_monitorThreadGrid)
            .Select(item => item.ThreadId).ToHashSet(StringComparer.OrdinalIgnoreCase);
        _monitorThreadGrid.Rows.Clear();
        var ordered = ProjectMonitorPresentation.OrderMonitors(_projectMonitors);
        foreach (var group in ordered.GroupBy(item => new MainMonitorDrawerGroup(
                     item.Origin.ToLowerInvariant(), ProjectMonitorPresentation.NormalizeClassification(item.Classification))))
        {
            var children = group.ToList();
            var drawer = group.Key;
            var expanded = _expandedMainMonitorGroups.Contains(drawer.StorageKey);
            var values = new object[_monitorThreadGrid.Columns.Count];
            for (var column = 1; column < values.Length; column++) values[column] = string.Empty;
            values[0] = ProjectMonitorPresentation.DrawerGroupTitle(drawer.Classification, children.Count, expanded);
            values[2] = drawer.Origin.Equals("manual", StringComparison.OrdinalIgnoreCase) ? "手动" : "自动";
            var headerIndex = _monitorThreadGrid.Rows.Add(values);
            var header = _monitorThreadGrid.Rows[headerIndex];
            header.Tag = drawer;
            header.ReadOnly = true;
            header.Height = DrawerVisualMetrics.ForDpi(_layoutDpi).RowHeight;
            DrawerRowPresentation.Apply(
                header,
                expanded ? DrawerRowKind.ExpandedGroup : DrawerRowKind.CollapsedGroup,
                _layoutDpi);
            header.Cells[0].ToolTipText = expanded ? "单击收起这个分组" : "单击展开这个分组";
            if (!expanded) continue;

            for (var childIndex = 0; childIndex < children.Count; childIndex++)
            {
                var item = children[childIndex];
                var title = item.Title.Length > 100 ? item.Title[..99] + "…" : item.Title;
                var index = _monitorThreadGrid.Rows.Add(
                    title,
                    ProjectMonitorPresentation.NormalizeClassification(item.Classification),
                    item.IsManual ? "手动" : "自动",
                    ProjectMonitorPresentation.FormatTimestamp(item.LastActivityAt),
                    ProjectMonitorPresentation.FormatRemaining(item),
                    item.ThreadId);
                var row = _monitorThreadGrid.Rows[index];
                row.Tag = item;
                DrawerRowPresentation.Apply(
                    row,
                    DrawerRowKind.Child,
                    _layoutDpi,
                    isLastChild: childIndex == children.Count - 1);
                row.Selected = selected.Contains(item.ThreadId);
                row.Cells[0].ToolTipText = item.Title;
                row.Cells[1].ToolTipText = ProjectMonitorPresentation.IsPersonal(item.Classification)
                    ? "Codex 个人对话"
                    : $"Codex 项目：{item.Classification}";
                row.Cells[2].ToolTipText = item.IsManual
                    ? "手动添加：长期保留，不参与 24 小时未回复自动移除。"
                    : "自动发现：有效期与自动移除由 FeiShuBOT 管理。";
                row.Cells[4].ToolTipText = item.IsManual
                    ? "手动监测项长期有效。"
                    : item.ExpiresAt.HasValue ? $"到期时间：{ProjectMonitorPresentation.FormatTimestamp(item.ExpiresAt)}" : "FeiShuBOT 未提供到期时间。";
                if (!item.IsManual && item.ExpiresAt <= DateTimeOffset.Now)
                    row.Cells[4].Style.ForeColor = UiTheme.Stopped;
            }
        }
        _monitorThreadGrid.ClearSelection();
    }

    private void ToggleMainMonitorDrawer(int rowIndex)
    {
        if (rowIndex < 0 || _monitorThreadGrid.Rows[rowIndex].Tag is not MainMonitorDrawerGroup drawer) return;
        var firstDisplayed = _monitorThreadGrid.FirstDisplayedScrollingRowIndex;
        var targetExpanded = DrawerAnimationController.NextExpandedState(_expandedMainMonitorGroups.Contains(drawer.StorageKey));
        if (!targetExpanded)
        {
            var children = DrawerAnimationController.ChildRowsAfter(_monitorThreadGrid, rowIndex);
            // Flip the logical destination at gesture time.  This lets a rapid
            // second click enter the expansion branch and replace the keyed
            // collapse from the rows' current visual progress.
            _expandedMainMonitorGroups.Remove(drawer.StorageKey);
            DrawerAnimationController.Collapse(_monitorThreadGrid, drawer.StorageKey, children, () =>
            {
                RenderMonitorThreadGrid();
                RestoreMainDrawerPosition(drawer, firstDisplayed);
            });
            return;
        }
        var reversingChildren = DrawerAnimationController.ChildRowsAfter(_monitorThreadGrid, rowIndex);
        _expandedMainMonitorGroups.Add(drawer.StorageKey);
        if (DrawerAnimationController.TryReverseToExpanded(
                _monitorThreadGrid, drawer.StorageKey, reversingChildren))
        {
            RestoreMainDrawerPosition(drawer, firstDisplayed);
            return;
        }
        RenderMonitorThreadGrid();
        var refreshed = RestoreMainDrawerPosition(drawer, firstDisplayed);
        if (refreshed is not null)
            DrawerAnimationController.Expand(
                _monitorThreadGrid,
                drawer.StorageKey,
                DrawerAnimationController.ChildRowsAfter(_monitorThreadGrid, refreshed.Index));
    }

    private DataGridViewRow? RestoreMainDrawerPosition(MainMonitorDrawerGroup drawer, int firstDisplayed)
    {
        var refreshedHeader = _monitorThreadGrid.Rows.Cast<DataGridViewRow>()
            .FirstOrDefault(row => row.Tag is MainMonitorDrawerGroup group &&
                group.StorageKey.Equals(drawer.StorageKey, StringComparison.CurrentCultureIgnoreCase));
        if (refreshedHeader is not null)
        {
            _monitorThreadGrid.CurrentCell = refreshedHeader.Cells[0];
            if (firstDisplayed >= 0 && _monitorThreadGrid.Rows.Count > 0)
            {
                try { _monitorThreadGrid.FirstDisplayedScrollingRowIndex = Math.Min(firstDisplayed, _monitorThreadGrid.Rows.Count - 1); }
                catch (InvalidOperationException) { }
            }
        }
        _monitorThreadGrid.Invalidate(true);
        return refreshedHeader;
    }

    private async Task AddMonitorThreadsAsync()
    {
        using var dialog = new ThreadIdDialog();
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        try
        {
            foreach (var id in dialog.ThreadIds) await _projectMonitorCli.AddAsync(id);
            await PopulateMonitorThreadGridAsync();
            _notifications.Show("项目监测", $"已手动添加 {dialog.ThreadIds.Count} 个任务；这些任务长期有效。互斥与热加载由 FeiShuBOT 处理。");
        }
        catch (Exception error)
        {
            await PopulateMonitorThreadGridAsync();
            ShowError("手动添加项目监测失败", error);
        }
    }

    private void ManageProjectMonitors()
    {
        using var dialog = new ProjectMonitorManagerDialog(
            _projectMonitorCli,
            _codexThreadCatalog,
            _sessionSearchCacheMode);
        dialog.ShowDialog(this);
        _ = PopulateMonitorThreadGridAsync();
    }

    private async Task ManageProjectMonitorSettingsAsync()
    {
        using var dialog = new ProjectMonitorSettingsDialog(_projectMonitorCli);
        if (dialog.ShowDialog(this) != DialogResult.OK || dialog.SavedSettings is null) return;
        await PopulateMonitorThreadGridAsync();
        var state = dialog.SavedSettings.AutoMonitoringEnabled ? "已开启" : "已关闭";
        var detail = dialog.SavedSettings.Changed == true ? $"自动监测{state}并已热生效。" : $"自动监测原本就是{state}状态。";
        _notifications.Show("项目监测设置", detail);
    }

    private async Task RemoveMonitorThreadsAsync()
    {
        var items = SelectedRows<ProjectMonitorItem>(_monitorThreadGrid).ToArray();
        if (items.Length == 0) { ShowHint("请先选择一个或多个项目监测任务。"); return; }
        if (MessageBox.Show(this,
                $"明确移除选中的 {items.Length} 个监测任务？\n\n此操作会由 FeiShuBOT 写入用户抑制记录，自动发现不会立即重新添加。\n不会删除或关闭 Codex 任务。",
                "确认明确移除",
                MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        try
        {
            foreach (var item in items) await _projectMonitorCli.RemoveAsync(item.ThreadId);
            await PopulateMonitorThreadGridAsync();
            _notifications.Show("项目监测", $"已明确移除 {items.Length} 个任务，并交由 FeiShuBOT 记录用户抑制。 ");
        }
        catch (Exception error)
        {
            await PopulateMonitorThreadGridAsync();
            ShowError("明确移除项目监测失败", error);
        }
    }

    private void OpenMonitorThread()
    {
        var item = SelectedRows<ProjectMonitorItem>(_monitorThreadGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个项目监测任务。"); return; }
        var result = _codexDesktopLauncher.TryOpen(item.ThreadId);
        if (!result.Opened) ShowHint(result.Message);
    }

    private async Task RefreshTimerDataAsync()
    {
        await RefreshSessionsAsync();
        await RefreshResetAlertAsync();
        if (DateTimeOffset.UtcNow - _lastProjectMonitorRefresh >= TimeSpan.FromSeconds(20))
            await PopulateMonitorThreadGridAsync();
        await CheckForUpdatesAsync(manual: false);
    }

    private async Task UpdateResetAlertHistoryLinkAsync()
    {
        try
        {
            var history = await _resetAlertDedup.ReadHistoryAsync();
            if (_resetAlertHistoryButton is { IsDisposed: false } button)
            {
                var unread = history.Count(x => !x.Read);
                button.Text = unread > 0 ? $"预警记录({(unread > 9 ? "9+" : unread.ToString())})" : "预警记录";
                button.AccessibleDescription = $"查看重置预警历史；{unread} 条未读";
            }
        }
        catch (Exception error)
        {
            if (_resetAlertHistoryButton is { IsDisposed: false } button)
            {
                button.Text = "预警记录 !";
                button.AccessibleDescription = "读取失败；点击查看错误详情";
            }
            _logger.Error("读取预警记录失败：" + error.Message);
        }
    }

    private async Task ShowResetAlertHistoryAsync()
    {
        try
        {
            var history = await _resetAlertDedup.ReadHistoryAsync();
            using var dialog = new Form
            {
                Text = "Codex 重置预警记录", Size = new Size(760, 560), MinimumSize = new Size(480, 320),
                StartPosition = FormStartPosition.CenterParent, ShowInTaskbar = false,
            };
            var text = new TextBox
            {
                Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical,
                Font = UiTheme.CreateFont(UiTheme.BodyFontSize),
                Text = history.Count == 0 ? "暂无预警记录。来源故障不代表已经发生重置。" :
                    string.Join("\r\n\r\n────────────\r\n\r\n", history.Select(x =>
                        $"{(x.Read ? "已读" : "未读")} · {x.Event.CreatedAt.ToLocalTime():yyyy-MM-dd HH:mm} · {(x.Event.ExpiresAt <= DateTimeOffset.Now ? "已过期，仅供回看" : "有效期内")}\r\n" +
                        x.Event.NotificationBody.Replace("\n", "\r\n") + "\r\n飞书投递状态：" + ResetAlertDeliveryLabel(x.Event.DeliveryState))),
            };
            var markRead = new Button { Text = "将本次显示的记录标为已读", Dock = DockStyle.Bottom, Height = 38, Enabled = history.Any(x => !x.Read) };
            markRead.Click += async (_, _) =>
            {
                markRead.Enabled = false;
                try
                {
                    await _resetAlertDedup.MarkReadAsync(history.Select(x => x.Event.EventIdentity!));
                    await UpdateResetAlertHistoryLinkAsync();
                    dialog.Close();
                }
                catch (Exception error) { markRead.Enabled = true; MessageBox.Show(dialog, error.Message, "保存已读状态失败"); }
            };
            dialog.Controls.Add(text);
            dialog.Controls.Add(markRead);
            dialog.Shown += (_, _) =>
            {
                text.Select(0, 0);
                text.ScrollToCaret();
            };
            dialog.ShowDialog(this);
        }
        catch (Exception error) { MessageBox.Show(this, error.Message, "读取预警记录失败"); }
    }

    private static string ResetAlertDeliveryLabel(string state) => state.ToLowerInvariant() switch
    {
        "pending" => "待发送", "retrying" => "重试中", "claimed" => "发送处理中",
        "delivered" or "confirmed" => "已送达", "uncertain" => "结果待确认",
        "rejected" => "发送被拒绝", "expired" => "已过期", _ => "暂无可确认的投递状态",
    };

    private async Task RefreshResetAlertAsync(
        bool force = false,
        CancellationToken cancellationToken = default)
    {
        if (_refreshingResetAlert) return;
        if (!_resetAlertRefreshGate.TryEnter(DateTimeOffset.UtcNow, force)) return;
        _refreshingResetAlert = true;
        try
        {
            var status = await _resetAlertCli.GetStatusAsync(cancellationToken);
            var events = status.Available
                ? await _resetAlertCli.GetLatestAsync(cancellationToken: cancellationToken)
                : [];
            var notifications = await _resetAlertDedup.SelectPendingAsync(events, DateTimeOffset.Now, cancellationToken);
            await UpdateResetAlertHistoryLinkAsync();
            _resetAlertStatus = status;
            _resetAlertFailure = null;
            _resetAlertCheckedAt = DateTimeOffset.Now;
            PopulateSessionGrid();
            foreach (var item in notifications)
            {
                var delivered = _notifications.Show("Codex 重置预警", item.NotificationBody);
                if (delivered.Succeeded && item.EventIdentity is { Length: > 0 } identity)
                    await _resetAlertDedup.MarkDeliveredAsync(identity, cancellationToken);
            }
        }
        catch (Exception error)
        {
            var failure = error.Message;
            if (!string.Equals(_resetAlertFailure, failure, StringComparison.Ordinal))
                _logger.Error("读取 Codex 重置预警失败：" + error.GetType().Name + "：" + failure);
            _resetAlertStatus = null;
            _resetAlertFailure = failure;
            _resetAlertCheckedAt = DateTimeOffset.Now;
            PopulateSessionGrid();
        }
        finally
        {
            _refreshingResetAlert = false;
        }
    }

    private void PopulateSessionGrid()
    {
        var selected = SelectedRows<SessionDefinition>(_sessionGrid).Select(item => item.Id).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var resetAlertSelected = _sessionGrid.SelectedRows.Cast<DataGridViewRow>()
            .Any(row => row.Tag is ResetAlertLogicalRow);
        _populatingSessionGrid = true;
        try
        {
            _sessionGrid.Rows.Clear();
            foreach (var session in _sessions)
            {
                var snapshot = _sessionManager.GetSnapshot(session);
                var auto = _sessionManager.IsExternallyControlled(session) ? "守护管理" : session.AutoStart || session.AutoRestart
                    ? $"{(session.AutoStart ? "自启" : "")} {(session.AutoRestart ? $"重启×{session.MaxRestartAttempts}" : "")}".Trim()
                    : "手动";
                var index = _sessionGrid.Rows.Add(SessionDisplayName(session), StatusText(snapshot.Status), snapshot.Message, auto,
                    string.IsNullOrWhiteSpace(session.SourcePluginId) ? "内置" : session.SourcePluginId,
                    snapshot.CheckedAt == DateTimeOffset.MinValue ? "—" : snapshot.CheckedAt.LocalDateTime.ToString("HH:mm:ss"));
                var row = _sessionGrid.Rows[index];
                row.Tag = session;
                row.Cells[1].Style.ForeColor = StatusColor(snapshot.Status);
                row.Selected = selected.Contains(session.Id);
            }
            AddResetAlertLogicalRow(resetAlertSelected);
            if (_sessionGrid.Rows.Count > 0 && _sessionGrid.SelectedRows.Count == 0)
            {
                _sessionGrid.CurrentCell = _sessionGrid.Rows[0].Cells[0];
                _sessionGrid.Rows[0].Selected = true;
            }
        }
        finally
        {
            _populatingSessionGrid = false;
        }
        UpdateSessionPowerButton();
    }

    private void AddResetAlertLogicalRow(bool selected)
    {
        var parent = _sessions.FirstOrDefault(IsFeiShuParentSession);
        var parentSnapshot = parent is null ? null : _sessionManager.GetSnapshot(parent);
        string status;
        string detail;
        Color statusColor;
        if (parentSnapshot is null)
        {
            status = "不可用";
            detail = "未找到同进程的指令使用（飞书）父服务";
            statusColor = UiTheme.Stopped;
        }
        else if (parentSnapshot.Status is SessionStatus.Stopped or SessionStatus.Disabled)
        {
            status = "已停止";
            detail = "与指令使用（飞书）共用进程；请通过父服务行启动";
            statusColor = UiTheme.Muted;
        }
        else if (parentSnapshot.Status == SessionStatus.Unknown)
        {
            status = "连接中";
            detail = "正在读取同一 FeiShuBOT 进程的预警模块状态";
            statusColor = UiTheme.Warning;
        }
        else if (parentSnapshot.Status == SessionStatus.Error)
        {
            status = "重连中";
            detail = "父服务状态暂不可确认；不会据此弹出停止通知";
            statusColor = UiTheme.Warning;
        }
        else if (_resetAlertStatus is { } resetStatus)
        {
            (status, detail) = ResetAlertPresentation.Describe(resetStatus, DateTimeOffset.Now);
            statusColor = status is "运行中" || status.StartsWith("下次 ", StringComparison.Ordinal)
                ? UiTheme.Running
                : status is "等待 08:00" ? UiTheme.Muted
                : status is "部分可用" or "投递结果未知" ? UiTheme.Warning
                : UiTheme.Stopped;
        }
        else
        {
            status = "不可用";
            detail = string.IsNullOrWhiteSpace(_resetAlertFailure) ? "预警状态尚未返回" : _resetAlertFailure;
            statusColor = UiTheme.Stopped;
        }

        var checkedAt = _resetAlertCheckedAt == DateTimeOffset.MinValue
            ? "—"
            : _resetAlertCheckedAt.LocalDateTime.ToString("HH:mm:ss");
        var index = _sessionGrid.Rows.Add("Codex 重置预警", status, detail, "随飞书", "内置·同进程", checkedAt);
        var row = _sessionGrid.Rows[index];
        row.Tag = ResetAlertLogicalRow.Instance;
        row.Cells[1].Style.ForeColor = statusColor;
        foreach (DataGridViewCell cell in row.Cells) cell.ToolTipText = detail;
        row.Selected = selected;
    }

    internal static string SessionDisplayName(SessionDefinition session) =>
        IsFeiShuParentSession(session) && session.Name is "Codex管理（飞书）" or "Codex 管理（飞书）"
            ? "指令使用（飞书）" : session.Name;

    private static bool IsFeiShuParentSession(SessionDefinition session)
    {
        if (string.IsNullOrWhiteSpace(session.WorkingDirectory)) return false;
        try
        {
            return Path.GetFullPath(session.WorkingDirectory)
                .Equals(Path.GetFullPath(AppPaths.ProgressNotificationRoot), StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception error) when (error is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return false;
        }
    }

    private void PopulateToolGrid()
    {
        _toolGrid.Rows.Clear();
        foreach (var tool in _tools.Where(item => item.Enabled))
        {
            var index = _toolGrid.Rows.Add(tool.Name, tool.Category, tool.TargetPath,
                string.IsNullOrWhiteSpace(tool.SourcePluginId) ? "内置" : tool.SourcePluginId);
            _toolGrid.Rows[index].Tag = tool;
        }
    }

    private void PopulatePluginGrid()
    {
        _loadingPlugins = true;
        try
        {
            _pluginGrid.Rows.Clear();
            foreach (var plugin in _pluginCatalog.Plugins)
            {
                var description = plugin.Error is null ? plugin.Manifest.Description : "无效：" + plugin.Error;
                var index = _pluginGrid.Rows.Add(IsPluginEnabled(plugin), plugin.Manifest.Name, plugin.Manifest.Version,
                    plugin.Manifest.Author, $"工具 {plugin.Tools.Count} / 会话 {plugin.Sessions.Count}", description);
                var row = _pluginGrid.Rows[index];
                row.Tag = plugin;
                if (plugin.Error is not null)
                {
                    row.Cells[0].ReadOnly = true;
                    row.DefaultCellStyle.ForeColor = UiTheme.Stopped;
                }
            }
        }
        finally { _loadingPlugins = false; }
    }

    private async Task PluginValueChangedAsync(int rowIndex, int columnIndex)
    {
        if (_loadingPlugins || rowIndex < 0 || columnIndex != 0) return;
        if (_pluginGrid.Rows[rowIndex].Tag is not PluginDescriptor plugin || plugin.Error is not null) return;
        var enabled = Convert.ToBoolean(_pluginGrid.Rows[rowIndex].Cells[0].Value);
        _config.PluginEnabled[plugin.Manifest.Id] = enabled;
        await _configStore.SaveAsync(_config);
        RebuildEffectiveItems();
        PopulateSessionGrid(); PopulateToolGrid();
        _notifications.Show("插件中心", $"{plugin.Manifest.Name} 已{(enabled ? "启用" : "停用")}。");
    }

    private async Task RefreshSessionsAsync(bool force = false)
    {
        if (_refreshing || (_sessionManager.IsPaused && !force)) return;
        _refreshing = true;
        try
        {
            if (force && _sessionManager.IsPaused)
            {
                var paused = _sessionManager.IsPaused;
                _sessionManager.IsPaused = false;
                try { await _sessionManager.RefreshAllAsync(_sessions); }
                finally { _sessionManager.IsPaused = paused; }
            }
            else await _sessionManager.RefreshAllAsync(_sessions);
            PopulateSessionGrid();
        }
        catch (Exception error) { ShowError("刷新状态失败", error); }
        finally { _refreshing = false; }
    }

    private async Task StartSelectedSessionsAsync()
    {
        var selected = SelectedRows<SessionDefinition>(_sessionGrid).ToArray();
        if (selected.Length == 0) { ShowHint("请先选择一个或多个会话。"); return; }
        foreach (var session in selected)
            try { await _sessionManager.StartAsync(session); }
            catch (Exception error) { ShowError($"启动“{SessionDisplayName(session)}”失败", error); }
        await _configStore.SaveAsync(_config);
        PopulateSessionGrid();
    }

    private async Task StopSelectedSessionsAsync()
    {
        var selected = SelectedRows<SessionDefinition>(_sessionGrid).ToArray();
        if (selected.Length == 0) { ShowHint("请先选择一个或多个会话。"); return; }
        foreach (var session in selected)
            try { await _sessionManager.StopAsync(session); }
            catch (Exception error) { ShowError($"停止“{SessionDisplayName(session)}”失败", error); }
        PopulateSessionGrid();
    }

    private async Task ToggleSelectedSessionsAsync()
    {
        if (SelectedRows<SessionDefinition>(_sessionGrid).Any() && SelectedSessionIsRunning())
            await StopSelectedSessionsAsync();
        else
            await StartSelectedSessionsAsync();
    }

    private bool SelectedSessionIsRunning() => _sessionGrid.SelectedRows.Cast<DataGridViewRow>()
        .Select(row => row.Tag as SessionDefinition)
        .Where(session => session is not null)
        .Any(session => _sessionManager.ShouldStop(session!));

    private void UpdateSessionPowerButton()
    {
        if (_sessionPowerButton is null) return;
        var selectedSessions = SelectedRows<SessionDefinition>(_sessionGrid).ToArray();
        var logicalOnly = selectedSessions.Length == 0 && _sessionGrid.SelectedRows.Cast<DataGridViewRow>()
            .Any(row => row.Tag is ResetAlertLogicalRow);
        _sessionPowerButton.Enabled = selectedSessions.Length > 0;
        if (logicalOnly)
        {
            _sessionPowerButton.Text = "随飞书";
            if (_sessionPowerButton is RoundedButton logicalButton) logicalButton.Glyph = ButtonGlyph.None;
            _sessionPowerButton.Invalidate();
            return;
        }
        var stopping = SelectedSessionIsRunning();
        _sessionPowerButton.Text = stopping ? "停止" : "启动";
        if (_sessionPowerButton is RoundedButton rounded) rounded.Glyph = stopping ? ButtonGlyph.Stop : ButtonGlyph.Play;
        _sessionPowerButton.Invalidate();
    }

    private void AddSession()
    {
        using var dialog = new SessionDialog(null, _config.Sessions.Select(item => item.Name));
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        _config.Sessions.Add(dialog.Result);
        SaveAndReload();
    }

    private void EditSession()
    {
        var item = SelectedRows<SessionDefinition>(_sessionGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个会话。"); return; }
        if (!string.IsNullOrWhiteSpace(item.SourcePluginId)) { ShowHint("插件会话是只读的，请修改对应插件的 manifest.json。"); return; }
        using var dialog = new SessionDialog(item, _config.Sessions.Where(x => x.Id != item.Id).Select(x => x.Name),
            _sessionManager.IsExternallyControlled(item));
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        dialog.Result.LastExecutedAt = item.LastExecutedAt;
        if (_sessionManager.IsExternallyControlled(item))
        {
            dialog.Result.WorkingDirectory = item.WorkingDirectory;
            dialog.Result.StartCommand = item.StartCommand;
            dialog.Result.StopCommand = item.StopCommand;
            dialog.Result.StatusCommand = item.StatusCommand;
        }
        var index = _config.Sessions.FindIndex(x => x.Id == item.Id);
        if (index >= 0) _config.Sessions[index] = dialog.Result;
        SaveAndReload();
    }

    private void DeleteSession()
    {
        var item = SelectedRows<SessionDefinition>(_sessionGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个会话。"); return; }
        if (!string.IsNullOrWhiteSpace(item.SourcePluginId)) { ShowHint("插件会话不能在这里删除，请停用或移除插件。"); return; }
        if (MessageBox.Show(this, $"仅从管理列表删除“{item.Name}”？\n不会停止或删除它对应的外部程序。", "确认删除",
                MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        _config.Sessions.RemoveAll(x => x.Id == item.Id);
        SaveAndReload();
    }

    private async Task RunSelectedToolAsync()
    {
        var item = SelectedRows<ToolDefinition>(_toolGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个工具。"); return; }
        if (item.Name.Contains("TUN", StringComparison.OrdinalIgnoreCase) &&
            MessageBox.Show(this, "即将运行“修复 TUN”，网络可能短暂切换。是否继续？", "运行网络工具",
                MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
        try
        {
            var result = await ToolRunner.RunAsync(item);
            item.LastExecutedAtCompat();
            _notifications.Show(item.Name, result.Message);
        }
        catch (Exception error) { ShowError($"运行“{item.Name}”失败", error); }
    }

    private void AddTool()
    {
        using var dialog = new ToolDialog(null, _config.Tools.Select(item => item.Name));
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        _config.Tools.Add(dialog.Result);
        SaveAndReload();
    }

    private void EditTool()
    {
        var item = SelectedRows<ToolDefinition>(_toolGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个工具。"); return; }
        if (!string.IsNullOrWhiteSpace(item.SourcePluginId)) { ShowHint("插件工具是只读的，请修改对应插件的 manifest.json。"); return; }
        using var dialog = new ToolDialog(item, _config.Tools.Where(x => x.Id != item.Id).Select(x => x.Name));
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        var index = _config.Tools.FindIndex(x => x.Id == item.Id);
        if (index >= 0) _config.Tools[index] = dialog.Result;
        SaveAndReload();
    }

    private void DeleteTool()
    {
        var item = SelectedRows<ToolDefinition>(_toolGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个工具。"); return; }
        if (!string.IsNullOrWhiteSpace(item.SourcePluginId)) { ShowHint("插件工具不能在这里删除，请停用或移除插件。"); return; }
        if (MessageBox.Show(this, $"仅从工具箱删除“{item.Name}”？\n不会删除原文件。", "确认删除",
                MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        _config.Tools.RemoveAll(x => x.Id == item.Id);
        SaveAndReload();
    }

    private void OpenSelectedToolDirectory()
    {
        var item = SelectedRows<ToolDefinition>(_toolGrid).FirstOrDefault();
        if (item is null) { ShowHint("请先选择一个工具。"); return; }
        var target = ToolPath.Resolve(item.TargetPath);
        var directory = Directory.Exists(target) ? target : Path.GetDirectoryName(target);
        if (!string.IsNullOrWhiteSpace(directory)) OpenPath(directory);
    }

    private async void SaveAndReload()
    {
        try
        {
            await _configStore.SaveAsync(_config);
            RebuildEffectiveItems(); PopulateAllGrids();
        }
        catch (Exception error) { ShowError("保存配置失败", error); }
    }

    private void LoadSettingsControls()
    {
        _loadingSettings = true;
        try
        {
            _notificationsSetting.Checked = _config.Settings.NotificationsEnabled;
            _autoStartSetting.Checked = _config.Settings.AutoStartEnabled;
            _startMinimizedSetting.Checked = _config.Settings.StartMinimizedToTray;
            _refreshSetting.Value = Math.Clamp(_config.Settings.RefreshIntervalSeconds, 1, 300);
            _autoUpdateCheckSetting.Checked = _config.Settings.AutoCheckForUpdates;
            _updateIntervalSetting.Value = Math.Clamp(_config.Settings.UpdateCheckIntervalHours, 1, 168);
            _themeModeSetting.Select(UiTheme.ParseMode(_config.Settings.ThemeMode) == ThemeMode.Night ? 1 : 0, animate: false);
        }
        finally { _loadingSettings = false; }
    }

    private async Task SaveSettingsAsync(bool applyAutoStart = false)
    {
        if (_loadingSettings) return;
        try
        {
            _config.Settings.NotificationsEnabled = _notificationsSetting.Checked;
            _config.Settings.AutoStartEnabled = _autoStartSetting.Checked;
            _config.Settings.StartMinimizedToTray = _startMinimizedSetting.Checked;
            _config.Settings.RefreshIntervalSeconds = (int)_refreshSetting.Value;
            _config.Settings.AutoCheckForUpdates = _autoUpdateCheckSetting.Checked;
            _config.Settings.UpdateCheckIntervalHours = (int)_updateIntervalSetting.Value;
            var selectedTheme = _themeModeSetting.SelectedIndex == 1 ? ThemeMode.Night : ThemeMode.Day;
            _config.Settings.ThemeMode = UiTheme.PersistedName(selectedTheme);
            if (UiTheme.Mode != selectedTheme)
                UiTheme.SetMode(selectedTheme, _themeModeSetting, animated: true);
            _timer.Interval = _config.Settings.RefreshIntervalSeconds * 1000;
            await _configStore.SaveAsync(_config);
            if (applyAutoStart) _autoStart.Apply(_config.Settings.AutoStartEnabled);
        }
        catch (Exception error) { ShowError("保存设置失败", error); }
    }

    private async Task CheckForUpdatesAsync(bool manual)
    {
        if (_checkingUpdates) return;
        if (!manual)
        {
            if (!_config.Settings.AutoCheckForUpdates) return;
            var interval = TimeSpan.FromHours(Math.Clamp(_config.Settings.UpdateCheckIntervalHours, 1, 168));
            if (_config.Settings.LastUpdateCheckAt is { } last && DateTimeOffset.UtcNow - last < interval) return;
        }

        _checkingUpdates = true;
        _checkForUpdatesButton.Enabled = false;
        _updateStatusLabel.Text = $"当前生态版本：{_ecosystemInstallation.DisplayVersion}\n正在检查 GitHub 正式版本…";
        try
        {
            var result = await _ecosystemUpdates.CheckAsync(
                _ecosystemInstallation.Version,
                manual ? null : _config.Settings.UpdateCheckETag);
            _config.Settings.LastUpdateCheckAt = DateTimeOffset.UtcNow;
            if (!string.IsNullOrWhiteSpace(result.ETag)) _config.Settings.UpdateCheckETag = result.ETag;

            if (result.NotModified && EcosystemVersion.TryParse(_config.Settings.LastNotifiedUpdateVersion, out var notified) &&
                notified > _ecosystemInstallation.Version)
            {
                _updateStatusLabel.Text = $"发现生态更新：{_ecosystemInstallation.DisplayVersion} → {EcosystemVersion.Display(notified)}\n等待你选择是否下载安装。";
                await _configStore.SaveAsync(_config);
                return;
            }
            if (!result.IsUpdateAvailable || result.Release is null)
            {
                _updateStatusLabel.Text = $"当前生态版本：{_ecosystemInstallation.DisplayVersion}\n已是最新正式版。上次检查：{DateTime.Now:yyyy-MM-dd HH:mm}";
                await _configStore.SaveAsync(_config);
                if (manual) ShowHint("当前整套生态已经是最新正式版。");
                return;
            }

            var release = result.Release;
            _updateStatusLabel.Text = $"发现生态更新：{_ecosystemInstallation.DisplayVersion} → {release.DisplayVersion}\n等待你选择是否下载安装。";
            var firstNotification = !_config.Settings.LastNotifiedUpdateVersion.Equals(
                release.DisplayVersion, StringComparison.OrdinalIgnoreCase);
            if (firstNotification)
            {
                _config.Settings.LastNotifiedUpdateVersion = release.DisplayVersion;
                _notifications.Show("整套生态有新版本", $"{release.DisplayVersion} 已发布。请打开百宝箱决定是否安装。");
            }
            await _configStore.SaveAsync(_config);
            if (manual || (firstNotification && Visible))
                await ShowAvailableUpdateAsync(release);
        }
        catch (Exception error)
        {
            _logger.Error("检查生态更新失败：" + error);
            _updateStatusLabel.Text = $"当前生态版本：{_ecosystemInstallation.DisplayVersion}\n检查失败：{error.Message}";
            _config.Settings.LastUpdateCheckAt = DateTimeOffset.UtcNow;
            try { await _configStore.SaveAsync(_config); } catch { }
            if (manual) ShowError("检查更新失败", error);
        }
        finally
        {
            _checkingUpdates = false;
            _checkForUpdatesButton.Enabled = true;
        }
    }

    private async Task ShowAvailableUpdateAsync(EcosystemReleaseInfo release)
    {
        using var dialog = new EcosystemUpdateDialog(_ecosystemInstallation, release);
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        using var progressDialog = new UpdateDownloadDialog(release.DisplayVersion);
        progressDialog.Show(this);
        progressDialog.BringToFront();
        try
        {
            var package = await _ecosystemUpdates.DownloadAsync(
                release,
                progressDialog.CreateProgress(),
                progressDialog.Cancellation.Token);
            progressDialog.SetVerifying();
            await Task.Yield();
            EcosystemUpdaterLauncher.Launch(_ecosystemInstallation, release, package);
            _logger.Info($"已启动独立生态更新器：{_ecosystemInstallation.DisplayVersion} -> {release.DisplayVersion}");
            _allowExit = true;
            Close();
        }
        catch (OperationCanceledException)
        {
            _logger.Info("用户取消了生态升级包下载。");
        }
        catch (Exception error)
        {
            ShowError("下载安装更新失败", error);
        }
        finally
        {
            if (!progressDialog.IsDisposed) progressDialog.Close();
        }
    }

    private async void ExportConfig()
    {
        using var dialog = new SaveFileDialog { Filter = "TreasureChest 配置 (*.json)|*.json", FileName = "TreasureChest-config.json" };
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        try { await _configStore.ExportAsync(_config, dialog.FileName); ShowHint("配置已导出。"); }
        catch (Exception error) { ShowError("导出配置失败", error); }
    }

    private async void ImportConfig()
    {
        using var dialog = new OpenFileDialog { Filter = "TreasureChest 配置 (*.json)|*.json|所有文件|*.*" };
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        try
        {
            _config = await _configStore.ImportAsync(dialog.FileName);
            LoadSettingsControls(); RebuildEffectiveItems(); PopulateAllGrids();
            _autoStart.Apply(_config.Settings.AutoStartEnabled);
            ShowHint("配置已导入并生效。");
        }
        catch (Exception error) { ShowError("导入配置失败", error); }
    }

    private async void ResetConfig()
    {
        if (MessageBox.Show(this, "恢复默认配置？现有会话和工具条目将被替换，但不会删除任何外部文件。", "恢复默认",
                MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return;
        try
        {
            _config = await _configStore.ResetAsync();
            LoadSettingsControls(); await ScanPluginsAsync();
            _autoStart.Apply(_config.Settings.AutoStartEnabled);
        }
        catch (Exception error) { ShowError("恢复默认失败", error); }
    }

    private void MinimizeToTaskbar()
    {
        if (WindowState != FormWindowState.Minimized) _windowStateBeforeHide = WindowState;
        WindowChromePresentation.MinimizeToTaskbar(this);
    }

    private void HideToTray()
    {
        if (WindowState != FormWindowState.Minimized) _windowStateBeforeHide = WindowState;
        Hide();
        ShowInTaskbar = false;
        TrimWorkingSet();
    }

    private void RestoreFromTray()
    {
        Opacity = 1;
        ShowInTaskbar = true;
        Show();
        WindowState = _windowStateBeforeHide;
        Activate();
        BringToFront();
        ScheduleWorkingAreaConstraint();
    }

    internal void ActivateFromExternalInstance() => RestoreFromTray();

    private void ExitApplication()
    {
        _allowExit = true;
        Close();
    }

    private static IEnumerable<T> SelectedRows<T>(DataGridView grid) where T : class =>
        grid.SelectedRows.Cast<DataGridViewRow>().OrderBy(row => row.Index).Select(row => row.Tag).OfType<T>();

    private void OpenPath(string path)
    {
        try
        {
            if (!File.Exists(path) && !Directory.Exists(path)) throw new FileNotFoundException("目标不存在。", path);
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
        catch (Exception error) { ShowError("无法打开", error); }
    }

    private void ShowHint(string message) => MessageBox.Show(this, message, "TreasureChest", MessageBoxButtons.OK, MessageBoxIcon.Information);
    private void ShowError(string title, Exception error)
    {
        _logger.Error(title + "：" + error);
        MessageBox.Show(this, error.Message, title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private void DragWindow(object? sender, MouseEventArgs e)
    {
        if (e.Button != MouseButtons.Left) return;
        var screenPoint = sender is Control control ? control.PointToScreen(e.Location) : Cursor.Position;
        var windowBounds = NativeMethods.WindowRectangle(Handle);
        var dpi = (int)NativeMethods.GetDpiForWindow(Handle);
        var resizeHit = DpiLayout.HitTest(windowBounds, screenPoint, dpi, WindowState);
        NativeMethods.ReleaseCapture();
        NativeMethods.SendMessage(
            Handle,
            NativeMethods.WmNcLButtonDown,
            resizeHit == WindowResizeHit.Client ? (IntPtr)NativeMethods.HtCaption : (IntPtr)(int)resizeHit,
            NativeMethods.LParamFromScreenPoint(screenPoint));
    }

    private void ToggleMaximize()
    {
        WindowState = WindowChromePresentation.ToggleMaximize(WindowState);
        _windowStateBeforeHide = WindowState;
        UpdateWindowChrome();
    }

    private void UpdateWindowChrome()
    {
        if (_maximizeButton is null || _resizeGrip is null) return;
        _maximizeButton.Kind = WindowChromePresentation.MaximizeKind(WindowState);
        _maximizeButton.AccessibleName = WindowChromePresentation.MaximizeAccessibleName(WindowState);
        _windowChromeTips.SetToolTip(_maximizeButton, _maximizeButton.AccessibleName);
        _resizeGrip.Place(ClientRectangle, _layoutDpi, WindowState);
        _resizeGrip.BringToFront();
        _dwmRoundedCornersActive = NativeMethods.TrySetWindowCornerPreference(
            Handle, WindowChromePresentation.DwmCornerPreference(WindowState));
        ApplyRoundedWindowRegion();
    }

    private void ApplyRoundedWindowRegion()
    {
        var previous = Region;
        if (WindowState == FormWindowState.Maximized || _dwmRoundedCornersActive ||
            ClientSize.Width <= 1 || ClientSize.Height <= 1)
        {
            Region = null;
            previous?.Dispose();
            return;
        }
        using var path = RoundedButton.RoundedPath(ClientRectangle, WindowChromePresentation.CornerRadius(_layoutDpi, WindowState));
        Region = new Region(path);
        previous?.Dispose();
    }

    private void PreparePageForCurrentDpi(Control page)
    {
        UiTheme.ApplyCurrentTheme(page);
        if (!_dpiLayoutReady) return;
        _dpiLayout.CaptureBaseTree(page);
        _dpiLayout.ApplyTree(page, _layoutDpi);
    }

    private void ApplyDpiLayout(int targetDpi, Point? suggestedLocation, bool initial)
    {
        if (!_dpiLayoutReady) return;
        targetDpi = DpiLayout.NormalizeDpi(targetDpi);
        if (_initialDpiApplied && targetDpi == _layoutDpi && !initial) return;

        _applyingDpiLayout = true;
        SuspendLayout();
        try
        {
            _dpiLayout.RefreshMutableValues(this, _layoutDpi);
            _dpiLayout.CaptureTree(this, _layoutDpi);
            _layoutDpi = targetDpi;
            _dpiLayout.ApplyTree(this, targetDpi);

            var metrics = DpiLayout.Metrics(targetDpi);
            MinimumSize = metrics.MinimumWindowSize;
            if (_navigation is not null)
            {
                var expandedNavigation = DpiLayout.Scale(_expandedNavigationLogicalWidth, targetDpi);
                var collapsedNavigation = DpiLayout.Scale(82, targetDpi);
                _navigation.Width = (int)Math.Round(expandedNavigation +
                    (collapsedNavigation - expandedNavigation) * _sidebarCollapseProgress);
                foreach (var navigationButton in _navigationButtons)
                    navigationButton.CollapseProgress = _sidebarCollapseProgress;
                if (_sidebarToggle is not null) _sidebarToggle.CollapseProgress = _sidebarCollapseProgress;
            }

            if (WindowState == FormWindowState.Normal)
            {
                var desiredSize = DpiLayout.Scale(_normalLogicalSize, targetDpi);
                if (initial && suggestedLocation is null)
                {
                    Size = desiredSize;
                }
                else
                {
                    var location = suggestedLocation ?? Location;
                    var workingArea = Screen.FromPoint(location).WorkingArea;
                    Bounds = DpiLayout.ConstrainToWorkingArea(location, desiredSize, workingArea, targetDpi);
                }
            }
            _initialDpiApplied = true;
            UpdateWindowChrome();
        }
        finally
        {
            ResumeLayout(performLayout: true);
            _applyingDpiLayout = false;
        }
        Invalidate(true);
    }

    private void ScheduleWorkingAreaConstraint()
    {
        if (_workingAreaConstraintPending || IsDisposed || Disposing || !IsHandleCreated) return;
        _workingAreaConstraintPending = true;
        try
        {
            BeginInvoke(new Action(() =>
            {
                _workingAreaConstraintPending = false;
                if (IsDisposed || Disposing) return;
                ConstrainWindowToCurrentWorkingArea();
            }));
        }
        catch (InvalidOperationException)
        {
            _workingAreaConstraintPending = false;
        }
    }

    private void ConstrainWindowToCurrentWorkingArea()
    {
        if (!WindowChromePresentation.ShouldConstrainToWorkingArea(WindowState) ||
            ClientSize.Width <= 1 || ClientSize.Height <= 1) return;

        var currentBounds = Bounds;
        var workingArea = Screen.FromRectangle(currentBounds).WorkingArea;
        var dpi = (int)NativeMethods.GetDpiForWindow(Handle);
        var constrained = DpiLayout.ConstrainToWorkingArea(
            currentBounds.Location, currentBounds.Size, workingArea, dpi);
        if (constrained == currentBounds) return;

        // Do not overwrite _normalLogicalSize here.  A temporary small working
        // area may require a bounded physical size, but returning to a larger
        // display should still restore the user's logical normal-window size.
        _applyingDpiLayout = true;
        try { Bounds = constrained; }
        finally { _applyingDpiLayout = false; }
    }

    private static void TrimWorkingSet()
    {
        try
        {
            GC.Collect(2, GCCollectionMode.Optimized, blocking: false, compacting: false);
            using var process = Process.GetCurrentProcess();
            NativeMethods.SetProcessWorkingSetSize(process.Handle, new IntPtr(-1), new IntPtr(-1));
        }
        catch { }
    }

    private static string StatusText(SessionStatus status) => status switch
    {
        SessionStatus.Running => "运行中",
        SessionStatus.Stopped => "已停止",
        SessionStatus.Error => "异常",
        SessionStatus.Disabled => "已禁用",
        _ => "检查中",
    };

    private static Color StatusColor(SessionStatus status) => status switch
    {
        SessionStatus.Running => UiTheme.Running,
        SessionStatus.Stopped or SessionStatus.Error => UiTheme.Stopped,
        SessionStatus.Disabled => UiTheme.Muted,
        _ => UiTheme.Warning,
    };
}

internal static class ToolDefinitionExtensions
{
    // 工具模型暂不持久化最近运行时间；保留扩展点以兼容后续 SDK。
    public static void LastExecutedAtCompat(this ToolDefinition _) { }
}

internal sealed record MainMonitorDrawerGroup(string Origin, string Classification)
{
    public string StorageKey => $"{Origin}\u001f{Classification}";
}
