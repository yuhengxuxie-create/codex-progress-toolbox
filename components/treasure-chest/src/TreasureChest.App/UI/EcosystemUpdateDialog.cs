using System.Diagnostics;
using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class EcosystemUpdateDialog : Form
{
    public EcosystemUpdateDialog(EcosystemInstallation installation, EcosystemReleaseInfo release)
    {
        UiTheme.ConfigureDpiAwareForm(this);
        Text = "发现生态更新";
        Font = UiTheme.CreateFont();
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = false;
        ClientSize = new Size(650, 520);
        BackColor = UiTheme.Background;
        Icon = IconService.LoadAppIcon();

        var title = UiTheme.Heading($"生态更新 {installation.DisplayVersion} → {release.DisplayVersion}", 16F);
        title.Location = new Point(24, 20);
        var scope = new Label
        {
            Text = "此次更新将统一升级：指令使用、进度监测／飞书机器人、百宝箱及配套文件。",
            AutoSize = false,
            Location = new Point(26, 62),
            Size = new Size(598, 42),
            ForeColor = UiTheme.Text,
        };
        var package = new Label
        {
            Text = $"升级包：{FormatSize(release.Package.Size)}    发布时间：{release.PublishedAt?.LocalDateTime:yyyy-MM-dd HH:mm}",
            AutoSize = true,
            Location = new Point(26, 108),
            ForeColor = UiTheme.Muted,
        };
        var management = new Label
        {
            Text = installation.ManagementMessage,
            AutoSize = false,
            Location = new Point(26, 136),
            Size = new Size(598, 42),
            ForeColor = installation.IsManaged ? UiTheme.Running : UiTheme.Warning,
        };
        var notesLabel = new Label
        {
            Text = "本次更新内容",
            AutoSize = true,
            Location = new Point(26, 185),
            Font = UiTheme.CreateFont(style: FontStyle.Bold),
            ForeColor = UiTheme.Text,
        };
        var notes = new ThemedEmbeddedTextBox
        {
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Vertical,
            Location = new Point(26, 212),
            Size = new Size(598, 230),
            BackColor = UiTheme.Surface,
            ForeColor = UiTheme.Text,
            Text = string.IsNullOrWhiteSpace(release.ReleaseNotes) ? "发布者尚未填写更新说明。" : release.ReleaseNotes,
        };
        var later = UiTheme.Button("稍后");
        later.Location = new Point(428, 465);
        later.DialogResult = DialogResult.Cancel;
        var details = UiTheme.Button("查看完整说明");
        details.Location = new Point(26, 465);
        details.Click += (_, _) => Process.Start(new ProcessStartInfo(release.ReleasePage.ToString()) { UseShellExecute = true });
        var install = UiTheme.Button("下载并安装", true);
        install.Location = new Point(516, 465);
        install.Enabled = installation.IsManaged;
        install.DialogResult = DialogResult.OK;

        var notesHost = new ThemedInputHost(notes) { Location = new Point(26, 212), Size = new Size(598, 230) };
        Controls.AddRange([title, scope, package, management, notesLabel, notesHost, later, details, install]);
        AcceptButton = install.Enabled ? install : details;
        CancelButton = later;
    }

    private static string FormatSize(long bytes) => bytes <= 0
        ? "未知大小"
        : $"{bytes / 1024d / 1024d:0.0} MB";
}
