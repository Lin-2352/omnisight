using OmniSight.Core.Logging;

namespace OmniSight.App.Tests;

[Collection("AppLog")]
public sealed class AppLogTests : IDisposable
{
    private readonly string _folder = Path.Combine(Path.GetTempPath(), "omnisight-log-" + Guid.NewGuid().ToString("N"));

    public AppLogTests() => AppLog.PathOverride = Path.Combine(_folder, "logs", "app.log");

    public void Dispose()
    {
        AppLog.PathOverride = null;
        if (Directory.Exists(_folder))
        {
            Directory.Delete(_folder, true);
        }
    }

    [Fact]
    public void Lines_are_timestamped_and_levelled_and_the_folder_is_created()
    {
        AppLog.Info("started");
        AppLog.Warning("careful");
        AppLog.Error("boom", new InvalidOperationException("bad thing"));
        var text = File.ReadAllText(AppLog.FilePath);
        Assert.Matches(@"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO  started", text);
        Assert.Contains("WARN  careful", text);
        Assert.Contains("ERROR boom: InvalidOperationException: bad thing", text);
    }

    [Fact]
    public void A_very_long_message_is_cut()
    {
        AppLog.Info(new string('x', 10_000));
        Assert.True(new FileInfo(AppLog.FilePath).Length < 3000);
    }

    [Fact]
    public void The_file_rotates_instead_of_growing_forever()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(AppLog.FilePath)!);
        File.WriteAllText(AppLog.FilePath, new string('a', (int)AppLog.MaxBytes + 10));
        AppLog.Info("after rotation");
        Assert.True(File.Exists(AppLog.FilePath + ".1"));
        Assert.Contains("after rotation", File.ReadAllText(AppLog.FilePath));
        Assert.True(new FileInfo(AppLog.FilePath).Length < 1000);
    }

    [Fact]
    public void Logging_never_throws_even_when_the_path_is_impossible()
    {
        AppLog.PathOverride = Path.Combine(Path.GetTempPath(), "bad\0name", "app.log");
        var ex = Record.Exception(() => AppLog.Error("still fine", new Exception("x")));
        Assert.Null(ex);
    }

    [Fact]
    public async Task Concurrent_writers_do_not_lose_or_break_lines()
    {
        await Task.WhenAll(Enumerable.Range(0, 20).Select(i => Task.Run(() =>
        {
            for (var j = 0; j < 25; j++)
            {
                AppLog.Info($"writer {i} line {j}");
            }
        })));
        var lines = File.ReadAllLines(AppLog.FilePath);
        Assert.Equal(500, lines.Length);
        Assert.All(lines, line => Assert.Matches(@"INFO  writer \d+ line \d+$", line));
    }
}
