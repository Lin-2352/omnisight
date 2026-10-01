using System.Text.Json;
using OmniSight.Core.Protocol;

namespace OmniSight.App.Tests;

public class ProtocolTests
{
    [Fact]
    public void Hello_carries_the_protocol_version()
    {
        var evt = Assert.IsType<HelloEvent>(BridgeProtocol.ParseEvent("""{"event":"hello","protocol":1}"""));
        Assert.Equal(1, evt.Protocol);
    }

    [Fact]
    public void State_node_notice_and_the_rest_parse_into_typed_events()
    {
        Assert.Equal(new StateEvent("analyzing", "Analyzing…"), BridgeProtocol.ParseEvent("""{"event":"state","state":"analyzing","message":"Analyzing…"}"""));
        Assert.Equal(
            new NodeEvent("ready", "", "GPU: RTX", true, "local_gpu"),
            BridgeProtocol.ParseEvent("""{"event":"node","state":"ready","message":"","device":"GPU: RTX","owned":true,"engine":"local_gpu"}"""));
        Assert.Equal(new NoticeEvent("careful", true), BridgeProtocol.ParseEvent("""{"event":"notice","text":"careful","error":true}"""));
        Assert.Equal(new ProgressEvent("Searching the web…"), BridgeProtocol.ParseEvent("""{"event":"progress","text":"Searching the web…"}"""));
        Assert.Equal(new EngineEvent("local_cpu"), BridgeProtocol.ParseEvent("""{"event":"engine","key":"local_cpu"}"""));
        Assert.Equal(new SwitchEvent("memory", false), BridgeProtocol.ParseEvent("""{"event":"switch","name":"memory","on":false}"""));
        Assert.Equal(new SpeakAvailableEvent(true), BridgeProtocol.ParseEvent("""{"event":"speak_available","available":true}"""));
        Assert.Equal(new SpeakingEvent(true), BridgeProtocol.ParseEvent("""{"event":"speaking","on":true}"""));
        Assert.Equal(new WatchEvent("Watching", true), BridgeProtocol.ParseEvent("""{"event":"watch","text":"Watching","paused":true}"""));
        Assert.IsType<ClearEvent>(BridgeProtocol.ParseEvent("""{"event":"clear"}"""));
        Assert.IsType<ShowEvent>(BridgeProtocol.ParseEvent("""{"event":"show"}"""));
        Assert.IsType<PongEvent>(BridgeProtocol.ParseEvent("""{"event":"pong"}"""));
        Assert.Equal(new ErrorEvent("bad"), BridgeProtocol.ParseEvent("""{"event":"error","message":"bad"}"""));
    }

    [Fact]
    public void A_node_without_a_device_has_a_null_device()
    {
        var node = Assert.IsType<NodeEvent>(BridgeProtocol.ParseEvent("""{"event":"node","state":"stopped","message":"","device":null,"owned":false,"engine":"auto"}"""));
        Assert.Null(node.Device);
    }

    [Fact]
    public void An_exchange_keeps_its_response_and_metrics_as_json_that_outlives_parsing()
    {
        var evt = Assert.IsType<ExchangeEvent>(BridgeProtocol.ParseEvent(
            """{"event":"exchange","question":"why?","searched":"q","note":"n","response":{"summary":"S","code_blocks":[]},"metrics":{"tier":"local"}}"""));
        Assert.Equal("why?", evt.Question);
        Assert.Equal("S", evt.Response.GetProperty("summary").GetString());
        Assert.Equal("local", evt.Metrics.GetProperty("tier").GetString());
    }

    [Fact]
    public void Unicode_survives()
    {
        var evt = Assert.IsType<NoticeEvent>(BridgeProtocol.ParseEvent("""{"event":"notice","text":"Größe → 日本語 “q”","error":false}"""));
        Assert.Equal("Größe → 日本語 “q”", evt.Text);
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    [InlineData("{broken")]
    [InlineData("[1,2]")]
    [InlineData("42")]
    [InlineData("null")]
    [InlineData("{}")]
    [InlineData("""{"event":5}""")]
    [InlineData("""{"event":null}""")]
    public void Garbage_is_malformed_not_an_exception(string line)
    {
        Assert.IsType<MalformedEvent>(BridgeProtocol.ParseEvent(line));
    }

    [Fact]
    public void An_event_from_a_newer_python_client_is_unknown_not_an_error()
    {
        var evt = Assert.IsType<UnknownEvent>(BridgeProtocol.ParseEvent("""{"event":"from_the_future","x":1}"""));
        Assert.Equal("from_the_future", evt.Name);
    }

    [Fact]
    public void Missing_or_wrongly_typed_fields_fall_back_to_safe_defaults()
    {
        Assert.Equal(new NoticeEvent("", false), BridgeProtocol.ParseEvent("""{"event":"notice"}"""));
        Assert.Equal(new SwitchEvent("", false), BridgeProtocol.ParseEvent("""{"event":"switch","name":7,"on":"yes"}"""));
        Assert.Equal(new HelloEvent(0), BridgeProtocol.ParseEvent("""{"event":"hello","protocol":"one"}"""));
        var exchange = Assert.IsType<ExchangeEvent>(BridgeProtocol.ParseEvent("""{"event":"exchange"}"""));
        Assert.Equal(JsonValueKind.Object, exchange.Response.ValueKind);
    }

    [Fact]
    public void Commands_are_single_line_json_with_the_expected_shape()
    {
        AssertJson(BridgeCommands.Auth("tok", 42), ("cmd", "auth"), ("token", "tok"), ("pid", 42));
        AssertJson(BridgeCommands.Ping(), ("cmd", "ping"));
        AssertJson(BridgeCommands.Ask("why?"), ("cmd", "ask"), ("text", "why?"));
        AssertJson(BridgeCommands.Capture(), ("cmd", "capture"));
        AssertJson(BridgeCommands.Mic("typed"), ("cmd", "mic"), ("typed", "typed"));
        AssertJson(BridgeCommands.Engine("local_gpu"), ("cmd", "engine"), ("key", "local_gpu"));
        AssertJson(BridgeCommands.NodeToggle(), ("cmd", "node_toggle"));
        AssertJson(BridgeCommands.Clear(), ("cmd", "clear"));
        AssertJson(BridgeCommands.Settings(), ("cmd", "settings"));
        AssertJson(BridgeCommands.StopSpeaking(), ("cmd", "stop_speaking"));
        AssertJson(BridgeCommands.WatchPause(), ("cmd", "watch_pause"));
        AssertJson(BridgeCommands.Run("powershell", "Get-Date"), ("cmd", "run"), ("language", "powershell"), ("command", "Get-Date"));
        AssertJson(BridgeCommands.Set("search", true), ("cmd", "set"), ("name", "search"), ("on", true));
    }

    [Theory]
    [InlineData("line one\nline two")]
    [InlineData("carriage\rreturn")]
    [InlineData("quote \" and backslash \\ and tab \t")]
    [InlineData("emoji 🙂 and 日本語")]
    public void Hostile_text_never_breaks_the_one_line_framing(string text)
    {
        var line = BridgeCommands.Ask(text);
        Assert.DoesNotContain('\n', line);
        Assert.DoesNotContain('\r', line);
        using var doc = JsonDocument.Parse(line);
        Assert.Equal(text, doc.RootElement.GetProperty("text").GetString());
    }

    [Fact]
    public void Only_known_switches_can_be_built()
    {
        foreach (var name in BridgeCommands.Switches)
        {
            Assert.Contains("\"on\":false", BridgeCommands.Set(name, false));
        }

        Assert.Throws<ArgumentException>(() => BridgeCommands.Set("bogus", true));
        Assert.Throws<ArgumentException>(() => BridgeCommands.Set("", true));
    }

    private static void AssertJson(string line, params (string Key, object Value)[] expected)
    {
        Assert.DoesNotContain('\n', line);
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        Assert.Equal(expected.Length, root.EnumerateObject().Count());
        foreach (var (key, value) in expected)
        {
            var element = root.GetProperty(key);
            switch (value)
            {
                case string s:
                    Assert.Equal(s, element.GetString());
                    break;
                case int i:
                    Assert.Equal(i, element.GetInt32());
                    break;
                case bool b:
                    Assert.Equal(b, element.GetBoolean());
                    break;
                default:
                    throw new InvalidOperationException("unsupported test value");
            }
        }
    }
}
