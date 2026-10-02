using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using OmniSight.Core.Rendering;

namespace OmniSight.App.Views;

/// <summary>
/// Fills a <see cref="TextBlock"/> from parsed inline runs. Only text, weight, style, a monospace face and links are produced, so
/// nothing in model text can become a control, an image or markup. Links that reach here already passed the link policy.
/// </summary>
public static class RichText
{
    public static readonly DependencyProperty RunsProperty =
        DependencyProperty.RegisterAttached("Runs", typeof(IReadOnlyList<InlineRun>), typeof(RichText), new PropertyMetadata(null, OnRunsChanged));

    /// <summary>Raised with the address of a clicked link (already checked); the window decides how to open it.</summary>
    public static event Action<string>? LinkClicked;

    public static IReadOnlyList<InlineRun>? GetRuns(DependencyObject element) => (IReadOnlyList<InlineRun>?)element.GetValue(RunsProperty);

    public static void SetRuns(DependencyObject element, IReadOnlyList<InlineRun>? value) => element.SetValue(RunsProperty, value);

    private static void OnRunsChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not TextBlock block)
        {
            return;
        }

        block.Inlines.Clear();
        if (e.NewValue is not IReadOnlyList<InlineRun> runs)
        {
            return;
        }

        foreach (var run in runs)
        {
            block.Inlines.Add(Build(run));
        }
    }

    private static Inline Build(InlineRun run)
    {
        var text = new Run(run.Text);
        if (run.Bold)
        {
            text.FontWeight = FontWeights.SemiBold;
        }

        if (run.Italic)
        {
            text.FontStyle = FontStyles.Italic;
        }

        if (run.Code)
        {
            text.FontFamily = new FontFamily("Cascadia Mono, Consolas, Courier New");
            text.SetResourceReference(TextElement.ForegroundProperty, "AccentTextFillColorPrimaryBrush");
        }

        if (run.Url is null)
        {
            return text;
        }

        var link = new Hyperlink(text) { ToolTip = run.Url, Focusable = true };
        var url = run.Url;
        link.Click += (_, _) => LinkClicked?.Invoke(url);
        return link;
    }
}
