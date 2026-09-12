using TreasureChest.Services;

namespace TreasureChest.UI;

internal sealed class UpdateDownloadDialog : Form
{
    private readonly Label _stage = new();
    private readonly Label _detail = new();
    private readonly ThemedProgressBar _progress = new();
    private readonly Button _cancel = UiTheme.Button("取消下载");

    public UpdateDownloadDialog(string version)
    {
        UiTheme.ConfigureDpiAwareForm(this);
        Text = "下载生态更新";
        Font = UiTheme.CreateFont();
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ControlBox = false;
        ShowInTaskbar = false;
        ClientSize = new Size(520, 210);
        BackColor = UiTheme.Background;
        Icon = IconService.LoadAppIcon();

        var title = UiTheme.Heading($"正在准备 {version}", 15F);
        title.Location = new Point(24, 20);
        _stage.Text = "正在连接 GitHub…";
        _stage.AutoSize = true;
        _stage.Location = new Point(26, 70);
        _stage.ForeColor = UiTheme.Text;
        _detail.Text = "尚未开始下载";
        _detail.AutoSize = false;
        _detail.Size = new Size(468, 28);
        _detail.Location = new Point(26, 98);
        _detail.ForeColor = UiTheme.Muted;
        _progress.Location = new Point(26, 130);
        _progress.Size = new Size(468, 20);
        _progress.Style = ProgressBarStyle.Continuous;
        _cancel.AutoSize = true;
        _cancel.Location = new Point(402, 168);
        _cancel.Click += (_, _) => Cancellation.Cancel();
        Controls.AddRange([title, _stage, _detail, _progress, _cancel]);
    }

    public CancellationTokenSource Cancellation { get; } = new();

    public IProgress<UpdateDownloadProgress> CreateProgress() => new Progress<UpdateDownloadProgress>(value =>
    {
        _stage.Text = "正在下载统一生态升级包…";
        if (value.TotalBytes is > 0)
        {
            _progress.Style = ProgressBarStyle.Continuous;
            _progress.Value = value.Percentage;
            _detail.Text = $"{FormatSize(value.BytesReceived)} / {FormatSize(value.TotalBytes.Value)}    {value.Percentage}%";
        }
        else
        {
            _progress.Style = ProgressBarStyle.Marquee;
            _detail.Text = $"已下载 {FormatSize(value.BytesReceived)}";
        }
    });

    public void SetVerifying()
    {
        _cancel.Enabled = false;
        _stage.Text = "下载完成，正在校验升级包…";
        _detail.Text = "正在核对文件大小与 SHA-256，校验通过后才会启动更新。";
        _progress.Style = ProgressBarStyle.Marquee;
    }

    private static string FormatSize(long bytes) => $"{bytes / 1024d / 1024d:0.0} MB";

    protected override void Dispose(bool disposing)
    {
        if (disposing) Cancellation.Dispose();
        base.Dispose(disposing);
    }
}
