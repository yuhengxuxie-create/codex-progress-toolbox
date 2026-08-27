using System.Diagnostics;
using TreasureChest.Core.Models;
using TreasureChest.Core.Services;
using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class MainForm : Form
{
    private readonly ConfigStore _configStore;
    private readonly AppLogger _logger;
    private readonly SessionManager _sessionManager;
    private readonly PluginCatalog _pluginCatalog;
    private readonly ProjectMonitorCliService _projectMonitorCli;
    private readonly CodexThreadCatalogService _codexThreadCatalog;
    private readonly AutoStartService _autoStart;
    private readonly bool _startupLaunch;
    private readonly NotifyIcon _trayIcon;
    private readonly NotificationService _notifications;
    private readonly System.Windows.Forms.Timer _timer = new();
    private readonly Panel _content = new() { Dock = DockStyle.Fill, BackColor = UiTheme.Background };
    private readonly DataGridView _sessionGrid = CreateGrid();
    private readonly DataGridView _monitorThreadGrid = CreateGrid();
    private readonly DataGridView _toolGrid = CreateGrid();
    private readonly DataGridView _pluginGrid = CreateGrid();
    private readonly CheckBox _notificationsSetting = new() { Text = "启用系统托盘通知", AutoSize = true };
    private readonly CheckBox _autoStartSetting = new() { Text = "登录 Windows 后自动启动", AutoSize = true };
    private readonly CheckBox _startMinimizedSetting = new() { Text = "自动启动时最小化到托盘", AutoSize = true };
    private readonly NumericUpDown _refreshSetting = new() { Minimum = 1, Maximum = 300, Width = 90 };
    private readonly HashSet<string> _expandedMainMonitorGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private readonly ToolStripMenuItem _pauseMenu = new("暂停状态监控");
    private AppConfiguration _config;
    private IReadOnlyList<SessionDefinition> _sessions = [];
    private IReadOnlyList<ToolDefinition> _tools = [];
    private IReadOnlyList<ProjectMonitorItem> _projectMonitors = [];
    private bool _loadingPlugins;
    private bool _loadingSettings;
    private bool _refreshing;
    private bool _refreshingProjectMonitors;
    private DateTimeOffset _lastProjectMonitorRefresh = DateTimeOffset.MinValue;
    private bool _allowExit;
    private Button? _activeNav;

    public MainForm(
        AppConfiguration config,
        ConfigStore configStore,
        AppLogger logger,
        SessionManager sessionManager,
        PluginCatalog pluginCatalog,
        AutoStartService autoStart,
        bool startupLaunch)
    {
        _config = config;
        _configStore = configStore;
        _logger = logger;
        _sessionManager = sessionManager;
        _pluginCatalog = pluginCatalog;
        _autoStart = autoStart;
        _startupLaunch = startupLaunch;
        _projectMonitorCli = new ProjectMonitorCliService(AppPaths.ProgressNotificationRoot, AppPaths.ProgressPythonPath);
        _codexThreadCatalog = new CodexThreadCatalogService(AppPaths.ProgressNotificationRoot, AppPaths.ProgressPythonPath);

        Text = "TreasureChest";
        Font = new Font("Microsoft YaHei UI", 9F);
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

        _trayIcon = new NotifyIcon
        {
            Text = "TreasureChest 管理中心",
            Icon = (Icon)Icon.Clone(),
            Visible = true,
            ContextMenuStrip = BuildTrayMenu(),
        };
        _notifications = new NotificationService(_trayIcon, () => _config.Settings.NotificationsEnabled);
        _trayIcon.DoubleClick += (_, _) => RestoreFromTray();
        _trayIcon.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) RestoreFromTray(); };

        BuildShell();
        BuildSessionGrid();
        BuildMonitorThreadGrid();
        BuildToolGrid();
        BuildPluginGrid();
        WireEvents();

        _timer.Interval = Math.Clamp(_config.Settings.RefreshIntervalSeconds, 1, 300) * 1000;
        _timer.Tick += async (_, _) => await RefreshTimerDataAsync();
        Shown += async (_, _) => await InitializeAsync();
    }

    public static int ActivateMessage { get; set; }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        var preference = 2;
        NativeMethods.DwmSetWindowAttribute(Handle, NativeMethods.DwmWindowCornerPreference, ref preference, sizeof(int));
    }

    protected override void WndProc(ref Message m)
    {
        if (ActivateMessage != 0 && m.Msg == ActivateMessage)
        {
            RestoreFromTray();
            return;
        }
        base.WndProc(ref m);
        if (m.Msg == NativeMethods.WmNcHitTest && m.Result == (IntPtr)NativeMethods.HtClient && WindowState == FormWindowState.Normal)
            m.Result = (IntPtr)ResizeHitTest(PointToClient(Cursor.Position));
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!_allowExit && e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            HideToTray();
            return;
        }
        _timer.Stop();
        _trayIcon.Visible = false;
        _trayIcon.Dispose();
        _sessionManager.Dispose();
        base.OnFormClosing(e);
    }

    private void BuildShell()
    {
        var titleBar = new Panel { Dock = DockStyle.Top, Height = 50, BackColor = Color.FromArgb(24, 33, 54) };
        var title = new Label
        {
            Text = "  TreasureChest  ·  本地工具管理中心",
            ForeColor = Color.White,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft,
            Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold),
        };
        title.MouseDown += DragWindow;
        titleBar.MouseDown += DragWindow;
        var close = TitleButton("×", "关闭到托盘");
        close.Click += (_, _) => HideToTray();
        var minimize = TitleButton("−", "最小化到托盘");
        minimize.Click += (_, _) => HideToTray();
        titleBar.Controls.Add(title);
        titleBar.Controls.Add(minimize);
        titleBar.Controls.Add(close);

        var navigation = new Panel { Dock = DockStyle.Left, Width = 210, BackColor = UiTheme.Navigation, Padding = new Padding(14, 28, 14, 14) };
        var brand = new Label
        {
            Text = "TREASURE\nCHEST",
            ForeColor = Color.White,
            Font = new Font("Microsoft YaHei UI", 18F, FontStyle.Bold),
            Height = 82,
            Dock = DockStyle.Top,
            TextAlign = ContentAlignment.MiddleLeft,
        };
        var navFlow = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, WrapContents = false };
        navFlow.Controls.Add(NavButton("会话管理", BuildSessionsPage));
        navFlow.Controls.Add(NavButton("工具箱", BuildToolsPage));
        navFlow.Controls.Add(NavButton("插件中心", BuildPluginsPage));
        navFlow.Controls.Add(NavButton("设置", BuildSettingsPage));
        navigation.Controls.Add(navFlow);
        navigation.Controls.Add(brand);

        Controls.Add(_content);
        Controls.Add(navigation);
        Controls.Add(titleBar);

        var resizeGrip = new Panel
        {
            Size = new Size(20, 20),
            Location = new Point(ClientSize.Width - 20, ClientSize.Height - 20),
            Anchor = AnchorStyles.Right | AnchorStyles.Bottom,
            Cursor = Cursors.SizeNWSE,
            BackColor = Color.Transparent,
            AccessibleName = "拖拽调整窗口大小",
        };
        resizeGrip.Paint += (_, e) =>
        {
            using var pen = new Pen(Color.FromArgb(145, 154, 174), 1F);
            for (var offset = 0; offset < 3; offset++)
            {
                var inset = 4 + offset * 5;
                e.Graphics.DrawLine(pen, resizeGrip.Width - inset, resizeGrip.Height - 2,
                    resizeGrip.Width - 2, resizeGrip.Height - inset);
            }
        };
        resizeGrip.MouseDown += (_, e) =>
        {
            if (e.Button != MouseButtons.Left) return;
            NativeMethods.ReleaseCapture();
            NativeMethods.SendMessage(Handle, NativeMethods.WmNcLButtonDown, (IntPtr)NativeMethods.HtBottomRight, IntPtr.Zero);
        };
        Controls.Add(resizeGrip);
        resizeGrip.BringToFront();
    }

    private Button NavButton(string text, Func<Control> pageFactory)
    {
        var button = new Button
        {
            Text = "  " + text,
            Width = 180,
            Height = 46,
            Margin = new Padding(0, 0, 0, 8),
            FlatStyle = FlatStyle.Flat,
            FlatAppearance = { BorderSize = 0 },
            BackColor = UiTheme.Navigation,
            ForeColor = Color.FromArgb(220, 226, 240),
            TextAlign = ContentAlignment.MiddleLeft,
            Cursor = Cursors.Hand,
            Font = new Font("Microsoft YaHei UI", 10F),
        };
        button.Click += (_, _) =>
        {
            if (_activeNav is not null) _activeNav.BackColor = UiTheme.Navigation;
            _activeNav = button;
            button.BackColor = UiTheme.Accent;
            _content.Controls.Clear();
            var page = pageFactory();
            page.Dock = DockStyle.Fill;
            _content.Controls.Add(page);
        };
        if (_activeNav is null)
        {
            _activeNav = button;
            button.BackColor = UiTheme.Accent;
            var page = pageFactory();
            page.Dock = DockStyle.Fill;
            _content.Controls.Add(page);
        }
        return button;
    }

    private Control BuildSessionsPage()
    {
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 62));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 38));
        body.Controls.Add(BuildMonitorThreadPanel(), 0, 0);
        body.Controls.Add(BuildSessionPanel(), 0, 1);

        var noPageActions = new Panel { Dock = DockStyle.Top, Height = 0, Visible = false };
        return Page("会话管理", "上方管理飞书机器人正在监测的 Codex 项目与会话；下方管理后台服务。", noPageActions, body);
    }

    private Control BuildMonitorThreadPanel()
    {
        var panel = new Panel { Dock = DockStyle.Fill, Padding = new Padding(0, 0, 0, 14) };
        var heading = new Panel { Dock = DockStyle.Top, Height = 44 };
        heading.Controls.Add(new Label
        {
            Text = "项目监测列表",
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold),
            ForeColor = UiTheme.Text,
            Location = new Point(2, 10),
        });
        var monitorActions = new FlowLayoutPanel { Dock = DockStyle.Right, Width = 500, FlowDirection = FlowDirection.RightToLeft };
        monitorActions.Controls.Add(ActionButton("管理监测", ManageProjectMonitors, true));
        monitorActions.Controls.Add(ActionButton("监测设置", ManageProjectMonitorSettingsAsync));
        monitorActions.Controls.Add(ActionButton("在 Codex 打开", OpenMonitorThread));
        monitorActions.Controls.Add(ActionButton("刷新", PopulateMonitorThreadGridAsync));
        heading.Controls.Add(monitorActions);
        panel.Controls.Add(_monitorThreadGrid);
        panel.Controls.Add(heading);
        return panel;
    }

    private Control BuildSessionPanel()
    {
        var panel = new Panel { Dock = DockStyle.Fill };
        var heading = new Panel { Dock = DockStyle.Top, Height = 44 };
        heading.Controls.Add(new Label
        {
            Text = "后台服务",
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold),
            ForeColor = UiTheme.Text,
            Location = new Point(2, 10),
        });

        var sessionActions = new FlowLayoutPanel
        {
            Dock = DockStyle.Right,
            Width = 630,
            FlowDirection = FlowDirection.RightToLeft,
        };
        sessionActions.Controls.Add(ActionButton("删除", DeleteSession));
        sessionActions.Controls.Add(ActionButton("编辑", EditSession));
        sessionActions.Controls.Add(ActionButton("添加后台服务", AddSession, true));
        sessionActions.Controls.Add(ActionButton("立即刷新", async () => await RefreshSessionsAsync(force: true)));
        sessionActions.Controls.Add(ActionButton("停止", async () => await StopSelectedSessionsAsync()));
        sessionActions.Controls.Add(ActionButton("启动", async () => await StartSelectedSessionsAsync()));
        heading.Controls.Add(sessionActions);

        panel.Controls.Add(_sessionGrid);
        panel.Controls.Add(heading);
        return panel;
    }

    private Control BuildToolsPage()
    {
        var actions = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 48 };
        actions.Controls.Add(ActionButton("运行工具", async () => await RunSelectedToolAsync(), true));
        actions.Controls.Add(ActionButton("添加工具", AddTool));
        actions.Controls.Add(ActionButton("编辑", EditTool));
        actions.Controls.Add(ActionButton("删除", DeleteTool));
        actions.Controls.Add(ActionButton("打开工具目录", OpenSelectedToolDirectory));
        return Page("工具箱", "把常用脚本、快捷方式、程序和目录集中到一个入口。", actions, _toolGrid);
    }

    private Control BuildPluginsPage()
    {
        var actions = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 48 };
        actions.Controls.Add(ActionButton("重新扫描", async () => await ScanPluginsAsync(), true));
        actions.Controls.Add(ActionButton("打开插件目录", () => OpenPath(_pluginCatalog.PluginsRoot)));
        actions.Controls.Add(ActionButton("打开开发说明", () => OpenPath(Path.Combine(AppPaths.Root, "PLUGIN_DEVELOPMENT.md"))));
        return Page("插件中心", "每个插件都是独立目录；取消勾选即可停用，不修改插件自身文件。", actions, _pluginGrid);
    }

    private Control BuildSettingsPage()
    {
        var panel = new Panel { BackColor = UiTheme.Background, Padding = new Padding(32) };
        var stack = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
        };
        stack.Controls.Add(UiTheme.Heading("设置"));
        stack.Controls.Add(new Label { Text = "所有配置均保存在 TreasureChest 目录，不上传到外部服务。", AutoSize = true, ForeColor = UiTheme.Muted, Margin = new Padding(0, 6, 0, 24) });
        foreach (var check in new[] { _notificationsSetting, _autoStartSetting, _startMinimizedSetting })
        {
            check.Font = new Font("Microsoft YaHei UI", 10F);
            check.Margin = new Padding(0, 8, 0, 8);
            stack.Controls.Add(check);
        }
        var refresh = new FlowLayoutPanel { AutoSize = true, Margin = new Padding(0, 10, 0, 20) };
        refresh.Controls.Add(new Label { Text = "状态刷新间隔（秒）", AutoSize = true, Margin = new Padding(0, 7, 12, 0) });
        refresh.Controls.Add(_refreshSetting);
        stack.Controls.Add(refresh);
        var actions = new FlowLayoutPanel { AutoSize = true };
        actions.Controls.Add(ActionButton("导出配置", ExportConfig));
        actions.Controls.Add(ActionButton("导入配置", ImportConfig));
        actions.Controls.Add(ActionButton("恢复默认", ResetConfig));
        actions.Controls.Add(ActionButton("打开日志", () => OpenPath(_logger.Path)));
        actions.Controls.Add(ActionButton("打开应用目录", () => OpenPath(AppPaths.Root)));
        stack.Controls.Add(actions);
        panel.Controls.Add(stack);
        return panel;
    }

    private static Control Page(string heading, string subheading, Control actions, Control body)
    {
        var page = new Panel { Dock = DockStyle.Fill, BackColor = UiTheme.Background, Padding = new Padding(28) };
        var header = new Panel { Dock = DockStyle.Top, Height = 82 };
        var title = UiTheme.Heading(heading);
        title.Location = new Point(0, 0);
        var subtitle = new Label { Text = subheading, AutoSize = true, ForeColor = UiTheme.Muted, Location = new Point(2, 42) };
        header.Controls.Add(title); header.Controls.Add(subtitle);
        body.Dock = DockStyle.Fill;
        page.Controls.Add(body);
        page.Controls.Add(actions);
        page.Controls.Add(header);
        return page;
    }

    private static Button ActionButton(string text, Action action, bool primary = false)
    {
        var button = UiTheme.Button(text, primary);
        button.Click += (_, _) => action();
        return button;
    }

    private static Button ActionButton(string text, Func<Task> action, bool primary = false)
    {
        var button = UiTheme.Button(text, primary);
        button.Click += async (_, _) =>
        {
            button.Enabled = false;
            try { await action(); }
            finally { button.Enabled = true; }
        };
        return button;
    }

    private static Button TitleButton(string text, string accessibleName)
    {
        var button = new Button
        {
            Text = text,
            AccessibleName = accessibleName,
            Dock = DockStyle.Right,
            Width = 50,
            Height = 50,
            Margin = Padding.Empty,
            Padding = Padding.Empty,
            FlatStyle = FlatStyle.Flat,
            ForeColor = Color.White,
            BackColor = Color.FromArgb(24, 33, 54),
            Font = new Font("Segoe UI", 13F),
            Cursor = Cursors.Hand,
            TextAlign = ContentAlignment.MiddleCenter,
        };
        button.FlatAppearance.BorderSize = 0;
        button.FlatAppearance.MouseOverBackColor = text == "×" ? Color.FromArgb(196, 43, 54) : Color.FromArgb(48, 61, 88);
        button.FlatAppearance.MouseDownBackColor = Color.FromArgb(59, 76, 111);
        return button;
    }

    private static DataGridView CreateGrid() => new()
    {
        Dock = DockStyle.Fill,
        BackgroundColor = UiTheme.Surface,
        BorderStyle = BorderStyle.None,
        AllowUserToAddRows = false,
        AllowUserToDeleteRows = false,
        AllowUserToResizeRows = false,
        AutoGenerateColumns = false,
        MultiSelect = true,
        SelectionMode = DataGridViewSelectionMode.FullRowSelect,
        RowHeadersVisible = false,
        ReadOnly = true,
        EnableHeadersVisualStyles = false,
        ColumnHeadersHeight = 42,
        RowTemplate = { Height = 44 },
        GridColor = Color.FromArgb(231, 234, 241),
        ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle
        {
            BackColor = Color.FromArgb(239, 242, 249), ForeColor = UiTheme.Text,
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold), Alignment = DataGridViewContentAlignment.MiddleLeft,
        },
        DefaultCellStyle = new DataGridViewCellStyle
        {
            BackColor = UiTheme.Surface, ForeColor = UiTheme.Text, SelectionBackColor = Color.FromArgb(224, 231, 255),
            SelectionForeColor = UiTheme.Text, Padding = new Padding(6, 0, 6, 0),
        },
    };

    private void BuildSessionGrid()
    {
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "会话", Width = 190 });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Status", HeaderText = "状态", Width = 90 });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Detail", HeaderText = "详情", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Auto", HeaderText = "自动策略", Width = 130 });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Source", HeaderText = "来源", Width = 110 });
        _sessionGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Checked", HeaderText = "检查时间", Width = 90 });
        _sessionGrid.CellDoubleClick += async (_, e) => { if (e.RowIndex >= 0) await StartSelectedSessionsAsync(); };
    }

    private void BuildMonitorThreadGrid()
    {
        _monitorThreadGrid.MultiSelect = true;
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Title", HeaderText = "会话名称", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill, MinimumWidth = 180 });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Classification", HeaderText = "项目 / 个人归属", Width = 150 });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Origin", HeaderText = "来源", Width = 82 });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "LastActivity", HeaderText = "最后活动时间", Width = 145 });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Remaining", HeaderText = "剩余有效时间", Width = 125 });
        _monitorThreadGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "ThreadId", HeaderText = "完整任务 ID", Width = 300 });
        _monitorThreadGrid.CellDoubleClick += (_, e) =>
        {
            if (e.RowIndex >= 0 && _monitorThreadGrid.Rows[e.RowIndex].Tag is ProjectMonitorItem) OpenMonitorThread();
        };
        _monitorThreadGrid.CellMouseClick += (_, e) =>
        {
            if (e.Button == MouseButtons.Left && e.Clicks == 1) ToggleMainMonitorDrawer(e.RowIndex);
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

    private void BuildToolGrid()
    {
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "工具", Width = 190 });
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Category", HeaderText = "分类", Width = 130 });
        _toolGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Target", HeaderText = "目标", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill });
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
        _pluginGrid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Description", HeaderText = "说明 / 状态", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill, ReadOnly = true });
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
                    _notifications.Show(e.Session.Name, $"状态变为：{StatusText(e.Current.Status)}\n{e.Current.Message}",
                        e.Current.Status == SessionStatus.Error ? ToolTipIcon.Error : ToolTipIcon.Info);
            });
        };
        _notificationsSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync();
        _autoStartSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync(applyAutoStart: true);
        _startMinimizedSetting.CheckedChanged += async (_, _) => await SaveSettingsAsync();
        _refreshSetting.ValueChanged += async (_, _) => await SaveSettingsAsync();
    }

    private ContextMenuStrip BuildTrayMenu()
    {
        var menu = new ContextMenuStrip();
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
        menu.Items.Add(show); menu.Items.Add(_pauseMenu); menu.Items.Add(new ToolStripSeparator()); menu.Items.Add(exit);
        return menu;
    }

    private async Task InitializeAsync()
    {
        try
        {
            LoadSettingsControls();
            await ScanPluginsAsync();
            await PopulateMonitorThreadGridAsync();
            await RefreshSessionsAsync(force: true);
            await _sessionManager.StartAutoStartSessionsAsync(_sessions);
            _timer.Start();
            if (_startupLaunch && _config.Settings.StartMinimizedToTray) HideToTray();
            else RestoreFromTray();
            _logger.Info("TreasureChest 主窗口初始化完成。");
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
            var drawer = group.Key;
            var expanded = _expandedMainMonitorGroups.Contains(drawer.StorageKey);
            var values = new object[_monitorThreadGrid.Columns.Count];
            for (var column = 1; column < values.Length; column++) values[column] = string.Empty;
            values[0] = ProjectMonitorPresentation.DrawerGroupTitle(drawer.Classification, group.Count(), expanded);
            values[2] = drawer.Origin.Equals("manual", StringComparison.OrdinalIgnoreCase) ? "手动" : "自动";
            var headerIndex = _monitorThreadGrid.Rows.Add(values);
            var header = _monitorThreadGrid.Rows[headerIndex];
            header.Tag = drawer;
            header.ReadOnly = true;
            header.Height = 36;
            header.DefaultCellStyle.BackColor = Color.FromArgb(224, 231, 247);
            header.DefaultCellStyle.SelectionBackColor = Color.FromArgb(224, 231, 247);
            header.DefaultCellStyle.ForeColor = UiTheme.Navigation;
            header.DefaultCellStyle.Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold);
            header.Cells[0].ToolTipText = expanded ? "单击收起这个分组" : "单击展开这个分组";
            if (!expanded) continue;

            foreach (var item in group)
            {
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
        if (!_expandedMainMonitorGroups.Add(drawer.StorageKey)) _expandedMainMonitorGroups.Remove(drawer.StorageKey);
        RenderMonitorThreadGrid();
        var refreshedHeader = _monitorThreadGrid.Rows.Cast<DataGridViewRow>()
            .FirstOrDefault(row => row.Tag is MainMonitorDrawerGroup group &&
                group.StorageKey.Equals(drawer.StorageKey, StringComparison.CurrentCultureIgnoreCase));
        if (refreshedHeader is null) return;
        _monitorThreadGrid.CurrentCell = refreshedHeader.Cells[0];
        if (firstDisplayed < 0 || _monitorThreadGrid.Rows.Count == 0) return;
        try { _monitorThreadGrid.FirstDisplayedScrollingRowIndex = Math.Min(firstDisplayed, _monitorThreadGrid.Rows.Count - 1); }
        catch (InvalidOperationException) { }
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
        using var dialog = new ProjectMonitorManagerDialog(_projectMonitorCli, _codexThreadCatalog);
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
        try { Process.Start(new ProcessStartInfo("codex://threads/" + item.ThreadId) { UseShellExecute = true }); }
        catch (Exception error) { ShowError("无法在 Codex 打开任务", error); }
    }

    private async Task RefreshTimerDataAsync()
    {
        await RefreshSessionsAsync();
        if (DateTimeOffset.UtcNow - _lastProjectMonitorRefresh >= TimeSpan.FromSeconds(20))
            await PopulateMonitorThreadGridAsync();
    }

    private void PopulateSessionGrid()
    {
        var selected = SelectedRows<SessionDefinition>(_sessionGrid).Select(item => item.Id).ToHashSet(StringComparer.OrdinalIgnoreCase);
        _sessionGrid.Rows.Clear();
        foreach (var session in _sessions)
        {
            var snapshot = _sessionManager.GetSnapshot(session);
            var auto = session.AutoStart || session.AutoRestart
                ? $"{(session.AutoStart ? "自启" : "")} {(session.AutoRestart ? $"重启×{session.MaxRestartAttempts}" : "")}".Trim()
                : "手动";
            var index = _sessionGrid.Rows.Add(session.Name, StatusText(snapshot.Status), snapshot.Message, auto,
                string.IsNullOrWhiteSpace(session.SourcePluginId) ? "内置" : session.SourcePluginId,
                snapshot.CheckedAt == DateTimeOffset.MinValue ? "—" : snapshot.CheckedAt.LocalDateTime.ToString("HH:mm:ss"));
            var row = _sessionGrid.Rows[index];
            row.Tag = session;
            row.Cells[1].Style.ForeColor = StatusColor(snapshot.Status);
            row.Selected = selected.Contains(session.Id);
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
            catch (Exception error) { ShowError($"启动“{session.Name}”失败", error); }
        await _configStore.SaveAsync(_config);
        PopulateSessionGrid();
    }

    private async Task StopSelectedSessionsAsync()
    {
        var selected = SelectedRows<SessionDefinition>(_sessionGrid).ToArray();
        if (selected.Length == 0) { ShowHint("请先选择一个或多个会话。"); return; }
        foreach (var session in selected)
            try { await _sessionManager.StopAsync(session); }
            catch (Exception error) { ShowError($"停止“{session.Name}”失败", error); }
        PopulateSessionGrid();
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
        using var dialog = new SessionDialog(item, _config.Sessions.Where(x => x.Id != item.Id).Select(x => x.Name));
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        dialog.Result.LastExecutedAt = item.LastExecutedAt;
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
            _timer.Interval = _config.Settings.RefreshIntervalSeconds * 1000;
            await _configStore.SaveAsync(_config);
            if (applyAutoStart) _autoStart.Apply(_config.Settings.AutoStartEnabled);
        }
        catch (Exception error) { ShowError("保存设置失败", error); }
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

    private void HideToTray()
    {
        Hide();
        ShowInTaskbar = false;
        TrimWorkingSet();
    }

    private void RestoreFromTray()
    {
        Opacity = 1;
        ShowInTaskbar = true;
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
        BringToFront();
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
        NativeMethods.ReleaseCapture();
        NativeMethods.SendMessage(Handle, NativeMethods.WmNcLButtonDown, (IntPtr)NativeMethods.HtCaption, IntPtr.Zero);
    }

    private int ResizeHitTest(Point point)
    {
        var edge = Math.Max(7, DeviceDpi / 14);
        var left = point.X >= 0 && point.X < edge;
        var right = point.X <= ClientSize.Width && point.X > ClientSize.Width - edge;
        var top = point.Y >= 0 && point.Y < edge;
        var bottom = point.Y <= ClientSize.Height && point.Y > ClientSize.Height - edge;
        if (left && top) return NativeMethods.HtTopLeft;
        if (right && top) return NativeMethods.HtTopRight;
        if (left && bottom) return NativeMethods.HtBottomLeft;
        if (right && bottom) return NativeMethods.HtBottomRight;
        if (left) return NativeMethods.HtLeft;
        if (right) return NativeMethods.HtRight;
        if (top) return NativeMethods.HtTop;
        if (bottom) return NativeMethods.HtBottom;
        return NativeMethods.HtClient;
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
