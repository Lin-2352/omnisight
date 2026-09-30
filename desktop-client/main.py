"""OmniSight desktop client: bootstrap, tray icon, global hotkeys, and orchestration.

Threads:
    * GUI thread: the Qt event loop only (HUD, tray, dialogs).
    * Capture pool (one QThreadPool thread): mss grab + JPEG encode, and the
      audio stop/trim/encode after push-to-talk.
    * InferenceWorker (QThread): HTTP with retries and failover.
    * pynput hook thread: global keyboard hook; it only emits Qt signals.

Hotkeys (captured system-wide and not passed on to the focused app):
    Alt+C  analyze the active screen
    Alt+V  hold to record a voice question, release to send it with the screen
    Esc    hide the HUD (passed through to the focused app as well)

Run:  python desktop-client/main.py [--override-url URL] [--log-level DEBUG]
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any, Final

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# DPI awareness must be set before Qt or any capture code touches the display.
from capture.screen import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()

from capture.audio import (  # noqa: E402
    MIC_SETTINGS_URI,
    PERMISSION_MESSAGES,
    AudioDeviceError,
    AudioRecorder,
    MicrophonePermissionError,
    NoSpeechError,
    RecordingResult,
    microphone_permission,
)
from capture.screen import BlackFrameError, CaptureResult, ScreenCapturer  # noqa: E402
from core.capability import describe, probe  # noqa: E402
from core.actions import ActionRunner, is_shell_language  # noqa: E402
from core.config import (  # noqa: E402
    BACKENDS,
    ENGINE_CHOICES,
    LOCAL_DEVICES,
    ClientSettings,
    EndpointResolver,
)
from core.foreground import POLL_INTERVAL_MS, ForegroundTracker  # noqa: E402
from core.logger import configure_logging, default_log_dir, get_logger  # noqa: E402
from core.memory import MODE_PHRASES, ConversationMemory  # noqa: E402
from core.node_supervisor import NodeError, NodeState, NodeSupervisor, repo_root  # noqa: E402
from core.search import SearchOutcome, WebSearch, smart_query  # noqa: E402
from core.state import AppState, StateMachine  # noqa: E402
from core.tts import Speaker  # noqa: E402
from core.watch import (  # noqa: E402
    CHECK_PROMPT,
    DEFAULT_INTERVAL_S,
    DESCRIBE_PROMPT,
    DESCRIBE_TOKENS,
    GENERIC_FINDING,
    FindingTracker,
    alert_text,
    WatchScheduler,
    frame_changed,
    frame_is_flat,
    frame_signature,
    parse_watch_reply,
    parse_yes_no,
)
from network.client import HealthCheckWorker, InferenceClient, InferenceWorker, _is_loopback, build_request  # noqa: E402
from network.schemas import (  # noqa: E402
    CONTRACT_VERSION,
    AnalysisMode,
    ClientResult,
    LatencyMetrics,
)
from PyQt6.QtCore import (  # noqa: E402
    QObject,
    QRunnable,
    QSettings,
    QThread,
    QThreadPool,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (  # noqa: E402
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)
from ui.components import BASE, BLUE, GREEN, SURFACE0, TEXT  # noqa: E402
from ui.hud import HudWindow  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.run_dialog import RunDialog  # noqa: E402

logger = get_logger("main")

MUTEX_NAME: Final[str] = "Global\\OmniSight_Client_SingleInstance_Mutex"
LOCAL_MUTEX_NAME: Final[str] = "Local\\OmniSight_Client_SingleInstance_Mutex"
LOCK_PORT: Final[int] = 47_831
ERROR_ALREADY_EXISTS: Final[int] = 183
ERROR_ACCESS_DENIED: Final[int] = 5
VK_C: Final[int] = 0x43
VK_V: Final[int] = 0x56
VK_ESCAPE: Final[int] = 0x1B
VK_ALTS: Final[frozenset[int]] = frozenset({0x12, 0xA4, 0xA5})
VK_MASK: Final[int] = 0xE8  # unassigned; injected so a suppressed Alt combo does not open app menus
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------


class SingleInstanceLock:
    """Named Win32 mutex (Global\\ then Local\\), or a localhost socket without ctypes."""

    def __init__(self) -> None:
        self._handle: int | None = None
        self._socket: socket.socket | None = None
        self.mechanism = "none"

    def acquire(self) -> bool:
        if os.name == "nt":
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CreateMutexW.restype = ctypes.c_void_p
                for name in (MUTEX_NAME, LOCAL_MUTEX_NAME):
                    handle = kernel32.CreateMutexW(None, False, name)
                    error = ctypes.get_last_error()
                    if handle and error == ERROR_ALREADY_EXISTS:
                        kernel32.CloseHandle(ctypes.c_void_p(handle))
                        return False
                    if handle:
                        self._handle = handle
                        self.mechanism = name
                        return True
                    if error != ERROR_ACCESS_DENIED:
                        break
            except (AttributeError, OSError) as exc:
                logger.warning("mutex unavailable (%s); using socket lock", exc)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFB), 1)
            sock.bind(("127.0.0.1", LOCK_PORT))
            sock.listen(1)
        except OSError:
            return False
        self._socket = sock
        self.mechanism = f"socket 127.0.0.1:{LOCK_PORT}"
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            except (AttributeError, OSError):
                pass
            self._handle = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None


def notify_already_running() -> None:
    message = "OmniSight is already running.\n\nUse its tray icon, or press Alt+C to analyze the screen."
    print(message.replace("\n\n", " "), file=sys.stderr)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "OmniSight", 0x40 | 0x10000)  # MB_ICONINFORMATION | MB_SETFOREGROUND
        except (AttributeError, OSError):
            pass


# ---------------------------------------------------------------------------
# Global hotkeys
# ---------------------------------------------------------------------------


class HotkeyBridge(QObject):
    """Global keyboard hook on pynput's thread; every event becomes a Qt signal."""

    capture_requested = pyqtSignal()
    voice_pressed = pyqtSignal()
    voice_released = pyqtSignal()
    dismiss_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._listener: Any = None
        self._alt_down = False
        self._suppressed: set[int] = set()
        self._voice_active = False

    def start(self) -> None:
        from pynput import keyboard

        if os.name == "nt":
            self._listener = keyboard.Listener(win32_event_filter=self._win32_filter)
        else:
            self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()
        logger.info("global hotkeys active: Alt+C, hold Alt+V, Esc")

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    @staticmethod
    def _inject_mask_key() -> None:
        """Tap an unassigned key so the target app does not treat Alt as a lone menu tap."""
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(VK_MASK, 0, 0, 0)
            user32.keybd_event(VK_MASK, 0, 0x0002, 0)  # KEYEVENTF_KEYUP
        except (AttributeError, OSError):
            pass

    def _win32_filter(self, msg: int, data: Any) -> bool:
        """Low-level hook filter (hook thread). Suppresses our combos; returns False (no callbacks)."""
        vk = data.vkCode
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        up = msg in (WM_KEYUP, WM_SYSKEYUP)
        if vk in VK_ALTS:
            self._alt_down = down
            if up and self._voice_active:
                self._voice_active = False
                self.voice_released.emit()
            return False
        if vk == VK_ESCAPE and down:
            self.dismiss_requested.emit()
            return False
        if vk in (VK_C, VK_V):
            if down and self._alt_down:
                first = vk not in self._suppressed
                self._suppressed.add(vk)
                if first:
                    threading.Timer(0.0, self._inject_mask_key).start()
                    if vk == VK_C:
                        self.capture_requested.emit()
                    elif not self._voice_active:
                        self._voice_active = True
                        self.voice_pressed.emit()
                self._listener.suppress_event()
            if up and vk in self._suppressed:
                self._suppressed.discard(vk)
                if vk == VK_V and self._voice_active:
                    self._voice_active = False
                    self.voice_released.emit()
                self._listener.suppress_event()
        return False

    # Non-Windows fallback (no suppression).
    def _on_press(self, key: Any) -> None:
        from pynput import keyboard

        if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
            self._alt_down = True
        elif key == keyboard.Key.esc:
            self.dismiss_requested.emit()
        elif self._alt_down and getattr(key, "char", None) in ("c", "v"):
            if key.char == "c":
                self.capture_requested.emit()
            elif not self._voice_active:
                self._voice_active = True
                self.voice_pressed.emit()

    def _on_release(self, key: Any) -> None:
        from pynput import keyboard

        is_alt = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr)
        if is_alt:
            self._alt_down = False
        if self._voice_active and (is_alt or getattr(key, "char", None) == "v"):
            self._voice_active = False
            self.voice_released.emit()


# ---------------------------------------------------------------------------
# Background capture task
# ---------------------------------------------------------------------------


class CaptureSignals(QObject):
    finished = pyqtSignal(object, object)  # CaptureResult, RecordingResult | None
    failed = pyqtSignal(str)


class CaptureTask(QRunnable):
    """Screen grab + encode (and, for voice, stop/trim/encode the recording) off the GUI thread."""

    def __init__(
        self,
        capturer: ScreenCapturer,
        recorder: AudioRecorder | None = None,
        point: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.capturer = capturer
        self.recorder = recorder
        self.point = point  # the user's window, so OmniSight's own window never picks the monitor
        self.signals = CaptureSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        recording: RecordingResult | None = None
        try:
            if self.recorder is not None:
                recording = self.recorder.stop_recording()
            capture = self.capturer.capture(point=self.point)
        except NoSpeechError as exc:
            self.signals.failed.emit(f"No speech detected - speak while recording (hold Alt+V, or press Speak and then Stop and send) ({exc}).")
            return
        except AudioDeviceError as exc:
            self.signals.failed.emit(str(exc))
            return
        except BlackFrameError as exc:
            logger.warning("capture aborted: %s", exc)
            self.signals.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - report every capture failure to the HUD
            logger.exception("capture failed")
            self.signals.failed.emit(f"Screen capture failed: {type(exc).__name__}: {exc}")
            return
        self.signals.finished.emit(capture, recording)


# ---------------------------------------------------------------------------
# Tray icon + settings
# ---------------------------------------------------------------------------


def make_tray_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor(0, 0, 0, 0))
    painter.setBrush(QColor(BASE))
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    pen = QPen(QColor(BLUE), 5)
    painter.setPen(pen)
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.drawEllipse(14, 14, 36, 36)
    painter.setPen(QColor(0, 0, 0, 0))
    painter.setBrush(QColor(GREEN))
    painter.drawEllipse(26, 26, 12, 12)
    painter.end()
    return QIcon(pixmap)


class SettingsDialog(QDialog):
    def __init__(self, controller: OmniSightController) -> None:
        super().__init__(None)
        self.controller = controller
        self.setWindowTitle("OmniSight settings")
        self.setMinimumWidth(520)
        self.setStyleSheet(
            f"QDialog {{ background: {BASE}; }} QLabel {{ color: {TEXT}; }}"
            f"QLineEdit, QComboBox {{ background: {SURFACE0}; color: {TEXT}; border-radius: 6px; padding: 5px; }}"
            f"QPushButton {{ background: {SURFACE0}; color: {TEXT}; border-radius: 6px; padding: 5px 12px; }}"
        )
        layout = QVBoxLayout(self)
        form = QFormLayout()
        resolution = controller.resolver.resolve_active_endpoint()
        self.endpoint_label = QLabel(f"{resolution.url or 'none'}  ({resolution.source}; {resolution.detail})")
        self.endpoint_label.setWordWrap(True)
        form.addRow("Active endpoint:", self.endpoint_label)
        self.override = QLineEdit(controller.settings.manual_override_url or "")
        self.override.setPlaceholderText("https://<name>.trycloudflare.com (empty = use the gist)")
        form.addRow("Override URL:", self.override)
        self.backend = QComboBox()
        for key, (label, _backend, _device) in ENGINE_CHOICES.items():
            self.backend.addItem(label, key)
        self.backend.setCurrentIndex(list(ENGINE_CHOICES).index(controller.settings.engine_choice))
        form.addRow("Engine:", self.backend)
        self.local_url = QLineEdit(controller.settings.local_dev_url)
        self.local_url.setPlaceholderText("http://127.0.0.1:8000 (scripts\\run-local-gpu.ps1, GPU or -Device cpu)")
        form.addRow("Local node:", self.local_url)
        this_pc = QLabel(controller.capability_line or "checking this PC's CPU, RAM and GPU...")
        this_pc.setWordWrap(True)
        form.addRow("This PC:", this_pc)
        form.addRow("Fallback URL:", QLabel(controller.settings.fallback_api_url or "not set (FALLBACK_API_URL)"))
        form.addRow("Hotkeys:", QLabel("Alt+C analyze   ·   hold Alt+V voice   ·   Esc hide"))
        layout.addLayout(form)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        buttons = QHBoxLayout()
        for text, slot in (
            ("Apply", self.apply),
            ("Test connection", self.test_connection),
            ("Open logs", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(default_log_dir())))),
            ("Close", self.accept),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self._health: HealthCheckWorker | None = None

    def apply(self) -> None:
        try:
            self.controller.set_override(self.override.text())
            self.controller.set_local_url(self.local_url.text())
            self.controller.set_engine(str(self.backend.currentData()))
        except ValueError as exc:
            self.result_label.setText(str(exc))
            return
        resolution = self.controller.resolver.resolve_active_endpoint(force_refresh=True)
        self.endpoint_label.setText(f"{resolution.url or 'none'}  ({resolution.source}; {resolution.detail})")
        self.result_label.setText("Saved for this session.")

    def test_connection(self) -> None:
        self.result_label.setText("Checking…")
        self._health = HealthCheckWorker(self.controller.settings, self.controller.resolver)
        self._health.finished_with.connect(self.result_label.setText)
        self._health.start()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SearchWorker(QThread):
    """Runs one web search (and the optional smart-query rewrite) off the GUI thread."""

    done = pyqtSignal(int, object)  # token, SearchOutcome

    def __init__(self, token: int, search: WebSearch, text: str, ask: Callable[[str], str] | None) -> None:
        super().__init__()
        self._token = token
        self._search = search
        self._text = text
        self._ask = ask
        self.setObjectName("omnisight-search")

    def run(self) -> None:
        text = self._text
        try:
            if self._ask is not None:
                text = smart_query(self._ask, text) or text
            outcome = self._search.search(text)
        except Exception as exc:  # noqa: BLE001 - a search bug must never stop the answer
            logger.exception("web search crashed")
            outcome = SearchOutcome(query="", notice=f"Web search failed ({type(exc).__name__}). Answering without it.")
        self.done.emit(self._token, outcome)


class OmniSightController(QObject):
    def __init__(self, app: QApplication, settings: ClientSettings, enable_hotkeys: bool = True) -> None:
        super().__init__()
        self.app = app
        self.settings = settings
        self.resolver = EndpointResolver(settings)
        self.state = StateMachine(parent=self)
        self.capturer = ScreenCapturer()
        self.recorder = AudioRecorder()
        self.hud = HudWindow()
        self.window = MainWindow()
        self.foreground = ForegroundTracker()
        self.node = NodeSupervisor(repo_root(), settings.local_dev_url)
        saved = QSettings()
        self.memory = ConversationMemory(enabled=saved.value("memory_enabled", True, type=bool))
        self.speaker = Speaker()
        self.web_search = WebSearch()
        self.actions = ActionRunner()
        self._actions_enabled = bool(saved.value("actions_enabled", False, type=bool))
        self._actions_cwd = Path(str(saved.value("actions_cwd", str(Path.home()))))
        self._action_dialog_open = False  # while it is open (and its output on screen) watch must not look at the screen
        self._search_enabled = bool(saved.value("web_search", False, type=bool))
        self._smart_enabled = bool(saved.value("smart_query", False, type=bool)) and self._search_enabled
        self.watch = WatchScheduler(_watch_interval())
        self._watch_tracker = FindingTracker()
        self._watch_signature: bytes | None = None  # the last frame the model looked at
        self._watch_pending_signature: bytes | None = None
        self._watch_image: Any = None  # the frame being looked at; dropped when the tick ends
        self._watch_stage = "check"
        self._watch_worker: InferenceWorker | None = None
        self._watch_notice_pending = False
        self._search_token = 0
        self._chat_sent = -1  # the search token whose chat request has already gone out
        self._search_pending = False
        self._search_worker: SearchWorker | None = None
        self._search_outcome: SearchOutcome | None = None
        self._web_search_flag = False
        self._held_capture: tuple[CaptureResult, RecordingResult | None] | None = None
        self._speak_enabled = bool(saved.value("speak_enabled", False, type=bool)) and self.speaker.available
        self._last_node_status: Any = None
        self._origin = "hud"  # who asked: "hud" (hotkeys) or "window"
        self._pending_prompt = ""
        self._pending_question = ""
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)  # one capture thread keeps its warm mss instance
        self.pool.setExpiryTimeout(-1)  # never retire it (Qt's default is 30 s idle)
        self._worker: InferenceWorker | None = None
        self._retired_workers: list[InferenceWorker] = []
        self._pending_mode = AnalysisMode.DEBUG
        self._settings_dialog: SettingsDialog | None = None
        self._error_actions: list[tuple[str, Any]] = []
        self._mic_notice_pending = False

        self.state.state_changed.connect(self._on_state_changed)
        self.hud.dismissed.connect(self.state.reset)
        self.window.ask_requested.connect(self.on_window_ask)
        self.window.capture_requested.connect(self.on_window_capture)
        self.window.mic_clicked.connect(self.on_window_mic)
        self.window.engine_selected.connect(self.set_engine)
        self.window.node_toggle_clicked.connect(self.toggle_node)
        self.window.clear_requested.connect(self.clear_history)
        self.window.settings_requested.connect(self.open_settings)
        self.window.memory_toggled.connect(self.set_memory_enabled)
        self.window.speak_toggled.connect(self.set_speak_enabled)
        self.window.stop_speaking_clicked.connect(self.stop_speaking)
        self.window.search_toggled.connect(self.set_search_enabled)
        self.window.watch_toggled.connect(self.set_watch_enabled)
        self.window.actions_toggled.connect(self.set_actions_enabled)
        self.window.run_requested.connect(self.on_run_requested)
        self.window.watch_pause_clicked.connect(self.toggle_watch_pause)
        self.window.smart_toggled.connect(self.set_smart_enabled)
        self.window.set_engine(self.settings.engine_choice)
        self.window.set_memory_enabled(self.memory.enabled)
        self.window.set_speak_available(self.speaker.available)
        self.window.set_speak_enabled(self._speak_enabled)
        self.window.set_search_enabled(self._search_enabled)
        self.window.set_actions_enabled(self._actions_enabled)
        self.window.set_smart_enabled(self._smart_enabled)
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start(1000)
        self._speaking_timer = QTimer(self)
        self._speaking_timer.timeout.connect(lambda: self.window.set_speaking(self.speaker.speaking))
        self._speaking_timer.start(500)

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("OmniSight - Alt+C analyze, hold Alt+V to ask")
        menu = QMenu()
        for label, slot in (
            ("Open OmniSight", self.open_window),
            ("Show HUD", self.show_hud),
            ("Clear History", self.clear_history),
            ("Settings", self.open_settings),
        ):
            action = QAction(label, menu)
            action.triggered.connect(slot)
            menu.addAction(action)
        self._watch_action = QAction("Pause watching", menu)
        self._watch_action.setEnabled(False)
        self._watch_action.triggered.connect(self.toggle_watch_pause)
        menu.addAction(self._watch_action)
        backend_menu = menu.addMenu("Backend")
        self._backend_group = QActionGroup(backend_menu)
        self._backend_group.setExclusive(True)
        self._backend_actions: dict[str, QAction] = {}
        for key, label in BACKENDS.items():
            action = QAction(label, backend_menu)
            action.setCheckable(True)
            action.setChecked(key == self.settings.backend)
            action.triggered.connect(lambda _checked=False, choice=key: self.set_backend(choice))
            self._backend_group.addAction(action)
            backend_menu.addAction(action)
            self._backend_actions[key] = action
        menu.addSeparator()
        exit_action = QAction("Exit", menu)
        exit_action.triggered.connect(self.app.quit)
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self._menu = menu
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        permission = microphone_permission()
        if permission.startswith("denied"):
            logger.warning("microphone blocked by Windows privacy settings (%s)", permission)
            self._mic_notice_pending = True
            self.tray.showMessage(
                "OmniSight",
                "Microphone access is off, so Alt+V can't record. Click here to open the microphone settings.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )

        self.hotkeys = HotkeyBridge()
        self.hotkeys.capture_requested.connect(self.on_capture_requested)
        self.hotkeys.voice_pressed.connect(self.on_voice_pressed)
        self.hotkeys.voice_released.connect(self.on_voice_released)
        self.hotkeys.dismiss_requested.connect(self.on_dismiss)
        if enable_hotkeys:
            self.hotkeys.start()

        self.capability_line = ""
        self.pool.start(_WarmUp(self.capturer, self))
        # The user's own window is remembered so clicking OmniSight never changes which monitor is captured.
        self._foreground_timer = QTimer(self)
        self._foreground_timer.timeout.connect(self.foreground.poll)
        self._foreground_timer.start(POLL_INTERVAL_MS)
        self._node_timer = QTimer(self)
        self._node_timer.timeout.connect(self._refresh_node)
        self._node_timer.start(1000)
        self._refresh_node()
        logger.info("OmniSight client %s ready", CONTRACT_VERSION)

    # -- tray -----------------------------------------------------------------

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_window()

    def open_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def show_hud(self) -> None:
        latest = self.state.latest()
        if latest is not None and not self.state.is_busy:
            self.hud.show_result(ClientResult(response=latest.response, metrics=latest.metrics))
        elif not self.state.is_busy:
            self.hud.set_idle()
        self.hud.place_on_screen(None)
        self.hud.fade_in()

    def set_memory_enabled(self, on: bool) -> None:
        """Remember the conversation (text only, on this PC) so follow-up questions make sense."""
        self.memory.set_enabled(on)
        QSettings().setValue("memory_enabled", on)
        logger.info("conversation memory %s", "on" if on else "off (and cleared)")

    def set_search_enabled(self, on: bool) -> None:
        """Search the web for typed questions (Stack Overflow + Wikipedia, free and keyless); off by default."""
        self._search_enabled = on
        if not on:
            self._smart_enabled = False
            QSettings().setValue("smart_query", False)
            self.window.set_smart_enabled(False)
        QSettings().setValue("web_search", on)
        logger.info("web search %s", "on" if on else "off")

    def set_smart_enabled(self, on: bool) -> None:
        self._smart_enabled = on and self._search_enabled
        QSettings().setValue("smart_query", self._smart_enabled)

    def set_speak_enabled(self, on: bool) -> None:
        self._speak_enabled = on and self.speaker.available
        QSettings().setValue("speak_enabled", self._speak_enabled)
        if not self._speak_enabled:
            self.stop_speaking()

    def stop_speaking(self) -> None:
        self.speaker.stop()
        self.window.set_speaking(False)

    def clear_history(self) -> None:
        self.memory.clear()
        self.state.clear_history()
        self.window.clear_exchanges()
        if not self.state.is_busy:
            self.state.reset()
            self.hud.show_state(AppState.IDLE)
            self.hud.set_idle()
        self.tray.showMessage("OmniSight", "History cleared.", QSystemTrayIcon.MessageIcon.Information, 2000)

    def open_settings(self) -> None:
        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.show()
        self._settings_dialog.raise_()

    def set_override(self, url: str) -> None:
        self.settings = self.settings.with_override(url)
        self.resolver.update_settings(self.settings)

    def set_backend(self, backend: str) -> None:
        """Switch between auto / kaggle / local and remember the choice across restarts."""
        self.settings = self.settings.with_backend(backend)
        self.resolver.update_settings(self.settings)
        QSettings().setValue("backend", self.settings.backend)
        action = self._backend_actions.get(self.settings.backend)
        if action is not None and not action.isChecked():
            action.setChecked(True)
        logger.info("backend set to %s", self.settings.backend)
        self._stop_watching_if_not_local()
        self.window.set_engine(self.settings.engine_choice)
        self._last_node_status = None
        self._refresh_node()
        self.tray.showMessage(
            "OmniSight", f"Backend: {BACKENDS[self.settings.backend]}", QSystemTrayIcon.MessageIcon.Information, 2500
        )

    def set_engine(self, key: str) -> None:
        """Pick where answers come from: Auto, Kaggle, or this PC's GPU / CPU (window or Settings)."""
        self.settings = self.settings.with_engine_choice(key)
        self.resolver.update_settings(self.settings)
        saved = QSettings()
        saved.setValue("backend", self.settings.backend)
        saved.setValue("local_device", self.settings.local_device)
        action = self._backend_actions.get(self.settings.backend)
        if action is not None and not action.isChecked():
            action.setChecked(True)
        self.window.set_engine(key)
        self._stop_watching_if_not_local()
        logger.info("engine set to %s (%s)", key, ENGINE_CHOICES[key][0])
        status = self.node.status
        if (
            key in ("local_gpu", "local_cpu")
            and status.owned
            and status.state in (NodeState.STARTING, NodeState.READY)
            and status.requested_device != self.settings.local_device
        ):
            self._start_node()  # the node this app started is on the other device: restart it
            return
        self._last_node_status = None
        self._refresh_node()

    # -- local node (started and stopped from the window) ------------------------------------------------

    def _start_node(self) -> None:
        try:
            self.node.set_url(self.settings.local_dev_url)
            self.node.start(self.settings.local_device)
        except NodeError as exc:
            self.window.show_notice(str(exc), error=True)
        self._last_node_status = None
        self._refresh_node()

    def toggle_node(self) -> None:
        status = self.node.status
        if status.owned and status.state in (NodeState.STARTING, NodeState.READY):
            self.node.stop()
            self._last_node_status = None
            self._refresh_node()
        else:
            self._start_node()

    def _refresh_node(self) -> None:
        status = self.node.refresh()
        if status != self._last_node_status:
            self._last_node_status = status
            self.window.set_node_status(status, self.settings.engine_choice)

    def set_local_url(self, url: str) -> None:
        self.settings = self.settings.with_local_url(url)
        self.resolver.update_settings(self.settings)
        self.node.set_url(self.settings.local_dev_url)
        QSettings().setValue("local_url", self.settings.local_dev_url)
        self._stop_watching_if_not_local()

    # -- hotkeys ----------------------------------------------------------------

    def on_capture_requested(self) -> None:
        if self.state.is_busy:
            logger.info("Alt+C ignored: %s", self.state.state.value)
            return
        self.stop_speaking()
        self._cancel_watch_request()
        self._origin, self._pending_prompt, self._pending_question = "hud", "", ""
        self._pending_mode = AnalysisMode.DEBUG
        self._start_search("")
        self.state.transition(AppState.CAPTURING, reason="Alt+C")
        self._start_capture(None)

    # -- window actions (the same pipeline, started with the mouse) ------------------------------------------

    def on_window_ask(self, text: str) -> None:
        if self.window.include_screen:
            self._begin_window_request(AnalysisMode.EXPLAIN, text)
        else:
            self._begin_chat(text)

    def _begin_chat(self, text: str) -> None:
        """A typed message with no screenshot: nothing is captured and no image leaves this PC."""
        if self.state.is_busy:
            return
        self.stop_speaking()
        self._cancel_watch_request()
        self._origin, self._pending_prompt, self._pending_question = "window", text, text
        self._pending_mode = AnalysisMode.CHAT
        self.state.transition(AppState.ANALYZING, reason="chat")
        self._start_search(text)
        self._dispatch()

    def _send_chat(self) -> None:
        results, flag = self._web_fields()
        try:
            request = build_request(
                None,
                mode=AnalysisMode.CHAT,
                prompt=self._pending_prompt,
                max_new_tokens=self.settings.max_new_tokens,
                history=self.memory.history(),
                web_results=results,
                web_search=flag,
            )
        except ValueError as exc:
            self.state.fail(f"Could not build the request: {exc}")
            return
        self._start_worker(request, LatencyMetrics())

    # -- watch mode (local engine only) -----------------------------------------------------------------

    def set_actions_enabled(self, on: bool) -> None:
        """The master switch for Run... buttons. Turning it on asks once, in plain words; it is remembered either way."""
        if on and not self._actions_enabled:
            answer = QMessageBox.question(
                self.window,
                "Allow running commands?",
                "OmniSight will show a Run... button on answers that contain a terminal command. Clicking it opens a dialog with "
                "the exact command; nothing runs until you type RUN, and never by itself.\n\n"
                "The command is written by an AI model that reads your screen, and text on a screen or web page can influence it. "
                "A few catastrophic commands are always refused, but this is not a sandbox: read every command.\n\n"
                "Turn it on?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.window.set_actions_enabled(False)
                return
        self._actions_enabled = on
        QSettings().setValue("actions_enabled", on)
        self.window.set_actions_enabled(on)
        logger.info("running commands %s", "allowed (each needs an approval)" if on else "off")

    def on_run_requested(self, language: str, command: str) -> None:
        """A card's Run... button: show the approval dialog (only when the switch is on and it is a terminal command)."""
        if not self._actions_enabled or not is_shell_language(language) or not command.strip():
            return
        dialog = RunDialog(command, language, self.actions, self._actions_cwd, parent=self.window)
        self._action_dialog_open = True
        try:
            dialog.exec()
        finally:
            self._action_dialog_open = False
        self._actions_cwd = dialog.cwd
        QSettings().setValue("actions_cwd", str(self._actions_cwd))

    def set_watch_enabled(self, on: bool) -> None:
        """Start or stop watching the screen. Always off at start-up; only ever on a local engine."""
        if not on:
            self._stop_watching()
            return
        if self.settings.backend != "local":
            self.window.set_watch_enabled(False)
            self.window.show_notice(
                "Watching needs a local engine, so your screen never leaves this PC. Pick Local GPU, Local CPU or Local auto first."
            )
            return
        if not _is_loopback(self.settings.local_dev_url):
            self.window.set_watch_enabled(False)
            self.window.show_notice(WATCH_NOT_LOOPBACK)
            return
        self._watch_signature = None
        self._watch_tracker.reset()
        self.watch.start()
        self._update_watch_ui()
        logger.info("watch mode on (every %.0f s, local engine only)", self.watch.interval_s)
        if self.settings.local_device == "cpu":
            self.window.show_notice(WATCH_ON_CPU)

    def toggle_watch_pause(self) -> None:
        if not self.watch.running:
            return
        if self.watch.paused:
            self.watch.resume()
        else:
            self.watch.pause()
        self._update_watch_ui()

    def _update_watch_ui(self) -> None:
        if not self.watch.running:
            self.window.set_watch_status("")
            self._watch_action.setEnabled(False)
            self._watch_action.setText("Pause watching")
            self.tray.setToolTip("OmniSight - Alt+C analyze, hold Alt+V to ask")
            return
        if self.watch.paused:
            text, tip = "Paused", "OmniSight - watching is PAUSED"
        else:
            text, tip = (
                f"Watching every {self.watch.interval_s:.0f} s - frames stay on this PC",
                "OmniSight - WATCHING your screen (local engine)",
            )
        self.window.set_watch_status(text, paused=self.watch.paused)
        self._watch_action.setEnabled(True)
        self._watch_action.setText("Resume watching" if self.watch.paused else "Pause watching")
        self.tray.setToolTip(tip)

    def _stop_watching(self, notice: str = "") -> None:
        self._cancel_watch_request()
        self.watch.stop()
        self._watch_signature = None
        self._watch_image = None
        self._watch_tracker.reset()
        self.window.set_watch_enabled(False)
        self._update_watch_ui()
        if notice:
            self.window.show_notice(notice)
            logger.info("watch mode stopped: %s", notice)

    def _stop_watching_if_not_local(self) -> None:
        if not self.watch.running:
            return
        if self.settings.backend != "local":
            self._stop_watching("Watching stopped: it only runs on a local engine, so your screen stays on this PC.")
        elif not _is_loopback(self.settings.local_dev_url):
            self._stop_watching(WATCH_NOT_LOOPBACK)

    def _cancel_watch_request(self) -> None:
        """A question from the user wins: drop a watch request that is still waiting for the model."""
        worker = self._watch_worker
        if worker is not None:
            worker.cancel()

    def _watch_tick(self) -> None:
        if not self.watch.running:
            return
        last = self.foreground.last_external
        busy = self.state.is_busy or self._worker is not None or self._action_dialog_open
        if not self.watch.due(busy=busy, window=last.hwnd if last is not None else None):
            return
        self.watch.begin()
        task = CaptureTask(self.capturer, None, self.foreground.capture_point())
        task.signals.finished.connect(self._on_watch_captured)
        task.signals.failed.connect(self._on_watch_capture_failed)
        self.pool.start(task)

    def _on_watch_capture_failed(self, message: str) -> None:
        # A black screen, a locked display or a grab error: skip this tick, do not count it against the node.
        logger.debug("watch capture skipped: %s", message)
        self.watch.finish(True)

    def _on_watch_captured(self, capture: CaptureResult, recording: RecordingResult | None) -> None:
        if not self.watch.running or self.watch.paused:
            self.watch.finish(True)
            return
        if self.state.is_busy or self._worker is not None or self._action_dialog_open:
            self.watch.finish(True)  # the user started something while this frame was being taken
            return
        try:
            signature = frame_signature(capture.image_b64)
        except Exception:  # noqa: BLE001 - an undecodable frame is skipped, never fatal
            logger.exception("watch: could not fingerprint the frame")
            self._on_watch_failed("could not read the frame")
            return
        if not frame_changed(self._watch_signature, signature):
            self.watch.finish(True)  # the screen is the same: no model call
            return
        if frame_is_flat(signature):
            # A plain colour or blank window has nothing to read (the model says YES to those): not sent.
            self._watch_signature = signature
            self._watch_tracker.report(None)
            self.watch.finish(True)
            return
        self._watch_pending_signature = signature
        self._watch_image = capture.to_image_payload()
        self._send_watch_request("check", CHECK_PROMPT, 16)

    def _send_watch_request(self, stage: str, prompt: str, max_new_tokens: int) -> None:
        """One request to the local node about the frame being looked at (step 1: yes/no, step 2: describe)."""
        self._watch_stage = stage
        if not _is_loopback(self.settings.local_dev_url):  # second guard: never send a frame to another machine
            self._stop_watching(WATCH_NOT_LOOPBACK)
            return
        try:
            request = build_request(self._watch_image, mode=AnalysisMode.EXPLAIN, prompt=prompt, max_new_tokens=max_new_tokens)
        except ValueError as exc:
            self._on_watch_failed(f"could not build the request: {exc}")
            return
        # Forced local, whatever the window says: a race must never send a frame to Kaggle or the web tier.
        local_only = dataclasses.replace(self.settings, backend="local", fallback_api_url=None)
        worker = InferenceWorker(local_only, self.resolver, request, LatencyMetrics())
        worker.succeeded.connect(self._on_watch_answer)
        worker.failed.connect(self._on_watch_failed)
        worker.finished.connect(lambda w=worker: self._retire_watch(w))
        self._watch_worker = worker
        worker.start()

    def _finish_watch_tick(self, ok: bool = True) -> bool:
        self._watch_image = None  # the frame is dropped as soon as the tick is over
        return self.watch.finish(ok)

    def _on_watch_answer(self, payload: dict[str, Any]) -> None:
        if not self.watch.running:
            return
        result = ClientResult.model_validate(payload)
        text = result.response.markdown
        if self._watch_stage == "check":
            if self.state.is_busy or self._worker is not None:
                self._finish_watch_tick()  # the user is using the model: look again later (this frame is not marked seen)
                return
            self._watch_signature = self._watch_pending_signature
            if parse_yes_no(text) is not True:
                self._watch_tracker.report(None)  # nothing wrong on screen: a later error is news again
                self._finish_watch_tick()
            elif self._watch_tracker.active:
                self._finish_watch_tick()  # the error that was already reported is still showing
            else:
                self._send_watch_request("describe", DESCRIBE_PROMPT, DESCRIBE_TOKENS)  # a new error: say what it is
            return
        news = self._watch_tracker.report(parse_watch_reply(text) or GENERIC_FINDING)
        self._finish_watch_tick()
        if news:
            self._notify_finding(alert_text(news), result)

    def _on_watch_failed(self, message: str) -> None:
        if not self.watch.running:
            return
        logger.info("watch tick failed: %s", message)
        if self._finish_watch_tick(False):
            self._stop_watching("Watching stopped: the local node is not answering. Start it from the window, then turn watching on again.")

    def _retire_watch(self, worker: InferenceWorker) -> None:
        if self._watch_worker is worker:
            self._watch_worker = None
            if self.watch.in_flight:  # cancelled in favour of the user: the tick ends without a verdict
                self._finish_watch_tick()
        worker.deleteLater()

    def _notify_finding(self, finding: str, result: ClientResult) -> None:
        logger.info("watch: noticed something (%d characters)", len(finding))
        self._watch_notice_pending = True
        self.tray.showMessage("OmniSight noticed something", finding, QSystemTrayIcon.MessageIcon.Warning, 10000)
        # An unprompted card never offers "Copy Fix" / "Copy Terminal Command" for code the model read off the screen.
        plain = result.model_copy(update={"response": result.response.model_copy(update={"code_blocks": []})})
        self.window.add_exchange("Noticed while watching", plain)

    # -- web search ------------------------------------------------------------------------------------

    def _start_search(self, text: str, *, voice: bool = False) -> None:
        """Search for the typed question while the screen is captured (or the user speaks).

        Only the typed question is ever a query. A spoken question has no text, so it is not searched.
        Asking the cloud tier to ground itself (``web_search``) lets Gemini write its own queries from
        the whole prompt, screen included, so that happens only when the user turned Smart query on.
        """
        self._search_token += 1
        self._search_outcome = None
        self._held_capture = None
        self._web_search_flag = False
        self._search_pending = False
        if not self._search_enabled:
            return
        typed = text.strip()
        if not typed:
            self._web_search_flag = voice and self._smart_enabled
            return
        self._search_pending = True
        ask = self._ask_engine if self._smart_enabled else None
        worker = SearchWorker(self._search_token, self.web_search, typed, ask)
        worker.done.connect(self._on_search_done)
        worker.finished.connect(lambda w=worker: self._retire_search(w))
        self._search_worker = worker
        self.window.set_progress("Searching the web…")
        worker.start()

    def _retire_search(self, worker: SearchWorker) -> None:
        if self._search_worker is worker:
            self._search_worker = None
        worker.deleteLater()

    def _ask_engine(self, prompt: str) -> str:
        """One short chat call for the smart query. Never uses the web tier: the rewrite stays on Kaggle or this PC."""
        bounded = dataclasses.replace(
            self.settings, fallback_api_url=None, request_deadline_s=20.0, read_timeout_s=15.0, local_timeout_s=20.0, retries=0
        )
        client = InferenceClient(bounded, self.resolver)
        request = build_request(None, mode=AnalysisMode.CHAT, prompt=prompt, max_new_tokens=32)
        return client.analyze(request).response.markdown

    def _on_search_done(self, token: int, outcome: SearchOutcome) -> None:
        if token != self._search_token:
            return  # a newer question started; this answer is stale
        self._search_pending = False
        self._search_outcome = outcome
        if not outcome.results and self._smart_enabled:
            self._web_search_flag = True  # Smart query is on: let the cloud tier write its own search if it answers
        if outcome.notice:
            self.window.show_notice(outcome.notice)
        self._dispatch()

    def _web_fields(self) -> tuple[list[Any], bool]:
        outcome = self._search_outcome
        return (list(outcome.results) if outcome else []), self._web_search_flag

    def _dispatch(self) -> None:
        """Send the request once everything it needs (capture, search) is ready."""
        if self._search_pending:
            return
        if self._pending_mode is AnalysisMode.CHAT:
            if self.state.state is AppState.ANALYZING and self._chat_sent != self._search_token:
                self._chat_sent = self._search_token
                self._send_chat()
        elif self._held_capture is not None:
            capture, recording = self._held_capture
            self._held_capture = None
            self._send_capture(capture, recording)

    def on_window_capture(self) -> None:
        self._begin_window_request(AnalysisMode.DEBUG, "")

    def _begin_window_request(self, mode: AnalysisMode, prompt: str) -> None:
        if self.state.is_busy:
            return
        self.stop_speaking()
        self._cancel_watch_request()
        self._origin, self._pending_prompt, self._pending_question = "window", prompt, prompt
        self._pending_mode = mode
        self.state.transition(AppState.CAPTURING, reason="window")
        self._start_search(prompt)
        self._start_capture(None)

    def on_window_mic(self) -> None:
        """Click to start listening, click again to stop and send (Alt+V is hold-to-talk)."""
        if self.state.state is AppState.RECORDING_VOICE:
            self.on_voice_released()
        else:
            typed = self.window.ask_box.text().strip()
            self.window.ask_box.clear()
            self.on_voice_pressed(origin="window", prompt=typed)

    def on_voice_pressed(self, origin: str = "hud", prompt: str = "") -> None:
        if self.state.is_busy:
            logger.info("Alt+V ignored: %s", self.state.state.value)
            return
        self.stop_speaking()
        self._cancel_watch_request()
        self._origin, self._pending_prompt, self._pending_question = origin, prompt, prompt
        try:
            self.recorder.start_recording()
        except MicrophonePermissionError as exc:
            self._error_actions = self._microphone_actions()
            self.state.fail(str(exc))
            return
        except AudioDeviceError as exc:
            self.state.fail(str(exc))
            return
        self.state.transition(AppState.RECORDING_VOICE, reason="Alt+V down")
        self._start_search(prompt, voice=True)

    # -- microphone permission ------------------------------------------------------

    def _microphone_actions(self) -> list[tuple[str, Any]]:
        return [("Open microphone settings", self.open_microphone_settings), ("Check again", self.recheck_microphone)]

    def open_microphone_settings(self) -> None:
        logger.info("opening %s", MIC_SETTINGS_URI)
        QDesktopServices.openUrl(QUrl(MIC_SETTINGS_URI))

    def recheck_microphone(self) -> None:
        permission = microphone_permission()
        logger.info("microphone permission re-checked: %s", permission)
        if permission.startswith("denied"):
            self.hud.show_error(
                "Still blocked. " + PERMISSION_MESSAGES.get(permission, "Microphone access is off."),
                self._microphone_actions(),
            )
            return
        self.state.reset()
        self.hud.set_idle()
        self.hud.status_label.setText("Microphone access is on. Hold Alt+V and speak.")

    def _on_tray_message_clicked(self) -> None:
        if self._watch_notice_pending:
            self._watch_notice_pending = False
            self.open_window()
        elif self._mic_notice_pending:
            self._mic_notice_pending = False
            self.open_microphone_settings()

    def on_voice_released(self) -> None:
        if self.state.state is not AppState.RECORDING_VOICE:
            return
        self._pending_mode = AnalysisMode.VOICE_QUERY
        self.state.transition(AppState.CAPTURING, reason="Alt+V up")
        self._start_capture(self.recorder)

    def on_dismiss(self) -> None:
        self.stop_speaking()  # Esc always quiets the voice, even with the HUD closed
        if self.hud.isVisible() and not self.state.is_busy:
            self.hud.dismiss()

    # -- pipeline ---------------------------------------------------------------

    def _start_capture(self, recorder: AudioRecorder | None) -> None:
        task = CaptureTask(self.capturer, recorder, self.foreground.capture_point())
        task.signals.finished.connect(self._on_capture_finished)
        task.signals.failed.connect(self.state.fail)
        self.pool.start(task)

    def _on_capture_finished(self, capture: CaptureResult, recording: RecordingResult | None) -> None:
        if self.state.state is not AppState.CAPTURING:
            return
        self._held_capture = (capture, recording)
        self._dispatch()  # waits here while the web search is still running

    def _send_capture(self, capture: CaptureResult, recording: RecordingResult | None) -> None:
        if self.state.state is not AppState.CAPTURING:
            return
        results, flag = self._web_fields()
        try:
            request = build_request(
                capture.to_image_payload(),
                mode=self._pending_mode,
                audio_wav=recording.wav_bytes if recording else None,
                audio_duration_ms=recording.duration_ms if recording else None,
                prompt=self._pending_prompt,
                max_new_tokens=self.settings.max_new_tokens,
                history=self.memory.history(),
                web_results=results,
                web_search=flag,
            )
        except ValueError as exc:
            self.state.fail(f"Could not build the request: {exc}")
            return
        metrics = LatencyMetrics(
            capture_ms=capture.capture_latency_ms,
            encode_ms=capture.encode_latency_ms,
            audio_ms=float(recording.duration_ms) if recording else 0.0,
        )
        if self._origin == "hud":
            self.hud.place_on_screen(capture.monitor_rect, capture.monitor_device)
        self.state.transition(AppState.ANALYZING, reason=f"{capture.scaled_res} {capture.payload_bytes // 1024} KB")
        self._start_worker(request, metrics)

    def _start_worker(self, request: Any, metrics: LatencyMetrics) -> None:
        worker = InferenceWorker(self.settings, self.resolver, request, metrics)
        worker.succeeded.connect(self._on_inference_succeeded)
        worker.failed.connect(self.state.fail)
        worker.tier_changed.connect(self.hud.set_tier)
        worker.finished.connect(lambda w=worker: self._retire(w))
        self._worker = worker
        worker.start()

    def _retire(self, worker: InferenceWorker) -> None:
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _on_inference_succeeded(self, payload: dict[str, Any]) -> None:
        result = ClientResult.model_validate(payload)
        self.state.add_result(result.response, result.metrics)
        outcome = self._search_outcome
        attached = bool(outcome and outcome.results)
        note = ""
        if attached and not result.response.sources:
            note = "This engine could not use the web results, so it answered without them."
        elif outcome is not None and not outcome.results and outcome.notice:
            note = outcome.notice  # for example "Web search is unavailable right now ... Answering without it."
        self.window.add_exchange(self._pending_question, result, outcome.query if attached and result.response.sources else "", note)
        question = self._pending_question or result.response.transcript or MODE_PHRASES[self._pending_mode]
        self.memory.add_exchange(question, result.response.markdown)
        if self._speak_enabled:
            self.speaker.speak(result.response.summary)
        if self.state.transition(AppState.DISPLAYING, reason=result.metrics.tier or ""):
            if self._origin == "hud":
                self.hud.show_result(result)
            else:
                self.state.reset()  # the answer is in the window; nothing to dismiss

    def _on_state_changed(self, old: AppState, new: AppState) -> None:
        self.window.set_app_state(new, WINDOW_MESSAGES.get(new, ""))
        if new is AppState.ERROR:
            self.window.show_notice(self.state.last_error or "Something went wrong.", error=True)
            if self._origin != "hud":
                self._error_actions = []
                return
        elif self._origin != "hud":
            return
        if new is AppState.RECORDING_VOICE:
            self.hud.place_on_screen(None)
            self.hud.show_state(new, "Listening… release Alt+V to send your question with the screen.")
        elif new is AppState.CAPTURING:
            if old is AppState.RECORDING_VOICE:
                self.hud.show_state(new, "Capturing the screen…")
        elif new is AppState.ANALYZING:
            self.hud.show_state(new, "Analyzing the screen…")
        elif new is AppState.ERROR:
            actions, self._error_actions = self._error_actions, []
            self.hud.place_on_screen(None)
            self.hud.show_error(self.state.last_error or "Something went wrong.", actions)
        elif new is AppState.IDLE:
            self.hud.set_idle()

    # -- shutdown -----------------------------------------------------------------

    def shutdown(self) -> None:
        logger.info("shutting down")
        self._foreground_timer.stop()
        self._node_timer.stop()
        self._speaking_timer.stop()
        self._watch_timer.stop()
        self._stop_watching()
        watcher = self._watch_worker
        if watcher is not None and not watcher.wait(2000):
            watcher.terminate()
            watcher.wait(500)
        self.speaker.stop()
        self._search_token += 1  # a late search result must not touch a closing app
        worker = self._search_worker
        if worker is not None and not worker.wait(2000):
            worker.terminate()  # a rewrite stuck on a slow node must not outlive the app as a destroyed running thread
            worker.wait(500)
        self.node.shutdown()  # stops a node this app started; an adopted one is left alone
        self.hotkeys.stop()
        self.recorder.close()
        worker = self._worker
        if worker is not None:
            worker.cancel()
            worker.wait(3000)
        self.pool.waitForDone(3000)
        self.capturer.close()
        self.tray.hide()
        self.hud.hide()
        self.window.hide()


WATCH_NOT_LOOPBACK: Final[str] = (
    "Watching only works with a model node on this PC (127.0.0.1 or localhost). The local node URL in Settings points "
    "elsewhere, so your screen would leave this PC: watching is off."
)


WATCH_ON_CPU: Final[str] = (
    "Watching on the CPU engine works but is slow: each check takes several seconds of CPU time and a question you ask "
    "meanwhile can wait behind it. Local GPU is the intended engine for watching; you can pause any time."
)


def _watch_interval() -> float:
    """Seconds between watch ticks (``OMNISIGHT_WATCH_INTERVAL_S``, at least 5; the default is 10)."""
    try:
        return float(os.environ.get("OMNISIGHT_WATCH_INTERVAL_S", DEFAULT_INTERVAL_S))
    except ValueError:
        return DEFAULT_INTERVAL_S


WINDOW_MESSAGES: Final[dict[AppState, str]] = {
    AppState.CAPTURING: "Capturing the screen…",
    AppState.ANALYZING: "Analyzing the screen…",
    AppState.RECORDING_VOICE: "Listening… click “Stop and send” when you are done.",
}


class _WarmUp(QRunnable):
    """First grab on the capture thread so the user's first Alt+C is not the slow one.

    Also probes this PC's CPU/RAM/GPU once (nvidia-smi can take a moment) for the log
    and the Settings dialog.
    """

    def __init__(self, capturer: ScreenCapturer, controller: OmniSightController) -> None:
        super().__init__()
        self.capturer = capturer
        self.controller = controller

    def run(self) -> None:
        try:
            logger.info("capture warm-up %.1f ms [tid %d]", self.capturer.warm_up(), threading.get_native_id())
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            logger.warning("capture warm-up failed: %s", exc)
        try:
            self.controller.capability_line = describe(probe())
            logger.info("this PC: %s", self.controller.capability_line)
        except Exception as exc:  # noqa: BLE001 - informational only
            logger.warning("capability probe failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniSight desktop client")
    parser.add_argument("--override-url", help="talk to this node instead of discovering it from the gist")
    parser.add_argument("--log-level", default=None, choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--no-hotkeys", action="store_true", help="do not install the global keyboard hook")
    parser.add_argument("--backend", choices=tuple(BACKENDS), help="auto, kaggle or local (overrides the saved choice)")
    parser.add_argument("--tray-only", action="store_true", help="start in the tray without opening the window")
    args = parser.parse_args(argv)

    try:
        settings = ClientSettings.from_environment()
        if args.override_url:
            settings = settings.with_override(args.override_url)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    log_path = configure_logging(args.log_level or settings.log_level)
    logger.info("log file: %s", log_path)

    lock = SingleInstanceLock()
    if not lock.acquire():
        logger.info("another instance is running; exiting")
        notify_already_running()
        return 0
    logger.info("single-instance lock: %s", lock.mechanism)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("OmniSight")
    app.setOrganizationName("OmniSight")
    app.setApplicationVersion(CONTRACT_VERSION)
    app.setQuitOnLastWindowClosed(False)

    # Precedence: --backend flag > choice saved from the tray/settings > OMNISIGHT_BACKEND > auto.
    saved = QSettings()
    saved_backend = str(saved.value("backend", "") or "")
    saved_local = str(saved.value("local_url", "") or "")
    saved_device = str(saved.value("local_device", "") or "")
    try:
        if args.backend:
            settings = settings.with_backend(args.backend)
        elif saved_backend in BACKENDS:
            settings = settings.with_backend(saved_backend)
        if saved_local:
            settings = settings.with_local_url(saved_local)
        if saved_device in LOCAL_DEVICES:
            settings = settings.with_local_device(saved_device)
    except ValueError as exc:
        logger.warning("ignoring saved settings: %s", exc)
    logger.info("backend: %s (%s)", settings.backend, BACKENDS[settings.backend])
    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.warning("system tray unavailable; use the hotkeys")

    controller = OmniSightController(app, settings, enable_hotkeys=not args.no_hotkeys)
    app.aboutToQuit.connect(controller.shutdown)
    if not args.tray_only:
        controller.open_window()

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break in a Windows console
        signal.signal(signal.SIGBREAK, lambda *_: app.quit())
    heartbeat = QTimer()
    heartbeat.start(250)  # lets Python run signal handlers while Qt's loop is in C++
    heartbeat.timeout.connect(lambda: None)

    try:
        return app.exec()
    finally:
        lock.release()
        logger.info("OmniSight client exited")


if __name__ == "__main__":
    sys.exit(main())
