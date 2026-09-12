using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class SessionSearchWorkflowDialog : Form
{
    private static readonly Size LogicalWindowSize = new(820, 600);
    private readonly ProjectMonitorCliService _service;
    private readonly CancellationToken _ownerToken;
    private readonly bool _autoStart;
    private readonly SessionSearchCacheMode _cacheMode;
    private readonly TextBox _name = new ThemedEmbeddedTextBox { Dock = DockStyle.Fill, MaxLength = 1000 };
    private readonly TextBox _description = new ThemedEmbeddedTextBox { Dock = DockStyle.Fill, MaxLength = 1000, Multiline = true, ScrollBars = ScrollBars.Vertical };
    private readonly TextBox _lastActivity = new ThemedEmbeddedTextBox { Dock = DockStyle.Fill, MaxLength = 1000 };
    private readonly Panel _criteriaPanel = new() { Dock = DockStyle.Fill };
    private readonly Panel _progressPanel = new() { Dock = DockStyle.Fill, Visible = false };
    private readonly Label _phase = new() { AutoSize = false, Dock = DockStyle.Top, Height = 34, Font = UiTheme.CreateFont(11F, FontStyle.Bold) };
    private readonly Label _message = new() { AutoSize = false, Dock = DockStyle.Top, Height = 56, ForeColor = UiTheme.Muted };
    private readonly Label _counter = new() { AutoSize = false, Dock = DockStyle.Top, Height = 28, ForeColor = UiTheme.Muted };
    private readonly ThemedProgressBar _progress = new() { Dock = DockStyle.Top, Height = 18 };
    private readonly Button _submit = UiTheme.Button("开始搜索", true);
    private readonly Button _cancel = UiTheme.Button("取消");
    private CancellationTokenSource? _runCancellation;
    private bool _running;
    private SessionSearchRequest _request;
    private readonly DpiLayoutStore _dpiLayout = new();
    private bool _dpiLayoutReady;
    private bool _initialDpiApplied;
    private bool _applyingDpiLayout;
    private int _layoutDpi = DpiLayout.BaselineDpi;

    public SessionSearchWorkflowDialog(
        ProjectMonitorCliService service,
        SessionSearchRequest request,
        bool autoStart,
        CancellationToken ownerToken,
        SessionSearchCacheMode cacheMode = SessionSearchCacheMode.Persistent)
    {
        UiTheme.ConfigureDpiAwareForm(this, manuallyManaged: true);
        _service = service;
        _request = request;
        _autoStart = autoStart;
        _ownerToken = ownerToken;
        _cacheMode = cacheMode;
        var qaSuffix = cacheMode == SessionSearchCacheMode.Ephemeral ? "（隔离 QA）" : string.Empty;
        Text = (autoStart ? "扩大搜索范围" : "模糊搜索会话") + qaSuffix;
        Icon = IconService.LoadAppIcon();
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = false;
        MinimumSize = LogicalWindowSize;
        Size = LogicalWindowSize;
        Font = UiTheme.CreateFont();
        BackColor = UiTheme.Background;
        BuildLayout();
        _dpiLayout.CaptureBaseTree(this);
        _dpiLayoutReady = true;
        _name.Text = request.Name;
        _description.Text = request.Description;
        _lastActivity.Text = request.LastActivity;
        _submit.Click += async (_, _) => await StartSearchAsync();
        _cancel.Click += (_, _) => CancelOrClose();
        Shown += async (_, _) =>
        {
            if (_autoStart) await StartSearchAsync();
            else _name.Focus();
        };
    }

    public SessionSearchResult? SearchResult { get; private set; }
    public SessionSearchRequest SearchRequest => _request;
    public string? FailureMessage { get; private set; }
    public bool WasCancelled { get; private set; }
    internal int LayoutDpiForTests => _layoutDpi;

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        if (_dpiLayoutReady && !_initialDpiApplied)
            ApplyDpiLayout((int)NativeMethods.GetDpiForWindow(Handle), initial: true);
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        ApplyDpiLayout(e.DeviceDpiNew, initial: false);
        if (e.SuggestedRectangle.Location != Point.Empty) Location = e.SuggestedRectangle.Location;
    }

    protected override void OnShown(EventArgs e)
    {
        // ShowDialog(owner) assigns the real owner only after construction and
        // handle setup. On a 150% monitor, relying solely on OnHandleCreated can
        // therefore freeze this dialog at the 96-DPI 820x600 baseline while its
        // child controls are later scaled/clipped. Resolve the owner generation
        // here, commit one complete layout, and center the resulting bounds before
        // the public Shown event focuses the editor.
        ApplyOwnerDpiAtShow();
        base.OnShown(e);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (_running)
        {
            e.Cancel = true;
            RequestCancel();
            return;
        }
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) _runCancellation?.Dispose();
        base.Dispose(disposing);
    }

    private void BuildLayout()
    {
        var header = new Panel { Dock = DockStyle.Top, Height = 106, BackColor = UiTheme.Navigation, Padding = new Padding(24, 12, 24, 8) };
        header.Controls.Add(new Label
        {
            Text = "模糊搜索 Codex 会话",
            Dock = DockStyle.Top,
            Height = 38,
            ForeColor = UiTheme.NavigationText,
            Font = UiTheme.CreateFont(16F, FontStyle.Bold),
        });
        header.Controls.Add(new Label
        {
            Text = "名称、描述、最后活动时间可任意留空或记错；\n所有非空内容会由 FeiShuBOT 共同理解。",
            Dock = DockStyle.Bottom,
            Height = 42,
            ForeColor = UiTheme.NavigationMuted,
            TextAlign = ContentAlignment.MiddleLeft,
        });

        var criteria = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(28, 22, 28, 12),
            ColumnCount = 2,
            RowCount = 5,
            BackColor = UiTheme.Background,
        };
        criteria.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 150));
        criteria.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        criteria.RowStyles.Add(new RowStyle(SizeType.Absolute, 46));
        criteria.RowStyles.Add(new RowStyle(SizeType.Absolute, 142));
        criteria.RowStyles.Add(new RowStyle(SizeType.Absolute, 46));
        criteria.RowStyles.Add(new RowStyle(SizeType.Absolute, 60));
        criteria.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        AddField(criteria, 0, "会话名称：", _name);
        AddField(criteria, 1, "会话描述：", _description);
        AddField(criteria, 2, "最后活动时间：", _lastActivity);
        criteria.Controls.Add(new Label
        {
            Text = "示例：这几天、上个月、2026 年 8 月；\n无法解析时仍会作为普通语义线索。",
            AutoSize = false,
            Dock = DockStyle.Fill,
            ForeColor = UiTheme.Muted,
            Margin = new Padding(3, 8, 3, 3),
            TextAlign = ContentAlignment.TopLeft,
        }, 1, 3);
        _criteriaPanel.Controls.Add(criteria);

        var progressHost = new Panel { Dock = DockStyle.Fill, Padding = new Padding(44, 54, 44, 24), BackColor = UiTheme.Background };
        _phase.Text = "正在启动会话搜索…";
        _message.Text = "正在等待 FeiShuBOT 写入真实进度。";
        _counter.Text = "当前阶段：0 / 0";
        progressHost.Controls.Add(_counter);
        progressHost.Controls.Add(_progress);
        progressHost.Controls.Add(_message);
        progressHost.Controls.Add(_phase);
        _progressPanel.Controls.Add(progressHost);

        var footer = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = 62,
            Padding = new Padding(0, 11, 24, 9),
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false,
            BackColor = UiTheme.Surface,
        };
        _submit.Size = new Size(104, 36);
        _submit.AutoSize = false;
        _cancel.Size = new Size(104, 36);
        _cancel.AutoSize = false;
        footer.Controls.Add(_submit);
        footer.Controls.Add(_cancel);

        var body = new Panel { Dock = DockStyle.Fill };
        body.Controls.Add(_progressPanel);
        body.Controls.Add(_criteriaPanel);
        Controls.Add(body);
        Controls.Add(footer);
        Controls.Add(header);
        AcceptButton = _submit;
        CancelButton = _cancel;
    }

    private void ApplyDpiLayout(int dpi, bool initial)
    {
        if (!_dpiLayoutReady || _applyingDpiLayout) return;
        dpi = DpiLayout.NormalizeDpi(dpi);
        _applyingDpiLayout = true;
        SuspendLayout();
        try
        {
            _dpiLayout.ApplyTree(this, dpi);
            _layoutDpi = dpi;
            _initialDpiApplied = true;
            if (initial)
            {
                var preferred = DpiLayout.Scale(LogicalWindowSize, dpi);
                var workingArea = Screen.FromHandle(Handle).WorkingArea;
                var constrained = DpiLayout.ConstrainToWorkingArea(Location, preferred, workingArea, dpi);
                Size = constrained.Size;
            }
        }
        finally
        {
            ResumeLayout(performLayout: true);
            _applyingDpiLayout = false;
        }
    }

    private void ApplyOwnerDpiAtShow()
    {
        var owner = Owner;
        var dpi = owner is { IsHandleCreated: true }
            ? (int)NativeMethods.GetDpiForWindow(owner.Handle)
            : (int)NativeMethods.GetDpiForWindow(Handle);
        dpi = DpiLayout.NormalizeDpi(dpi);
        ApplyDpiLayout(dpi, initial: false);

        var preferred = DpiLayout.Scale(LogicalWindowSize, dpi);
        var ownerBounds = owner?.Bounds ?? Bounds;
        var preferredLocation = new Point(
            ownerBounds.Left + (ownerBounds.Width - preferred.Width) / 2,
            ownerBounds.Top + (ownerBounds.Height - preferred.Height) / 2);
        var workingArea = owner is null ? Screen.FromHandle(Handle).WorkingArea : Screen.FromControl(owner).WorkingArea;
        Bounds = DpiLayout.ConstrainToWorkingArea(preferredLocation, preferred, workingArea, dpi);
        _initialDpiApplied = true;
    }

    private static void AddField(TableLayoutPanel table, int row, string label, Control control)
    {
        table.Controls.Add(new Label
        {
            Text = label,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.TopRight,
            Padding = new Padding(0, 7, 10, 0),
            Font = UiTheme.CreateFont(style: FontStyle.Bold),
        }, 0, row);
        var input = new ThemedInputHost((TextBoxBase)control)
        {
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 3, 0, 7),
        };
        table.Controls.Add(input, 1, row);
    }

    private async Task StartSearchAsync()
    {
        if (_running) return;
        if (!_autoStart)
            _request = new SessionSearchRequest(_name.Text.Trim(), _description.Text.Trim(), _lastActivity.Text.Trim(), "auto");
        try { ProjectMonitorCliService.ValidateSearchRequest(_request); }
        catch (Exception error)
        {
            MessageBox.Show(this, error.Message, "搜索条件不正确", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        _running = true;
        _criteriaPanel.Visible = false;
        _progressPanel.Visible = true;
        _progressPanel.BringToFront();
        _submit.Enabled = false;
        _cancel.Text = "取消搜索";
        _cancel.Enabled = true;
        _runCancellation = CancellationTokenSource.CreateLinkedTokenSource(_ownerToken);
        var reporter = new Progress<SessionSearchProgress>(UpdateProgress);
        try
        {
            SearchResult = await _service.SearchSessionsAsync(
                _request,
                reporter,
                _runCancellation.Token,
                _cacheMode);
        }
        catch (OperationCanceledException)
        {
            WasCancelled = true;
        }
        catch (Exception error)
        {
            FailureMessage = error.Message;
        }
        finally
        {
            _running = false;
            DialogResult = DialogResult.OK;
            Close();
        }
    }

    private void UpdateProgress(SessionSearchProgress value)
    {
        if (IsDisposed) return;
        _phase.Text = value.Phase switch
        {
            "starting" => "正在启动",
            "collecting" => "正在收集会话",
            "reading" => "正在读取会话",
            "narrowing" => "正在缩小候选范围",
            "scoring" => "正在判断候选",
            "completed" => "搜索完成",
            "cancelled" => "搜索已取消",
            "failed" => "搜索失败",
            _ => value.Phase,
        };
        _message.Text = value.Message;
        _counter.Text = $"当前阶段：{value.Current} / {value.Total}";
        if (value.Total <= 0)
        {
            _progress.Style = ProgressBarStyle.Marquee;
            return;
        }
        _progress.Style = ProgressBarStyle.Continuous;
        _progress.Minimum = 0;
        _progress.Maximum = Math.Max(1, value.Total);
        _progress.Value = Math.Clamp(value.Current, 0, _progress.Maximum);
    }

    private void CancelOrClose()
    {
        if (!_running) { DialogResult = DialogResult.Cancel; Close(); return; }
        RequestCancel();
    }

    private void RequestCancel()
    {
        if (_runCancellation?.IsCancellationRequested == true) return;
        _runCancellation?.Cancel();
        _cancel.Enabled = false;
        _phase.Text = "正在安全取消…";
        _message.Text = "FeiShuBOT 会在安全检查点停止，请稍候。";
    }
}
