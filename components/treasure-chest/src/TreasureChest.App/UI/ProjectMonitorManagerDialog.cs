using System.Diagnostics;
using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class ProjectMonitorManagerDialog : Form
{
    private static readonly Font SearchResultFont = ApprovedMonitorManagerLayout.CreateBodyFont(FontStyle.Bold);
    internal bool SuppressInitialLoadForTests { get; set; }
    internal bool SuppressSearchWorkflowForTests { get; set; }
    internal int SearchWorkflowOpenRequestsForTests { get; private set; }
    private readonly ProjectMonitorCliService _monitorService;
    private readonly CodexThreadCatalogService _catalogService;
    private readonly SessionSearchCacheMode _sessionSearchCacheMode;
    private readonly CodexDesktopLauncher _threadLauncher = new();
    private readonly TextBox _search = new ThemedEmbeddedTextBox { Width = 250, Height = 40, AutoSize = false, PlaceholderText = "准确输入完整项目名称" };
    private readonly SlidingSegmentedControl _searchModeSelector = new("精确搜索", "模糊搜索") { Width = 190, Height = 40 };
    private readonly ComboBox _classification = new ThemedComboBox { Width = 170, Height = 40, ItemHeight = 28, DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly PillCheckBox _archived = new() { Text = "包含已归档", Checked = true, Width = 165, Height = 40, Margin = Padding.Empty };
    private readonly DataGridView _available = CreateCatalogGrid();
    private readonly DataGridView _manual = CreateMonitorGrid();
    private readonly DataGridView _automatic = CreateMonitorGrid();
    private readonly Label _availableTitle = SectionTitle("全部会话");
    private readonly Label _manualTitle = SectionTitle("长期监测");
    private readonly Label _automaticTitle = SectionTitle("临时监测");
    private readonly Label _status = new() { AutoSize = false, Dock = DockStyle.Fill, ForeColor = UiTheme.Muted, TextAlign = ContentAlignment.MiddleLeft };
    private readonly Button _refresh = UiTheme.Button("刷新全部", true, ButtonGlyph.Refresh);
    private readonly Button _manualPaste = UiTheme.Button("手动粘贴 ID", false, ButtonGlyph.Paste);
    private readonly Button _add = UiTheme.Button("加入", true, ButtonGlyph.ArrowRight);
    private readonly Button _remove = UiTheme.Button("移除", false, ButtonGlyph.ArrowLeft);
    private readonly Button _promote = UiTheme.Button("移入长期监测", true, ButtonGlyph.ArrowUp);
    private readonly CancellationTokenSource _lifetime = new();
    private readonly HashSet<string> _expandedAvailableGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private readonly HashSet<string> _expandedManualGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private readonly HashSet<string> _expandedAutomaticGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private IReadOnlyList<CodexThreadInfo> _catalog = [];
    private IReadOnlyList<ProjectMonitorItem> _monitors = [];
    private IReadOnlyList<SessionSearchMatch> _searchMatches = [];
    private bool _loading;
    private bool _syncingSelection;
    private bool _searchSlotAnimating;
    private SessionSearchMode _searchMode = SessionSearchMode.Exact;
    private string _exactProjectText = string.Empty;
    private SessionSearchRequest _fuzzyRequest = new(string.Empty, string.Empty, string.Empty, "auto");
    private bool _workingAreaConstraintPending;
    private readonly DpiLayoutStore _dpiLayout = new();
    private int _layoutDpi = DpiLayout.BaselineDpi;
    private Size _normalLogicalSize = ApprovedMonitorManagerLayout.DefaultWindowSize;
    private bool _dpiLayoutReady;
    private bool _initialDpiApplied;
    private bool _applyingDpiLayout;
    private bool _handlingDpiChange;

    public ProjectMonitorManagerDialog(
        ProjectMonitorCliService monitorService,
        CodexThreadCatalogService catalogService,
        SessionSearchCacheMode sessionSearchCacheMode = SessionSearchCacheMode.Persistent)
    {
        UiTheme.ConfigureDpiAwareForm(this, manuallyManaged: true);
        _monitorService = monitorService;
        _catalogService = catalogService;
        _sessionSearchCacheMode = sessionSearchCacheMode;
        ApplyManagerTypography();
        Text = sessionSearchCacheMode == SessionSearchCacheMode.Ephemeral
            ? "项目监测管理（隔离 QA）"
            : "项目监测管理";
        Icon = IconService.LoadAppIcon();
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = ApprovedMonitorManagerLayout.MinimumWindowSize;
        Size = ApprovedMonitorManagerLayout.DefaultWindowSize;
        FormBorderStyle = FormBorderStyle.Sizable;
        MaximizeBox = true;
        SetStyle(ControlStyles.ResizeRedraw | ControlStyles.OptimizedDoubleBuffer, true);
        Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        BackColor = UiTheme.Background;
        BuildLayout();
        WireEvents();
        _dpiLayout.CaptureBaseTree(this);
        _dpiLayoutReady = true;
        if (IsHandleCreated && !_initialDpiApplied)
            ApplyDpiLayout((int)NativeMethods.GetDpiForWindow(Handle), suggestedLocation: null, initial: true);
        Shown += async (_, _) =>
        {
            ScheduleWorkingAreaConstraint();
            UpdateResponsiveGridColumns();
            if (SuppressInitialLoadForTests) return;
            await LoadEverythingAsync();
        };
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        NativeMethods.TrySetWindowCornerPreference(
            Handle, WindowChromePresentation.DwmCornerPreference(WindowState));
        if (_dpiLayoutReady && !_initialDpiApplied)
            ApplyDpiLayout((int)NativeMethods.GetDpiForWindow(Handle), suggestedLocation: null, initial: true);
        ScheduleWorkingAreaConstraint();
    }

    protected override void WndProc(ref Message message)
    {
        var dpiChange = message.Msg == NativeMethods.WmDpiChanged;
        if (dpiChange) _handlingDpiChange = true;
        try { base.WndProc(ref message); }
        finally { if (dpiChange) _handlingDpiChange = false; }
        if (message.Msg == NativeMethods.WmDisplayChange)
            ScheduleWorkingAreaConstraint();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        ApplyDpiLayout(e.DeviceDpiNew, e.SuggestedRectangle.Location, initial: false);
        ScheduleWorkingAreaConstraint();
        UpdateResponsiveGridColumns();
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (IsHandleCreated)
            NativeMethods.TrySetWindowCornerPreference(
                Handle, WindowChromePresentation.DwmCornerPreference(WindowState));
        if (_dpiLayoutReady && !_applyingDpiLayout && !_handlingDpiChange && WindowState == FormWindowState.Normal)
            _normalLogicalSize = DpiLayout.Unscale(Size, _layoutDpi);
        if (WindowState == FormWindowState.Normal) ScheduleWorkingAreaConstraint();
    }

    protected override void OnLayout(LayoutEventArgs levent)
    {
        base.OnLayout(levent);
        UpdateResponsiveGridColumns();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _lifetime.Cancel();
        _lifetime.Dispose();
        base.OnFormClosed(e);
    }

    private void ApplyManagerTypography()
    {
        Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        _search.Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        _searchModeSelector.Font = ApprovedMonitorManagerLayout.CreateBodyFont(FontStyle.Bold);
        _classification.Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        _archived.Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        _status.Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        if (_archived is PillCheckBox pill)
        {
            var preferred = pill.GetPreferredSize(Size.Empty);
            pill.MinimumSize = new Size(preferred.Width + 6, ApprovedMonitorManagerLayout.ToolbarControlHeight);
            pill.Width = Math.Max(pill.Width, pill.MinimumSize.Width);
        }
        foreach (var button in new[] { _refresh, _manualPaste, _add, _remove, _promote })
            ConfigureManagerButton(button);
    }

    private static void ConfigureManagerButton(Button button, int minimumWidth = 0)
    {
        button.Font = ApprovedMonitorManagerLayout.CreateBodyFont();
        button.Padding = new Padding(10, 0, 10, 0);
        if (minimumWidth > 0) button.MinimumSize = new Size(minimumWidth, UiTheme.ButtonHeight);
        if (button is RoundedButton rounded)
        {
            rounded.GlyphSizeLogical = 14;
        }
    }

    private void BuildLayout()
    {
        var header = new ManagerPageHeaderPanel(
            "项目监测管理",
            "长期与临时监测分开管理；左侧列出全部会话及监测状态，项目会话排在个人对话之前。")
        {
            Dock = DockStyle.Top,
            Height = ApprovedMonitorManagerLayout.HeaderHeight,
            BackColor = UiTheme.Background,
        };

        var toolbar = new MonitorToolbarPanel(
            _search,
            _searchModeSelector,
            _classification,
            _archived,
            _refresh,
            _manualPaste);
        toolbar.Dock = DockStyle.Fill;
        toolbar.DrawShadow = true;
        toolbar.CornerRadiusLogical = 22;
        var toolbarHost = new Panel
        {
            Dock = DockStyle.Top,
            Height = ApprovedMonitorManagerLayout.ToolbarHostHeight,
            BackColor = UiTheme.Background,
            Padding = new Padding(12, 4, 12, 6),
        };
        toolbarHost.Controls.Add(toolbar);

        var body = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(
                ApprovedMonitorManagerLayout.ContentHorizontalInset,
                ApprovedMonitorManagerLayout.ContentTopInset,
                ApprovedMonitorManagerLayout.ContentHorizontalInset,
                ApprovedMonitorManagerLayout.ContentBottomInset),
            ColumnCount = 3,
            RowCount = 1,
            BackColor = UiTheme.Background,
        };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, ApprovedMonitorManagerLayout.CatalogColumnPercent));
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, ApprovedMonitorManagerLayout.TransferColumnWidth));
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, ApprovedMonitorManagerLayout.MonitorColumnPercent));
        var catalogSection = ThreadSection(_availableTitle, _available);
        catalogSection.Margin = Padding.Empty;
        var transferButtons = BuildTransferButtons();
        transferButtons.Margin = Padding.Empty;
        var monitorSections = BuildMonitorSections();
        monitorSections.Margin = Padding.Empty;
        body.Controls.Add(catalogSection, 0, 0);
        body.Controls.Add(transferButtons, 1, 0);
        body.Controls.Add(monitorSections, 2, 0);

        var footer = new TableLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = ApprovedMonitorManagerLayout.FooterHeight,
            Padding = new Padding(24, 6, 24, 6),
            ColumnCount = 2,
            BackColor = UiTheme.Background,
        };
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        footer.Controls.Add(_status, 0, 0);
        var close = UiTheme.Button("关闭", false, ButtonGlyph.Close);
        ConfigureManagerButton(close, 100);
        close.AutoSize = false;
        close.Size = new Size(100, 42);
        close.Click += (_, _) => Close();
        footer.Controls.Add(close, 1, 0);

        Controls.Add(body);
        Controls.Add(footer);
        Controls.Add(toolbarHost);
        Controls.Add(header);
        CancelButton = close;
        foreach (var grid in new[] { _available, _manual, _automatic })
            grid.SizeChanged += (_, _) => UpdateResponsiveGridColumns();
    }

    private Control BuildTransferButtons()
    {
        var host = new AlignedButtonStackPanel(_add, _remove)
        {
            Dock = DockStyle.Fill,
            Margin = Padding.Empty,
            BackColor = UiTheme.Background,
        };
        foreach (var button in new[] { _add, _remove })
        {
            button.AutoSize = false;
            button.Size = new Size(90, 42);
            button.Margin = Padding.Empty;
            button.TextAlign = ContentAlignment.MiddleCenter;
        }
        return host;
    }

    private Control BuildMonitorSections()
    {
        var card = new SurfacePanel
        {
            Dock = DockStyle.Fill,
            BackColor = UiTheme.Surface,
            Padding = new Padding(16),
            CornerRadiusLogical = 24,
            DrawShadow = true,
        };
        var stack = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1, BackColor = UiTheme.Surface };
        stack.RowStyles.Add(new RowStyle(SizeType.Percent, 57));
        stack.RowStyles.Add(new RowStyle(SizeType.Percent, 43));
        stack.Controls.Add(ThreadSubsection(_manualTitle, _manual), 0, 0);

        var automaticSection = ThreadSubsection(_automaticTitle, _automatic, _promote);
        automaticSection.Margin = new Padding(0, 14, 0, 0);
        _promote.AutoSize = false;
        _promote.Size = new Size(160, 40);
        _promote.Margin = Padding.Empty;
        _promote.TextAlign = ContentAlignment.MiddleCenter;
        stack.Controls.Add(automaticSection, 0, 1);
        card.Controls.Add(stack);
        return card;
    }

    private static Control ThreadSection(Label title, DataGridView grid)
    {
        var panel = new SurfacePanel
        {
            Dock = DockStyle.Fill,
            BackColor = UiTheme.Surface,
            Padding = new Padding(16),
            CornerRadiusLogical = 24,
            DrawShadow = true,
        };
        var content = ThreadSubsection(title, grid);
        panel.Controls.Add(content);
        return panel;
    }

    private static Panel ThreadSubsection(Label title, DataGridView grid, Button? action = null)
    {
        var panel = new Panel { Dock = DockStyle.Fill, BackColor = UiTheme.Surface };
        var titlePanel = new SectionActionHeaderPanel(title, action)
        {
            Dock = DockStyle.Top,
            Height = 52,
            BackColor = UiTheme.Surface,
            Padding = Padding.Empty,
        };
        var gridWell = new GridWellPanel
        {
            Dock = DockStyle.Fill,
            Margin = Padding.Empty,
        };
        gridWell.Controls.Add(grid);
        panel.Controls.Add(gridWell);
        panel.Controls.Add(titlePanel);
        return panel;
    }

    private static Label SectionTitle(string text) => new()
    {
        Text = text,
        ForeColor = UiTheme.Text,
        Font = ApprovedMonitorManagerLayout.CreateSectionFont(),
        TextAlign = ContentAlignment.MiddleLeft,
    };

    private static DataGridView CreateBaseGrid()
    {
        var grid = new LiveResizableDataGridView
        {
            Font = ApprovedMonitorManagerLayout.CreateBodyFont(),
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
            ColumnHeadersHeight = 44,
            RowTemplate = { Height = DrawerVisualMetrics.ForDpi(DpiLayout.BaselineDpi).RowHeight },
            GridColor = UiTheme.GridLine,
            ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = UiTheme.Surface,
                ForeColor = UiTheme.Text,
                SelectionBackColor = UiTheme.Surface,
                SelectionForeColor = UiTheme.Text,
                Font = ApprovedMonitorManagerLayout.CreateBodyFont(FontStyle.Bold),
            },
            DefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = UiTheme.Surface,
                ForeColor = UiTheme.Text,
                SelectionBackColor = UiTheme.AccentSoft,
                SelectionForeColor = UiTheme.Text,
                Padding = new Padding(5, 0, 5, 0),
            },
        };
        DrawerRowPresentation.ConfigureGrid(
            grid,
            ApprovedMonitorManagerLayout.CreateBodyFont(),
            ApprovedMonitorManagerLayout.CreateBodyFont(FontStyle.Bold));
        GridVisualStyler.Configure(grid);
        return grid;
    }

    private static DataGridView CreateCatalogGrid()
    {
        var grid = CreateBaseGrid();
        grid.Columns.Add(ResizableTitleColumn());
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "LastActivity", HeaderText = "最后活动", Width = 132 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "MonitoringStatus", HeaderText = "监测状态", Width = 120 });
        GridVisualStyler.Configure(grid, chatColumn: "Title", statusColumn: "MonitoringStatus");
        return grid;
    }

    private static DataGridView CreateMonitorGrid()
    {
        var grid = CreateBaseGrid();
        grid.Columns.Add(ResizableTitleColumn());
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "LastActivity", HeaderText = "最后活动", Width = 132 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Remaining", HeaderText = "有效时间", Width = 110 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Id", HeaderText = "完整任务 ID", Width = 285 });
        GridVisualStyler.Configure(grid, chatColumn: "Title");
        return grid;
    }

    private static DataGridViewTextBoxColumn ResizableTitleColumn() => new()
    {
        Name = "Title",
        HeaderText = "会话名称",
        AutoSizeMode = DataGridViewAutoSizeColumnMode.None,
        Width = 220,
        MinimumWidth = 120,
        Resizable = DataGridViewTriState.True,
        SortMode = DataGridViewColumnSortMode.NotSortable,
    };

    private void UpdateResponsiveGridColumns()
    {
        if (_available.ClientSize.Width > 1)
        {
            var columns = ApprovedMonitorManagerLayout.CatalogColumns(_available.ClientSize.Width, _available.DeviceDpi);
            SetColumnWidths(_available, columns.Title, columns.LastActivity, columns.MonitoringStatus);
        }
        foreach (var grid in new[] { _manual, _automatic })
        {
            if (grid.ClientSize.Width <= 1) continue;
            var columns = ApprovedMonitorManagerLayout.MonitorColumns(grid.ClientSize.Width, grid.DeviceDpi);
            SetColumnWidths(grid, columns.Title, columns.LastActivity, columns.Remaining, columns.ThreadId);
        }
    }

    private static void SetColumnWidths(DataGridView grid, params int[] widths)
    {
        for (var index = 0; index < Math.Min(grid.Columns.Count, widths.Length); index++)
        {
            var width = Math.Max(grid.Columns[index].MinimumWidth, widths[index]);
            if (grid.Columns[index].Width != width) grid.Columns[index].Width = width;
        }
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
                if (IsDisposed || Disposing ||
                    !WindowChromePresentation.ShouldConstrainToWorkingArea(WindowState)) return;
                var current = Bounds;
                var workingArea = Screen.FromRectangle(current).WorkingArea;
                var dpi = (int)NativeMethods.GetDpiForWindow(Handle);
                var constrained = DpiLayout.ConstrainToWorkingArea(
                    current.Location, current.Size, workingArea, dpi);
                if (constrained != current) Bounds = constrained;
            }));
        }
        catch (InvalidOperationException)
        {
            _workingAreaConstraintPending = false;
        }
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
            MinimumSize = ApprovedMonitorManagerLayout.DeviceMinimumWindowSize(targetDpi);
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
            UpdateResponsiveGridColumns();
        }
        finally
        {
            ResumeLayout(performLayout: true);
            _applyingDpiLayout = false;
        }
        Invalidate(true);
    }

    private void WireEvents()
    {
        _search.TextChanged += (_, _) => { if (_searchMode == SessionSearchMode.Exact && !_searchSlotAnimating) Render(); };
        _searchModeSelector.SelectedIndexChanged += (_, _) =>
            SelectSearchMode(_searchModeSelector.SelectedIndex == 0 ? SessionSearchMode.Exact : SessionSearchMode.Fuzzy);
        _search.Click += (_, _) => { if (_searchMode == SessionSearchMode.Fuzzy) RunFuzzySearch(); };
        _search.KeyDown += (_, e) =>
        {
            if (_searchMode != SessionSearchMode.Fuzzy || e.KeyCode is not (Keys.Enter or Keys.Space)) return;
            RunFuzzySearch();
            e.Handled = true;
            e.SuppressKeyPress = true;
        };
        _classification.SelectedIndexChanged += (_, _) => Render();
        _archived.CheckedChanged += (_, _) => Render();
        _refresh.Click += async (_, _) => await LoadEverythingAsync();
        _manualPaste.Click += async (_, _) => await ManualPasteAsync();
        _add.Click += async (_, _) => await AddSelectedAsync();
        _remove.Click += async (_, _) => await RemoveSelectedAsync();
        _promote.Click += async (_, _) => await PromoteSelectedAsync();
        _available.CellDoubleClick += (_, e) => { if (e.RowIndex >= 0 && _available.Rows[e.RowIndex].Tag is CodexThreadInfo item) OpenThread(item.Id); };
        _manual.CellDoubleClick += (_, e) => { if (e.RowIndex >= 0 && _manual.Rows[e.RowIndex].Tag is ProjectMonitorItem item) OpenThread(item.ThreadId); };
        _automatic.CellDoubleClick += (_, e) => { if (e.RowIndex >= 0 && _automatic.Rows[e.RowIndex].Tag is ProjectMonitorItem item) OpenThread(item.ThreadId); };
        foreach (var grid in new[] { _available, _manual, _automatic })
        {
            grid.CellMouseDown += (_, e) =>
            {
                if (DrawerAnimationController.IsToggleGesture(e.Button, e.Clicks)) ToggleDrawer(grid, e.RowIndex);
            };
            grid.CellMouseEnter += (_, e) =>
                grid.Cursor = e.RowIndex >= 0 && grid.Rows[e.RowIndex].Tag is DrawerGroup ? Cursors.Hand : Cursors.Default;
            grid.CellMouseLeave += (_, _) => grid.Cursor = Cursors.Default;
            grid.KeyDown += (_, e) =>
            {
                if (e.KeyCode is not (Keys.Enter or Keys.Space) || grid.CurrentRow?.Tag is not DrawerGroup) return;
                ToggleDrawer(grid, grid.CurrentRow.Index);
                e.Handled = true;
                e.SuppressKeyPress = true;
            };
        }
        _available.SelectionChanged += (_, _) => ClearOtherSelections(_available);
        _manual.SelectionChanged += (_, _) => ClearOtherSelections(_manual);
        _automatic.SelectionChanged += (_, _) => ClearOtherSelections(_automatic);
    }

    private async Task LoadEverythingAsync()
    {
        if (_loading) return;
        SetLoading(true, "正在读取 Codex 会话与项目监测列表…");
        try
        {
            var catalogTask = _catalogService.LoadAsync(_lifetime.Token);
            var monitorTask = _monitorService.ListAsync(_lifetime.Token);
            await Task.WhenAll(catalogTask, monitorTask);
            _catalog = await catalogTask;
            _monitors = await monitorTask;
            RebuildClassificationFilter();
            Render();
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
        catch (Exception error) { ShowError("刷新项目监测失败", error); }
        finally { if (!IsDisposed) SetLoading(false, _status.Text); }
    }

    private void RebuildClassificationFilter()
    {
        var previous = _classification.SelectedItem?.ToString() ?? "全部项目与个人对话";
        var values = _catalog.Select(item => ProjectMonitorPresentation.NormalizeClassification(item.Classification))
            .Concat(_monitors.Select(item => ProjectMonitorPresentation.NormalizeClassification(item.Classification)))
            .Distinct(StringComparer.CurrentCultureIgnoreCase)
            .OrderBy(value => ProjectMonitorPresentation.IsPersonal(value) ? 1 : 0)
            .ThenBy(value => value, StringComparer.CurrentCultureIgnoreCase)
            .ToArray();
        _classification.Items.Clear();
        _classification.Items.Add("全部项目与个人对话");
        _classification.Items.AddRange(values);
        _classification.SelectedItem = _classification.Items.Contains(previous) ? previous : "全部项目与个人对话";
    }

    private void Render()
    {
        var exactQuery = _searchMode == SessionSearchMode.Exact
            ? SessionSearchPresentation.NormalizeExactProjectName(_search.Text)
            : string.Empty;
        if (_classification.Items.Count == 0)
        {
            UpdateModeStatus(exactQuery, exactProjectExists: false, availableCount: 0);
            return;
        }
        var exactProjectExists = SessionSearchPresentation.HasExactProject(
            _catalog.Select(item => item.Classification).Concat(_monitors.Select(item => item.Classification)),
            exactQuery);
        var available = SessionSearchPresentation.OrderCatalogWithMatches(_catalog
            .Where(item => ProjectMonitorPresentation.IsCatalogVisible(item, _archived.Checked))
            .Where(MatchesFilter), _searchMatches);
        var manual = ProjectMonitorPresentation.OrderWithinOrigin(_monitors.Where(item => item.IsManual && MatchesFilter(item)));
        var automatic = ProjectMonitorPresentation.OrderWithinOrigin(_monitors.Where(item => !item.IsManual && MatchesFilter(item)));

        FillCatalogGrid(_available, available, _monitors, _expandedAvailableGroups);
        ApplySearchHighlights();
        FillMonitorGrid(_manual, manual, _expandedManualGroups);
        FillMonitorGrid(_automatic, automatic, _expandedAutomaticGroups);
        _availableTitle.Text = ProjectMonitorPresentation.CatalogCountTitle(available.Count, _catalog.Count);
        _manualTitle.Text = $"长期监测（{manual.Count}，长期有效）";
        _automaticTitle.Text = $"临时监测（{automatic.Count}，24 小时规则）";
        _promote.Enabled = !_loading && automatic.Count > 0;
        UpdateModeStatus(exactQuery, exactProjectExists, available.Count);
    }

    private string BuildLoadedSummary() =>
        $"已加载 {_catalog.Count} 个 Codex 会话；长期监测 {_monitors.Count(item => item.IsManual)} 个，" +
        $"临时监测 {_monitors.Count(item => !item.IsManual)} 个。";

    private void UpdateModeStatus(string exactQuery, bool exactProjectExists, int availableCount)
    {
        if (_searchMode == SessionSearchMode.Fuzzy)
        {
            _status.Text = SessionSearchPresentation.ModeHint(SessionSearchMode.Fuzzy);
            return;
        }
        _status.Text = exactQuery.Length == 0
            ? BuildLoadedSummary()
            : exactProjectExists
                ? $"精确项目“{exactQuery}”：全部会话中匹配 {availableCount} 个。"
                : $"没有名为“{exactQuery}”的项目。精确搜索只接受修剪后的完整项目名称。";
    }

    private bool MatchesFilter(CodexThreadInfo item) =>
        MatchesClassification(item.Classification) && MatchesExactProject(item.Classification);

    private bool MatchesFilter(ProjectMonitorItem item) =>
        MatchesClassification(item.Classification) && MatchesExactProject(item.Classification);

    private bool MatchesClassification(string value)
    {
        if (_searchMode == SessionSearchMode.Exact && SessionSearchPresentation.NormalizeExactProjectName(_search.Text).Length > 0)
            return true;
        var selected = _classification.SelectedItem?.ToString() ?? "全部项目与个人对话";
        return selected == "全部项目与个人对话" ||
            ProjectMonitorPresentation.NormalizeClassification(value).Equals(selected, StringComparison.CurrentCultureIgnoreCase);
    }

    private bool MatchesExactProject(string classification) =>
        _searchMode != SessionSearchMode.Exact || SessionSearchPresentation.ExactProjectMatches(classification, _search.Text);

    private void SelectSearchMode(SessionSearchMode mode)
    {
        var targetIndex = mode == SessionSearchMode.Exact ? 0 : 1;
        // Programmatic selector synchronization raises SelectedIndexChanged
        // synchronously. Once both state holders already reached the requested
        // endpoint, that nested call must not save the fuzzy summary as exact text.
        if (_searchMode == mode && _searchModeSelector.SelectedIndex == targetIndex) return;
        if (_searchMode == SessionSearchMode.Exact) _exactProjectText = _search.Text;
        _searchMode = mode;
        if (_searchModeSelector.SelectedIndex != targetIndex) _searchModeSelector.Select(targetIndex);
        AnimateSearchSlotChange(mode);
        _search.ReadOnly = mode == SessionSearchMode.Fuzzy;
        _search.Cursor = mode == SessionSearchMode.Fuzzy ? Cursors.Hand : Cursors.IBeam;
        _classification.Enabled = !_loading && mode == SessionSearchMode.Exact;
        Render();
    }

    private static void FillCatalogGrid(
        DataGridView grid,
        IReadOnlyList<CodexThreadInfo> records,
        IReadOnlyList<ProjectMonitorItem> monitors,
        HashSet<string> expandedGroups)
    {
        grid.Rows.Clear();
        foreach (var group in records.GroupBy(item => item.Classification, StringComparer.CurrentCultureIgnoreCase))
        {
            var children = group.ToList();
            var key = ProjectMonitorPresentation.NormalizeClassification(group.Key);
            var expanded = expandedGroups.Contains(key);
            AddGroupRow(grid, key, children.Count, expanded);
            if (!expanded) continue;
            for (var childIndex = 0; childIndex < children.Count; childIndex++)
            {
                var item = children[childIndex];
                var status = ProjectMonitorPresentation.MonitoringStatus(item, monitors);
                var index = grid.Rows.Add(
                    Truncate(item.DisplayTitle, 100),
                    ProjectMonitorPresentation.FormatCatalogTimestamp(item.UpdatedAtMs),
                    ProjectMonitorPresentation.MonitoringStatusText(status));
                var row = grid.Rows[index];
                row.Tag = item;
                DrawerRowPresentation.Apply(
                    row,
                    DrawerRowKind.Child,
                    grid.DeviceDpi,
                    isLastChild: childIndex == children.Count - 1);
                row.Cells[0].ToolTipText = item.DisplayTitle;
                row.Cells[2].ToolTipText = $"完整任务 ID：{item.Id}";
                row.Cells[2].Style.ForeColor = status switch
                {
                    CatalogMonitoringStatus.LongTerm => UiTheme.Running,
                    CatalogMonitoringStatus.Temporary => UiTheme.Warning,
                    _ => UiTheme.Muted,
                };
                if (item.Archived) row.DefaultCellStyle.ForeColor = UiTheme.Muted;
            }
        }
        grid.ClearSelection();
    }

    private static void FillMonitorGrid(DataGridView grid, IReadOnlyList<ProjectMonitorItem> records, HashSet<string> expandedGroups)
    {
        grid.Rows.Clear();
        foreach (var group in records.GroupBy(item => item.Classification, StringComparer.CurrentCultureIgnoreCase))
        {
            var children = group.ToList();
            var key = ProjectMonitorPresentation.NormalizeClassification(group.Key);
            var expanded = expandedGroups.Contains(key);
            AddGroupRow(grid, key, children.Count, expanded);
            if (!expanded) continue;
            for (var childIndex = 0; childIndex < children.Count; childIndex++)
            {
                var item = children[childIndex];
                var index = grid.Rows.Add(
                    Truncate(item.Title, 100),
                    ProjectMonitorPresentation.FormatTimestamp(item.LastActivityAt),
                    ProjectMonitorPresentation.FormatRemaining(item),
                    item.ThreadId);
                var row = grid.Rows[index];
                row.Tag = item;
                DrawerRowPresentation.Apply(
                    row,
                    DrawerRowKind.Child,
                    grid.DeviceDpi,
                    isLastChild: childIndex == children.Count - 1);
                row.Cells[0].ToolTipText = item.Title;
                row.Cells[3].ToolTipText = item.ThreadId;
                if (!item.IsManual && item.ExpiresAt <= DateTimeOffset.Now) row.Cells[2].Style.ForeColor = UiTheme.Stopped;
            }
        }
        grid.ClearSelection();
    }

    private static void AddGroupRow(DataGridView grid, string key, int count, bool expanded)
    {
        var values = new object[grid.Columns.Count];
        values[0] = ProjectMonitorPresentation.DrawerGroupTitle(key, count, expanded);
        for (var index = 1; index < values.Length; index++) values[index] = string.Empty;
        var rowIndex = grid.Rows.Add(values);
        var row = grid.Rows[rowIndex];
        row.Tag = new DrawerGroup(key);
        row.ReadOnly = true;
        row.Height = DrawerVisualMetrics.ForDpi(grid.DeviceDpi).RowHeight;
        DrawerRowPresentation.Apply(
            row,
            expanded ? DrawerRowKind.ExpandedGroup : DrawerRowKind.CollapsedGroup,
            grid.DeviceDpi);
        row.Cells[0].ToolTipText = expanded ? "单击收起这个分组" : "单击展开这个分组";
    }

    private void ToggleDrawer(DataGridView grid, int rowIndex)
    {
        if (rowIndex < 0 || grid.Rows[rowIndex].Tag is not DrawerGroup drawer) return;
        var scrollPositions = CaptureScrollPositions();
        var expandedGroups = ExpandedGroupsFor(grid);
        var targetExpanded = DrawerAnimationController.NextExpandedState(expandedGroups.Contains(drawer.Key));
        if (!targetExpanded)
        {
            var children = DrawerAnimationController.ChildRowsAfter(grid, rowIndex);
            // Record the requested destination immediately.  A second click while
            // the rows are still collapsing must therefore take the opposite
            // branch and replace this keyed animation from its current geometry.
            expandedGroups.Remove(drawer.Key);
            DrawerAnimationController.Collapse(grid, drawer.Key, children, () =>
            {
                Render();
                RestoreDrawerPosition(grid, drawer, scrollPositions);
            });
            return;
        }
        var reversingChildren = DrawerAnimationController.ChildRowsAfter(grid, rowIndex);
        expandedGroups.Add(drawer.Key);
        if (DrawerAnimationController.TryReverseToExpanded(grid, drawer.Key, reversingChildren))
        {
            RestoreDrawerPosition(grid, drawer, scrollPositions);
            return;
        }
        Render();
        var refreshedHeader = RestoreDrawerPosition(grid, drawer, scrollPositions);
        if (refreshedHeader is not null)
            DrawerAnimationController.Expand(grid, drawer.Key, DrawerAnimationController.ChildRowsAfter(grid, refreshedHeader.Index));
    }

    private DataGridViewRow? RestoreDrawerPosition(
        DataGridView grid,
        DrawerGroup drawer,
        IReadOnlyDictionary<DataGridView, int> scrollPositions)
    {
        var refreshedHeader = grid.Rows.Cast<DataGridViewRow>()
            .FirstOrDefault(row => row.Tag is DrawerGroup group && group.Key.Equals(drawer.Key, StringComparison.CurrentCultureIgnoreCase));
        if (refreshedHeader is not null) grid.CurrentCell = refreshedHeader.Cells[0];
        RestoreScrollPositions(scrollPositions);
        grid.Invalidate(true);
        return refreshedHeader;
    }

    private Dictionary<DataGridView, int> CaptureScrollPositions()
    {
        var positions = new Dictionary<DataGridView, int>();
        foreach (var grid in new[] { _available, _manual, _automatic })
        {
            if (grid.Rows.Count == 0) continue;
            var firstDisplayed = grid.FirstDisplayedScrollingRowIndex;
            if (firstDisplayed >= 0) positions[grid] = firstDisplayed;
        }
        return positions;
    }

    private static void RestoreScrollPositions(IReadOnlyDictionary<DataGridView, int> positions)
    {
        foreach (var (grid, position) in positions)
        {
            if (grid.Rows.Count == 0) continue;
            try { grid.FirstDisplayedScrollingRowIndex = Math.Min(position, grid.Rows.Count - 1); }
            catch (InvalidOperationException) { }
        }
    }

    private HashSet<string> ExpandedGroupsFor(DataGridView grid)
    {
        if (ReferenceEquals(grid, _available)) return _expandedAvailableGroups;
        if (ReferenceEquals(grid, _manual)) return _expandedManualGroups;
        return _expandedAutomaticGroups;
    }

    private async Task AddSelectedAsync()
    {
        var items = SelectedRecords<CodexThreadInfo>(_available)
            .Where(item => ProjectMonitorPresentation.MonitoringStatus(item, _monitors) != CatalogMonitoringStatus.LongTerm)
            .ToArray();
        if (items.Length == 0) { ShowHint("请先在左侧选择一个未监测或临时监测的会话；已是长期监测的会话无需重复加入。"); return; }
        var animation = TransferOverlayAnimator.ShouldAnimate(MonitorTransferBucket.AllSessions, MonitorTransferBucket.LongTerm)
            ? TransferAnimation(_available, _manual, items[0].Id)
            : null;
        await ExecuteMutationAsync(items.Select(item => item.Id), id => _monitorService.AddAsync(id, _lifetime.Token),
            $"已将 {items.Length} 个会话加入长期监测。", "加入长期监测失败", animation);
    }

    private void RunFuzzySearch()
    {
        if (SuppressSearchWorkflowForTests)
        {
            SearchWorkflowOpenRequestsForTests++;
            return;
        }
        var request = _fuzzyRequest;
        var autoStart = false;
        while (!_lifetime.IsCancellationRequested)
        {
            using var workflow = new SessionSearchWorkflowDialog(
                _monitorService,
                request,
                autoStart,
                _lifetime.Token,
                _sessionSearchCacheMode);
            if (workflow.ShowDialog(this) != DialogResult.OK) return;
            request = workflow.SearchRequest;
            _fuzzyRequest = request;
            UpdateFuzzySearchSlot();
            if (workflow.SearchResult is { } result)
            {
                if (result.Matches.Count > 0) ApplySearchResult(result);
                else ClearSearchResult();
                using var resultDialog = new SessionSearchResultDialog(result);
                resultDialog.ShowDialog(this);
                _status.Text = BuildSearchStatus(result);
                if (!resultDialog.ExpandRequested) return;
                if (!result.CanExpand || string.IsNullOrWhiteSpace(result.NextScope))
                {
                    ShowHint("当前搜索结果不能继续扩大范围。");
                    return;
                }
                request = request.WithScope(result.NextScope);
                autoStart = true;
                continue;
            }

            ClearSearchResult();
            using var failureDialog = new SessionSearchResultDialog(
                result: null,
                failureMessage: workflow.FailureMessage,
                cancelled: workflow.WasCancelled);
            failureDialog.ShowDialog(this);
            _status.Text = workflow.WasCancelled ? "会话搜索已取消；临时文件已清理。" : workflow.FailureMessage ?? "会话搜索没有返回结果。";
            return;
        }
    }

    private void AnimateSearchSlotChange(SessionSearchMode mode)
    {
        var oldText = _search.Text;
        var newText = mode == SessionSearchMode.Exact ? _exactProjectText : FuzzySummary();
        _searchSlotAnimating = true;
        _search.PlaceholderText = mode == SessionSearchMode.Exact
            ? "准确输入完整项目名称"
            : "填写模糊条件";
        SearchSlotAnimator.Transition(
            _search,
            oldText,
            newText,
            oldGlyph: mode == SessionSearchMode.Exact ? ButtonGlyph.Filter : ButtonGlyph.Search,
            newGlyph: mode == SessionSearchMode.Exact ? ButtonGlyph.Search : ButtonGlyph.Filter,
            completed: () =>
            {
                _searchSlotAnimating = false;
                _search.ForeColor = UiTheme.Text;
                if (mode == SessionSearchMode.Exact) Render();
            });
    }

    private void UpdateFuzzySearchSlot()
    {
        if (_searchMode != SessionSearchMode.Fuzzy) return;
        _search.Text = FuzzySummary();
        _search.ForeColor = UiTheme.Text;
    }

    private string FuzzySummary()
    {
        var clues = new[] { _fuzzyRequest.Name, _fuzzyRequest.Description, _fuzzyRequest.LastActivity }
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .Select(value => value.Trim())
            .ToArray();
        return clues.Length == 0 ? "填写模糊条件" : string.Join(" · ", clues.Select(value => Truncate(value, 16)));
    }


    private void ApplySearchResult(SessionSearchResult result)
    {
        _searchMatches = SessionSearchPresentation.OrderMatches(result.Matches);
        var catalogById = _catalog.ToDictionary(item => item.Id, StringComparer.OrdinalIgnoreCase);
        foreach (var match in _searchMatches)
            if (catalogById.TryGetValue(match.ThreadId, out var item))
                _expandedAvailableGroups.Add(ProjectMonitorPresentation.NormalizeClassification(item.Classification));
        Render();
        ScrollBestSearchMatchIntoView();
    }

    private void ClearSearchResult()
    {
        if (_searchMatches.Count == 0) return;
        _searchMatches = [];
        Render();
    }

    private void ApplySearchHighlights()
    {
        if (_searchMatches.Count == 0) return;
        var ranks = _searchMatches.Select((item, index) => (item.ThreadId, index))
            .GroupBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.Min(item => item.index), StringComparer.OrdinalIgnoreCase);
        var details = _searchMatches.GroupBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.OrdinalIgnoreCase);
        foreach (DataGridViewRow row in _available.Rows)
        {
            if (row.Tag is not CodexThreadInfo item || !ranks.TryGetValue(item.Id, out var rank)) continue;
            var backColor = rank == 0 ? UiTheme.AccentSoftStrong : UiTheme.AccentSoft;
            row.DefaultCellStyle.BackColor = backColor;
            row.DefaultCellStyle.SelectionBackColor = rank == 0 ? UiTheme.Accent : UiTheme.AccentSoftStrong;
            row.DefaultCellStyle.ForeColor = UiTheme.Text;
            row.DefaultCellStyle.SelectionForeColor = rank == 0 ? UiTheme.OnAccent : UiTheme.Text;
            row.DefaultCellStyle.Font = SearchResultFont;
            row.Cells[0].Value = $"#{rank + 1}  {Truncate(item.DisplayTitle, 94)}";
            var match = details[item.Id];
            row.Cells[0].ToolTipText = $"{item.DisplayTitle}\n匹配度 {match.Score:0.000}\n{match.Reason}";
        }
    }

    private void ScrollBestSearchMatchIntoView()
    {
        var bestId = _searchMatches.FirstOrDefault()?.ThreadId;
        if (bestId is null) return;
        var target = _available.Rows.Cast<DataGridViewRow>()
            .FirstOrDefault(row => row.Tag is CodexThreadInfo item && item.Id.Equals(bestId, StringComparison.OrdinalIgnoreCase));
        if (target is null)
        {
            _status.Text = "搜索已返回候选，但第一名不在当前 Codex 目录页中；请在结果窗口查看完整任务 ID。";
            return;
        }
        var displayed = Math.Max(1, _available.DisplayedRowCount(includePartialRow: false));
        try
        {
            _available.FirstDisplayedScrollingRowIndex = SessionSearchPresentation.CenteredFirstRow(target.Index, displayed, _available.Rows.Count);
        }
        catch (InvalidOperationException) { }
        _available.ClearSelection();
    }

    private static string BuildSearchStatus(SessionSearchResult result)
    {
        var heading = SessionSearchPresentation.ResultHeading(result.Status);
        var warning = result.Warnings.Count > 0 ? $"；有 {result.Warnings.Count} 条不完整记录" : string.Empty;
        return $"{heading}：范围 {result.ScopeLabel}，读取 {result.ExaminedCount} 个会话，候选 {result.Matches.Count} 个{warning}。";
    }

    private async Task ManualPasteAsync()
    {
        using var dialog = new ThreadIdDialog();
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        await ExecuteMutationAsync(dialog.ThreadIds, id => _monitorService.AddAsync(id, _lifetime.Token),
            $"已将 {dialog.ThreadIds.Count} 个任务加入长期监测。", "加入长期监测失败");
    }

    private async Task PromoteSelectedAsync()
    {
        var items = SelectedRecords<ProjectMonitorItem>(_automatic).ToArray();
        if (items.Length == 0) { ShowHint("请先在临时监测列表选择一个或多个会话。"); return; }
        await ExecuteMutationAsync(items.Select(item => item.ThreadId), id => _monitorService.AddAsync(id, _lifetime.Token),
            $"已将 {items.Length} 个临时监测移入长期监测。", "移入长期监测失败");
    }

    private async Task RemoveSelectedAsync()
    {
        var items = SelectedRecords<ProjectMonitorItem>(_manual)
            .Concat(SelectedRecords<ProjectMonitorItem>(_automatic))
            .DistinctBy(item => item.ThreadId, StringComparer.OrdinalIgnoreCase)
            .ToArray();
        if (items.Length == 0) { ShowHint("请先在右侧选择一个或多个监测任务。分组标题不能移除。"); return; }
        if (MessageBox.Show(this,
                $"明确移除选中的 {items.Length} 个监测任务？\n\nFeiShuBOT 会记录用户抑制，自动发现不会立刻把它们加回来；Codex 任务本身不会被删除。",
                "确认明确移除", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        var manualItem = items.FirstOrDefault(item => item.IsManual);
        var animation = manualItem is not null &&
                        TransferOverlayAnimator.ShouldAnimate(MonitorTransferBucket.LongTerm, MonitorTransferBucket.AllSessions)
            ? TransferAnimation(_manual, _available, manualItem.ThreadId)
            : null;
        await ExecuteMutationAsync(items.Select(item => item.ThreadId), id => _monitorService.RemoveAsync(id, _lifetime.Token),
            $"已明确移除 {items.Length} 个监测任务。", "明确移除项目监测失败", animation);
    }

    private async Task ExecuteMutationAsync(
        IEnumerable<string> ids,
        Func<string, Task> operation,
        string success,
        string errorTitle,
        (Rectangle Source, Rectangle Target)? animation = null)
    {
        if (_loading) return;
        SetLoading(true, "正在通过 FeiShuBOT 应用变更…");
        try
        {
            foreach (var id in ids) await operation(id);
            if (animation is { } transfer)
                await TransferOverlayAnimator.PlayAsync(this, transfer.Source, transfer.Target, _lifetime.Token);
            _monitors = await _monitorService.ListAsync(_lifetime.Token);
            RebuildClassificationFilter();
            Render();
            _status.Text = success;
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
        catch (Exception error) { ShowError(errorTitle, error); }
        finally { if (!IsDisposed) SetLoading(false, _status.Text); }
    }

    private (Rectangle Source, Rectangle Target)? TransferAnimation(DataGridView source, DataGridView target, string id)
    {
        var sourceRow = source.Rows.Cast<DataGridViewRow>().FirstOrDefault(row => row.Tag switch
        {
            CodexThreadInfo thread => thread.Id.Equals(id, StringComparison.OrdinalIgnoreCase),
            ProjectMonitorItem monitor => monitor.ThreadId.Equals(id, StringComparison.OrdinalIgnoreCase),
            _ => false,
        });
        if (sourceRow is null || !sourceRow.Displayed) return null;
        var sourceRectangle = source.RectangleToScreen(source.GetRowDisplayRectangle(sourceRow.Index, cutOverflow: true));
        var targetClient = target.ClientRectangle;
        var targetRectangle = target.RectangleToScreen(new Rectangle(
            targetClient.Left + targetClient.Width / 4,
            targetClient.Top + Math.Min(targetClient.Height / 3, DpiLayout.Scale(90, DeviceDpi)),
            Math.Max(80, targetClient.Width / 2),
            DpiLayout.Scale(38, DeviceDpi)));
        return (sourceRectangle, targetRectangle);
    }

    private void ClearOtherSelections(DataGridView active)
    {
        if (_syncingSelection || active.SelectedRows.Count == 0) return;
        _syncingSelection = true;
        try
        {
            foreach (var grid in new[] { _available, _manual, _automatic })
                if (!ReferenceEquals(grid, active)) grid.ClearSelection();
        }
        finally { _syncingSelection = false; }
    }

    private void SetLoading(bool value, string text)
    {
        _loading = value;
        UseWaitCursor = value;
        _search.Enabled = !value;
        _searchModeSelector.Enabled = !value;
        _classification.Enabled = !value && _searchMode == SessionSearchMode.Exact;
        _archived.Enabled = _refresh.Enabled = _manualPaste.Enabled = !value;
        _add.Enabled = _remove.Enabled = !value;
        _promote.Enabled = !value && _monitors.Any(item => !item.IsManual);
        _status.Text = text;
    }

    private void ShowError(string title, Exception error)
    {
        _status.Text = error.Message;
        MessageBox.Show(this, error.Message, title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private void ShowHint(string message) =>
        MessageBox.Show(this, message, "项目监测管理", MessageBoxButtons.OK, MessageBoxIcon.Information);

    private void OpenThread(string id)
    {
        var result = _threadLauncher.TryOpen(id);
        _status.Text = result.Message;
    }

    private static string Truncate(string value, int length) => value.Length > length ? value[..(length - 1)] + "…" : value;

    private static IEnumerable<T> SelectedRecords<T>(DataGridView grid) where T : class =>
        grid.SelectedRows.Cast<DataGridViewRow>().OrderBy(row => row.Index).Select(row => row.Tag).OfType<T>();

    private sealed record DrawerGroup(string Key);
}
