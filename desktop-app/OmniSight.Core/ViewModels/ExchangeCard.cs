using System.Globalization;
using CommunityToolkit.Mvvm.ComponentModel;
using OmniSight.Core.Protocol;
using OmniSight.Core.Rendering;
using OmniSight.Core.Security;

namespace OmniSight.Core.ViewModels;

public abstract record CardBlock;

/// <summary>Prose, already reduced to styled runs (never markup).</summary>
public sealed record ProseBlock(IReadOnlyList<MarkdownBlock> Blocks) : CardBlock;

public sealed record CodeCardBlock(string Language, string Code) : CardBlock;

/// <summary>A numbered source. <see cref="Url"/> is null when the address is not safe to open (the title is then plain text).</summary>
public sealed record SourceLink(int Number, string Title, string? Url)
{
    public bool HasUrl => Url is not null;
}

/// <summary>A "Run..." button. It only asks to open the approval dialog; it never runs anything.</summary>
public sealed record RunButton(string Label, string AutomationName, string Language, string Command);

/// <summary>One question and its answer, ready to show. Built from an <see cref="ExchangeEvent"/>; holds no model markup.</summary>
public sealed partial class ExchangeCard : ObservableObject
{
    [ObservableProperty]
    private bool _showRun;

    private ExchangeCard()
    {
    }

    public string Heading { get; private init; } = "";
    public string Summary { get; private init; } = "";
    public IReadOnlyList<CardBlock> Blocks { get; private init; } = [];
    public string CopyFix { get; private init; } = "";
    public string CopyCommand { get; private init; } = "";
    public IReadOnlyList<RunButton> RunButtons { get; private init; } = [];
    public IReadOnlyList<SourceLink> Sources { get; private init; } = [];
    public string SourcesTitle { get; private init; } = "";
    public string Note { get; private init; } = "";
    public string Details { get; private init; } = "";
    public bool IsWatch { get; private init; }

    public override string ToString() => Heading;

    public bool HasCopyFix => CopyFix.Length > 0;
    public bool HasCopyCommand => CopyCommand.Length > 0;
    public bool HasRunButtons => RunButtons.Count > 0;
    public bool HasSources => Sources.Count > 0;
    public bool HasNote => Note.Length > 0;
    public bool HasBlocks => Blocks.Count > 0;

    public static ExchangeCard From(ExchangeEvent evt, bool actionsEnabled)
    {
        var response = evt.Response;
        var heading = response.Transcript is { Length: > 0 } transcript ? $"You said: “{transcript}”" : evt.Question.Length > 0 ? evt.Question : "Screen capture";

        var blocks = new List<CardBlock>();
        foreach (var segment in evt.Segments)
        {
            if (segment.Kind == "code")
            {
                blocks.Add(new CodeCardBlock(segment.Language, segment.Text));
            }
            else if (segment.Kind == "prose" && MarkdownLite.Parse(segment.Text) is { Count: > 0 } parsed)
            {
                blocks.Add(new ProseBlock(parsed));
            }
        }

        var run = evt.Actions.Run;
        var buttons = run
            .Select((r, index) => new RunButton(run.Count == 1 ? "Run..." : $"Run {index + 1}...", $"Run command {index + 1}", r.Language, r.Command))
            .ToList();

        var sources = response.Sources
            .Select((s, index) => new SourceLink(index + 1, s.Title.Length > 0 ? s.Title : s.Url, LinkPolicy.TryNormalize(s.Url, out var safe) ? safe : null))
            .ToList();

        return new ExchangeCard
        {
            Heading = heading,
            Summary = response.Summary,
            Blocks = blocks,
            CopyFix = evt.Actions.CopyFix,
            CopyCommand = evt.Actions.CopyCommand,
            RunButtons = buttons,
            Sources = sources,
            SourcesTitle = evt.Searched.Length > 0 ? $"Sources (searched: {evt.Searched})" : "Sources",
            Note = evt.Note,
            Details = DetailsLine(evt),
            IsWatch = evt.Origin == "watch",
            ShowRun = actionsEnabled && evt.Origin != "watch",
        };
    }

    private static string DetailsLine(ExchangeEvent evt)
    {
        var r = evt.Response;
        var parts = new List<string>
        {
            $"{r.ModelId} via {evt.Metrics.Tier ?? "?"}",
            string.Create(CultureInfo.InvariantCulture, $"first token {evt.Metrics.ServerTtftMs / 1000:0.0}s"),
        };
        if (r.Sources.Count > 0)
        {
            parts.Add("web");
        }

        if (r.Confidence is { } confidence)
        {
            parts.Add(string.Create(CultureInfo.InvariantCulture, $"confidence {Math.Round(confidence * 100):0}%"));
        }

        if (r.FinishReason != "stop")
        {
            parts.Add($"stopped: {r.FinishReason}");
        }

        return string.Join("  ·  ", parts);
    }
}
