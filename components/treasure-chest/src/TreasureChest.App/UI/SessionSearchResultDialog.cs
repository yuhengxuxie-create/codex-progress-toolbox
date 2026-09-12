using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class SessionSearchResultDialog : Form
{
    private readonly SessionSearchResult? _result;
    private readonly IReadOnlyList<SessionSearchMatch> _matches;
    private readonly DataGridView _grid = CreateGrid();
    private readonly RichTextBox _details = new() { Dock = DockStyle.Fill, ReadOnly = true, BorderStyle = BorderStyle.None, BackColor = UiTheme.Surface };
    private readonly Label _page = new() { AutoSize = true, Margin = new Padding(12, 9, 12, 0), ForeColor = UiTheme.Muted };
    private readonly Button _previous = UiTheme.Button("上一页");
    private readonly Button _next = UiTheme.Button("下一页");
    private int _pageIndex;

    public SessionSearchResultDialog(SessionSearchResult? result, string? failureMessage = null, bool cancelled = false)
    {
        UiTheme.ConfigureDpiAwareForm(this);
        _result = result;
        _matches = result is null ? [] : SessionSearchPresentation.OrderMatches(result.Matches);
        Text = "会话搜索结果";
        Icon = IconService.LoadAppIcon();
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = new Size(900, 660);
        Size = new Size(1080, 780);
        Font = UiTheme.CreateFont();
        BackColor = UiTheme.Background;
        BuildLayout(failureMessage, cancelled);
        _grid.SelectionChanged += (_, _) => ShowSelectedDetails();
        _grid.CellDoubleClick += (_, e) =>
        {
            if (e.RowIndex < 0 || _grid.Rows[e.RowIndex].Tag is not SessionSearchMatch item) return;
            var opened = new CodexDesktopLauncher().TryOpen(item.ThreadId);
            MessageBox.Show(this, opened.Message, "会话搜索结果", MessageBoxButtons.OK,
                opened.Opened ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        };
        _previous.Click += (_, _) => { if (_pageIndex > 0) { _pageIndex--; PopulatePage(); } };
        _next.Click += (_, _) => { if (_pageIndex + 1 < SessionSearchPresentation.PageCount(_matches.Count)) { _pageIndex++; PopulatePage(); } };
        PopulatePage();
    }

    public bool ExpandRequested { get; private set; }

    private void BuildLayout(string? failureMessage, bool cancelled)
    {
        var headingText = cancelled ? "搜索已取消" : failureMessage is not null ? "搜索失败" : SessionSearchPresentation.ResultHeading(_result!.Status);
        var header = new Panel { Dock = DockStyle.Top, Height = 92, BackColor = UiTheme.Navigation, Padding = new Padding(24, 14, 24, 8) };
        header.Controls.Add(new Label
        {
            Text = headingText,
            Dock = DockStyle.Top,
            Height = 40,
            ForeColor = UiTheme.NavigationText,
            Font = UiTheme.CreateFont(17F, FontStyle.Bold),
        });
        header.Controls.Add(new Label
        {
            Text = ResultSummary(failureMessage, cancelled),
            Dock = DockStyle.Bottom,
            Height = 30,
            ForeColor = UiTheme.NavigationMuted,
        });

        var summary = new Label
        {
            Dock = DockStyle.Top,
            Height = 82,
            Padding = new Padding(24, 12, 24, 4),
            ForeColor = failureMessage is not null ? UiTheme.Stopped : UiTheme.Text,
            Text = failureMessage ?? (_result?.CostWarning ?? "搜索已由用户取消。"),
        };
        var warning = new Label
        {
            Dock = DockStyle.Top,
            Height = _result?.Warnings.Count > 0 ? 64 : 0,
            Visible = _result?.Warnings.Count > 0,
            Padding = new Padding(24, 8, 24, 4),
            ForeColor = UiTheme.Warning,
            Text = _result is null ? string.Empty : WarningText(_result.Warnings),
            TextAlign = ContentAlignment.MiddleLeft,
        };

        var body = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(24, 8, 24, 12),
            RowCount = 3,
            ColumnCount = 1,
        };
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 46));
        body.RowStyles.Add(new RowStyle(SizeType.Absolute, 44));
        body.RowStyles.Add(new RowStyle(SizeType.Percent, 54));
        body.Controls.Add(GridWellPanel.Wrap(_grid), 0, 0);
        var paging = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, WrapContents = false };
        paging.Controls.Add(_next);
        paging.Controls.Add(_page);
        paging.Controls.Add(_previous);
        body.Controls.Add(paging, 0, 1);
        body.Controls.Add(new ThemedInputHost(_details) { Dock = DockStyle.Fill }, 0, 2);

        var footer = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = 62,
            Padding = new Padding(0, 11, 24, 9),
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false,
            BackColor = UiTheme.Surface,
        };
        var close = UiTheme.Button(_result?.Status == "not_found" ? "取消搜索" : "关闭结果");
        close.AutoSize = false;
        close.Size = new Size(126, 36);
        close.Click += (_, _) => { DialogResult = DialogResult.Cancel; Close(); };
        footer.Controls.Add(close);
        if (_result?.CanExpand == true)
        {
            var expand = UiTheme.Button("扩大搜索范围", true);
            expand.AutoSize = false;
            expand.Size = new Size(132, 36);
            expand.Click += (_, _) => { ExpandRequested = true; DialogResult = DialogResult.OK; Close(); };
            footer.Controls.Add(expand);
        }

        Controls.Add(body);
        Controls.Add(warning);
        Controls.Add(summary);
        Controls.Add(footer);
        Controls.Add(header);
        CancelButton = close;
    }

    private string ResultSummary(string? failureMessage, bool cancelled)
    {
        if (cancelled) return "搜索临时文件已清理，没有改变任何监测状态。";
        if (failureMessage is not null) return "进度窗已关闭；以下是 FeiShuBOT 返回的真实错误。";
        return $"范围：{_result!.ScopeLabel}　读取 {_result.ExaminedCount} 个会话　深层候选 {_result.SemanticCandidateCount} 个　Luna 调用 {_result.ModelCallCount} 次";
    }

    private void PopulatePage()
    {
        _grid.Rows.Clear();
        var pageItems = SessionSearchPresentation.Page(_matches, _pageIndex);
        for (var pageOffset = 0; pageOffset < pageItems.Count; pageOffset++)
        {
            var item = pageItems[pageOffset];
            var rank = _pageIndex * SessionSearchPresentation.ResultsPerPage + pageOffset + 1;
            var index = _grid.Rows.Add(rank, item.Title, string.IsNullOrWhiteSpace(item.ProjectName) ? "个人对话" : item.ProjectName,
                item.LastActivityAtBeijing, item.Score.ToString("0.000"), item.Confidence);
            _grid.Rows[index].Tag = item;
            _grid.Rows[index].Cells[1].ToolTipText = item.Title;
        }
        var pages = SessionSearchPresentation.PageCount(_matches.Count);
        _page.Text = _matches.Count == 0 ? "无候选" : $"第 {_pageIndex + 1} / {pages} 页，共 {_matches.Count} 项";
        _previous.Enabled = _pageIndex > 0;
        _next.Enabled = _pageIndex + 1 < pages;
        _grid.Visible = _matches.Count > 0;
        _details.Visible = _matches.Count > 0;
        _page.Visible = _matches.Count > 0;
        _previous.Visible = _next.Visible = _matches.Count > 0;
        if (_grid.Rows.Count > 0) _grid.Rows[0].Selected = true;
        else _details.Text = _result?.Status == "not_found" ? "没有找到符合当前线索的会话。" : string.Empty;
    }

    private void ShowSelectedDetails()
    {
        if (_grid.SelectedRows.Cast<DataGridViewRow>().FirstOrDefault()?.Tag is not SessionSearchMatch item) return;
        _details.Text = $"会话名称：{item.Title}\n任务 ID：{item.ThreadId}\n项目 / 个人归属：{(string.IsNullOrWhiteSpace(item.ProjectName) ? "个人对话" : item.ProjectName)}\n最后活动：{item.LastActivityAtBeijing}\n匹配度：{item.Score:0.000}（{item.Confidence} / {item.Classification}）\n监测状态：{SessionSearchPresentation.MonitorText(item.Monitor)}\n\n匹配原因\n{item.Reason}\n\n会话描述\n{item.Description}\n\n最后结果\n{item.LastResult}";
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
            MultiSelect = false,
            SelectionMode = DataGridViewSelectionMode.FullRowSelect,
            RowHeadersVisible = false,
            ReadOnly = true,
            EnableHeadersVisualStyles = false,
            ColumnHeadersHeight = 38,
            RowTemplate = { Height = 40 },
            GridColor = UiTheme.GridLine,
            ColumnHeadersDefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = UiTheme.SurfaceAlt,
                ForeColor = UiTheme.Text,
                SelectionBackColor = UiTheme.SurfaceAlt,
                SelectionForeColor = UiTheme.Text,
                Font = UiTheme.CreateFont(style: FontStyle.Bold),
            },
            DefaultCellStyle = new DataGridViewCellStyle
            {
                BackColor = UiTheme.Surface,
                ForeColor = UiTheme.Text,
                SelectionBackColor = UiTheme.AccentSoftStrong,
                SelectionForeColor = UiTheme.Text,
                Padding = new Padding(5, 0, 5, 0),
            },
        };
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Rank", HeaderText = "排名", Width = 76 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Title", HeaderText = "会话名称", Width = 285, MinimumWidth = 140, Resizable = DataGridViewTriState.True });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Project", HeaderText = "项目 / 个人", Width = 165 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Activity", HeaderText = "最后活动", Width = 155 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Score", HeaderText = "分数", Width = 78 });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Confidence", HeaderText = "置信度", Width = 96 });
        return grid;
    }

    private static string WarningText(IReadOnlyList<SessionSearchWarning> warnings)
    {
        var codes = warnings.Select(item => item.Code).Distinct(StringComparer.OrdinalIgnoreCase).Take(5);
        return $"注意：搜索过程中发现 {warnings.Count} 条不完整记录（{string.Join("、", codes)}）。结果仍可查看，但部分摘要可能缺失。";
    }
}
