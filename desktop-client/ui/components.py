"""Reusable HUD widgets: status pill, latency badge, spinner, code viewer, copy buttons.

All widgets are created and updated on the GUI thread only.
"""

from __future__ import annotations

import re
from typing import Final

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QPainter,
    QPen,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Catppuccin Mocha
BASE: Final[str] = "#1E1E2E"
MANTLE: Final[str] = "#181825"
CRUST: Final[str] = "#11111B"
SURFACE0: Final[str] = "#313244"
SURFACE1: Final[str] = "#45475A"
OVERLAY0: Final[str] = "#6C7086"
SUBTEXT: Final[str] = "#A6ADC8"
TEXT: Final[str] = "#CDD6F4"
BLUE: Final[str] = "#89B4FA"
GREEN: Final[str] = "#A6E3A1"
RED: Final[str] = "#F38BA8"
YELLOW: Final[str] = "#F9E2AF"
PEACH: Final[str] = "#FAB387"
MAUVE: Final[str] = "#CBA6F7"
TEAL: Final[str] = "#94E2D5"

MONO_CANDIDATES: Final[tuple[str, ...]] = ("JetBrains Mono", "Cascadia Code", "Consolas", "Courier New")
SHELL_LANGUAGES: Final[frozenset[str]] = frozenset({"bash", "sh", "powershell", "cmd", "bat", "batch", "console", "shell", "zsh"})


def monospace_font(point_size: float = 10.0) -> QFont:
    families = set(QFontDatabase.families())
    for candidate in MONO_CANDIDATES:
        if candidate in families:
            font = QFont(candidate)
            break
    else:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSizeF(point_size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


# ---------------------------------------------------------------------------
# Status pill
# ---------------------------------------------------------------------------


class StatusPill(QWidget):
    """Rounded ``● LABEL`` pill whose color animates between states."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = "IDLE"
        self._color = QColor(OVERLAY0)
        self._pulse = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(260)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_color)
        self._pulser = QVariantAnimation(self)
        self._pulser.setStartValue(0.35)
        self._pulser.setEndValue(1.0)
        self._pulser.setDuration(700)
        self._pulser.setLoopCount(-1)
        self._pulser.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulser.valueChanged.connect(self._on_pulse)
        font = self.font()
        font.setPointSizeF(8.5)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        self.setFont(font)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _on_color(self, value: QColor) -> None:
        self._color = value
        self.update()

    def _on_pulse(self, value: float) -> None:
        self._pulse = float(value)
        self.update()

    @property
    def text(self) -> str:
        return self._text

    def set_state(self, text: str, color: str, pulsing: bool = False) -> None:
        self._text = text.upper()
        self._animation.stop()
        self._animation.setStartValue(QColor(self._color))
        self._animation.setEndValue(QColor(color))
        self._animation.start()
        if pulsing:
            self._pulser.start()
        else:
            self._pulser.stop()
            self._pulse = 1.0
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = self.fontMetrics().horizontalAdvance(f"●  {self._text}") + 22
        return QSize(width, 22)

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        background = QColor(self._color)
        background.setAlpha(46)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        dot = QColor(self._color)
        dot.setAlphaF(self._pulse)
        painter.setBrush(dot)
        painter.drawEllipse(QRectF(10, rect.center().y() - 3.5, 7, 7))
        painter.setPen(QColor(self._color))
        painter.drawText(rect.adjusted(22, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        painter.end()


# ---------------------------------------------------------------------------
# Latency badge
# ---------------------------------------------------------------------------


class LatencyBadge(QLabel):
    """``⚡ 840ms · kaggle`` badge; hidden until a latency is known."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"QLabel {{ color: {YELLOW}; background: rgba(249,226,175,0.10); border-radius: 9px; "
            f"padding: 2px 8px; font-size: 8.5pt; font-weight: 600; }}"
        )
        self.hide()

    def set_latency(self, milliseconds: float | None, tier: str | None = None) -> None:
        if milliseconds is None:
            self.hide()
            return
        value = f"{milliseconds / 1000:.1f}s" if milliseconds >= 1000 else f"{milliseconds:.0f}ms"
        self.setText(f"\u26a1 {value}" + (f"  \u00b7  {tier}" if tier else ""))
        self.setToolTip("Hotkey to answer: capture + encode + network (server generation included)")
        self.show()

    def set_tier(self, tier: str) -> None:
        self.setText(f"\u21c4 {tier}")
        self.setToolTip("Endpoint currently being tried")
        self.show()


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------


class Spinner(QWidget):
    """Rotating arc driven by a ``QVariantAnimation``."""

    def __init__(self, diameter: int = 22, color: str = BLUE, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0.0
        self._color = QColor(color)
        self.setFixedSize(diameter, diameter)
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(360.0)
        self._animation.setDuration(900)
        self._animation.setLoopCount(-1)
        self._animation.valueChanged.connect(self._on_angle)

    def _on_angle(self, value: float) -> None:
        self._angle = float(value)
        self.update()

    def start(self) -> None:
        self.show()
        if self._animation.state() != QVariantAnimation.State.Running:
            self._animation.start()

    def stop(self) -> None:
        self._animation.stop()
        self.hide()

    @property
    def running(self) -> bool:
        return self._animation.state() == QVariantAnimation.State.Running

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(SURFACE1), 2.4)
        painter.setPen(pen)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.drawEllipse(rect)
        pen.setColor(self._color)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(-self._angle * 16), 100 * 16)
        painter.end()


# ---------------------------------------------------------------------------
# Syntax highlighting
# ---------------------------------------------------------------------------


def _fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Weight.DemiBold)
    fmt.setFontItalic(italic)
    return fmt


_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "python": ("and as assert async await break class continue def del elif else except False finally for "
               "from global if import in is lambda None nonlocal not or pass raise return True try while with yield").split(),
    "javascript": ("async await break case catch class const continue default delete do else export extends false "
                   "finally for from function if import in instanceof let new null of return super switch this throw "
                   "true try typeof undefined var void while yield").split(),
    "typescript": ("abstract any as async await boolean break case catch class const continue declare default else "
                   "enum export extends false finally for from function if implements import in interface keyof let "
                   "never new null number private protected public readonly return string super switch this throw true "
                   "try type typeof undefined unknown var void while").split(),
    "rust": ("as async await break const continue crate dyn else enum extern false fn for if impl in let loop match "
             "mod move mut pub ref return self Self static struct super trait true type unsafe use where while").split(),
    "bash": "if then else elif fi for while do done case esac function in export local return sudo echo cd".split(),
    "powershell": "if else elseif foreach for while do function param return try catch finally throw".split(),
    "json": ("true", "false", "null"),
}
_COMMENT: Final[dict[str, str]] = {
    "python": r"#[^\n]*", "bash": r"#[^\n]*", "powershell": r"#[^\n]*",
    "javascript": r"//[^\n]*", "typescript": r"//[^\n]*", "rust": r"//[^\n]*",
}
_ALIASES: Final[dict[str, str]] = {"sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash", "js": "javascript",
                                     "ts": "typescript", "tsx": "typescript", "jsx": "javascript", "ps1": "powershell"}


class CodeHighlighter(QSyntaxHighlighter):
    """Small regex highlighter (no Pygments dependency) for the languages OmniSight sees most."""

    def __init__(self, document: QTextDocument, language: str) -> None:
        super().__init__(document)
        self.language = _ALIASES.get(language, language)
        self._rules: list[tuple[re.Pattern[str], QTextCharFormat]] = []
        keywords = _KEYWORDS.get(self.language, ())
        if keywords:
            self._rules.append((re.compile(r"\b(" + "|".join(map(re.escape, keywords)) + r")\b"), _fmt(MAUVE, bold=True)))
        self._rules.append((re.compile(r"\b\d+(\.\d+)?\b"), _fmt(PEACH)))
        self._rules.append((re.compile(r"\b([A-Za-z_]\w*)(?=\s*\()"), _fmt(BLUE)))
        if self.language == "python":
            self._rules.append((re.compile(r"@\w+"), _fmt(YELLOW)))
        if self.language in ("bash", "powershell"):
            self._rules.append((re.compile(r"\$\{?\w+\}?"), _fmt(TEAL)))
            self._rules.append((re.compile(r"(?<=\s)--?[\w-]+"), _fmt(SUBTEXT)))
        if self.language == "json":
            self._rules.append((re.compile(r'"[^"\\]*(\\.[^"\\]*)*"(?=\s*:)'), _fmt(BLUE)))
        if self.language == "rust":
            self._rules.append((re.compile(r"'[a-z_]\w*\b(?!')"), _fmt(PEACH, italic=True)))
        self._rules.append((re.compile(r'"[^"\\\n]*(\\.[^"\\\n]*)*"|\'[^\'\\\n]*(\\.[^\'\\\n]*)*\'|`[^`]*`'), _fmt(GREEN)))
        comment = _COMMENT.get(self.language)
        if comment:
            self._rules.append((re.compile(comment), _fmt(OVERLAY0, italic=True)))

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        for pattern, fmt in self._rules:
            for match in pattern.finditer(text):
                start, end = match.span()
                self.setFormat(start, end - start, fmt)


# ---------------------------------------------------------------------------
# Copy button + toast
# ---------------------------------------------------------------------------


class CopyButton(QPushButton):
    """Copies ``text`` to the clipboard and flashes ``Copied!`` for 1.2 s."""

    copied = pyqtSignal(str)

    def __init__(self, label: str, text: str = "", accent: str = BLUE, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self._label = label
        self._payload = text
        self._accent = accent
        self._restore = QTimer(self)
        self._restore.setSingleShot(True)
        self._restore.timeout.connect(self._reset_label)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._copy)
        self._apply_style(accent)
        self.set_payload(text)

    def _apply_style(self, color: str) -> None:
        self.setStyleSheet(
            f"QPushButton {{ color: {color}; background: {SURFACE0}; border: 1px solid {SURFACE1}; "
            f"border-radius: 7px; padding: 5px 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {SURFACE1}; }}"
            f"QPushButton:disabled {{ color: {OVERLAY0}; background: {MANTLE}; border-color: {SURFACE0}; }}"
        )

    @property
    def payload(self) -> str:
        return self._payload

    def set_payload(self, text: str) -> None:
        self._payload = text
        self.setEnabled(bool(text.strip()))

    def _copy(self) -> None:
        if not self._payload:
            return
        QGuiApplication.clipboard().setText(self._payload)
        self.setText("Copied!")
        self._apply_style(GREEN)
        self._restore.start(1200)
        self.copied.emit(self._payload)

    def _reset_label(self) -> None:
        self.setText(self._label)
        self._apply_style(self._accent)


class Toast(QLabel):
    """Small fading message pinned to the bottom of its parent."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"QLabel {{ color: {CRUST}; background: {GREEN}; border-radius: 8px; padding: 4px 12px; font-weight: 700; }}"
        )
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(900)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.Type.InQuad)
        self._fade.finished.connect(self.hide)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._fade.start)
        self.hide()

    def show_message(self, text: str) -> None:
        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 18)
        self._fade.stop()
        self._effect.setOpacity(1.0)
        self.show()
        self.raise_()
        self._hold.start(700)


# ---------------------------------------------------------------------------
# Code block
# ---------------------------------------------------------------------------


class CodeBlockWidget(QFrame):
    """Read-only, syntax-highlighted code block with a language tag and a copy button."""

    MAX_VISIBLE_LINES: Final[int] = 18

    copied = pyqtSignal(str)

    def __init__(self, code: str, language: str = "text", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.code = code
        self.language = language
        self.setObjectName("codeBlock")
        self.setStyleSheet(
            f"QFrame#codeBlock {{ background: {CRUST}; border: 1px solid {SURFACE0}; border-radius: 9px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 6, 6, 4)
        tag = QLabel(language, header)
        tag.setStyleSheet(f"color: {SUBTEXT}; font-size: 8pt; font-weight: 600;")
        header_layout.addWidget(tag)
        header_layout.addStretch(1)
        self.copy_button = CopyButton("Copy", code, BLUE, header)
        self.copy_button.setStyleSheet(
            self.copy_button.styleSheet().replace("padding: 5px 12px", "padding: 2px 9px")
        )
        self.copy_button.copied.connect(self.copied)
        header_layout.addWidget(self.copy_button)
        layout.addWidget(header)

        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        self.editor.setPlainText(code)
        self.editor.setFont(monospace_font(9.5))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setFrameShape(QFrame.Shape.NoFrame)
        self.editor.setStyleSheet(
            f"QPlainTextEdit {{ background: transparent; color: {TEXT}; padding: 2px 10px 8px 12px; "
            f"selection-background-color: {SURFACE1}; }}"
        )
        self.highlighter = CodeHighlighter(self.editor.document(), language)
        lines = max(1, min(self.MAX_VISIBLE_LINES, code.count("\n") + 1))
        line_height = self.editor.fontMetrics().lineSpacing()
        scrollbar_room = 14 if max((len(l) for l in code.splitlines()), default=0) > 70 else 0
        self.editor.setFixedHeight(lines * line_height + 22 + scrollbar_room)
        if code.count("\n") + 1 <= self.MAX_VISIBLE_LINES:
            self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.editor)
