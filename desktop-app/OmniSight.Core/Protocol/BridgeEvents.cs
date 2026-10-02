namespace OmniSight.Core.Protocol;

/// <summary>Something the Python client tells the app (one JSON object per line, with an "event" key).</summary>
public abstract record BridgeEvent;

public sealed record HelloEvent(int Protocol) : BridgeEvent;
public sealed record StateEvent(string State, string Message) : BridgeEvent;
public sealed record NodeEvent(string State, string Message, string? Device, bool Owned, string Engine) : BridgeEvent;
public sealed record NoticeEvent(string Text, bool Error) : BridgeEvent;
public sealed record ProgressEvent(string Text) : BridgeEvent;
public sealed record EngineEvent(string Key) : BridgeEvent;
public sealed record SwitchEvent(string Name, bool On) : BridgeEvent;
public sealed record SpeakAvailableEvent(bool Available) : BridgeEvent;
public sealed record SpeakingEvent(bool On) : BridgeEvent;
public sealed record WatchEvent(string Text, bool Paused) : BridgeEvent;
public sealed record ClearEvent : BridgeEvent;
public sealed record ShowEvent : BridgeEvent;
public sealed record PongEvent : BridgeEvent;
public sealed record ErrorEvent(string Message) : BridgeEvent;

/// <summary>A question Python needs answered (today only "allow running commands"). The safe answer is No.</summary>
public sealed record ConfirmEvent(string Id, string Kind, string Title, string Text) : BridgeEvent;

public sealed record SettingsInfoEvent(
    string Endpoint,
    string OverrideUrl,
    string LocalUrl,
    string Capability,
    string FallbackUrl,
    string Hotkeys,
    string LogDir) : BridgeEvent;

public sealed record SettingsResultEvent(string Text) : BridgeEvent;

public sealed record EngineChoice(string Key, string Label)
{
    /// <summary>What a list or a screen reader shows for this choice.</summary>
    public override string ToString() => Label;
}

/// <summary>The engine picker's entries, sent once per connection (the labels live in Python's config).</summary>
public sealed record EnginesEvent(IReadOnlyList<EngineChoice> Choices) : BridgeEvent;

public sealed record AnswerSource(string Title, string Url);

public sealed record AnswerResponse(
    string ModelId,
    string Summary,
    string Markdown,
    string? Transcript,
    double? Confidence,
    string FinishReason,
    IReadOnlyList<AnswerSource> Sources);

public sealed record AnswerMetrics(string? Tier, double ServerTtftMs);

/// <summary>A run of prose or one fenced code block, already split by Python (the app never parses fences out of model text).</summary>
public sealed record AnswerSegment(string Kind, string Text, string Language);

public sealed record RunCommand(string Language, string Command);

/// <summary>What the card may offer, decided by Python from the answer's code blocks only (empty for watch alerts).</summary>
public sealed record ExchangeActions(string CopyFix, string CopyCommand, IReadOnlyList<RunCommand> Run);

/// <summary>A question and its answer. <see cref="Origin"/> is "window" or "watch".</summary>
public sealed record ExchangeEvent(
    string Origin,
    string Question,
    string Searched,
    string Note,
    AnswerResponse Response,
    AnswerMetrics Metrics,
    IReadOnlyList<AnswerSegment> Segments,
    ExchangeActions Actions) : BridgeEvent;

/// <summary>An event name this version does not know (kept so a newer Python client never breaks an older app).</summary>
public sealed record UnknownEvent(string Name) : BridgeEvent;

/// <summary>A line that was not a usable event (not JSON, not an object, no "event" name).</summary>
public sealed record MalformedEvent(string Reason) : BridgeEvent;
