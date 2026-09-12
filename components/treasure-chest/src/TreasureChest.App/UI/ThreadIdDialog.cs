using TreasureChest.Integrations;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class ThreadIdDialog : Form
{
    private readonly TextBox _value = new ThemedEmbeddedTextBox { Width = 560, Height = 90, Multiline = true, ScrollBars = ScrollBars.Vertical };

    public ThreadIdDialog()
    {
        UiTheme.ConfigureDpiAwareForm(this);
        Text = "添加监听任务";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        Size = new Size(720, 310);
        Font = UiTheme.CreateFont();
        BackColor = UiTheme.Background;
        Icon = IconService.LoadAppIcon();

        var layout = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill, Padding = new Padding(24), FlowDirection = FlowDirection.TopDown, WrapContents = false,
        };
        layout.Controls.Add(new Label { Text = "手动粘贴 Codex 任务链接或任务 ID", AutoSize = true, Font = UiTheme.CreateFont(11F, FontStyle.Bold) });
        layout.Controls.Add(new Label { Text = "可输入多个，以换行或逗号分隔；添加后会设为手动监测，不受 24 小时自动移除规则影响。", AutoSize = true, ForeColor = UiTheme.Muted, Margin = new Padding(0, 5, 0, 10) });
        layout.Controls.Add(new ThemedInputHost(_value) { Width = 560, Height = 90, Margin = Padding.Empty });
        var buttons = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.LeftToRight, Margin = new Padding(0, 16, 0, 0) };
        var add = UiTheme.Button("添加", true);
        var cancel = UiTheme.Button("取消");
        add.Click += (_, _) => AcceptValue();
        cancel.Click += (_, _) => { DialogResult = DialogResult.Cancel; Close(); };
        buttons.Controls.Add(add); buttons.Controls.Add(cancel); layout.Controls.Add(buttons);
        Controls.Add(layout); AcceptButton = add; CancelButton = cancel;
    }

    public string ThreadId { get; private set; } = string.Empty;
    public IReadOnlyList<string> ThreadIds { get; private set; } = [];

    private void AcceptValue()
    {
        try
        {
            ThreadIds = _value.Text.Replace("\r", "\n", StringComparison.Ordinal)
                .Split(['\n', ',', '，', ';', '；'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(ProjectMonitorCliService.NormalizeThreadId)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();
            if (ThreadIds.Count == 0) throw new FormatException("请至少输入一个 Codex 任务 ID。");
            ThreadId = ThreadIds[0];
            DialogResult = DialogResult.OK;
            Close();
        }
        catch (Exception error)
        {
            MessageBox.Show(this, error.Message, "任务 ID 无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }
}
