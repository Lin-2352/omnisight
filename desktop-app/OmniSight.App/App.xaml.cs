using System.Windows;
using OmniSight.Core.Bridge;
using OmniSight.Core.Hosting;
using OmniSight.Core.Logging;
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
        AppLog.Info("OmniSight app starting");
        // A crash or a swallowed error should leave a trail in app.log.
        DispatcherUnhandledException += (_, args) =>
        {
            AppLog.Error("unhandled UI exception", args.Exception);
            _viewModel.Apply(new NoticeEvent("Something went wrong in the window. Details are in the app log (Options, Open logs).", true));
            args.Handled = true;
        };
        AppDomain.CurrentDomain.UnhandledException += (_, args) => AppLog.Error("unhandled exception (the app is closing)", args.ExceptionObject as Exception);
        TaskScheduler.UnobservedTaskException += (_, args) =>
        {
            AppLog.Error("unobserved task exception", args.Exception);
            args.SetObserved();
        };
        _viewModel.CommandRequested += OnCommand;
        _window = new MainWindow(_viewModel, StartEngineAsync);
        MainWindow = _window;
        ApplyTheme();
        _window.Show();
        await StartEngineAsync();
    }

    /// <summary>Follows the Windows theme. <c>OMNISIGHT_THEME=light</c> or <c>dark</c> forces one (for checking the design).</summary>
    private static void ApplyTheme()
    {
        switch (Environment.GetEnvironmentVariable("OMNISIGHT_THEME")?.Trim().ToLowerInvariant())
        {
            case "light":
                ApplicationThemeManager.Apply(ApplicationTheme.Light);
                break;
            case "dark":
                ApplicationThemeManager.Apply(ApplicationTheme.Dark);
                break;
            default:
                ApplicationThemeManager.ApplySystemTheme();
                break;
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        AppLog.Info($"OmniSight app exiting (code {e.ApplicationExitCode})");
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
            AppLog.Info($"engine started (python pid {_host.ProcessId}, bridge port {_host.Port})");
            _client = new BridgeClient("127.0.0.1", _host.Port, _host.Token, Environment.ProcessId);
            _client.EventReceived += evt => Dispatcher.BeginInvoke(() => OnEvent(evt));
            _client.Disconnected += reason => Dispatcher.BeginInvoke(() => OnDisconnected(reason));
            await _client.ConnectAsync();
        }
        catch (Exception ex) when (ex is PythonHostException or BridgeException)
        {
            var detail = _host?.Diagnostics;
            AppLog.Error("could not start or reach the engine", ex);
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
        AppLog.Warning($"engine connection ended: {reason}");
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
