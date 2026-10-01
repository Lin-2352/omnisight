using System.ComponentModel;
using OmniSight.App.Interop;
using OmniSight.Core.ViewModels;
using Wpf.Ui.Controls;

namespace OmniSight.App;

public partial class MainWindow : FluentWindow
{
    private readonly ShellViewModel _viewModel;
    private readonly Func<Task> _restart;
    private readonly Func<Task> _ping;

    public MainWindow(ShellViewModel viewModel, Func<Task> restart, Func<Task> ping)
    {
        _viewModel = viewModel;
        _restart = restart;
        _ping = ping;
        DataContext = viewModel;
        InitializeComponent();
        SourceInitialized += (_, _) =>
        {
            // Never let OmniSight capture itself. If Windows refuses, say so instead of pretending.
            if (!WindowCapture.Allowed && !WindowCapture.Exclude(this))
            {
                _viewModel.Apply(new Core.Protocol.NoticeEvent("This window could not be hidden from screen capture, so OmniSight may see its own answers.", true));
            }
        };
        _viewModel.PropertyChanged += OnViewModelChanged;
        _viewModel.Log.CollectionChanged += (_, _) =>
        {
            if (EventLog.Items.Count > 0)
            {
                EventLog.ScrollIntoView(EventLog.Items[^1]);
            }
        };
        SyncNotice();
    }

    private void OnViewModelChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(ShellViewModel.NoticeText) or nameof(ShellViewModel.NoticeIsError))
        {
            SyncNotice();
        }
    }

    private void SyncNotice()
    {
        Notice.IsOpen = !string.IsNullOrEmpty(_viewModel.NoticeText);
        Notice.Severity = _viewModel.NoticeIsError ? InfoBarSeverity.Error : InfoBarSeverity.Warning;
    }

    private async void OnPing(object sender, System.Windows.RoutedEventArgs e) => await SafeAsync(_ping);

    private async void OnRetry(object sender, System.Windows.RoutedEventArgs e) => await SafeAsync(_restart);

    private async Task SafeAsync(Func<Task> action)
    {
        try
        {
            await action();
        }
        catch (Exception ex)
        {
            _viewModel.SetFailed(ex.Message);
        }
    }
}
