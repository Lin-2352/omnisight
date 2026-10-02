"""The OmniSight window and how the controller drives it (Qt offscreen; every backend is faked).

Covers the mouse path end to end: typed question -> capture -> request (with the prompt) -> answer
in the window, hotkeys still using the HUD, capture ignoring OmniSight's own window, click-to-talk,
the engine picker and starting/stopping the local node. The model worker, node supervisor,
recorder and the QSettings store are replaced so nothing real starts and no registry key is written.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import main
from capture import screen
from capture.audio import RecordingResult
from core.foreground import WindowInfo
from core.actions import ActionRunner, AuditLog
from core.memory import ConversationMemory
from core.search import SearchOutcome
from core.node_supervisor import NodeState, NodeStatus
from core.state import AppState
from network.schemas import AnalysisMode, AnalyzeResponse, ClientResult, LatencyMetrics, WebResult
from tests.support import FakeMss, ManualClock, analyze_response_json, render_terminal, wait_until, wav_bytes
from ui.main_window import MAX_EXCHANGES, MainWindow

TIMEOUT_S = 10.0


class MemorySettings:
    """Stands in for QSettings (which would write to the user's registry)."""

    store: dict[str, Any] = {}

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:  # noqa: A002 - Qt API
        return self.store.get(key, default)

    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - Qt API
        self.store[key] = value


class FakeWorker(QObject):
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)
    tier_changed = pyqtSignal(str)
    finished = pyqtSignal()
    instances: list[FakeWorker] = []
    echo_sources = True
    reply: str | None = None  # markdown to answer with (default: the fake node's text)
    script: list[str] = []  # replies handed out in order, one per worker, before falling back to ``reply``
    fail_with: str | None = None
    hold = False  # do not answer until release() is called

    def __init__(self, settings: Any, resolver: Any, request: Any, base_metrics: Any = None) -> None:
        super().__init__()
        self.request = request
        self.settings = settings
        self.cancelled = False
        self.scripted = FakeWorker.script.pop(0) if FakeWorker.script else None
        FakeWorker.instances.append(self)

    def start(self) -> None:
        if not FakeWorker.hold:
            QTimer.singleShot(0, self._run)

    def release(self, reply: str | None = None) -> None:
        if reply is not None:
            self.scripted = reply
        self._run()

    def _run(self) -> None:
        if FakeWorker.fail_with:
            self.failed.emit(FakeWorker.fail_with)
            self.finished.emit()
            return
        self.tier_changed.emit("local")
        body = analyze_response_json(str(self.request.request_id), source="kaggle", model_id="fake/engine")
        reply = self.scripted if self.scripted is not None else FakeWorker.reply
        if reply is not None:
            body["markdown"] = reply
            body["summary"] = reply[:400]
            body["code_blocks"] = []
        if FakeWorker.echo_sources:  # like the real node: the results it was given are its sources
            body["sources"] = [r.model_dump() for r in self.request.web_results]
        response = AnalyzeResponse.model_validate(body)
        result = ClientResult(response=response, metrics=LatencyMetrics(tier="local", server_ttft_ms=1500, tokens_generated=42))
        self.succeeded.emit(result.model_dump(mode="json"))
        self.finished.emit()

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, timeout_ms: int = 0) -> bool:
        return True


class FakeNode:
    def __init__(self, root: Any, url: str) -> None:
        self.url = url
        self.status = NodeStatus(NodeState.STOPPED, "The local node is not running.")
        self.calls: list[tuple[str, ...]] = []

    def set_url(self, url: str) -> None:
        self.url = url

    def start(self, device: str = "auto", model: str = "2b") -> NodeStatus:
        self.calls.append(("start", device))
        self.status = NodeStatus(NodeState.STARTING, "Starting the local node...", device, owned=True, pid=1)
        return self.status

    def stop(self) -> NodeStatus:
        self.calls.append(("stop",))
        self.status = NodeStatus(NodeState.STOPPED, "The local node was stopped.", self.status.requested_device)
        return self.status

    def refresh(self) -> NodeStatus:
        return self.status

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))


class FakeSpeaker:
    """Stands in for the Windows voice: records what would be spoken."""

    available = True
    spoken: list[str] = []
    stops = 0
    speaking = False

    def speak(self, text: str) -> bool:
        FakeSpeaker.spoken.append(text)
        return True

    def stop(self) -> None:
        FakeSpeaker.stops += 1


class FakeSearch:
    """Stands in for the web: records the queries and returns canned results (optionally after a gate)."""

    queries: list[str] = []
    gate: threading.Event | None = None
    outcome: SearchOutcome | None = None

    def search(self, text: str) -> SearchOutcome:
        if FakeSearch.gate is not None:
            FakeSearch.gate.wait(10)
        FakeSearch.queries.append(text)
        if FakeSearch.outcome is not None:
            return FakeSearch.outcome
        results = [
            WebResult(title="KeyError in Python", url="https://stackoverflow.com/q/1", snippet="Use dict.get."),
            WebResult(title="Dictionary", url="https://en.wikipedia.org/wiki/Dictionary", snippet=""),
        ]
        return SearchOutcome(query=text, results=results, providers=["Stack Overflow", "Wikipedia"])


class FakeRecorder:
    def __init__(self) -> None:
        self.recording = False

    def start_recording(self) -> None:
        self.recording = True

    def stop_recording(self) -> RecordingResult:
        self.recording = False
        return RecordingResult(wav_bytes=wav_bytes(1.0), sample_rate=16000, duration_ms=1000, raw_duration_ms=1000, gain_db=0.0, speech_rms_dbfs=-20.0)

    def cancel(self) -> None:
        self.recording = False

    def close(self) -> None:
        self.recording = False


@pytest.fixture
def make_controller(qapp: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    controllers: list[Any] = []

    def build(frames: list[Image.Image] | None = None, *, backend: str = "auto", window: Any = None) -> tuple[Any, FakeMss]:
        MemorySettings.store = {}
        FakeWorker.instances = []
        FakeSpeaker.spoken, FakeSpeaker.stops = [], 0
        FakeSearch.queries, FakeSearch.gate, FakeSearch.outcome = [], None, None
        FakeWorker.echo_sources = True
        FakeWorker.reply, FakeWorker.fail_with, FakeWorker.hold, FakeWorker.script = None, None, False, []
        fake = FakeMss(frames or [render_terminal(1280, 720)])
        monkeypatch.setattr(screen, "BLACK_FRAME_RETRY_DELAY_S", 0.0)
        real_capturer = screen.ScreenCapturer

        def capturer_factory() -> Any:
            capturer = real_capturer()
            capturer._sct = lambda: fake  # type: ignore[method-assign]
            return capturer

        monkeypatch.setattr(main, "ScreenCapturer", capturer_factory)
        monkeypatch.setattr(main, "QSettings", MemorySettings)
        monkeypatch.setattr(main, "InferenceWorker", FakeWorker)
        monkeypatch.setattr(main, "NodeSupervisor", FakeNode)
        monkeypatch.setattr(main, "Speaker", FakeSpeaker)
        monkeypatch.setattr(main, "WebSearch", FakeSearch)
        monkeypatch.setattr(main, "ActionRunner", lambda: ActionRunner(audit=AuditLog(tmp_path / "actions.log")))
        monkeypatch.setattr(
            main, "ConversationMemory", lambda enabled=True: ConversationMemory(tmp_path / "history.json", enabled=enabled)
        )
        monkeypatch.setattr(main, "AudioRecorder", FakeRecorder)
        monkeypatch.setattr(main, "probe", lambda: None)
        monkeypatch.setattr(main, "describe", lambda capability: "test pc")
        settings = main.ClientSettings(backend=backend, fallback_api_url=None)
        controller = main.OmniSightController(qapp, settings, enable_hotkeys=False, window=window)
        controllers.append(controller)
        return controller, fake

    yield build
    for controller in controllers:
        controller.shutdown()


def pump(qapp: Any):
    return lambda: qapp.processEvents()


# ---------------------------------------------------------------------------
# The window on its own
# ---------------------------------------------------------------------------


def result(question_model: str = "m") -> ClientResult:
    response = AnalyzeResponse.model_validate(analyze_response_json(model_id=question_model))
    return ClientResult(response=response, metrics=LatencyMetrics(tier="kaggle"))


def test_pressing_enter_sends_the_typed_question(qapp: Any) -> None:
    window = MainWindow()
    asked: list[str] = []
    window.ask_requested.connect(asked.append)
    window.ask_box.setText("   ")
    window.ask_box.returnPressed.emit()
    assert asked == []
    window.ask_box.setText("  why does this crash?  ")
    window.send_button.click()
    assert asked == ["why does this crash?"] and window.ask_box.text() == ""


def test_controls_are_disabled_while_busy_and_the_mic_can_stop_a_recording(qapp: Any) -> None:
    window = MainWindow()
    window.set_app_state(AppState.ANALYZING, "Analyzing the screen…")
    assert not window.send_button.isEnabled() and not window.capture_button.isEnabled() and not window.mic_button.isEnabled()
    assert not window.engine.isEnabled() and window.status.text() == "Analyzing the screen…"
    window.set_app_state(AppState.RECORDING_VOICE, "Listening…")
    assert window.mic_button.isEnabled() and window.mic_button.text() == "Stop and send"
    assert not window.send_button.isEnabled() and not window.ask_box.isEnabled()
    window.set_app_state(AppState.IDLE)
    assert window.send_button.isEnabled() and window.mic_button.text() == "Speak" and window.status.text() == "Ready."


def test_buttons_emit_their_signals(qapp: Any) -> None:
    window = MainWindow()
    seen: list[str] = []
    for name in ("capture_requested", "mic_clicked", "node_toggle_clicked", "clear_requested", "settings_requested"):
        getattr(window, name).connect(lambda name=name: seen.append(name))
    for button in (window.capture_button, window.mic_button, window.node_button, window.clear_button, window.settings_button):
        button.click()
    assert seen == ["capture_requested", "mic_clicked", "node_toggle_clicked", "clear_requested", "settings_requested"]
    picked: list[str] = []
    window.engine_selected.connect(picked.append)
    window.engine.activated.emit(window.engine.findData("local_cpu"))
    assert picked == ["local_cpu"]


def test_accessible_names_let_ui_automation_find_every_control(qapp: Any) -> None:
    window = MainWindow()
    names = {w.accessibleName() for w in (window.ask_box, window.send_button, window.capture_button, window.mic_button, window.engine, window.node_button, window.clear_button, window.settings_button)}
    assert names == {"Ask box", "Send", "Capture", "Microphone", "Engine", "Node", "Clear", "Settings"}


def test_answers_are_listed_capped_and_clearable(qapp: Any) -> None:
    window = MainWindow()
    assert window.exchange_count == 0 and not window.empty_label.isHidden()
    for index in range(MAX_EXCHANGES + 5):
        window.add_exchange(f"question {index}", result())
    assert window.exchange_count == MAX_EXCHANGES and window.empty_label.isHidden()
    window.clear_exchanges()
    assert window.exchange_count == 0 and not window.empty_label.isHidden()


def test_an_answer_shows_the_question_or_what_you_said(qapp: Any) -> None:
    window = MainWindow()
    window.add_exchange("why?", result())
    voice = result()
    voice.response.transcript = "what is wrong here"
    window.add_exchange("", voice)
    headings = [w.findChildren(type(window.status))[0].text() for w in window._exchanges]
    assert headings[0] == "why?" and headings[1] == "You said: “what is wrong here”"


def test_node_status_shows_the_device_that_is_actually_running(qapp: Any) -> None:
    window = MainWindow()
    window.set_node_status(NodeStatus(NodeState.STARTING, "Creating .venv-gpu", "cpu", owned=True), "local_cpu")
    assert "starting: Creating .venv-gpu" in window.status.text() and window.node_button.text() == "Stop local node"
    window.set_node_status(NodeStatus(NodeState.READY, "Ready.", "cuda", "CPU: Intel i9", owned=True), "local_gpu")
    assert window.status.text() == "Local node ready on CPU: Intel i9."  # what runs, not what was requested
    window.set_node_status(NodeStatus(NodeState.FAILED, "Another program is using the port"), "local_gpu")
    assert window.notice.text() == "Another program is using the port" and window.node_button.text() == "Start local node"
    window.set_node_status(NodeStatus(NodeState.STOPPED), "local_cpu")
    assert "not running" in window.status.text()
    window.set_node_status(NodeStatus(NodeState.STOPPED), "kaggle")
    assert window.status.text() == "Ready." and window.node_button.isHidden()


def test_closing_the_window_keeps_the_app_running(qapp: Any) -> None:
    window = MainWindow()
    window.show()
    assert window.close() is False or not window.isVisible()
    assert not window.isVisible()


# ---------------------------------------------------------------------------
# The controller driving the window
# ---------------------------------------------------------------------------


def test_a_typed_question_goes_through_the_whole_pipeline_into_the_window(qapp: Any, make_controller: Any) -> None:
    controller, fake = make_controller()
    controller.window.ask_box.setText("why does this crash?")
    controller.window.send_button.click()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    (worker,) = FakeWorker.instances
    assert worker.request.prompt == "why does this crash?" and worker.request.mode is AnalysisMode.EXPLAIN
    assert worker.request.audio is None and worker.request.client.kind == "desktop"
    assert fake.grabs >= 1  # the startup warm-up grab may also be counted
    assert wait_until(lambda: controller.state.state is AppState.IDLE, TIMEOUT_S, pump(qapp))
    assert not controller.hud.isVisible(), "an answer to a window question must not pop the HUD"
    assert controller.window.status.text() == "Ready."
    assert controller.state.latest() is not None


def test_the_capture_button_explains_the_screen_without_a_question(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.capture_button.click()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    request = FakeWorker.instances[0].request
    assert request.prompt == "" and request.mode is AnalysisMode.DEBUG


def test_hotkeys_still_use_the_hud_and_the_window_keeps_the_history(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.on_capture_requested()  # what Alt+C does
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    assert controller.hud.isVisible() and controller.state.state is AppState.DISPLAYING
    request = FakeWorker.instances[0].request
    assert request.prompt == "" and request.mode is AnalysisMode.DEBUG


def test_a_hotkey_after_a_window_question_does_not_reuse_its_prompt(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.on_window_ask("first question")
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    assert wait_until(lambda: controller.state.state is AppState.IDLE, TIMEOUT_S, pump(qapp))
    controller.on_capture_requested()
    assert wait_until(lambda: controller.window.exchange_count == 2, TIMEOUT_S, pump(qapp))
    assert [w.request.prompt for w in FakeWorker.instances] == ["first question", ""]


def test_capture_uses_the_users_window_not_omnisights_own(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.foreground import ForegroundTracker

    controller, _ = make_controller()
    current = {"info": WindowInfo(hwnd=1, pid=200, center=(4000, 300))}  # the user's editor on a second monitor
    controller.foreground = ForegroundTracker(own_pid=100, probe=lambda: current["info"])
    controller.foreground.poll()
    current["info"] = WindowInfo(hwnd=2, pid=100, center=(100, 100))  # the user clicks OmniSight's window
    points: list[Any] = []
    original = controller.capturer.capture
    monkeypatch.setattr(controller.capturer, "capture", lambda monitor_index=None, point=None: (points.append(point), original(point=point))[1])
    controller.window.capture_button.click()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    assert points == [(4000, 300)]


def test_click_to_talk_records_then_sends_the_typed_text_with_the_voice(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.ask_box.setText("focus on the second line")
    controller.window.mic_button.click()
    assert controller.state.state is AppState.RECORDING_VOICE and controller.recorder.recording
    assert controller.window.mic_button.text() == "Stop and send" and controller.window.ask_box.text() == ""
    controller.window.mic_button.click()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    request = FakeWorker.instances[0].request
    assert request.mode is AnalysisMode.VOICE_QUERY and request.audio is not None and request.audio.duration_ms == 1000
    assert request.prompt == "focus on the second line"
    assert not controller.hud.isVisible()


def test_a_black_screen_becomes_a_notice_in_the_window_not_a_hud_popup(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller([Image.new("RGB", (1280, 720), (0, 0, 0))])
    controller.window.capture_button.click()
    assert wait_until(lambda: controller.state.state is AppState.ERROR, TIMEOUT_S, pump(qapp))
    assert "looks black" in controller.window.notice.text() and not controller.window.notice.isHidden()
    assert not controller.hud.isVisible() and FakeWorker.instances == []
    assert controller.window.send_button.isEnabled(), "the window recovers after an error"


def test_a_hotkey_error_still_shows_in_the_hud(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller([Image.new("RGB", (1280, 720), (0, 0, 0))])
    controller.on_capture_requested()
    assert wait_until(lambda: controller.state.state is AppState.ERROR, TIMEOUT_S, pump(qapp))
    assert controller.hud.isVisible() and "looks black" in controller.window.notice.text()


def test_requests_are_ignored_while_busy(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.on_window_ask("one")
    controller.on_window_ask("two")
    controller.on_window_capture()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    assert len(FakeWorker.instances) == 1 and FakeWorker.instances[0].request.prompt == "one"


def test_clear_empties_the_window_and_the_history(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.on_window_capture()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    controller.window.clear_button.click()
    assert controller.window.exchange_count == 0 and controller.state.latest() is None


def test_the_engine_picker_switches_and_remembers_the_backend_and_device(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.engine.activated.emit(controller.window.engine.findData("local_cpu"))
    assert (controller.settings.backend, controller.settings.local_device) == ("local", "cpu")
    assert MemorySettings.store == {"backend": "local", "local_device": "cpu"}
    assert controller.window.engine.currentData() == "local_cpu"
    assert controller._backend_actions["local"].isChecked()
    controller.set_engine("kaggle")
    assert controller.settings.backend == "kaggle" and MemorySettings.store["backend"] == "kaggle"
    assert controller.window.node_button.isHidden()


def test_tray_backend_choices_keep_the_window_in_sync(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.set_backend("kaggle")
    assert controller.window.engine.currentData() == "kaggle"
    controller.set_backend("local")
    assert controller.window.engine.currentData() == "local_auto"


def test_the_window_starts_and_stops_the_local_node_on_the_chosen_device(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller(backend="local")
    controller.set_engine("local_gpu")
    controller.window.node_button.click()
    assert controller.node.calls == [("start", "cuda")]
    assert controller.window.node_button.text() == "Stop local node"
    assert "starting" in controller.window.status.text()
    controller.window.node_button.click()
    assert controller.node.calls[-1] == ("stop",) and controller.window.node_button.text() == "Start local node"


def test_switching_device_restarts_the_node_the_app_started(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller(backend="local")
    controller.set_engine("local_cpu")
    controller.toggle_node()
    controller.set_engine("local_gpu")
    assert controller.node.calls == [("start", "cpu"), ("start", "cuda")]
    controller.set_engine("kaggle")  # leaving This-PC modes never touches the node
    assert controller.node.calls == [("start", "cpu"), ("start", "cuda")]


def test_a_node_error_reaches_the_window(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller(backend="local")
    controller.node.status = NodeStatus(NodeState.FAILED, "Another program is using http://127.0.0.1:8000.")
    controller._last_node_status = None
    controller._refresh_node()
    assert "Another program is using" in controller.window.notice.text()


def test_shutdown_stops_the_node_and_hides_the_window(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.open_window()
    assert controller.window.isVisible()
    controller.shutdown()
    assert ("shutdown",) in controller.node.calls and not controller.window.isVisible()


def test_settings_dialog_lists_every_engine(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    dialog = main.SettingsDialog(controller)
    assert [dialog.backend.itemData(i) for i in range(dialog.backend.count())] == ["auto", "kaggle", "local_auto", "local_gpu", "local_cpu"]
    dialog.backend.setCurrentIndex(dialog.backend.findData("local_gpu"))
    dialog.apply()
    assert controller.settings.engine_choice == "local_gpu"


# ---------------------------------------------------------------------------
# Memory, chat and spoken answers
# ---------------------------------------------------------------------------


def ask(controller: Any, qapp: Any, text: str, answers: int) -> None:
    controller.window.ask_box.setText(text)
    controller.window.send_button.click()
    assert wait_until(lambda: controller.window.exchange_count == answers, TIMEOUT_S, pump(qapp))
    assert wait_until(lambda: controller.state.state is AppState.IDLE, TIMEOUT_S, pump(qapp))


def test_a_follow_up_carries_the_earlier_exchange_as_history(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    ask(controller, qapp, "why does this crash?", 1)
    assert FakeWorker.instances[0].request.history == []
    ask(controller, qapp, "and how do I fix it?", 2)
    history = FakeWorker.instances[1].request.history
    assert [(turn.role, turn.text) for turn in history[:1]] == [("user", "why does this crash?")]
    assert history[1].role == "assistant" and history[1].text


def test_turning_memory_off_stops_sending_and_forgets(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    ask(controller, qapp, "first", 1)
    controller.window.memory_check.setChecked(False)
    assert not controller.memory.enabled and MemorySettings.store["memory_enabled"] is False
    ask(controller, qapp, "second", 2)
    assert FakeWorker.instances[1].request.history == []
    assert len(controller.memory) == 0
    controller.window.memory_check.setChecked(True)
    ask(controller, qapp, "third", 3)
    ask(controller, qapp, "fourth", 4)
    assert [t.text for t in FakeWorker.instances[3].request.history][:1] == ["third"]


def test_clear_also_forgets_the_conversation(qapp: Any, make_controller: Any, tmp_path: Path) -> None:
    controller, _ = make_controller()
    ask(controller, qapp, "remember me", 1)
    assert (tmp_path / "history.json").exists()
    controller.window.clear_button.click()
    assert len(controller.memory) == 0 and not (tmp_path / "history.json").exists()
    ask(controller, qapp, "next", 1)
    assert FakeWorker.instances[-1].request.history == []


def test_hotkey_questions_are_remembered_too(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.on_capture_requested()
    assert wait_until(lambda: len(controller.memory) == 2, TIMEOUT_S, pump(qapp))
    assert controller.memory.history()[0].text == "Find the problem on my screen."


def test_chat_without_the_screen_captures_nothing_and_sends_no_image(qapp: Any, make_controller: Any) -> None:
    controller, fake = make_controller()
    assert wait_until(lambda: not controller.pool.activeThreadCount(), TIMEOUT_S, pump(qapp))  # warm-up done
    grabs = fake.grabs
    controller.window.screen_check.setChecked(False)
    assert "no screenshot" in controller.window.ask_box.placeholderText()
    ask(controller, qapp, "what is a generator?", 1)
    request = FakeWorker.instances[0].request
    assert request.mode is AnalysisMode.CHAT and request.image is None and request.prompt == "what is a generator?"
    assert fake.grabs == grabs
    ask(controller, qapp, "show an example", 2)
    assert FakeWorker.instances[1].request.history[0].text == "what is a generator?"
    controller.window.screen_check.setChecked(True)
    assert "your screen" in controller.window.ask_box.placeholderText()
    ask(controller, qapp, "and this screen?", 3)
    assert FakeWorker.instances[2].request.image is not None  # memory is shared with screen questions


def test_chat_is_ignored_while_busy_and_a_bad_request_becomes_an_error(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.screen_check.setChecked(False)
    controller.state.transition(AppState.CAPTURING)
    controller._begin_chat("hello")
    assert not FakeWorker.instances
    controller.state.transition(AppState.IDLE)
    controller._begin_chat("x" * 4001)  # over the contract's prompt limit
    assert "Could not build the request" in (controller.state.last_error or "")
    assert not FakeWorker.instances


def test_answers_are_spoken_only_when_the_switch_is_on(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    ask(controller, qapp, "quiet", 1)
    assert FakeSpeaker.spoken == []
    controller.window.speak_check.setChecked(True)
    assert MemorySettings.store["speak_enabled"] is True
    ask(controller, qapp, "loud", 2)
    assert len(FakeSpeaker.spoken) == 1 and FakeSpeaker.spoken[0] == controller.state.latest().response.summary
    stops = FakeSpeaker.stops
    controller.window.speak_check.setChecked(False)
    assert FakeSpeaker.stops > stops  # turning it off cuts the voice off


def test_a_new_question_or_escape_stops_the_voice(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    base = FakeSpeaker.stops
    controller.on_window_capture()
    assert FakeSpeaker.stops > base
    assert wait_until(lambda: controller.state.state is AppState.IDLE, TIMEOUT_S, pump(qapp))
    base = FakeSpeaker.stops
    controller.on_dismiss()
    assert FakeSpeaker.stops == base + 1
    controller.window.stop_speaking_button.click()
    assert FakeSpeaker.stops == base + 2


def test_window_options_can_be_set_without_echoing_and_the_stop_button_follows_the_voice(qapp: Any) -> None:
    window = MainWindow()
    window.set_memory_enabled(False)
    window.set_speak_enabled(True)
    assert not window.memory_check.isChecked() and window.speak_check.isChecked()
    window.set_speaking(True)
    assert not window.stop_speaking_button.isHidden()
    window.set_speaking(False)
    assert window.stop_speaking_button.isHidden()
    window.set_speak_available(False)
    assert not window.speak_check.isEnabled()
    toggled: list[bool] = []
    window.memory_toggled.connect(toggled.append)
    window.set_memory_enabled(True)  # programmatic changes do not echo back as user toggles
    assert toggled == []
    window.memory_check.setChecked(False)
    assert toggled == [False]


def test_the_new_controls_have_accessible_names_and_lock_while_busy(qapp: Any) -> None:
    window = MainWindow()
    boxes = (window.screen_check, window.memory_check, window.speak_check, window.stop_speaking_button)
    assert {w.accessibleName() for w in boxes} == {"Include screen", "Remember", "Speak", "Stop voice"}
    window.set_app_state(AppState.ANALYZING)
    assert not window.screen_check.isEnabled()
    window.set_app_state(AppState.IDLE)
    assert window.screen_check.isEnabled()


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


def labels(widget: Any) -> list[str]:
    from PyQt6.QtWidgets import QLabel

    return [label.text() for label in widget.findChildren(QLabel)]


def test_search_is_off_by_default_and_nothing_is_searched(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    assert not controller.window.search_check.isChecked() and not controller.window.smart_check.isEnabled()
    ask(controller, qapp, "why does this crash?", 1)
    assert FakeSearch.queries == []
    request = FakeWorker.instances[0].request
    assert request.web_results == [] and request.web_search is False


def test_a_typed_screen_question_is_searched_and_the_results_ride_with_the_request(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    assert MemorySettings.store["web_search"] is True and controller.window.smart_check.isEnabled()
    ask(controller, qapp, "why does this crash?", 1)
    assert FakeSearch.queries == ["why does this crash?"]
    request = FakeWorker.instances[0].request
    assert [r.title for r in request.web_results] == ["KeyError in Python", "Dictionary"]
    assert request.web_search is False and request.mode is AnalysisMode.EXPLAIN and request.image is not None
    card = labels(controller.window._exchanges[0])
    assert any("Sources (searched: why does this crash?)" in text and "stackoverflow.com/q/1" in text for text in card)
    assert any(text.endswith("  ·  ".join(["fake/engine via local", "first token 1.5s", "confidence 87%", "web"])) or "web" in text.split("  ·  ") for text in card)


def test_chat_is_searched_too(qapp: Any, make_controller: Any) -> None:
    controller, fake = make_controller()
    controller.window.search_check.setChecked(True)
    controller.window.screen_check.setChecked(False)
    ask(controller, qapp, "what is a generator?", 1)
    request = FakeWorker.instances[0].request
    assert request.mode is AnalysisMode.CHAT and request.image is None and len(request.web_results) == 2
    assert FakeSearch.queries == ["what is a generator?"]


def test_hotkeys_and_the_capture_button_have_no_typed_question_so_nothing_is_searched(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    controller.on_capture_requested()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    request = FakeWorker.instances[0].request
    assert FakeSearch.queries == [] and request.web_results == [] and request.web_search is False


def test_a_spoken_question_is_not_searched_and_does_not_let_the_cloud_write_searches_by_default(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)  # Smart query stays off
    controller.on_voice_pressed()
    controller.on_voice_released()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    request = FakeWorker.instances[0].request
    assert FakeSearch.queries == [] and request.web_results == [] and request.web_search is False


def test_with_smart_query_on_a_spoken_question_lets_the_cloud_tier_ground_itself(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    controller.window.smart_check.setChecked(True)
    controller.on_voice_pressed()
    controller.on_voice_released()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    request = FakeWorker.instances[0].request
    assert FakeSearch.queries == [] and request.web_search is True


def test_a_typed_note_with_voice_is_the_query(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    controller.on_voice_pressed(origin="window", prompt="python keyerror")
    controller.on_voice_released()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    assert FakeSearch.queries == ["python keyerror"] and len(FakeWorker.instances[0].request.web_results) == 2


def test_a_search_that_finds_nothing_warns_and_asks_for_no_cloud_search_by_default(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    FakeSearch.outcome = SearchOutcome(query="q", notice="Web search is unavailable right now. Answering without it.")
    controller.window.search_check.setChecked(True)  # Smart query off: the cloud tier must not search on its own
    ask(controller, qapp, "why?", 1)
    request = FakeWorker.instances[0].request
    assert request.web_results == [] and request.web_search is False
    assert any("unavailable" in t for t in labels(controller.window._exchanges[0]))


def test_with_smart_query_on_a_failed_search_lets_the_cloud_tier_try(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = make_controller()
    FakeSearch.outcome = SearchOutcome(query="q", notice="Web search is unavailable right now. Answering without it.")
    monkeypatch.setattr(controller, "_ask_engine", lambda prompt: "keywords")
    controller.window.search_check.setChecked(True)
    controller.window.smart_check.setChecked(True)
    ask(controller, qapp, "why?", 1)
    assert FakeWorker.instances[0].request.web_search is True


def test_the_search_tooltips_are_honest_about_what_the_cloud_tier_may_see(qapp: Any) -> None:
    window = MainWindow()
    assert "not used as a query" in window.search_check.toolTip() and "Smart query" in window.search_check.toolTip()
    smart = window.smart_check.toolTip()
    assert "screen" in smart and "voice" in smart and "Google searches" in smart


def test_a_chat_sent_before_the_previous_worker_retired_is_not_dropped(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.screen_check.setChecked(False)
    ask(controller, qapp, "first", 1)
    controller._worker = FakeWorker(None, None, FakeWorker.instances[0].request)  # the old worker has not retired yet
    controller.window.ask_box.setText("second")
    controller.window.send_button.click()
    assert wait_until(lambda: controller.window.exchange_count == 2, TIMEOUT_S, pump(qapp))
    assert controller.state.state is AppState.IDLE


def test_the_rewrite_call_is_bounded_and_never_uses_the_web_tier(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = make_controller()
    seen: list[Any] = []

    class FakeClient:
        def __init__(self, settings: Any, resolver: Any) -> None:
            seen.append(settings)

        def analyze(self, request: Any) -> Any:
            response = AnalyzeResponse.model_validate({**analyze_response_json(), "markdown": "python keyerror"})
            return ClientResult(response=response, metrics=LatencyMetrics())

    monkeypatch.setattr(main, "InferenceClient", FakeClient)
    assert controller._ask_engine("rewrite this") == "python keyerror"
    (settings,) = seen
    assert settings.fallback_api_url is None and settings.retries == 0
    assert settings.request_deadline_s <= 20 and settings.read_timeout_s <= 15 and settings.local_timeout_s <= 20


def test_the_request_waits_for_a_slow_search_and_then_goes_out(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    FakeSearch.gate = threading.Event()
    controller.window.ask_box.setText("slow question")
    controller.window.send_button.click()
    assert wait_until(lambda: controller._held_capture is not None, TIMEOUT_S, pump(qapp))  # capture done, search not
    assert FakeWorker.instances == [] and controller.window.status.text() == "Searching the web…"
    FakeSearch.gate.set()
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))
    assert len(FakeWorker.instances[0].request.web_results) == 2


def test_a_stale_search_result_is_ignored(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    ask(controller, qapp, "first", 1)
    stale = controller._search_token - 1
    controller._search_outcome = None
    controller._on_search_done(stale, SearchOutcome(query="old", results=[]))
    assert controller._search_outcome is None


def test_an_engine_that_ignores_the_results_is_reported(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    FakeWorker.echo_sources = False
    ask(controller, qapp, "why?", 1)
    assert any("could not use the web results" in t for t in labels(controller.window._exchanges[0]))
    assert not any("Sources" in text for text in labels(controller.window._exchanges[0]))


def test_smart_query_rewrites_the_question_and_the_rewrite_is_what_is_searched(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = make_controller()
    asked: list[Any] = []

    class FakeClient:
        def __init__(self, settings: Any, resolver: Any) -> None:
            asked.append(settings)

        def analyze(self, request: Any) -> Any:
            asked.append(request)
            response = AnalyzeResponse.model_validate({**analyze_response_json(), "markdown": '"python keyerror dict get"\nextra words'})
            return ClientResult(response=response, metrics=LatencyMetrics())

    monkeypatch.setattr(main, "InferenceClient", FakeClient)
    controller.window.search_check.setChecked(True)
    controller.window.smart_check.setChecked(True)
    assert MemorySettings.store["smart_query"] is True
    ask(controller, qapp, "why does my thing break when the key is missing???", 1)
    assert FakeSearch.queries == ["python keyerror dict get"]
    settings, request = asked
    assert settings.fallback_api_url is None  # the rewrite never goes to the web tier
    assert request.mode is AnalysisMode.CHAT and request.image is None and "why does my thing break" in request.prompt


def test_smart_query_failure_falls_back_to_the_typed_question(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = make_controller()

    class Broken:
        def __init__(self, settings: Any, resolver: Any) -> None:
            pass

        def analyze(self, request: Any) -> Any:
            raise RuntimeError("no node")

    monkeypatch.setattr(main, "InferenceClient", Broken)
    controller.window.search_check.setChecked(True)
    controller.window.smart_check.setChecked(True)
    ask(controller, qapp, "typed question", 1)
    assert FakeSearch.queries == ["typed question"]


def test_turning_search_off_turns_smart_query_off_and_the_settings_persist(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    controller.window.smart_check.setChecked(True)
    controller.window.search_check.setChecked(False)
    assert not controller.window.smart_check.isChecked() and not controller.window.smart_check.isEnabled()
    assert MemorySettings.store["web_search"] is False and MemorySettings.store["smart_query"] is False
    assert not controller._smart_enabled and not controller._search_enabled


def test_the_search_boxes_follow_the_busy_state_and_have_accessible_names(qapp: Any) -> None:
    window = MainWindow()
    assert {window.search_check.accessibleName(), window.smart_check.accessibleName()} == {"Search web", "Smart query"}
    window.set_search_enabled(True)
    assert window.search_check.isChecked() and window.smart_check.isEnabled()
    window.set_smart_enabled(True)
    assert window.smart_check.isChecked()
    window.set_app_state(AppState.ANALYZING)
    assert not window.search_check.isEnabled()
    window.set_app_state(AppState.IDLE)
    assert window.search_check.isEnabled()
    window.set_progress("Searching the web…")
    assert window.status.text() == "Searching the web…"


def test_source_links_are_escaped_so_a_hostile_title_cannot_inject_markup(qapp: Any) -> None:
    hostile = WebResult(title='<img src=x onerror="alert(1)"> & more', url="https://example.com/a?x=1&y=\"2\"", snippet="")
    response = AnalyzeResponse.model_validate({**analyze_response_json(), "sources": [hostile.model_dump()]})
    window = MainWindow()
    window.add_exchange("q", ClientResult(response=response, metrics=LatencyMetrics(tier="local")), "a <b>query</b>")
    text = next(t for t in labels(window._exchanges[0]) if t.startswith("Sources"))
    assert "<img" not in text and "&lt;img" in text and "a &lt;b&gt;query&lt;/b&gt;" in text
    assert 'href="https://example.com/a?x=1&amp;y=&quot;2&quot;"' in text


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------


class Tray:
    """Records tray notifications instead of showing them."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str, *args: Any) -> None:
        self.messages.append((title, message))


def error_screen() -> Image.Image:
    from PIL import ImageDraw

    from tests.support import monospace_font

    image = render_terminal(1280, 720)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 480, 1240, 700), fill=(130, 20, 20))
    for i in range(6):
        draw.text((60, 495 + i * 30), "Traceback (most recent call last): KeyError: 'discount_rate'", font=monospace_font(18), fill=(255, 255, 255))
    return image


def other_screen(font_px: int = 22) -> Image.Image:
    return render_terminal(1280, 720, font_px=font_px)


def show(fake: Any, image: Image.Image) -> None:
    """The next grab returns ``image`` (the start-up warm-up already used the first scripted frame)."""
    from tests.support import FakeShot

    fake._shots = [FakeShot(image)]
    fake.grabs = 0


@pytest.fixture
def watching(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch):
    """A controller on a local engine with a hand-driven clock and a recording tray."""

    def build(backend: str = "local") -> tuple[Any, Any, ManualClock, Tray]:
        controller, fake = make_controller(backend=backend)
        clock = ManualClock()
        controller.watch._clock = clock
        tray = Tray()
        monkeypatch.setattr(controller.tray, "showMessage", tray)
        wait_until(lambda: not controller.pool.activeThreadCount(), TIMEOUT_S, pump(qapp))  # warm-up grab done
        return controller, fake, clock, tray

    return build


def tick(controller: Any, qapp: Any, clock: ManualClock) -> None:
    """Advance past the interval, start one tick and wait until it (all its steps) has finished."""
    clock.advance(11.0)
    controller._watch_tick()
    assert wait_until(lambda: not controller.pool.activeThreadCount(), TIMEOUT_S, pump(qapp))
    assert wait_until(lambda: not controller.watch.in_flight, TIMEOUT_S, pump(qapp)), "the tick never finished"


def test_watching_is_off_at_start_up_and_nothing_happens_on_its_own(qapp: Any, watching: Any) -> None:
    controller, fake, clock, tray = watching()
    assert not controller.watch.running and not controller.window.watch_check.isChecked()
    grabs = fake.grabs
    for _ in range(3):
        clock.advance(60.0)
        controller._watch_tick()
    assert fake.grabs == grabs and not FakeWorker.instances and tray.messages == []
    assert not controller.window.watch_label.isVisible() and not controller._watch_action.isEnabled()


@pytest.mark.parametrize("backend", ["auto", "kaggle"])
def test_watching_is_refused_on_an_engine_that_is_not_local(qapp: Any, watching: Any, backend: str) -> None:
    controller, fake, clock, _ = watching(backend)
    controller.window.watch_check.setChecked(True)
    assert not controller.watch.running and not controller.window.watch_check.isChecked()
    assert "local engine" in controller.window.notice.text() and "never leaves this PC" in controller.window.notice.text()
    clock.advance(60.0)
    controller._watch_tick()
    assert not FakeWorker.instances


def test_a_changed_screen_gets_the_fixed_yes_no_question_from_the_local_node_only(qapp: Any, watching: Any) -> None:
    from core.watch import CHECK_PROMPT

    controller, fake, clock, tray = watching()
    FakeWorker.script = ["NO"]
    controller.window.watch_check.setChecked(True)
    assert controller.watch.running and "Watching every 10 s" in controller.window.watch_label.text()
    assert "frames stay on this PC" in controller.window.watch_label.text() and not controller.watch.paused
    tick(controller, qapp, clock)
    (worker,) = FakeWorker.instances
    request = worker.request
    assert request.mode is AnalysisMode.EXPLAIN and request.prompt == CHECK_PROMPT and request.image is not None
    assert request.max_new_tokens == 16 and request.history == [] and request.web_results == [] and request.web_search is False
    assert worker.settings.backend == "local" and worker.settings.fallback_api_url is None
    assert controller.state.state is AppState.IDLE  # a background check does not look like the user's own request
    assert tray.messages == [] and controller.window.exchange_count == 0 and controller._watch_image is None


def test_the_worker_is_forced_local_even_if_the_window_says_auto(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    FakeWorker.script = ["NO"]
    controller.window.watch_check.setChecked(True)
    controller.settings = controller.settings.with_backend("auto")  # a race: the engine changed, watching not stopped yet
    tick(controller, qapp, clock)
    assert FakeWorker.instances and FakeWorker.instances[0].settings.backend == "local"
    assert FakeWorker.instances[0].settings.fallback_api_url is None


def test_an_unchanged_screen_costs_no_model_call(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    FakeWorker.script = ["NO"]
    screen = render_terminal(1280, 720)
    show(fake, screen)
    controller.window.watch_check.setChecked(True)
    tick(controller, qapp, clock)
    assert len(FakeWorker.instances) == 1
    for _ in range(4):
        show(fake, screen)
        tick(controller, qapp, clock)
    assert len(FakeWorker.instances) == 1  # four more looks, no more model calls


def test_a_plain_colour_screen_is_never_sent_to_the_model(qapp: Any, watching: Any) -> None:
    controller, fake, clock, tray = watching()
    show(fake, Image.new("RGB", (1280, 720), (30, 60, 100)))  # a blank blue desktop (the model says YES to these)
    controller.window.watch_check.setChecked(True)
    tick(controller, qapp, clock)
    assert not FakeWorker.instances and tray.messages == [] and controller.watch.failures == 0


def test_a_new_error_takes_two_steps_and_is_reported_once_and_not_stored(qapp: Any, watching: Any) -> None:
    from core.watch import DESCRIBE_PROMPT

    controller, fake, clock, tray = watching()
    FakeWorker.script = ["YES", "A KeyError for 'discount_rate' is shown in a traceback."]
    controller.window.watch_check.setChecked(True)
    show(fake, error_screen())
    tick(controller, qapp, clock)
    check, describe = FakeWorker.instances
    assert describe.request.prompt == DESCRIBE_PROMPT and describe.request.image is not None and describe.request.max_new_tokens == 40
    assert describe.settings.backend == "local"
    assert tray.messages == [("OmniSight noticed something", "Possible error on your screen: A KeyError for 'discount_rate' is shown in a traceback.")]
    assert controller.window.exchange_count == 1
    assert any("Noticed while watching" in text for text in labels(controller.window._exchanges[0]))
    # the screen changes but the same error is still showing: a YES costs one call and no new alert
    FakeWorker.script = ["YES"]
    show(fake, other_screen())
    tick(controller, qapp, clock)
    assert len(FakeWorker.instances) == 3 and len(tray.messages) == 1
    # private by construction: no conversation memory, no answer history, no speech, no kept frame
    assert len(controller.memory) == 0 and controller.state.latest() is None and FakeSpeaker.spoken == []
    assert controller._watch_image is None


def test_a_cleared_screen_rearms_the_same_finding(qapp: Any, watching: Any) -> None:
    controller, fake, clock, tray = watching()
    controller.window.watch_check.setChecked(True)
    steps = (
        (["YES", "Build failed"], error_screen()),
        (["NO"], render_terminal(1280, 720)),
        (["YES", "Build failed"], error_screen()),
    )
    for replies, screen in steps:
        FakeWorker.script = list(replies)
        show(fake, screen)
        tick(controller, qapp, clock)
    assert [m[1] for m in tray.messages] == ["Possible error on your screen: Build failed"] * 2


def test_an_empty_description_still_raises_the_alert(qapp: Any, watching: Any) -> None:
    from core.watch import GENERIC_FINDING

    controller, fake, clock, tray = watching()
    FakeWorker.script = ["Yes.", ""]
    controller.window.watch_check.setChecked(True)
    show(fake, error_screen())
    tick(controller, qapp, clock)
    assert [m[1] for m in tray.messages] == [GENERIC_FINDING]


@pytest.mark.parametrize("reply", ["NO", "No.", "maybe", "", "I cannot tell"])
def test_anything_but_yes_means_no_alert(qapp: Any, watching: Any, reply: str) -> None:
    controller, fake, clock, tray = watching()
    FakeWorker.script = [reply]
    controller.window.watch_check.setChecked(True)
    tick(controller, qapp, clock)
    assert len(FakeWorker.instances) == 1 and tray.messages == []


def test_clicking_the_tray_message_opens_the_window(qapp: Any, watching: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, fake, clock, tray = watching()
    opened: list[bool] = []
    monkeypatch.setattr(controller, "open_window", lambda: opened.append(True))
    FakeWorker.script = ["YES", "Build failed"]
    controller.window.watch_check.setChecked(True)
    show(fake, error_screen())
    tick(controller, qapp, clock)
    controller._on_tray_message_clicked()
    assert opened == [True] and controller._watch_notice_pending is False


def test_pause_and_resume_from_the_window_and_the_tray(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    FakeWorker.script = ["NO"]
    controller.window.watch_check.setChecked(True)
    assert controller._watch_action.isEnabled() and controller._watch_action.text() == "Pause watching"
    assert "WATCHING" in controller.tray.toolTip()
    controller.window.watch_pause_button.click()
    assert controller.watch.paused and controller.window.watch_label.text() == "Paused"
    assert controller.window.watch_pause_button.text() == "Resume watching" and "PAUSED" in controller.tray.toolTip()
    assert controller._watch_action.text() == "Resume watching"
    grabs = fake.grabs
    clock.advance(120.0)
    controller._watch_tick()
    assert fake.grabs == grabs  # paused: no capture at all
    controller._watch_action.trigger()
    assert not controller.watch.paused and "Watching every" in controller.window.watch_label.text()


def test_turning_watching_off_stops_everything(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    controller.window.watch_check.setChecked(False)
    assert not controller.watch.running and not controller.window.watch_label.isVisible()
    assert not controller.window.watch_pause_button.isVisible() and not controller._watch_action.isEnabled()
    assert "WATCHING" not in controller.tray.toolTip()


def test_switching_to_a_non_local_engine_stops_watching(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    controller.set_engine("kaggle")
    assert not controller.watch.running and not controller.window.watch_check.isChecked()
    assert "only runs on a local engine" in controller.window.notice.text()


def test_switching_between_local_engines_keeps_watching_but_auto_stops_it(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    controller.set_engine("local_gpu")
    assert controller.watch.running
    controller.set_engine("local_cpu")
    assert controller.watch.running
    controller.set_backend("auto")
    assert not controller.watch.running


def test_the_app_being_busy_defers_the_tick(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    FakeWorker.script = ["NO"]
    controller.window.watch_check.setChecked(True)
    controller.state.transition(AppState.CAPTURING)
    grabs = fake.grabs
    clock.advance(60.0)
    controller._watch_tick()
    assert fake.grabs == grabs and not FakeWorker.instances
    controller.state.transition(AppState.IDLE)
    controller._watch_tick()
    assert wait_until(lambda: fake.grabs > grabs, TIMEOUT_S, pump(qapp))


def test_a_user_question_cancels_a_watch_request_that_is_still_running(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    FakeWorker.hold = True
    controller.window.watch_check.setChecked(True)
    clock.advance(11.0)
    controller._watch_tick()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    watch_worker = FakeWorker.instances[0]
    assert controller.watch.in_flight
    FakeWorker.hold = False
    controller.on_window_capture()  # the user asks something
    assert watch_worker.cancelled
    watch_worker.finished.emit()  # the cancelled worker ends without an answer
    assert wait_until(lambda: not controller.watch.in_flight, TIMEOUT_S, pump(qapp))
    assert wait_until(lambda: controller.window.exchange_count == 1, TIMEOUT_S, pump(qapp))  # the user's answer arrived
    assert controller._watch_image is None


def test_a_yes_that_arrives_while_the_user_is_busy_is_not_described_and_is_looked_at_again(qapp: Any, watching: Any) -> None:
    controller, fake, clock, tray = watching()
    FakeWorker.hold = True
    FakeWorker.script = ["YES", "Build failed"]
    controller.window.watch_check.setChecked(True)
    show(fake, error_screen())
    clock.advance(11.0)
    controller._watch_tick()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    controller.state.transition(AppState.CAPTURING)  # the user starts something before the answer arrives
    FakeWorker.instances[0].release()
    qapp.processEvents()
    assert len(FakeWorker.instances) == 1 and tray.messages == [] and not controller.watch.in_flight
    assert controller._watch_signature is None  # this frame was not marked seen, so the next tick checks it again
    controller.state.transition(AppState.IDLE)
    FakeWorker.hold = False
    FakeWorker.script = ["YES", "Build failed"]  # the second look: yes, then the description
    show(fake, error_screen())
    tick(controller, qapp, clock)
    assert [m[1] for m in tray.messages] == ["Possible error on your screen: Build failed"]


def test_a_watch_answer_that_arrives_after_stopping_is_ignored(qapp: Any, watching: Any) -> None:
    controller, fake, clock, tray = watching()
    FakeWorker.hold = True
    controller.window.watch_check.setChecked(True)
    clock.advance(11.0)
    controller._watch_tick()
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    controller.window.watch_check.setChecked(False)
    FakeWorker.instances[0].release("YES")
    qapp.processEvents()
    assert tray.messages == [] and controller.window.exchange_count == 0 and len(FakeWorker.instances) == 1


def test_repeated_node_failures_back_off_and_then_stop_watching(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    FakeWorker.fail_with = "every inference endpoint failed"
    controller.window.watch_check.setChecked(True)
    for expected, font in ((20.0, 20), (40.0, 24)):
        show(fake, other_screen(font))  # a new frame each time
        tick(controller, qapp, clock)
        assert controller.watch.running and controller.watch.current_interval_s == expected
        clock.advance(expected)
    show(fake, other_screen(28))
    tick(controller, qapp, clock)
    assert not controller.watch.running and not controller.window.watch_check.isChecked()
    assert "local node is not answering" in controller.window.notice.text() and controller._watch_image is None


def test_a_failed_capture_is_skipped_without_counting_as_a_failure(qapp: Any, watching: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    monkeypatch.setattr(controller.capturer, "capture", lambda monitor_index=None, point=None: (_ for _ in ()).throw(RuntimeError("grab failed")))
    tick(controller, qapp, clock)
    assert controller.watch.running and controller.watch.failures == 0 and not FakeWorker.instances


def test_an_undecodable_frame_counts_as_a_failure_not_a_crash(qapp: Any, watching: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    monkeypatch.setattr(main, "frame_signature", lambda b64: (_ for _ in ()).throw(ValueError("corrupt")))
    tick(controller, qapp, clock)
    assert controller.watch.failures == 1 and controller.watch.running and not FakeWorker.instances


def test_shutdown_stops_watching(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    controller.shutdown()
    assert not controller.watch.running and not controller._watch_timer.isActive()


def test_the_watch_controls_have_accessible_names(qapp: Any) -> None:
    window = MainWindow()
    assert window.watch_check.accessibleName() == "Watch screen"
    assert "THIS PC" in window.watch_check.toolTip() and "Nothing leaves this PC" in window.watch_check.toolTip()
    window.set_watch_status("Watching every 10 s", paused=False)
    assert window.watch_pause_button.accessibleName() == "Pause watching" and not window.watch_pause_button.isHidden()
    window.set_watch_status("Paused", paused=True)
    assert window.watch_pause_button.accessibleName() == "Resume watching"
    window.set_watch_status("")
    assert window.watch_pause_button.isHidden() and window.watch_label.isHidden()
    toggles: list[bool] = []
    window.watch_toggled.connect(toggles.append)
    window.set_watch_enabled(True)  # programmatic: no echo
    assert toggles == [] and window.watch_check.isChecked()
    window.watch_check.setChecked(False)
    assert toggles == [False]


@pytest.mark.parametrize("url", ["http://192.168.1.50:8000", "http://my-gpu-box.lan:8000", "https://example.com"])
def test_watching_is_refused_when_the_local_node_is_on_another_machine(qapp: Any, watching: Any, url: str) -> None:
    controller, fake, clock, _ = watching()
    controller.settings = controller.settings.with_local_url(url)
    controller.window.watch_check.setChecked(True)
    assert not controller.watch.running and not controller.window.watch_check.isChecked()
    assert "this PC" in controller.window.notice.text() and "leave this PC" in controller.window.notice.text()
    clock.advance(60.0)
    controller._watch_tick()
    assert not FakeWorker.instances and fake.grabs <= 1  # nothing was captured for watching


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000", "http://127.0.0.2:9000"])
def test_loopback_node_addresses_are_accepted_for_watching(qapp: Any, watching: Any, url: str) -> None:
    controller, fake, clock, _ = watching()
    controller.settings = controller.settings.with_local_url(url)
    controller.window.watch_check.setChecked(True)
    assert controller.watch.running


def test_changing_the_node_url_to_another_machine_stops_watching(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    assert controller.watch.running
    controller.set_local_url("http://192.168.1.50:8000")
    assert not controller.watch.running and not controller.window.watch_check.isChecked()
    assert "leave this PC" in controller.window.notice.text()


def test_a_request_is_never_sent_if_the_url_changed_after_watching_started(qapp: Any, watching: Any) -> None:
    """The second guard: even if only ``settings`` changed (no UI path), the frame stays on this PC."""
    controller, fake, clock, _ = watching()
    controller.window.watch_check.setChecked(True)
    controller.settings = controller.settings.with_local_url("http://192.168.1.50:8000")
    tick(controller, qapp, clock)
    assert not FakeWorker.instances and not controller.watch.running and controller._watch_image is None


def test_turning_watching_on_with_the_cpu_engine_warns_that_it_is_slow(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.set_engine("local_cpu")
    controller.window.watch_check.setChecked(True)
    assert controller.watch.running  # allowed, but with a warning
    assert "CPU engine" in controller.window.notice.text() and "Local GPU is the intended engine" in controller.window.notice.text()


def test_the_gpu_engine_gets_no_cpu_warning(qapp: Any, watching: Any) -> None:
    controller, fake, clock, _ = watching()
    controller.set_engine("local_gpu")
    controller.window.watch_check.setChecked(True)
    assert controller.watch.running and "CPU engine" not in controller.window.notice.text()


def test_an_unprompted_alert_card_offers_no_code_to_copy(qapp: Any, watching: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, fake, clock, tray = watching()
    shown: list[Any] = []
    origins: list[str] = []
    monkeypatch.setattr(
        controller.window, "add_exchange", lambda question, result, searched="", note="", origin="window": (shown.append(result), origins.append(origin))
    )
    response = AnalyzeResponse.model_validate(analyze_response_json())
    assert response.code_blocks, "the fake answer contains a code block"
    controller._notify_finding("Build failed", ClientResult(response=response, metrics=LatencyMetrics(tier="local")))
    assert shown and shown[0].response.code_blocks == [] and shown[0].response.markdown == response.markdown
    assert origins == ["watch"]
    assert tray.messages == [("OmniSight noticed something", "Build failed")]


def test_a_search_problem_stays_visible_with_the_answer_it_affected(qapp: Any, make_controller: Any) -> None:
    """The notice shown while the model works used to be hidden the moment the answer card was added."""
    controller, _ = make_controller()
    FakeSearch.outcome = SearchOutcome(query="q", notice="Web search is unavailable right now (Wikipedia: timeout). Answering without it.")
    controller.window.search_check.setChecked(True)
    ask(controller, qapp, "why?", 1)
    texts = labels(controller.window._exchanges[0])
    assert any("Web search is unavailable right now" in t for t in texts), texts  # the note is part of the answer's card


def test_the_engine_ignoring_the_results_is_also_noted_on_the_answer_card(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    FakeWorker.echo_sources = False
    ask(controller, qapp, "why?", 1)
    assert any("could not use the web results" in t for t in labels(controller.window._exchanges[0]))


def test_an_answer_with_no_problem_has_no_note(qapp: Any, make_controller: Any) -> None:
    controller, _ = make_controller()
    controller.window.search_check.setChecked(True)
    ask(controller, qapp, "why?", 1)
    assert not any("unavailable" in t or "could not use" in t for t in labels(controller.window._exchanges[0]))


# ---------------------------------------------------------------------------
# Running commands (Phase 5)
# ---------------------------------------------------------------------------


def answer_with(*blocks: tuple[str, str]) -> ClientResult:
    """An answer whose code blocks are exactly ``blocks`` (language, code)."""
    body = analyze_response_json()
    body["code_blocks"] = [{"language": language, "code": code} for language, code in blocks]
    return ClientResult(response=AnalyzeResponse.model_validate(body), metrics=LatencyMetrics(tier="local"))


class FakeSignal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, code: int = 0) -> None:
        for slot in list(self._slots):
            slot(code)


class FakeDialog:
    """Stands in for RunDialog: records what it was asked to show, never runs anything, is opened (not exec'd)."""

    opened: list[tuple[str, str, Path]] = []
    last: Any = None

    def __init__(self, command: str, language: str, runner: Any, cwd: Path, parent: Any = None) -> None:
        self.cwd = cwd
        self.command, self.language = command, language
        self.finished = FakeSignal()
        self.deleted = False
        FakeDialog.opened.append((command, language, cwd))
        FakeDialog.last = self

    def open(self) -> None:
        pass

    def finish(self, cwd: Path | None = None) -> None:
        if cwd is not None:
            self.cwd = cwd
        self.finished.emit(0)

    def deleteLater(self) -> None:
        self.deleted = True


@pytest.fixture
def acting(qapp: Any, make_controller: Any, monkeypatch: pytest.MonkeyPatch):
    """A controller whose "Allow running commands?" question is answered at once (``controller._answer``) and whose Run
    dialog is a fake. The real question box and dialog are tested separately."""

    def build(**kw: Any) -> tuple[Any, list[Any]]:
        controller, _ = make_controller(**kw)
        FakeDialog.opened, FakeDialog.last = [], None
        asked: list[Any] = []
        controller._answer = True

        def ask(on_answer: Any) -> None:
            asked.append(on_answer)
            if controller._answer is not None:
                on_answer(controller._answer)

        controller._ask_allow_actions = ask
        monkeypatch.setattr(main, "RunDialog", FakeDialog)
        return controller, asked

    return build


def run_buttons(controller: Any) -> list[Any]:
    return [b for card in controller.window._exchanges for b in card.run_buttons]


def test_running_commands_is_off_by_default_and_no_run_button_is_visible(qapp: Any, acting: Any) -> None:
    controller, _ = acting()
    assert not controller._actions_enabled and not controller.window.actions_check.isChecked()
    controller.window.add_exchange("q", answer_with(("powershell", "git status")))
    assert len(run_buttons(controller)) == 1 and not run_buttons(controller)[0].isVisibleTo(controller.window)
    controller.on_run_requested("powershell", "git status")
    assert FakeDialog.opened == []  # the switch is off: nothing opens even if asked


def test_turning_the_switch_on_asks_and_a_yes_turns_it_on_and_remembers_it(qapp: Any, acting: Any) -> None:
    controller, asked = acting()
    controller.window.actions_check.setChecked(True)
    assert len(asked) == 1 and controller._actions_enabled and MemorySettings.store["actions_enabled"] is True


def test_the_question_box_is_plain_spoken_defaults_to_no_and_is_window_modal_without_a_nested_loop(qapp: Any, acting: Any) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QMessageBox

    controller, _ = acting()
    box = controller._build_allow_box()
    assert box.windowTitle() == "Allow running commands?" and box.windowModality() == Qt.WindowModality.WindowModal
    assert box.defaultButton() is box.button(QMessageBox.StandardButton.No) and box.escapeButton() is box.button(QMessageBox.StandardButton.No)
    assert "type RUN" in box.text() and "not a sandbox" in box.text() and "influence" in box.text() and "never by itself" in box.text()
    box.deleteLater()


def test_declining_the_question_leaves_it_off_and_unchecks_the_box(qapp: Any, acting: Any) -> None:
    controller, asked = acting()
    controller._answer = False
    controller.window.actions_check.setChecked(True)
    assert not controller._actions_enabled and not controller.window.actions_check.isChecked()
    assert MemorySettings.store["actions_enabled"] is False and len(asked) == 1


def test_turning_it_off_needs_no_question_and_hides_the_buttons_again(qapp: Any, acting: Any) -> None:
    controller, asked = acting()
    controller.window.actions_check.setChecked(True)
    controller.window.add_exchange("q", answer_with(("bash", "ls")))
    assert run_buttons(controller)[0].isVisibleTo(controller.window)
    controller.window.actions_check.setChecked(False)
    assert len(asked) == 1 and MemorySettings.store["actions_enabled"] is False
    assert not run_buttons(controller)[0].isVisibleTo(controller.window)


def test_each_shell_block_gets_its_own_run_button_and_other_languages_get_none(qapp: Any, acting: Any) -> None:
    controller, _ = acting()
    controller.window.actions_check.setChecked(True)
    controller.window.add_exchange("one", answer_with(("python", "print(1)"), ("powershell", "git status")))
    controller.window.add_exchange("two", answer_with(("bash", "ls"), ("cmd", "dir"), ("python", "x = 1")))
    controller.window.add_exchange("three", answer_with(("python", "print(1)"), ("json", "{}")))
    first, second, third = (card.run_buttons for card in controller.window._exchanges)
    assert [b.text() for b in first] == ["Run..."] and [b.text() for b in second] == ["Run 1...", "Run 2..."] and third == []
    assert [b.accessibleName() for b in second] == ["Run command 1", "Run command 2"]
    assert all(b.isVisibleTo(controller.window) for b in first + second)


def test_switching_on_reveals_the_buttons_of_earlier_answers_and_later_ones_show_theirs(qapp: Any, acting: Any) -> None:
    controller, _ = acting()
    controller.window.add_exchange("off", answer_with(("bash", "ls")))
    controller.window.actions_check.setChecked(True)
    controller.window.add_exchange("on", answer_with(("bash", "ls")))
    earlier, later = controller.window._exchanges
    assert later.run_buttons[0].isVisibleTo(controller.window)
    assert earlier.run_buttons[0].isVisibleTo(controller.window)  # the card made while the switch was off gets its button too


def test_clicking_run_opens_the_dialog_with_the_exact_command_and_remembers_the_folder(qapp: Any, acting: Any) -> None:
    controller, _ = acting()
    controller.window.actions_check.setChecked(True)
    controller.window.add_exchange("q", answer_with(("powershell", "git status\ngit diff --stat")))
    run_buttons(controller)[0].click()
    ((command, language, cwd),) = FakeDialog.opened
    assert (command, language) == ("git status\ngit diff --stat", "powershell") and cwd == controller._actions_cwd
    assert controller._action_dialog_open is True and controller._run_dialog is FakeDialog.last
    chosen = cwd / "chosen"
    FakeDialog.last.finish(chosen)  # the user closes it
    assert controller._action_dialog_open is False and controller._run_dialog is None and FakeDialog.last.deleted
    assert controller._actions_cwd == chosen and MemorySettings.store["actions_cwd"] == str(chosen)
    run_buttons(controller)[0].click()
    assert FakeDialog.opened[1][2] == chosen  # the next dialog starts in the remembered folder


def test_a_second_run_click_while_a_dialog_is_open_changes_nothing(qapp: Any, acting: Any) -> None:
    controller, _ = acting()
    controller.window.actions_check.setChecked(True)
    controller.window.add_exchange("q", answer_with(("powershell", "git status")))
    run_buttons(controller)[0].click()
    run_buttons(controller)[0].click()
    controller.on_run_requested("bash", "ls")
    assert len(FakeDialog.opened) == 1
    FakeDialog.last.finish()
    controller.on_run_requested("bash", "ls")
    assert len(FakeDialog.opened) == 2  # once the first is closed, the next can open


def test_a_second_toggle_while_the_question_is_showing_asks_nothing_more(qapp: Any, acting: Any) -> None:
    controller, asked = acting()
    controller._answer = None  # the question stays open
    controller._allow_box = object()  # as if the real box were showing
    controller.window.actions_check.setChecked(True)
    assert asked == [] and not controller._actions_enabled


@pytest.mark.parametrize(("language", "command"), [("python", "print(1)"), ("json", "{}"), ("", "ls"), ("bash", ""), ("bash", "   \n ")])
def test_only_non_empty_terminal_commands_open_the_dialog(qapp: Any, acting: Any, language: str, command: str) -> None:
    controller, _ = acting()
    controller.window.actions_check.setChecked(True)
    controller.on_run_requested(language, command)
    assert FakeDialog.opened == []


def test_a_watch_alert_never_offers_a_run_button(qapp: Any, acting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = acting()
    controller.window.actions_check.setChecked(True)
    monkeypatch.setattr(controller.tray, "showMessage", lambda *a, **k: None)
    controller._notify_finding("Possible error on your screen: rm -rf /", answer_with(("powershell", "Remove-Item -Recurse $HOME")))
    (card,) = controller.window._exchanges
    assert card.run_buttons == []  # the alert card has no code blocks, so nothing on it can be run


def test_the_switch_is_remembered_across_restarts_and_shows_the_buttons(qapp: Any, acting: Any) -> None:
    first, _ = acting()
    MemorySettings.store["actions_enabled"] = True  # what an earlier run saved
    restored = main.OmniSightController(qapp, main.ClientSettings(backend="auto", fallback_api_url=None), enable_hotkeys=False)
    try:
        assert restored._actions_enabled and restored.window.actions_check.isChecked()
        restored.window.add_exchange("q", answer_with(("bash", "ls")))
        assert restored.window._exchanges[0].run_buttons[0].isVisibleTo(restored.window)
        assert not first._actions_enabled  # the first controller was built before the setting existed
    finally:
        restored.shutdown()


def test_the_switch_and_the_run_button_have_accessible_names_and_honest_tooltips(qapp: Any) -> None:
    window = MainWindow()
    assert window.actions_check.accessibleName() == "Allow running commands"
    tip = window.actions_check.toolTip()
    assert "Off by default" in tip and "type RUN every time" in tip and "by itself" in tip
    window.add_exchange("q", answer_with(("bash", "ls")))
    assert "type RUN" in window._exchanges[0].run_buttons[0].toolTip()
    window.set_actions_enabled(True)
    assert window.actions_check.isChecked() and window._exchanges[0].run_buttons[0].isVisibleTo(window)
    toggles: list[bool] = []
    window.actions_toggled.connect(toggles.append)
    window.set_actions_enabled(False)  # programmatic: no echo as a user toggle
    assert toggles == []


def test_watch_does_not_look_at_the_screen_while_the_run_dialog_is_open(qapp: Any, watching: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dialog shows the command's output; it must not be captured and sent to a model as a 'possible error'."""
    controller, fake, clock, tray = watching()
    FakeWorker.script = ["NO"]
    controller.window.watch_check.setChecked(True)
    controller._actions_enabled = True
    monkeypatch.setattr(main, "RunDialog", FakeDialog)
    controller.on_run_requested("powershell", "git status")  # the dialog is open now (opened, not blocking)
    assert controller._action_dialog_open is True
    before = fake.grabs
    clock.advance(60.0)
    controller._watch_tick()
    qapp.processEvents()
    assert fake.grabs == before  # nothing was captured while the dialog is open
    FakeDialog.last.finish()
    clock.advance(11.0)
    controller._watch_tick()  # and watching resumes afterwards
    assert wait_until(lambda: fake.grabs > before, TIMEOUT_S, pump(qapp))


def test_a_frame_taken_just_before_the_dialog_opened_is_dropped(qapp: Any, watching: Any) -> None:
    controller, fake, clock, tray = watching()
    controller.window.watch_check.setChecked(True)
    controller.watch.begin()
    controller._action_dialog_open = True
    controller._on_watch_captured(object(), None)  # type: ignore[arg-type]
    assert not controller.watch.in_flight and not FakeWorker.instances
    controller._action_dialog_open = False


def test_the_real_question_box_answers_yes_no_and_escape_correctly(qapp: Any, acting: Any) -> None:
    """No stub here: the actual non-blocking QMessageBox, answered the way a user (or UI Automation) would."""
    from PyQt6.QtWidgets import QMessageBox

    controller, _ = acting()
    controller.__dict__.pop("_ask_allow_actions")  # back to the real method
    answers: list[bool] = []
    controller._ask_allow_actions(answers.append)
    assert controller._allow_box is not None  # opened, and the call returned at once
    controller._allow_box.button(QMessageBox.StandardButton.Yes).click()
    assert answers == [True] and controller._allow_box is None
    controller._ask_allow_actions(answers.append)
    controller._allow_box.button(QMessageBox.StandardButton.No).click()
    controller._ask_allow_actions(answers.append)
    controller._allow_box.reject()  # Esc or the window's close button
    assert answers == [True, False, False] and controller._allow_box is None


def test_the_real_switch_flow_turns_on_only_after_yes(qapp: Any, acting: Any) -> None:
    from PyQt6.QtWidgets import QMessageBox

    controller, _ = acting()
    controller.__dict__.pop("_ask_allow_actions")
    controller.window.actions_check.setChecked(True)  # returns at once; the question is showing
    assert controller._allow_box is not None and not controller._actions_enabled
    controller.window.actions_check.setChecked(True)  # already checked: nothing more to ask
    controller._allow_box.button(QMessageBox.StandardButton.Yes).click()
    assert controller._actions_enabled and controller.window.actions_check.isChecked() and MemorySettings.store["actions_enabled"] is True


def test_a_status_pill_animation_that_ticks_after_its_widget_is_gone_does_not_raise(qapp: Any) -> None:
    from PyQt6 import sip
    from PyQt6.QtGui import QColor

    from ui.components import StatusPill

    pill = StatusPill()
    pill.set_state("Working", "#89B4FA", pulsing=True)
    sip.delete(pill)
    pill._on_color(QColor("#FF0000"))  # what a late animation tick calls; PyQt would abort the process on an exception here
    pill._on_pulse(0.5)


def test_two_asks_sent_back_to_back_through_the_bridge_start_only_one_request(qapp: Any, make_controller: Any) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    controller, _ = make_controller(window=bridge)
    FakeWorker.hold = True
    bridge._command({"cmd": "ask", "text": "first"})
    bridge._command({"cmd": "ask", "text": "second"})
    bridge._command({"cmd": "capture"})
    assert wait_until(lambda: len(FakeWorker.instances) == 1, TIMEOUT_S, pump(qapp))
    wait_until(lambda: False, 0.3, pump(qapp))  # time for a second request to start, if one were going to
    assert len(FakeWorker.instances) == 1
    assert FakeWorker.instances[0].request.prompt == "first"
    bridge._command({"cmd": "engine", "key": "local_gpu"})  # refused while the request runs: the engine did not change
    assert controller.settings.engine_choice != "local_gpu"
    FakeWorker.instances[0].release()
    assert wait_until(lambda: not controller.state.is_busy, TIMEOUT_S, pump(qapp))
    assert bridge.exchange_count == 1


def _bridge_controller(make_controller: Any) -> tuple[Any, Any, list[Any]]:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    asked: list[Any] = []
    bridge.ask_allow_actions = lambda title, text, on_answer: asked.append((title, text, on_answer))  # type: ignore[method-assign]
    controller, _ = make_controller(window=bridge)
    return controller, bridge, asked


def test_turning_on_commands_through_the_bridge_asks_the_app_and_waits_for_its_answer(qapp: Any, make_controller: Any) -> None:
    controller, bridge, asked = _bridge_controller(make_controller)
    bridge._command({"cmd": "set", "name": "actions", "on": True})
    assert len(asked) == 1 and not controller._actions_enabled
    title, text, answer = asked[0]
    assert "Allow running commands" in title and "nothing runs until you type RUN" in text
    bridge._command({"cmd": "set", "name": "actions", "on": True})  # a second click while the question is open asks nothing more
    assert len(asked) == 1
    answer(True)
    assert controller._actions_enabled and MemorySettings.store["actions_enabled"] is True
    assert controller._allow_box is None


def test_a_no_leaves_commands_off_and_the_switch_is_pushed_back_off(qapp: Any, make_controller: Any) -> None:
    controller, bridge, asked = _bridge_controller(make_controller)
    bridge._command({"cmd": "set", "name": "actions", "on": True})
    asked[0][2](False)
    assert not controller._actions_enabled
    assert bridge._sticky["switch:actions"]["on"] is False
    bridge._command({"cmd": "set", "name": "actions", "on": True})  # and it can be asked again later
    assert len(asked) == 2


def test_with_no_app_connected_the_real_bridge_answers_no(qapp: Any, make_controller: Any) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    controller, _ = make_controller(window=bridge)
    bridge._command({"cmd": "set", "name": "actions", "on": True})
    assert not controller._actions_enabled and controller._allow_box is None


def test_the_settings_page_data_comes_from_the_controller(qapp: Any, make_controller: Any) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    controller, _ = make_controller(window=bridge)
    controller.capability_line = "test pc"
    bridge._command({"cmd": "settings.apply", "override": "https://abc.trycloudflare.com", "local_url": "http://127.0.0.1:9000"})
    assert controller.settings.manual_override_url == "https://abc.trycloudflare.com"
    assert controller.settings.local_dev_url == "http://127.0.0.1:9000"
    info = bridge._sticky["settings_info"]
    assert info["override_url"] == "https://abc.trycloudflare.com" and info["local_url"] == "http://127.0.0.1:9000"
    assert info["capability"] == "test pc" and info["log_dir"].endswith("logs") and "Alt+C" in info["hotkeys"]


def test_a_bad_address_is_reported_not_applied(qapp: Any, make_controller: Any) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    results: list[str] = []
    bridge.set_settings_result = results.append  # type: ignore[method-assign]
    controller, _ = make_controller(window=bridge)
    before = controller.settings.local_dev_url
    bridge._command({"cmd": "settings.apply", "override": "", "local_url": "ftp://not-allowed"})
    assert results and results[-1] != "Saved for this session."
    assert controller.settings.local_dev_url == before


class _FakeSession(QObject):
    """Stands in for a BridgeRunSession: closes when told to."""

    finished = pyqtSignal(int)

    def __init__(self, cwd: Path) -> None:
        super().__init__()
        self.cwd = cwd


def test_a_run_request_opens_the_approval_in_the_app_and_keeps_watch_off_the_screen_while_it_is_open(
    qapp: Any, make_controller: Any, tmp_path: Path
) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    opened: list[tuple[str, str]] = []
    session = _FakeSession(tmp_path)

    def opener(command: str, language: str, runner: Any, cwd: Path) -> Any:
        opened.append((language, command))
        return session

    bridge.open_run_session = opener  # type: ignore[method-assign]
    controller, _ = make_controller(window=bridge)
    controller._actions_enabled = True
    controller.on_run_requested("powershell", "Get-Date")
    assert opened == [("powershell", "Get-Date")] and controller._action_dialog_open and controller._run_dialog is session
    controller.on_run_requested("powershell", "Get-Date")  # one approval at a time
    assert len(opened) == 1
    session.finished.emit(0)
    assert not controller._action_dialog_open and controller._run_dialog is None
    assert MemorySettings.store["actions_cwd"] == str(tmp_path)


def test_no_approval_opens_when_the_app_cannot_show_one(qapp: Any, make_controller: Any) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")  # nobody connected: open_run_session returns None
    controller, _ = make_controller(window=bridge)
    controller._actions_enabled = True
    controller.on_run_requested("powershell", "Get-Date")
    assert controller._run_dialog is None and not controller._action_dialog_open


def test_the_switch_must_be_on_and_the_text_a_terminal_command_for_the_bridge_too(qapp: Any, make_controller: Any) -> None:
    from ui.bridge import BridgeWindow

    bridge = BridgeWindow("token")
    opened: list[Any] = []
    bridge.open_run_session = lambda *a: opened.append(a)  # type: ignore[method-assign]
    controller, _ = make_controller(window=bridge)
    controller.on_run_requested("powershell", "Get-Date")  # the switch is off
    controller._actions_enabled = True
    controller.on_run_requested("python", "print(1)")  # not a terminal command
    controller.on_run_requested("powershell", "   ")  # nothing to run
    assert opened == []
