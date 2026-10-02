using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using OmniSight.App.Interop;
using OmniSight.App.Views;
using OmniSight.Core.Protocol;
using OmniSight.Core.ViewModels;
using Wpf.Ui.Controls;

namespace OmniSight.App;

public partial class MainWindow : FluentWindow
{
    private readonly ShellViewModel _viewModel;
    private readonly Func<Task> _restart;
    private WindowSnapshot? _snapshot;

    public MainWindow(ShellViewModel viewModel, Func<Task> restart)
    {
        _viewModel = viewModel;
        _restart = restart;
        DataContext = viewModel;
        InitializeComponent();

        SourceInitialized += (_, _) =>
        {
            // Never let OmniSight capture itself. If Windows refuses, say so instead of pretending.
            if (!WindowCapture.Allowed && !WindowCapture.Exclude(this))
            {
                _viewModel.Apply(new NoticeEvent("This window could not be hidden from screen capture, so OmniSight may see its own answers.", true));
            }
        };
        _viewModel.PropertyChanged += OnViewModelChanged;
        _viewModel.Conversation.CollectionChanged += (_, _) => Dispatcher.BeginInvoke(() => Scroller.ScrollToEnd(), System.Windows.Threading.DispatcherPriority.Background);
        _viewModel.CopyRequested += text => _ = CopyAsync(text);
        _viewModel.OpenLinkRequested += OpenInBrowser;
        _viewModel.ConfirmRequested += confirm => _ = ConfirmAsync(confirm);
        _viewModel.OpenFolderRequested += OpenFolder;
        _viewModel.RunApprovalOpened += _ => Dispatcher.BeginInvoke(() => ConfirmBox.Focus(), System.Windows.Threading.DispatcherPriority.Input);
        RichText.LinkClicked += OnRichTextLink;
        Closed += (_, _) => RichText.LinkClicked -= OnRichTextLink;
        SyncNotice();
        Loaded += (_, _) =>
        {
            AskBox.Focus();
            _snapshot = WindowSnapshot.StartIfRequested(this);
        };
    }

    private void OnRichTextLink(string url) => _viewModel.OpenLink(url);

    /// <summary>The options pane's width including its margin; the window grows by this much so the answers keep their room.</summary>
    private const double PaneWidth = 384;

    private void OnViewModelChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(ShellViewModel.IsPaneOpen) && WindowState == WindowState.Normal)
        {
            var work = SystemParameters.WorkArea;
            Width = Math.Clamp(Width + (_viewModel.IsPaneOpen ? PaneWidth : -PaneWidth), MinWidth, work.Width);
            if (Left + Width > work.Right)
            {
                Left = Math.Max(work.Left, work.Right - Width);
            }
        }

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

    // -- composer ---------------------------------------------------------------------------

    private void OnAskKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && Keyboard.Modifiers == ModifierKeys.None)
        {
            e.Handled = true;
            _viewModel.Send();
        }
    }

    private void OnSend(object sender, RoutedEventArgs e) => _viewModel.Send();

    private void OnCapture(object sender, RoutedEventArgs e) => _viewModel.Capture();

    private void OnMic(object sender, RoutedEventArgs e) => _viewModel.Mic();

    private void OnClear(object sender, RoutedEventArgs e) => _viewModel.Clear();

    private void OnNode(object sender, RoutedEventArgs e) => _viewModel.ToggleNode();

    private void OnStopVoice(object sender, RoutedEventArgs e) => _viewModel.StopVoice();

    private void OnPauseWatch(object sender, RoutedEventArgs e) => _viewModel.PauseWatch();

    private void OnApplySettings(object sender, RoutedEventArgs e) => _viewModel.ApplySettings();

    private void OnTestConnection(object sender, RoutedEventArgs e) => _viewModel.TestConnection();

    private void OnOpenLogs(object sender, RoutedEventArgs e) => _viewModel.OpenLogs();

    private async void OnRestart(object sender, RoutedEventArgs e)
    {
        try
        {
            await _restart();
        }
        catch (Exception ex)
        {
            _viewModel.SetFailed(ex.Message);
        }
    }

    // -- cards ---------------------------------------------------------------------------------

    private void OnCopyFix(object sender, RoutedEventArgs e)
    {
        if (CardOf(sender) is { } card)
        {
            _viewModel.CopyText(card.CopyFix);
        }
    }

    private void OnCopyCommand(object sender, RoutedEventArgs e)
    {
        if (CardOf(sender) is { } card)
        {
            _viewModel.CopyText(card.CopyCommand);
        }
    }

    private void OnCopyBlock(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.DataContext is CodeCardBlock block)
        {
            _viewModel.CopyText(block.Code);
        }
    }

    private void OnRunClick(object sender, RoutedEventArgs e)
    {
        // Only asks Python to open its approval dialog; the view model refuses a button that is not shown on that card.
        if ((sender as FrameworkElement)?.DataContext is RunButton button && CardOf(sender) is { } card)
        {
            _viewModel.RequestRun(card, button);
        }
    }

    private void OnSourceClick(object sender, RoutedEventArgs e)
    {
        if (sender is Hyperlink { Tag: string url })
        {
            _viewModel.OpenLink(url);
        }
    }

    /// <summary>The card a control sits in (the nearest ancestor whose data is an <see cref="ExchangeCard"/>).</summary>
    private static ExchangeCard? CardOf(object sender)
    {
        DependencyObject? node = sender as DependencyObject;
        while (node is not null)
        {
            if (node is FrameworkElement { DataContext: ExchangeCard card })
            {
                return card;
            }

            node = node is Visual or System.Windows.Media.Media3D.Visual3D ? VisualTreeHelper.GetParent(node) : LogicalTreeHelper.GetParent(node);
        }

        return null;
    }

    // -- the "Run command" approval ----------------------------------------------------------------

    private void OnRunExecute(object sender, RoutedEventArgs e) => _viewModel.RunApproval?.Execute();

    private void OnRunCancel(object sender, RoutedEventArgs e) => _viewModel.RunApproval?.CancelOrClose();

    private void OnRunBrowse(object sender, RoutedEventArgs e)
    {
        if (_viewModel.RunApproval is not { } approval)
        {
            return;
        }

        var dialog = new Microsoft.Win32.OpenFolderDialog { Title = "Working folder", InitialDirectory = approval.Cwd };
        if (dialog.ShowDialog(this) == true)
        {
            approval.Cwd = dialog.FolderName;
        }
    }

    /// <summary>Enter in the confirmation box does nothing: it never runs a command and never closes the approval.</summary>
    private void OnConfirmKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key is Key.Enter or Key.Return)
        {
            e.Handled = true;
        }
    }

    /// <summary>Esc stops a running command, otherwise closes the approval without running anything.</summary>
    protected override void OnPreviewKeyDown(KeyEventArgs e)
    {
        if (e.Key == Key.Escape && _viewModel.RunApproval is { } approval)
        {
            approval.Escape();
            e.Handled = true;
            return;
        }

        base.OnPreviewKeyDown(e);
    }

    // -- questions from Python -------------------------------------------------------------------

    /// <summary>
    /// Shows Python's question inside the window (not a nested message loop, so assistive tools stay responsive). The default
    /// answer is No, closing the dialog is No, and only the explicit "Turn on" button is Yes.
    /// </summary>
    private async Task ConfirmAsync(ConfirmEvent question)
    {
        var yes = false;
        try
        {
            var dialog = new ContentDialog(DialogHost)
            {
                Title = question.Title,
                Content = new TextBlock { Text = question.Text, TextWrapping = TextWrapping.Wrap, MaxWidth = 460 },
                PrimaryButtonText = "Turn on",
                CloseButtonText = "Cancel",
                DefaultButton = ContentDialogButton.Close,
            };
            yes = await dialog.ShowAsync() == ContentDialogResult.Primary;
        }
        catch (Exception)
        {
            yes = false;  // anything unexpected is No
        }

        _viewModel.AnswerConfirm(question.Id, yes);
    }

    private void OpenFolder(string folder)
    {
        try
        {
            Process.Start(new ProcessStartInfo("explorer.exe") { ArgumentList = { folder }, UseShellExecute = false });
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        {
            _viewModel.Apply(new NoticeEvent("Could not open the folder.", true));
        }
    }

    // -- clipboard and links (both already validated by the view model) -----------------------------

    private async Task CopyAsync(string text)
    {
        for (var attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                Clipboard.SetText(text);
                return;
            }
            catch (COMException)
            {
                await Task.Delay(60);  // another program has the clipboard open for a moment
            }
        }

        _viewModel.Apply(new NoticeEvent("Could not copy: the clipboard is busy. Try again.", true));
    }

    private void OpenInBrowser(string safeUrl)
    {
        try
        {
            Process.Start(new ProcessStartInfo(safeUrl) { UseShellExecute = true });
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        {
            _viewModel.Apply(new NoticeEvent("Could not open the link.", true));
        }
    }
}
