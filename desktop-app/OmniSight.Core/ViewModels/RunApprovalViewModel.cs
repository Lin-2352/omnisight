using CommunityToolkit.Mvvm.ComponentModel;
using OmniSight.Core.Protocol;

namespace OmniSight.Core.ViewModels;

/// <summary>
/// The "Run command" approval, with the Qt dialog's rules: <see cref="CanRun"/> is true only while the exact confirmation word is
/// typed, the command is not one of the always-refused kinds and nothing is running; Enter never runs anything; one approval runs
/// one command (the word is cleared afterwards). The app only asks: Python holds the command text, re-checks everything and runs.
/// </summary>
public sealed partial class RunApprovalViewModel : ObservableObject
{
    public const int MaxOutputChars = 70_000;

    private readonly Action<string> _send;
    private bool _applying;

    public RunApprovalViewModel(RunOpenEvent opened, Action<string> send)
    {
        _send = send;
        Id = opened.Id;
        Command = opened.Command;
        ShellName = opened.ShellName;
        Banner = opened.Banner;
        ConfirmWord = opened.ConfirmWord;
        MinTimeout = opened.MinTimeoutS;
        MaxTimeout = opened.MaxTimeoutS;
        _applying = true;
        _cwd = opened.Cwd;
        _timeoutSeconds = Math.Clamp(opened.TimeoutS, opened.MinTimeoutS, opened.MaxTimeoutS);
        _applying = false;
        ApplyVerdict(opened.Refused, opened.Warnings);
    }

    public string Id { get; }
    public string Command { get; }
    public string ShellName { get; }
    public string Banner { get; }
    public string ConfirmWord { get; }
    public double MinTimeout { get; }
    public double MaxTimeout { get; }

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanRun))]
    private string? _refused;

    [ObservableProperty]
    private string _verdictText = "";

    [ObservableProperty]
    private bool _verdictIsRefusal;

    [ObservableProperty]
    private string _cwd = "";

    [ObservableProperty]
    private double _timeoutSeconds;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanRun))]
    private string _typed = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanRun), nameof(CloseText), nameof(CanEdit))]
    private bool _isRunning;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CloseText))]
    private bool _hasRun;

    [ObservableProperty]
    private string _status = "";

    [ObservableProperty]
    private bool _statusIsError;

    [ObservableProperty]
    private string _output = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(HasOutput))]
    private bool _showOutput;

    public bool CanRun => !IsRunning && Refused is null && string.Equals(Typed, ConfirmWord, StringComparison.Ordinal);
    public bool CanEdit => !IsRunning;
    public bool HasOutput => ShowOutput;
    public string CloseText => IsRunning ? "Stop" : HasRun ? "Close" : "Cancel";
    public bool HasVerdict => VerdictText.Length > 0;

    // -- what the person does -------------------------------------------------------------------------

    public bool Execute()
    {
        if (!CanRun)
        {
            return false;
        }

        _send(BridgeCommands.RunExecute(Id, Typed, Cwd, TimeoutSeconds));
        return true;
    }

    /// <summary>The Cancel / Stop / Close button.</summary>
    public void CancelOrClose() => _send(BridgeCommands.RunCancel(Id));

    /// <summary>Esc and the overlay's close: stops a running command, otherwise closes without running.</summary>
    public void Escape() => _send(IsRunning ? BridgeCommands.RunCancel(Id) : BridgeCommands.RunClose(Id));

    partial void OnCwdChanged(string value)
    {
        if (!_applying && !IsRunning)
        {
            _send(BridgeCommands.RunCheck(Id, value));
        }
    }

    partial void OnVerdictTextChanged(string value) => OnPropertyChanged(nameof(HasVerdict));

    // -- what Python says -----------------------------------------------------------------------------

    public void ApplyVerdict(string? refused, IReadOnlyList<string> warnings)
    {
        Refused = refused;
        if (refused is not null)
        {
            VerdictText = $"Refused: {refused} OmniSight will not run this command, even if you approve it.";
            VerdictIsRefusal = true;
        }
        else if (warnings.Count > 0)
        {
            VerdictText = "Take a second look: this command " + string.Join("; ", warnings) + ".";
            VerdictIsRefusal = false;
        }
        else
        {
            VerdictText = "";
            VerdictIsRefusal = false;
        }
    }

    public void ApplyState(RunStateEvent state)
    {
        switch (state.State)
        {
            case "running":
                IsRunning = true;
                ShowOutput = true;
                Output = "";
                Status = "Running...";
                StatusIsError = false;
                break;
            case "stopping":
                Status = "Stopping...";
                break;
            case "finished":
                IsRunning = false;
                HasRun = true;
                ShowOutput = true;
                var text = state.Output.Length > MaxOutputChars ? state.Output[..MaxOutputChars] : state.Output;
                Output = text + (state.Truncated ? "\n[output cut at 64 KB]" : "");
                (Status, StatusIsError) = Describe(state);
                ClearWord();
                break;
            case "refused":
                IsRunning = false;
                HasRun = true;
                Status = state.Message;
                StatusIsError = true;
                ClearWord();
                break;
        }
    }

    private (string Text, bool Error) Describe(RunStateEvent state)
    {
        if (state.TimedOut)
        {
            return ($"Stopped: it did not finish within {TimeoutSeconds:0} s. Everything it started was ended.", false);
        }

        if (state.Cancelled)
        {
            return ("Stopped by you. Everything it started was ended.", false);
        }

        return ($"Finished with exit code {state.ExitCode?.ToString() ?? "?"} in {state.DurationS:0.0} s.", false);
    }

    /// <summary>One approval, one run: the confirmation word is cleared once a run has ended.</summary>
    private void ClearWord() => Typed = "";
}
