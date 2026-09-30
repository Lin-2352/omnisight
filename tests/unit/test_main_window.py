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
from core.memory import ConversationMemory
from core.search import SearchOutcome
from core.node_supervisor import NodeState, NodeStatus
from core.state import AppState
from network.schemas import AnalysisMode, AnalyzeResponse, ClientResult, LatencyMetrics, WebResult
from tests.support import FakeMss, analyze_response_json, render_terminal, wait_until, wav_bytes
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

    def __init__(self, settings: Any, resolver: Any, request: Any, base_metrics: Any = None) -> None:
        super().__init__()
        self.request = request
        FakeWorker.instances.append(self)

    def start(self) -> None:
        QTimer.singleShot(0, self._run)

    def _run(self) -> None:
        self.tier_changed.emit("local")
        body = analyze_response_json(str(self.request.request_id), source="kaggle", model_id="fake/engine")
        if FakeWorker.echo_sources:  # like the real node: the results it was given are its sources
            body["sources"] = [r.model_dump() for r in self.request.web_results]
        response = AnalyzeResponse.model_validate(body)
        result = ClientResult(response=response, metrics=LatencyMetrics(tier="local", server_ttft_ms=1500, tokens_generated=42))
        self.succeeded.emit(result.model_dump(mode="json"))
        self.finished.emit()

    def cancel(self) -> None:
        pass

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

    def build(frames: list[Image.Image] | None = None, *, backend: str = "auto") -> tuple[Any, FakeMss]:
        MemorySettings.store = {}
        FakeWorker.instances = []
        FakeSpeaker.spoken, FakeSpeaker.stops = [], 0
        FakeSearch.queries, FakeSearch.gate, FakeSearch.outcome = [], None, None
        FakeWorker.echo_sources = True
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
        monkeypatch.setattr(
            main, "ConversationMemory", lambda enabled=True: ConversationMemory(tmp_path / "history.json", enabled=enabled)
        )
        monkeypatch.setattr(main, "AudioRecorder", FakeRecorder)
        monkeypatch.setattr(main, "probe", lambda: None)
        monkeypatch.setattr(main, "describe", lambda capability: "test pc")
        settings = main.ClientSettings(backend=backend, fallback_api_url=None)
        controller = main.OmniSightController(qapp, settings, enable_hotkeys=False)
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
    assert "unavailable" in controller.window.notice.text()


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
    assert "could not use the web results" in controller.window.notice.text()
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
