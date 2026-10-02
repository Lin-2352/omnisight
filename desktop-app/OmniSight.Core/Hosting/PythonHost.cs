using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;

namespace OmniSight.Core.Hosting;

public class PythonHostException(string message) : Exception(message);

/// <summary>The Qt app (or another OmniSight) already holds the single-instance lock, so the Python client exited at once with code 0.</summary>
public sealed class AlreadyRunningException() : PythonHostException(
    "OmniSight is already running (look for its icon in the system tray). Choose Exit there, then start this app again.");


public sealed record PythonHostOptions(
    string RepoRoot,
    string PythonExe,
    IReadOnlyList<string>? ExtraArgs = null,
    IReadOnlyDictionary<string, string>? Environment = null,
    TimeSpan? StartupTimeout = null);

/// <summary>Starts the Python client in bridge mode and learns its port. The token goes over stdin, never a command line.</summary>
public sealed class PythonHost : IAsyncDisposable
{
    public const string AnnouncePrefix = "OMNISIGHT_BRIDGE ";

    private readonly Process _process;
    private readonly Queue<string> _stderrTail;
    private readonly object _tailLock;

    private PythonHost(Process process, int port, string token, Queue<string> stderrTail, object tailLock)
    {
        _process = process;
        Port = port;
        Token = token;
        _stderrTail = stderrTail;
        _tailLock = tailLock;
    }

    public int Port { get; }
    public string Token { get; }
    public int ProcessId => _process.Id;
    public bool HasExited => _process.HasExited;

    /// <summary>The last few lines the Python client wrote to stderr (for error messages).</summary>
    public string Diagnostics
    {
        get
        {
            lock (_tailLock)
            {
                return string.Join(Environment.NewLine, _stderrTail);
            }
        }
    }

    public static string NewToken() => RandomNumberGenerator.GetHexString(48, lowercase: true);

    /// <summary>Parse "OMNISIGHT_BRIDGE {"port":N,"protocol":1}". False for anything else.</summary>
    public static bool TryParseAnnouncement(string? line, out int port)
    {
        port = 0;
        if (line is null || !line.StartsWith(AnnouncePrefix, StringComparison.Ordinal))
        {
            return false;
        }

        try
        {
            using var doc = JsonDocument.Parse(line[AnnouncePrefix.Length..]);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("port", out var p) || p.ValueKind != JsonValueKind.Number || !p.TryGetInt32(out var value)
                || value is < 1 or > 65535
                || !root.TryGetProperty("protocol", out var v) || v.ValueKind != JsonValueKind.Number || !v.TryGetInt32(out var protocol)
                || protocol != Protocol.BridgeProtocol.Version)
            {
                return false;
            }

            port = value;
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    public static async Task<PythonHost> StartAsync(PythonHostOptions options, CancellationToken ct = default)
    {
        var main = Path.Combine(options.RepoRoot, "desktop-client", "main.py");
        if (!File.Exists(main))
        {
            throw new PythonHostException($"cannot find {main}");
        }

        var info = new ProcessStartInfo(options.PythonExe)
        {
            WorkingDirectory = options.RepoRoot,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        info.ArgumentList.Add("-u");
        info.ArgumentList.Add(main);
        info.ArgumentList.Add("--bridge");
        foreach (var arg in options.ExtraArgs ?? [])
        {
            info.ArgumentList.Add(arg);
        }

        foreach (var (key, value) in options.Environment ?? new Dictionary<string, string>())
        {
            info.Environment[key] = value;
        }

        Process process;
        try
        {
            process = Process.Start(info) ?? throw new PythonHostException("could not start Python");
        }
        catch (System.ComponentModel.Win32Exception ex)
        {
            throw new PythonHostException($"could not start Python ({options.PythonExe}): {ex.Message}");
        }

        ProcessJob.Assign(process);  // only the Python client dies with this app; the app itself stays out of the job
        var token = NewToken();
        var tail = new Queue<string>();
        var tailLock = new object();
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is null)
            {
                return;
            }

            lock (tailLock)
            {
                tail.Enqueue(e.Data);
                while (tail.Count > 40)
                {
                    tail.Dequeue();
                }
            }
        };
        process.BeginErrorReadLine();

        try
        {
            try
            {
                await process.StandardInput.WriteLineAsync(token.AsMemory(), ct).ConfigureAwait(false);
                await process.StandardInput.FlushAsync(ct).ConfigureAwait(false);
                process.StandardInput.Close();
            }
            catch (IOException)
            {
                // The child may already be gone (for example "already running"); the loop below finds out why from its exit.
            }

            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(options.StartupTimeout ?? TimeSpan.FromSeconds(60));
            int port;
            while (true)
            {
                var line = await process.StandardOutput.ReadLineAsync(timeout.Token).ConfigureAwait(false);
                if (line is null)
                {
                    await Task.Delay(100, CancellationToken.None).ConfigureAwait(false);
                    string detail;
                    lock (tailLock)
                    {
                        detail = string.Join(" | ", tail);
                    }

                    if (process.HasExited && process.ExitCode == 0)
                    {
                        throw new AlreadyRunningException();
                    }

                    throw new PythonHostException($"the Python client exited before it was ready (exit {(process.HasExited ? process.ExitCode : -1)}). {detail}".Trim());
                }

                if (TryParseAnnouncement(line, out port))
                {
                    break;
                }
            }

            _ = Task.Run(() => DrainAsync(process));
            return new PythonHost(process, port, token, tail, tailLock);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            Kill(process);
            throw new PythonHostException("the Python client did not become ready in time");
        }
        catch
        {
            Kill(process);
            throw;
        }
    }

    private static async Task DrainAsync(Process process)
    {
        try
        {
            while (await process.StandardOutput.ReadLineAsync().ConfigureAwait(false) is not null)
            {
                // keep the pipe from filling; nothing else is sent on stdout after the announcement
            }
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException or ObjectDisposedException)
        {
            // process gone
        }
    }

    private static void Kill(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            // already gone
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (!_process.HasExited)
        {
            // The bridge exits by itself when the app disconnects; give it a moment, then make sure.
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            try
            {
                await _process.WaitForExitAsync(cts.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                Kill(_process);
            }
        }

        _process.Dispose();
    }
}
