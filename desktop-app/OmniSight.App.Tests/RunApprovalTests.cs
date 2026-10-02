using System.Text.Json;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;

namespace OmniSight.App.Tests;

public class RunApprovalTests
{
    private static RunOpenEvent Opened(string? refused = null, string[]? warnings = null, string id = "abc") =>
        new(id, "Get-Date", "powershell", "Windows PowerShell", "banner text", @"C:\work", "RUN", 60, 5, 300, refused, warnings ?? []);

    private static (RunApprovalViewModel Vm, List<string> Sent) Build(RunOpenEvent? opened = null)
    {
        var sent = new List<string>();
        return (new RunApprovalViewModel(opened ?? Opened(), sent.Add), sent);
    }

    [Fact]
    public void It_starts_with_what_python_opened_it_with()
    {
        var (vm, sent) = Build();
        Assert.Equal(("abc", "Get-Date", "Windows PowerShell", @"C:\work", "RUN"), (vm.Id, vm.Command, vm.ShellName, vm.Cwd, vm.ConfirmWord));
        Assert.Equal(60, vm.TimeoutSeconds);
        Assert.Equal("Cancel", vm.CloseText);
        Assert.False(vm.CanRun);
        Assert.False(vm.HasVerdict);
        Assert.Empty(sent);  // opening it sends nothing
    }

    [Theory]
    [InlineData("RUN", true)]
    [InlineData("", false)]
    [InlineData("run", false)]
    [InlineData("Run", false)]
    [InlineData("RUN ", false)]
    [InlineData(" RUN", false)]
    [InlineData("RUNN", false)]
    [InlineData("RU", false)]
    [InlineData("ＲＵＮ", false)]
    [InlineData("R\u200bUN", false)]
    public void Run_is_enabled_only_for_the_exact_word(string typed, bool allowed)
    {
        var (vm, _) = Build();
        vm.Typed = typed;
        Assert.Equal(allowed, vm.CanRun);
        Assert.Equal(allowed, vm.Execute());
    }

    [Fact]
    public void A_refused_command_can_never_be_run_even_with_the_word()
    {
        var (vm, sent) = Build(Opened(refused: "'shutdown' shuts the computer down."));
        vm.Typed = "RUN";
        Assert.False(vm.CanRun);
        Assert.False(vm.Execute());
        Assert.Empty(sent);
        Assert.True(vm.VerdictIsRefusal);
        Assert.Equal("Refused: 'shutdown' shuts the computer down. OmniSight will not run this command, even if you approve it.", vm.VerdictText);
    }

    [Fact]
    public void Warnings_are_shown_but_do_not_block()
    {
        var (vm, _) = Build(Opened(warnings: ["downloads and runs code", "changes system files"]));
        vm.Typed = "RUN";
        Assert.True(vm.CanRun);
        Assert.False(vm.VerdictIsRefusal);
        Assert.Equal("Take a second look: this command downloads and runs code; changes system files.", vm.VerdictText);
    }

    [Fact]
    public void Execute_sends_the_word_folder_and_timeout_but_never_any_command_text()
    {
        var (vm, sent) = Build();
        vm.Typed = "RUN";
        vm.Cwd = @"D:\project";
        sent.Clear();  // the folder edit sent a check
        vm.TimeoutSeconds = 90;
        Assert.True(vm.Execute());
        using var doc = JsonDocument.Parse(Assert.Single(sent));
        var root = doc.RootElement;
        Assert.Equal("run.execute", root.GetProperty("cmd").GetString());
        Assert.Equal(("abc", "RUN", @"D:\project", 90d), (root.GetProperty("id").GetString(), root.GetProperty("typed").GetString(), root.GetProperty("cwd").GetString(), root.GetProperty("timeout_s").GetDouble()));
        Assert.False(root.TryGetProperty("command", out _), "the app must not send the command back");
        Assert.DoesNotContain("Get-Date", sent[0]);
    }

    [Fact]
    public void Run_is_disabled_while_running_and_the_inputs_are_locked()
    {
        var (vm, _) = Build();
        vm.Typed = "RUN";
        vm.ApplyState(new RunStateEvent("abc", "running", "Running...", "", false, null, false, false, 0));
        Assert.True(vm.IsRunning);
        Assert.False(vm.CanRun);
        Assert.False(vm.CanEdit);
        Assert.Equal("Stop", vm.CloseText);
        Assert.Equal("Running...", vm.Status);
        Assert.True(vm.HasOutput);
    }

    [Fact]
    public void A_finished_run_shows_its_output_clears_the_word_and_offers_close()
    {
        var (vm, _) = Build();
        vm.Typed = "RUN";
        vm.ApplyState(new RunStateEvent("abc", "running", "", "", false, null, false, false, 0));
        vm.ApplyState(new RunStateEvent("abc", "finished", "", "hello\r\nworld", false, 0, false, false, 1.234));
        Assert.Equal("hello\r\nworld", vm.Output);
        Assert.Equal("Finished with exit code 0 in 1.2 s.", vm.Status);
        Assert.False(vm.StatusIsError);
        Assert.Equal("", vm.Typed);  // one approval, one run
        Assert.False(vm.CanRun);
        Assert.Equal("Close", vm.CloseText);
        Assert.False(vm.IsRunning);
        Assert.True(vm.CanEdit);
    }

    [Fact]
    public void A_second_run_needs_the_word_typed_again()
    {
        var (vm, sent) = Build();
        vm.Typed = "RUN";
        vm.Execute();
        vm.ApplyState(new RunStateEvent("abc", "finished", "", "x", false, 0, false, false, 0.1));
        sent.Clear();
        Assert.False(vm.Execute());
        Assert.Empty(sent);
        vm.Typed = "RUN";
        Assert.True(vm.Execute());
    }

    [Fact]
    public void Stopped_and_timed_out_runs_are_described_honestly()
    {
        var (vm, _) = Build();
        vm.TimeoutSeconds = 30;
        vm.ApplyState(new RunStateEvent("abc", "finished", "", "", false, null, true, false, 30));
        Assert.Equal("Stopped: it did not finish within 30 s. Everything it started was ended.", vm.Status);
        vm.ApplyState(new RunStateEvent("abc", "finished", "", "", false, null, false, true, 2));
        Assert.Equal("Stopped by you. Everything it started was ended.", vm.Status);
        vm.ApplyState(new RunStateEvent("abc", "finished", "", "boom", false, 3, false, false, 0.5));
        Assert.Equal("Finished with exit code 3 in 0.5 s.", vm.Status);
    }

    [Fact]
    public void Truncated_output_says_so_and_huge_output_is_cut()
    {
        var (vm, _) = Build();
        vm.ApplyState(new RunStateEvent("abc", "finished", "", "text", true, 0, false, false, 0));
        Assert.EndsWith("[output cut at 64 KB]", vm.Output);
        vm.ApplyState(new RunStateEvent("abc", "finished", "", new string('x', 500_000), false, 0, false, false, 0));
        Assert.True(vm.Output.Length <= RunApprovalViewModel.MaxOutputChars);
    }

    [Fact]
    public void A_refusal_at_run_time_is_an_error_and_clears_the_word()
    {
        var (vm, _) = Build();
        vm.Typed = "RUN";
        vm.ApplyState(new RunStateEvent("abc", "refused", "The command changed.", "", false, null, false, false, 0));
        Assert.Equal(("The command changed.", true, ""), (vm.Status, vm.StatusIsError, vm.Typed));
        Assert.Equal("Close", vm.CloseText);
    }

    [Fact]
    public void Escape_stops_a_running_command_and_otherwise_closes()
    {
        var (vm, sent) = Build();
        vm.Escape();
        Assert.Equal([BridgeCommands.RunClose("abc")], sent);
        sent.Clear();
        vm.ApplyState(new RunStateEvent("abc", "running", "", "", false, null, false, false, 0));
        vm.Escape();
        Assert.Equal([BridgeCommands.RunCancel("abc")], sent);
    }

    [Fact]
    public void The_cancel_button_always_asks_python_which_decides_between_stop_and_close()
    {
        var (vm, sent) = Build();
        vm.CancelOrClose();
        Assert.Equal([BridgeCommands.RunCancel("abc")], sent);
    }

    [Fact]
    public void Changing_the_folder_asks_python_to_judge_the_command_again_but_not_while_running_or_from_python()
    {
        var (vm, sent) = Build();
        vm.Cwd = @"C:\Users\me";
        Assert.Equal([BridgeCommands.RunCheck("abc", @"C:\Users\me")], sent);
        sent.Clear();
        vm.ApplyState(new RunStateEvent("abc", "running", "", "", false, null, false, false, 0));
        vm.Cwd = @"C:\elsewhere";
        Assert.Empty(sent);
    }

    [Fact]
    public void A_new_verdict_can_refuse_and_then_allow_again()
    {
        var (vm, _) = Build();
        vm.Typed = "RUN";
        Assert.True(vm.CanRun);
        vm.ApplyVerdict("It would delete everything in a protected folder.", []);
        Assert.False(vm.CanRun);
        Assert.True(vm.HasVerdict && vm.VerdictIsRefusal);
        vm.ApplyVerdict(null, []);
        Assert.True(vm.CanRun);
        Assert.False(vm.HasVerdict);
    }

    [Fact]
    public void The_timeout_is_kept_inside_the_allowed_range()
    {
        Assert.Equal(300, new RunApprovalViewModel(Opened() with { TimeoutS = 9999 }, _ => { }).TimeoutSeconds);
        Assert.Equal(5, new RunApprovalViewModel(Opened() with { TimeoutS = 0 }, _ => { }).TimeoutSeconds);
    }

    // -- inside the shell -----------------------------------------------------------------------------------

    private static (ShellViewModel Vm, List<string> Sent) Shell()
    {
        var vm = new ShellViewModel();
        var sent = new List<string>();
        vm.CommandRequested += sent.Add;
        return (vm, sent);
    }

    [Fact]
    public void The_shell_opens_updates_and_closes_an_approval_and_locks_the_rest_while_it_is_open()
    {
        var (vm, sent) = Shell();
        RunApprovalViewModel? announced = null;
        vm.RunApprovalOpened += a => announced = a;
        Assert.True(vm.NoRunApproval);
        vm.Apply(Opened());
        Assert.NotNull(vm.RunApproval);
        Assert.Same(vm.RunApproval, announced);
        Assert.True(vm.HasRunApproval);
        Assert.False(vm.NoRunApproval);
        vm.Apply(new RunVerdictEvent("abc", "refused now", []));
        Assert.Equal("refused now", vm.RunApproval!.Refused);
        vm.Apply(new RunStateEvent("abc", "running", "", "", false, null, false, false, 0));
        Assert.True(vm.RunApproval.IsRunning);
        vm.Apply(new RunClosedEvent("abc"));
        Assert.Null(vm.RunApproval);
        Assert.True(vm.NoRunApproval);
        Assert.Empty(sent);
    }

    [Fact]
    public void Events_for_another_approval_are_ignored()
    {
        var (vm, _) = Shell();
        vm.Apply(Opened(id: "mine"));
        vm.Apply(new RunVerdictEvent("other", "refused", []));
        vm.Apply(new RunStateEvent("other", "running", "", "", false, null, false, false, 0));
        vm.Apply(new RunClosedEvent("other"));
        Assert.NotNull(vm.RunApproval);
        Assert.Null(vm.RunApproval!.Refused);
        Assert.False(vm.RunApproval.IsRunning);
    }

    [Fact]
    public void Events_with_no_approval_open_are_ignored()
    {
        var (vm, _) = Shell();
        vm.Apply(new RunVerdictEvent("abc", null, []));
        vm.Apply(new RunStateEvent("abc", "finished", "", "x", false, 0, false, false, 0));
        vm.Apply(new RunClosedEvent("abc"));
        Assert.Null(vm.RunApproval);
    }

    // -- protocol --------------------------------------------------------------------------------------------

    [Fact]
    public void The_run_events_parse()
    {
        var open = Assert.IsType<RunOpenEvent>(BridgeProtocol.ParseEvent(
            """{"event":"run_open","id":"i","command":"Get-Date","shell":"powershell","shell_name":"Windows PowerShell","banner":"b","cwd":"C:\\x","confirm_word":"RUN","timeout_s":60,"min_timeout_s":5,"max_timeout_s":300,"refused":null,"warnings":["w1","w2"]}"""));
        Assert.Equal(("i", "Get-Date", "powershell", "RUN", 60d, 5d, 300d), (open.Id, open.Command, open.Shell, open.ConfirmWord, open.TimeoutS, open.MinTimeoutS, open.MaxTimeoutS));
        Assert.Null(open.Refused);
        Assert.Equal(["w1", "w2"], open.Warnings);
        var refused = Assert.IsType<RunOpenEvent>(BridgeProtocol.ParseEvent("""{"event":"run_open","id":"i","command":"x","refused":"no way"}"""));
        Assert.Equal("no way", refused.Refused);
        Assert.Equal("RUN", refused.ConfirmWord);  // a missing word falls back to the safe default
        var verdict = Assert.IsType<RunVerdictEvent>(BridgeProtocol.ParseEvent("""{"event":"run_verdict","id":"i","refused":null,"warnings":[]}"""));
        Assert.Equal("i", verdict.Id);
        var state = Assert.IsType<RunStateEvent>(BridgeProtocol.ParseEvent(
            """{"event":"run_state","id":"i","state":"finished","output":"hi","truncated":true,"exit_code":2,"timed_out":false,"cancelled":true,"duration_s":1.5}"""));
        Assert.Equal(("finished", "hi", true, 2, false, true, 1.5), (state.State, state.Output, state.Truncated, state.ExitCode, state.TimedOut, state.Cancelled, state.DurationS));
        Assert.Equal(new RunClosedEvent("i"), BridgeProtocol.ParseEvent("""{"event":"run_closed","id":"i"}"""));
    }

    [Fact]
    public void Odd_run_events_never_throw()
    {
        foreach (var line in new[]
        {
            """{"event":"run_open"}""", """{"event":"run_open","warnings":"nope","refused":5,"timeout_s":"x"}""",
            """{"event":"run_state","exit_code":"x","truncated":"yes"}""", """{"event":"run_verdict","warnings":[1,2,{"a":1}]}""",
        })
        {
            Assert.NotNull(BridgeProtocol.ParseEvent(line));
        }
    }

    [Fact]
    public void The_run_commands_are_single_line_json()
    {
        foreach (var line in new[]
        {
            BridgeCommands.RunCheck("i\nd", "C:\\a\nb"), BridgeCommands.RunExecute("i", "RUN", "C:\\a\rb", 60), BridgeCommands.RunCancel("i"), BridgeCommands.RunClose("i"),
        })
        {
            Assert.DoesNotContain('\n', line);
            Assert.DoesNotContain('\r', line);
            using var doc = JsonDocument.Parse(line);
            Assert.StartsWith("run.", doc.RootElement.GetProperty("cmd").GetString());
        }
    }
}
