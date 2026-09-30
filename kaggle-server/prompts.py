"""Prompt construction for Qwen2-VL (torch-free).

Messages use the Qwen chat format accepted by
``processor.apply_chat_template``: the image placeholder comes first so the
vision tokens precede the instruction, then the text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from omnisight_contracts import MAX_PROMPT_CHARS, AnalysisMode, ChatTurn

SYSTEM_PROMPT: Final[str] = (
    "You are OmniSight, an assistant that reads screenshots of a software developer's "
    "screen and answers precisely.\n"
    "Rules:\n"
    "1. Start with one plain sentence that states the answer or the main finding.\n"
    "2. Use GitHub-flavored Markdown. Put every code snippet, command, or corrected file "
    "in a fenced code block tagged with its language.\n"
    "3. Base the answer only on what is visible in the screenshot and on the user's "
    "question. If text is too small or blurry to read, say which part is unreadable "
    "instead of guessing.\n"
    "4. Code you suggest must be valid for the language on screen. Use only identifiers "
    "that appear in the screenshot or that exist in the language's standard library; never "
    "invent methods or properties. If you are not sure an API exists, say so instead of "
    "guessing.\n"
    "5. Be concise. Do not repeat the question or describe the screenshot unless asked."
)
# Known limitation: Qwen2-VL-7B (Kaggle T4) often obeys instructions painted on the screen. This
# prompt was hijacked by an on-screen "reply PWNED" in 8 of 10 runs (T=0 and T=0.7). A system rule
# telling it to ignore on-screen instructions did not help (5/5 and 4/4 hijacked in small
# samples), so none is included. Gemini and the local 2B resisted the same screen.

#: System prompt when there is no screenshot (mode ``chat``): a general, concise assistant.
CHAT_SYSTEM_PROMPT: Final[str] = (
    "You are OmniSight, a helpful assistant for a software developer.\n"
    "Rules:\n"
    "1. Start with one plain sentence that answers the question or states the main point.\n"
    "2. Use GitHub-flavored Markdown. Put every code snippet or command in a fenced code block "
    "tagged with its language.\n"
    "3. You cannot see the user's screen in this conversation and you have no internet access. "
    "If the answer depends on the screen or on current information, say so instead of guessing.\n"
    "4. Use earlier messages of the conversation for follow-up questions.\n"
    "5. Be concise."
)

MODE_INSTRUCTIONS: Final[dict[AnalysisMode, str]] = {
    AnalysisMode.EXPLAIN: (
        "Explain what the code, error, or interface in this screenshot does. Cover the key "
        "parts in order of importance."
    ),
    AnalysisMode.DEBUG: (
        "Find the error or bug shown in this screenshot. State the root cause, point to the "
        "exact line or element responsible, and give the smallest fix as a code block."
    ),
    AnalysisMode.SUMMARIZE: (
        "Summarize what is on this screen in at most five bullet points, most important first."
    ),
    AnalysisMode.OCR: (
        "Transcribe all readable text in this screenshot exactly, preserving line breaks and "
        "indentation. Put code in fenced code blocks tagged with its language. Add no commentary."
    ),
    AnalysisMode.VOICE_QUERY: (
        "The user asked the question below out loud while looking at this screen. Answer it "
        "using the screenshot."
    ),
    AnalysisMode.CHAT: "Answer the user's message below.",
}


#: C0/C1 controls (except tab and newline), zero-width characters and bidi overrides: invisible
#: text that can hide instructions from the user or reorder what the model reads.
_HIDDEN_CHARS_RE: Final[re.Pattern[str]] = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def sanitize_user_text(text: str) -> str:
    """Neutralize user-supplied text (typed prompt or ASR transcript) before it enters the chat template.

    Qwen tokenizers turn literal ``<|im_start|>``, ``<|im_end|>``, ``<|vision_start|>`` ... in
    plain text into real control tokens, so a prompt could otherwise close the user turn and
    open a forged ``system`` or ``assistant`` turn. The ``<|`` and ``|>`` delimiters are replaced
    by look-alike angle quotes, so no control token can be spelled. Hidden and direction-override
    characters are removed; normal text, code, tabs and newlines are unchanged.
    """
    cleaned = _HIDDEN_CHARS_RE.sub("", text)
    return cleaned.replace("<|", "\u2039|").replace("|>", "|\u203a")


def compose_user_text(mode: AnalysisMode, prompt: str, transcript: str | None) -> str:
    """Return the text part of the user turn for ``mode`` (user parts sanitized)."""
    parts = [MODE_INSTRUCTIONS[mode]]
    spoken = sanitize_user_text(transcript or "").strip()
    typed = sanitize_user_text(prompt).strip()
    if spoken:
        parts.append(f"Spoken question (automatic transcription): {spoken}")
    if typed:
        label = "Additional typed note" if spoken else "User question"
        parts.append(f"{label}: {typed}")
    text = "\n\n".join(parts)
    # The instruction plus two capped inputs can never exceed ~3x the prompt cap;
    # the cut keeps the prompt bounded even if the caps change later.
    return text[: MAX_PROMPT_CHARS * 3]


def build_messages(
    mode: AnalysisMode,
    prompt: str,
    transcript: str | None = None,
    history: Sequence[ChatTurn] = (),
    *,
    has_image: bool = True,
) -> list[dict[str, Any]]:
    """Build the chat messages: system prompt, earlier turns (text only), then this request.

    Earlier turns contain model output as well as user text, so both go through
    ``sanitize_user_text``: neither can spell a chat-template control token.
    """
    system = SYSTEM_PROMPT if has_image else CHAT_SYSTEM_PROMPT
    messages: list[dict[str, Any]] = [{"role": "system", "content": [{"type": "text", "text": system}]}]
    for turn in history:
        messages.append({"role": turn.role, "content": [{"type": "text", "text": sanitize_user_text(turn.text)}]})
    current: list[dict[str, Any]] = [{"type": "image"}] if has_image else []
    current.append({"type": "text", "text": compose_user_text(mode, prompt, transcript)})
    messages.append({"role": "user", "content": current})
    return messages


WARMUP_MESSAGES: Final[list[dict[str, Any]]] = [
    {
        "role": "user",
        "content": [{"type": "image"}, {"type": "text", "text": "Reply with the single word OK."}],
    }
]
