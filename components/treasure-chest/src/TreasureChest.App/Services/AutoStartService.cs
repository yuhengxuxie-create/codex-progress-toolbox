using Microsoft.Win32;

namespace TreasureChest.Services;

internal sealed class AutoStartService
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "TreasureChest";

    public void Apply(bool enabled)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey, writable: true)
            ?? throw new InvalidOperationException("无法打开当前用户开机启动注册表项。");
        if (enabled)
            key.SetValue(ValueName, $"\"{Environment.ProcessPath}\" --startup", RegistryValueKind.String);
        else
            key.DeleteValue(ValueName, throwOnMissingValue: false);
    }

    public bool IsEnabled()
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKey, writable: false);
        return key?.GetValue(ValueName) is string value && value.Contains("TreasureChest", StringComparison.OrdinalIgnoreCase);
    }
}
