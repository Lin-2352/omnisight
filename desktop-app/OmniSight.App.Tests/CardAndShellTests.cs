using System.Text.Json;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;

namespace OmniSight.App.Tests;

internal static class Samples
{
    public static ExchangeEvent Exchange(
        string question = "why?",
        string origin = "window",
        string summary = "It crashes.",
        string? transcript = null,
        double? confidence = 0.9,
        string finish = "stop",
        IReadOnlyList<AnswerSource>? sources = null,
        IReadOnlyList<AnswerSegment>? segments = null,
        ExchangeActions? actions = null,
        string searched = "",
        string note = "",
        string tier = "local",
        double ttftMs = 1234) =>
        new(
            origin,
            question,
            searched,
            note,
            new AnswerResponse("Qwen2-VL-2B", summary, "md", transcript, confidence, finish, sources ?? []),
            new AnswerMetrics(tier, ttftMs),
            segments ?? [new AnswerSegment("prose", "It runs past the end.", "text"), new AnswerSegment("code", "print(1)", "python")],
            actions ?? new ExchangeActions("print(1)", "", []));

    public static ExchangeActions WithRun(params (string Language, string Command)[] runs) =>
        new("fix()", runs.Length > 0 ? runs[0].Command : "", runs.Select(r => new RunCommand(r.Language, r.Command)).ToList());
}

public class ExchangeCardTests
{
    [Fact]
    public void The_heading_prefers_what_was_said_then_the_question_then_a_generic_title()
    {
        Assert.Equal("You said: “why does it crash”", ExchangeCard.From(Samples.Exchange(transcript: "why does it crash"), false).Heading);
        Assert.Equal("why?", ExchangeCard.From(Samples.Exchange(), false).Heading);
        Assert.Equal("Screen capture", ExchangeCard.From(Samples.Exchange(question: ""), false).Heading);
        Assert.Equal("Screen capture", ExchangeCard.From(Samples.Exchange(question: "", transcript: ""), false).Heading);
    }

    [Fact]
    public void Prose_and_code_segments_become_blocks_in_order_and_unknown_kinds_are_ignored()
    {
        var card = ExchangeCard.From(
            Samples.Exchange(segments:
            [
                new AnswerSegment("prose", "First **bold** text", "text"),
                new AnswerSegment("code", "x = 1", "python"),
                new AnswerSegment("mystery", "ignored", "text"),
                new AnswerSegment("prose", "   ", "text"),
                new AnswerSegment("prose", "Last", "text"),
            ]),
            false);
        Assert.Collection(card.Blocks, b => Assert.IsType<ProseBlock>(b), b => Assert.Equal(new CodeCardBlock("python", "x = 1"), b), b => Assert.IsType<ProseBlock>(b));
        Assert.True(card.HasBlocks);
    }

    [Fact]
    public void A_summary_only_answer_has_no_blocks()
    {
        Assert.False(ExchangeCard.From(Samples.Exchange(segments: []), false).HasBlocks);
    }

    [Fact]
    public void Copy_actions_come_from_the_actions_python_sent()
    {
        var card = ExchangeCard.From(Samples.Exchange(actions: new ExchangeActions("fix()", "ls", [])), false);
        Assert.True(card.HasCopyFix && card.HasCopyCommand);
        Assert.Equal(("fix()", "ls"), (card.CopyFix, card.CopyCommand));
        var none = ExchangeCard.From(Samples.Exchange(actions: new ExchangeActions("", "", [])), false);
        Assert.False(none.HasCopyFix || none.HasCopyCommand);
    }

    [Fact]
    public void One_command_is_run_dots_and_several_are_numbered_with_distinct_automation_names()
    {
        var one = ExchangeCard.From(Samples.Exchange(actions: Samples.WithRun(("powershell", "Get-Date"))), true);
        Assert.Equal(["Run..."], one.RunButtons.Select(b => b.Label));
        Assert.Equal(["Run command 1"], one.RunButtons.Select(b => b.AutomationName));
        var two = ExchangeCard.From(Samples.Exchange(actions: Samples.WithRun(("powershell", "a"), ("bash", "b"))), true);
        Assert.Equal(["Run 1...", "Run 2..."], two.RunButtons.Select(b => b.Label));
        Assert.Equal(["Run command 1", "Run command 2"], two.RunButtons.Select(b => b.AutomationName));
    }

    [Theory]
    [InlineData(true, "window", true)]
    [InlineData(false, "window", false)]
    [InlineData(true, "watch", false)]
    [InlineData(false, "watch", false)]
    public void Run_buttons_show_only_when_the_switch_is_on_and_the_card_is_not_a_watch_alert(bool enabled, string origin, bool shown)
    {
        var card = ExchangeCard.From(Samples.Exchange(origin: origin, actions: Samples.WithRun(("bash", "ls"))), enabled);
        Assert.Equal(shown, card.ShowRun);
        Assert.Equal(origin == "watch", card.IsWatch);
    }

    [Fact]
    public void Unsafe_source_links_keep_their_title_but_lose_their_address()
    {
        var card = ExchangeCard.From(
            Samples.Exchange(
                sources:
                [
                    new AnswerSource("Docs", "https://docs.python.org/3/"),
                    new AnswerSource("Local", "http://localhost:8000/admin"),
                    new AnswerSource("Share", @"\\host\share\x.exe"),
                    new AnswerSource("Settings", "ms-settings:privacy-microphone"),
                    new AnswerSource("", "https://example.com/untitled"),
                ],
                searched: "python keyerror"),
            false);
        Assert.Equal("Sources (searched: python keyerror)", card.SourcesTitle);
        Assert.Equal(["https://docs.python.org/3/", null, null, null, "https://example.com/untitled"], card.Sources.Select(s => s.Url));
        Assert.Equal(["Docs", "Local", "Share", "Settings", "https://example.com/untitled"], card.Sources.Select(s => s.Title));
        Assert.Equal([1, 2, 3, 4, 5], card.Sources.Select(s => s.Number));
        Assert.Equal("Sources", ExchangeCard.From(Samples.Exchange(sources: [new AnswerSource("a", "https://a.example.com/")]), false).SourcesTitle);
    }

    [Fact]
    public void The_details_line_reads_like_the_qt_card()
    {
        var plain = ExchangeCard.From(Samples.Exchange(), false);
        Assert.Equal("Qwen2-VL-2B via local  ·  first token 1.2s  ·  confidence 90%", plain.Details);
        var rich = ExchangeCard.From(
            Samples.Exchange(confidence: null, finish: "length", tier: "kaggle", ttftMs: 3300, sources: [new AnswerSource("a", "https://a.example.com/")]), false);
        Assert.Equal("Qwen2-VL-2B via kaggle  ·  first token 3.3s  ·  web  ·  stopped: length", rich.Details);
    }

    [Fact]
    public void A_note_is_kept_with_the_card()
    {
        var card = ExchangeCard.From(Samples.Exchange(note: "Web search was slow."), false);
        Assert.True(card.HasNote);
        Assert.Equal("Web search was slow.", card.Note);
        Assert.False(ExchangeCard.From(Samples.Exchange(), false).HasNote);
    }

    [Fact]
    public void Parsing_a_full_exchange_line_gives_the_same_card_data()
    {
        var line = """
        {"event":"exchange","origin":"window","question":"why?","searched":"q","note":"n",
         "response":{"model_id":"m","summary":"S","markdown":"md","transcript":null,"confidence":0.5,"finish_reason":"stop",
                     "sources":[{"title":"T","url":"https://a.example.com/","snippet":"x"}]},
         "metrics":{"tier":"local","server_ttft_ms":2000},
         "segments":[{"kind":"prose","text":"hello","language":"text"},{"kind":"code","text":"ls","language":"bash"}],
         "actions":{"copy_fix":"","copy_command":"ls","run":[{"language":"bash","command":"ls"},{"language":"bash","command":"   "}]}}
        """.ReplaceLineEndings(" ");
        var evt = Assert.IsType<ExchangeEvent>(BridgeProtocol.ParseEvent(line));
        Assert.Equal(("window", "why?", "q", "n"), (evt.Origin, evt.Question, evt.Searched, evt.Note));
        Assert.Equal(("m", "S", 0.5), (evt.Response.ModelId, evt.Response.Summary, evt.Response.Confidence));
        Assert.Equal(["T"], evt.Response.Sources.Select(s => s.Title));
        Assert.Equal(("local", 2000d), (evt.Metrics.Tier, evt.Metrics.ServerTtftMs));
        Assert.Equal(["prose", "code"], evt.Segments.Select(s => s.Kind));
        Assert.Single(evt.Actions.Run);  // a blank command is dropped
        Assert.Equal("ls", evt.Actions.CopyCommand);
    }

    [Fact]
    public void Absurd_sizes_are_capped_when_parsing()
    {
        var segments = string.Join(",", Enumerable.Range(0, 1000).Select(i => $"{{\"kind\":\"prose\",\"text\":\"p{i}\",\"language\":\"text\"}}"));
        var runs = string.Join(",", Enumerable.Range(0, 1000).Select(i => $"{{\"language\":\"bash\",\"command\":\"c{i}\"}}"));
        var sources = string.Join(",", Enumerable.Range(0, 1000).Select(i => $"{{\"title\":\"t{i}\",\"url\":\"https://a.example.com/{i}\"}}"));
        var evt = Assert.IsType<ExchangeEvent>(BridgeProtocol.ParseEvent(
            $"{{\"event\":\"exchange\",\"response\":{{\"sources\":[{sources}]}},\"segments\":[{segments}],\"actions\":{{\"run\":[{runs}]}}}}"));
        Assert.Equal(200, evt.Segments.Count);
        Assert.Equal(32, evt.Actions.Run.Count);
        Assert.Equal(16, evt.Response.Sources.Count);
    }

    [Theory]
    [InlineData("""{"event":"exchange","response":5,"metrics":"x","actions":[1],"segments":{"a":1}}""")]
    [InlineData("""{"event":"exchange","response":{"sources":"nope"},"segments":[1,"a",null],"actions":{"run":[5]}}""")]
    [InlineData("""{"event":"exchange","response":null,"metrics":null,"actions":null,"segments":null}""")]
    public void Wrongly_shaped_exchange_fields_never_throw(string line)
    {
        var evt = Assert.IsType<ExchangeEvent>(BridgeProtocol.ParseEvent(line));
        Assert.NotNull(ExchangeCard.From(evt, true));
    }

    [Fact]
    public void The_engines_event_lists_choices_and_skips_empty_keys()
    {
        var evt = Assert.IsType<EnginesEvent>(BridgeProtocol.ParseEvent(
            """{"event":"engines","choices":[{"key":"auto","label":"Auto"},{"key":"","label":"x"},{"key":"kaggle"},5]}"""));
        Assert.Equal([new EngineChoice("auto", "Auto"), new EngineChoice("kaggle", "kaggle")], evt.Choices);
    }

    [Fact]
    public void Json_can_be_deeply_nested_without_trouble()
    {
        var nested = string.Concat(Enumerable.Repeat("[", 60)) + string.Concat(Enumerable.Repeat("]", 60));
        Assert.IsType<ExchangeEvent>(BridgeProtocol.ParseEvent($"{{\"event\":\"exchange\",\"segments\":{nested}}}"));
        _ = JsonDocument.Parse("{}");
    }
}

public class ShellBehaviorTests
{
    private static (ShellViewModel Vm, List<string> Sent) Build()
    {
        var vm = new ShellViewModel();
        var sent = new List<string>();
        vm.CommandRequested += sent.Add;
        return (vm, sent);
    }

    [Fact]
    public void Send_trims_clears_and_sends_one_ask()
    {
        var (vm, sent) = Build();
        vm.ComposerText = "   why does this crash?  ";
        Assert.True(vm.Send());
        Assert.Equal([BridgeCommands.Ask("why does this crash?")], sent);
        Assert.Equal("", vm.ComposerText);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("\n\t")]
    public void Send_ignores_blank_text(string text)
    {
        var (vm, sent) = Build();
        vm.ComposerText = text;
        Assert.False(vm.Send());
        Assert.Empty(sent);
    }

    [Theory]
    [InlineData("capturing")]
    [InlineData("analyzing")]
    [InlineData("recording_voice")]
    public void Nothing_is_sent_while_busy_or_recording_and_the_text_is_kept(string state)
    {
        var (vm, sent) = Build();
        vm.Apply(new StateEvent(state, "working"));
        vm.ComposerText = "keep me";
        Assert.False(vm.CanInput);
        Assert.False(vm.Send());
        Assert.False(vm.Capture());
        Assert.False(vm.Clear());
        Assert.Empty(sent);
        Assert.Equal("keep me", vm.ComposerText);
    }

    [Theory]
    [InlineData("idle")]
    [InlineData("displaying")]
    [InlineData("error")]
    public void Input_works_again_when_idle_showing_an_answer_or_after_an_error(string state)
    {
        var (vm, sent) = Build();
        vm.Apply(new StateEvent(state, ""));
        Assert.True(vm.CanInput);
        Assert.True(vm.Capture());
        Assert.True(vm.Clear());
        Assert.Equal([BridgeCommands.Capture(), BridgeCommands.Clear()], sent);
    }

    [Fact]
    public void The_input_limit_is_applied()
    {
        var (vm, sent) = Build();
        vm.ComposerText = new string('a', ShellViewModel.MaxInputChars + 500);
        vm.Send();
        using var doc = JsonDocument.Parse(sent[0]);
        Assert.Equal(ShellViewModel.MaxInputChars, doc.RootElement.GetProperty("text").GetString()!.Length);
    }

    [Fact]
    public void The_microphone_starts_with_typed_text_and_stops_without_resending_it()
    {
        var (vm, sent) = Build();
        vm.ComposerText = " explain this ";
        Assert.True(vm.Mic());
        Assert.Equal(BridgeCommands.Mic("explain this"), sent[0]);
        Assert.Equal("", vm.ComposerText);
        vm.Apply(new StateEvent("recording_voice", "Listening…"));
        Assert.Equal("Stop and send", vm.MicText);
        vm.ComposerText = "typed while talking";
        Assert.True(vm.Mic());
        Assert.Equal(BridgeCommands.Mic(""), sent[1]);
        Assert.Equal("typed while talking", vm.ComposerText);
        vm.Apply(new StateEvent("analyzing", "Analyzing…"));
        Assert.Equal("Speak", vm.MicText);
        Assert.False(vm.CanUseMic);
        Assert.False(vm.Mic());
        Assert.Equal(2, sent.Count);
    }

    [Fact]
    public void The_screen_switch_changes_the_placeholder_and_tells_python_but_python_events_do_not_echo()
    {
        var (vm, sent) = Build();
        Assert.Equal(ShellViewModel.AskPlaceholder, vm.Placeholder);
        vm.IncludeScreen = false;
        Assert.Equal(ShellViewModel.ChatPlaceholder, vm.Placeholder);
        Assert.Equal([BridgeCommands.Set("include_screen", false)], sent);
        sent.Clear();
        vm.Apply(new EngineEvent("auto"));
        Assert.Empty(sent);
    }

    [Fact]
    public void Choosing_an_engine_sends_it_but_hearing_it_from_python_does_not()
    {
        var (vm, sent) = Build();
        vm.Apply(new EnginesEvent([new EngineChoice("auto", "Auto"), new EngineChoice("local_gpu", "This PC: GPU")]));
        vm.Apply(new EngineEvent("auto"));
        Assert.Empty(sent);
        Assert.Equal("auto", vm.SelectedEngineKey);
        vm.SelectedEngineKey = "local_gpu";
        Assert.Equal([BridgeCommands.Engine("local_gpu")], sent);
        sent.Clear();
        vm.Apply(new EngineEvent("local_gpu"));
        Assert.Empty(sent);
        Assert.Equal("local_gpu", vm.EngineKey);
    }

    [Fact]
    public void A_refused_engine_change_snaps_the_picker_back()
    {
        var (vm, _) = Build();
        vm.Apply(new EnginesEvent([new EngineChoice("auto", "Auto"), new EngineChoice("local_gpu", "This PC: GPU")]));
        vm.Apply(new EngineEvent("auto"));
        vm.SelectedEngineKey = "local_gpu";
        vm.Apply(new ErrorEvent("OmniSight is busy: wait for the current answer to finish."));
        Assert.Equal("auto", vm.SelectedEngineKey);
        Assert.True(vm.NoticeIsError);
    }

    [Fact]
    public void The_conversation_grows_is_capped_and_clears()
    {
        var (vm, _) = Build();
        var changes = new List<string?>();
        vm.PropertyChanged += (_, e) => changes.Add(e.PropertyName);
        Assert.False(vm.HasConversation);
        for (var i = 0; i < ShellViewModel.MaxCards + 5; i++)
        {
            vm.Apply(Samples.Exchange(question: $"q{i}"));
        }

        Assert.Equal(ShellViewModel.MaxCards, vm.Conversation.Count);
        Assert.Equal("q5", vm.Conversation[0].Heading);
        Assert.True(vm.HasConversation);
        Assert.Contains(nameof(ShellViewModel.HasConversation), changes);
        vm.Apply(new ClearEvent());
        Assert.False(vm.HasConversation);
    }

    [Fact]
    public void An_answer_clears_the_notice()
    {
        var (vm, _) = Build();
        vm.Apply(new NoticeEvent("careful", false));
        vm.Apply(Samples.Exchange());
        Assert.Equal("", vm.NoticeText);
    }

    [Fact]
    public void The_run_switch_shows_and_hides_run_buttons_on_existing_cards_but_never_on_watch_alerts()
    {
        var (vm, _) = Build();
        vm.Apply(Samples.Exchange(question: "a", actions: Samples.WithRun(("bash", "ls"))));
        vm.Apply(Samples.Exchange(question: "w", origin: "watch", actions: Samples.WithRun(("bash", "ls"))));
        Assert.All(vm.Conversation, c => Assert.False(c.ShowRun));
        vm.Apply(new SwitchEvent("actions", true));
        Assert.True(vm.Conversation[0].ShowRun);
        Assert.False(vm.Conversation[1].ShowRun);
        vm.Apply(Samples.Exchange(question: "later", actions: Samples.WithRun(("bash", "ls"))));
        Assert.True(vm.Conversation[2].ShowRun);
        vm.Apply(new SwitchEvent("actions", false));
        Assert.All(vm.Conversation, c => Assert.False(c.ShowRun));
    }

    [Fact]
    public void A_run_request_needs_a_visible_button_that_belongs_to_that_card()
    {
        var (vm, sent) = Build();
        vm.Apply(Samples.Exchange(actions: Samples.WithRun(("powershell", "Get-Date"))));
        vm.Apply(Samples.Exchange(actions: Samples.WithRun(("bash", "ls"))));
        var first = vm.Conversation[0];
        var second = vm.Conversation[1];
        Assert.False(vm.RequestRun(first, first.RunButtons[0]));  // switch is off
        vm.Apply(new SwitchEvent("actions", true));
        Assert.False(vm.RequestRun(first, second.RunButtons[0]));  // a button from another card
        Assert.False(vm.RequestRun(first, new RunButton("Run...", "Run command 1", "powershell", "Remove-Item C:\\ -Recurse")));  // a made-up button
        Assert.Empty(sent);
        Assert.True(vm.RequestRun(first, first.RunButtons[0]));
        Assert.Equal([BridgeCommands.Run("powershell", "Get-Date")], sent);
    }

    [Theory]
    [InlineData("https://docs.python.org/3/", true)]
    [InlineData("file:///C:/Windows/System32/calc.exe", false)]
    [InlineData("ms-settings:privacy", false)]
    [InlineData("http://127.0.0.1:8000/", false)]
    [InlineData(null, false)]
    public void Only_safe_links_are_ever_passed_on_to_be_opened(string? url, bool opens)
    {
        var (vm, _) = Build();
        var opened = new List<string>();
        vm.OpenLinkRequested += opened.Add;
        Assert.Equal(opens, vm.OpenLink(url));
        Assert.Equal(opens ? 1 : 0, opened.Count);
    }

    [Fact]
    public void Copy_passes_text_on_and_ignores_empty_text()
    {
        var (vm, _) = Build();
        var copied = new List<string>();
        vm.CopyRequested += copied.Add;
        vm.CopyText("fix()");
        vm.CopyText("");
        Assert.Equal(["fix()"], copied);
    }

    [Fact]
    public void The_status_line_follows_the_node_and_the_request_like_the_qt_window()
    {
        var (vm, _) = Build();
        vm.Apply(new NodeEvent("ready", "", "GPU: RTX", true, "local_gpu"));
        Assert.Equal("Local node ready on GPU: RTX.", vm.StatusText);
        vm.Apply(new StateEvent("analyzing", "Analyzing the screen…"));
        Assert.Equal("Analyzing the screen…", vm.StatusText);
        vm.Apply(new NodeEvent("ready", "", "GPU: RTX", true, "local_gpu"));
        Assert.Equal("Analyzing the screen…", vm.StatusText);  // a node update does not overwrite a running request
        vm.Apply(new StateEvent("displaying", ""));
        Assert.Equal("Local node ready on GPU: RTX.", vm.StatusText);
        vm.Apply(new NodeEvent("stopped", "", null, false, "local_gpu"));
        Assert.Contains("Start local node", vm.StatusText);
        vm.Apply(new NodeEvent("stopped", "", null, false, "auto"));
        Assert.Equal("Ready.", vm.StatusText);
        vm.Apply(new NodeEvent("starting", "loading the model", null, true, "local_gpu"));
        Assert.Equal("Local node starting: loading the model", vm.StatusText);
        vm.Apply(new NodeEvent("failed", "no GPU found", null, false, "local_gpu"));
        Assert.Equal("The local node is not running.", vm.StatusText);
        Assert.Equal(("no GPU found", true), (vm.NoticeText, vm.NoticeIsError));
    }

    [Fact]
    public void After_an_error_the_status_line_no_longer_says_analyzing()
    {
        var (vm, _) = Build();
        vm.Apply(new StateEvent("analyzing", "Analyzing the screen…"));
        vm.Apply(new StateEvent("error", ""));
        Assert.Equal("Ready.", vm.StatusText);
        Assert.True(vm.CanInput);
    }

    [Fact]
    public void The_progress_line_shows_what_python_is_doing()
    {
        var (vm, _) = Build();
        vm.Apply(new ProgressEvent("Searching the web…"));
        Assert.Equal("Searching the web…", vm.StatusText);
    }
}
