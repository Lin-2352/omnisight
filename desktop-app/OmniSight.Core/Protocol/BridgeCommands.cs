using System.Text.Json;

namespace OmniSight.Core.Protocol;

/// <summary>Builds the one-line JSON commands the Python bridge understands (each has a "cmd" key).</summary>
public static class BridgeCommands
{
    /// <summary>The switches the "set" command accepts. "include_screen" is kept by the bridge; the rest drive the controller.</summary>
    public static readonly IReadOnlyList<string> Switches = ["memory", "speak", "search", "smart", "watch", "actions", "include_screen"];

    public static string Auth(string token, int pid) => Build(("cmd", "auth"), ("token", token), ("pid", pid));
    public static string Ping() => Build(("cmd", "ping"));
    public static string Ask(string text) => Build(("cmd", "ask"), ("text", text));
    public static string Capture() => Build(("cmd", "capture"));
    public static string Mic(string typed = "") => Build(("cmd", "mic"), ("typed", typed));
    public static string Engine(string key) => Build(("cmd", "engine"), ("key", key));
    public static string NodeToggle() => Build(("cmd", "node_toggle"));
    public static string Clear() => Build(("cmd", "clear"));
    public static string Settings() => Build(("cmd", "settings"));
    public static string StopSpeaking() => Build(("cmd", "stop_speaking"));
    public static string WatchPause() => Build(("cmd", "watch_pause"));
    public static string Answer(string id, bool yes) => Build(("cmd", "answer"), ("id", id), ("yes", yes));
    public static string SettingsGet() => Build(("cmd", "settings.get"));
    public static string SettingsApply(string overrideUrl, string localUrl) => Build(("cmd", "settings.apply"), ("override", overrideUrl), ("local_url", localUrl));
    public static string TestConnection() => Build(("cmd", "test_connection"));
    public static string RunCheck(string id, string cwd) => Build(("cmd", "run.check"), ("id", id), ("cwd", cwd));

    /// <summary>Asks Python to run the command the approval was opened with. The command text is deliberately not part of this message.</summary>
    public static string RunExecute(string id, string typed, string cwd, double timeoutSeconds) =>
        Build(("cmd", "run.execute"), ("id", id), ("typed", typed), ("cwd", cwd), ("timeout_s", timeoutSeconds));

    public static string RunCancel(string id) => Build(("cmd", "run.cancel"), ("id", id));
    public static string RunClose(string id) => Build(("cmd", "run.close"), ("id", id));
    public static string Run(string language, string command) => Build(("cmd", "run"), ("language", language), ("command", command));

    public static string Set(string name, bool on)
    {
        if (!Switches.Contains(name))
        {
            throw new ArgumentException($"unknown switch '{name}'", nameof(name));
        }

        return Build(("cmd", "set"), ("name", name), ("on", on));
    }

    private static string Build(params (string Key, object Value)[] fields)
    {
        var map = new Dictionary<string, object>(fields.Length);
        foreach (var (key, value) in fields)
        {
            map[key] = value;
        }

        // The wire format is one JSON object per line: it must never contain a raw newline.
        return JsonSerializer.Serialize(map);
    }
}
