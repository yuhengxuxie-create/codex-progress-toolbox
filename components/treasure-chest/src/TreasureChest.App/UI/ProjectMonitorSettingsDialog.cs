using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class ProjectMonitorSettingsDialog : Form
{
    private readonly ProjectMonitorCliService _monitorService;
    private readonly CheckBox _automatic = new()
    {
        Text = "启用自动监测",
        AutoSize = true,
        Font = new Font("Microsoft YaHei UI", 11F, FontStyle.Bold),
        ForeColor = UiTheme.Text,
        Margin = new Padding(0, 8, 0, 10),
    };
    private readonly Label _current = new()
    {
        AutoSize = false,
        Dock = DockStyle.Top,
        Height = 34,
        Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold),
        ForeColor = UiTheme.Muted,
    };
    private readonly Label _description = new()
    {
        AutoSize = false,
        Dock = DockStyle.Top,
        Height = 72,
        ForeColor = UiTheme.Muted,
    };
    private readonly Label _effective = new()
    {
        AutoSize = false,
        Dock = DockStyle.Top,
        Height = 28,
        ForeColor = UiTheme.Muted,
    };
    private readonly Label _status = new()
    {
        AutoSize = false,
        Dock = DockStyle.Fill,
        ForeColor = UiTheme.Muted,
        TextAlign = ContentAlignment.MiddleLeft,
    };
    private readonly Button _save = UiTheme.Button("保存设置", true);
    private readonly Button _cancel = UiTheme.Button("取消");
    private ProjectMonitorSettings? _loaded;
    private bool _busy;

    public ProjectMonitorSettingsDialog(ProjectMonitorCliService monitorService)
    {
        _monitorService = monitorService;
        Text = "监测设置";
        Icon = IconService.LoadAppIcon();
        StartPosition = FormStartPosition.CenterParent;
        Size = new Size(680, 500);
        MinimumSize = new Size(620, 480);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        Font = new Font("Microsoft YaHei UI", 9F);
        BackColor = UiTheme.Background;
        BuildLayout();
        WireEvents();
        Shown += async (_, _) => await LoadSettingsAsync();
    }

    public ProjectMonitorSettings? SavedSettings { get; private set; }

    private void BuildLayout()
    {
        var header = new Panel
        {
            Dock = DockStyle.Top,
            Height = 96,
            BackColor = UiTheme.Navigation,
            Padding = new Padding(28, 14, 28, 8),
        };
        header.Controls.Add(new Label
        {
            Text = "监测设置",
            ForeColor = Color.White,
            Font = new Font("Microsoft YaHei UI", 17F, FontStyle.Bold),
            Dock = DockStyle.Top,
            Height = 40,
        });
        header.Controls.Add(new Label
        {
            Text = "控制 FeiShuBOT 是否自动发现并加入新的 Codex 会话。",
            ForeColor = Color.FromArgb(203, 213, 236),
            Dock = DockStyle.Bottom,
            Height = 28,
        });

        var footer = new TableLayoutPanel
        {
            Dock = DockStyle.Bottom,
            Height = 66,
            ColumnCount = 2,
            Padding = new Padding(24, 10, 24, 10),
            BackColor = UiTheme.Surface,
        };
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        footer.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false,
        };
        buttons.Controls.Add(_save);
        buttons.Controls.Add(_cancel);
        footer.Controls.Add(_status, 0, 0);
        footer.Controls.Add(buttons, 1, 0);

        var card = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            BackColor = UiTheme.Surface,
            Padding = new Padding(24, 18, 24, 14),
            ColumnCount = 1,
            RowCount = 5,
        };
        card.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        card.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
        card.RowStyles.Add(new RowStyle(SizeType.Absolute, 44));
        card.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        card.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
        card.RowStyles.Add(new RowStyle(SizeType.Absolute, 58));
        var note = new Label
        {
            Text = "关闭自动监测不会删除已有自动项：它们继续正常通知，并在原有效期结束后自然退出。手动长期监测始终不受影响。",
            AutoSize = false,
            Dock = DockStyle.Fill,
            Padding = new Padding(12, 8, 12, 8),
            BackColor = Color.FromArgb(237, 241, 250),
            ForeColor = UiTheme.Text,
        };
        _current.Dock = DockStyle.Fill;
        _automatic.Dock = DockStyle.Fill;
        _description.Dock = DockStyle.Fill;
        _effective.Dock = DockStyle.Fill;
        card.Controls.Add(_current, 0, 0);
        card.Controls.Add(_automatic, 0, 1);
        card.Controls.Add(_description, 0, 2);
        card.Controls.Add(_effective, 0, 3);
        card.Controls.Add(note, 0, 4);

        var content = new Panel { Dock = DockStyle.Fill, Padding = new Padding(24, 20, 24, 18) };
        content.Controls.Add(card);
        Controls.Add(content);
        Controls.Add(footer);
        Controls.Add(header);
        AcceptButton = _save;
        CancelButton = _cancel;
    }

    private void WireEvents()
    {
        _automatic.CheckedChanged += (_, _) => UpdateDescription();
        _save.Click += async (_, _) => await SaveAsync();
        _cancel.Click += (_, _) => { DialogResult = DialogResult.Cancel; Close(); };
    }

    private async Task LoadSettingsAsync()
    {
        SetBusy(true, "正在读取 FeiShuBOT 设置…");
        try
        {
            _loaded = await _monitorService.GetSettingsAsync();
            _automatic.Checked = _loaded.AutoMonitoringEnabled;
            _current.Text = _loaded.AutoMonitoringEnabled ? "当前状态：自动监测已开启" : "当前状态：自动监测已关闭";
            _current.ForeColor = _loaded.AutoMonitoringEnabled ? UiTheme.Running : UiTheme.Stopped;
            _effective.Text = _loaded.EffectiveAt.HasValue
                ? $"最近一次实际变化：{_loaded.EffectiveAt.Value.ToLocalTime():yyyy-MM-dd HH:mm:ss}"
                : "最近一次实际变化：尚无记录（默认开启）";
            UpdateDescription();
            SetBusy(false, "设置已就绪。");
        }
        catch (Exception error)
        {
            SetBusy(false, "读取设置失败。");
            MessageBox.Show(this, error.Message, "无法读取监测设置", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private async Task SaveAsync()
    {
        if (_busy || _loaded is null) return;
        if (_automatic.Checked == _loaded.AutoMonitoringEnabled)
        {
            SavedSettings = _loaded with { Changed = false };
            DialogResult = DialogResult.OK;
            Close();
            return;
        }

        SetBusy(true, _automatic.Checked ? "正在开启自动监测…" : "正在关闭自动监测…");
        try
        {
            SavedSettings = await _monitorService.SetAutoMonitoringAsync(_automatic.Checked);
            DialogResult = DialogResult.OK;
            Close();
        }
        catch (Exception error)
        {
            SetBusy(false, "保存设置失败。");
            MessageBox.Show(this, error.Message, "无法保存监测设置", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private void UpdateDescription()
    {
        _description.Text = _automatic.Checked
            ? "开启后，FeiShuBOT 会继续自动发现新的项目会话和个人对话，并按 24 小时规则管理自动项。"
            : "关闭后，FeiShuBOT 不再新增自动项，也不会延长现有自动项的有效时间。";
    }

    private void SetBusy(bool busy, string text)
    {
        _busy = busy;
        UseWaitCursor = busy;
        _automatic.Enabled = !busy && _loaded is not null;
        _save.Enabled = !busy && _loaded is not null;
        _cancel.Enabled = !busy;
        _status.Text = text;
    }
}
