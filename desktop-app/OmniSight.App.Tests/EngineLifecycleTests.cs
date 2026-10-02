using OmniSight.Core.Hosting;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;

namespace OmniSight.App.Tests;

/// <summary>Starting the engine can fail in several ways; each must end in a clear message, never a hang or a raw exception.</summary>
public sealed class EngineLifecycleTests : IDisposable
{
    private readonly string _folder = Path.Combine(Path.GetTempPath(), "omnisight-host-" + Guid.NewGuid().ToString("N"));

    public EngineLifecycleTests()
    {
        Directory.CreateDirectory(Path.Combine(_folder, "desktop-client"));
        File.WriteAllText(Path.Combine(_folder, "desktop-client", "main.py"), "# stand-in");
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_folder, true);
        }
        catch (IOException)
        {
            // a stand-in process may still be shutting down; the folder is in the temp directory anyway
        }
    }

    private string FakePython(string body)
    {
        var path = Path.Combine(_folder, "fake-python.cmd");
        File.WriteAllText(path, "@echo off\r\n" + body + "\r\n");
        return path;
    }

    [Fact]
    public async Task A_program_that_exits_with_zero_at_once_means_omnisight_is_already_running()
    {
        // what the Python client does when another OmniSight holds the single-instance lock: exit 0 before reading anything
        var python = FakePython("echo OmniSight is already running. 1>&2\r\nexit /b 0");
        for (var attempt = 0; attempt < 5; attempt++)
        {
            var started = DateTime.UtcNow;
            await Assert.ThrowsAsync<AlreadyRunningException>(() => PythonHost.StartAsync(new PythonHostOptions(_folder, python, null, null, TimeSpan.FromSeconds(20))));
            Assert.True(DateTime.UtcNow - started < TimeSpan.FromSeconds(15), "it must not wait for a timeout");
        }
    }

    [Fact]
    public async Task A_crash_is_reported_with_its_exit_code_and_what_it_printed()
    {
        var python = FakePython("echo something broke 1>&2\r\nexit /b 3");
        var ex = await Assert.ThrowsAsync<PythonHostException>(() => PythonHost.StartAsync(new PythonHostOptions(_folder, python, null, null, TimeSpan.FromSeconds(20))));
        Assert.IsNotType<AlreadyRunningException>(ex);
        Assert.Contains("exit 3", ex.Message);
        Assert.Contains("something broke", ex.Message);
    }

    [Fact]
    public async Task A_program_that_never_announces_itself_times_out_and_is_stopped()
    {
        var python = FakePython("ping -n 40 127.0.0.1 >nul");
        var started = DateTime.UtcNow;
        var ex = await Assert.ThrowsAsync<PythonHostException>(() => PythonHost.StartAsync(new PythonHostOptions(_folder, python, null, null, TimeSpan.FromSeconds(2))));
        Assert.Contains("in time", ex.Message);
        Assert.True(DateTime.UtcNow - started < TimeSpan.FromSeconds(20));
    }

    [Fact]
    public void The_goodbye_event_closes_the_window_and_is_logged_not_shown_as_a_crash()
    {
        var vm = new ShellViewModel();
        var quits = 0;
        vm.EngineQuit += () => quits++;
        vm.Apply(new ByeEvent());
        Assert.Equal(1, quits);
        Assert.Contains("engine is quitting", vm.Log);
        Assert.Equal(ConnectionState.Starting, vm.Connection);  // it did not become "Disconnected" or "Failed"
    }

    [Fact]
    public void The_trays_settings_opens_the_options_pane_and_loads_the_settings()
    {
        var vm = new ShellViewModel();
        var sent = new List<string>();
        vm.CommandRequested += sent.Add;
        vm.Apply(new OpenOptionsEvent());
        Assert.True(vm.IsPaneOpen);
        Assert.Equal([BridgeCommands.SettingsGet()], sent);
    }

    [Fact]
    public void The_new_events_parse()
    {
        Assert.IsType<ByeEvent>(BridgeProtocol.ParseEvent("""{"event":"bye"}"""));
        Assert.IsType<OpenOptionsEvent>(BridgeProtocol.ParseEvent("""{"event":"open_options"}"""));
    }
}
