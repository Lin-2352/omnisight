using System.Windows;
using OmniSight.Core.Bridge;
using OmniSight.Core.Hosting;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;
using Wpf.Ui.Appearance;

namespace OmniSight.App;

public partial class App : Application
{
    private readonly ShellViewModel _viewModel = new();
    private MainWindow? _window;
    private PythonHost? _host;
    private BridgeClient? _client;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        _viewModel.CommandRequested += OnCommand;
        _window = new MainWindow(_viewModel, StartEngineAsync);
        MainWindow = _window;
        ApplicationThemeManager.ApplySystemTheme();
        _window.Show();
        await StartEngineAsync();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // Disposing the client makes the Python client exit; the job object kills it if it ever does not.
        try
        {
            Task.Run(StopEngineAsync).Wait(TimeSpan.FromSeconds(8));  // off the UI thread: its continuations need the dispatcher we block here
        }
        catch (Exception)
        {
            // exiting anyway
        }

        base.OnExit(e);
    }

    /// <summary>Whatever the window asks for goes to the Python client; if it cannot be reached the person is told.</summary>
    private async void OnCommand(string json)
    {
        try
        {
            var client = _client;
            if (client is not { IsConnected: true })
            {
                _viewModel.Apply(new NoticeEvent("The OmniSight engine is not running. Use Restart engine.", true));
                return;
            }

            await client.SendAsync(json);
        }
        catch (BridgeException ex)
        {
            _viewModel.Apply(new NoticeEvent(ex.Message, true));
        }
    }

    private async Task StartEngineAsync()
    {
        await StopEngineAsync();
        _viewModel.SetStarting("Starting the OmniSight engine…");
        _viewModel.NoticeText = "";
        var root = Locator.FindRepoRoot(AppContext.BaseDirectory);
        if (root is null)
        {
            _viewModel.SetFailed($"Cannot find the OmniSight folder. Set {Locator.RootVariable} to the folder that contains desktop-client.");
            return;
        }

        try
        {
            _host = await PythonHost.StartAsync(new PythonHostOptions(root, Locator.FindPython(root), Locator.AppArguments));
            _client = new BridgeClient("127.0.0.1", _host.Port, _host.Token, Environment.ProcessId);
            _client.EventReceived += evt => Dispatcher.BeginInvoke(() => OnEvent(evt));
            _client.Disconnected += reason => Dispatcher.BeginInvoke(() => OnDisconnected(reason));
            await _client.ConnectAsync();
        }
        catch (Exception ex) when (ex is PythonHostException or BridgeException)
        {
            var detail = _host?.Diagnostics;
            _viewModel.SetFailed(string.IsNullOrWhiteSpace(detail) ? ex.Message : $"{ex.Message}  {detail}");
            await StopEngineAsync();
        }
    }

    private void OnEvent(BridgeEvent evt)
    {
        _viewModel.Apply(evt);
        if (evt is ShowEvent && _window is not null)
        {
            _window.WindowState = WindowState.Normal;
            _window.Activate();
        }
    }

    private void OnDisconnected(string reason)
    {
        if (_viewModel.Connection != ConnectionState.Failed)
        {
            _viewModel.SetDisconnected($"The engine stopped ({reason}). Use Restart engine.");
        }
    }

    private async Task StopEngineAsync()
    {
        var client = Interlocked.Exchange(ref _client, null);
        var host = Interlocked.Exchange(ref _host, null);
        if (client is not null)
        {
            await client.DisposeAsync().ConfigureAwait(false);
        }

        if (host is not null)
        {
            await host.DisposeAsync().ConfigureAwait(false);
        }
    }
}
