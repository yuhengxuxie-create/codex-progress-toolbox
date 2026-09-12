namespace TreasureChest.Services;

internal static class ShellIdentityService
{
    internal const string AppUserModelId = "TreasureChest.LocalToolCenter";

    public static bool TryInitializeCurrentProcess(out int hresult)
    {
        hresult = NativeMethods.SetCurrentProcessExplicitAppUserModelID(AppUserModelId);
        return hresult >= 0;
    }
}
