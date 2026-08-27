using TreasureChest.Core.Models;
using TreasureChest.Core.Services;

namespace TreasureChest.UI;

internal sealed class ToolDialog : Form
{
    private readonly TextBox _name = new();
    private readonly TextBox _target = new();
    private readonly TextBox _arguments = new();
    private readonly TextBox _working = new();
    private readonly TextBox _icon = new();
    private readonly TextBox _category = new();
    private readonly CheckBox _enabled = new() { Text = "启用此工具", Checked = true };
    private readonly HashSet<string> _otherNames;
    private readonly string _id;

    public ToolDialog(ToolDefinition? source, IEnumerable<string> otherNames)
    {
        _otherNames = new HashSet<string>(otherNames, StringComparer.OrdinalIgnoreCase);
        _id = source?.Id ?? Guid.NewGuid().ToString("N");
        Text = source is null ? "添加工具" : "编辑工具";
        StartPosition = FormStartPosition.CenterParent;
        Size = new Size(720, 490);
        MinimumSize = Size;
        Font = new Font("Microsoft YaHei UI", 9F);
        BackColor = UiTheme.Background;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        BuildLayout();
        if (source is not null) LoadSource(source);
    }

    public ToolDefinition Result { get; private set; } = new();

    private void BuildLayout()
    {
        var table = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(20), ColumnCount = 3, RowCount = 8 };
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 120));
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        table.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 86));
        AddRow(table, 0, "工具名称 *", _name);
        AddRow(table, 1, "目标路径 *", _target, BrowseTarget());
        AddRow(table, 2, "参数", _arguments);
        AddRow(table, 3, "工作目录", _working, BrowseFolder(_working));
        AddRow(table, 4, "自定义图标", _icon, BrowseIcon());
        AddRow(table, 5, "分类", _category);
        table.Controls.Add(_enabled, 1, 6);
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, AutoSize = true };
        var save = UiTheme.Button("保存", true);
        var cancel = UiTheme.Button("取消");
        save.Click += (_, _) => SaveAndClose();
        cancel.Click += (_, _) => { DialogResult = DialogResult.Cancel; Close(); };
        buttons.Controls.Add(save); buttons.Controls.Add(cancel);
        table.Controls.Add(buttons, 0, 7); table.SetColumnSpan(buttons, 3);
        Controls.Add(table); AcceptButton = save; CancelButton = cancel;
    }

    private static void AddRow(TableLayoutPanel table, int row, string label, Control control, Control? browse = null)
    {
        table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        table.Controls.Add(new Label { Text = label, AutoSize = true, Margin = new Padding(0, 10, 8, 8) }, 0, row);
        control.Dock = DockStyle.Top; control.Margin = new Padding(0, 5, 8, 8); table.Controls.Add(control, 1, row);
        if (browse is not null) table.Controls.Add(browse, 2, row);
    }

    private Button BrowseTarget()
    {
        var button = UiTheme.Button("浏览…");
        button.Click += (_, _) =>
        {
            using var dialog = new OpenFileDialog { Filter = "支持的工具|*.exe;*.lnk;*.bat;*.cmd;*.ps1|所有文件|*.*" };
            if (dialog.ShowDialog() == DialogResult.OK)
            { _target.Text = dialog.FileName; if (_working.TextLength == 0) _working.Text = Path.GetDirectoryName(dialog.FileName); }
        };
        return button;
    }

    private Button BrowseIcon()
    {
        var button = UiTheme.Button("浏览…");
        button.Click += (_, _) =>
        {
            using var dialog = new OpenFileDialog { Filter = "图标或图片|*.ico;*.png;*.exe|所有文件|*.*" };
            if (dialog.ShowDialog() == DialogResult.OK) _icon.Text = dialog.FileName;
        };
        return button;
    }

    private static Button BrowseFolder(TextBox target)
    {
        var button = UiTheme.Button("浏览…");
        button.Click += (_, _) =>
        {
            using var dialog = new FolderBrowserDialog { SelectedPath = target.Text };
            if (dialog.ShowDialog() == DialogResult.OK) target.Text = dialog.SelectedPath;
        };
        return button;
    }

    private void LoadSource(ToolDefinition source)
    {
        _name.Text = source.Name; _target.Text = source.TargetPath; _arguments.Text = source.Arguments;
        _working.Text = source.WorkingDirectory; _icon.Text = source.IconPath; _category.Text = source.Category;
        _enabled.Checked = source.Enabled;
    }

    private void SaveAndClose()
    {
        var name = _name.Text.Trim(); var target = ToolPath.NormalizeInput(_target.Text);
        if (name.Length == 0 || target.Length == 0)
        { MessageBox.Show(this, "工具名称和目标路径不能为空。", "请检查", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
        if (_otherNames.Contains(name))
        { MessageBox.Show(this, "工具名称必须唯一。", "请检查", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
        Result = new ToolDefinition
        {
            Id = _id, Name = name, TargetPath = target, Arguments = _arguments.Text.Trim(),
            WorkingDirectory = ToolPath.NormalizeWorkingDirectoryInput(_working.Text, target),
            IconPath = ToolPath.NormalizeInput(_icon.Text),
            Category = string.IsNullOrWhiteSpace(_category.Text) ? "常用工具" : _category.Text.Trim(), Enabled = _enabled.Checked,
        };
        DialogResult = DialogResult.OK; Close();
    }
}
