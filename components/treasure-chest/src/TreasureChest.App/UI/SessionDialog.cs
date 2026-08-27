using TreasureChest.Core.Models;

namespace TreasureChest.UI;

internal sealed class SessionDialog : Form
{
    private readonly TextBox _name = new();
    private readonly TextBox _description = new();
    private readonly TextBox _start = new();
    private readonly TextBox _stop = new();
    private readonly TextBox _status = new();
    private readonly TextBox _process = new();
    private readonly TextBox _working = new();
    private readonly TextBox _log = new();
    private readonly CheckBox _enabled = new() { Text = "启用此会话" };
    private readonly CheckBox _autoStart = new() { Text = "TreasureChest 启动后自动启动" };
    private readonly CheckBox _autoRestart = new() { Text = "意外退出时自动重启" };
    private readonly NumericUpDown _attempts = new() { Minimum = 0, Maximum = 20, Value = 3, Width = 80 };
    private readonly HashSet<string> _otherNames;
    private readonly string _id;

    public SessionDialog(SessionDefinition? source, IEnumerable<string> otherNames)
    {
        _otherNames = new HashSet<string>(otherNames, StringComparer.OrdinalIgnoreCase);
        _id = source?.Id ?? Guid.NewGuid().ToString("N");
        Text = source is null ? "添加后台服务" : "编辑后台服务";
        StartPosition = FormStartPosition.CenterParent;
        MinimumSize = new Size(760, 650);
        Size = new Size(820, 720);
        Font = new Font("Microsoft YaHei UI", 9F);
        BackColor = UiTheme.Background;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        BuildLayout();
        if (source is not null) LoadSource(source);
    }

    public SessionDefinition Result { get; private set; } = new();

    private void BuildLayout()
    {
        var table = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(20),
            ColumnCount = 3,
            RowCount = 13,
            AutoScroll = true,
        };
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 145));
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 86));
        AddRow(table, 0, "会话名称 *", _name);
        AddRow(table, 1, "说明", _description);
        AddRow(table, 2, "启动命令 *", _start);
        AddRow(table, 3, "停止命令", _stop);
        AddRow(table, 4, "状态命令", _status);
        AddRow(table, 5, "进程名称", _process);
        AddRow(table, 6, "工作目录", _working, BrowseFolder(_working));
        AddRow(table, 7, "日志文件", _log, BrowseLog(_log));
        table.Controls.Add(_enabled, 1, 8);
        table.Controls.Add(_autoStart, 1, 9);
        table.Controls.Add(_autoRestart, 1, 10);
        var attempts = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
        attempts.Controls.Add(new Label { Text = "最大自动重启次数", AutoSize = true, Margin = new Padding(0, 8, 10, 0) });
        attempts.Controls.Add(_attempts);
        table.Controls.Add(attempts, 1, 11);
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, AutoSize = true };
        var save = UiTheme.Button("保存", true);
        var cancel = UiTheme.Button("取消");
        save.Click += (_, _) => SaveAndClose();
        cancel.Click += (_, _) => { DialogResult = DialogResult.Cancel; Close(); };
        buttons.Controls.Add(save);
        buttons.Controls.Add(cancel);
        table.Controls.Add(buttons, 0, 12);
        table.SetColumnSpan(buttons, 3);
        Controls.Add(table);
        AcceptButton = save;
        CancelButton = cancel;
    }

    private static void AddRow(TableLayoutPanel table, int row, string label, Control control, Control? browse = null)
    {
        table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        table.Controls.Add(new Label { Text = label, AutoSize = true, Margin = new Padding(0, 10, 8, 8) }, 0, row);
        control.Dock = DockStyle.Top;
        control.Margin = new Padding(0, 5, 8, 8);
        table.Controls.Add(control, 1, row);
        if (browse is not null) table.Controls.Add(browse, 2, row);
    }

    private static Button BrowseFolder(TextBox target)
    {
        var button = UiTheme.Button("浏览…");
        button.Click += (_, _) =>
        {
            using var dialog = new FolderBrowserDialog { SelectedPath = target.Text, UseDescriptionForTitle = true, Description = "选择工作目录" };
            if (dialog.ShowDialog() == DialogResult.OK) target.Text = dialog.SelectedPath;
        };
        return button;
    }

    private static Button BrowseLog(TextBox target)
    {
        var button = UiTheme.Button("浏览…");
        button.Click += (_, _) =>
        {
            using var dialog = new SaveFileDialog { Filter = "日志文件 (*.log)|*.log|文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*", FileName = target.Text };
            if (dialog.ShowDialog() == DialogResult.OK) target.Text = dialog.FileName;
        };
        return button;
    }

    private void LoadSource(SessionDefinition source)
    {
        _name.Text = source.Name; _description.Text = source.Description; _start.Text = source.StartCommand;
        _stop.Text = source.StopCommand; _status.Text = source.StatusCommand; _process.Text = source.ProcessName;
        _working.Text = source.WorkingDirectory; _log.Text = source.LogFilePath; _enabled.Checked = source.Enabled;
        _autoStart.Checked = source.AutoStart; _autoRestart.Checked = source.AutoRestart;
        _attempts.Value = Math.Clamp(source.MaxRestartAttempts, 0, 20);
    }

    private void SaveAndClose()
    {
        var name = _name.Text.Trim();
        if (name.Length == 0 || _start.Text.Trim().Length == 0)
        { MessageBox.Show(this, "会话名称和启动命令不能为空。", "请检查", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
        if (_otherNames.Contains(name))
        { MessageBox.Show(this, "会话名称必须唯一。", "请检查", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
        Result = new SessionDefinition
        {
            Id = _id, Name = name, Description = _description.Text.Trim(), Enabled = _enabled.Checked,
            StartCommand = _start.Text.Trim(), StopCommand = _stop.Text.Trim(), StatusCommand = _status.Text.Trim(),
            ProcessName = _process.Text.Trim(), WorkingDirectory = _working.Text.Trim(), LogFilePath = _log.Text.Trim(),
            AutoStart = _autoStart.Checked, AutoRestart = _autoRestart.Checked, MaxRestartAttempts = (int)_attempts.Value,
        };
        DialogResult = DialogResult.OK;
        Close();
    }
}
