using System.Runtime.InteropServices;
using Microsoft.UI.Composition;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace WinFx;

/// <summary>
/// A backdrop that stays acrylic even when the window is not the foreground window.
///
/// The stock <see cref="DesktopAcrylicBackdrop"/> follows the window's activation state: as soon
/// as the window loses focus the material collapses to a flat solid fill (~248,248,248 on a light
/// theme), which would destroy the whole point of a "frosted" material window.  Driving
/// <see cref="DesktopAcrylicController"/> ourselves and pinning
/// <see cref="SystemBackdropConfiguration.IsInputActive"/> to true keeps the real blurred
/// desktop sampling alive at all times (this window never takes focus), and lets the
/// tint/luminosity be tuned live via the 'params' protocol command.
///
/// Defaults per spec: Kind=Thin, TintColor=#202020, TintOpacity=0.00, LuminosityOpacity=0.20.
/// </summary>
public sealed class AcrylicBackdrop : SystemBackdrop
{
    private DesktopAcrylicController _controller;
    private SystemBackdropConfiguration _config;

    public DesktopAcrylicKind Kind { get; set; } = DesktopAcrylicKind.Thin;
    public double TintOpacity { get; set; } = 0.00;
    public double LuminosityOpacity { get; set; } = 0.20;
    public Color TintColor { get; set; } = Color.FromArgb(255, 32, 32, 32);

    public bool Alive => _controller != null;

    protected override void OnTargetConnected(ICompositionSupportsSystemBackdrop connectedTarget, XamlRoot xamlRoot)
    {
        base.OnTargetConnected(connectedTarget, xamlRoot);
        if (!DesktopAcrylicController.IsSupported()) { Log.Info("acrylic NOT supported"); return; }

        EnsureDispatcherQueue();
        _config = new SystemBackdropConfiguration
        {
            IsInputActive = true,                     // never flipped: no focus-loss degradation
            IsHighContrast = false,
            Theme = SystemBackdropTheme.Dark,
        };
        _controller = new DesktopAcrylicController();
        Apply();
        _controller.AddSystemBackdropTarget(connectedTarget);
        _controller.SetSystemBackdropConfiguration(_config);
        Log.Info("AcrylicBackdrop connected to " + connectedTarget.GetType().Name);
    }

    protected override void OnTargetDisconnected(ICompositionSupportsSystemBackdrop disconnectedTarget)
    {
        base.OnTargetDisconnected(disconnectedTarget);
        if (_controller == null) return;
        _controller.RemoveSystemBackdropTarget(disconnectedTarget);
        _controller.Dispose();
        _controller = null;
    }

    public void Apply()
    {
        if (_controller == null) return;
        _controller.Kind = Kind;
        _controller.TintColor = TintColor;
        _controller.TintOpacity = (float)TintOpacity;
        _controller.LuminosityOpacity = (float)LuminosityOpacity;
    }

    public string Status => string.Format(
        System.Globalization.CultureInfo.InvariantCulture,
        "tint={0:0.00} lum={1:0.00} {2}",
        TintOpacity, LuminosityOpacity, Kind == DesktopAcrylicKind.Thin ? "Thin" : "Base");

    // DesktopAcrylicController needs a Windows.System.DispatcherQueue on the thread.
    [StructLayout(LayoutKind.Sequential)]
    private struct DispatcherQueueOptions { public int dwSize; public int threadType; public int apartmentType; }

    [DllImport("CoreMessaging.dll")]
    private static extern int CreateDispatcherQueueController(DispatcherQueueOptions options, out IntPtr controller);

    private static void EnsureDispatcherQueue()
    {
        if (Windows.System.DispatcherQueue.GetForCurrentThread() != null) return;
        var o = new DispatcherQueueOptions
        {
            dwSize = Marshal.SizeOf<DispatcherQueueOptions>(),
            threadType = 2,       // DQTYPE_THREAD_CURRENT
            apartmentType = 2,    // DQTAT_COM_STA
        };
        CreateDispatcherQueueController(o, out _);
    }
}
