namespace WinFx;

/// <summary>
/// Diagnostics sink. stderr only — stdout is reserved for the NDJSON protocol
/// ({"ok":true,"msg":"..."} lines), so it must never carry log text.
/// </summary>
internal static class Log
{
    public static void Info(string s)
    {
        string line = DateTime.Now.ToString("HH:mm:ss.fff") + " " + s;
        try { Console.Error.WriteLine(line); Console.Error.Flush(); } catch { }
    }
}
