using TreasureChest.Core.Services;
using TreasureChest.Integrations;
using TreasureChest.Services;
using TreasureChest.UI;

namespace TreasureChest;

internal static class Program
{
    private const string MutexName = "Local\\TreasureChest.Management.App.v1";
    private const string ActivationEventName = "Local\\TreasureChest.Management.App.ActivateEvent.v1";
    private const string ActivationMessageName = "TreasureChest.Management.App.Activate.v1";

    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var activateMessage = NativeMethods.RegisterWindowMessage(ActivationMessageName);
        using var activationEvent = new EventWaitHandle(false, EventResetMode.AutoReset, ActivationEventName);
        using var mutex = new Mutex(true, MutexName, out var isFirstInstance);
        if (!isFirstInstance)
        {
            activationEvent.Set();
            NativeMethods.PostMessage((IntPtr)NativeMethods.HwndBroadcast, activateMessage, IntPtr.Zero, IntPtr.Zero);
            return;
        }

        Directory.CreateDirectory(Path.Combine(AppPaths.Root, "logs"));
        var logger = new AppLogger(AppPaths.LogFile);
        if (!ShellIdentityService.TryInitializeCurrentProcess(out var shellIdentityResult))
            logger.Error($"Shell 身份初始化失败：HRESULT 0x{shellIdentityResult:X8}");
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, eventArgs) =>
        {
            logger.Error("界面线程未处理异常：" + eventArgs.Exception);
            MessageBox.Show(eventArgs.Exception.Message, "TreasureChest 发生错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
        };
        AppDomain.CurrentDomain.UnhandledException += (_, eventArgs) => logger.Error("未处理异常：" + eventArgs.ExceptionObject);

        try
        {
            var configStore = new ConfigStore(AppPaths.ConfigFile, AppPaths.Root);
            var config = configStore.LoadAsync().GetAwaiter().GetResult();
            UiTheme.Initialize(config.Settings.ThemeMode);
            var sessionSearchCacheMode = SessionSearchExecutionPolicy.FromApplicationArguments(args);
            var autoStart = new AutoStartService(
                suppressWrites: SessionSearchExecutionPolicy.SuppressSystemStateWrites(args));
            if (string.Equals(Path.GetFileNameWithoutExtension(Environment.ProcessPath), "TreasureChest", StringComparison.OrdinalIgnoreCase))
                autoStart.Apply(config.Settings.AutoStartEnabled);
            var sessionManager = new SessionManager(logger,
                new GuardianCliService(AppPaths.ProgressNotificationRoot, AppPaths.ProgressPythonPath));
            var pluginCatalog = new PluginCatalog(Path.Combine(AppPaths.Root, "plugins"));
            MainForm.ActivateMessage = activateMessage;
            using var form = new MainForm(config, configStore, logger, sessionManager, pluginCatalog, autoStart,
                args.Any(value => value.Equals("--startup", StringComparison.OrdinalIgnoreCase)),
                sessionSearchCacheMode);
            var shuttingDown = false;
            var activationThread = new Thread(() =>
            {
                while (true)
                {
                    activationEvent.WaitOne();
                    if (Volatile.Read(ref shuttingDown)) return;
                    try { form.BeginInvoke(form.ActivateFromExternalInstance); }
                    catch (InvalidOperationException) when (form.IsDisposed || !form.IsHandleCreated) { return; }
                    catch (ObjectDisposedException) { return; }
                }
            })
            {
                IsBackground = true,
                Name = "TreasureChest activation listener",
            };
            activationThread.Start();
            try { Application.Run(form); }
            finally
            {
                Volatile.Write(ref shuttingDown, true);
                activationEvent.Set();
                activationThread.Join(1000);
            }
        }
        catch (Exception error)
        {
            logger.Error("应用启动失败：" + error);
            MessageBox.Show(error.ToString(), "TreasureChest 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
