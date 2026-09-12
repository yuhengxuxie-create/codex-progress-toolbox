using TreasureChest;
using TreasureChest.Core.Models;
using TreasureChest.Core.Services;
using TreasureChest.Integrations;
using TreasureChest.Services;
using TreasureChest.UI;
using System.Windows.Forms;

internal static class ResetAlertInboxGui
{
    // Explicit opt-in manual QA only. This bypasses Program's production mutex
    // and startup policy and uses no production configuration or CLI.
    public static void Run()
    {
        var root = Path.Combine(Path.GetTempPath(), "TreasureChest-ResetInbox-QA-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        File.WriteAllText(Path.Combine(root, "treasurechest.root"), "isolated reset inbox QA");
        File.WriteAllText(Path.Combine(root, "progress-wx.py"), "# QA placeholder, never executes Python");
        var stub = Path.Combine(root, "disabled-cli.cmd");
        File.WriteAllText(stub, "@echo off\r\necho QA: production CLI is disabled 1>&2\r\nexit /b 91\r\n");
        Environment.SetEnvironmentVariable("TREASURECHEST_ROOT", root);
        Environment.SetEnvironmentVariable("PROGRESS_WX_ROOT", root);
        Environment.SetEnvironmentVariable("PROGRESS_WX_PYTHON", stub);
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        var config = AppConfiguration.CreateDefault(root);
        config.Sessions.Clear();
        config.Tools.Clear();
        config.Settings.AutoStartEnabled = false;
        config.Settings.AutoCheckForUpdates = false;
        config.Settings.NotificationsEnabled = false;
        config.Settings.StartMinimizedToTray = false;
        var store = new ConfigStore(Path.Combine(root, "config.json"), root);
        store.SaveAsync(config).GetAwaiter().GetResult();
        var now = DateTimeOffset.Now;
        var inbox = new ResetAlertDedupStore(Path.Combine(root, ".state", "reset-alert-consumer.json"));
        inbox.SelectPendingAsync([
            new ResetAlertEvent("qa-upcoming", "A", "【模拟测试】官方未来公告示例", "今晚（模拟）", "仅用于验收", now, now.AddHours(2), null, "pending", true, "active", "upcoming"),
            new ResetAlertEvent("qa-available", "A", "【模拟测试】官方已公布重置券示例", "按公告适用条件（模拟）", "查看额度或重置券；本例不代表真实账户到账", now.AddMinutes(-1), now.AddHours(2), null, "uncertain", true, "active", "announced_available"),
            new ResetAlertEvent("qa-expired", "B", "【模拟测试】历史证据", "已过期", "仅供回看", now.AddDays(-1), now.AddHours(-1), null, "expired", false, "expired", "watch"),
        ], now).GetAwaiter().GetResult();
        var logger = new AppLogger(Path.Combine(root, "qa.log"));
        UiTheme.Initialize("day");
        using var form = new MainForm(config, store, logger, new SessionManager(logger),
            new PluginCatalog(Path.Combine(root, "plugins")), new AutoStartService(suppressWrites: true),
            startupLaunch: false, SessionSearchCacheMode.Ephemeral, suppressInitialLoadForTests: true);
        form.Text = "模拟验收 · Codex 重置预警记录";
        // QA uses the real disposal path, but never hides behind a tray icon
        // indistinguishable from the running production instance.
        typeof(MainForm).GetField("_allowExit", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
            .SetValue(form, true);
        var tray = (ShellTrayIcon)typeof(MainForm).GetField("_trayIcon",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!.GetValue(form)!;
        tray.Visible = false;
        var exitButton = new Button
        {
            Text = "退出模拟验收 (F12)", Width = 168, Height = 32,
            Location = new System.Drawing.Point(form.ClientSize.Width - 300, 12),
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
        };
        exitButton.Click += (_, _) => form.Close();
        form.Controls.Add(exitButton);
        form.Shown += (_, _) => exitButton.BringToFront();
        form.KeyPreview = true;
        form.KeyDown += (_, e) =>
        {
            if (e.KeyCode != Keys.F12) return;
            e.Handled = true;
            form.Close();
        };
        using var exitTimer = new System.Windows.Forms.Timer { Interval = 15 * 60 * 1000 };
        exitTimer.Tick += (_, _) => { exitTimer.Stop(); form.Close(); };
        exitTimer.Start();
        Console.WriteLine("QA data only: " + root);
        Console.WriteLine("Exit using 退出模拟验收 or F12 from the main window. Automatic exit after 15 minutes, even if hidden. Product X retains hide behavior.");
        Console.WriteLine("Open 会话管理 → 预警记录. No production CLI, polling, shell alerts or HKCU Run writes.");
        Application.Run(form);
    }
}
