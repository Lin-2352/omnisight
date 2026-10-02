using System.Text.Json;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;

namespace OmniSight.App.Tests;

public class OptionsAndSettingsTests
{
    private static (ShellViewModel Vm, List<string> Sent) Build()
    {
        var vm = new ShellViewModel();
        var sent = new List<string>();
        vm.CommandRequested += sent.Add;
        return (vm, sent);
    }

    [Theory]
    [InlineData("memory")]
    [InlineData("speak")]
    [InlineData("search")]
    [InlineData("smart")]
    [InlineData("watch")]
    [InlineData("actions")]
    public void A_switch_sends_its_command_when_the_person_flips_it_but_not_when_python_reports_it(string name)
    {
        var (vm, sent) = Build();
        vm.Apply(new SwitchEvent(name, true));
        Assert.Empty(sent);

        switch (name)
        {
            case "memory": vm.MemoryOn = false; break;
            case "speak": vm.SpeakOn = false; break;
            case "search": vm.SearchOn = false; break;
            case "smart": vm.SmartOn = false; break;
            case "watch": vm.WatchOn = false; break;
            default: vm.ActionsOn = false; break;
        }

        Assert.Equal([BridgeCommands.Set(name, false)], sent);
    }

    [Fact]
    public void Python_pushing_a_switch_back_off_does_not_send_anything()
    {
        var (vm, sent) = Build();
        vm.Apply(new SwitchEvent("actions", true));
        vm.Apply(new SwitchEvent("actions", false));  // the person answered No to the question
        Assert.False(vm.ActionsOn);
        Assert.Empty(sent);
    }

    [Fact]
    public void Smart_query_is_available_only_while_search_is_on()
    {
        var (vm, _) = Build();
        var changed = new List<string?>();
        vm.PropertyChanged += (_, e) => changed.Add(e.PropertyName);
        Assert.False(vm.SmartEnabled);
        vm.Apply(new SwitchEvent("search", true));
        Assert.True(vm.SmartEnabled);
        Assert.Contains(nameof(ShellViewModel.SmartEnabled), changed);
        vm.Apply(new SwitchEvent("search", false));
        Assert.False(vm.SmartEnabled);
    }

    [Fact]
    public void The_run_switch_follows_python_and_updates_existing_cards()
    {
        var (vm, _) = Build();
        vm.Apply(Samples.Exchange(actions: Samples.WithRun(("bash", "ls"))));
        vm.Apply(new SwitchEvent("actions", true));
        Assert.True(vm.ActionsOn);
        Assert.True(vm.Conversation[0].ShowRun);
        vm.Apply(Samples.Exchange(actions: Samples.WithRun(("bash", "ls"))));
        Assert.True(vm.Conversation[1].ShowRun);
    }

    [Fact]
    public void Speech_availability_and_speaking_are_tracked()
    {
        var (vm, sent) = Build();
        Assert.True(vm.SpeakAvailable);
        vm.Apply(new SpeakAvailableEvent(false));
        Assert.False(vm.SpeakAvailable);
        Assert.False(vm.CanStopVoice);
        vm.Apply(new SpeakingEvent(true));
        Assert.True(vm.CanStopVoice);
        vm.StopVoice();
        Assert.Equal([BridgeCommands.StopSpeaking()], sent);
        vm.Apply(new SpeakingEvent(false));
        Assert.False(vm.CanStopVoice);
    }

    [Fact]
    public void Watching_shows_its_status_and_the_pause_button_changes_its_label()
    {
        var (vm, sent) = Build();
        Assert.False(vm.HasWatch);
        vm.Apply(new WatchEvent("Watching every 10 s - frames stay on this PC", false));
        Assert.True(vm.HasWatch);
        Assert.Equal("Pause watching", vm.PauseText);
        vm.Apply(new WatchEvent("Paused", true));
        Assert.Equal("Resume watching", vm.PauseText);
        vm.PauseWatch();
        Assert.Equal([BridgeCommands.WatchPause()], sent);
        vm.Apply(new WatchEvent("", false));
        Assert.False(vm.HasWatch);
    }

    [Theory]
    [InlineData("local_gpu", "ready", true, true, true, "Stop local node")]
    [InlineData("local_gpu", "starting", true, true, true, "Stop local node")]
    [InlineData("local_gpu", "stopped", false, true, false, "Start local node")]
    [InlineData("local_gpu", "ready", false, true, false, "Start local node")]  // a node somebody else started is not ours to stop
    [InlineData("auto", "stopped", false, true, false, "Start local node")]
    [InlineData("kaggle", "stopped", false, false, false, "Start local node")]
    [InlineData("local_cpu", "failed", false, true, false, "Start local node")]
    public void The_node_button_follows_the_node_and_the_engine(string engine, string state, bool owned, bool visible, bool running, string label)
    {
        var (vm, sent) = Build();
        vm.Apply(new NodeEvent(state, "", null, owned, engine));
        Assert.Equal((visible, running, label), (vm.NodeVisible, vm.NodeRunning, vm.NodeButtonText));
        vm.ToggleNode();
        Assert.Equal([BridgeCommands.NodeToggle()], sent);
    }

    [Fact]
    public void The_allow_commands_question_is_handed_to_the_window_and_the_answer_goes_back()
    {
        var (vm, sent) = Build();
        ConfirmEvent? asked = null;
        vm.ConfirmRequested += c => asked = c;
        vm.Apply(new ConfirmEvent("abc123", "allow_actions", "Allow running commands?", "Turn it on?"));
        Assert.Equal("abc123", asked?.Id);
        Assert.Empty(sent);
        vm.AnswerConfirm("abc123", false);
        Assert.Equal([BridgeCommands.Answer("abc123", false)], sent);
    }

    [Fact]
    public void A_question_the_window_cannot_show_is_answered_no_at_once()
    {
        var (vm, sent) = Build();  // nobody subscribed to ConfirmRequested
        vm.Apply(new ConfirmEvent("q1", "allow_actions", "t", "x"));
        Assert.Equal([BridgeCommands.Answer("q1", false)], sent);
    }

    [Fact]
    public void A_question_of_an_unknown_kind_is_answered_no_even_when_the_window_could_show_dialogs()
    {
        var (vm, sent) = Build();
        var shown = false;
        vm.ConfirmRequested += _ => shown = true;
        vm.Apply(new ConfirmEvent("q2", "format_disk", "Format?", "really?"));
        Assert.False(shown);
        Assert.Equal([BridgeCommands.Answer("q2", false)], sent);
    }

    [Fact]
    public void Settings_arrive_edit_apply_and_test()
    {
        var (vm, sent) = Build();
        vm.Apply(new SettingsInfoEvent("http://127.0.0.1:8000 (local)", "", "http://127.0.0.1:8000", "test pc", "off", "Alt+C", "C:\\logs"));
        Assert.Equal(("http://127.0.0.1:8000 (local)", "http://127.0.0.1:8000", "test pc"), (vm.ActiveEndpoint, vm.LocalUrl, vm.Capability));
        vm.OverrideUrl = "  https://a.trycloudflare.com ";
        vm.LocalUrl = " http://127.0.0.1:9000 ";
        vm.ApplySettings();
        Assert.Equal([BridgeCommands.SettingsApply("https://a.trycloudflare.com", "http://127.0.0.1:9000")], sent);
        vm.Apply(new SettingsResultEvent("Saved for this session."));
        Assert.Equal("Saved for this session.", vm.SettingsResult);
        vm.TestConnection();
        Assert.Equal("Checking…", vm.SettingsResult);
        Assert.Equal(BridgeCommands.TestConnection(), sent[^1]);
    }

    [Fact]
    public void Opening_the_options_pane_asks_python_for_the_settings()
    {
        var (vm, sent) = Build();
        vm.IsPaneOpen = true;
        Assert.Equal([BridgeCommands.SettingsGet()], sent);
        vm.IsPaneOpen = false;
        Assert.Single(sent);
    }

    [Fact]
    public void The_log_folder_is_opened_only_when_it_is_a_real_absolute_folder()
    {
        var (vm, _) = Build();
        var opened = new List<string>();
        vm.OpenFolderRequested += opened.Add;
        foreach (var bad in new[] { "", "logs", @"..\..\Windows", @"C:\definitely\not\here", "https://example.com", @"\\server\share" })
        {
            vm.Apply(new SettingsInfoEvent("", "", "", "", "", "", bad));
            Assert.False(vm.OpenLogs(), bad);
        }

        var folder = Path.GetTempPath();
        vm.Apply(new SettingsInfoEvent("", "", "", "", "", "", folder));
        Assert.True(vm.OpenLogs());
        Assert.Equal([folder], opened);
    }

    [Fact]
    public void The_new_commands_are_single_line_json()
    {
        foreach (var line in new[]
        {
            BridgeCommands.Answer("id\nwith newline", true), BridgeCommands.SettingsGet(),
            BridgeCommands.SettingsApply("a\nb", "c\rd"), BridgeCommands.TestConnection(),
        })
        {
            Assert.DoesNotContain('\n', line);
            Assert.DoesNotContain('\r', line);
            using var doc = JsonDocument.Parse(line);
            Assert.True(doc.RootElement.TryGetProperty("cmd", out _));
        }
    }

    [Fact]
    public void The_new_events_parse()
    {
        Assert.Equal(
            new ConfirmEvent("i", "allow_actions", "T", "X"),
            BridgeProtocol.ParseEvent("""{"event":"confirm","id":"i","kind":"allow_actions","title":"T","text":"X"}"""));
        Assert.Equal(
            new SettingsInfoEvent("e", "o", "l", "c", "f", "h", "d"),
            BridgeProtocol.ParseEvent(
                """{"event":"settings_info","endpoint":"e","override_url":"o","local_url":"l","capability":"c","fallback_url":"f","hotkeys":"h","log_dir":"d"}"""));
        Assert.Equal(new SettingsResultEvent("ok"), BridgeProtocol.ParseEvent("""{"event":"settings_result","text":"ok"}"""));
    }
}
