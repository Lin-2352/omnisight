using System.Text.Json;

namespace OmniSight.Core.Protocol;

/// <summary>Reads events and builds commands. Never throws on input from the wire.</summary>
public static class BridgeProtocol
{
    public const int Version = 1;

    /// <summary>The Python bridge refuses lines above this size, so the app never sends one.</summary>
    public const int MaxLineBytes = 1024 * 1024;

    public static BridgeEvent ParseEvent(string line)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(line);
        }
        catch (JsonException)
        {
            return new MalformedEvent("not JSON");
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return new MalformedEvent("not an object");
            }

            if (!root.TryGetProperty("event", out var name) || name.ValueKind != JsonValueKind.String)
            {
                return new MalformedEvent("no event name");
            }

            var kind = name.GetString() ?? "";
            return kind switch
            {
                "hello" => new HelloEvent(Int(root, "protocol")),
                "state" => new StateEvent(Str(root, "state"), Str(root, "message")),
                "node" => new NodeEvent(Str(root, "state"), Str(root, "message"), NullableStr(root, "device"), Bool(root, "owned"), Str(root, "engine")),
                "notice" => new NoticeEvent(Str(root, "text"), Bool(root, "error")),
                "progress" => new ProgressEvent(Str(root, "text")),
                "engine" => new EngineEvent(Str(root, "key")),
                "switch" => new SwitchEvent(Str(root, "name"), Bool(root, "on")),
                "speak_available" => new SpeakAvailableEvent(Bool(root, "available")),
                "speaking" => new SpeakingEvent(Bool(root, "on")),
                "watch" => new WatchEvent(Str(root, "text"), Bool(root, "paused")),
                "exchange" => new ExchangeEvent(
                    Str(root, "question"), Str(root, "searched"), Str(root, "note"), Clone(root, "response"), Clone(root, "metrics")),
                "clear" => new ClearEvent(),
                "show" => new ShowEvent(),
                "pong" => new PongEvent(),
                "error" => new ErrorEvent(Str(root, "message")),
                _ => new UnknownEvent(kind),
            };
        }
    }

    private static string Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    private static string? NullableStr(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool Bool(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    private static int Int(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var i) ? i : 0;

    private static JsonElement Clone(JsonElement e, string name)
    {
        if (e.TryGetProperty(name, out var v))
        {
            return v.Clone();
        }

        using var empty = JsonDocument.Parse("{}");
        return empty.RootElement.Clone();
    }
}
