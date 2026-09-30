"""Conversation memory: the last few exchanges, text only, kept on this PC.

What is stored, and why it is safe to send back to a model:

* **Text only.** Old screenshots are never kept or re-sent; an exchange is the user's question and the
  answer's Markdown (truncated), so a follow-up like "and how do I fix that?" has context without the
  cost of another image.
* **Capped.** At most ``STORE_TURNS`` turns are kept on disk and at most ``SEND_TURNS`` /
  ``SEND_CHARS`` go into one request (well inside the contract's 12 turns / 12 000 characters), so
  the prompt, and with it time-to-first-token and VRAM, stays bounded.
* **Cleaned.** Control and direction-override characters are removed. The node also neutralizes
  chat-template control tokens (``sanitize_user_text``) because earlier turns contain model output.
* **Local and optional.** ``%APPDATA%/OmniSight/history.json``; turn it off or press Clear and the file
  is gone. A corrupt file is moved aside instead of crashing the app.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Final

from core.logger import get_logger
from network.schemas import AnalysisMode, ChatTurn

logger = get_logger("memory")

STORE_TURNS: Final[int] = 24
SEND_TURNS: Final[int] = 8
SEND_CHARS: Final[int] = 6_000
#: Longest single turn stored (the contract allows 2000).
TURN_CHARS: Final[int] = 1_500
FILE_VERSION: Final[int] = 1

_HIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")

MODE_PHRASES: Final[dict[AnalysisMode, str]] = {
    AnalysisMode.EXPLAIN: "Explain what is on my screen.",
    AnalysisMode.DEBUG: "Find the problem on my screen.",
    AnalysisMode.SUMMARIZE: "Summarize my screen.",
    AnalysisMode.OCR: "Read the text on my screen.",
    AnalysisMode.VOICE_QUERY: "(I asked a question out loud.)",
    AnalysisMode.CHAT: "",
}


def clean(text: str, limit: int = TURN_CHARS) -> str:
    """Strip hidden characters and whitespace; truncate on a word boundary with an ellipsis."""
    text = _HIDDEN.sub("", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def default_path() -> Path:
    appdata = os.environ.get("APPDATA")
    return (Path(appdata) / "OmniSight" if appdata else Path.home() / ".omnisight") / "history.json"


class ConversationMemory:
    """Thread-safe store of ``ChatTurn`` objects with atomic persistence."""

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.path = path if path is not None else default_path()
        self._enabled = enabled
        self._lock = threading.Lock()
        self._turns: list[ChatTurn] = []
        if enabled:
            self._turns = self._read()

    # -- state ----------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Turning memory off forgets everything, on disk too; turning it on starts empty."""
        with self._lock:
            if enabled == self._enabled:
                return
            self._enabled = enabled
            self._turns = []
        self._delete()

    def __len__(self) -> int:
        with self._lock:
            return len(self._turns)

    # -- use ------------------------------------------------------------------------------

    def history(self) -> list[ChatTurn]:
        """The turns to send with the next request (newest that fit, starting on a user turn)."""
        with self._lock:
            if not self._enabled:
                return []
            chosen: list[ChatTurn] = []
            total = 0
            for turn in reversed(self._turns):
                if len(chosen) >= SEND_TURNS or total + len(turn.text) > SEND_CHARS:
                    break
                chosen.append(turn)
                total += len(turn.text)
            chosen.reverse()
        while chosen and chosen[0].role != "user":
            chosen.pop(0)
        return chosen

    def add_exchange(self, question: str, answer: str) -> None:
        """Remember one question and its answer. Empty text on either side records nothing."""
        q, a = clean(question), clean(answer)
        if not self._enabled or not q or not a:
            return
        with self._lock:
            self._turns.append(ChatTurn(role="user", text=q))
            self._turns.append(ChatTurn(role="assistant", text=a))
            del self._turns[: max(0, len(self._turns) - STORE_TURNS)]
            while self._turns and self._turns[0].role != "user":
                self._turns.pop(0)
            snapshot = list(self._turns)
        self._write(snapshot)

    def clear(self) -> None:
        with self._lock:
            self._turns = []
        self._delete()

    # -- disk -----------------------------------------------------------------------------

    def _read(self) -> list[ChatTurn]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            self._quarantine(f"unreadable ({type(exc).__name__})")
            return []
        items = raw.get("turns") if isinstance(raw, dict) and raw.get("version") == FILE_VERSION else None
        if not isinstance(items, list):
            self._quarantine("unexpected format")
            return []
        turns: list[ChatTurn] = []
        for item in items[-STORE_TURNS:]:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant") or not isinstance(item.get("text"), str):
                continue
            text = clean(item["text"])
            if text:
                turns.append(ChatTurn(role=item["role"], text=text))
        while turns and turns[0].role != "user":
            turns.pop(0)
        return turns

    def _write(self, turns: list[ChatTurn]) -> None:
        payload = {"version": FILE_VERSION, "turns": [{"role": t.role, "text": t.text} for t in turns]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix="history-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("could not save the conversation: %s", exc)

    def _delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not delete %s: %s", self.path, exc)

    def _quarantine(self, reason: str) -> None:
        aside = self.path.with_suffix(".corrupt")
        logger.warning("conversation file %s; moving it to %s", reason, aside.name)
        try:
            os.replace(self.path, aside)
        except OSError:
            pass
