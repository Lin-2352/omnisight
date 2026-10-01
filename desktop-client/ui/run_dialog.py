"""The "Run command" dialog: the exact command, what it could do, and a typed confirmation.

Nothing here runs by itself. The dialog shows the text it was given and never edits it; **Run command** stays
disabled until ``RUN`` (capital letters) has been typed and the command is not one of the always-refused
kinds. There is no default button, so pressing Enter cannot run (or cancel) anything by accident; Esc
cancels. One approval runs one command: after it finishes the confirmation box is cleared.
"""

from __future__ import annotations

import threading
from pathlib import Path

from core.actions import (
    CONFIRM_WORD,
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    ActionRefused,
    ActionRunner,
    Approval,
    RunResult,
    check_command,
    shell_for,
)
from core.logger import get_logger
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QFont, QShowEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.components import BASE, BLUE, MANTLE, OVERLAY0, PEACH, RED, SUBTEXT, SURFACE0, SURFACE1, TEXT, YELLOW
from ui.hud import exclude_from_capture

logger = get_logger("run_dialog")

BANNER = (
    "This command was written by an AI model from your screen and your question. Text on the screen or on a web "
    "page can influence what it writes, so read it before you run it."
)
SHELL_NAMES = {"powershell": "Windows PowerShell", "cmd": "Command Prompt (cmd)"}


class RunWorker(QThread):
    """Runs one approved command off the GUI thread."""

    finished_with = pyqtSignal(object)  # RunResult
    refused = pyqtSignal(str)

    def __init__(self, runner: ActionRunner, approval: Approval, cwd: Path, timeout_s: float, cancel: threading.Event) -> None:
        super().__init__()
        self._runner, self._approval, self._cwd, self._timeout_s, self._cancel = runner, approval, cwd, timeout_s, cancel
        self.setObjectName("omnisight-action")

    def run(self) -> None:
        try:
            self.finished_with.emit(self._runner.run(self._approval, self._cwd, self._timeout_s, self._cancel))
        except ActionRefused as exc:
            self.refused.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - never let the worker thread die silently
            logger.exception("command worker failed")
            self.refused.emit(f"Unexpected error: {type(exc).__name__}: {exc}")


def _button(text: str, name: str, tooltip: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setAccessibleName(name)
    button.setAutoDefault(False)
    button.setDefault(False)
    button.setToolTip(tooltip)
    button.setStyleSheet(
        f"QPushButton {{ background: {SURFACE0}; color: {TEXT}; border: 1px solid {SURFACE1}; border-radius: 8px; padding: 7px 16px; font-weight: 600; }}"
        f"QPushButton:hover {{ background: {SURFACE1}; }} QPushButton:disabled {{ color: {OVERLAY0}; background: {MANTLE}; }}"
    )
    return button


class RunDialog(QDialog):
    def __init__(self, command: str, language: str, runner: ActionRunner, cwd: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._command = command  # never edited; the approval is built from exactly this string
        self._shell = shell_for(language)
        self._runner = runner
        self._verdict = check_command(command, self._shell, Path(cwd))
        self._worker: RunWorker | None = None
        self._cancel = threading.Event()
        self._ran = False
        self.cwd = Path(cwd)
        self.capture_excluded = False
        self.result: RunResult | None = None
        self.setWindowTitle("Run command")
        self.setModal(True)
        self.setMinimumWidth(640)
        self.setStyleSheet(f"QDialog {{ background: {BASE}; }} QLabel {{ color: {TEXT}; }}")

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        banner = QLabel(BANNER, self)
        banner.setWordWrap(True)
        banner.setStyleSheet(f"color: {YELLOW}; background: {MANTLE}; border: 1px solid {SURFACE1}; border-radius: 8px; padding: 8px 12px;")
        layout.addWidget(banner)
        layout.addWidget(QLabel(f"Shell: {SHELL_NAMES[self._shell]}", self))

        self.command_box = QPlainTextEdit(self)
        self.command_box.setAccessibleName("Command")
        self.command_box.setReadOnly(True)
        self.command_box.setPlainText(command)
        self.command_box.setFont(QFont("Consolas", 10))
        self.command_box.setMinimumHeight(110)
        self.command_box.setStyleSheet(f"QPlainTextEdit {{ background: {SURFACE0}; color: {TEXT}; border: 1px solid {SURFACE1}; border-radius: 8px; padding: 6px; }}")
        layout.addWidget(self.command_box)

        self.verdict_label = QLabel("", self)
        self.verdict_label.setWordWrap(True)
        if self._verdict.refused:
            self.verdict_label.setText(f"Refused: {self._verdict.refused} OmniSight will not run this command, even if you approve it.")
            self.verdict_label.setStyleSheet(f"color: {RED}; font-weight: 700;")
        elif self._verdict.warnings:
            self.verdict_label.setText("Take a second look: this command " + "; ".join(self._verdict.warnings) + ".")
            self.verdict_label.setStyleSheet(f"color: {PEACH};")
        else:
            self.verdict_label.setVisible(False)
        layout.addWidget(self.verdict_label)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Working folder:", self))
        self.folder_edit = QLineEdit(str(self.cwd), self)
        self.folder_edit.setAccessibleName("Working folder")
        self.folder_edit.setStyleSheet(f"QLineEdit {{ background: {SURFACE0}; color: {TEXT}; border: 1px solid {SURFACE1}; border-radius: 6px; padding: 5px; }}")
        folder_row.addWidget(self.folder_edit, 1)
        self.browse_button = _button("Browse...", "Browse folder")
        self.browse_button.clicked.connect(self._browse)
        folder_row.addWidget(self.browse_button)
        folder_row.addWidget(QLabel("Stop after", self))
        self.timeout_box = QSpinBox(self)
        self.timeout_box.setAccessibleName("Timeout seconds")
        self.timeout_box.setRange(5, int(MAX_TIMEOUT_S))
        self.timeout_box.setValue(int(DEFAULT_TIMEOUT_S))
        self.timeout_box.setSuffix(" s")
        folder_row.addWidget(self.timeout_box)
        layout.addLayout(folder_row)

        confirm_row = QHBoxLayout()
        confirm_row.addWidget(QLabel(f"To allow this, type {CONFIRM_WORD}:", self))
        self.confirm_edit = QLineEdit(self)
        self.confirm_edit.setAccessibleName("Confirm text")
        self.confirm_edit.setPlaceholderText(f"type {CONFIRM_WORD}")
        self.confirm_edit.setMaxLength(16)
        self.confirm_edit.setStyleSheet(f"QLineEdit {{ background: {SURFACE0}; color: {TEXT}; border: 1px solid {SURFACE1}; border-radius: 6px; padding: 5px; }}")
        self.confirm_edit.textChanged.connect(self._refresh)
        self.folder_edit.textChanged.connect(self._recheck)
        confirm_row.addWidget(self.confirm_edit, 1)
        layout.addLayout(confirm_row)

        buttons = QHBoxLayout()
        self.run_button = _button("Run command", "Run command", "Runs exactly the command shown above, once")
        self.run_button.clicked.connect(self._run)
        buttons.addWidget(self.run_button)
        self.cancel_button = _button("Cancel", "Cancel", "Close without running (Esc does the same)")
        self.cancel_button.clicked.connect(self._cancel_clicked)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {SUBTEXT};")
        layout.addWidget(self.status)
        self.output_box = QPlainTextEdit(self)
        self.output_box.setAccessibleName("Output")
        self.output_box.setReadOnly(True)
        self.output_box.setFont(QFont("Consolas", 9))
        self.output_box.setMinimumHeight(120)
        self.output_box.setStyleSheet(f"QPlainTextEdit {{ background: {MANTLE}; color: {TEXT}; border: 1px solid {SURFACE1}; border-radius: 8px; padding: 6px; }}")
        self.output_box.setVisible(False)
        layout.addWidget(self.output_box)

        if self._verdict.refused:
            self._runner.refuse(command, self._shell, self._verdict.refused, str(self.cwd))
        self._refresh()

    # -- state ------------------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _approved(self) -> bool:
        return self.confirm_edit.text() == CONFIRM_WORD and not self._verdict.refused and not self.running

    def _recheck(self) -> None:
        """The working folder changed: the same text can mean something else (``.`` or ``*`` in a protected folder)."""
        folder = Path(self.folder_edit.text().strip() or str(Path.home()))
        self._verdict = check_command(self._command, self._shell, folder)
        if self._verdict.refused:
            self.verdict_label.setText(f"Refused: {self._verdict.refused} OmniSight will not run this command, even if you approve it.")
            self.verdict_label.setStyleSheet(f"color: {RED}; font-weight: 700;")
            self.verdict_label.setVisible(True)
        elif self._verdict.warnings:
            self.verdict_label.setText("Take a second look: this command " + "; ".join(self._verdict.warnings) + ".")
            self.verdict_label.setStyleSheet(f"color: {PEACH};")
            self.verdict_label.setVisible(True)
        else:
            self.verdict_label.setVisible(False)
        self._refresh()

    def _refresh(self) -> None:
        self.run_button.setEnabled(self._approved())
        self.cancel_button.setText("Stop" if self.running else ("Close" if self._ran else "Cancel"))
        for widget in (self.confirm_edit, self.folder_edit, self.browse_button, self.timeout_box):
            widget.setEnabled(not self.running)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Working folder", self.folder_edit.text())
        if chosen:
            self.folder_edit.setText(chosen)

    # -- actions ----------------------------------------------------------------------------------------

    def _run(self) -> None:
        if not self._approved():
            return
        self.cwd = Path(self.folder_edit.text().strip() or str(Path.home()))
        approval = Approval.create(self._command, self._shell, self.confirm_edit.text())
        self._cancel.clear()
        self.output_box.clear()
        self.output_box.setVisible(True)
        self.status.setText("Running...")
        self._worker = RunWorker(self._runner, approval, self.cwd, float(self.timeout_box.value()), self._cancel)
        self._worker.finished_with.connect(self._finished)
        self._worker.refused.connect(self._refused)
        self._worker.finished.connect(self._refresh)
        self._worker.start()
        self._refresh()

    def _finished(self, result: RunResult) -> None:
        self.result = result
        self._ran = True
        self.output_box.setPlainText(result.output + ("\n[output cut at 64 KB]" if result.truncated else ""))
        if result.timed_out:
            self.status.setText(f"Stopped: it did not finish within {self.timeout_box.value()} s. Everything it started was ended.")
        elif result.cancelled:
            self.status.setText("Stopped by you. Everything it started was ended.")
        else:
            self.status.setText(f"Finished with exit code {result.exit_code} in {result.duration_s:.1f} s.")
        self.confirm_edit.clear()  # one approval, one run
        self._refresh()

    def _refused(self, message: str) -> None:
        self._ran = True
        self.status.setText(message)
        self.status.setStyleSheet(f"color: {RED};")
        self.confirm_edit.clear()
        self._refresh()

    def _cancel_clicked(self) -> None:
        if self.running:
            self._cancel.set()  # Stop: the runner ends the command and its children
            self.status.setText("Stopping...")
            return
        self.reject()

    def reject(self) -> None:  # noqa: D401 - Qt API (Esc and the window close button)
        if self.running:
            self._cancel.set()
            return
        if not self._ran and not self._verdict.refused:
            self._runner.cancel_record(self._command, self._shell, str(self.cwd))
        super().reject()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        """The dialog and the command's output must never appear in a screenshot a model could read (like the HUD and the window)."""
        super().showEvent(event)
        if not self.capture_excluded:
            self.capture_excluded = exclude_from_capture(self)
            logger.info("run dialog excluded from screen capture: %s", self.capture_excluded)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.running:
            self._cancel.set()
            self._worker.wait(10_000)  # type: ignore[union-attr]
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802, ANN001 - Qt API
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()  # Enter never runs anything (and does not close the dialog either)
            return
        super().keyPressEvent(event)
