"""The rules that decide what an answer card offers (shared by the Qt cards and the C# app through the bridge)."""

from __future__ import annotations

from core.answer import COPY_SHELL_LANGUAGES, body_segments, copy_actions, plain_key, run_commands
from core.actions import SHELL_LANGUAGES

MARKDOWN = """**The loop reads scores[3].**

It runs one past the end.

```python
for i in range(3):
    print(scores[i])
```

Then run it again:

```powershell
python app.py
```
"""


def test_the_repeated_summary_paragraph_is_dropped() -> None:
    segments = body_segments(MARKDOWN, "The loop reads scores[3].")
    assert segments[0].kind == "prose" and segments[0].text.startswith("It runs one past the end.")
    assert [s.kind for s in segments] == ["prose", "code", "prose", "code"]
    assert [s.language for s in segments if s.kind == "code"] == ["python", "powershell"]


def test_a_different_first_paragraph_is_kept() -> None:
    segments = body_segments(MARKDOWN, "something else entirely")
    assert segments[0].text.startswith("**The loop reads scores[3].**")


def test_a_summary_only_answer_has_no_body_left() -> None:
    assert body_segments("The loop reads scores[3].", "the loop reads scores[3]") == []


def test_no_summary_means_nothing_is_dropped() -> None:
    assert body_segments(MARKDOWN, "") == body_segments(MARKDOWN)


def test_plain_key_ignores_markup_case_spacing_and_a_final_dot() -> None:
    assert plain_key("**Hello**,  `world`.") == plain_key("hello, world")


def test_copy_actions_take_the_first_fix_and_the_first_command() -> None:
    blocks = [("python", "fix()"), ("bash", "ls"), ("python", "other()"), ("powershell", "dir")]
    assert copy_actions(blocks) == ("fix()", "ls")


def test_copy_actions_are_empty_without_blocks() -> None:
    assert copy_actions([]) == ("", "")
    assert copy_actions([("python", "x")]) == ("x", "")
    assert copy_actions([("bash", "ls")]) == ("", "ls")


def test_run_commands_are_the_non_blank_terminal_blocks_in_order() -> None:
    blocks = [("python", "x"), ("pwsh", "Get-Date"), ("bash", "   "), ("cmd", "dir"), ("text", "no")]
    assert run_commands(blocks) == [{"language": "pwsh", "command": "Get-Date"}, {"language": "cmd", "command": "dir"}]


def test_the_two_language_lists_still_differ_in_the_documented_way() -> None:
    """Copy Terminal Command uses the narrower list and Run the wider one, exactly as the Qt cards always had it."""
    assert COPY_SHELL_LANGUAGES < SHELL_LANGUAGES
    assert {"pwsh", "ps1", "terminal", "dos"} <= SHELL_LANGUAGES - COPY_SHELL_LANGUAGES
