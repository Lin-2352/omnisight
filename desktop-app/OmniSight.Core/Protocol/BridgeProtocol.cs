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
                "engines" => new EnginesEvent(Engines(root)),
                "exchange" => ParseExchange(root),
                "clear" => new ClearEvent(),
                "show" => new ShowEvent(),
                "pong" => new PongEvent(),
                "error" => new ErrorEvent(Str(root, "message")),
                _ => new UnknownEvent(kind),
            };
        }
    }

    private const int MaxSegments = 200;
    private const int MaxSources = 16;
    private const int MaxRuns = 32;
    private const int MaxEngines = 32;

    private static IReadOnlyList<EngineChoice> Engines(JsonElement root)
    {
        var list = new List<EngineChoice>();
        foreach (var item in Items(root, "choices", MaxEngines))
        {
            var key = Str(item, "key");
            if (key.Length > 0)
            {
                list.Add(new EngineChoice(key, Str(item, "label") is { Length: > 0 } label ? label : key));
            }
        }

        return list;
    }

    private static ExchangeEvent ParseExchange(JsonElement root)
    {
        var response = Obj(root, "response");
        var metrics = Obj(root, "metrics");
        var actions = Obj(root, "actions");
        var sources = Items(response, "sources", MaxSources).Select(x => new AnswerSource(Str(x, "title"), Str(x, "url"))).ToList();
        var segments = Items(root, "segments", MaxSegments).Select(x => new AnswerSegment(Str(x, "kind"), Str(x, "text"), Str(x, "language"))).ToList();
        var run = Items(actions, "run", MaxRuns).Select(x => new RunCommand(Str(x, "language"), Str(x, "command"))).Where(r => r.Command.Trim().Length > 0).ToList();
        return new ExchangeEvent(
            Str(root, "origin") is { Length: > 0 } origin ? origin : "window",
            Str(root, "question"),
            Str(root, "searched"),
            Str(root, "note"),
            new AnswerResponse(
                Str(response, "model_id"),
                Str(response, "summary"),
                Str(response, "markdown"),
                NullableStr(response, "transcript"),
                NullableDouble(response, "confidence"),
                Str(response, "finish_reason") is { Length: > 0 } finish ? finish : "stop",
                sources),
            new AnswerMetrics(NullableStr(metrics, "tier"), Double(metrics, "server_ttft_ms")),
            segments,
            new ExchangeActions(Str(actions, "copy_fix"), Str(actions, "copy_command"), run));
    }

    private static JsonElement Obj(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Object ? v : default;

    private static IEnumerable<JsonElement> Items(JsonElement e, string name, int max)
    {
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty(name, out var array) || array.ValueKind != JsonValueKind.Array)
        {
            return [];
        }

        return array.EnumerateArray().Where(x => x.ValueKind == JsonValueKind.Object).Take(max).ToList();
    }

    private static double? NullableDouble(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;

    private static double Double(JsonElement e, string name) => NullableDouble(e, name) ?? 0;

    private static string Str(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    private static string? NullableStr(JsonElement e, string name) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool Bool(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    private static int Int(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var i) ? i : 0;
}
