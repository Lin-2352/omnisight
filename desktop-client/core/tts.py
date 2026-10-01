"""Spoken replies with Windows' built-in offline voice (System.Speech), no extra install.

The text is handed to a short-lived PowerShell process through an environment variable, never spliced
into the command line, so nothing in an answer can become a command. The process is started without a
window and killed to stop speaking (Esc, a new question, or the switch turned off).

Only a short, plain version of the answer is spoken: code blocks are skipped, Markdown markers and
URLs are removed, and the length is capped so a long answer never reads for minutes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any, Final

from core.logger import get_logger

logger = get_logger("tts")

SPEAK_CHARS: Final[int] = 600
ENV_VAR: Final[str] = "OMNISIGHT_SPEAK_TEXT"
SCRIPT: Final[str] = (
    "Add-Type -AssemblyName System.Speech; "
    "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    f"$voice.Speak($env:{ENV_VAR})"
)

_FENCE = re.compile(r"```.*?(```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKERS = re.compile(r"(^|\n)\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>)\s+")
_EMPHASIS = re.compile(r"[*_~]{1,3}")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")
_SPACES = re.compile(r"\s+")


def speakable(markdown: str, limit: int = SPEAK_CHARS) -> str:
    """Turn Markdown into a short plain sentence or two for a voice; ``""`` if nothing is worth saying."""
    text = _FENCE.sub(" ", markdown)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MARKERS.sub(r"\1", text)
    text = _EMPHASIS.sub("", text)
    text = _SPACES.sub(" ", _CONTROL.sub(" ", text)).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit * 0.4 else cut.rsplit(" ", 1)[0] + "…"


def _default_spawn(argv: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(  # noqa: S603 - fixed argv; the text travels in the environment
        argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
    )


class Speaker:
    """Speaks one utterance at a time; starting another (or ``stop()``) cuts the current one off."""

    def __init__(
        self,
        *,
        spawn: Callable[[list[str], dict[str, str]], Any] = _default_spawn,
        powershell: str | None = None,
        platform: str = sys.platform,
        base_env: dict[str, str] | None = None,
    ) -> None:
        self._spawn = spawn
        self._platform = platform
        self._powershell = powershell if powershell is not None else (shutil.which("powershell") or "")
        self._base_env = base_env
        self._lock = threading.Lock()
        self._process: Any = None

    @property
    def available(self) -> bool:
        return self._platform == "win32" and bool(self._powershell)

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def speak(self, markdown: str) -> bool:
        """Start speaking ``markdown`` (see ``speakable``). Returns whether anything was started."""
        self.stop()
        text = speakable(markdown)
        if not text or not self.available:
            return False
        env = dict(self._base_env if self._base_env is not None else os.environ)
        env[ENV_VAR] = text
        argv = [self._powershell, "-NoProfile", "-NonInteractive", "-Command", SCRIPT]
        try:
            process = self._spawn(argv, env)
        except OSError as exc:
            logger.warning("could not start the voice: %s", exc)
            return False
        with self._lock:
            self._process = process
        return True

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("could not stop the voice cleanly: %s", exc)
