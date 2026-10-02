"""One "Run command" approval, driven from the C# app over the bridge.

It does what ``RunDialog`` does, minus the drawing, and it is deliberately stricter about what the app can influence:

* the command is the text this session was opened with and is never taken from a later message, so the app cannot swap in
  another command after the person has read it;
* the working folder is re-checked every time it changes and again at the moment of running, because the same text can mean
  something else in another folder;
* ``execute`` needs the exact confirmation word every time, and the always-refused kinds never run, whatever is sent;
* the runner (``core.actions.ActionRunner``) re-verifies the approval, the refusal list and the audit log as it always did.

The command's output goes back to the app to be shown and nowhere else: never to the model.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from core.actions import (
    CONFIRM_WORD,
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    ActionRunner,
    Approval,
    RunResult,
    Verdict,
    check_command,
    shell_for,
)
from core.logger import get_logger
from PyQt6.QtCore import QObject, pyqtSignal

from ui.run_dialog import BANNER, SHELL_NAMES, RunWorker

logger = get_logger("bridge_run")

MIN_TIMEOUT_S: Final[float] = 5.0
MAX_FOLDER_CHARS: Final[int] = 1000


class BridgeRunSession(QObject):
    #: Emitted once when the session is over (the controller treats it like a closed dialog); the value is unused.
    finished = pyqtSignal(int)

    def __init__(
        self,
        key: str,
        command: str,
        language: str,
        runner: ActionRunner,
        cwd: Path,
        send: Callable[[dict[str, Any]], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.cwd = Path(cwd)
        self._command = command  # never replaced: the approval is built from exactly this string
        self._shell = shell_for(language)
        self._runner = runner
        self._send = send
        self._verdict: Verdict = check_command(command, self._shell, self.cwd)
        self._cancel = threading.Event()
        self._worker: RunWorker | None = None
        self._ran = False
        self._closed = False
        self._abandoned = False
        if self._verdict.refused:
            runner.refuse(command, self._shell, self._verdict.refused, str(self.cwd))
        send(
            {
                "event": "run_open",
                "id": key,
                "command": command,
                "shell": self._shell,
                "shell_name": SHELL_NAMES[self._shell],
                "banner": BANNER,
                "cwd": str(self.cwd),
                "confirm_word": CONFIRM_WORD,
                "timeout_s": DEFAULT_TIMEOUT_S,
                "min_timeout_s": MIN_TIMEOUT_S,
                "max_timeout_s": MAX_TIMEOUT_S,
                **self._verdict_fields(),
            }
        )

    # -- state ---------------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    @property
    def closed(self) -> bool:
        return self._closed

    def _verdict_fields(self) -> dict[str, Any]:
        return {"refused": self._verdict.refused, "warnings": list(self._verdict.warnings)}

    @staticmethod
    def _folder(text: str) -> Path:
        return Path(text.strip() or str(Path.home()))

    # -- what the app can ask for --------------------------------------------------------------------

    def check(self, cwd_text: str) -> None:
        """The working folder changed: the same text can mean something else there (``.`` or ``*`` in a protected folder)."""
        if self._closed or self.running:
            return
        self._verdict = check_command(self._command, self._shell, self._folder(cwd_text))
        self._send({"event": "run_verdict", "id": self.key, **self._verdict_fields()})

    def execute(self, typed: str, cwd_text: str, timeout_s: float) -> str | None:
        """Run the stored command once. Returns why not (for an ``error`` event), or ``None`` when it started."""
        if self._closed:
            return "that approval is closed"
        if self.running:
            return "a command is already running"
        folder = self._folder(cwd_text)
        self._verdict = check_command(self._command, self._shell, folder)  # judged in the folder it will really run in
        if self._verdict.refused:
            self._send({"event": "run_verdict", "id": self.key, **self._verdict_fields()})
            return f"Refused: {self._verdict.refused}"
        if typed != CONFIRM_WORD:
            return f"type {CONFIRM_WORD} (capital letters) to allow this"
        approval = Approval.create(self._command, self._shell, typed)
        seconds = min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, float(timeout_s)))
        self.cwd = folder
        self._cancel.clear()
        worker = RunWorker(self._runner, approval, folder, seconds, self._cancel)
        worker.finished_with.connect(self._on_finished)
        worker.refused.connect(self._on_refused)
        worker.finished.connect(self._on_worker_done)
        self._worker = worker
        self._send({"event": "run_state", "id": self.key, "state": "running", "message": "Running..."})
        worker.start()
        return None

    def cancel_or_close(self) -> None:
        """The Cancel / Stop / Close button: stops a running command, otherwise closes the approval."""
        if self.running:
            self._cancel.set()  # the runner ends the command and everything it started
            self._send({"event": "run_state", "id": self.key, "state": "stopping", "message": "Stopping..."})
            return
        self.close()

    def close(self) -> None:
        """Esc and the window's close button: never closes under a running command (that is stopped instead)."""
        if self._closed:
            return
        if self.running:
            self._cancel.set()
            return
        if not self._ran and not self._verdict.refused:
            self._runner.cancel_record(self._command, self._shell, str(self.cwd))
        self._finish()

    def abandon(self) -> None:
        """The app went away: stop what runs, end the approval, tell nobody."""
        self._abandoned = True
        if self.running:
            self._cancel.set()
            return
        if not self._closed:
            self._finish(announce=False)

    # -- worker callbacks ----------------------------------------------------------------------------

    def _on_finished(self, result: RunResult) -> None:
        self._ran = True
        self._send(
            {
                "event": "run_state",
                "id": self.key,
                "state": "finished",
                "output": result.output,
                "truncated": result.truncated,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "duration_s": round(result.duration_s, 2),
            }
        )

    def _on_refused(self, message: str) -> None:
        self._ran = True
        self._send({"event": "run_state", "id": self.key, "state": "refused", "message": message})

    def _on_worker_done(self) -> None:
        if self._abandoned and not self._closed:
            self._finish(announce=False)

    def _finish(self, *, announce: bool = True) -> None:
        self._closed = True
        if announce:
            self._send({"event": "run_closed", "id": self.key})
        self.finished.emit(0)
