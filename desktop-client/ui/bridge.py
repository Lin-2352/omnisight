"""A window that is not a window: the controller's view of an external UI (the C# app) over a loopback socket.

``BridgeWindow`` has the same signals and methods as ``MainWindow``, so ``OmniSightController`` drives it without
knowing the difference. Instead of drawing, it writes one JSON object per line to the connected app and turns the
app's commands back into the same signals a click would have emitted.

Trust and limits:

* It listens on 127.0.0.1 only, on a port the OS picks, and the first line from a client must be an ``auth`` message
  carrying the per-launch token (read from stdin by ``main.py``; never put on a command line).
* One client at a time. A wrong token, a late or missing ``auth``, an oversized line or garbage closes the connection.
* A command can only do what a click on the old window could: every switch and action still goes through the
  controller's own rules (for example "Allow running commands" and the Run approval are enforced in Python).

Events (Python to app) have an ``event`` key; commands (app to Python) have a ``cmd`` key. The sticky events
(settings, node, status, watch, speaking) are replayed to a client that connects later.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Callable
from typing import Any, Final

from core.answer import body_segments, copy_actions, run_commands
from core.config import ENGINE_CHOICES
from core.logger import get_logger
from core.node_supervisor import NodeStatus
from core.state import AppState
from network.schemas import ClientResult
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from ui.bridge_run import MAX_FOLDER_CHARS, BridgeRunSession

logger = get_logger("bridge")

PROTOCOL_VERSION: Final[int] = 1
MAX_LINE_BYTES: Final[int] = 1024 * 1024
AUTH_TIMEOUT_MS: Final[int] = 5000
MAX_EXCHANGES: Final[int] = 30

#: ``set`` command names that are plain switches, and the signal each one drives.
SWITCHES: Final[tuple[str, ...]] = ("memory", "speak", "search", "smart", "watch", "actions")

#: The window greyed these out while a request ran or a recording was in progress (``MainWindow._refresh_controls``);
#: the bridge enforces the same rules, because a client can send a command whenever it likes.
NEEDS_IDLE: Final[frozenset[str]] = frozenset({"ask", "capture", "engine", "clear"})
NEEDS_IDLE_SWITCHES: Final[frozenset[str]] = frozenset({"search", "include_screen"})
BUSY_STATES: Final[frozenset[AppState]] = frozenset({AppState.CAPTURING, AppState.ANALYZING})
MAX_URL_CHARS: Final[int] = 500
BUSY_MESSAGE: Final[str] = "OmniSight is busy: wait for the current answer to finish."


class _TypedText:
    """Stands in for ``MainWindow.ask_box`` where the controller reads what was typed before a voice question."""

    def __init__(self) -> None:
        self.value = ""

    def text(self) -> str:
        return self.value

    def clear(self) -> None:
        self.value = ""


class BridgeWindow(QObject):
    ask_requested = pyqtSignal(str)
    capture_requested = pyqtSignal()
    mic_clicked = pyqtSignal()
    engine_selected = pyqtSignal(str)
    node_toggle_clicked = pyqtSignal()
    clear_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    memory_toggled = pyqtSignal(bool)
    speak_toggled = pyqtSignal(bool)
    stop_speaking_clicked = pyqtSignal()
    search_toggled = pyqtSignal(bool)
    smart_toggled = pyqtSignal(bool)
    watch_toggled = pyqtSignal(bool)
    actions_toggled = pyqtSignal(bool)
    run_requested = pyqtSignal(str, str)
    watch_pause_clicked = pyqtSignal()
    #: The app told us its process id: the controller must never capture or watch that process's windows.
    peer_pid = pyqtSignal(int)
    connected_changed = pyqtSignal(bool)
    settings_info_requested = pyqtSignal()
    settings_apply_requested = pyqtSignal(str, str)  # (override URL, local node URL)
    connection_test_requested = pyqtSignal()

    def __init__(self, token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if not token:
            raise ValueError("the bridge needs a non-empty token")
        self._token = token
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._accept)
        self._client: QTcpSocket | None = None
        self._authed = False
        self._buffer = b""
        self._auth_timer = QTimer(self)
        self._auth_timer.setSingleShot(True)
        self._auth_timer.timeout.connect(lambda: self._drop("no auth message in time"))
        self._sticky: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, Callable[[bool], None]] = {}
        self._run: BridgeRunSession | None = None
        self._exchanges: list[dict[str, Any]] = []
        self._state = AppState.IDLE
        self.ask_box = _TypedText()
        self.include_screen = True
        self.capture_excluded = False

    # -- server ---------------------------------------------------------------------------

    def listen(self) -> int:
        """Start listening on a free loopback port and return it."""
        if not self._server.listen(QHostAddress.SpecialAddress.LocalHost, 0):
            raise OSError(f"bridge could not listen: {self._server.errorString()}")
        return int(self._server.serverPort())

    def close(self) -> None:
        self._drop("closing", quiet=True)
        self._server.close()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._authed

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            if self._client is not None:  # one client at a time
                socket.close()
                socket.deleteLater()
                continue
            self._client = socket
            self._authed = False
            self._buffer = b""
            socket.readyRead.connect(self._read)
            socket.disconnected.connect(lambda: self._drop("client disconnected", quiet=True))
            self._auth_timer.start(AUTH_TIMEOUT_MS)

    def _drop(self, reason: str, *, quiet: bool = False) -> None:
        client, self._client = self._client, None
        was_authed, self._authed = self._authed, False
        self._buffer = b""
        self._auth_timer.stop()
        self._cancel_pending()
        if self._run is not None:
            self._run.abandon()
        if client is not None:
            client.blockSignals(True)
            client.abort()
            client.deleteLater()
            if not quiet:
                logger.warning("bridge connection closed: %s", reason)
        if was_authed:
            self.connected_changed.emit(False)

    def _read(self) -> None:
        client = self._client
        if client is None:
            return
        self._buffer += bytes(client.readAll())
        if len(self._buffer) > MAX_LINE_BYTES and b"\n" not in self._buffer[:MAX_LINE_BYTES]:
            self._drop("line too long")
            return
        while self._client is client and b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if line.strip():
                try:
                    self._handle_line(line)
                except Exception:  # noqa: BLE001 - a bad message must never take the controller down with it
                    logger.exception("bridge: could not handle a message")
                    self._send({"event": "error", "message": "that command could not be handled"})

    def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            if not self._authed:
                self._drop("not a JSON auth message")
            else:
                self._send({"event": "error", "message": "that was not valid JSON"})
            return
        if not isinstance(message, dict):
            if self._authed:
                self._send({"event": "error", "message": "a command must be a JSON object"})
            else:
                self._drop("bad first message")
            return
        if not self._authed:
            self._authenticate(message)
            return
        self._command(message)

    def _authenticate(self, message: dict[str, Any]) -> None:
        sent = message.get("token")
        if message.get("cmd") != "auth" or not isinstance(sent, str) or not hmac.compare_digest(sent.encode(), self._token.encode()):
            self._drop("bad token")
            return
        self._auth_timer.stop()
        self._authed = True
        self._send({"event": "hello", "protocol": PROTOCOL_VERSION})
        # The engine labels live in one place (core.config); the app only displays what it is told.
        self._send({"event": "engines", "choices": [{"key": key, "label": label} for key, (label, _b, _d) in ENGINE_CHOICES.items()]})
        for event in self._sticky.values():
            self._send(event)
        for exchange in self._exchanges:
            self._send(exchange)
        pid = message.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            self.peer_pid.emit(pid)
        self.connected_changed.emit(True)

    # -- commands from the app -----------------------------------------------------------------

    def _command(self, message: dict[str, Any]) -> None:
        name = message.get("cmd")
        handlers: dict[str, Callable[[dict[str, Any]], str | None]] = {
            "ping": lambda _m: self._send({"event": "pong"}) or None,
            "ask": self._cmd_ask,
            "capture": lambda _m: self.capture_requested.emit() or None,
            "mic": self._cmd_mic,
            "engine": self._cmd_engine,
            "node_toggle": lambda _m: self.node_toggle_clicked.emit() or None,
            "clear": lambda _m: self.clear_requested.emit() or None,
            "settings": lambda _m: self.settings_requested.emit() or None,
            "stop_speaking": lambda _m: self.stop_speaking_clicked.emit() or None,
            "watch_pause": lambda _m: self.watch_pause_clicked.emit() or None,
            "set": self._cmd_set,
            "run": self._cmd_run,
            "answer": self._cmd_answer,
            "run.check": self._cmd_run_check,
            "run.execute": self._cmd_run_execute,
            "run.cancel": lambda m: self._with_session(m, lambda s: s.cancel_or_close()),
            "run.close": lambda m: self._with_session(m, lambda s: s.close()),
            "settings.get": lambda _m: self.settings_info_requested.emit() or None,
            "settings.apply": self._cmd_settings_apply,
            "test_connection": lambda _m: self.connection_test_requested.emit() or None,
        }
        handler = handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            self._send({"event": "error", "message": f"unknown command {name!r}"})
            return
        if self._refused_while_busy(name, message):
            self._send({"event": "error", "message": BUSY_MESSAGE})
            return
        problem = handler(message)
        if problem:
            self._send({"event": "error", "message": problem})

    def _refused_while_busy(self, name: str, message: dict[str, Any]) -> bool:
        busy = self._state in BUSY_STATES
        idle = not busy and self._state is not AppState.RECORDING_VOICE
        if name in NEEDS_IDLE or (name == "set" and message.get("name") in NEEDS_IDLE_SWITCHES):
            return not idle
        if name == "mic":  # "Speak" is also "Stop and send" while recording, so only a running request blocks it
            return busy
        return False

    def _cmd_ask(self, message: dict[str, Any]) -> str | None:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return "ask needs non-empty text"
        self.ask_requested.emit(text.strip()[:4000])
        return None

    def _cmd_mic(self, message: dict[str, Any]) -> str | None:
        typed = message.get("typed", "")
        self.ask_box.value = typed.strip()[:4000] if isinstance(typed, str) else ""
        self.mic_clicked.emit()
        return None

    def _cmd_engine(self, message: dict[str, Any]) -> str | None:
        key = message.get("key")
        if not isinstance(key, str) or not key:
            return "engine needs a key"
        self.engine_selected.emit(key)
        return None

    def _cmd_set(self, message: dict[str, Any]) -> str | None:
        name, on = message.get("name"), message.get("on")
        if not isinstance(on, bool):
            return "set needs a true or false value"
        if name == "include_screen":
            self.include_screen = on
            return None
        if name not in SWITCHES:
            return f"unknown switch {name!r}"
        getattr(self, f"{name}_toggled").emit(on)
        return None

    def _cmd_run(self, message: dict[str, Any]) -> str | None:
        language, command = message.get("language"), message.get("command")
        if not isinstance(language, str) or not isinstance(command, str) or not command.strip():
            return "run needs a language and a command"
        if not self._was_offered(language, command):
            return "that command was not offered by an answer"
        self.run_requested.emit(language, command)
        return None

    def _was_offered(self, language: str, command: str) -> bool:
        """Only a command that an answer's Run... button offered can open an approval (never a watch alert's, which has none)."""
        return any(
            run["language"] == language and run["command"] == command
            for exchange in self._exchanges
            if exchange["origin"] != "watch"
            for run in exchange["actions"]["run"]
        )

    def _with_session(self, message: dict[str, Any], action: Callable[[BridgeRunSession], None]) -> str | None:
        session = self._run
        if session is None or message.get("id") != session.key:
            return "there is no such approval open"
        action(session)
        return None

    def _cmd_run_check(self, message: dict[str, Any]) -> str | None:
        cwd = message.get("cwd", "")
        if not isinstance(cwd, str) or len(cwd) > MAX_FOLDER_CHARS:
            return "run.check needs a folder as text"
        return self._with_session(message, lambda s: s.check(cwd))

    def _cmd_run_execute(self, message: dict[str, Any]) -> str | None:
        typed, cwd, timeout = message.get("typed", ""), message.get("cwd", ""), message.get("timeout_s")
        if not isinstance(typed, str) or len(typed) > 16 or not isinstance(cwd, str) or len(cwd) > MAX_FOLDER_CHARS:
            return "run.execute needs the typed word and a folder as text"
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            return "run.execute needs a timeout in seconds"
        problems: list[str] = []
        # The command is never read from this message: the session runs the text it was opened with.
        found = self._with_session(message, lambda s: problems.append(s.execute(typed, cwd, float(timeout)) or ""))
        return found or (problems[0] if problems and problems[0] else None)

    def _cmd_answer(self, message: dict[str, Any]) -> str | None:
        """The app's reply to a question Python asked (``confirm``). Only a pending question, answered once, with a real boolean."""
        key, yes = message.get("id"), message.get("yes")
        if not isinstance(yes, bool) or not isinstance(key, str):
            return "answer needs an id and a true or false value"
        callback = self._pending.pop(key, None)
        if callback is None:
            return "there is no such question to answer"
        callback(yes)
        return None

    def _cmd_settings_apply(self, message: dict[str, Any]) -> str | None:
        override, local_url = message.get("override", ""), message.get("local_url", "")
        if not isinstance(override, str) or not isinstance(local_url, str) or len(override) > MAX_URL_CHARS or len(local_url) > MAX_URL_CHARS:
            return "settings need two addresses as text"
        self.settings_apply_requested.emit(override.strip(), local_url.strip())
        return None

    def _cancel_pending(self) -> None:
        """Questions nobody can answer any more are answered No (the safe default)."""
        pending, self._pending = self._pending, {}
        for callback in pending.values():
            callback(False)

    # -- events to the app (the MainWindow interface the controller calls) ------------------------------

    def _send(self, event: dict[str, Any]) -> None:
        client = self._client
        if client is None or not self._authed:
            return
        client.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")

    def _emit(self, event: dict[str, Any], *, sticky: str | None = None) -> None:
        if sticky is not None:
            self._sticky[sticky] = event
        self._send(event)

    def show(self) -> None:
        self._emit({"event": "show"})

    def raise_(self) -> None:
        return None

    def activateWindow(self) -> None:  # noqa: N802 - Qt name the controller calls
        return None

    def hide(self) -> None:
        return None

    def set_engine(self, key: str) -> None:
        self._emit({"event": "engine", "key": key}, sticky="engine")

    def set_app_state(self, state: AppState, message: str = "") -> None:
        self._state = state
        self._emit({"event": "state", "state": state.value, "message": message}, sticky="state")

    def set_node_status(self, status: NodeStatus, engine_key: str) -> None:
        self._emit(
            {
                "event": "node",
                "state": status.state.value if hasattr(status.state, "value") else str(status.state),
                "message": status.message,
                "device": status.actual_device,
                "owned": status.owned,
                "engine": engine_key,
            },
            sticky="node",
        )

    def show_notice(self, text: str, *, error: bool = False) -> None:
        self._emit({"event": "notice", "text": text, "error": error})

    def set_progress(self, text: str) -> None:
        self._emit({"event": "progress", "text": text})

    def add_exchange(self, question: str, result: ClientResult, searched: str = "", note: str = "", origin: str = "window") -> None:
        response = result.response
        blocks = [(block.language, block.code) for block in response.code_blocks]
        fix, command = copy_actions(blocks)
        event = {
            "event": "exchange",
            "origin": origin,
            "question": question,
            "searched": searched,
            "note": note,
            "response": response.model_dump(mode="json"),
            "metrics": result.metrics.model_dump(mode="json"),
            # Decided here, once, from code_blocks only (watch alerts have none): the app never parses fences out of model text.
            "segments": [
                {"kind": s.kind, "text": s.text, "language": s.language} for s in body_segments(response.markdown, response.summary)
            ],
            "actions": {"copy_fix": fix, "copy_command": command, "run": run_commands(blocks)},
        }
        self._exchanges.append(event)
        del self._exchanges[:-MAX_EXCHANGES]
        self._send(event)

    def clear_exchanges(self) -> None:
        self._exchanges.clear()
        self._send({"event": "clear"})

    @property
    def exchange_count(self) -> int:
        return len(self._exchanges)

    def _switch(self, name: str, on: bool) -> None:
        self._emit({"event": "switch", "name": name, "on": on}, sticky=f"switch:{name}")

    def set_memory_enabled(self, on: bool) -> None:
        self._switch("memory", on)

    def set_speak_enabled(self, on: bool) -> None:
        self._switch("speak", on)

    def set_search_enabled(self, on: bool) -> None:
        self._switch("search", on)

    def set_smart_enabled(self, on: bool) -> None:
        self._switch("smart", on)

    def set_actions_enabled(self, on: bool) -> None:
        self._switch("actions", on)

    def set_watch_enabled(self, on: bool) -> None:
        self._switch("watch", on)
        if not on:
            self.set_watch_status("")

    def set_speak_available(self, available: bool) -> None:
        self._emit({"event": "speak_available", "available": available}, sticky="speak_available")

    def set_speaking(self, speaking: bool) -> None:
        self._emit({"event": "speaking", "on": speaking}, sticky="speaking")

    def ask_allow_actions(self, title: str, text: str, on_answer: Callable[[bool], None]) -> None:
        """Ask the app the "Allow running commands?" question. With nobody to answer, the answer is No."""
        if not self.connected:
            on_answer(False)
            return
        key = secrets.token_hex(8)
        self._pending[key] = on_answer
        self._send({"event": "confirm", "id": key, "kind": "allow_actions", "title": title, "text": text})

    def open_run_session(self, command: str, language: str, runner: Any, cwd: Any) -> BridgeRunSession | None:
        """Open an approval in the app. Nothing opens with nobody connected or while another approval is open."""
        if not self.connected or (self._run is not None and not self._run.closed):
            return None
        key = secrets.token_hex(8)
        session = BridgeRunSession(key, command, language, runner, cwd, self._send, parent=self)
        self._run = session
        session.finished.connect(lambda _code, s=session: self._run_over(s))
        return session

    def _run_over(self, session: BridgeRunSession) -> None:
        if self._run is session:
            self._run = None
        session.deleteLater()

    def set_settings_info(self, info: dict[str, Any]) -> None:
        self._emit({"event": "settings_info", **info}, sticky="settings_info")

    def set_settings_result(self, text: str) -> None:
        self._send({"event": "settings_result", "text": text})

    def set_watch_status(self, text: str, *, paused: bool = False) -> None:
        self._emit({"event": "watch", "text": text, "paused": paused}, sticky="watch")
