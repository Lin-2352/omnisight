using OmniSight.Core.Bridge;
using OmniSight.Core.Hosting;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;

namespace OmniSight.App.Tests;

public class AnnouncementTests
{
    [Theory]
    [InlineData("""OMNISIGHT_BRIDGE {"port":5000,"protocol":1}""", 5000)]
    [InlineData("""OMNISIGHT_BRIDGE {"protocol":1,"port":65535}""", 65535)]
    [InlineData("""OMNISIGHT_BRIDGE {"port":1,"protocol":1,"extra":true}""", 1)]
    public void A_valid_announcement_yields_the_port(string line, int expected)
    {
        Assert.True(PythonHost.TryParseAnnouncement(line, out var port));
        Assert.Equal(expected, port);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("hello world")]
    [InlineData("OMNISIGHT_BRIDGE")]
    [InlineData("OMNISIGHT_BRIDGE not json")]
    [InlineData("OMNISIGHT_BRIDGE []")]
    [InlineData("""OMNISIGHT_BRIDGE {"port":0,"protocol":1}""")]
    [InlineData("""OMNISIGHT_BRIDGE {"port":70000,"protocol":1}""")]
    [InlineData("""OMNISIGHT_BRIDGE {"port":-5,"protocol":1}""")]
    [InlineData("""OMNISIGHT_BRIDGE {"port":"5000","protocol":1}""")]
    [InlineData("""OMNISIGHT_BRIDGE {"port":5000}""")]
    [InlineData("""OMNISIGHT_BRIDGE {"port":5000,"protocol":2}""")]
    [InlineData("""  OMNISIGHT_BRIDGE {"port":5000,"protocol":1}""")]
    public void Anything_else_is_not_an_announcement(string? line)
    {
        Assert.False(PythonHost.TryParseAnnouncement(line, out var port));
        Assert.Equal(0, port);
    }

    [Fact]
    public void Tokens_are_long_random_and_different_every_time()
    {
        var tokens = Enumerable.Range(0, 50).Select(_ => PythonHost.NewToken()).ToList();
        Assert.Equal(50, tokens.Distinct().Count());
        Assert.All(tokens, t => Assert.Equal(48, t.Length));
        Assert.All(tokens, t => Assert.Matches("^[0-9a-f]+$", t));
    }
}

public class LocatorTests
{
    private static string TempRepo()
    {
        var root = Path.Combine(Path.GetTempPath(), "omnisight-locator-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "desktop-client"));
        File.WriteAllText(Path.Combine(root, "desktop-client", "main.py"), "");
        return root;
    }

    [Fact]
    public void The_repo_is_found_by_walking_up_from_a_nested_folder()
    {
        var root = TempRepo();
        try
        {
            var nested = Path.Combine(root, "desktop-app", "OmniSight.App", "bin", "Debug");
            Directory.CreateDirectory(nested);
            Assert.Equal(root, Locator.FindRepoRoot(nested, _ => null));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }

    [Fact]
    public void The_environment_variable_wins_but_must_point_at_a_real_repo()
    {
        var root = TempRepo();
        try
        {
            Assert.Equal(root, Locator.FindRepoRoot(Path.GetTempPath(), name => name == Locator.RootVariable ? root : null));
            Assert.Null(Locator.FindRepoRoot(root, name => name == Locator.RootVariable ? Path.Combine(root, "nope") : null));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }

    [Fact]
    public void No_repo_means_null()
    {
        var empty = Path.Combine(Path.GetTempPath(), "omnisight-empty-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(empty);
        try
        {
            Assert.Null(Locator.FindRepoRoot(empty, _ => null));
        }
        finally
        {
            Directory.Delete(empty, true);
        }
    }

    [Fact]
    public void Python_comes_from_the_variable_then_the_venv_then_the_path()
    {
        var root = TempRepo();
        try
        {
            Assert.Equal("python", Locator.FindPython(root, _ => null));
            Assert.Equal(@"C:\custom\python.exe", Locator.FindPython(root, name => name == Locator.PythonVariable ? @"C:\custom\python.exe" : null));
            var venv = Path.Combine(root, ".venv", "Scripts");
            Directory.CreateDirectory(venv);
            File.WriteAllText(Path.Combine(venv, "python.exe"), "");
            Assert.Equal(Path.Combine(venv, "python.exe"), Locator.FindPython(root, _ => null));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }
}

public class PythonHostProcessTests
{
    private static string? RepoRoot() => Locator.FindRepoRoot(AppContext.BaseDirectory);

    [Fact]
    public async Task A_missing_main_py_fails_cleanly()
    {
        var ex = await Assert.ThrowsAsync<PythonHostException>(() =>
            PythonHost.StartAsync(new PythonHostOptions(Path.GetTempPath(), "python")));
        Assert.Contains("main.py", ex.Message);
    }

    [Fact]
    public async Task A_python_that_cannot_start_fails_cleanly()
    {
        var root = RepoRoot();
        Skip.If(root is null, "not inside the repository");
        var ex = await Assert.ThrowsAsync<PythonHostException>(() =>
            PythonHost.StartAsync(new PythonHostOptions(root!, @"C:\definitely\not\python.exe")));
        Assert.Contains("could not start Python", ex.Message);
    }

    [SkippableFact]
    public async Task The_real_python_client_starts_in_bridge_mode_talks_the_protocol_and_exits_with_the_app()
    {
        var root = RepoRoot();
        Skip.If(root is null, "not inside the repository");
        var python = Locator.FindPython(root!);
        Skip.IfNot(File.Exists(python), "no .venv in this checkout");

        PythonHost host;
        try
        {
            host = await PythonHost.StartAsync(new PythonHostOptions(
                root!, python, ["--no-hotkeys", "--backend", "local"],
                new Dictionary<string, string> { ["QT_QPA_PLATFORM"] = "offscreen", ["FALLBACK_API_URL"] = "off" },
                TimeSpan.FromSeconds(60)));
        }
        catch (PythonHostException ex) when (ex.Message.Contains("exit 0"))
        {
            Skip.If(true, "another OmniSight client holds the single-instance lock");
            return;
        }

        await using (host)
        {
            await using var client = new BridgeClient("127.0.0.1", host.Port, host.Token, Environment.ProcessId);
            var events = new List<BridgeEvent>();
            var pong = new TaskCompletionSource();
            var error = new TaskCompletionSource();
            client.EventReceived += e =>
            {
                lock (events)
                {
                    events.Add(e);
                }

                if (e is PongEvent)
                {
                    pong.TrySetResult();
                }

                if (e is ErrorEvent)
                {
                    error.TrySetResult();
                }
            };
            await client.ConnectAsync(TimeSpan.FromSeconds(20));
            await client.SendAsync(BridgeCommands.Ping());
            await pong.Task.WaitAsync(TimeSpan.FromSeconds(10));
            await client.SendAsync("""{"cmd":"set","name":"memory","on":"yes"}""");
            await error.Task.WaitAsync(TimeSpan.FromSeconds(10));
            await Task.Delay(500);
            lock (events)
            {
                Assert.Contains(events, e => e is EngineEvent { Key: var key } && key.StartsWith("local", StringComparison.Ordinal));
                Assert.Contains(events, e => e is SwitchEvent { Name: "memory" });
            }

            await client.DisposeAsync();
            var exited = SpinWait.SpinUntil(() => host.HasExited, TimeSpan.FromSeconds(20));
            Assert.True(exited, "the Python client must exit when the app disconnects");
        }
    }
}

public class ShellViewModelTests
{
    [Fact]
    public void It_starts_in_the_starting_state()
    {
        var vm = new ShellViewModel();
        Assert.Equal(ConnectionState.Starting, vm.Connection);
        Assert.False(string.IsNullOrEmpty(vm.ConnectionText));
    }

    [Fact]
    public void Events_update_what_the_window_shows()
    {
        var vm = new ShellViewModel();
        vm.Apply(new HelloEvent(1));
        vm.Apply(new EngineEvent("local_gpu"));
        vm.Apply(new StateEvent("analyzing", "Analyzing the screen…"));
        vm.Apply(new NodeEvent("ready", "", "GPU: RTX", true, "local_gpu"));
        Assert.Equal(ConnectionState.Connected, vm.Connection);
        Assert.Equal("local_gpu", vm.EngineKey);
        Assert.Equal("Analyzing the screen…", vm.StatusText);
        Assert.Equal("ready: GPU: RTX", vm.NodeText);
        vm.Apply(new NodeEvent("stopped", "", null, false, "auto"));
        Assert.Equal("stopped", vm.NodeText);
    }

    [Fact]
    public void Notices_and_errors_are_kept_with_their_severity()
    {
        var vm = new ShellViewModel();
        vm.Apply(new NoticeEvent("careful", false));
        Assert.Equal(("careful", false), (vm.NoticeText, vm.NoticeIsError));
        vm.Apply(new ErrorEvent("bad"));
        Assert.Equal(("bad", true), (vm.NoticeText, vm.NoticeIsError));
    }

    [Fact]
    public void Failure_and_disconnect_are_distinct_states()
    {
        var vm = new ShellViewModel();
        vm.SetFailed("Python was not found");
        Assert.Equal(ConnectionState.Failed, vm.Connection);
        vm.SetDisconnected("Connection lost");
        Assert.Equal((ConnectionState.Disconnected, "Connection lost"), (vm.Connection, vm.ConnectionText));
        vm.SetStarting("Starting…");
        Assert.Equal(ConnectionState.Starting, vm.Connection);
    }

    [Fact]
    public void The_log_is_capped_and_unknown_or_malformed_events_are_logged_not_fatal()
    {
        var vm = new ShellViewModel();
        for (var i = 0; i < ShellViewModel.MaxLogLines + 50; i++)
        {
            vm.Apply(new ProgressEvent($"step {i}"));
        }

        Assert.Equal(ShellViewModel.MaxLogLines, vm.Log.Count);
        Assert.Equal($"progress: step {ShellViewModel.MaxLogLines + 49}", vm.Log[^1]);
        vm.Apply(new UnknownEvent("from_the_future"));
        vm.Apply(new MalformedEvent("not JSON"));
        Assert.Contains("unknown event from_the_future", vm.Log);
        Assert.Contains("malformed line (not JSON)", vm.Log);
    }

    [Fact]
    public void Every_event_kind_has_a_readable_log_line()
    {
        var vm = new ShellViewModel();
        BridgeEvent[] all =
        [
            new HelloEvent(1), new StateEvent("idle", ""), new NodeEvent("ready", "", null, false, "auto"), new NoticeEvent("n", false),
            new ProgressEvent("p"), new EngineEvent("auto"), new SwitchEvent("memory", true), new SpeakAvailableEvent(true), new SpeakingEvent(false),
            new WatchEvent("", false), new WatchEvent("Watching", true), new ClearEvent(), new ShowEvent(), new PongEvent(), new ErrorEvent("e"),
            new UnknownEvent("x"), new MalformedEvent("m"),
        ];
        foreach (var evt in all)
        {
            vm.Apply(evt);
        }

        Assert.Equal(all.Length, vm.Log.Count);
        Assert.All(vm.Log, line => Assert.DoesNotContain("Event", line));
        Assert.Contains("pong", vm.Log);
        Assert.Contains("speech available", vm.Log);
        Assert.Contains("watch Watching (paused)", vm.Log);
    }
}
