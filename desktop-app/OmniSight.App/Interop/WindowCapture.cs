using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace OmniSight.App.Interop;

/// <summary>
/// Keeps this window out of screenshots (the same <c>WDA_EXCLUDEFROMCAPTURE</c> the Qt window used), so OmniSight never sees
/// its own answer and watch mode never reads its own output. Set <c>OMNISIGHT_ALLOW_CAPTURE=1</c> only to take review screenshots.
/// </summary>
public static class WindowCapture
{
    private const uint WdaExcludeFromCapture = 0x11;
    public const string AllowVariable = "OMNISIGHT_ALLOW_CAPTURE";

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetWindowDisplayAffinity(IntPtr hwnd, uint affinity);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetWindowDisplayAffinity(IntPtr hwnd, out uint affinity);

    public static bool Allowed => Environment.GetEnvironmentVariable(AllowVariable) == "1";

    /// <summary>Exclude the window from capture. Returns false if Windows refused (then the app must say so).</summary>
    public static bool Exclude(Window window)
    {
        if (Allowed)
        {
            return false;
        }

        var handle = new WindowInteropHelper(window).Handle;
        return handle != IntPtr.Zero && SetWindowDisplayAffinity(handle, WdaExcludeFromCapture);
    }

    public static bool IsExcluded(Window window)
    {
        var handle = new WindowInteropHelper(window).Handle;
        return handle != IntPtr.Zero && GetWindowDisplayAffinity(handle, out var affinity) && affinity == WdaExcludeFromCapture;
    }
}
