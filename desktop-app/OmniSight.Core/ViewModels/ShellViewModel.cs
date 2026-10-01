using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using OmniSight.Core.Protocol;

namespace OmniSight.Core.ViewModels;

public enum ConnectionState
{
    Starting,
    Connected,
    Disconnected,
    Failed,
}

/// <summary>What the main window shows. It holds no threads: the app marshals events to the UI thread before calling <see cref="Apply"/>.</summary>
public sealed partial class ShellViewModel : ObservableObject
{
    public const int MaxLogLines = 200;

    [ObservableProperty]
    private ConnectionState _connection = ConnectionState.Starting;

    [ObservableProperty]
    private string _connectionText = "Starting the OmniSight engine…";

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

    public ObservableCollection<string> Log { get; } = [];

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

    public void Apply(BridgeEvent evt)
    {
        switch (evt)
        {
            case HelloEvent:
                Connection = ConnectionState.Connected;
                ConnectionText = "Connected";
                break;
            case StateEvent s:
                StatusText = s.Message;
                break;
            case EngineEvent e:
                EngineKey = e.Key;
                break;
            case NodeEvent n:
                NodeText = n.Device is { Length: > 0 } ? $"{n.State}: {n.Device}" : n.State;
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
                break;
        }

        AddLog(Describe(evt));
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
