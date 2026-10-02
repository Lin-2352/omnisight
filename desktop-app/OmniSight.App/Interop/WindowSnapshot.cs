using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace OmniSight.App.Interop;

/// <summary>
/// Development only: when <c>OMNISIGHT_SNAPSHOT_DIR</c> names a folder, a file <c>snapshot.request</c> dropped there makes the
/// window render ITSELF to <c>&lt;name&gt;.png</c> (the request's text, letters and digits only). It draws the window's own
/// content, so it never sees anything else on the screen, and it does nothing at all when the variable is not set.
/// </summary>
public sealed class WindowSnapshot
{
    public const string Variable = "OMNISIGHT_SNAPSHOT_DIR";

    private readonly Window _window;
    private readonly string _folder;
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(300) };

    private WindowSnapshot(Window window, string folder)
    {
        _window = window;
        _folder = folder;
        _timer.Tick += (_, _) => Poll();
    }

    public static WindowSnapshot? StartIfRequested(Window window)
    {
        var folder = Environment.GetEnvironmentVariable(Variable);
        if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
        {
            return null;
        }

        var snapshot = new WindowSnapshot(window, folder);
        snapshot._timer.Start();
        return snapshot;
    }

    public static string SafeName(string text)
    {
        var name = new string(text.Where(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-').Take(60).ToArray());
        return name.Length > 0 ? name : "snapshot";
    }

    private void Poll()
    {
        var request = Path.Combine(_folder, "snapshot.request");
        if (!File.Exists(request))
        {
            return;
        }

        string text;
        try
        {
            text = File.ReadAllText(request);
            File.Delete(request);
        }
        catch (IOException)
        {
            return;  // still being written; the next tick tries again
        }

        Save(Path.Combine(_folder, SafeName(text) + ".png"));
    }

    private void Save(string path)
    {
        if (_window.Content is not FrameworkElement root || root.ActualWidth < 1 || root.ActualHeight < 1)
        {
            return;
        }

        var dpi = VisualTreeHelper.GetDpi(root);
        var width = (int)Math.Ceiling(root.ActualWidth * dpi.DpiScaleX);
        var height = (int)Math.Ceiling(root.ActualHeight * dpi.DpiScaleY);
        var bitmap = new RenderTargetBitmap(width, height, 96 * dpi.DpiScaleX, 96 * dpi.DpiScaleY, PixelFormats.Pbgra32);
        var backdrop = new DrawingVisual();  // Mica is see-through in a plain render, so give the picture a solid backdrop
        using (var context = backdrop.RenderOpen())
        {
            var brush = _window.TryFindResource("ApplicationBackgroundBrush") as Brush ?? new SolidColorBrush(Color.FromRgb(0x20, 0x20, 0x20));
            context.DrawRectangle(brush, null, new Rect(0, 0, root.ActualWidth, root.ActualHeight));
        }

        bitmap.Render(backdrop);
        bitmap.Render(root);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));
        using var stream = File.Create(path);
        encoder.Save(stream);
    }
}
