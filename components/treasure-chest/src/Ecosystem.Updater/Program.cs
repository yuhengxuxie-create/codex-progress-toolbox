using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;

namespace Ecosystem.Updater;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        try
        {
            var request = UpdateRequest.Parse(args);
            Application.Run(new UpdateProgressForm(request));
        }
        catch (Exception error)
        {
            Application.Run(new UpdaterErrorDialog(error.Message));
        }
    }
}

internal sealed record UpdateRequest(int ParentPid, string PackagePath, string InstallRoot, string Sha256, string Version)
{
    public static UpdateRequest Parse(string[] args)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var index = 0; index < args.Length; index += 2)
        {
            if (index + 1 >= args.Length || !args[index].StartsWith("--", StringComparison.Ordinal))
                throw new ArgumentException("更新器参数不完整。");
            values[args[index]] = args[index + 1];
        }
        if (!values.TryGetValue("--parent-pid", out var parent) || !int.TryParse(parent, out var parentPid) || parentPid <= 0)
            throw new ArgumentException("父进程 ID 无效。");
        var package = RequiredPath(values, "--package", mustExist: true);
        var installRoot = RequiredPath(values, "--install-root", mustExist: true);
        if (!values.TryGetValue("--sha256", out var sha) || sha.Length != 64 || !sha.All(Uri.IsHexDigit))
            throw new ArgumentException("升级包 SHA-256 无效。");
        if (!values.TryGetValue("--version", out var version) || string.IsNullOrWhiteSpace(version))
            throw new ArgumentException("目标版本无效。");
        return new UpdateRequest(parentPid, package, installRoot, sha.ToUpperInvariant(), version.Trim());
    }

    private static string RequiredPath(IReadOnlyDictionary<string, string> values, string name, bool mustExist)
    {
        if (!values.TryGetValue(name, out var value) || string.IsNullOrWhiteSpace(value))
            throw new ArgumentException($"缺少参数：{name}");
        var full = Path.GetFullPath(value);
        if (new Uri(full).IsUnc) throw new ArgumentException("更新只能在本机磁盘执行。");
        if (mustExist && !File.Exists(full) && !Directory.Exists(full)) throw new FileNotFoundException("更新目标不存在。", full);
        return full;
    }
}

internal sealed class UpdateProgressForm : Form
{
    private readonly UpdateRequest _request;
    private readonly Label _stage = new();
    private readonly Label _detail = new();
    private readonly UpdaterProgressBar _progress = new();
    private readonly UpdaterLogView _log = new();
    private readonly UpdaterButton _close = new();
    private readonly string _cacheRoot;
    private string _stagingRoot = string.Empty;
    private string _logPath = string.Empty;

    public UpdateProgressForm(UpdateRequest request)
    {
        _request = request;
        _cacheRoot = GetCacheRoot();
        AssertWithin(_request.PackagePath, _cacheRoot, "升级包不在受控缓存目录中。");
        Text = "Codex 飞书生态更新";
        Font = new Font("Microsoft YaHei UI", 9F);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        AutoScaleMode = AutoScaleMode.Dpi;
        AutoScaleDimensions = new SizeF(96F, 96F);
        MaximizeBox = false;
        MinimizeBox = false;
        ControlBox = false;
        ClientSize = new Size(640, 430);
        BackColor = UpdaterTheme.Background;

        var heading = new Label
        {
            Text = $"正在更新整套生态到 v{request.Version}",
            AutoSize = true,
            Font = new Font("Microsoft YaHei UI", 16F, FontStyle.Bold),
            ForeColor = UpdaterTheme.Text,
            Location = new Point(26, 22),
        };
        _stage.Text = "正在准备更新…";
        _stage.AutoSize = true;
        _stage.Font = new Font(Font, FontStyle.Bold);
        _stage.ForeColor = UpdaterTheme.Text;
        _stage.Location = new Point(28, 76);
        _detail.Text = "请不要关闭电脑。";
        _detail.AutoSize = false;
        _detail.Size = new Size(584, 42);
        _detail.ForeColor = UpdaterTheme.Muted;
        _detail.Location = new Point(28, 104);
        _progress.Location = new Point(28, 150);
        _progress.Size = new Size(584, 22);
        _progress.Style = ProgressBarStyle.Continuous;
        _log.BackColor = UpdaterTheme.Surface;
        _log.ForeColor = UpdaterTheme.Text;
        var logHost = new UpdaterSurfaceHost(_log)
        {
            Location = new Point(28, 190),
            Size = new Size(584, 180),
        };
        _close.Text = "关闭";
        _close.AutoSize = false;
        _close.Size = new Size(78, 38);
        _close.Enabled = false;
        _close.Location = new Point(534, 386);
        _close.Click += (_, _) => Close();
        Controls.AddRange([heading, _stage, _detail, _progress, logHost, _close]);
        Shown += async (_, _) => await RunUpdateAsync();
    }

    private async Task RunUpdateAsync()
    {
        await RunAttemptAsync(async () =>
        {
            Directory.CreateDirectory(Path.Combine(_cacheRoot, "logs"));
            _logPath = Path.Combine(_cacheRoot, "logs", $"update-{DateTime.Now:yyyyMMdd-HHmmss}.log");
            _stagingRoot = Path.Combine(_cacheRoot, "staging", _request.Version + "-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_stagingRoot);

            SetProgress(2, "等待百宝箱退出", "更新器将在原程序完全退出后开始替换文件。");
            await WaitForParentAsync(_request.ParentPid);
            SetProgress(7, "校验升级包", "正在核对 SHA-256，防止文件损坏或被替换。");
            await VerifyHashAsync(_request.PackagePath, _request.Sha256);
            SetProgress(14, "解压升级包", "正在把经过校验的文件解压到受控临时目录。");
            await ExtractSafelyAsync(_request.PackagePath, _stagingRoot, value => SetProgress(14 + value * 16 / 100, "解压升级包", $"解压进度 {value}%"));
            var packageRoot = FindPackageRoot(_stagingRoot);

            SetProgress(31, "复核安装包", "正在校验包内全部文件及运行环境签名。");
            var verify = Path.Combine(packageRoot, "installer", "verify-package.ps1");
            var verifyExit = await RunPowerShellAsync(verify, ["-PackageRoot", packageRoot]);
            if (verifyExit != 0) throw new InvalidOperationException("安装包复核失败，未修改现有程序。");

            var progressPath = Path.Combine(_cacheRoot, "progress-" + Guid.NewGuid().ToString("N") + ".json");
            SetProgress(38, "开始事务式更新", "正在备份必要数据并升级三个生态组件。");
            var upgrade = Path.Combine(packageRoot, "installer", "upgrade.ps1");
            var upgradeTask = RunPowerShellAsync(upgrade,
            [
                "-LegacyRoot", _request.InstallRoot,
                "-InstallRoot", _request.InstallRoot,
                "-NonInteractive",
                "-NoLaunch",
                "-ProgressFile", progressPath,
                "-CleanupBackupOnSuccess",
            ]);
            while (!upgradeTask.IsCompleted)
            {
                await ReadUpgradeProgressAsync(progressPath);
                await Task.Delay(250);
            }
            var upgradeExit = await upgradeTask;
            await ReadUpgradeProgressAsync(progressPath);
            if (upgradeExit != 0) throw new InvalidOperationException("生态更新脚本返回失败，请查看更新日志确认处理结果。");
            File.Delete(progressPath);

            SetProgress(100, "更新完成", "旧程序文件、升级包和临时文件将自动清理，正在重新打开百宝箱。");
            AppendLog("更新成功。\r\n");
            await Task.Delay(1500);
        }, error =>
        {
            SetProgress(Math.Max(1, _progress.Value), "更新没有完成", FailureDetail(error.Message, _logPath), failed: true);
            AppendLog("ERROR " + error + "\r\n");
            _close.Enabled = true;
        }, () =>
        {
            CleanupMaterial();
            TryLaunchTreasureChest();
            Close();
        });
    }

    internal static async Task RunAttemptAsync(Func<Task> update, Action<Exception> failed, Action succeeded)
    {
        try { await update(); }
        catch (Exception error) { failed(error); return; }
        succeeded();
    }

    internal static string FailureDetail(string error, string logPath) =>
        error + "\n未自动重新打开程序；请查看更新日志确认回滚结果，再决定恢复或重试。" +
        (string.IsNullOrWhiteSpace(logPath) ? "\n请查看本窗口的更新日志。" : "\n日志文件：" + logPath);

    private async Task<int> RunPowerShellAsync(string script, IReadOnlyList<string> arguments)
    {
        var start = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(script)!,
        };
        foreach (var argument in new[] { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script })
            start.ArgumentList.Add(argument);
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("无法启动 PowerShell 更新脚本。");
        var stdout = PumpAsync(process.StandardOutput);
        var stderr = PumpAsync(process.StandardError);
        await Task.WhenAll(process.WaitForExitAsync(), stdout, stderr);
        return process.ExitCode;
    }

    private async Task PumpAsync(StreamReader reader)
    {
        while (await reader.ReadLineAsync() is { } line) AppendLog(line + "\r\n");
    }

    private async Task ReadUpgradeProgressAsync(string path)
    {
        if (!File.Exists(path)) return;
        try
        {
            await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var value = await JsonSerializer.DeserializeAsync<UpgradeProgress>(stream,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            if (value is null) return;
            SetProgress(Math.Clamp(value.Percent, 38, 99), value.Stage ?? "正在更新", value.Message ?? string.Empty,
                value.State?.Equals("rollback", StringComparison.OrdinalIgnoreCase) == true);
        }
        catch (Exception error) when (error is IOException or JsonException) { }
    }

    private void CleanupMaterial()
    {
        try
        {
            if (!string.IsNullOrWhiteSpace(_stagingRoot) && Directory.Exists(_stagingRoot))
            {
                AssertWithin(_stagingRoot, Path.Combine(_cacheRoot, "staging"), "临时目录越界。");
                Directory.Delete(_stagingRoot, true);
            }
            if (File.Exists(_request.PackagePath)) File.Delete(_request.PackagePath);
        }
        catch (Exception error) { AppendLog("清理将在下次启动继续：" + error.Message + "\r\n"); }
    }

    private void TryLaunchTreasureChest()
    {
        try
        {
            var app = Path.Combine(_request.InstallRoot, "TreasureChest.exe");
            if (File.Exists(app)) Process.Start(new ProcessStartInfo(app) { WorkingDirectory = _request.InstallRoot, UseShellExecute = true });
        }
        catch (Exception error) { AppendLog("无法重新打开百宝箱：" + error.Message + "\r\n"); }
    }

    private static async Task WaitForParentAsync(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            await process.WaitForExitAsync();
        }
        catch (ArgumentException) { }
    }

    private static async Task VerifyHashAsync(string path, string expected)
    {
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1024 * 128,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        var actual = Convert.ToHexString(await SHA256.HashDataAsync(stream));
        if (!actual.Equals(expected, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("升级包 SHA-256 不匹配，已拒绝安装。");
    }

    private static async Task ExtractSafelyAsync(string zipPath, string destination, Action<int> progress)
    {
        await Task.Run(() =>
        {
            var root = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            using var archive = ZipFile.OpenRead(zipPath);
            long total = archive.Entries.Sum(entry => Math.Max(0, entry.Length));
            long completed = 0;
            foreach (var entry in archive.Entries)
            {
                var target = Path.GetFullPath(Path.Combine(destination, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("压缩包包含越界路径。");
                if (string.IsNullOrEmpty(entry.Name)) Directory.CreateDirectory(target);
                else
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                    entry.ExtractToFile(target, true);
                }
                completed += Math.Max(0, entry.Length);
                progress(total > 0 ? (int)Math.Clamp(completed * 100 / total, 0, 100) : 100);
            }
        });
    }

    private static string FindPackageRoot(string stagingRoot)
    {
        var candidates = Directory.EnumerateFiles(stagingRoot, "upgrade.ps1", SearchOption.AllDirectories)
            .Where(path => string.Equals(Path.GetFileName(Path.GetDirectoryName(path)), "installer", StringComparison.OrdinalIgnoreCase))
            .Select(path => Directory.GetParent(Path.GetDirectoryName(path)!)!.FullName)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        return candidates.Length == 1 ? candidates[0] : throw new InvalidDataException("升级包目录结构无效。");
    }

    private void SetProgress(int percent, string stage, string detail, bool failed = false)
    {
        if (InvokeRequired) { BeginInvoke(() => SetProgress(percent, stage, detail, failed)); return; }
        _progress.Value = Math.Clamp(percent, 0, 100);
        _stage.Text = stage;
        _stage.ForeColor = failed ? UpdaterTheme.Error : UpdaterTheme.Text;
        _detail.Text = detail;
    }

    private void AppendLog(string text)
    {
        if (InvokeRequired) { BeginInvoke(() => AppendLog(text)); return; }
        _log.AppendText(text);
        if (!string.IsNullOrWhiteSpace(_logPath))
        {
            try { File.AppendAllText(_logPath, text); } catch { }
        }
    }

    private static string GetCacheRoot()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(local)) throw new InvalidOperationException("LOCALAPPDATA 不可用。");
        return Path.GetFullPath(Path.Combine(local, "CodexFeishuEcosystemUpdates"));
    }

    private static void AssertWithin(string path, string root, string message)
    {
        var full = Path.GetFullPath(path);
        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        if (!full.Equals(rootFull, StringComparison.OrdinalIgnoreCase) &&
            !full.StartsWith(rootFull + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException(message);
    }

    private sealed class UpgradeProgress
    {
        public int Percent { get; set; }
        public string? Stage { get; set; }
        public string? Message { get; set; }
        public string? State { get; set; }
    }
}
