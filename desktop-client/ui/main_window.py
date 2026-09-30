"""The OmniSight window: type or speak a question about your screen, click Capture, read the answers.

Everything the hotkeys do is reachable with the mouse here. The window is excluded from screen
capture (``WDA_EXCLUDEFROMCAPTURE``, like the HUD), so it never appears in what the model sees.
Widgets carry accessible names ("Ask box", "Send", "Capture", "Microphone", "Engine", ...) so UI
Automation can drive them in tests.

The window owns no logic: it emits signals and the controller in ``main.py`` acts on them.
"""

from __future__ import annotations

import html
from typing import Final

from core.config import ENGINE_CHOICES
from core.logger import get_logger
from core.node_supervisor import NodeState, NodeStatus
from core.state import AppState
from network.schemas import ClientResult
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.components import (
    BASE,
    BLUE,
    GREEN,
    MANTLE,
    OVERLAY0,
    PEACH,
    RED,
    SUBTEXT,
    SURFACE0,
    SURFACE1,
    TEXT,
    YELLOW,
)
from ui.hud import ActionWidget, DiagnosticWidget, SummaryWidget, exclude_from_capture

logger = get_logger("window")

MAX_EXCHANGES: Final[int] = 30
BUSY_STATES: Final[frozenset[AppState]] = frozenset({AppState.CAPTURING, AppState.ANALYZING})

_BUTTON = (
    f"QPushButton {{ background: {SURFACE0}; color: {TEXT}; border: 1px solid {SURFACE1}; border-radius: 8px; "
    f"padding: 7px 14px; font-weight: 600; }}"
    f"QPushButton:hover {{ background: {SURFACE1}; }}"
    f"QPushButton:disabled {{ color: {OVERLAY0}; background: {MANTLE}; }}"
)
_PRIMARY = (
    f"QPushButton {{ background: {BLUE}; color: {BASE}; border: none; border-radius: 8px; padding: 7px 18px; font-weight: 700; }}"
    f"QPushButton:hover {{ background: #B4D0FB; }}"
    f"QPushButton:disabled {{ background: {SURFACE1}; color: {OVERLAY0}; }}"
)
_RECORDING = (
    f"QPushButton {{ background: {RED}; color: {BASE}; border: none; border-radius: 8px; padding: 7px 14px; font-weight: 700; }}"
)


def _button(text: str, name: str, style: str = _BUTTON, tooltip: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setAccessibleName(name)
    button.setToolTip(tooltip or name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(style)
    return button


class ExchangeWidget(QFrame):
    """One question and its answer."""

    def __init__(self, question: str, result: ClientResult, parent: QWidget, searched: str = "") -> None:
        super().__init__(parent)
        response, metrics = result.response, result.metrics
        self.setStyleSheet(f"ExchangeWidget {{ background: {MANTLE}; border: 1px solid {SURFACE0}; border-radius: 10px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = QLabel(response.transcript and f"You said: “{response.transcript}”" or question or "Screen capture", self)
        heading.setWordWrap(True)
        heading.setStyleSheet(f"color: {SUBTEXT}; font-size: 9.5pt;")
        layout.addWidget(heading)
        summary = SummaryWidget(self)
        summary.setText(response.summary)
        layout.addWidget(summary)
        diagnostic = DiagnosticWidget(self)
        diagnostic.set_markdown(response.markdown, skip_leading=response.summary)
        layout.addWidget(diagnostic)
        actions = ActionWidget(self)
        actions.set_blocks([(block.language, block.code) for block in response.code_blocks])
        layout.addWidget(actions)
        if response.sources:
            lines = [f"Sources{f' (searched: {html.escape(searched)})' if searched else ''}"]
            for number, source in enumerate(response.sources, start=1):
                lines.append(
                    f'[{number}] <a href="{html.escape(source.url, quote=True)}" style="color: {BLUE};">{html.escape(source.title)}</a>'
                )
            sources = QLabel("<br>".join(lines), self)
            sources.setTextFormat(Qt.TextFormat.RichText)
            sources.setOpenExternalLinks(True)
            sources.setWordWrap(True)
            sources.setStyleSheet(f"color: {SUBTEXT}; font-size: 9pt;")
            layout.addWidget(sources)
        details = [f"{response.model_id} via {metrics.tier}", f"first token {metrics.server_ttft_ms / 1000:.1f}s"]
        if response.sources:
            details.append("web")
        if response.confidence is not None:
            details.append(f"confidence {response.confidence:.0%}")
        if response.finish_reason != "stop":
            details.append(f"stopped: {response.finish_reason}")
        meta = QLabel("  ·  ".join(details), self)
        meta.setStyleSheet(f"color: {OVERLAY0}; font-size: 8.5pt;")
        layout.addWidget(meta)


class MainWindow(QWidget):
    ask_requested = pyqtSignal(str)  # typed question ("" is not emitted; use capture_requested)
    capture_requested = pyqtSignal()
    mic_clicked = pyqtSignal()
    engine_selected = pyqtSignal(str)  # an ENGINE_CHOICES key
    node_toggle_clicked = pyqtSignal()
    clear_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    memory_toggled = pyqtSignal(bool)  # "Remember the conversation"
    speak_toggled = pyqtSignal(bool)  # "Speak answers"
    stop_speaking_clicked = pyqtSignal()
    search_toggled = pyqtSignal(bool)  # "Search the web"
    smart_toggled = pyqtSignal(bool)  # "Smart query"

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowTitle("OmniSight")
        self.resize(760, 780)
        self.setMinimumSize(560, 520)
        self.setStyleSheet(
            f"MainWindow {{ background: {BASE}; }} QLabel {{ color: {TEXT}; }}"
            f"QLineEdit, QComboBox {{ background: {SURFACE0}; color: {TEXT}; border: 1px solid {SURFACE1}; "
            f"border-radius: 8px; padding: 8px; font-size: 10.5pt; }}"
            f"QComboBox QAbstractItemView {{ background: {SURFACE0}; color: {TEXT}; selection-background-color: {SURFACE1}; }}"
        )
        self.capture_excluded = False
        self._exchanges: list[ExchangeWidget] = []
        self._node_running = False
        self._recording = False
        self._state = AppState.IDLE
        self._idle_text = "Ready."

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("OmniSight", self)
        title.setStyleSheet(f"color: {TEXT}; font-size: 15pt; font-weight: 800;")
        header.addWidget(title)
        header.addStretch(1)
        self.engine = QComboBox(self)
        self.engine.setAccessibleName("Engine")
        self.engine.setToolTip("Where the answer is computed")
        for key, (label, _backend, _device) in ENGINE_CHOICES.items():
            self.engine.addItem(label, key)
        self.engine.activated.connect(lambda index: self.engine_selected.emit(str(self.engine.itemData(index))))
        header.addWidget(self.engine)
        self.node_button = _button("Start local node", "Node", tooltip="Start or stop the model running on this PC")
        self.node_button.clicked.connect(self.node_toggle_clicked)
        header.addWidget(self.node_button)
        self.settings_button = _button("Settings", "Settings")
        self.settings_button.clicked.connect(self.settings_requested)
        header.addWidget(self.settings_button)
        root.addLayout(header)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {SUBTEXT}; font-size: 9.5pt;")
        root.addWidget(self.status)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget { background: transparent; }"
            f"QScrollBar:vertical {{ background: transparent; width: 9px; }}"
            f"QScrollBar::handle:vertical {{ background: {SURFACE1}; border-radius: 4px; min-height: 30px; }}"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
        )
        self._content = QWidget(self.scroll)
        self._list = QVBoxLayout(self._content)
        self._list.setContentsMargins(0, 0, 8, 0)
        self._list.setSpacing(12)
        self.empty_label = QLabel(
            "Ask a question about what is on your screen, or press Capture to have it explained.\n"
            "Hotkeys still work anywhere: Alt+C analyze, hold Alt+V to ask by voice.",
            self._content,
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {OVERLAY0}; font-size: 10.5pt; padding: 40px;")
        self._list.addWidget(self.empty_label)
        self._list.addStretch(1)
        self.scroll.setWidget(self._content)
        root.addWidget(self.scroll, 1)

        self.notice = QLabel("", self)
        self.notice.setWordWrap(True)
        self.notice.hide()
        root.addWidget(self.notice)

        ask_row = QHBoxLayout()
        self.ask_box = QLineEdit(self)
        self.ask_box.setAccessibleName("Ask box")
        self.ask_box.setPlaceholderText("Ask about your screen…  (Enter to send)")
        self.ask_box.setMaxLength(4000)
        self.ask_box.returnPressed.connect(self._send)
        ask_row.addWidget(self.ask_box, 1)
        self.send_button = _button("Send", "Send", _PRIMARY, "Capture the screen and ask this question")
        self.send_button.clicked.connect(self._send)
        ask_row.addWidget(self.send_button)
        root.addLayout(ask_row)

        options = QHBoxLayout()
        self.screen_check = QCheckBox("Include my screen", self)
        self.screen_check.setAccessibleName("Include screen")
        self.screen_check.setChecked(True)
        self.screen_check.setToolTip("Off: just chat, nothing is captured and no image is sent")
        self.screen_check.toggled.connect(self._screen_toggled)
        options.addWidget(self.screen_check)
        self.memory_check = QCheckBox("Remember the conversation", self)
        self.memory_check.setAccessibleName("Remember")
        self.memory_check.setChecked(True)
        self.memory_check.setToolTip("Keeps the last few questions and answers (text only) so follow-ups make sense")
        self.memory_check.toggled.connect(self.memory_toggled)
        options.addWidget(self.memory_check)
        self.speak_check = QCheckBox("Speak answers", self)
        self.speak_check.setAccessibleName("Speak")
        self.speak_check.setToolTip("Read the summary of each answer aloud with the Windows voice")
        self.speak_check.toggled.connect(self.speak_toggled)
        options.addWidget(self.speak_check)
        self.search_check = QCheckBox("Search the web", self)
        self.search_check.setAccessibleName("Search web")
        self.search_check.setToolTip(
            "Off by default. On: your typed question is sent as a search to Stack Overflow and Wikipedia "
            "(free, no account) and the top results are quoted to the model. Your screen is never searched."
        )
        self.search_check.toggled.connect(self._search_changed)
        options.addWidget(self.search_check)
        self.smart_check = QCheckBox("Smart query", self)
        self.smart_check.setAccessibleName("Smart query")
        self.smart_check.setEnabled(False)
        self.smart_check.setToolTip("Let the model rewrite your question into search keywords first (one extra short model call)")
        self.smart_check.toggled.connect(self.smart_toggled)
        options.addWidget(self.smart_check)
        options.addStretch(1)
        self.stop_speaking_button = _button("Stop voice", "Stop voice", tooltip="Stop reading the answer aloud (Esc does this too)")
        self.stop_speaking_button.clicked.connect(self.stop_speaking_clicked)
        self.stop_speaking_button.hide()
        options.addWidget(self.stop_speaking_button)
        root.addLayout(options)
        for box in (self.screen_check, self.memory_check, self.speak_check, self.search_check, self.smart_check):
            box.setStyleSheet(f"QCheckBox {{ color: {TEXT}; spacing: 6px; }}")

        buttons = QHBoxLayout()
        self.capture_button = _button("Capture screen", "Capture", tooltip="Explain what is on the screen (same as Alt+C)")
        self.capture_button.clicked.connect(self.capture_requested)
        buttons.addWidget(self.capture_button)
        self.mic_button = _button("Speak", "Microphone", tooltip="Click, ask out loud, click again to send")
        self.mic_button.clicked.connect(self.mic_clicked)
        buttons.addWidget(self.mic_button)
        buttons.addStretch(1)
        self.clear_button = _button("Clear", "Clear", tooltip="Clear the answers shown here")
        self.clear_button.clicked.connect(self.clear_requested)
        buttons.addWidget(self.clear_button)
        root.addLayout(buttons)

        self._refresh_controls()

    # -- outgoing actions ------------------------------------------------------------

    @property
    def include_screen(self) -> bool:
        return self.screen_check.isChecked()

    def _screen_toggled(self, on: bool) -> None:
        self.ask_box.setPlaceholderText(
            "Ask about your screen…  (Enter to send)" if on else "Message OmniSight (no screenshot is sent)…  (Enter to send)"
        )
        self.send_button.setToolTip("Capture the screen and ask this question" if on else "Send this message without the screen")

    def _search_changed(self, on: bool) -> None:
        self.smart_check.setEnabled(on)
        self.search_toggled.emit(on)

    def set_search_enabled(self, on: bool) -> None:
        self._set_checked(self.search_check, on)
        self.smart_check.setEnabled(on)

    def set_smart_enabled(self, on: bool) -> None:
        self._set_checked(self.smart_check, on)

    def set_progress(self, text: str) -> None:
        """A transient status line while the controller works on something other than the model."""
        self.status.setText(text)

    def set_memory_enabled(self, on: bool) -> None:
        self._set_checked(self.memory_check, on)

    def set_speak_enabled(self, on: bool) -> None:
        self._set_checked(self.speak_check, on)

    def set_speak_available(self, available: bool) -> None:
        self.speak_check.setEnabled(available)
        if not available:
            self.speak_check.setToolTip("Spoken answers need Windows PowerShell and the built-in voice")

    def set_speaking(self, speaking: bool) -> None:
        self.stop_speaking_button.setVisible(speaking)

    @staticmethod
    def _set_checked(box: QCheckBox, on: bool) -> None:
        box.blockSignals(True)
        box.setChecked(on)
        box.blockSignals(False)

    def _send(self) -> None:
        text = self.ask_box.text().strip()
        if not text or not self.send_button.isEnabled():
            return
        self.ask_box.clear()
        self.ask_requested.emit(text)

    # -- state from the controller ------------------------------------------------------

    def set_engine(self, key: str) -> None:
        index = self.engine.findData(key)
        if index >= 0 and index != self.engine.currentIndex():
            self.engine.setCurrentIndex(index)
        self._refresh_controls()

    def set_app_state(self, state: AppState, message: str = "") -> None:
        self._state = state
        self._recording = state is AppState.RECORDING_VOICE
        if message:
            self.status.setText(message)
        elif state in (AppState.IDLE, AppState.DISPLAYING):
            self.status.setText(self._idle_text)
        self._refresh_controls()

    def _set_idle_text(self, text: str) -> None:
        """What the status line says when nothing is running (the node's state)."""
        self._idle_text = text
        if self._state not in BUSY_STATES and not self._recording:
            self.status.setText(text)

    def set_node_status(self, status: NodeStatus, engine_key: str) -> None:
        """Show the local node's state (the device it *actually* runs on) for This-PC engines."""
        local = engine_key.startswith("local") or engine_key == "auto"
        self._node_running = status.state in (NodeState.STARTING, NodeState.READY) and status.owned
        if status.state is NodeState.READY:
            self._set_idle_text(f"Local node ready on {status.actual_device}.")
        elif status.state is NodeState.STARTING:
            self._set_idle_text(f"Local node starting: {status.message}")
        elif status.state is NodeState.FAILED:
            self._set_idle_text("The local node is not running.")
            self.show_notice(status.message, error=True)
        elif engine_key.startswith("local"):
            self._set_idle_text("The local node is not running. Press “Start local node”.")
        else:
            self._set_idle_text("Ready.")
        self.node_button.setText("Stop local node" if self._node_running else "Start local node")
        self.node_button.setVisible(local)
        self._refresh_controls()

    def show_notice(self, text: str, *, error: bool = False) -> None:
        color = RED if error else YELLOW
        self.notice.setStyleSheet(
            f"color: {color}; background: {MANTLE}; border: 1px solid {SURFACE1}; border-radius: 8px; padding: 8px 12px;"
        )
        self.notice.setText(text)
        self.notice.show()
        QTimer.singleShot(20000, self._hide_stale_notice)

    def _hide_stale_notice(self) -> None:
        if self.notice.isVisible() and self._state not in BUSY_STATES:
            self.notice.hide()

    def add_exchange(self, question: str, result: ClientResult, searched: str = "") -> None:
        self.notice.hide()
        self.empty_label.hide()
        widget = ExchangeWidget(question, result, self._content, searched)
        self._list.insertWidget(self._list.count() - 1, widget)
        self._exchanges.append(widget)
        while len(self._exchanges) > MAX_EXCHANGES:
            old = self._exchanges.pop(0)
            self._list.removeWidget(old)
            old.deleteLater()
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def clear_exchanges(self) -> None:
        for widget in self._exchanges:
            self._list.removeWidget(widget)
            widget.deleteLater()
        self._exchanges.clear()
        self.empty_label.show()
        self.notice.hide()

    @property
    def exchange_count(self) -> int:
        return len(self._exchanges)

    def _refresh_controls(self) -> None:
        busy = self._state in BUSY_STATES
        idle_for_input = not busy and not self._recording
        self.send_button.setEnabled(idle_for_input)
        self.capture_button.setEnabled(idle_for_input)
        self.ask_box.setEnabled(not busy and not self._recording)
        self.mic_button.setEnabled(not busy)
        self.mic_button.setText("Stop and send" if self._recording else "Speak")
        self.mic_button.setStyleSheet(_RECORDING if self._recording else _BUTTON)
        self.engine.setEnabled(not busy and not self._recording)
        self.clear_button.setEnabled(idle_for_input)
        self.screen_check.setEnabled(idle_for_input)
        self.search_check.setEnabled(idle_for_input)
        if busy and not self.status.text():
            self.status.setText("Working…")
        color = {AppState.ERROR: RED, AppState.CAPTURING: BLUE, AppState.ANALYZING: BLUE, AppState.RECORDING_VOICE: PEACH}.get(
            self._state, GREEN
        )
        self.status.setStyleSheet(f"color: {color if busy or self._recording else SUBTEXT}; font-size: 9.5pt;")

    # -- window behavior ---------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self.capture_excluded:
            self.capture_excluded = exclude_from_capture(self)
            logger.info("window excluded from screen capture: %s", self.capture_excluded)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Closing the window keeps OmniSight running in the tray."""
        event.ignore()
        self.hide()
