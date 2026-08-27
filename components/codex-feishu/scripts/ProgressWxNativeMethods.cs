using System;
using System.Runtime.InteropServices;

namespace ProgressWx
{
    // 仅用于向当前 Windows 会话广播用户环境已更新；不读写代理、路由或网络设置。
    public static class NativeMethods
    {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(
            IntPtr hWnd,
            uint msg,
            UIntPtr wParam,
            string lParam,
            uint flags,
            uint timeout,
            out UIntPtr result);
    }
}
