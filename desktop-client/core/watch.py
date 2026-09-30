"""Watch mode: notice an error on the screen without being asked. Pure logic, no Qt.

Every few seconds the controller captures the screen. If (and only if) the picture changed and has
something to read, it asks the **local** model a constrained YES/NO question. Measured on the local
2B, a free-form "reply NONE or one sentence" prompt flagged clean screens as errors, while YES/NO found
every real error and kept clean screens quiet. Only when the answer is YES and no error was already
showing does a second call ask for a one-sentence description. This module decides what to do with
the answers:

* ``frame_signature`` / ``frame_changed``: a tiny grayscale fingerprint of the frame, so an unchanged
  screen, a blinking cursor or a ticking clock costs no model call;
* ``WatchScheduler``: when the next tick is due (interval, extra tick on a window change, single
  flight, pause, back-off after failures), with an injectable clock;
* ``parse_watch_reply`` and ``FindingTracker``: turn the model's reply into "nothing" or a finding,
  and report each finding once.

Frames never leave the PC (the controller only ever uses the local node) and are not stored.
"""

from __future__ import annotations

import base64
import io
import re
import time
from collections.abc import Callable
from typing import Final

from PIL import Image

#: Step 1: the fixed yes/no question asked about a changed screen.
CHECK_PROMPT: Final[str] = (
    "Is an error message, exception, crash or failed build visible on this screen? "
    "Answer with exactly one word: YES or NO."
)
#: Step 2 (only after a new YES): what the error is, in a sentence.
DESCRIBE_PROMPT: Final[str] = "In at most 15 words, say what error message, exception, crash or failed build is shown on this screen."
#: Generation cap for the description: short, because every token costs time on a slow CPU.
DESCRIBE_TOKENS: Final[int] = 40
#: Used when the description comes back empty: the alert is still worth showing.
GENERIC_FINDING: Final[str] = "An error message appears to be on your screen."
DEFAULT_INTERVAL_S: Final[float] = 10.0
MIN_INTERVAL_S: Final[float] = 5.0
#: Fewest seconds between two ticks, even when the window changes.
MIN_GAP_S: Final[float] = 3.0
MAX_BACKOFF_S: Final[float] = 120.0
#: Consecutive failed ticks after which watching stops by itself.
MAX_FAILURES: Final[int] = 3

SIGNATURE_SIZE: Final[tuple[int, int]] = (32, 18)
#: Mean absolute difference (0-255) above which a frame counts as changed.
CHANGE_MEAN: Final[float] = 2.0
#: A cell counts as different when it moved by more than this (0-255)...
CELL_DELTA: Final[int] = 24
#: ...and the frame counts as changed when at least this fraction of the cells differ.
CHANGE_CELLS: Final[float] = 0.02


def frame_signature(image_b64: str) -> bytes:
    """A 32x18 grayscale fingerprint of a base64 JPEG frame (JPEG draft mode makes this a few ms)."""
    with Image.open(io.BytesIO(base64.b64decode(image_b64))) as image:
        image.draft("L", (SIGNATURE_SIZE[0] * 4, SIGNATURE_SIZE[1] * 4))
        small = image.convert("L").resize(SIGNATURE_SIZE, Image.Resampling.BOX)
        data = small.tobytes()
        small.close()
    return data


#: A screen whose fingerprint varies by no more than this (0-255) has nothing to read: a plain desktop
#: colour or a blank window. The model answers YES to those, so they are never sent.
FLAT_SPREAD: Final[int] = 12


def frame_is_flat(signature: bytes) -> bool:
    """True for a uniform picture (nothing on it worth a model call)."""
    return not signature or max(signature) - min(signature) <= FLAT_SPREAD


def frame_changed(previous: bytes | None, current: bytes) -> bool:
    """Whether ``current`` differs enough from ``previous`` to be worth a model call (always, the first time)."""
    if previous is None or len(previous) != len(current):
        return True
    diffs = [abs(a - b) for a, b in zip(previous, current, strict=True)]
    mean = sum(diffs) / len(diffs)
    cells = sum(1 for d in diffs if d > CELL_DELTA) / len(diffs)
    return mean >= CHANGE_MEAN or cells >= CHANGE_CELLS


class WatchScheduler:
    """Decides when the next tick is due. One tick runs at a time (single flight)."""

    def __init__(self, interval_s: float = DEFAULT_INTERVAL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = max(MIN_INTERVAL_S, float(interval_s))
        self._clock = clock
        self.running = False
        self.paused = False
        self.in_flight = False
        self.failures = 0
        self._last_tick: float | None = None
        self._last_window: object | None = None
        self._window_seen: object | None = None

    # -- control ----------------------------------------------------------------------------------

    def start(self) -> None:
        self.running, self.paused, self.in_flight, self.failures = True, False, False, 0
        self._last_tick = None  # the first tick is due at once
        self._last_window = self._window_seen = None

    def stop(self) -> None:
        self.running = self.paused = self.in_flight = False
        self.failures = 0

    def pause(self) -> None:
        if self.running:
            self.paused = True

    def resume(self) -> None:
        if self.running and self.paused:
            self.paused = False
            self._last_tick = None  # look again right away

    @property
    def active(self) -> bool:
        """Watching and not paused."""
        return self.running and not self.paused

    @property
    def current_interval_s(self) -> float:
        """The interval now: doubled per consecutive failure, capped."""
        return min(MAX_BACKOFF_S, self.interval_s * (2**self.failures))

    # -- ticking ----------------------------------------------------------------------------------

    def due(self, *, busy: bool = False, window: object | None = None) -> bool:
        """Whether to start a tick now. ``busy``: the user is using the model or capture pipeline."""
        if window is not None and window != self._window_seen:
            self._window_seen = window  # remembered even while busy, so the change is not lost
        if not self.active or self.in_flight or busy:
            return False
        if self._last_tick is None:
            return True
        elapsed = self._clock() - self._last_tick
        if elapsed < MIN_GAP_S:
            return False
        window_changed = self._window_seen is not None and self._window_seen != self._last_window
        return elapsed >= self.current_interval_s or (window_changed and self.failures == 0)

    def begin(self) -> None:
        self.in_flight = True
        self._last_tick = self._clock()
        self._last_window = self._window_seen

    def finish(self, ok: bool) -> bool:
        """End the tick. Returns ``True`` when watching should stop (too many failures in a row)."""
        self.in_flight = False
        if ok:
            self.failures = 0
            return False
        self.failures += 1
        return self.failures >= MAX_FAILURES


def parse_yes_no(reply: str) -> bool | None:
    """``True`` for YES, ``False`` for NO, ``None`` when the reply is neither (treated as NO by the caller)."""
    words = re.findall(r"[a-z]+", (reply or "").lower())
    if not words:
        return None
    return {"yes": True, "no": False}.get(words[0])


_NOTHING: Final[re.Pattern[str]] = re.compile(
    r"^(?:none|nothing|n/?a|no\b|there (?:is|are|was|were) no\b|i (?:do not|don't|can't|cannot|could not) |"
    r"cannot |can't |unable |not visible|no error)",
    re.IGNORECASE,
)
_MARKUP: Final[re.Pattern[str]] = re.compile(r"[*`]+|^[#>\s]+")  # emphasis and code marks, a leading heading or quote; never "_"
MAX_FINDING_CHARS: Final[int] = 240


def parse_watch_reply(reply: str) -> str | None:
    """The finding in the model's reply, or ``None`` when it says there is nothing wrong."""
    lines = [line for line in (reply or "").strip().splitlines() if line.strip()]
    if not lines:
        return None
    text = re.sub(r"\s+", " ", _MARKUP.sub("", lines[0])).strip().strip("\"'").strip()
    if not text or _NOTHING.match(text):
        return None
    if len(text) > MAX_FINDING_CHARS:
        text = text[: MAX_FINDING_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _key(finding: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", finding.lower()).strip()


class FindingTracker:
    """Reports each finding once: a persistent error notifies once, a cleared screen re-arms it."""

    def __init__(self) -> None:
        self._last: str | None = None

    @property
    def active(self) -> bool:
        """An error has been reported and the screen has not been clean since."""
        return self._last is not None

    def report(self, finding: str | None) -> str | None:
        """The finding if it is new since the last report, else ``None``."""
        if finding is None:
            self._last = None
            return None
        key = _key(finding)
        if key == self._last:
            return None
        self._last = key
        return finding

    def reset(self) -> None:
        self._last = None
