using System.Text;

namespace OmniSight.Core.Logging;

/// <summary>
/// A small log file for the app (<c>%APPDATA%\OmniSight\logs\app.log</c>, next to the Python client's <c>client.log</c>), so a
/// crash or a refused connection leaves a trail. It never throws, never logs the connection token or typed text, and keeps at
/// most two files of about 1 MB each.
/// </summary>
public static class AppLog
{
    public const long MaxBytes = 1024 * 1024;
    private const int MaxLineChars = 2000;
    private static readonly object Gate = new();

    /// <summary>Tests point this at a temp file.</summary>
    public static string? PathOverride { get; set; }

    public static string FilePath =>
        PathOverride ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "OmniSight", "logs", "app.log");

    public static void Info(string message) => Write("INFO", message);

    public static void Warning(string message) => Write("WARN", message);

    public static void Error(string message, Exception? exception = null) =>
        Write("ERROR", exception is null ? message : $"{message}: {exception.GetType().Name}: {exception.Message}{Environment.NewLine}{exception.StackTrace}");

    public static void Write(string level, string message)
    {
        try
        {
            lock (Gate)
            {
                var path = FilePath;
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                Rotate(path);
                var clean = message.Length > MaxLineChars ? message[..MaxLineChars] + "…" : message;
                File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {level,-5} {clean}{Environment.NewLine}", Encoding.UTF8);
            }
        }
        catch (Exception)
        {
            // logging must never be the reason something fails
        }
    }

    private static void Rotate(string path)
    {
        var info = new FileInfo(path);
        if (info.Exists && info.Length > MaxBytes)
        {
            var old = path + ".1";
            File.Delete(old);
            File.Move(path, old);
        }
    }
}
