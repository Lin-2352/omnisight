using OmniSight.Core.Security;

namespace OmniSight.Core.Rendering;

/// <summary>One styled piece of inline text. <see cref="Url"/> is set only for links that passed <see cref="LinkPolicy"/>.</summary>
public sealed record InlineRun(string Text, bool Bold = false, bool Italic = false, bool Code = false, string? Url = null);

public enum BlockKind
{
    Paragraph,
    Heading,
    Bullet,
    Numbered,
}

/// <summary>A paragraph, heading or list item made of inline runs.</summary>
public sealed record MarkdownBlock(BlockKind Kind, IReadOnlyList<InlineRun> Runs, int Number = 0, int Level = 0);

/// <summary>
/// A deliberately small markdown reader for model prose: paragraphs, headings, lists, **bold**, *italic*, `code` and links.
/// It produces data (runs), never markup, so raw HTML in the text is shown as the literal text it is. Unsafe links lose their link
/// and keep their text. One pass, no backtracking, so hostile input cannot make it slow.
/// </summary>
public static class MarkdownLite
{
    private const int MaxDepth = 3;

    public static IReadOnlyList<MarkdownBlock> Parse(string markdown)
    {
        var blocks = new List<MarkdownBlock>();
        var paragraph = new List<string>();

        void Flush()
        {
            if (paragraph.Count > 0)
            {
                blocks.Add(new MarkdownBlock(BlockKind.Paragraph, Inline(string.Join(" ", paragraph.Select(l => l.Trim())))));
                paragraph.Clear();
            }
        }

        foreach (var raw in markdown.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n'))
        {
            var line = raw.TrimEnd();
            var trimmed = line.TrimStart();
            if (trimmed.Length == 0)
            {
                Flush();
                continue;
            }

            var level = HeadingLevel(trimmed);
            if (level > 0)
            {
                Flush();
                blocks.Add(new MarkdownBlock(BlockKind.Heading, Inline(trimmed[(level + 1)..].Trim()), Level: level));
            }
            else if (IsBullet(trimmed))
            {
                Flush();
                blocks.Add(new MarkdownBlock(BlockKind.Bullet, Inline(trimmed[2..].Trim())));
            }
            else if (TryNumbered(trimmed, out var number, out var rest))
            {
                Flush();
                blocks.Add(new MarkdownBlock(BlockKind.Numbered, Inline(rest), Number: number));
            }
            else
            {
                paragraph.Add(line);
            }
        }

        Flush();
        return blocks;
    }

    /// <summary>Inline runs for a single line of text (also used for headings and list items).</summary>
    public static IReadOnlyList<InlineRun> Inline(string text) => Merge(Scan(text, bold: false, italic: false, depth: 0));

    private static int HeadingLevel(string line)
    {
        var count = 0;
        while (count < line.Length && line[count] == '#')
        {
            count++;
        }

        return count is >= 1 and <= 6 && line.Length > count && line[count] == ' ' ? count : 0;
    }

    private static bool IsBullet(string line) => line.Length > 2 && (line[0] is '-' or '*' or '+') && line[1] == ' ';

    private static bool TryNumbered(string line, out int number, out string rest)
    {
        number = 0;
        rest = "";
        var digits = 0;
        while (digits < line.Length && digits < 4 && char.IsAsciiDigit(line[digits]))
        {
            digits++;
        }

        if (digits == 0 || digits + 1 >= line.Length || line[digits] != '.' || line[digits + 1] != ' ')
        {
            return false;
        }

        number = int.Parse(line[..digits]);
        rest = line[(digits + 2)..].Trim();
        return true;
    }

    private static List<InlineRun> Scan(string text, bool bold, bool italic, int depth)
    {
        var runs = new List<InlineRun>();
        var buffer = new System.Text.StringBuilder();
        var find = new Finder(text);

        void Text()
        {
            if (buffer.Length > 0)
            {
                runs.Add(new InlineRun(buffer.ToString(), bold, italic));
                buffer.Clear();
            }
        }

        var i = 0;
        while (i < text.Length)
        {
            var c = text[i];
            if (c == '\\' && i + 1 < text.Length && char.IsPunctuation(text[i + 1]))
            {
                buffer.Append(text[i + 1]);
                i += 2;
                continue;
            }

            if (c == '`')
            {
                var close = find.Backtick(i + 1);
                if (close > i + 1)
                {
                    Text();
                    runs.Add(new InlineRun(text[(i + 1)..close], bold, italic, Code: true));
                    i = close + 1;
                    continue;
                }
            }
            else if (c == '*' && i + 1 < text.Length && text[i + 1] == '*' && depth < MaxDepth)
            {
                var close = find.DoubleStar(i + 2);
                if (close > i + 2 && !char.IsWhiteSpace(text[i + 2]) && !char.IsWhiteSpace(text[close - 1]))
                {
                    Text();
                    runs.AddRange(Scan(text[(i + 2)..close], true, italic, depth + 1));
                    i = close + 2;
                    continue;
                }
            }
            else if ((c == '*' || c == '_') && depth < MaxDepth && CanOpenEmphasis(text, i, c))
            {
                var close = find.Emphasis(c, i + 1);
                if (close > i + 1)
                {
                    Text();
                    runs.AddRange(Scan(text[(i + 1)..close], bold, true, depth + 1));
                    i = close + 1;
                    continue;
                }
            }
            else if (c == '[' && TryLink(text, find, i, out var label, out var url, out var end))
            {
                Text();
                var safe = LinkPolicy.TryNormalize(url, out var normalized);
                foreach (var run in Scan(label, bold, italic, depth + 1))
                {
                    runs.Add(safe ? run with { Url = normalized } : run);
                }

                i = end;
                continue;
            }

            buffer.Append(c);
            i++;
        }

        Text();
        return runs;
    }

    private static bool CanOpenEmphasis(string text, int i, char marker)
    {
        if (i + 1 >= text.Length || char.IsWhiteSpace(text[i + 1]) || text[i + 1] == marker)
        {
            return false;
        }

        // snake_case_names, __dunder__ and 2*3*4 are not emphasis: the marker must not sit inside a word or next to another marker.
        return i == 0 || (!char.IsLetterOrDigit(text[i - 1]) && text[i - 1] != marker);
    }

    private static bool IsEmphasisCloser(string text, int j, char marker) =>
        text[j] == marker && j > 0 && !char.IsWhiteSpace(text[j - 1]) && text[j - 1] != marker
        && (j + 1 == text.Length || (!char.IsLetterOrDigit(text[j + 1]) && text[j + 1] != marker));

    /// <summary>[label](address), with balanced parentheses allowed in the address (Wikipedia style).</summary>
    private static bool TryLink(string text, Finder find, int open, out string label, out string url, out int end)
    {
        label = url = "";
        end = open;
        var closeLabel = find.CloseBracket(open + 1);
        if (closeLabel < 0 || closeLabel + 1 >= text.Length || text[closeLabel + 1] != '(')
        {
            return false;
        }

        var closeUrl = find.CloseParen(closeLabel + 2);
        if (closeUrl < 0)
        {
            return false;
        }

        label = text[(open + 1)..closeLabel];
        url = text[(closeLabel + 2)..closeUrl];
        end = closeUrl + 1;
        return label.Length > 0;
    }

    /// <summary>
    /// "First match at or after this position" searches, remembered. The scanner only moves forward, so each answer is valid for every
    /// later position up to the match (or for all of them when there is none): the whole scan stays linear however hostile the text.
    /// </summary>
    private sealed class Finder(string text)
    {
        private readonly (int From, int Result)[] _cache = Enumerable.Repeat((From: -1, Result: -1), 6).ToArray();

        public int Backtick(int from) => Cached(0, from, f => text.IndexOf('`', f));

        public int DoubleStar(int from) => Cached(1, from, f => text.IndexOf("**", f, StringComparison.Ordinal));

        public int CloseBracket(int from) => Cached(2, from, f => text.IndexOf(']', f));

        public int Emphasis(char marker, int from) => Cached(marker == '*' ? 3 : 4, from, f =>
        {
            for (var j = f; j < text.Length; j++)
            {
                if (IsEmphasisCloser(text, j, marker))
                {
                    return j;
                }
            }

            return -1;
        });

        /// <summary>The ")" that closes an address which started just before <paramref name="from"/>, counting nested pairs.</summary>
        public int CloseParen(int from) => Cached(5, from, f =>
        {
            var depth = 1;
            for (var j = f; j < text.Length; j++)
            {
                if (text[j] == '(')
                {
                    depth++;
                }
                else if (text[j] == ')' && --depth == 0)
                {
                    return j;
                }
            }

            return -1;
        });

        private int Cached(int kind, int from, Func<int, int> search)
        {
            var (known, result) = _cache[kind];
            if (known >= 0 && from >= known && (result < 0 || result >= from))
            {
                return result;
            }

            result = search(from);
            _cache[kind] = (from, result);
            return result;
        }
    }

    private static List<InlineRun> Merge(List<InlineRun> runs)
    {
        var merged = new List<InlineRun>();
        foreach (var run in runs)
        {
            if (merged.Count > 0 && merged[^1] is var last && !last.Code && !run.Code && last.Bold == run.Bold && last.Italic == run.Italic && last.Url == run.Url)
            {
                merged[^1] = last with { Text = last.Text + run.Text };
            }
            else
            {
                merged.Add(run);
            }
        }

        return merged;
    }
}
