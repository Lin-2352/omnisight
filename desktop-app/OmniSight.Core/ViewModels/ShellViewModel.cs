using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using OmniSight.Core.Protocol;
using OmniSight.Core.Security;

namespace OmniSight.Core.ViewModels;

public enum ConnectionState
{
    Starting,
    Connected,
    Disconnected,
    Failed,
}

/// <summary>
/// What the main window shows and what its controls may do. It holds no threads and no controls: the app marshals events to
/// the UI thread before calling <see cref="Apply"/>, and sends whatever <see cref="CommandRequested"/> raises.
/// The enable rules are the Qt window's, so the two UIs behave the same.
/// </summary>
public sealed partial class ShellViewModel : ObservableObject
{
    public const int MaxLogLines = 200;
    public const int MaxCards = 30;
    public const int MaxInputChars = 4000;
    public const string AskPlaceholder = "Ask about your screen…  (Enter to send)";
    public const string ChatPlaceholder = "Message OmniSight (no screenshot is sent)…  (Enter to send)";

    private bool _applying;
    private string _idleText = "Ready.";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanRestart))]
    private ConnectionState _connection = ConnectionState.Starting;

    [ObservableProperty]
    private string _connectionText = "Starting the OmniSight engine…";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsBusy), nameof(IsRecording), nameof(CanInput), nameof(CanUseMic), nameof(MicText))]
    private string _appState = "idle";

    [ObservableProperty]
    private string _statusText = "";

    [ObservableProperty]
    private string _engineKey = "";

    [ObservableProperty]
    private string _nodeText = "";

    [ObservableProperty]
    private string _noticeText = "";

    [ObservableProperty]
    private bool _noticeIsError;

    [ObservableProperty]
    private string _composerText = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(Placeholder))]
    private bool _includeScreen = true;

    [ObservableProperty]
    private string? _selectedEngineKey;

    // the switches: they send a command when the person flips them, never when Python tells us their state
    [ObservableProperty]
    private bool _memoryOn;

    [ObservableProperty]
    private bool _speakOn;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(SmartEnabled))]
    private bool _searchOn;

    [ObservableProperty]
    private bool _smartOn;

    [ObservableProperty]
    private bool _watchOn;

    [ObservableProperty]
    private bool _actionsOn;

    [ObservableProperty]
    private bool _speakAvailable = true;

    [ObservableProperty]
    private bool _speaking;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasWatch))]
    private string _watchText = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(PauseText))]
    private bool _watchPaused;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(NodeButtonText))]
    private bool _nodeRunning;

    [ObservableProperty]
    private bool _nodeVisible;

    [ObservableProperty]
    private bool _isPaneOpen;

    /// <summary>The open "Run command" approval, if any. Nothing else in the window can be used while it is open.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasRunApproval), nameof(NoRunApproval))]
    private RunApprovalViewModel? _runApproval;

    // settings page
    [ObservableProperty]
    private string _overrideUrl = "";

    [ObservableProperty]
    private string _localUrl = "";

    [ObservableProperty]
    private string _activeEndpoint = "";

    [ObservableProperty]
    private string _capability = "";

    [ObservableProperty]
    private string _fallbackUrl = "";

    [ObservableProperty]
    private string _hotkeys = "";

    [ObservableProperty]
    private string _logDir = "";

    [ObservableProperty]
    private string _settingsResult = "";

    public ObservableCollection<string> Log { get; } = [];
    public ObservableCollection<ExchangeCard> Conversation { get; } = [];
    public ObservableCollection<EngineChoice> Engines { get; } = [];

    /// <summary>Raised with a ready-to-send command line; the app writes it to the socket.</summary>
    public event Action<string>? CommandRequested;

    /// <summary>Raised with text that should go on the clipboard.</summary>
    public event Action<string>? CopyRequested;

    /// <summary>Raised with a safe (checked by <see cref="LinkPolicy"/>) address to open in the browser.</summary>
    public event Action<string>? OpenLinkRequested;

    /// <summary>Raised when Python needs a yes or no. The window must show a dialog whose default answer is No.</summary>
    public event Action<ConfirmEvent>? ConfirmRequested;

    /// <summary>Raised when the engine says it is quitting on purpose: the window should close.</summary>
    public event Action? EngineQuit;

    /// <summary>Raised with an existing folder to open in Explorer (the log folder).</summary>
    public event Action<string>? OpenFolderRequested;

    /// <summary>The engine is not running: offer "Restart engine".</summary>
    public bool CanRestart => Connection is ConnectionState.Failed or ConnectionState.Disconnected;

    public bool IsBusy => AppState is "capturing" or "analyzing";
    public bool IsRecording => AppState == "recording_voice";
    public bool CanInput => !IsBusy && !IsRecording;
    public bool CanUseMic => !IsBusy;
    public string MicText => IsRecording ? "Stop and send" : "Speak";
    public string Placeholder => IncludeScreen ? AskPlaceholder : ChatPlaceholder;
    public bool HasConversation => Conversation.Count > 0;
    public bool SmartEnabled => SearchOn;
    public bool HasWatch => WatchText.Length > 0;
    public string PauseText => WatchPaused ? "Resume watching" : "Pause watching";
    public string NodeButtonText => NodeRunning ? "Stop local node" : "Start local node";
    public bool CanStopVoice => Speaking;
    public bool HasRunApproval => RunApproval is not null;
    public bool NoRunApproval => RunApproval is null;

    /// <summary>Raised when an approval opens, so the window can put the keyboard focus in it.</summary>
    public event Action<RunApprovalViewModel>? RunApprovalOpened;

    public void SetStarting(string text)
    {
        Connection = ConnectionState.Starting;
        ConnectionText = text;
    }

    public void SetFailed(string text)
    {
        Connection = ConnectionState.Failed;
        ConnectionText = text;
    }

    public void SetDisconnected(string text)
    {
        Connection = ConnectionState.Disconnected;
        ConnectionText = text;
    }

    // -- what the user does ------------------------------------------------------------------

    /// <summary>Send the typed text. Does nothing when empty or while busy (the Qt window disabled Send then).</summary>
    public bool Send()
    {
        var text = ComposerText.Trim();
        if (text.Length == 0 || !CanInput)
        {
            return false;
        }

        ComposerText = "";
        CommandRequested?.Invoke(BridgeCommands.Ask(text.Length > MaxInputChars ? text[..MaxInputChars] : text));
        return true;
    }

    public bool Capture()
    {
        if (!CanInput)
        {
            return false;
        }

        CommandRequested?.Invoke(BridgeCommands.Capture());
        return true;
    }

    /// <summary>Click to start listening, click again to stop and send. Typed text goes along with the voice question.</summary>
    public bool Mic()
    {
        if (!CanUseMic)
        {
            return false;
        }

        var typed = IsRecording ? "" : ComposerText.Trim();
        if (!IsRecording)
        {
            ComposerText = "";
        }

        CommandRequested?.Invoke(BridgeCommands.Mic(typed.Length > MaxInputChars ? typed[..MaxInputChars] : typed));
        return true;
    }

    public bool Clear()
    {
        if (!CanInput)
        {
            return false;
        }

        CommandRequested?.Invoke(BridgeCommands.Clear());
        return true;
    }

    public void CopyText(string text)
    {
        if (text.Length > 0)
        {
            CopyRequested?.Invoke(text);
        }
    }

    /// <summary>Open a link from an answer or a source. Anything <see cref="LinkPolicy"/> does not accept is ignored.</summary>
    public bool OpenLink(string? url)
    {
        if (!LinkPolicy.TryNormalize(url, out var safe))
        {
            return false;
        }

        OpenLinkRequested?.Invoke(safe);
        return true;
    }

    /// <summary>Ask Python to open its approval dialog for this command. Only a card with its Run button shown can do this.</summary>
    public bool RequestRun(ExchangeCard card, RunButton button)
    {
        if (!card.ShowRun || !card.RunButtons.Contains(button))
        {
            return false;
        }

        CommandRequested?.Invoke(BridgeCommands.Run(button.Language, button.Command));
        return true;
    }

    partial void OnMemoryOnChanged(bool value) => SendSwitch("memory", value);

    partial void OnSpeakOnChanged(bool value) => SendSwitch("speak", value);

    partial void OnSearchOnChanged(bool value) => SendSwitch("search", value);

    partial void OnSmartOnChanged(bool value) => SendSwitch("smart", value);

    partial void OnWatchOnChanged(bool value) => SendSwitch("watch", value);

    partial void OnActionsOnChanged(bool value) => SendSwitch("actions", value);

    private void SendSwitch(string name, bool on)
    {
        if (!_applying)
        {
            CommandRequested?.Invoke(BridgeCommands.Set(name, on));
        }
    }

    public void ToggleNode() => CommandRequested?.Invoke(BridgeCommands.NodeToggle());

    public void StopVoice() => CommandRequested?.Invoke(BridgeCommands.StopSpeaking());

    public void PauseWatch() => CommandRequested?.Invoke(BridgeCommands.WatchPause());

    /// <summary>The person's answer to a <see cref="ConfirmEvent"/>.</summary>
    public void AnswerConfirm(string id, bool yes) => CommandRequested?.Invoke(BridgeCommands.Answer(id, yes));

    public void RefreshSettings() => CommandRequested?.Invoke(BridgeCommands.SettingsGet());

    public void ApplySettings()
    {
        SettingsResult = "";
        CommandRequested?.Invoke(BridgeCommands.SettingsApply(OverrideUrl.Trim(), LocalUrl.Trim()));
    }

    public void TestConnection()
    {
        SettingsResult = "Checking…";
        CommandRequested?.Invoke(BridgeCommands.TestConnection());
    }

    public bool OpenLogs()
    {
        if (LogDir.Length == 0 || !Path.IsPathRooted(LogDir) || !Directory.Exists(LogDir))
        {
            return false;
        }

        OpenFolderRequested?.Invoke(LogDir);
        return true;
    }

    partial void OnIsPaneOpenChanged(bool value)
    {
        if (value)
        {
            RefreshSettings();
        }
    }

    partial void OnIncludeScreenChanged(bool value)
    {
        if (!_applying)
        {
            CommandRequested?.Invoke(BridgeCommands.Set("include_screen", value));
        }
    }

    partial void OnSelectedEngineKeyChanged(string? value)
    {
        if (!_applying && !string.IsNullOrEmpty(value) && value != EngineKey)
        {
            CommandRequested?.Invoke(BridgeCommands.Engine(value));
        }
    }

    // -- what Python tells us ----------------------------------------------------------------------

    public void Apply(BridgeEvent evt)
    {
        _applying = true;
        try
        {
            ApplyCore(evt);
        }
        finally
        {
            _applying = false;
        }

        AddLog(Describe(evt));
    }

    private void ApplyCore(BridgeEvent evt)
    {
        switch (evt)
        {
            case HelloEvent:
                Connection = ConnectionState.Connected;
                ConnectionText = "Connected";
                break;
            case EnginesEvent e:
                Engines.Clear();
                foreach (var choice in e.Choices)
                {
                    Engines.Add(choice);
                }

                SelectedEngineKey = EngineKey.Length > 0 && Engines.Any(c => c.Key == EngineKey) ? EngineKey : SelectedEngineKey;
                break;
            case EngineEvent e:
                EngineKey = e.Key;
                SelectedEngineKey = e.Key;
                break;
            case StateEvent s:
                AppState = s.State;
                if (s.Message.Length > 0)
                {
                    StatusText = s.Message;
                }
                else if (s.State is "idle" or "displaying" or "error")
                {
                    StatusText = _idleText;
                }

                break;
            case NodeEvent n:
                NodeText = n.Device is { Length: > 0 } ? $"{n.State}: {n.Device}" : n.State;
                NodeVisible = n.Engine.StartsWith("local", StringComparison.Ordinal) || n.Engine == "auto";
                NodeRunning = n.State is "starting" or "ready" && n.Owned;
                ApplyNode(n);
                break;
            case SpeakAvailableEvent a:
                SpeakAvailable = a.Available;
                break;
            case SpeakingEvent sp:
                Speaking = sp.On;
                OnPropertyChanged(nameof(CanStopVoice));
                break;
            case WatchEvent w:
                WatchText = w.Text;
                WatchPaused = w.Paused;
                break;
            case ConfirmEvent c:
                if (c.Kind == "allow_actions" && ConfirmRequested is not null)
                {
                    ConfirmRequested(c);
                }
                else
                {
                    CommandRequested?.Invoke(BridgeCommands.Answer(c.Id, false));  // a question nobody can show is answered No
                }

                break;
            case SettingsInfoEvent i:
                ActiveEndpoint = i.Endpoint;
                OverrideUrl = i.OverrideUrl;
                LocalUrl = i.LocalUrl;
                Capability = i.Capability;
                FallbackUrl = i.FallbackUrl;
                Hotkeys = i.Hotkeys;
                LogDir = i.LogDir;
                break;
            case SettingsResultEvent r:
                SettingsResult = r.Text;
                break;
            case RunOpenEvent o:
                RunApproval = new RunApprovalViewModel(o, json => CommandRequested?.Invoke(json));
                RunApprovalOpened?.Invoke(RunApproval);
                break;
            case RunVerdictEvent v when RunApproval?.Id == v.Id:
                RunApproval.ApplyVerdict(v.Refused, v.Warnings);
                break;
            case RunStateEvent st when RunApproval?.Id == st.Id:
                RunApproval.ApplyState(st);
                break;
            case RunClosedEvent c when RunApproval?.Id == c.Id:
                RunApproval = null;
                break;
            case NoticeEvent n:
                NoticeText = n.Text;
                NoticeIsError = n.Error;
                break;
            case ProgressEvent p:
                StatusText = p.Text;
                break;
            case ErrorEvent e:
                NoticeText = e.Message;
                NoticeIsError = true;
                SelectedEngineKey = EngineKey.Length > 0 ? EngineKey : SelectedEngineKey;  // a refused engine change must not stay selected
                break;
            case ExchangeEvent x:
                NoticeText = "";
                Conversation.Add(ExchangeCard.From(x, ActionsOn));
                while (Conversation.Count > MaxCards)
                {
                    Conversation.RemoveAt(0);
                }

                OnPropertyChanged(nameof(HasConversation));
                break;
            case ClearEvent:
                Conversation.Clear();
                OnPropertyChanged(nameof(HasConversation));
                break;
            case ByeEvent:
                EngineQuit?.Invoke();
                break;
            case OpenOptionsEvent:
                IsPaneOpen = true;
                break;
            case SwitchEvent s:
                ApplySwitch(s);
                break;
        }
    }

    private void ApplySwitch(SwitchEvent s)
    {
        switch (s.Name)
        {
            case "memory":
                MemoryOn = s.On;
                break;
            case "speak":
                SpeakOn = s.On;
                break;
            case "search":
                SearchOn = s.On;
                break;
            case "smart":
                SmartOn = s.On;
                break;
            case "watch":
                WatchOn = s.On;
                break;
            case "actions":
                ActionsOn = s.On;
                foreach (var card in Conversation)
                {
                    card.ShowRun = s.On && !card.IsWatch;
                }

                break;
        }
    }

    /// <summary>The status line when nothing is running: the node's state, as the Qt window words it.</summary>
    private void ApplyNode(NodeEvent n)
    {
        var local = n.Engine.StartsWith("local", StringComparison.Ordinal);
        switch (n.State)
        {
            case "ready":
                _idleText = $"Local node ready on {n.Device}.";
                break;
            case "starting":
                _idleText = $"Local node starting: {n.Message}";
                break;
            case "failed":
                _idleText = "The local node is not running.";
                NoticeText = n.Message;
                NoticeIsError = true;
                break;
            default:
                _idleText = local ? "The local node is not running. Press “Start local node”." : "Ready.";
                break;
        }

        if (!IsBusy && !IsRecording)
        {
            StatusText = _idleText;
        }
    }

    private void AddLog(string line)
    {
        Log.Add(line);
        while (Log.Count > MaxLogLines)
        {
            Log.RemoveAt(0);
        }
    }

    private static string Describe(BridgeEvent evt) => evt switch
    {
        HelloEvent h => $"hello (protocol {h.Protocol})",
        EnginesEvent e => $"{e.Choices.Count} engine choices",
        StateEvent s => $"state {s.State} {s.Message}".TrimEnd(),
        NodeEvent n => $"node {n.State} {n.Device}".TrimEnd(),
        NoticeEvent n => $"notice{(n.Error ? " (error)" : "")}: {n.Text}",
        ProgressEvent p => $"progress: {p.Text}",
        EngineEvent e => $"engine {e.Key}",
        SwitchEvent s => $"switch {s.Name} {(s.On ? "on" : "off")}",
        ConfirmEvent c => $"question: {c.Title}",
        SettingsInfoEvent => "settings loaded",
        SettingsResultEvent r => $"settings: {r.Text}",
        RunOpenEvent o => o.Refused is null ? "run approval opened" : "run approval opened (refused command)",
        RunVerdictEvent => "run verdict changed",
        RunStateEvent st => $"run {st.State}",
        RunClosedEvent => "run approval closed",
        ExchangeEvent x => $"answer to: {x.Question}",
        ErrorEvent e => $"error: {e.Message}",
        SpeakAvailableEvent s => $"speech {(s.Available ? "available" : "not available")}",
        SpeakingEvent s => $"speaking {(s.On ? "on" : "off")}",
        WatchEvent w => $"watch {(w.Text.Length == 0 ? "off" : w.Text)}{(w.Paused ? " (paused)" : "")}",
        ClearEvent => "conversation cleared",
        ByeEvent => "engine is quitting",
        OpenOptionsEvent => "open options",
        ShowEvent => "show window",
        PongEvent => "pong",
        UnknownEvent u => $"unknown event {u.Name}",
        MalformedEvent m => $"malformed line ({m.Reason})",
        _ => evt.GetType().Name,
    };
}
