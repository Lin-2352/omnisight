using System.Text.Json;

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
public sealed record ExchangeEvent(string Question, string Searched, string Note, JsonElement Response, JsonElement Metrics) : BridgeEvent;
public sealed record ClearEvent : BridgeEvent;
public sealed record ShowEvent : BridgeEvent;
public sealed record PongEvent : BridgeEvent;
public sealed record ErrorEvent(string Message) : BridgeEvent;

/// <summary>An event name this version does not know (kept so a newer Python client never breaks an older app).</summary>
public sealed record UnknownEvent(string Name) : BridgeEvent;

/// <summary>A line that was not a usable event (not JSON, not an object, no "event" name).</summary>
public sealed record MalformedEvent(string Reason) : BridgeEvent;
