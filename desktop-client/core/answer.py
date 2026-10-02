"""What an answer card shows, decided once.

The Qt cards and the bridge (for the C# app) both ask this module, so the rules live in one place and a UI never has
to parse code fences out of model text itself:

* ``body_segments``: the answer split into prose and code, without the first paragraph when it only repeats the summary;
* ``copy_actions``: which block "Copy Fix" and "Copy Terminal Command" copy;
* ``run_commands``: which blocks get a "Run..." button (that button only ever opens an approval dialog).

Copy and Run are driven by ``code_blocks`` only, never by fences found in the markdown: the controller empties
``code_blocks`` for watch alerts so that an unprompted card never offers to copy or run code read off the screen.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from core.actions import is_shell_language
from omnisight_contracts.markdown import MarkdownSegment, split_markdown_segments

#: Languages "Copy Terminal Command" takes. (Narrower than ``core.actions.SHELL_LANGUAGES``, which decides Run; kept as the cards had it.)
COPY_SHELL_LANGUAGES: Final[frozenset[str]] = frozenset({"bash", "sh", "powershell", "cmd", "bat", "batch", "console", "shell", "zsh"})


def plain_key(text: str) -> str:
    """Markdown-insensitive comparison key (drops inline markup and whitespace differences)."""
    return " ".join(re.sub(r"[`*_]", "", text).split()).rstrip(".").lower()


def body_segments(markdown: str, skip_leading: str = "") -> list[MarkdownSegment]:
    """Prose and code segments of ``markdown``; the first paragraph is dropped if it only repeats ``skip_leading`` (the summary)."""
    segments = split_markdown_segments(markdown)
    if skip_leading and segments and segments[0].kind == "prose":
        first, _, rest = segments[0].text.partition("\n\n")
        if plain_key(first) == plain_key(skip_leading):
            return ([MarkdownSegment("prose", rest)] if rest.strip() else []) + segments[1:]
    return segments


def copy_actions(blocks: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """``(fix, command)``: the first non-shell block and the first shell block, ``""`` when there is none."""
    fix = next((code for language, code in blocks if language not in COPY_SHELL_LANGUAGES), "")
    command = next((code for language, code in blocks if language in COPY_SHELL_LANGUAGES), "")
    return fix, command


def run_commands(blocks: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    """The blocks that get a Run... button: terminal commands that are not blank, in order, with their language."""
    return [{"language": language, "command": code} for language, code in blocks if is_shell_language(language) and code.strip()]
