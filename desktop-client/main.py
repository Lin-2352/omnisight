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
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Final

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# DPI awareness must be set before Qt or any capture code touches the display.
from capture.screen import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()

from PyQt6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)

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
from core.config import BACKENDS, ClientSettings, EndpointResolver  # noqa: E402
from core.logger import configure_logging, default_log_dir, get_logger  # noqa: E402
from core.state import AppState, StateMachine  # noqa: E402
from network.client import HealthCheckWorker, InferenceWorker, build_request  # noqa: E402
from network.schemas import CONTRACT_VERSION, AnalysisMode, ClientResult, LatencyMetrics  # noqa: E402
from ui.components import BASE, BLUE, GREEN, SURFACE0, TEXT  # noqa: E402
from ui.hud import HudWindow  # noqa: E402

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

    def __init__(self, capturer: ScreenCapturer, recorder: AudioRecorder | None = None) -> None:
        super().__init__()
        self.capturer = capturer
        self.recorder = recorder
        self.signals = CaptureSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        recording: RecordingResult | None = None
        try:
            if self.recorder is not None:
                recording = self.recorder.stop_recording()
            capture = self.capturer.capture()
        except NoSpeechError as exc:
            self.signals.failed.emit(f"No speech detected - hold Alt+V while you speak ({exc}).")
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
        for key, label in BACKENDS.items():
            self.backend.addItem(label, key)
        self.backend.setCurrentIndex(list(BACKENDS).index(controller.settings.backend))
        form.addRow("Backend:", self.backend)
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
            self.controller.set_backend(str(self.backend.currentData()))
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

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("OmniSight - Alt+C analyze, hold Alt+V to ask")
        menu = QMenu()
        for label, slot in (
            ("Show HUD", self.show_hud),
            ("Clear History", self.clear_history),
            ("Settings", self.open_settings),
        ):
            action = QAction(label, menu)
            action.triggered.connect(slot)
            menu.addAction(action)
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
        logger.info("OmniSight client %s ready", CONTRACT_VERSION)

    # -- tray -----------------------------------------------------------------

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_hud()

    def show_hud(self) -> None:
        latest = self.state.latest()
        if latest is not None and not self.state.is_busy:
            self.hud.show_result(ClientResult(response=latest.response, metrics=latest.metrics))
        elif not self.state.is_busy:
            self.hud.set_idle()
        self.hud.place_on_screen(None)
        self.hud.fade_in()

    def clear_history(self) -> None:
        self.state.clear_history()
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
        self.tray.showMessage(
            "OmniSight", f"Backend: {BACKENDS[self.settings.backend]}", QSystemTrayIcon.MessageIcon.Information, 2500
        )

    def set_local_url(self, url: str) -> None:
        self.settings = self.settings.with_local_url(url)
        self.resolver.update_settings(self.settings)
        QSettings().setValue("local_url", self.settings.local_dev_url)

    # -- hotkeys ----------------------------------------------------------------

    def on_capture_requested(self) -> None:
        if self.state.is_busy:
            logger.info("Alt+C ignored: %s", self.state.state.value)
            return
        self._pending_mode = AnalysisMode.DEBUG
        self.state.transition(AppState.CAPTURING, reason="Alt+C")
        self._start_capture(None)

    def on_voice_pressed(self) -> None:
        if self.state.is_busy:
            logger.info("Alt+V ignored: %s", self.state.state.value)
            return
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
        if self._mic_notice_pending:
            self._mic_notice_pending = False
            self.open_microphone_settings()

    def on_voice_released(self) -> None:
        if self.state.state is not AppState.RECORDING_VOICE:
            return
        self._pending_mode = AnalysisMode.VOICE_QUERY
        self.state.transition(AppState.CAPTURING, reason="Alt+V up")
        self._start_capture(self.recorder)

    def on_dismiss(self) -> None:
        if self.hud.isVisible() and not self.state.is_busy:
            self.hud.dismiss()

    # -- pipeline ---------------------------------------------------------------

    def _start_capture(self, recorder: AudioRecorder | None) -> None:
        task = CaptureTask(self.capturer, recorder)
        task.signals.finished.connect(self._on_capture_finished)
        task.signals.failed.connect(self.state.fail)
        self.pool.start(task)

    def _on_capture_finished(self, capture: CaptureResult, recording: RecordingResult | None) -> None:
        if self.state.state is not AppState.CAPTURING:
            return
        try:
            request = build_request(
                capture.to_image_payload(),
                mode=self._pending_mode,
                audio_wav=recording.wav_bytes if recording else None,
                audio_duration_ms=recording.duration_ms if recording else None,
                max_new_tokens=self.settings.max_new_tokens,
            )
        except ValueError as exc:
            self.state.fail(f"Could not build the request: {exc}")
            return
        metrics = LatencyMetrics(
            capture_ms=capture.capture_latency_ms,
            encode_ms=capture.encode_latency_ms,
            audio_ms=float(recording.duration_ms) if recording else 0.0,
        )
        self.hud.place_on_screen(capture.monitor_rect, capture.monitor_device)
        self.state.transition(AppState.ANALYZING, reason=f"{capture.scaled_res} {capture.payload_bytes // 1024} KB")
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
        if self.state.transition(AppState.DISPLAYING, reason=result.metrics.tier or ""):
            self.hud.show_result(result)

    def _on_state_changed(self, old: AppState, new: AppState) -> None:
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
    try:
        if args.backend:
            settings = settings.with_backend(args.backend)
        elif saved_backend in BACKENDS:
            settings = settings.with_backend(saved_backend)
        if saved_local:
            settings = settings.with_local_url(saved_local)
    except ValueError as exc:
        logger.warning("ignoring saved settings: %s", exc)
    logger.info("backend: %s (%s)", settings.backend, BACKENDS[settings.backend])
    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.warning("system tray unavailable; use the hotkeys")

    controller = OmniSightController(app, settings, enable_hotkeys=not args.no_hotkeys)
    app.aboutToQuit.connect(controller.shutdown)

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
