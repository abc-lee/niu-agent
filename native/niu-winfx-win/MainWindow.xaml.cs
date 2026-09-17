using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Windows.Graphics;

namespace WinFx;

/// <summary>
/// Zero-content WinUI 3 material window (route A native component, separate process).
///
/// Hosts a single system-acrylic backdrop element stretched over the whole client
/// area, clipped to arbitrary rects via SetWindowRgn, and stacked DIRECTLY BELOW the
/// attached host window (its hwnd is passed via 'attach'). No text, no buttons, no UI
/// of any kind — all content stays in the host's own window.
///
/// Protocol: NDJSON lines on stdin. stdout carries ONLY {"ok":true,"msg":"..."} lines;
/// diagnostics go to stderr.
///
/// Commands:
///   attach {hwnd, rect:{x,y,w,h}, scaleFactor}  remember the window to stack under + initial geometry
///   geom   {x,y,w,h}                             move/resize self AND re-assert z-order under the attached hwnd
///   rects  {rects:[{x,y,w,h,r}], viewW, viewH}  rebuild the window region (r>0 round, r=0 square;
///                                               empty list = empty region, NEVER SetWindowRgn(NULL))
///   show / hide                                  hide = SW_HIDE, process stays resident; show must be live same frame
///   params {kind,tint,tintOpacity,lumOpacity}    live material tuning
///   reveal {mode:'instant'|'follow'|'sweepFixed'|'params', ms}
///   quit
/// </summary>
public sealed partial class MainWindow : Window
{
    // ------------------------------------------------------------------ win32

    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int L, T, R, B; }

    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr h, int cmdShow);
    [DllImport("user32.dll", SetLastError = true)] private static extern int SetWindowRgn(IntPtr h, IntPtr rgn, bool redraw);
    // GetWindowRgn returns the region handle (NULL = NULLREGION) and the bounding box.
    // NOTE (measured on Win11 26200): user32's GetWindowRgn is a broken stub here — it returns
    // NULL even after SetWindowRgn=1, on ANY window kind (plain visible WinForms window, region
    // set, still NULL; gdi32 does not export it on this build). Keep the call for the record;
    // region truth on this build is SetWindowRgn's return value + pixel probing.
    [DllImport("user32.dll")] private static extern IntPtr GetWindowRgn(IntPtr h, out RECT r);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW", SetLastError = true)] private static extern IntPtr GetWindowLongPtr(IntPtr h, int idx);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)] private static extern IntPtr SetWindowLongPtr(IntPtr h, int idx, IntPtr v);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr h);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateRectRgn(int l, int t, int r, int b);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateRoundRectRgn(int l, int t, int r, int b, int ew, int eh);
    [DllImport("gdi32.dll")] private static extern IntPtr CombineRgn(IntPtr dst, IntPtr s1, IntPtr s2, int op);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr o);

    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_TOOLWINDOW = 0x0080;
    private const int WS_EX_NOACTIVATE = 0x08000000;
    private const int SW_HIDE = 0, SW_SHOWNA = 8;
    private const uint SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_NOZORDER = 0x0004, SWP_NOACTIVATE = 0x0010;
    private const int RGN_OR = 2;
    private static readonly IntPtr HWND_TOPMOST = new(-1);

    // ------------------------------------------------------------------ state

    private readonly IntPtr _hwnd;
    private IntPtr _attachHwnd = IntPtr.Zero;   // window we stack directly under (Electron mini window)
    private double _scale = 1.0;                // protocol units (DIP) -> physical px
    private List<DRect> _lastRects = new();     // last 'rects' payload, in protocol units
    private double _viewW, _viewH;
    private readonly List<DispatcherQueueTimer> _timers = new(); // rooted: collected timers never tick

    private readonly struct DRect
    {
        public readonly double X, Y, W, H, R;
        public DRect(double x, double y, double w, double h, double r) { X = x; Y = y; W = w; H = h; R = r; }
    }

    // ------------------------------------------------------------------ ctor

    public MainWindow()
    {
        InitializeComponent();
        Title = "niui-winfx material window";

        ExtendsContentIntoTitleBar = true;
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);

        // Tool window (no taskbar entry) + never takes focus.
        var ex = new IntPtr(GetWindowLongPtr(_hwnd, GWL_EXSTYLE).ToInt64() | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE);
        SetWindowLongPtr(_hwnd, GWL_EXSTYLE, ex);

        // Park off-screen until 'attach' arrives so nothing flashes at a default position.
        AppWindow.MoveAndResize(new RectInt32(-3000, -3000, 488, 216));

        Activated += (s, e) => OnActivation(e.WindowActivationState != WindowActivationState.Deactivated);
        Closed += (s, e) => { Log.Info("[win] closed -> exit"); Environment.Exit(0); };

        Root.Loaded += (s, e) =>
        {
            ShowWindow(_hwnd, SW_HIDE);   // hidden until attach/show
            // Always-on-top via the presenter API (WinAppSDK 2.4 has no AppWindow.IsAlwaysOnTop),
            // plus the HWND_TOPMOST belt-and-braces.
            if (AppWindow.Presenter is OverlappedPresenter op) op.IsAlwaysOnTop = true;
            SetWindowPos(_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            Log.Info("=== winfx start ===");
            Log.Info("hwnd=0x" + _hwnd.ToString("x") + " ex=0x" + ex.ToString("x") +
                     " presenter=" + (AppWindow.Presenter?.GetType().Name ?? "<null>") + " topmost=true");
            Log.Info("BackdropEl.SystemBackdrop = " + (BackdropEl.SystemBackdrop?.GetType().FullName ?? "<null>"));
            Log.Info("DesktopAcrylicController.IsSupported() = " + DesktopAcrylicController.IsSupported());
            Log.Info("GetDpiForWindow=" + GetDpiForWindow(_hwnd) + "  material: " + Acrylic.Status);
            StartStdin();
        };
    }

    // ------------------------------------------------------------------ stdin protocol

    private void StartStdin()
    {
        var t = new Thread(ReadStdin) { IsBackground = true, Name = "stdin-ndjson" };
        t.Start();
        Log.Info("[stdin] reader thread started");
    }

    private void ReadStdin()
    {
        try
        {
            using var reader = new StreamReader(Console.OpenStandardInput());
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                line = line.Trim();
                if (line.Length == 0) continue;
                var l = line;
                DispatcherQueue.TryEnqueue(() => HandleLine(l));
            }
            Log.Info("[stdin] EOF -> exit");
            DispatcherQueue.TryEnqueue(() => Environment.Exit(0));
        }
        catch (Exception ex) { Log.Info("[stdin] reader FAILED: " + ex.Message); }
    }

    private void HandleLine(string line)
    {
        Log.Info("[stdin] " + line);
        string msg;
        try
        {
            using var doc = JsonDocument.Parse(line);
            var e = doc.RootElement;
            string cmd = e.TryGetProperty("cmd", out var c) ? (c.GetString() ?? "") : "";

            if (cmd == "quit") { Log.Info("[cmd] quit"); EmitJson("quit"); Close(); return; }

            switch (cmd)
            {
                case "attach": msg = DoAttach(e); break;
                case "geom":   msg = DoGeom(e); break;
                case "rects":  msg = DoRects(e); break;
                case "show":   msg = DoShow(); break;
                case "hide":   msg = DoHide(); break;
                case "params": msg = DoParams(e); break;
                case "reveal": msg = DoReveal(e); break;
                default: Log.Info("!! unknown cmd: " + cmd); msg = "unknown:" + cmd; break;
            }
        }
        catch (Exception ex)
        {
            Log.Info("!! cmd FAILED: " + ex.Message);
            msg = "error:" + ex.Message;
        }
        EmitJson(msg);
    }

    private static void EmitJson(string msg)
    {
        // stdout is protocol-only: one {"ok":true,"msg":"..."} line per command.
        string safe = msg.Replace("\\", "\\\\").Replace("\"", "\\\"");
        string line = "{\"ok\":true,\"msg\":\"" + safe + "\"}";
        try { Console.Out.WriteLine(line); Console.Out.Flush(); } catch { }
    }

    private static double Dip(JsonElement e, string k) =>
        e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0.0;

    /// <summary>
    /// Geometry protocol units are DIP; the process is Per-Monitor-DPI-V2, so scale by the
    /// host-provided scaleFactor (fallback: GetDpiForWindow) before touching Win32 pixels.
    /// </summary>
    private string DoAttach(JsonElement e)
    {
        long h = 0;
        if (e.TryGetProperty("hwnd", out var he))
            h = he.ValueKind == JsonValueKind.String ? long.Parse(he.GetString()) : he.GetInt64();
        _attachHwnd = new IntPtr(h);

        var s = Dip(e, "scaleFactor");
        if (s <= 0) s = GetDpiForWindow(_hwnd) / 96.0;
        _scale = s;

        int x = 0, y = 0, w = 488, hh = 216;
        if (e.TryGetProperty("rect", out var r))
        {
            x = (int)Math.Round(Dip(r, "x") * s);
            y = (int)Math.Round(Dip(r, "y") * s);
            w = (int)Math.Round(Dip(r, "w") * s);
            hh = (int)Math.Round(Dip(r, "h") * s);
        }

        // Stacking: hWndInsertAfter = the host window's hwnd puts us DIRECTLY BELOW it.
        // (Measured: the -3 HWND_BOTTOM sentinel is rejected on Win11 26200 with
        //  ERROR_INVALID_WINDOW_HANDLE(1400); a real target hwnd works in every form.
        //  hwnd=0 is a valid no-z-order-change sentinel, used when no host window exists.)
        AppWindow.MoveAndResize(new RectInt32(x, y, w, hh));
        ShowWindow(_hwnd, SW_SHOWNA);
        bool z = SetWindowPos(_hwnd, _attachHwnd, x, y, w, hh, SWP_NOACTIVATE);
        Log.Info("[attach] under=0x" + h.ToString("x") + " rect=(" + x + "," + y + " " + w + "x" + hh +
                 ") s=" + s.ToString(System.Globalization.CultureInfo.InvariantCulture) +
                 " SetWindowPos=" + z + (z ? "" : " err=" + Marshal.GetLastWin32Error()));
        ReassertZ(250);
        return "attach";
    }

    private string DoGeom(JsonElement e)
    {
        int x = (int)Math.Round(Dip(e, "x") * _scale);
        int y = (int)Math.Round(Dip(e, "y") * _scale);
        int w = (int)Math.Round(Dip(e, "w") * _scale);
        int h = (int)Math.Round(Dip(e, "h") * _scale);
        AppWindow.MoveAndResize(new RectInt32(x, y, w, h));
        if (_attachHwnd != IntPtr.Zero)
            SetWindowPos(_hwnd, _attachHwnd, x, y, w, h, SWP_NOACTIVATE);   // re-assert z-order
        Log.Info("[geom] (" + x + "," + y + " " + w + "x" + h + ") re-z under=0x" + _attachHwnd.ToString("x"));
        ReassertZ(250);
        return "geom";
    }

    private string DoRects(JsonElement e)
    {
        _lastRects = new List<DRect>();
        if (e.TryGetProperty("rects", out var arr))
            foreach (var r in arr.EnumerateArray())
                _lastRects.Add(new DRect(Dip(r, "x"), Dip(r, "y"), Dip(r, "w"), Dip(r, "h"), Dip(r, "r")));
        _viewW = Dip(e, "viewW");
        _viewH = Dip(e, "viewH");
        string res = ApplyRegion(_lastRects);
        Log.Info("[rects] n=" + _lastRects.Count + " view=" + (int)_viewW + "x" + (int)_viewH +
                 " s=" + _scale + " -> " + res);
        ReassertZ(200);
        return "rects:" + res;
    }

    private string DoShow()
    {
        ShowWindow(_hwnd, SW_SHOWNA);
        // Measured: while the window is hidden SetWindowRgn returns 0, so rects packets sent
        // during the hidden period are lost. Re-apply the cached region now that the window is
        // visible again => the show frame is already the correct shape.
        ApplyRegion(_lastRects);
        Log.Info("[show] SW_SHOWNA region-reapplied");
        // Measured: ShowWindow on a topmost window makes the OS re-elevate it to the TOP of the
        // topmost band — ABOVE the attached host window. Re-assert "directly below" after.
        ReassertZ(60);
        return "show";
    }

    private string DoHide()
    {
        ShowWindow(_hwnd, SW_HIDE);
        Log.Info("[hide] SW_HIDE (resident, process stays alive)");
        return "hide";
    }

    private string DoParams(JsonElement e)
    {
        if (e.TryGetProperty("kind", out var k) && k.ValueKind == JsonValueKind.String)
        {
            var s = k.GetString();
            if (s != null)
                Acrylic.Kind = s.Equals("Base", StringComparison.OrdinalIgnoreCase)
                    ? DesktopAcrylicKind.Base : DesktopAcrylicKind.Thin;
        }
        if (e.TryGetProperty("tint", out var tint))
        {
            string hex = null;
            if (tint.ValueKind == JsonValueKind.String) hex = tint.GetString();
            else if (tint.ValueKind == JsonValueKind.Number) hex = tint.GetUInt32().ToString("x8");
            if (hex != null)
            {
                hex = hex.Trim().TrimStart('#');
                if (hex.Length == 6) hex = "FF" + hex;
                if (hex.Length == 8)
                {
                    Acrylic.TintColor = Windows.UI.Color.FromArgb(
                        (byte)int.Parse(hex.Substring(0, 2), System.Globalization.NumberStyles.HexNumber),
                        (byte)int.Parse(hex.Substring(2, 2), System.Globalization.NumberStyles.HexNumber),
                        (byte)int.Parse(hex.Substring(4, 2), System.Globalization.NumberStyles.HexNumber),
                        (byte)int.Parse(hex.Substring(6, 2), System.Globalization.NumberStyles.HexNumber));
                }
                else Log.Info("!! tint unparseable: " + hex);
            }
        }
        if (e.TryGetProperty("tintOpacity", out var to) && to.ValueKind == JsonValueKind.Number)
            Acrylic.TintOpacity = Math.Clamp(to.GetDouble(), 0, 1);
        if (e.TryGetProperty("lumOpacity", out var lo) && lo.ValueKind == JsonValueKind.Number)
            Acrylic.LuminosityOpacity = Math.Clamp(lo.GetDouble(), 0, 1);
        Acrylic.Apply();
        var c = Acrylic.TintColor;
        Log.Info("[params] " + Acrylic.Status + " tint=sc#" + c.A.ToString("X2") + c.R.ToString("X2")
                 + c.G.ToString("X2") + c.B.ToString("X2"));
        return "params:" + Acrylic.Status;
    }

    private string DoReveal(JsonElement e)
    {
        string mode = e.TryGetProperty("mode", out var m) && m.ValueKind == JsonValueKind.String
            ? (m.GetString() ?? "instant") : "instant";
        int ms = e.TryGetProperty("ms", out var mm) && mm.ValueKind == JsonValueKind.Number
            ? (int)mm.GetInt32() : 200;
        switch (mode)
        {
            case "instant":
                Log.Info("[reveal] instant -> region re-asserted: " + ApplyRegion(_lastRects));
                return "reveal:instant";
            case "follow":
                // 'follow' needs no local animation: the host sends per-frame rects and we
                // apply each one verbatim — the front edge tracks the curtain 1:1.
                Log.Info("[reveal] follow -> no-op by design; applying per-frame 'rects' from stdin");
                return "reveal:follow";
            case "sweepFixed":
                StartSweep(Math.Max(ms, 16));
                return "reveal:sweepFixed";
            case "params":
                StartParamFade(Math.Max(ms, 16));
                return "reveal:params";
            default:
                Log.Info("!! unknown reveal mode: " + mode);
                return "reveal:unknown(" + mode + ")";
        }
    }

    // ------------------------------------------------------------------ region

    /// <summary>
    /// Rebuilds the window region from DIP rects, scaled by _scale (window coordinates).
    /// Empty list => empty region CreateRectRgn(0,0,0,0). NEVER SetWindowRgn(NULL) to mean
    /// "no region": NULL removes the clip and exposes the whole window's material.
    /// </summary>
    private string ApplyRegion(List<DRect> rects)
    {
        double s = _scale;
        IntPtr acc = IntPtr.Zero;
        foreach (var rc in rects)
        {
            int l = (int)Math.Round(rc.X * s);
            int t = (int)Math.Round(rc.Y * s);
            int rr = (int)Math.Round((rc.X + rc.W) * s);
            int b = (int)Math.Round((rc.Y + rc.H) * s);
            int er = (int)Math.Round(2 * rc.R * s);
            IntPtr one = rc.R > 0
                ? CreateRoundRectRgn(l, t, rr, b, er, er)
                : CreateRectRgn(l, t, rr, b);
            if (one == IntPtr.Zero)
            {
                if (acc != IntPtr.Zero) DeleteObject(acc);
                return "create-rgn-fail err=" + Marshal.GetLastWin32Error();
            }
            if (acc != IntPtr.Zero)
            {
                // MSDN: hdest, hsrcrdc, hsrcndc must be THREE DISTINCT regions.
                // Measured on Win11 26200: the dst==s1 form CombineRgn(rgn,rgn,one) fails
                // deterministically for n>=3. Use a fresh independent dst.
                // Probed on this build: a successful CombineRgn does NOT consume the two source
                // regions -> delete them manually or every rects packet leaks GDI handles.
                IntPtr dst = CreateRectRgn(0, 0, 0, 0);
                if (dst == IntPtr.Zero)
                {
                    DeleteObject(one);
                    DeleteObject(acc);
                    return "create-rgn-fail err=" + Marshal.GetLastWin32Error();
                }
                if (CombineRgn(dst, acc, one, RGN_OR) == IntPtr.Zero)
                {
                    int err = Marshal.GetLastWin32Error();
                    DeleteObject(dst);
                    DeleteObject(one);
                    DeleteObject(acc);
                    return "combine-fail err=" + err;
                }
                DeleteObject(acc);
                DeleteObject(one);
                acc = dst;
            }
            else
            {
                acc = one;   // first rect becomes the accumulator
            }
        }
        IntPtr rgn = acc;
        if (rgn == IntPtr.Zero)
        {
            rgn = CreateRectRgn(0, 0, 0, 0);   // empty region: nothing visible
            if (rgn == IntPtr.Zero) return "create-rgn-fail err=" + Marshal.GetLastWin32Error();
        }
        int set = SetWindowRgn(_hwnd, rgn, true);
        IntPtr g = GetWindowRgn(_hwnd, out RECT box);   // user32 export (stub on 26200, see note)
        string res = "SetWindowRgn=" + set +
                     (set == 0 ? " err=" + Marshal.GetLastWin32Error() : "") +
                     " GetWindowRgn=" + (g == IntPtr.Zero ? "NULLREGION" : "REGION") +
                     " box=(" + box.L + "," + box.T + " " + (box.R - box.L) + "x" + (box.B - box.T) + ")";
        if (g != IntPtr.Zero) DeleteObject(g);
        if (set == 0) DeleteObject(rgn);   // on failure SetWindowRgn did not take ownership
        return res;
    }

    // ------------------------------------------------------------------ reveal timers

    /// <summary>
    /// Diagnostic only: independent fixed left->right sweep of the region over ms.
    /// Deliberately DECOUPLED from the host's curtain animation.
    /// </summary>
    private void StartSweep(int ms)
    {
        if (_lastRects.Count == 0) { Log.Info("[sweepFixed] no rects to sweep"); return; }
        double L = _lastRects.Min(r => r.X);
        double R = _lastRects.Max(r => r.X + r.W);
        var t0 = Stopwatch.StartNew();
        var timer = DispatcherQueue.CreateTimer();
        _timers.Add(timer);
        timer.Interval = TimeSpan.FromMilliseconds(16);
        timer.Tick += (s, e) =>
        {
            double f = Math.Clamp(t0.Elapsed.TotalMilliseconds / ms, 0.0, 1.0);
            if (f >= 1.0)
            {
                timer.Stop();
                Log.Info("[sweepFixed] done -> " + ApplyRegion(_lastRects));
                return;
            }
            double cap = L + (R - L) * f;    // sweep front in protocol units
            var clipped = new List<DRect>();
            foreach (var rc in _lastRects)
            {
                double l = Math.Max(rc.X, L);
                if (cap <= l) continue;
                double w = Math.Min(rc.X + rc.W, cap) - l;
                clipped.Add(new DRect(rc.X, rc.Y, w, rc.H, Math.Min(rc.R, Math.Min(w, rc.H / 2))));
            }
            ApplyRegion(clipped);
        };
        timer.Start();
        Log.Info("[reveal] sweepFixed: fixed left->right sweep " + L + "->" + R +
                 " over " + ms + "ms (decoupled from host curtain, diagnostic only)");
    }

    private void StartParamFade(int ms)
    {
        double target = Acrylic.LuminosityOpacity;
        Acrylic.LuminosityOpacity = 0.0;
        Acrylic.Apply();
        var t0 = Stopwatch.StartNew();
        var timer = DispatcherQueue.CreateTimer();
        _timers.Add(timer);
        timer.Interval = TimeSpan.FromMilliseconds(16);
        timer.Tick += (s, e) =>
        {
            double f = Math.Clamp(t0.Elapsed.TotalMilliseconds / ms, 0.0, 1.0);
            Acrylic.LuminosityOpacity = target * f;
            Acrylic.Apply();
            if (f >= 1.0)
            {
                timer.Stop();
                Log.Info("[reveal] params fade done lum=" +
                         target.ToString(System.Globalization.CultureInfo.InvariantCulture));
            }
        };
        timer.Start();
        Log.Info("[reveal] params: LuminosityOpacity 0 -> " +
                 target.ToString(System.Globalization.CultureInfo.InvariantCulture) + " over " + ms + "ms");
    }

    // ------------------------------------------------------------------ activation

    private void OnActivation(bool active)
    {
        // Re-assert the material on every activation change. AcrylicBackdrop pins
        // IsInputActive=true so the blur survives even though this window never takes focus.
        Acrylic.Apply();
        Log.Info("[activation] active=" + active + "  re-applied " + Acrylic.Status +
                 "  alive=" + Acrylic.Alive);
    }

    /// <summary>
    /// Measured on Win11 26200: after showing a topmost window the OS asynchronously re-asserts
    /// topmost placement (window jumps to the TOP of the topmost band), undoing the attach z-order
    /// within ~1-2s. A one-shot delayed re-assert restores "directly below the target"; the host's
    /// per-frame 'geom' re-asserts for good measure during motion.
    /// </summary>
    private void ReassertZ(int ms)
    {
        if (_attachHwnd == IntPtr.Zero) return;
        Arm(ms, () =>
        {
            GetWindowRect(_hwnd, out RECT r);
            bool z = SetWindowPos(_hwnd, _attachHwnd, r.L, r.T, r.R - r.L, r.B - r.T, SWP_NOACTIVATE);
            Log.Info("[z] re-assert under=0x" + _attachHwnd.ToString("x") +
                     (z ? " ok" : " FAIL err=" + Marshal.GetLastWin32Error()));
        });
    }

    private void Arm(int ms, Action a)
    {
        var t = DispatcherQueue.CreateTimer();
        _timers.Add(t);   // rooted: a collected timer never ticks
        t.Interval = TimeSpan.FromMilliseconds(ms);
        t.Tick += (s, e) => { t.Stop(); a(); };
        t.Start();
    }
}
