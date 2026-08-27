using System.Diagnostics;
using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class ProjectMonitorManagerDialog : Form
{
    private readonly ProjectMonitorCliService _monitorService;
    private readonly CodexThreadCatalogService _catalogService;
    private readonly TextBox _search = new() { Width = 330, PlaceholderText = "搜索名称、项目或任务 ID" };
    private readonly ComboBox _classification = new() { Width = 190, DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly CheckBox _archived = new() { Text = "显示已归档", AutoSize = true, Margin = new Padding(10, 8, 0, 0) };
    private readonly DataGridView _available = CreateCatalogGrid();
    private readonly DataGridView _manual = CreateMonitorGrid();
    private readonly DataGridView _automatic = CreateMonitorGrid();
    private readonly Label _availableTitle = SectionTitle("可手动添加的会话");
    private readonly Label _manualTitle = SectionTitle("手动监测");
    private readonly Label _automaticTitle = SectionTitle("自动监测");
    private readonly Label _status = new() { AutoSize = false, Dock = DockStyle.Fill, ForeColor = UiTheme.Muted, TextAlign = ContentAlignment.MiddleLeft };
    private readonly Button _refresh = UiTheme.Button("刷新全部", true);
    private readonly Button _manualPaste = UiTheme.Button("手动粘贴 ID");
    private readonly Button _add = UiTheme.Button("加入", true);
    private readonly Button _remove = UiTheme.Button("移除");
    private readonly Button _promote = UiTheme.Button("移入长期监测", true);
    private readonly CancellationTokenSource _lifetime = new();
    private readonly HashSet<string> _expandedAvailableGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private readonly HashSet<string> _expandedManualGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private readonly HashSet<string> _expandedAutomaticGroups = new(StringComparer.CurrentCultureIgnoreCase);
    private IReadOnlyList<CodexThreadInfo> _catalog = [];
    private IReadOnlyList<ProjectMonitorItem> _monitors = [];
    private bool _loading;
    private bool _syncingSelection;

    public ProjectMonitorManagerDialog(ProjectMonitorCliService monitorService, CodexThreadCatalogService catalogService)
    {
        _monitorService = monitorService;
        _catalogService = catalogService;
        Text = "项目监测管理";
        Icon = IconService.LoadAppIcon();
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = new Size(1080, 680);
        Size = new Size(1420, 840);
        FormBorderStyle = FormBorderStyle.Sizable;
        MaximizeBox = true;
        Font = new Font("Microsoft YaHei UI", 9F);
        BackColor = UiTheme.Background;
        BuildLayout();
        WireEvents();
        Shown += async (_, _) => await LoadEverythingAsync();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _lifetime.Cancel();
        _lifetime.Dispose();
        base.OnFormClosed(e);
    }

    private void BuildLayout()
    {
        var header = new Panel { Dock = DockStyle.Top, Height = 96, BackColor = UiTheme.Navigation, Padding = new Padding(28, 12, 28, 8) };
        header.Controls.Add(new Label
        {
            Text = "项目监测管理",
            ForeColor = Color.White,
            Font = new Font("Microsoft YaHei UI", 17F, FontStyle.Bold),
            Dock = DockStyle.Top,
            Height = 40,
            TextAlign = ContentAlignment.MiddleLeft,
        });
        header.Controls.Add(new Label
        {
            Text = "手动与自动监测分开管理；单击项目抽屉即可展开或收起，项目会话排在个人对话之前。",
            ForeColor = Color.FromArgb(203, 213, 236),
            Dock = DockStyle.Bottom,
            Height = 28,
            TextAlign = ContentAlignment.MiddleLeft,
        });

        var toolbar = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            Height = 58,
            Padding = new Padding(24, 11, 24, 8),
            BackColor = UiTheme.Surface,
            WrapContents = false,
        };
        toolbar.Controls.Add(_search);
        toolbar.Controls.Add(_classification);
        toolbar.Controls.Add(_archived);
        toolbar.Controls.Add(_refresh);
        toolbar.Controls.Add(_manualPaste);

        var body = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(24, 18, 24, 12),
            ColumnCount = 3,
            RowCount = 1,
            BackColor = UiTheme.Background,
        };
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 44));
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 112));
        body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 56));
        body.Controls.Add(ThreadSection(_availableTitle, _available), 0, 0);
        body.Controls.Add(BuildTransferButtons(), 1, 0);
        body.Controls.Add(BuildMonitorSections(), 2, 0);

        var footer = new TableLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = 64,
            Padding = new Padding(24, 9, 24, 9),
            ColumnCount = 2,
            BackColor = UiTheme.Surface,
        };
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        footer.Controls.Add(_status, 0, 0);
        var close = UiTheme.Button("关闭");
        close.Width = 88;
        close.Click += (_, _) => Close();
        footer.Controls.Add(close, 1, 0);

        Controls.Add(body);
        Controls.Add(footer);
        Controls.Add(toolbar);
        Controls.Add(header);
        CancelButton = close;
    }

    private Control BuildTransferButtons()
    {
        var host = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 3,
            Margin = Padding.Empty,
            BackColor = UiTheme.Background,
        };
        host.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
        host.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        host.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
            Anchor = AnchorStyles.None,
        };
        foreach (var button in new[] { _add, _remove })
        {
            button.AutoSize = false;
            button.Size = new Size(96, 38);
            button.Margin = new Padding(3, 4, 3, 4);
            button.TextAlign = ContentAlignment.MiddleCenter;
        }
        buttons.Controls.Add(_add);
        buttons.Controls.Add(_remove);
        host.Controls.Add(buttons, 0, 1);
        return host;
    }

    private Control BuildMonitorSections()
    {
        var stack = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        stack.RowStyles.Add(new RowStyle(SizeType.Percent, 70));
        stack.RowStyles.Add(new RowStyle(SizeType.Percent, 30));
        stack.Controls.Add(ThreadSection(_manualTitle, _manual), 0, 0);

        var automaticSection = new Panel { Dock = DockStyle.Fill, BackColor = UiTheme.Surface, Padding = new Padding(1), Margin = new Padding(0, 10, 0, 0) };
        var titlePanel = new Panel { Dock = DockStyle.Top, Height = 46, BackColor = Color.FromArgb(237, 241, 250), Padding = new Padding(14, 0, 8, 0) };
        _automaticTitle.Dock = DockStyle.Fill;
        var promoteHost = new FlowLayoutPanel
        {
            Dock = DockStyle.Right,
            Width = 154,
            Height = 44,
            Padding = new Padding(8, 6, 8, 4),
            WrapContents = false,
            FlowDirection = FlowDirection.LeftToRight,
            BackColor = Color.Transparent,
        };
        _promote.AutoSize = false;
        _promote.Size = new Size(138, 34);
        _promote.Margin = Padding.Empty;
        _promote.TextAlign = ContentAlignment.MiddleCenter;
        promoteHost.Controls.Add(_promote);
        titlePanel.Controls.Add(_automaticTitle);
        titlePanel.Controls.Add(promoteHost);
        automaticSection.Controls.Add(_automatic);
        automaticSection.Controls.Add(titlePanel);
        stack.Controls.Add(automaticSection, 0, 1);
        return stack;
    }

    private static Control ThreadSection(Label title, DataGridView grid)
    {
        var panel = new Panel { Dock = DockStyle.Fill, BackColor = UiTheme.Surface, Padding = new Padding(1) };
        var titlePanel = new Panel { Dock = DockStyle.Top, Height = 46, BackColor = Color.FromArgb(237, 241, 250), Padding = new Padding(14, 0, 0, 0) };
        title.Dock = DockStyle.Fill;
        titlePanel.Controls.Add(title);
        panel.Controls.Add(grid);
        panel.Controls.Add(titlePanel);
        return panel;
    }

    private static Label SectionTitle(string text) => new()
    {
        Text = text,
        ForeColor = UiTheme.Text,
        Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold),
        TextAlign = ContentAlignment.MiddleLeft,
    };

    private static DataGridView CreateBaseGrid() => new()
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
        ColumnHeadersHeight = 38,
        RowTemplate = { Height = 39 },
        GridColor = Color.FromArgb(231, 234, 241),
        ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle
        {
            BackColor = Color.FromArgb(246, 248, 252),
            ForeColor = UiTheme.Text,
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold),
        },
        DefaultCellStyle = new DataGridViewCellStyle
        {
            BackColor = UiTheme.Surface,
            ForeColor = UiTheme.Text,
            SelectionBackColor = Color.FromArgb(220, 229, 255),
            SelectionForeColor = UiTheme.Text,
            Padding = new Padding(5, 0, 5, 0),
        },
    };

    private static DataGridView CreateCatalogGrid()
    {
        var grid = CreateBaseGrid();
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Title", HeaderText = "会话名称", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill, MinimumWidth = 180 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "LastActivity", HeaderText = "最后活动", Width = 132 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Id", HeaderText = "完整任务 ID", Width = 285 });
        return grid;
    }

    private static DataGridView CreateMonitorGrid()
    {
        var grid = CreateBaseGrid();
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Title", HeaderText = "会话名称", AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill, MinimumWidth = 180 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "LastActivity", HeaderText = "最后活动", Width = 132 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Remaining", HeaderText = "有效时间", Width = 110 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Id", HeaderText = "完整任务 ID", Width = 285 });
        return grid;
    }

    private void WireEvents()
    {
        _search.TextChanged += (_, _) => Render();
        _classification.SelectedIndexChanged += (_, _) => Render();
        _archived.CheckedChanged += (_, _) => Render();
        _refresh.Click += async (_, _) => await LoadEverythingAsync();
        _manualPaste.Click += async (_, _) => await ManualPasteAsync();
        _add.Click += async (_, _) => await AddSelectedAsync();
        _remove.Click += async (_, _) => await RemoveSelectedAsync();
        _promote.Click += async (_, _) => await PromoteSelectedAsync();
        _available.CellDoubleClick += async (_, e) => { if (e.RowIndex >= 0 && _available.Rows[e.RowIndex].Tag is CodexThreadInfo) await AddSelectedAsync(); };
        _manual.CellDoubleClick += (_, e) => { if (e.RowIndex >= 0 && _manual.Rows[e.RowIndex].Tag is ProjectMonitorItem item) OpenThread(item.ThreadId); };
        _automatic.CellDoubleClick += (_, e) => { if (e.RowIndex >= 0 && _automatic.Rows[e.RowIndex].Tag is ProjectMonitorItem item) OpenThread(item.ThreadId); };
        foreach (var grid in new[] { _available, _manual, _automatic })
        {
            grid.CellMouseClick += (_, e) =>
            {
                if (e.Button == MouseButtons.Left && e.Clicks == 1) ToggleDrawer(grid, e.RowIndex);
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
            _status.Text = $"已加载 {_catalog.Count} 个 Codex 会话；手动监测 {_monitors.Count(item => item.IsManual)} 个，自动监测 {_monitors.Count(item => !item.IsManual)} 个。";
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
        if (_classification.Items.Count == 0) return;
        var monitoredIds = _monitors.Select(item => item.ThreadId).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var available = ProjectMonitorPresentation.OrderCatalog(_catalog
            .Where(item => !monitoredIds.Contains(item.Id))
            .Where(item => _archived.Checked || !item.Archived)
            .Where(MatchesFilter));
        var manual = ProjectMonitorPresentation.OrderWithinOrigin(_monitors.Where(item => item.IsManual && MatchesFilter(item)));
        var automatic = ProjectMonitorPresentation.OrderWithinOrigin(_monitors.Where(item => !item.IsManual && MatchesFilter(item)));

        FillCatalogGrid(_available, available, _expandedAvailableGroups);
        FillMonitorGrid(_manual, manual, _expandedManualGroups);
        FillMonitorGrid(_automatic, automatic, _expandedAutomaticGroups);
        _availableTitle.Text = $"可手动添加的会话（{available.Count}）";
        _manualTitle.Text = $"手动监测（{manual.Count}，长期有效）";
        _automaticTitle.Text = $"自动监测（{automatic.Count}，24 小时规则）";
        _promote.Enabled = !_loading && automatic.Count > 0;
    }

    private bool MatchesFilter(CodexThreadInfo item) =>
        MatchesClassification(item.Classification) && MatchesSearch($"{item.DisplayTitle}\n{item.Classification}\n{item.Cwd}\n{item.Id}");

    private bool MatchesFilter(ProjectMonitorItem item) =>
        MatchesClassification(item.Classification) && MatchesSearch($"{item.Title}\n{item.Classification}\n{item.ThreadId}");

    private bool MatchesClassification(string value)
    {
        var selected = _classification.SelectedItem?.ToString() ?? "全部项目与个人对话";
        return selected == "全部项目与个人对话" ||
            ProjectMonitorPresentation.NormalizeClassification(value).Equals(selected, StringComparison.CurrentCultureIgnoreCase);
    }

    private bool MatchesSearch(string value)
    {
        var query = _search.Text.Trim();
        return query.Length == 0 || value.Contains(query, StringComparison.CurrentCultureIgnoreCase);
    }

    private static void FillCatalogGrid(DataGridView grid, IReadOnlyList<CodexThreadInfo> records, HashSet<string> expandedGroups)
    {
        grid.Rows.Clear();
        foreach (var group in records.GroupBy(item => item.Classification, StringComparer.CurrentCultureIgnoreCase))
        {
            var key = ProjectMonitorPresentation.NormalizeClassification(group.Key);
            var expanded = expandedGroups.Contains(key);
            AddGroupRow(grid, key, group.Count(), expanded);
            if (!expanded) continue;
            foreach (var item in group)
            {
                var index = grid.Rows.Add(Truncate(item.DisplayTitle, 100), ProjectMonitorPresentation.FormatCatalogTimestamp(item.UpdatedAtMs), item.Id);
                var row = grid.Rows[index];
                row.Tag = item;
                row.Cells[0].ToolTipText = item.DisplayTitle;
                row.Cells[2].ToolTipText = item.Id;
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
            var key = ProjectMonitorPresentation.NormalizeClassification(group.Key);
            var expanded = expandedGroups.Contains(key);
            AddGroupRow(grid, key, group.Count(), expanded);
            if (!expanded) continue;
            foreach (var item in group)
            {
                var index = grid.Rows.Add(
                    Truncate(item.Title, 100),
                    ProjectMonitorPresentation.FormatTimestamp(item.LastActivityAt),
                    ProjectMonitorPresentation.FormatRemaining(item),
                    item.ThreadId);
                var row = grid.Rows[index];
                row.Tag = item;
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
        row.Height = 34;
        row.DefaultCellStyle.BackColor = Color.FromArgb(232, 237, 249);
        row.DefaultCellStyle.SelectionBackColor = Color.FromArgb(232, 237, 249);
        row.DefaultCellStyle.ForeColor = UiTheme.Navigation;
        row.DefaultCellStyle.Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold);
        row.Cells[0].ToolTipText = expanded ? "单击收起这个分组" : "单击展开这个分组";
    }

    private void ToggleDrawer(DataGridView grid, int rowIndex)
    {
        if (rowIndex < 0 || grid.Rows[rowIndex].Tag is not DrawerGroup drawer) return;
        var scrollPositions = CaptureScrollPositions();
        var expandedGroups = ExpandedGroupsFor(grid);
        if (!expandedGroups.Add(drawer.Key)) expandedGroups.Remove(drawer.Key);
        Render();
        var refreshedHeader = grid.Rows.Cast<DataGridViewRow>()
            .FirstOrDefault(row => row.Tag is DrawerGroup group && group.Key.Equals(drawer.Key, StringComparison.CurrentCultureIgnoreCase));
        if (refreshedHeader is null) return;
        grid.CurrentCell = refreshedHeader.Cells[0];
        RestoreScrollPositions(scrollPositions);
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
        var items = SelectedRecords<CodexThreadInfo>(_available).ToArray();
        if (items.Length == 0) { ShowHint("请先在左侧选择一个或多个会话。分组标题不能添加。"); return; }
        await ExecuteMutationAsync(items.Select(item => item.Id), id => _monitorService.AddAsync(id, _lifetime.Token),
            $"已将 {items.Length} 个会话加入手动监测。", "手动添加项目监测失败");
    }

    private async Task ManualPasteAsync()
    {
        using var dialog = new ThreadIdDialog();
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        await ExecuteMutationAsync(dialog.ThreadIds, id => _monitorService.AddAsync(id, _lifetime.Token),
            $"已手动添加 {dialog.ThreadIds.Count} 个任务。", "手动添加项目监测失败");
    }

    private async Task PromoteSelectedAsync()
    {
        var items = SelectedRecords<ProjectMonitorItem>(_automatic).ToArray();
        if (items.Length == 0) { ShowHint("请先在自动监测列表选择一个或多个会话。"); return; }
        await ExecuteMutationAsync(items.Select(item => item.ThreadId), id => _monitorService.AddAsync(id, _lifetime.Token),
            $"已将 {items.Length} 个自动项提升为长期有效的手动监测。", "提升为手动监测失败");
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
        await ExecuteMutationAsync(items.Select(item => item.ThreadId), id => _monitorService.RemoveAsync(id, _lifetime.Token),
            $"已明确移除 {items.Length} 个监测任务。", "明确移除项目监测失败");
    }

    private async Task ExecuteMutationAsync(IEnumerable<string> ids, Func<string, Task> operation, string success, string errorTitle)
    {
        if (_loading) return;
        SetLoading(true, "正在通过 FeiShuBOT 应用变更…");
        try
        {
            foreach (var id in ids) await operation(id);
            _monitors = await _monitorService.ListAsync(_lifetime.Token);
            RebuildClassificationFilter();
            Render();
            _status.Text = success;
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
        catch (Exception error) { ShowError(errorTitle, error); }
        finally { if (!IsDisposed) SetLoading(false, _status.Text); }
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
        _search.Enabled = _classification.Enabled = _archived.Enabled = _refresh.Enabled = _manualPaste.Enabled = !value;
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

    private static void OpenThread(string id)
    {
        try { Process.Start(new ProcessStartInfo("codex://threads/" + id) { UseShellExecute = true }); }
        catch (Exception error) { MessageBox.Show(error.Message, "无法在 Codex 打开任务", MessageBoxButtons.OK, MessageBoxIcon.Error); }
    }

    private static string Truncate(string value, int length) => value.Length > length ? value[..(length - 1)] + "…" : value;

    private static IEnumerable<T> SelectedRecords<T>(DataGridView grid) where T : class =>
        grid.SelectedRows.Cast<DataGridViewRow>().OrderBy(row => row.Index).Select(row => row.Tag).OfType<T>();

    private sealed record DrawerGroup(string Key);
}
