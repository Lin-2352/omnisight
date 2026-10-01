"""Frameless, translucent, always-on-top HUD overlay (PyQt6).

The window never steals focus (``WA_ShowWithoutActivating``), is excluded from
screen captures via ``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` so it
never shows up in its own screenshots, and fades in and out by animating
``windowOpacity``. It must only be touched from the GUI thread.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from typing import Final

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QScreen
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.logger import get_logger
from core.state import AppState
from core.answer import body_segments, copy_actions
from network.schemas import ClientResult
from ui.components import (
    BLUE,
    GREEN,
    OVERLAY0,
    RED,
    SUBTEXT,
    SURFACE0,
    SURFACE1,
    TEXT,
    YELLOW,
    CodeBlockWidget,
    CopyButton,
    LatencyBadge,
    Spinner,
    StatusPill,
    Toast,
)

logger = get_logger("ui.hud")

HUD_WIDTH: Final[int] = 580
SHADOW_MARGIN: Final[int] = 18
WDA_EXCLUDEFROMCAPTURE: Final[int] = 0x00000011

STATE_STYLE: Final[dict[AppState, tuple[str, str, bool]]] = {
    AppState.IDLE: ("online", GREEN, False),
    AppState.CAPTURING: ("capturing", BLUE, True),
    AppState.RECORDING_VOICE: ("listening", YELLOW, True),
    AppState.ANALYZING: ("analyzing", BLUE, True),
    AppState.DISPLAYING: ("online", GREEN, False),
    AppState.ERROR: ("error", RED, False),
}


def exclude_from_capture(window: QWidget) -> bool:
    """Hide ``window`` from screen capture (Windows 10 2004+). Returns True on success."""
    if os.name != "nt":
        return False
    try:
        hwnd = int(window.winId())
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE))
    except (AttributeError, OSError):
        return False


class TitleBar(QWidget):
    """Draggable header: brand, status pill, latency badge, minimize and close buttons."""

    close_clicked = pyqtSignal()
    minimize_clicked = pyqtSignal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._drag_offset: QPoint | None = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 8, 6)
        layout.setSpacing(8)
        brand = QLabel("OmniSight", self)
        brand.setStyleSheet(f"color: {TEXT}; font-weight: 700; font-size: 10.5pt;")
        layout.addWidget(brand)
        self.pill = StatusPill(self)
        layout.addWidget(self.pill)
        self.badge = LatencyBadge(self)
        layout.addWidget(self.badge)
        layout.addStretch(1)
        self.minimize_button = self._icon_button("–", "Collapse / expand")
        self.minimize_button.clicked.connect(self.minimize_clicked)
        layout.addWidget(self.minimize_button)
        self.close_button = self._icon_button("✕", "Hide (Esc)")
        self.close_button.clicked.connect(self.close_clicked)
        layout.addWidget(self.close_button)

    def _icon_button(self, glyph: str, tooltip: str) -> QPushButton:
        button = QPushButton(glyph, self)
        button.setToolTip(tooltip)
        button.setFixedSize(26, 24)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton {{ color: {SUBTEXT}; background: transparent; border: none; border-radius: 6px; font-size: 10pt; }}"
            f"QPushButton:hover {{ background: {SURFACE0}; color: {TEXT}; }}"
        )
        return button

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        self._drag_offset = None


class SummaryWidget(QLabel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setStyleSheet(f"color: {TEXT}; font-size: 11.5pt; font-weight: 700; line-height: 130%;")


class DiagnosticWidget(QWidget):
    """Markdown answer: prose through Qt's markdown renderer, code through ``CodeBlockWidget``."""

    copied = pyqtSignal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        self.code_blocks: list[CodeBlockWidget] = []

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.code_blocks = []

    def set_markdown(self, markdown: str, skip_leading: str = "") -> None:
        """Render ``markdown``; drop its first paragraph if it only repeats ``skip_leading`` (the summary)."""
        self.clear()
        for segment in body_segments(markdown, skip_leading):
            if segment.kind == "code":
                block = CodeBlockWidget(segment.text, segment.language, self)
                block.copied.connect(self.copied)
                self.code_blocks.append(block)
                self._layout.addWidget(block)
            else:
                label = QLabel(self)
                label.setTextFormat(Qt.TextFormat.MarkdownText)
                label.setText(segment.text)
                label.setWordWrap(True)
                label.setOpenExternalLinks(True)
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse
                )
                label.setStyleSheet(f"color: {TEXT}; font-size: 10pt;")
                label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
                self._layout.addWidget(label)


class ActionWidget(QWidget):
    """``Copy Fix`` (first non-shell block) and ``Copy Terminal Command`` (first shell block)."""

    copied = pyqtSignal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.copy_fix = CopyButton("Copy Fix", "", GREEN, self)
        self.copy_command = CopyButton("Copy Terminal Command", "", BLUE, self)
        for button in (self.copy_fix, self.copy_command):
            button.copied.connect(self.copied)
            layout.addWidget(button)
        layout.addStretch(1)

    def set_blocks(self, blocks: list[tuple[str, str]]) -> None:
        """``blocks`` is ``[(language, code), ...]`` in document order."""
        fix, command = copy_actions(blocks)
        self.copy_fix.set_payload(fix)
        self.copy_command.set_payload(command)


class HudWindow(QWidget):
    """The floating overlay."""

    dismissed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowTitle("OmniSight")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(HUD_WIDTH + 2 * SHADOW_MARGIN)
        self.capture_excluded = False
        self._collapsed = False
        self._screen: QScreen | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        self.panel.setStyleSheet(
            f"QFrame#panel {{ background: rgba(30, 30, 46, 242); border: 1px solid {SURFACE1}; border-radius: 14px; }}"
        )
        shadow = QGraphicsDropShadowEffect(self.panel)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.panel.setGraphicsEffect(shadow)
        outer.addWidget(self.panel)

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 12)
        panel_layout.setSpacing(0)
        self.title_bar = TitleBar(self.panel)
        self.title_bar.close_clicked.connect(self.dismiss)
        self.title_bar.minimize_clicked.connect(self.toggle_collapsed)
        panel_layout.addWidget(self.title_bar)

        divider = QFrame(self.panel)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {SURFACE0};")
        panel_layout.addWidget(divider)

        self.body = QWidget(self.panel)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(16, 12, 16, 0)
        body_layout.setSpacing(12)

        status_row = QHBoxLayout()
        self.spinner = Spinner(20, BLUE, self.body)
        self.status_label = QLabel(self.body)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {SUBTEXT}; font-size: 10pt;")
        status_row.addWidget(self.spinner)
        status_row.addWidget(self.status_label, 1)
        body_layout.addLayout(status_row)

        # Optional buttons under an error message (e.g. "Open microphone settings").
        self.error_actions = QWidget(self.body)
        error_actions_layout = QHBoxLayout(self.error_actions)
        error_actions_layout.setContentsMargins(28, 0, 0, 0)
        error_actions_layout.setSpacing(8)
        self._error_buttons: list[QPushButton] = []
        for _ in range(2):
            button = QPushButton(self.error_actions)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ color: {BLUE}; background: {SURFACE0}; border: 1px solid {SURFACE1}; "
                f"border-radius: 7px; padding: 5px 12px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {SURFACE1}; }}"
            )
            button.hide()
            error_actions_layout.addWidget(button)
            self._error_buttons.append(button)
        error_actions_layout.addStretch(1)
        self.error_actions.hide()
        body_layout.addWidget(self.error_actions)

        self.scroll = QScrollArea(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; }} QScrollArea > QWidget > QWidget {{ background: transparent; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 8px; }}"
            f"QScrollBar::handle:vertical {{ background: {SURFACE1}; border-radius: 4px; min-height: 30px; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}"
        )
        content = QWidget(self.scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 6, 0)
        content_layout.setSpacing(12)
        self.summary = SummaryWidget(content)
        content_layout.addWidget(self.summary)
        self.diagnostic = DiagnosticWidget(content)
        content_layout.addWidget(self.diagnostic)
        self.meta = QLabel(content)
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet(f"color: {OVERLAY0}; font-size: 8.5pt;")
        content_layout.addWidget(self.meta)
        content_layout.addStretch(1)
        self.scroll.setWidget(content)
        body_layout.addWidget(self.scroll, 1)

        self.actions = ActionWidget(self.body)
        body_layout.addWidget(self.actions)
        panel_layout.addWidget(self.body, 1)

        self.toast = Toast(self.panel)
        self.diagnostic.copied.connect(lambda _: self.toast.show_message("Copied!"))
        self.actions.copied.connect(lambda _: self.toast.show_message("Copied!"))

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)
        self._fading_out = False
        self.set_idle()

    # -- geometry ---------------------------------------------------------------

    def place_on_screen(self, native_rect: tuple[int, int, int, int] | None = None, device_name: str | None = None) -> None:
        """Top-right of the screen that was captured, else the primary screen.

        ``native_rect`` is the captured monitor in physical pixels (mss). Qt 6 keeps each
        screen's native top-left as its geometry origin, so screens match on that; the
        device name is a secondary key (Qt often reports the panel model instead).
        """
        screens = QGuiApplication.screens()
        screen = None
        if native_rect is not None:
            left, top = native_rect[0], native_rect[1]
            screen = next((s for s in screens if (s.geometry().x(), s.geometry().y()) == (left, top)), None)
            if screen is None:
                screen = next(
                    (
                        s
                        for s in screens
                        if s.geometry().x() <= left / s.devicePixelRatio() < s.geometry().x() + s.geometry().width()
                        and s.geometry().y() <= top / s.devicePixelRatio() < s.geometry().y() + s.geometry().height()
                    ),
                    None,
                )
        if screen is None and device_name:
            screen = next((s for s in screens if s.name() == device_name), None)
        self._screen = screen or QGuiApplication.primaryScreen()
        self._apply_geometry()

    def _apply_geometry(self) -> None:
        screen = self._screen or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area: QRect = screen.availableGeometry()
        max_height = int(area.height() * 0.78)
        if self.scroll.isVisible() or not self.scroll.isHidden():
            # Word-wrapped labels only report their true height for a given width, so size
            # the scroll area from the content's height-for-width, capped to the screen.
            content = self.scroll.widget()
            content_width = max(100, self.scroll.viewport().width() or HUD_WIDTH - 40)
            layout = content.layout()
            content_height = (
                layout.heightForWidth(content_width) if layout is not None and layout.hasHeightForWidth() else -1
            )
            if content_height <= 0:
                content_height = content.sizeHint().height()
            self.scroll.setFixedHeight(1)
            chrome = self.sizeHint().height() - 1
            available = max_height - chrome
            fits = content_height + 12 <= available
            self.scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff if fits else Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            self.scroll.setFixedHeight(max(60, min(content_height + 12, available)))
        height = min(max(self.sizeHint().height(), 150), max_height)
        self.resize(self.width(), height)
        self.move(area.right() - self.width() + SHADOW_MARGIN - 12, area.top() + 12)

    # -- states -----------------------------------------------------------------

    def set_idle(self) -> None:
        self.title_bar.pill.set_state("online", GREEN)
        self.spinner.stop()
        self.status_label.setText("Alt+C analyzes the active screen. Hold Alt+V to ask by voice.")
        self.status_label.show()

    def show_state(self, state: AppState, message: str = "") -> None:
        text, color, pulsing = STATE_STYLE[state]
        self.title_bar.pill.set_state(text, color, pulsing)
        busy = state in (AppState.CAPTURING, AppState.RECORDING_VOICE, AppState.ANALYZING)
        if busy:
            self.spinner.start()
            self.status_label.setText(message or text.capitalize() + "…")
            self.status_label.show()
            self.title_bar.badge.set_latency(None)
            self._clear_result()
            self.scroll.hide()
            self.actions.hide()
        else:
            self.spinner.stop()
        if not self.isVisible():
            self.fade_in()
        self._apply_geometry()

    def set_tier(self, tier: str) -> None:
        self.title_bar.badge.set_tier(tier)
        self.status_label.setText(f"Analyzing via {tier} endpoint…")

    def show_result(self, result: ClientResult) -> None:
        response, metrics = result.response, result.metrics
        self.title_bar.pill.set_state("online", GREEN)
        self.title_bar.badge.set_latency(metrics.end_to_end_ms, metrics.tier)
        self.spinner.stop()
        self.status_label.hide()
        self.summary.setText(response.summary)
        self.diagnostic.set_markdown(response.markdown, skip_leading=response.summary)
        self.actions.set_blocks([(block.language, block.code) for block in response.code_blocks])
        details = [
            f"{response.model_id} via {metrics.tier}",
            f"first token {metrics.server_ttft_ms / 1000:.1f}s",
            f"{metrics.tokens_generated} tokens at {metrics.tokens_per_sec:.1f}/s",
        ]
        if response.confidence is not None:
            details.append(f"confidence {response.confidence:.0%}")
        if response.finish_reason != "stop":
            details.append(f"stopped: {response.finish_reason}")
        meta = "  ·  ".join(details)
        if response.transcript:
            meta = f"“{response.transcript}”\n{meta}"
        self.meta.setText(meta)
        self.scroll.show()
        self.actions.show()
        self.scroll.verticalScrollBar().setValue(0)
        if not self.isVisible():
            self.fade_in()
        self._apply_geometry()

    def show_error(self, message: str, actions: list[tuple[str, Callable[[], None]]] | None = None) -> None:
        """Show ``message`` in red, with up to two optional ``(label, callback)`` buttons below it."""
        self.title_bar.pill.set_state("error", RED)
        self.title_bar.badge.set_latency(None)
        self.spinner.stop()
        self._clear_result()
        self._set_error_actions(actions or [])
        self.scroll.hide()
        self.actions.hide()
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {RED}; font-size: 10pt;")
        self.status_label.show()
        if not self.isVisible():
            self.fade_in()
        self._apply_geometry()

    def _set_error_actions(self, actions: list[tuple[str, Callable[[], None]]]) -> None:
        for index, button in enumerate(self._error_buttons):
            try:
                button.clicked.disconnect()
            except TypeError:
                pass  # no previous connection
            if index < len(actions):
                label, callback = actions[index]
                button.setText(label)
                button.clicked.connect(callback)
                button.show()
            else:
                button.hide()
        self.error_actions.setVisible(bool(actions))

    def _clear_result(self) -> None:
        self._set_error_actions([])
        self.status_label.setStyleSheet(f"color: {SUBTEXT}; font-size: 10pt;")
        self.summary.clear()
        self.diagnostic.clear()
        self.meta.clear()
        self.actions.set_blocks([])

    def toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self.body.setVisible(not self._collapsed)
        self.adjustSize()
        if not self._collapsed:
            self._apply_geometry()

    # -- visibility -------------------------------------------------------------

    def fade_in(self) -> None:
        self._fading_out = False
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
            if not self.capture_excluded:
                self.capture_excluded = exclude_from_capture(self)
                logger.info("HUD excluded from screen capture: %s", self.capture_excluded)
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    def fade_out(self) -> None:
        if not self.isVisible():
            return
        self._fading_out = True
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        if self._fading_out:
            self.hide()
            self._fading_out = False

    def dismiss(self) -> None:
        self.fade_out()
        self.dismissed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(event)
