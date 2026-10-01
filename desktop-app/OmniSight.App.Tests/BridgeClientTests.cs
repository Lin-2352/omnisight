using System.Net;
using System.Net.Sockets;
using System.Text;
using OmniSight.Core.Bridge;
using OmniSight.Core.Protocol;

namespace OmniSight.App.Tests;

/// <summary>A stand-in for the Python bridge: a loopback listener that speaks lines.</summary>
internal sealed class FakeServer : IAsyncDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private TcpClient? _client;
    private StreamReader? _reader;
    private StreamWriter? _writer;

    public FakeServer() => _listener.Start();

    public int Port => ((IPEndPoint)_listener.LocalEndpoint).Port;

    public async Task AcceptAsync()
    {
        _client = await _listener.AcceptTcpClientAsync();
        var stream = _client.GetStream();
        _reader = new StreamReader(stream, new UTF8Encoding(false));
        _writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" };
    }

    public async Task<string?> ReadLineAsync() => await _reader!.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(5));

    public Task SendAsync(string line) => _writer!.WriteLineAsync(line);

    public void CloseClient() => _client?.Close();

    public ValueTask DisposeAsync()
    {
        _client?.Close();
        _listener.Stop();
        return ValueTask.CompletedTask;
    }
}

public class BridgeClientTests
{
    private const string Token = "secret-token";

    private static async Task<(FakeServer Server, BridgeClient Client, Task Connect)> StartAsync(int pid = 99)
    {
        var server = new FakeServer();
        var client = new BridgeClient("127.0.0.1", server.Port, Token, pid);
        var connect = client.ConnectAsync(TimeSpan.FromSeconds(5));
        await server.AcceptAsync();
        return (server, client, connect);
    }

    [Fact]
    public async Task Connect_sends_the_token_and_pid_first_and_completes_on_hello()
    {
        var (server, client, connect) = await StartAsync(1234);
        await using var _ = server;
        await using var __ = client;
        var auth = await server.ReadLineAsync();
        Assert.Equal(BridgeCommands.Auth(Token, 1234), auth);
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        Assert.True(client.IsConnected);
    }

    [Fact]
    public async Task Events_are_parsed_and_delivered_in_order()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        var received = new List<BridgeEvent>();
        var done = new TaskCompletionSource();
        client.EventReceived += e =>
        {
            lock (received)
            {
                received.Add(e);
                if (received.Count == 4)
                {
                    done.TrySetResult();
                }
            }
        };
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await server.SendAsync("""{"event":"engine","key":"local_gpu"}""");
        await server.SendAsync("garbage that is not json");
        await server.SendAsync("""{"event":"notice","text":"hi","error":false}""");
        await connect;
        await done.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.IsType<HelloEvent>(received[0]);
        Assert.Equal(new EngineEvent("local_gpu"), received[1]);
        Assert.IsType<MalformedEvent>(received[2]);
        Assert.Equal(new NoticeEvent("hi", false), received[3]);
    }

    [Fact]
    public async Task Sent_commands_arrive_as_one_line_each()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        await client.SendAsync(BridgeCommands.Ask("line one\nline two"));
        await client.SendAsync(BridgeCommands.Capture());
        Assert.Equal(BridgeCommands.Ask("line one\nline two"), await server.ReadLineAsync());
        Assert.Equal(BridgeCommands.Capture(), await server.ReadLineAsync());
    }

    [Fact]
    public async Task Concurrent_sends_never_interleave()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        var text = new string('x', 5000);
        await Task.WhenAll(Enumerable.Range(0, 30).Select(i => client.SendAsync(BridgeCommands.Ask($"{i}:{text}"))));
        var seen = new HashSet<int>();
        for (var i = 0; i < 30; i++)
        {
            var line = await server.ReadLineAsync();
            Assert.NotNull(line);
            using var doc = System.Text.Json.JsonDocument.Parse(line!);
            var payload = doc.RootElement.GetProperty("text").GetString()!;
            Assert.EndsWith(text, payload);
            seen.Add(int.Parse(payload.Split(':')[0]));
        }

        Assert.Equal(30, seen.Count);
    }

    [Fact]
    public async Task A_refused_token_makes_connect_throw()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        await server.ReadLineAsync();
        server.CloseClient();  // what the Python bridge does for a wrong token
        var ex = await Assert.ThrowsAsync<BridgeException>(() => connect);
        Assert.Contains("refused", ex.Message);
    }

    [Fact]
    public async Task A_silent_server_times_out()
    {
        await using var server = new FakeServer();
        await using var client = new BridgeClient("127.0.0.1", server.Port, Token, 1);
        var connect = client.ConnectAsync(TimeSpan.FromMilliseconds(300));
        await server.AcceptAsync();
        var ex = await Assert.ThrowsAsync<BridgeException>(() => connect);
        Assert.Contains("in time", ex.Message);
    }

    [Fact]
    public async Task A_wrong_protocol_version_is_refused()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":99}""");
        var ex = await Assert.ThrowsAsync<BridgeException>(() => connect);
        Assert.Contains("protocol", ex.Message);
    }

    [Fact]
    public async Task Nothing_listening_throws_a_bridge_exception()
    {
        var port = new FakeServer();
        var closed = port.Port;
        await port.DisposeAsync();
        await using var client = new BridgeClient("127.0.0.1", closed, Token, 1);
        await Assert.ThrowsAsync<BridgeException>(() => client.ConnectAsync(TimeSpan.FromSeconds(3)));
    }

    [Fact]
    public async Task The_server_going_away_raises_disconnected_once()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        var reasons = new List<string>();
        var raised = new TaskCompletionSource();
        client.Disconnected += reason =>
        {
            lock (reasons)
            {
                reasons.Add(reason);
            }

            raised.TrySetResult();
        };
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        server.CloseClient();
        await raised.Task.WaitAsync(TimeSpan.FromSeconds(5));
        await Task.Delay(100);
        Assert.Single(reasons);
        await Assert.ThrowsAsync<BridgeException>(() => client.SendAsync(BridgeCommands.Ping()));
    }

    [Theory]
    [InlineData("two\nlines")]
    [InlineData("carriage\rreturn")]
    public async Task A_command_with_a_newline_is_refused_before_it_reaches_the_wire(string raw)
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        await Assert.ThrowsAsync<ArgumentException>(() => client.SendAsync(raw));
    }

    [Fact]
    public async Task An_oversized_command_is_refused()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        await Assert.ThrowsAsync<ArgumentException>(() => client.SendAsync(new string('a', BridgeProtocol.MaxLineBytes + 1)));
    }

    [Fact]
    public async Task A_subscriber_that_throws_does_not_stop_later_events()
    {
        var (server, client, connect) = await StartAsync();
        await using var _ = server;
        await using var __ = client;
        var good = new TaskCompletionSource<BridgeEvent>();
        var calls = 0;
        client.EventReceived += e =>
        {
            if (Interlocked.Increment(ref calls) == 2)
            {
                good.TrySetResult(e);
            }

            if (e is HelloEvent)
            {
                throw new InvalidOperationException("faulty subscriber");
            }
        };
        await server.ReadLineAsync();
        await server.SendAsync("""{"event":"hello","protocol":1}""");
        await connect;
        await server.SendAsync("""{"event":"clear"}""");
        Assert.IsType<ClearEvent>(await good.Task.WaitAsync(TimeSpan.FromSeconds(5)));
    }
}
