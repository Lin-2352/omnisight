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
    private bool _actionsEnabled;
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

    public ObservableCollection<string> Log { get; } = [];
    public ObservableCollection<ExchangeCard> Conversation { get; } = [];
    public ObservableCollection<EngineChoice> Engines { get; } = [];

    /// <summary>Raised with a ready-to-send command line; the app writes it to the socket.</summary>
    public event Action<string>? CommandRequested;

    /// <summary>Raised with text that should go on the clipboard.</summary>
    public event Action<string>? CopyRequested;

    /// <summary>Raised with a safe (checked by <see cref="LinkPolicy"/>) address to open in the browser.</summary>
    public event Action<string>? OpenLinkRequested;

    /// <summary>The engine is not running: offer "Restart engine".</summary>
    public bool CanRestart => Connection is ConnectionState.Failed or ConnectionState.Disconnected;

    public bool IsBusy => AppState is "capturing" or "analyzing";
    public bool IsRecording => AppState == "recording_voice";
    public bool CanInput => !IsBusy && !IsRecording;
    public bool CanUseMic => !IsBusy;
    public string MicText => IsRecording ? "Stop and send" : "Speak";
    public string Placeholder => IncludeScreen ? AskPlaceholder : ChatPlaceholder;
    public bool HasConversation => Conversation.Count > 0;

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
                ApplyNode(n);
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
                Conversation.Add(ExchangeCard.From(x, _actionsEnabled));
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
            case SwitchEvent { Name: "actions" } s:
                _actionsEnabled = s.On;
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
        ExchangeEvent x => $"answer to: {x.Question}",
        ErrorEvent e => $"error: {e.Message}",
        SpeakAvailableEvent s => $"speech {(s.Available ? "available" : "not available")}",
        SpeakingEvent s => $"speaking {(s.On ? "on" : "off")}",
        WatchEvent w => $"watch {(w.Text.Length == 0 ? "off" : w.Text)}{(w.Paused ? " (paused)" : "")}",
        ClearEvent => "conversation cleared",
        ShowEvent => "show window",
        PongEvent => "pong",
        UnknownEvent u => $"unknown event {u.Name}",
        MalformedEvent m => $"malformed line ({m.Reason})",
        _ => evt.GetType().Name,
    };
}
