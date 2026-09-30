"""Thread-safe application state machine for the desktop client.

States move along an explicit transition table. Every accepted transition
emits ``state_changed(old, new)``; the HUD listens to animate its status pill.
Signals may be emitted from any thread: receivers living on the GUI thread get
them through Qt's queued connections.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from PyQt6.QtCore import QObject, pyqtSignal

from core.logger import get_logger
from network.schemas import AnalyzeResponse, LatencyMetrics

logger = get_logger("state")

MAX_HISTORY: Final[int] = 20


class AppState(str, Enum):
    IDLE = "idle"
    CAPTURING = "capturing"
    RECORDING_VOICE = "recording_voice"
    ANALYZING = "analyzing"
    DISPLAYING = "displaying"
    ERROR = "error"


ALLOWED_TRANSITIONS: Final[dict[AppState, frozenset[AppState]]] = {
    # ANALYZING is reachable without a capture for chat, which sends no screenshot.
    AppState.IDLE: frozenset({AppState.CAPTURING, AppState.RECORDING_VOICE, AppState.ANALYZING, AppState.ERROR}),
    AppState.RECORDING_VOICE: frozenset({AppState.CAPTURING, AppState.IDLE, AppState.ERROR}),
    AppState.CAPTURING: frozenset({AppState.ANALYZING, AppState.IDLE, AppState.ERROR}),
    AppState.ANALYZING: frozenset({AppState.DISPLAYING, AppState.ERROR, AppState.IDLE}),
    AppState.DISPLAYING: frozenset({AppState.IDLE, AppState.CAPTURING, AppState.RECORDING_VOICE, AppState.ANALYZING}),
    AppState.ERROR: frozenset({AppState.IDLE, AppState.CAPTURING, AppState.RECORDING_VOICE, AppState.ANALYZING}),
}

BUSY_STATES: Final[frozenset[AppState]] = frozenset(
    {AppState.CAPTURING, AppState.RECORDING_VOICE, AppState.ANALYZING}
)


@dataclass(frozen=True)
class HistoryEntry:
    response: AnalyzeResponse
    metrics: LatencyMetrics
    created_at: float = field(default_factory=time.time)


class StateMachine(QObject):
    """Owns the current ``AppState`` and a bounded history of results."""

    state_changed = pyqtSignal(object, object)  # (old AppState, new AppState)
    error_raised = pyqtSignal(str)
    history_changed = pyqtSignal(int)  # number of entries

    def __init__(self, max_history: int = MAX_HISTORY, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._state = AppState.IDLE
        self._history: deque[HistoryEntry] = deque(maxlen=max_history)
        self._last_error: str | None = None

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def is_busy(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def can_transition(self, new: AppState) -> bool:
        with self._lock:
            return new in ALLOWED_TRANSITIONS[self._state]

    def transition(self, new: AppState, reason: str = "") -> bool:
        """Move to ``new`` if the table allows it; returns whether it happened."""
        with self._lock:
            old = self._state
            if new == old:
                return True
            if new not in ALLOWED_TRANSITIONS[old]:
                logger.warning("rejected transition %s -> %s (%s)", old.value, new.value, reason or "no reason")
                return False
            self._state = new
            if new is not AppState.ERROR:
                self._last_error = None
        logger.debug("state %s -> %s %s", old.value, new.value, f"({reason})" if reason else "")
        self.state_changed.emit(old, new)
        return True

    def fail(self, message: str) -> None:
        """Enter ERROR from any state (forcing through IDLE if the table requires it)."""
        with self._lock:
            old = self._state
            if AppState.ERROR not in ALLOWED_TRANSITIONS[old] and old is not AppState.ERROR:
                self._state = AppState.IDLE
            self._last_error = message
        logger.error("error: %s", message)
        if not self.transition(AppState.ERROR, reason=message):
            with self._lock:
                self._state = AppState.ERROR
            self.state_changed.emit(old, AppState.ERROR)
        self.error_raised.emit(message)

    def reset(self) -> None:
        """Return to IDLE from a finished state (DISPLAYING / ERROR); no-op while busy."""
        if self.state in (AppState.DISPLAYING, AppState.ERROR):
            self.transition(AppState.IDLE, reason="dismissed")

    def add_result(self, response: AnalyzeResponse, metrics: LatencyMetrics) -> HistoryEntry:
        entry = HistoryEntry(response=response, metrics=metrics)
        with self._lock:
            self._history.append(entry)
            count = len(self._history)
        self.history_changed.emit(count)
        return entry

    def latest(self) -> HistoryEntry | None:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._history)

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
        logger.info("history cleared")
        self.history_changed.emit(0)
