using System.Diagnostics;
using OmniSight.Core.Rendering;
using OmniSight.Core.Security;

namespace OmniSight.App.Tests;

public class LinkPolicyTests
{
    [Theory]
    [InlineData("https://stackoverflow.com/questions/1")]
    [InlineData("http://example.com/path?q=1&b=2#frag")]
    [InlineData("https://en.wikipedia.org/wiki/Python_(programming_language)")]
    [InlineData("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbCd")]
    [InlineData("HTTPS://Example.COM/Path")]
    [InlineData("https://sub.domain.example.co.uk:8443/x")]
    [InlineData("https://8.8.8.8/")]
    [InlineData("https://[2606:4700:4700::1111]/")]
    public void Public_http_and_https_links_are_allowed(string url)
    {
        Assert.True(LinkPolicy.IsSafe(url));
        Assert.True(LinkPolicy.TryNormalize(url, out var normalized));
        Assert.StartsWith("http", normalized);
    }

    [Theory]
    [InlineData("file:///C:/Windows/System32/cmd.exe")]
    [InlineData("file://server/share/x.exe")]
    [InlineData(@"\\server\share\payload.exe")]
    [InlineData("//server/share")]
    [InlineData("ms-settings:privacy-microphone")]
    [InlineData("ms-msdt:/id PCWDiagnostic")]
    [InlineData("javascript:alert(1)")]
    [InlineData("data:text/html,<script>alert(1)</script>")]
    [InlineData("vbscript:msgbox(1)")]
    [InlineData("mailto:a@b.com")]
    [InlineData("tel:+123")]
    [InlineData("ftp://example.com/x")]
    [InlineData("steam://run/1")]
    [InlineData("calculator:")]
    [InlineData("cmd.exe")]
    [InlineData("C:\\Windows\\notepad.exe")]
    [InlineData("example.com")]
    [InlineData("www.example.com")]
    [InlineData("/relative/path")]
    [InlineData("")]
    [InlineData("   ")]
    public void Dangerous_or_non_web_links_are_refused(string url)
    {
        Assert.False(LinkPolicy.IsSafe(url));
    }

    [Fact]
    public void Null_is_refused()
    {
        Assert.False(LinkPolicy.IsSafe(null));
    }

    [Theory]
    [InlineData("https://user:pass@example.com/")]
    [InlineData("https://google.com@evil.example.com/")]
    [InlineData("http://admin@example.com")]
    public void Links_with_credentials_in_them_are_refused(string url)
    {
        Assert.False(LinkPolicy.IsSafe(url));
    }

    [Theory]
    [InlineData("http://localhost/")]
    [InlineData("http://localhost:8000/v1/health")]
    [InlineData("http://LOCALHOST./")]
    [InlineData("http://app.localhost/")]
    [InlineData("http://127.0.0.1:8000/")]
    [InlineData("http://127.1/")]
    [InlineData("http://0.0.0.0/")]
    [InlineData("http://[::1]/")]
    [InlineData("http://[::ffff:127.0.0.1]/")]
    [InlineData("http://10.0.0.5/")]
    [InlineData("http://172.16.0.1/")]
    [InlineData("http://172.31.255.255/")]
    [InlineData("http://192.168.1.1/")]
    [InlineData("http://169.254.169.254/latest/meta-data")]
    [InlineData("http://100.64.0.1/")]
    [InlineData("http://224.0.0.1/")]
    [InlineData("http://[fe80::1]/")]
    [InlineData("http://[fd00::1]/")]
    [InlineData("http://printer.local/")]
    [InlineData("http://nas.lan/")]
    [InlineData("http://router.home.arpa/")]
    [InlineData("http://service.internal/")]
    [InlineData("http://intranet/")]
    [InlineData("http://2130706433/")]
    public void This_machine_and_the_local_network_are_refused(string url)
    {
        Assert.False(LinkPolicy.IsSafe(url));
    }

    [Theory]
    [InlineData("https://172.15.0.1/")]
    [InlineData("https://172.32.0.1/")]
    [InlineData("https://100.63.0.1/")]
    [InlineData("https://100.128.0.1/")]
    public void Addresses_just_outside_the_private_ranges_are_allowed(string url)
    {
        Assert.True(LinkPolicy.IsSafe(url));
    }

    [Theory]
    [InlineData("https://example.com/a b")]
    [InlineData("https://example.com/a\tb")]
    [InlineData("https://example.com/a\nb")]
    [InlineData("https://example.com/a\rb")]
    [InlineData("https://example.com/\0")]
    [InlineData("https://example.com\\evil")]
    [InlineData(" https://example.com/")]
    [InlineData("https://example.com/ ")]
    [InlineData("https:example.com")]
    [InlineData("https:/example.com")]
    [InlineData("https:///example.com")]
    public void Control_characters_spaces_backslashes_and_malformed_schemes_are_refused(string url)
    {
        Assert.False(LinkPolicy.IsSafe(url));
    }

    [Fact]
    public void Very_long_links_are_refused()
    {
        Assert.False(LinkPolicy.IsSafe("https://example.com/" + new string('a', LinkPolicy.MaxLength)));
        Assert.True(LinkPolicy.IsSafe("https://example.com/" + new string('a', 100)));
    }

    [Fact]
    public void The_normalized_address_is_what_gets_opened_not_the_raw_text()
    {
        Assert.True(LinkPolicy.TryNormalize("HTTPS://Example.COM/Path", out var normalized));
        Assert.Equal("https://example.com/Path", normalized);
    }
}

public class MarkdownLiteTests
{
    private static IReadOnlyList<InlineRun> Runs(string text) => MarkdownLite.Inline(text);

    [Fact]
    public void Plain_text_is_one_run()
    {
        Assert.Equal([new InlineRun("hello world")], Runs("hello world"));
    }

    [Fact]
    public void Bold_italic_and_code_are_styled()
    {
        Assert.Equal(
            [new InlineRun("a "), new InlineRun("b", Bold: true), new InlineRun(" c "), new InlineRun("d", Italic: true), new InlineRun(" e "), new InlineRun("f()", Code: true)],
            Runs("a **b** c *d* e `f()`"));
    }

    [Fact]
    public void Code_is_literal_so_markers_inside_it_do_nothing()
    {
        Assert.Equal([new InlineRun("**not bold** and [x](y)", Code: true)], Runs("`**not bold** and [x](y)`"));
    }

    [Fact]
    public void Bold_can_contain_italic()
    {
        var runs = Runs("**very *strong* text**");
        Assert.Contains(runs, r => r is { Text: "strong", Bold: true, Italic: true });
    }

    [Theory]
    [InlineData("snake_case_name stays")]
    [InlineData("2*3*4 equals 24")]
    [InlineData("a * b * c")]
    [InlineData("unmatched **bold")]
    [InlineData("unmatched *italic")]
    [InlineData("unmatched `code")]
    [InlineData("**")]
    [InlineData("* ")]
    [InlineData("__init__ method")]
    public void Markers_that_do_not_form_emphasis_stay_as_text(string text)
    {
        var runs = Runs(text);
        Assert.All(runs, r => Assert.False(r.Bold || r.Italic || r.Code, text));
        Assert.Equal(text.Replace("\\", ""), string.Concat(runs.Select(r => r.Text)));
    }

    [Fact]
    public void A_safe_link_keeps_its_address_and_an_unsafe_one_keeps_only_its_text()
    {
        var safe = Runs("see [the docs](https://docs.python.org/3/) now");
        Assert.Contains(safe, r => r is { Text: "the docs", Url: "https://docs.python.org/3/" });
        foreach (var bad in new[] { "file:///C:/x.exe", "javascript:alert(1)", "ms-settings:privacy", "http://localhost:8000/", @"\\host\share", "cmd.exe" })
        {
            var runs = Runs($"[click here]({bad})");
            Assert.All(runs, r => Assert.Null(r.Url));
            Assert.Equal("click here", string.Concat(runs.Select(r => r.Text)));
        }
    }

    [Fact]
    public void Raw_html_is_shown_as_text_never_interpreted()
    {
        var runs = Runs("<script>alert(1)</script> <img src=x onerror=alert(1)> <a href=\"https://x.com\">x</a>");
        Assert.All(runs, r => Assert.Null(r.Url));
        Assert.Equal("<script>alert(1)</script> <img src=x onerror=alert(1)> <a href=\"https://x.com\">x</a>", string.Concat(runs.Select(r => r.Text)));
    }

    [Fact]
    public void Backslash_escapes_a_marker()
    {
        Assert.Equal("*literal*", string.Concat(Runs(@"\*literal\*").Select(r => r.Text)));
    }

    [Fact]
    public void Paragraphs_headings_and_lists_are_blocks()
    {
        var blocks = MarkdownLite.Parse("# Title\n\nfirst line\nsecond line\n\n- one\n- two\n\n1. alpha\n2. beta");
        Assert.Equal(
            [BlockKind.Heading, BlockKind.Paragraph, BlockKind.Bullet, BlockKind.Bullet, BlockKind.Numbered, BlockKind.Numbered],
            blocks.Select(b => b.Kind));
        Assert.Equal(1, blocks[0].Level);
        Assert.Equal("first line second line", string.Concat(blocks[1].Runs.Select(r => r.Text)));
        Assert.Equal([1, 2], blocks.Where(b => b.Kind == BlockKind.Numbered).Select(b => b.Number));
    }

    [Theory]
    [InlineData("")]
    [InlineData("\n\n\n")]
    [InlineData("   ")]
    public void Empty_input_has_no_blocks(string text)
    {
        Assert.Empty(MarkdownLite.Parse(text));
    }

    [Fact]
    public void A_hash_without_a_space_is_not_a_heading()
    {
        var blocks = MarkdownLite.Parse("#hashtag and #1");
        Assert.Equal(BlockKind.Paragraph, Assert.Single(blocks).Kind);
    }

    [Fact]
    public void Hostile_input_is_parsed_in_linear_time()
    {
        var inputs = new[]
        {
            new string('*', 200_000),
            string.Concat(Enumerable.Repeat("[a](", 50_000)),
            string.Concat(Enumerable.Repeat("**a *b ", 30_000)),
            string.Concat(Enumerable.Repeat("`", 100_001)),
            new string('_', 200_000),
            string.Concat(Enumerable.Repeat("[", 100_000)) + string.Concat(Enumerable.Repeat("]", 100_000)),
        };
        foreach (var input in inputs)
        {
            var watch = Stopwatch.StartNew();
            _ = MarkdownLite.Parse(input);
            Assert.True(watch.Elapsed < TimeSpan.FromSeconds(5), $"too slow ({watch.Elapsed}) for input starting {input[..8]}");
        }
    }

    [Fact]
    public void Nesting_is_bounded()
    {
        var text = string.Concat(Enumerable.Repeat("*a ", 200)) + "x" + string.Concat(Enumerable.Repeat(" a*", 200));
        var runs = Runs(text);
        Assert.NotEmpty(runs);
    }
}
