"""Prompt construction for Qwen2-VL (torch-free).

Messages use the Qwen chat format accepted by
``processor.apply_chat_template``: the image placeholder comes first so the
vision tokens precede the instruction, then the text.
"""

from __future__ import annotations

from typing import Any, Final

from omnisight_contracts import MAX_PROMPT_CHARS, AnalysisMode

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
    "4. Be concise. Do not repeat the question or describe the screenshot unless asked."
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
}


def compose_user_text(mode: AnalysisMode, prompt: str, transcript: str | None) -> str:
    """Return the text part of the user turn for ``mode``."""
    parts = [MODE_INSTRUCTIONS[mode]]
    spoken = (transcript or "").strip()
    typed = prompt.strip()
    if spoken:
        parts.append(f"Spoken question (automatic transcription): {spoken}")
    if typed:
        label = "Additional typed note" if spoken else "User question"
        parts.append(f"{label}: {typed}")
    text = "\n\n".join(parts)
    # The instruction plus two capped inputs can never exceed ~3x the prompt cap;
    # the cut keeps the prompt bounded even if the caps change later.
    return text[: MAX_PROMPT_CHARS * 3]


def build_messages(mode: AnalysisMode, prompt: str, transcript: str | None = None) -> list[dict[str, Any]]:
    """Build the chat messages for one screenshot analysis."""
    return [
        {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]},
        {
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": compose_user_text(mode, prompt, transcript)},
            ],
        },
    ]


WARMUP_MESSAGES: Final[list[dict[str, Any]]] = [
    {
        "role": "user",
        "content": [{"type": "image"}, {"type": "text", "text": "Reply with the single word OK."}],
    }
]
