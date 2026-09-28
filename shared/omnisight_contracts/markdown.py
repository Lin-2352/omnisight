"""Markdown utilities shared by the inference node, fallback path, and HUD.

``extract_code_blocks`` follows the CommonMark rules for fenced code blocks:

* a fence is 3+ backticks or 3+ tildes, indented by at most 3 spaces;
* the closing fence uses the same character, is at least as long as the
  opening fence, and carries no info string;
* an unterminated block runs to the end of the document;
* content lines lose up to as many leading spaces as the opening fence had.
"""

from __future__ import annotations

import re
from typing import Final

from .models import MAX_CODE_BLOCKS, MAX_SUMMARY_CHARS, CodeBlock

_OPEN_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$"
)

LANGUAGE_ALIASES: Final[dict[str, str]] = {
    "py": "python",
    "python3": "python",
    "py3": "python",
    "pycon": "python",
    "ipython": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rs": "rust",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "console": "bash",
    "ps": "powershell",
    "ps1": "powershell",
    "pwsh": "powershell",
    "yml": "yaml",
    "c++": "cpp",
    "cc": "cpp",
    "hpp": "cpp",
    "cs": "csharp",
    "c#": "csharp",
    "golang": "go",
    "kt": "kotlin",
    "rb": "ruby",
    "md": "markdown",
    "plaintext": "text",
    "txt": "text",
    "none": "text",
}


def normalize_language(info: str) -> str:
    """Map a fence info string (``"py title=x"``, ``"{.rust}"``) to a canonical language id."""
    token = info.strip().split(maxsplit=1)[0] if info.strip() else ""
    token = token.strip("{}.").lower()
    if not token:
        return "text"
    token = LANGUAGE_ALIASES.get(token, token)
    return token[:32]


def _strip_indent(line: str, indent: int) -> str:
    removable = len(line) - len(line.lstrip(" "))
    return line[min(indent, removable) :]


def extract_code_blocks(markdown: str, limit: int = MAX_CODE_BLOCKS) -> list[CodeBlock]:
    """Return fenced code blocks in document order (at most ``limit`` blocks)."""
    blocks: list[CodeBlock] = []
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines) and len(blocks) < limit:
        match = _OPEN_FENCE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        fence = match.group("fence")
        info = match.group("info")
        fence_char = fence[0]
        # A backtick fence's info string may not contain backticks (CommonMark 4.5).
        if fence_char == "`" and "`" in info:
            index += 1
            continue
        indent = len(match.group("indent"))
        close_re = re.compile(rf"^ {{0,3}}{re.escape(fence_char)}{{{len(fence)},}}[ \t]*$")
        body: list[str] = []
        index += 1
        while index < len(lines) and close_re.match(lines[index]) is None:
            body.append(_strip_indent(lines[index], indent))
            index += 1
        index += 1  # skip closing fence (or step past EOF for unterminated blocks)
        blocks.append(CodeBlock(language=normalize_language(info), code="\n".join(body)))
    return blocks


def strip_code_blocks(markdown: str) -> str:
    """Return ``markdown`` with every fenced code block removed."""
    output: list[str] = []
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        match = _OPEN_FENCE_RE.match(lines[index])
        if match is None or (match.group("fence")[0] == "`" and "`" in match.group("info")):
            output.append(lines[index])
            index += 1
            continue
        fence = match.group("fence")
        close_re = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        index += 1
        while index < len(lines) and close_re.match(lines[index]) is None:
            index += 1
        index += 1
    return "\n".join(output)


def derive_summary(markdown: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Build a one-paragraph plain-text summary from a markdown answer.

    Picks the first prose paragraph (skipping headings, code, tables, and
    horizontal rules), strips inline markup, and truncates on a word boundary.
    """
    if max_chars < 2:
        raise ValueError("max_chars must be at least 2")
    prose = strip_code_blocks(markdown)
    paragraphs = re.split(r"\n\s*\n", prose)
    chosen = ""
    for paragraph in paragraphs:
        lines = [
            line.strip()
            for line in paragraph.split("\n")
            if line.strip()
            and not line.lstrip().startswith(("#", "|", ">"))
            and not re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", line)
        ]
        if lines:
            chosen = " ".join(lines)
            break
    chosen = re.sub(r"^[-*+]\s+|^\d+[.)]\s+", "", chosen)
    chosen = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", chosen)
    chosen = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", chosen)
    chosen = re.sub(r"`([^`]+)`", r"\1", chosen)
    # Emphasis markers only at word edges, so snake_case identifiers survive.
    chosen = re.sub(r"(?<!\w)(\*\*|__|\*|_)(\S(?:.*?\S)?)\1(?!\w)", r"\2", chosen)
    chosen = re.sub(r"\s+", " ", chosen).strip()
    if len(chosen) <= max_chars:
        return chosen
    cut = chosen[: max_chars - 1]
    boundary = cut.rfind(" ")
    if boundary >= max_chars // 2:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:.") + "…"
