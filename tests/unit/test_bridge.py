"""The bridge between the controller and the C# app: a real loopback socket, the Qt event loop pumped by the test."""

from __future__ import annotations

import json
from typing import Any

import pytest
from PyQt6.QtNetwork import QAbstractSocket, QHostAddress, QTcpSocket

from core.foreground import ForegroundTracker, WindowInfo
from core.node_supervisor import NodeState, NodeStatus
from core.state import AppState
from network.schemas import AnalyzeResponse, ClientResult, LatencyMetrics
from tests.support import analyze_response_json, wait_until
from ui.bridge import MAX_LINE_BYTES, PROTOCOL_VERSION, BridgeWindow

TOKEN = "t0ken-for-tests"


class Peer:
    """A stand-in for the C# app."""

    def __init__(self, qapp: Any, port: int) -> None:
        self.qapp = qapp
        self.socket = QTcpSocket()
        self.socket.connectToHost(QHostAddress.SpecialAddress.LocalHost, port)
        assert wait_until(lambda: self.socket.state() == QAbstractSocket.SocketState.ConnectedState, 20, self.pump)
        self.buffer = b""
        self.events: list[dict[str, Any]] = []

    def pump(self) -> None:
        self.qapp.processEvents()
        data = bytes(self.socket.readAll())
        if data:
            self.buffer += data
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self.events.append(json.loads(line))

    def send(self, message: Any) -> None:
        raw = message if isinstance(message, bytes) else json.dumps(message).encode()
        self.socket.write(raw + b"\n")
        self.socket.flush()

    def auth(self, token: str = TOKEN, pid: int | None = 4242) -> None:
        self.send({"cmd": "auth", "token": token, **({"pid": pid} if pid is not None else {})})

    def wait_for(self, kind: str, timeout: float = 20.0) -> dict[str, Any]:
        assert wait_until(lambda: any(e["event"] == kind for e in self.events), timeout, self.pump), f"no {kind!r} in {self.events}"
        return next(e for e in self.events if e["event"] == kind)

    def closed(self, timeout: float = 20.0) -> bool:
        return wait_until(lambda: self.socket.state() == QAbstractSocket.SocketState.UnconnectedState, timeout, self.pump)

    def settle(self, seconds: float = 0.3) -> None:
        wait_until(lambda: False, seconds, self.pump)


@pytest.fixture
def bridge(qapp: Any):
    window = BridgeWindow(TOKEN)
    window.port = window.listen()  # type: ignore[attr-defined]
    yield window
    window.close()


def connect(qapp: Any, bridge: BridgeWindow) -> Peer:
    peer = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    peer.auth()
    peer.wait_for("hello")
    return peer


def result() -> ClientResult:
    return ClientResult(response=AnalyzeResponse.model_validate(analyze_response_json()), metrics=LatencyMetrics(tier="local"))


# -- trust ---------------------------------------------------------------------------


def test_listens_on_loopback_only(bridge: BridgeWindow) -> None:
    assert bridge._server.serverAddress().isLoopback()
    assert bridge.port > 0  # type: ignore[attr-defined]


def test_empty_token_is_refused_at_construction(qapp: Any) -> None:
    with pytest.raises(ValueError):
        BridgeWindow("")


def test_wrong_token_is_dropped_and_nothing_is_sent(qapp: Any, bridge: BridgeWindow) -> None:
    bridge.set_engine("local_gpu")
    peer = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    peer.auth("not-the-token")
    assert peer.closed()
    assert peer.events == [] and not bridge.connected


@pytest.mark.parametrize("first", [b"not json at all", b"[1, 2]", b'{"cmd": "ask", "text": "hi"}', b'{"cmd": "auth"}', b'{"cmd": "auth", "token": 5}'])
def test_anything_but_a_valid_auth_first_closes_the_connection(qapp: Any, bridge: BridgeWindow, first: bytes) -> None:
    asked: list[str] = []
    bridge.ask_requested.connect(asked.append)
    peer = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    peer.send(first)
    assert peer.closed()
    assert asked == [] and not bridge.connected


def test_a_client_that_never_authenticates_is_dropped(qapp: Any, bridge: BridgeWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ui.bridge.AUTH_TIMEOUT_MS", 150)
    peer = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    assert peer.closed(20)


def test_only_one_client_at_a_time(qapp: Any, bridge: BridgeWindow) -> None:
    first = connect(qapp, bridge)
    second = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    assert second.closed()
    first.send({"cmd": "ping"})
    first.wait_for("pong")


def test_oversized_line_closes_the_connection(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    peer.socket.write(b"x" * (MAX_LINE_BYTES + 10))
    peer.socket.flush()
    assert peer.closed()


def test_the_engine_choices_come_from_core_config_right_after_hello(qapp: Any, bridge: BridgeWindow) -> None:
    from core.config import ENGINE_CHOICES

    peer = connect(qapp, bridge)
    choices = peer.wait_for("engines")["choices"]
    assert [c["key"] for c in choices] == list(ENGINE_CHOICES)
    assert [c["label"] for c in choices] == [label for label, _b, _d in ENGINE_CHOICES.values()]
    kinds = [e["event"] for e in peer.events]
    assert kinds.index("hello") < kinds.index("engines")


def test_hello_carries_the_protocol_version_and_the_peer_pid_is_announced(qapp: Any, bridge: BridgeWindow) -> None:
    pids: list[int] = []
    bridge.peer_pid.connect(pids.append)
    peer = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    peer.auth(pid=777)
    assert peer.wait_for("hello")["protocol"] == PROTOCOL_VERSION
    assert wait_until(lambda: pids == [777], 20, peer.pump)


@pytest.mark.parametrize("pid", [0, -5, "12", True, None])
def test_a_bad_pid_is_ignored(qapp: Any, bridge: BridgeWindow, pid: Any) -> None:
    pids: list[int] = []
    bridge.peer_pid.connect(pids.append)
    peer = Peer(qapp, bridge.port)  # type: ignore[attr-defined]
    peer.send({"cmd": "auth", "token": TOKEN, "pid": pid})
    peer.wait_for("hello")
    peer.settle()
    assert pids == []


# -- commands become the same signals a click would emit -----------------------------------------


def test_commands_emit_the_window_signals(qapp: Any, bridge: BridgeWindow) -> None:
    seen: list[tuple[str, Any]] = []
    for name in ("capture_requested", "node_toggle_clicked", "clear_requested", "settings_requested", "stop_speaking_clicked", "watch_pause_clicked", "mic_clicked"):
        getattr(bridge, name).connect(lambda *a, n=name: seen.append((n, a)))
    bridge.ask_requested.connect(lambda t: seen.append(("ask", t)))
    bridge.engine_selected.connect(lambda k: seen.append(("engine", k)))
    bridge.run_requested.connect(lambda lang, cmd: seen.append(("run", (lang, cmd))))
    bridge.add_exchange("q", answer("Try:\n\n```powershell\nGet-Date\n```\n"))  # an answer must offer a command before it can be run
    peer = connect(qapp, bridge)
    for message in (
        {"cmd": "ask", "text": "  why does this crash?  "},
        {"cmd": "capture"}, {"cmd": "node_toggle"}, {"cmd": "clear"}, {"cmd": "settings"},
        {"cmd": "stop_speaking"}, {"cmd": "watch_pause"}, {"cmd": "engine", "key": "local_gpu"},
        {"cmd": "run", "language": "powershell", "command": "Get-Date"},
    ):
        peer.send(message)
    assert wait_until(lambda: len(seen) == 9, 20, peer.pump), seen
    assert ("ask", "why does this crash?") in seen and ("engine", "local_gpu") in seen and ("run", ("powershell", "Get-Date")) in seen


@pytest.mark.parametrize("name", ["memory", "speak", "search", "smart", "watch", "actions"])
@pytest.mark.parametrize("on", [True, False])
def test_each_switch_drives_its_own_signal(qapp: Any, bridge: BridgeWindow, name: str, on: bool) -> None:
    got: list[bool] = []
    getattr(bridge, f"{name}_toggled").connect(got.append)
    peer = connect(qapp, bridge)
    peer.send({"cmd": "set", "name": name, "on": on})
    assert wait_until(lambda: got == [on], 20, peer.pump)


def test_include_screen_is_a_plain_property_and_voice_carries_typed_text(qapp: Any, bridge: BridgeWindow) -> None:
    mics: list[str] = []
    bridge.mic_clicked.connect(lambda: mics.append(bridge.ask_box.text()))
    peer = connect(qapp, bridge)
    assert bridge.include_screen is True
    peer.send({"cmd": "set", "name": "include_screen", "on": False})
    peer.send({"cmd": "mic", "typed": "  explain this  "})
    assert wait_until(lambda: mics == ["explain this"], 20, peer.pump)
    assert bridge.include_screen is False
    bridge.ask_box.clear()
    assert bridge.ask_box.text() == ""


@pytest.mark.parametrize(
    "message",
    [
        {"cmd": "nope"}, {"cmd": 5}, {"nocmd": True}, {"cmd": "ask"}, {"cmd": "ask", "text": "   "}, {"cmd": "ask", "text": 7},
        {"cmd": "engine"}, {"cmd": "engine", "key": ""}, {"cmd": "set", "name": "memory", "on": "yes"}, {"cmd": "set", "name": "memory", "on": 1},
        {"cmd": "set", "name": "bogus", "on": True}, {"cmd": "run", "language": "powershell"}, {"cmd": "run", "language": "powershell", "command": "  "},
        {"cmd": "run", "language": 3, "command": "x"},
    ],
)
def test_bad_commands_get_an_error_event_and_change_nothing(qapp: Any, bridge: BridgeWindow, message: dict[str, Any]) -> None:
    fired: list[str] = []
    for name in ("ask_requested", "engine_selected", "memory_toggled", "run_requested"):
        getattr(bridge, name).connect(lambda *a, n=name: fired.append(n))
    peer = connect(qapp, bridge)
    peer.send(message)
    peer.wait_for("error")
    assert fired == []


def test_invalid_json_after_auth_is_an_error_not_a_crash(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    peer.send(b"{broken")
    peer.send(b"[1]")
    assert wait_until(lambda: sum(e["event"] == "error" for e in peer.events) == 2, 20, peer.pump)
    peer.send({"cmd": "ping"})
    peer.wait_for("pong")


# -- events the controller pushes ---------------------------------------------------------------


def test_events_reach_the_app(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    bridge.set_app_state(AppState.ANALYZING, "Analyzing the screen…")
    bridge.show_notice("careful", error=True)
    bridge.set_progress("Searching the web…")
    bridge.set_node_status(NodeStatus(NodeState.READY, actual_device="GPU: RTX", owned=True), "local_gpu")
    bridge.add_exchange("why?", result(), searched="python keyerror", note="search was slow")
    peer.wait_for("exchange")
    kinds = {e["event"] for e in peer.events}
    assert {"state", "notice", "progress", "node", "exchange"} <= kinds
    state = next(e for e in peer.events if e["event"] == "state")
    assert state == {"event": "state", "state": "analyzing", "message": "Analyzing the screen…"}
    node = next(e for e in peer.events if e["event"] == "node")
    assert node["device"] == "GPU: RTX" and node["owned"] is True and node["engine"] == "local_gpu"
    ex = next(e for e in peer.events if e["event"] == "exchange")
    assert ex["question"] == "why?" and ex["searched"] == "python keyerror" and ex["note"] == "search was slow"
    assert ex["response"]["summary"] and ex["metrics"]["tier"] == "local"


def test_unicode_survives(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    bridge.show_notice("Fehler: Größe → 日本語 “quoted”")
    assert peer.wait_for("notice")["text"] == "Fehler: Größe → 日本語 “quoted”"


def test_a_late_client_gets_the_current_settings_and_the_conversation(qapp: Any, bridge: BridgeWindow) -> None:
    bridge.set_engine("local_cpu")
    bridge.set_memory_enabled(False)
    bridge.set_search_enabled(True)
    bridge.set_watch_status("Watching every 10 s - frames stay on this PC", paused=True)
    bridge.set_speak_available(True)
    bridge.add_exchange("first", result())
    bridge.add_exchange("second", result())
    peer = connect(qapp, bridge)
    peer.settle()
    assert next(e for e in peer.events if e["event"] == "engine")["key"] == "local_cpu"
    switches = {e["name"]: e["on"] for e in peer.events if e["event"] == "switch"}
    assert switches == {"memory": False, "search": True}
    assert next(e for e in peer.events if e["event"] == "watch")["paused"] is True
    assert [e["question"] for e in peer.events if e["event"] == "exchange"] == ["first", "second"]


def test_clear_empties_what_a_later_client_would_see(qapp: Any, bridge: BridgeWindow) -> None:
    bridge.add_exchange("q", result())
    bridge.clear_exchanges()
    assert bridge.exchange_count == 0
    peer = connect(qapp, bridge)
    peer.settle()
    assert not any(e["event"] == "exchange" for e in peer.events)


def test_the_conversation_is_capped(qapp: Any, bridge: BridgeWindow) -> None:
    for i in range(45):
        bridge.add_exchange(f"q{i}", result())
    assert bridge.exchange_count == 30


def test_turning_watch_off_clears_its_status(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    bridge.set_watch_status("Watching", paused=False)
    bridge.set_watch_enabled(False)
    assert wait_until(lambda: [e["text"] for e in peer.events if e["event"] == "watch"][-1:] == [""], 20, peer.pump)


def test_sending_with_no_client_is_harmless(qapp: Any, bridge: BridgeWindow) -> None:
    bridge.show_notice("nobody listening")
    bridge.set_speaking(True)
    assert not bridge.connected


def test_disconnect_is_reported_and_a_new_client_can_connect(qapp: Any, bridge: BridgeWindow) -> None:
    states: list[bool] = []
    bridge.connected_changed.connect(states.append)
    peer = connect(qapp, bridge)
    peer.socket.disconnectFromHost()
    assert wait_until(lambda: states == [True, False], 20, peer.pump)
    again = connect(qapp, bridge)
    again.send({"cmd": "ping"})
    again.wait_for("pong")


def test_the_window_interface_the_controller_calls_is_all_there(bridge: BridgeWindow) -> None:
    for name in (
        "show", "raise_", "activateWindow", "hide", "set_engine", "set_app_state", "set_node_status", "show_notice", "set_progress",
        "add_exchange", "clear_exchanges", "set_memory_enabled", "set_speak_enabled", "set_speak_available", "set_speaking",
        "set_search_enabled", "set_smart_enabled", "set_actions_enabled", "set_watch_enabled", "set_watch_status",
    ):
        assert callable(getattr(bridge, name)), name
    for name in (
        "ask_requested", "capture_requested", "mic_clicked", "engine_selected", "node_toggle_clicked", "clear_requested", "settings_requested",
        "memory_toggled", "speak_toggled", "stop_speaking_clicked", "search_toggled", "smart_toggled", "watch_toggled", "actions_toggled",
        "run_requested", "watch_pause_clicked",
    ):
        assert hasattr(bridge, name), name
    bridge.show(), bridge.raise_(), bridge.activateWindow(), bridge.hide()


# -- the peer's windows are never the user's window ------------------------------------------------


def test_the_peer_process_is_never_the_users_window() -> None:
    mine, theirs, user = WindowInfo(1, 100, (10, 10)), WindowInfo(2, 4242, (20, 20)), WindowInfo(3, 555, (30, 30))
    current = [user]
    tracker = ForegroundTracker(own_pid=100, probe=lambda: current[0])
    tracker.poll()
    assert tracker.last_external == user
    tracker.add_own_pid(4242)
    current[0] = theirs
    tracker.poll()
    assert tracker.last_external == user and tracker.capture_point() == (30, 30)
    assert tracker.foreground_is_own()
    current[0] = mine
    assert tracker.foreground_is_own()
    current[0] = user
    assert not tracker.foreground_is_own()


def test_registering_the_pid_forgets_a_window_of_that_process() -> None:
    tracker = ForegroundTracker(own_pid=1, probe=lambda: WindowInfo(9, 4242, (5, 5)))
    tracker.poll()
    assert tracker.last_external is not None
    tracker.add_own_pid(4242)
    assert tracker.last_external is None and tracker.capture_point() is None


def test_a_handler_that_blows_up_becomes_an_error_event_not_a_dead_process(qapp: Any, bridge: BridgeWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = connect(qapp, bridge)

    def boom(_message: dict[str, Any]) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(bridge, "_cmd_ask", boom)
    peer.send({"cmd": "ask", "text": "hi"})
    assert "could not be handled" in peer.wait_for("error")["message"]
    peer.send({"cmd": "ping"})
    peer.wait_for("pong")


# -- the window's busy rules, enforced here because a client can send a command at any time ----------------


@pytest.mark.parametrize("state", [AppState.CAPTURING, AppState.ANALYZING, AppState.RECORDING_VOICE])
@pytest.mark.parametrize(
    "message",
    [
        {"cmd": "ask", "text": "hi"}, {"cmd": "capture"}, {"cmd": "engine", "key": "local_gpu"}, {"cmd": "clear"},
        {"cmd": "set", "name": "search", "on": True}, {"cmd": "set", "name": "include_screen", "on": False},
    ],
)
def test_input_commands_are_refused_while_busy_or_recording(qapp: Any, bridge: BridgeWindow, state: AppState, message: dict[str, Any]) -> None:
    fired: list[str] = []
    for name in ("ask_requested", "capture_requested", "engine_selected", "clear_requested", "search_toggled"):
        getattr(bridge, name).connect(lambda *a, n=name: fired.append(n))
    peer = connect(qapp, bridge)
    bridge.set_app_state(state)
    peer.send(message)
    assert "busy" in peer.wait_for("error")["message"]
    assert fired == [] and bridge.include_screen is True


def test_the_same_commands_work_again_once_idle(qapp: Any, bridge: BridgeWindow) -> None:
    asked: list[str] = []
    bridge.ask_requested.connect(asked.append)
    peer = connect(qapp, bridge)
    bridge.set_app_state(AppState.ANALYZING)
    peer.send({"cmd": "ask", "text": "too early"})
    peer.wait_for("error")
    bridge.set_app_state(AppState.IDLE)
    peer.send({"cmd": "ask", "text": "now"})
    assert wait_until(lambda: asked == ["now"], 20, peer.pump)
    bridge.set_app_state(AppState.DISPLAYING)
    peer.send({"cmd": "ask", "text": "after an answer"})
    assert wait_until(lambda: asked == ["now", "after an answer"], 20, peer.pump)


def test_the_microphone_stops_a_recording_but_is_blocked_by_a_running_request(qapp: Any, bridge: BridgeWindow) -> None:
    clicks: list[int] = []
    bridge.mic_clicked.connect(lambda: clicks.append(1))
    peer = connect(qapp, bridge)
    bridge.set_app_state(AppState.RECORDING_VOICE)
    peer.send({"cmd": "mic"})
    assert wait_until(lambda: clicks == [1], 20, peer.pump)  # "Stop and send"
    bridge.set_app_state(AppState.ANALYZING)
    peer.send({"cmd": "mic"})
    assert "busy" in peer.wait_for("error")["message"]
    assert clicks == [1]


@pytest.mark.parametrize("name", ["memory", "speak", "smart", "watch", "actions"])
@pytest.mark.parametrize("state", [AppState.ANALYZING, AppState.RECORDING_VOICE])
def test_the_other_switches_and_actions_still_work_while_busy(qapp: Any, bridge: BridgeWindow, name: str, state: AppState) -> None:
    got: list[bool] = []
    getattr(bridge, f"{name}_toggled").connect(got.append)
    stops: list[int] = []
    bridge.stop_speaking_clicked.connect(lambda: stops.append(1))
    pauses: list[int] = []
    bridge.watch_pause_clicked.connect(lambda: pauses.append(1))
    peer = connect(qapp, bridge)
    bridge.set_app_state(state)
    peer.send({"cmd": "set", "name": name, "on": True})
    peer.send({"cmd": "stop_speaking"})
    peer.send({"cmd": "watch_pause"})
    assert wait_until(lambda: got == [True] and stops == [1] and pauses == [1], 20, peer.pump)


# -- the exchange event carries ready-made segments and actions, so the app never parses fences out of model text ------------

ANSWER = """**The loop reads scores[3].**

It runs one past the end.

```python
for i in range(3):
    print(scores[i])
```

```powershell
python app.py
```
"""


def answer(markdown: str = ANSWER) -> ClientResult:
    from omnisight_contracts import derive_summary, extract_code_blocks

    data = analyze_response_json()
    data["markdown"] = markdown
    data["summary"] = derive_summary(markdown)
    data["code_blocks"] = [block.model_dump(mode="json") for block in extract_code_blocks(markdown)]
    return ClientResult(response=AnalyzeResponse.model_validate(data), metrics=LatencyMetrics(tier="local"))


def test_exchange_has_segments_actions_and_an_origin(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    bridge.add_exchange("why?", answer())
    ex = peer.wait_for("exchange")
    assert ex["origin"] == "window"
    assert [s["kind"] for s in ex["segments"]] == ["prose", "code", "code"]
    assert ex["segments"][0]["text"] == "It runs one past the end."  # the repeated summary paragraph is gone
    assert ex["actions"]["copy_fix"].startswith("for i in range(3)")
    assert ex["actions"]["copy_command"] == "python app.py"
    assert ex["actions"]["run"] == [{"language": "powershell", "command": "python app.py"}]


def test_a_watch_alert_offers_no_copy_or_run_even_though_its_text_has_fences(qapp: Any, bridge: BridgeWindow) -> None:
    """The controller empties code_blocks for unprompted cards; the app must not get actions back from the markdown."""
    response = answer().response
    plain = ClientResult(response=response.model_copy(update={"code_blocks": []}), metrics=LatencyMetrics(tier="local"))
    peer = connect(qapp, bridge)
    bridge.add_exchange("Noticed while watching", plain, origin="watch")
    ex = peer.wait_for("exchange")
    assert ex["origin"] == "watch"
    assert ex["actions"] == {"copy_fix": "", "copy_command": "", "run": []}
    assert any(s["kind"] == "code" for s in ex["segments"])  # still shown (each code block has its own Copy, as in the Qt card)


def test_a_late_client_gets_the_same_exchange_with_its_actions(qapp: Any, bridge: BridgeWindow) -> None:
    bridge.add_exchange("q", answer())
    peer = connect(qapp, bridge)
    peer.settle()
    ex = next(e for e in peer.events if e["event"] == "exchange")
    assert ex["actions"]["run"] and ex["segments"]


# -- the "Allow running commands?" question, asked in the app: the safe answer is always No ----------------------------------


def test_the_question_reaches_the_app_and_a_yes_answers_it_once(qapp: Any, bridge: BridgeWindow) -> None:
    answers: list[bool] = []
    peer = connect(qapp, bridge)
    bridge.ask_allow_actions("Allow running commands?", "Turn it on?", answers.append)
    ask = peer.wait_for("confirm")
    assert ask["kind"] == "allow_actions" and ask["title"] == "Allow running commands?" and ask["text"] == "Turn it on?"
    peer.send({"cmd": "answer", "id": ask["id"], "yes": True})
    assert wait_until(lambda: answers == [True], 20, peer.pump)
    peer.send({"cmd": "answer", "id": ask["id"], "yes": True})  # a second answer to the same question
    assert "no such question" in peer.wait_for("error")["message"]
    assert answers == [True]


def test_a_no_answers_no(qapp: Any, bridge: BridgeWindow) -> None:
    answers: list[bool] = []
    peer = connect(qapp, bridge)
    bridge.ask_allow_actions("t", "x", answers.append)
    peer.send({"cmd": "answer", "id": peer.wait_for("confirm")["id"], "yes": False})
    assert wait_until(lambda: answers == [False], 20, peer.pump)


def test_with_nobody_connected_the_answer_is_no_at_once(qapp: Any, bridge: BridgeWindow) -> None:
    answers: list[bool] = []
    bridge.ask_allow_actions("t", "x", answers.append)
    assert answers == [False]


def test_if_the_app_goes_away_unanswered_the_answer_is_no(qapp: Any, bridge: BridgeWindow) -> None:
    answers: list[bool] = []
    peer = connect(qapp, bridge)
    bridge.ask_allow_actions("t", "x", answers.append)
    peer.wait_for("confirm")
    peer.socket.disconnectFromHost()
    assert wait_until(lambda: answers == [False], 20, peer.pump)


@pytest.mark.parametrize(
    "message",
    [
        {"cmd": "answer"}, {"cmd": "answer", "id": "nope", "yes": True}, {"cmd": "answer", "id": 5, "yes": True},
        {"cmd": "answer", "id": "x", "yes": "yes"}, {"cmd": "answer", "id": "x", "yes": 1}, {"cmd": "answer", "id": "x"},
    ],
)
def test_bad_or_invented_answers_change_nothing(qapp: Any, bridge: BridgeWindow, message: dict[str, Any]) -> None:
    answers: list[bool] = []
    peer = connect(qapp, bridge)
    bridge.ask_allow_actions("t", "x", answers.append)
    peer.wait_for("confirm")
    peer.send(message)
    peer.wait_for("error")
    assert answers == []


def test_each_question_has_its_own_unguessable_id(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    for _ in range(5):
        bridge.ask_allow_actions("t", "x", lambda _yes: None)
    assert wait_until(lambda: sum(e["event"] == "confirm" for e in peer.events) == 5, 20, peer.pump)
    ids = [e["id"] for e in peer.events if e["event"] == "confirm"]
    assert len(set(ids)) == 5 and all(len(i) == 16 for i in ids)


# -- settings commands ---------------------------------------------------------------------------------------------


def test_settings_commands_emit_signals(qapp: Any, bridge: BridgeWindow) -> None:
    seen: list[Any] = []
    bridge.settings_info_requested.connect(lambda: seen.append("info"))
    bridge.settings_apply_requested.connect(lambda o, local: seen.append((o, local)))
    bridge.connection_test_requested.connect(lambda: seen.append("test"))
    peer = connect(qapp, bridge)
    peer.send({"cmd": "settings.get"})
    peer.send({"cmd": "settings.apply", "override": "  https://a.trycloudflare.com  ", "local_url": " http://127.0.0.1:8000 "})
    peer.send({"cmd": "test_connection"})
    assert wait_until(lambda: len(seen) == 3, 20, peer.pump), seen
    assert seen == ["info", ("https://a.trycloudflare.com", "http://127.0.0.1:8000"), "test"]


@pytest.mark.parametrize(
    "message",
    [
        {"cmd": "settings.apply", "override": 5, "local_url": ""}, {"cmd": "settings.apply", "override": "", "local_url": None},
        {"cmd": "settings.apply", "override": "x" * 501, "local_url": ""}, {"cmd": "settings.apply", "override": "", "local_url": "y" * 501},
    ],
)
def test_bad_settings_are_refused_before_they_reach_the_controller(qapp: Any, bridge: BridgeWindow, message: dict[str, Any]) -> None:
    applied: list[Any] = []
    bridge.settings_apply_requested.connect(lambda o, local: applied.append((o, local)))
    peer = connect(qapp, bridge)
    peer.send(message)
    peer.wait_for("error")
    assert applied == []


def test_settings_info_and_results_reach_the_app_and_info_is_replayed(qapp: Any, bridge: BridgeWindow) -> None:
    bridge.set_settings_info({"endpoint": "http://x (gist)", "override_url": "", "local_url": "http://127.0.0.1:8000"})
    peer = connect(qapp, bridge)
    assert peer.wait_for("settings_info")["local_url"] == "http://127.0.0.1:8000"
    bridge.set_settings_result("Saved for this session.")
    assert peer.wait_for("settings_result")["text"] == "Saved for this session."


# -- a deliberate quit and the tray's Settings ----------------------------------------------------------------------


def test_goodbye_reaches_the_app_before_the_engine_quits(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    bridge.say_goodbye()
    peer.wait_for("bye")


def test_goodbye_and_settings_are_harmless_with_nobody_connected(bridge: BridgeWindow) -> None:
    bridge.say_goodbye()
    bridge.show_settings()


def test_the_trays_settings_opens_the_apps_own_page(qapp: Any, bridge: BridgeWindow) -> None:
    peer = connect(qapp, bridge)
    bridge.show_settings()
    peer.wait_for("open_options")
