using System.Net.Sockets;
using System.Text;
using OmniSight.Core.Protocol;

namespace OmniSight.Core.Bridge;

public sealed class BridgeException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>The app's end of the loopback socket to the Python client: one JSON object per line each way.</summary>
public sealed class BridgeClient : IAsyncDisposable
{
    private readonly string _host;
    private readonly int _port;
    private readonly string _token;
    private readonly int _pid;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly CancellationTokenSource _lifetime = new();
    private readonly TaskCompletionSource<HelloEvent> _hello = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private Task? _reader;
    private int _disconnected;
    private int _disposed;

    public BridgeClient(string host, int port, string token, int pid)
    {
        _host = host;
        _port = port;
        _token = token;
        _pid = pid;
    }

    /// <summary>Raised on a thread-pool thread for every event; marshal to the UI thread before touching controls.</summary>
    public event Action<BridgeEvent>? EventReceived;

    /// <summary>Raised once when the connection ends (the reason is for the log, not for users).</summary>
    public event Action<string>? Disconnected;

    public bool IsConnected => _tcp is { Connected: true } && _disconnected == 0;

    /// <summary>Connect, authenticate, and wait for the Python client's hello. Throws <see cref="BridgeException"/> when refused.</summary>
    public async Task ConnectAsync(TimeSpan? timeout = null, CancellationToken ct = default)
    {
        var limit = timeout ?? TimeSpan.FromSeconds(10);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, _lifetime.Token);
        linked.CancelAfter(limit);
        try
        {
            _tcp = new TcpClient { NoDelay = true };
            await _tcp.ConnectAsync(_host, _port, linked.Token).ConfigureAwait(false);
            _stream = _tcp.GetStream();
            _reader = Task.Run(() => ReadLoopAsync(_stream));
            await SendAsync(BridgeCommands.Auth(_token, _pid), linked.Token).ConfigureAwait(false);
            var hello = await _hello.Task.WaitAsync(linked.Token).ConfigureAwait(false);
            if (hello.Protocol != BridgeProtocol.Version)
            {
                throw new BridgeException($"the Python client speaks protocol {hello.Protocol}, this app speaks {BridgeProtocol.Version}");
            }
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            await DisposeAsync().ConfigureAwait(false);
            throw new BridgeException("the Python client did not answer in time");
        }
        catch (SocketException ex)
        {
            await DisposeAsync().ConfigureAwait(false);
            throw new BridgeException("could not reach the Python client", ex);
        }
        catch (BridgeException)
        {
            await DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    public async Task SendAsync(string commandJson, CancellationToken ct = default)
    {
        if (commandJson.Contains('\n') || commandJson.Contains('\r'))
        {
            throw new ArgumentException("a command must be a single line", nameof(commandJson));
        }

        var bytes = Encoding.UTF8.GetBytes(commandJson + "\n");
        if (bytes.Length > BridgeProtocol.MaxLineBytes)
        {
            throw new ArgumentException("command is too large", nameof(commandJson));
        }

        var stream = _stream ?? throw new BridgeException("not connected");
        if (Volatile.Read(ref _disconnected) != 0 || Volatile.Read(ref _disposed) != 0)
        {
            throw new BridgeException("the Python client is gone");
        }

        await _writeLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            await stream.WriteAsync(bytes, ct).ConfigureAwait(false);
            await stream.FlushAsync(ct).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException)
        {
            RaiseDisconnected("write failed");
            throw new BridgeException("the Python client is gone", ex);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task ReadLoopAsync(NetworkStream stream)
    {
        try
        {
            using var reader = new StreamReader(stream, Encoding.UTF8, false, 64 * 1024, leaveOpen: true);
            while (!_lifetime.IsCancellationRequested)
            {
                var line = await reader.ReadLineAsync(_lifetime.Token).ConfigureAwait(false);
                if (line is null)
                {
                    break;
                }

                if (line.Length == 0)
                {
                    continue;
                }

                var evt = line.Length > BridgeProtocol.MaxLineBytes ? new MalformedEvent("line too long") : BridgeProtocol.ParseEvent(line);
                if (evt is HelloEvent hello)
                {
                    _hello.TrySetResult(hello);
                }

                Dispatch(evt);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or OperationCanceledException or SocketException)
        {
            // the connection ended; reported below
        }

        _hello.TrySetException(new BridgeException("the Python client refused or closed the connection"));
        RaiseDisconnected("connection closed");
    }

    private void Dispatch(BridgeEvent evt)
    {
        try
        {
            EventReceived?.Invoke(evt);
        }
        catch (Exception)
        {
            // a faulty subscriber must not kill the reader
        }
    }

    private void RaiseDisconnected(string reason)
    {
        if (Interlocked.Exchange(ref _disconnected, 1) == 0)
        {
            Disconnected?.Invoke(reason);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;  // safe to call twice: a failed ConnectAsync disposes, and the caller's "await using" disposes again
        }

        await _lifetime.CancelAsync().ConfigureAwait(false);
        try
        {
            _tcp?.Close();
        }
        catch (ObjectDisposedException)
        {
            // already closed
        }

        if (_reader is not null)
        {
            await Task.WhenAny(_reader, Task.Delay(TimeSpan.FromSeconds(2))).ConfigureAwait(false);
        }

        RaiseDisconnected("disposed");
        _lifetime.Dispose();
        _writeLock.Dispose();
    }
}
