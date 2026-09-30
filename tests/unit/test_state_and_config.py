"""Client state machine, settings parsing and logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from core import config, logger as client_logger
from core.config import (
    BACKENDS,
    DEFAULT_FALLBACK_API_URL,
    DEFAULT_GIST_ID,
    LOCAL_DEV_URL,
    ClientSettings,
    EndpointResolver,
)
from core.state import ALLOWED_TRANSITIONS, BUSY_STATES, AppState, StateMachine
from network.schemas import AnalyzeResponse, LatencyMetrics
from tests.support import analyze_response_json


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@pytest.fixture
def machine(qapp: object) -> StateMachine:
    return StateMachine(max_history=3)


def record(machine: StateMachine) -> list[tuple[AppState, AppState]]:
    seen: list[tuple[AppState, AppState]] = []
    machine.state_changed.connect(lambda old, new: seen.append((old, new)))
    return seen


def test_starts_idle_and_not_busy(machine: StateMachine) -> None:
    assert machine.state is AppState.IDLE
    assert not machine.is_busy and machine.last_error is None


@pytest.mark.parametrize("start", list(AppState))
@pytest.mark.parametrize("target", list(AppState))
def test_transition_table_is_enforced_exactly(machine: StateMachine, start: AppState, target: AppState) -> None:
    machine._state = start  # place the machine in `start` directly
    seen = record(machine)
    accepted = machine.transition(target, reason="test")
    if target is start:
        assert accepted and seen == []  # same-state transition is a silent no-op
    elif target in ALLOWED_TRANSITIONS[start]:
        assert accepted and machine.state is target and seen == [(start, target)]
    else:
        assert not accepted and machine.state is start and seen == []


def test_busy_states(machine: StateMachine) -> None:
    for state in AppState:
        machine._state = state
        assert machine.is_busy is (state in BUSY_STATES)


def test_fail_from_idle_records_the_error_and_emits(machine: StateMachine) -> None:
    errors: list[str] = []
    machine.error_raised.connect(errors.append)
    seen = record(machine)
    machine.fail("screen is black")
    assert machine.state is AppState.ERROR
    assert machine.last_error == "screen is black"
    assert errors == ["screen is black"]
    assert seen == [(AppState.IDLE, AppState.ERROR)]


@pytest.mark.parametrize("origin", [AppState.IDLE, AppState.DISPLAYING, AppState.ERROR])
def test_chat_can_start_analysis_without_a_capture(machine: StateMachine, origin: AppState) -> None:
    machine._state = origin
    assert machine.transition(AppState.ANALYZING, reason="chat")
    assert machine.is_busy


def test_analysis_cannot_start_in_the_middle_of_a_recording(machine: StateMachine) -> None:
    machine._state = AppState.RECORDING_VOICE
    assert not machine.transition(AppState.ANALYZING)


def test_fail_from_displaying_is_forced_through_idle(machine: StateMachine) -> None:
    machine._state = AppState.DISPLAYING
    assert AppState.ERROR not in ALLOWED_TRANSITIONS[AppState.DISPLAYING]
    machine.fail("late failure")
    assert machine.state is AppState.ERROR and machine.last_error == "late failure"


def test_fail_while_already_in_error_updates_the_message(machine: StateMachine) -> None:
    machine.fail("first")
    machine.fail("second")
    assert machine.state is AppState.ERROR and machine.last_error == "second"


def test_leaving_error_clears_the_last_error(machine: StateMachine) -> None:
    machine.fail("boom")
    assert machine.transition(AppState.CAPTURING)
    assert machine.last_error is None


def test_reset_only_leaves_finished_states(machine: StateMachine) -> None:
    machine._state = AppState.ANALYZING
    machine.reset()
    assert machine.state is AppState.ANALYZING
    machine._state = AppState.DISPLAYING
    machine.reset()
    assert machine.state is AppState.IDLE


def test_history_is_bounded_and_clearable(machine: StateMachine) -> None:
    counts: list[int] = []
    machine.history_changed.connect(counts.append)
    for index in range(5):
        response = AnalyzeResponse.model_validate(analyze_response_json(model_id=f"model-{index}"))
        machine.add_result(response, LatencyMetrics(network_ms=index))
    assert [entry.response.model_id for entry in machine.history()] == ["model-2", "model-3", "model-4"]
    latest = machine.latest()
    assert latest is not None and latest.response.model_id == "model-4"
    machine.clear_history()
    assert machine.history() == [] and machine.latest() is None
    assert counts == [1, 2, 3, 3, 3, 0]


def test_end_to_end_latency_sums_client_side_stages() -> None:
    metrics = LatencyMetrics(capture_ms=20, encode_ms=8, network_ms=9000, server_ttft_ms=2900)
    assert metrics.end_to_end_ms == 9028


# ---------------------------------------------------------------------------
# ClientSettings
# ---------------------------------------------------------------------------


def settings(**env: str) -> ClientSettings:
    return ClientSettings.from_environment(env, load_files=False)


def test_defaults_need_no_configuration() -> None:
    s = settings()
    assert s.gist_id == DEFAULT_GIST_ID
    assert s.fallback_api_url == DEFAULT_FALLBACK_API_URL
    assert s.local_dev_url == LOCAL_DEV_URL
    assert (s.connect_timeout_s, s.read_timeout_s, s.request_deadline_s, s.cache_ttl_s, s.local_timeout_s) == (3.0, 60.0, 120.0, 30.0, 300.0)
    assert (s.backend, s.max_new_tokens, s.log_level, s.manual_override_url, s.github_token) == ("auto", 512, "INFO", None, None)


@pytest.mark.parametrize("value", ["off", "OFF", "none", "disabled", "0"])
def test_web_fallback_can_be_switched_off(value: str) -> None:
    assert settings(FALLBACK_API_URL=value).fallback_api_url is None


def test_custom_urls_are_validated_and_normalized() -> None:
    s = settings(
        FALLBACK_API_URL="https://example.test/api/fallback-infer/",
        MANUAL_OVERRIDE_URL="https://abc.trycloudflare.com/",
        OMNISIGHT_LOCAL_DEV_URL="http://127.0.0.1:9000/",
    )
    assert s.fallback_api_url == "https://example.test/api/fallback-infer"
    assert s.manual_override_url == "https://abc.trycloudflare.com"
    assert s.local_dev_url == "http://127.0.0.1:9000"
    assert settings(OMNISIGHT_ENDPOINT_OVERRIDE="http://127.0.0.1:8001").manual_override_url == "http://127.0.0.1:8001"
    with pytest.raises(ValueError, match="FALLBACK_API_URL must start with http"):
        settings(FALLBACK_API_URL="ftp://example.test")


def test_token_and_gist_aliases() -> None:
    assert settings(GITHUB_TOKEN="a", OMNISIGHT_CLIENT_GITHUB_TOKEN="b").github_token == "b"
    assert settings(GITHUB_TOKEN="a").github_token == "a"
    assert settings(OMNISIGHT_GIST_ID="abc").gist_id == "abc"
    assert settings(GITHUB_GIST_ID="def", OMNISIGHT_GIST_ID="abc").gist_id == "def"


def test_numeric_settings_are_validated() -> None:
    s = settings(OMNISIGHT_REQUEST_TIMEOUT_S="90", OMNISIGHT_CONNECT_TIMEOUT_S="2.5", OMNISIGHT_MAX_NEW_TOKENS="256")
    assert (s.read_timeout_s, s.connect_timeout_s, s.max_new_tokens) == (90.0, 2.5, 256)
    with pytest.raises(ValueError, match="must be a number"):
        settings(OMNISIGHT_REQUEST_TIMEOUT_S="soon")
    with pytest.raises(ValueError, match="must be positive"):
        settings(OMNISIGHT_CONNECT_TIMEOUT_S="0")
    with pytest.raises(ValueError, match="between 16 and 512"):
        settings(OMNISIGHT_MAX_NEW_TOKENS="1024")


def test_backend_choice() -> None:
    assert set(BACKENDS) == {"auto", "kaggle", "local"}
    assert settings(OMNISIGHT_BACKEND=" Local ").backend == "local"
    with pytest.raises(ValueError, match="OMNISIGHT_BACKEND must be one of"):
        settings(OMNISIGHT_BACKEND="cloud")
    base = settings()
    assert base.with_backend("kaggle").backend == "kaggle"
    assert base.with_local_url(" http://127.0.0.1:8123/ ").local_dev_url == "http://127.0.0.1:8123"
    assert base.with_local_url("").local_dev_url == LOCAL_DEV_URL
    assert base.with_override("https://x.trycloudflare.com").manual_override_url == "https://x.trycloudflare.com"
    assert base.with_override("  ").manual_override_url is None
    assert base.with_override(None).manual_override_url is None


def test_log_level_is_upper_cased() -> None:
    assert settings(OMNISIGHT_LOG_LEVEL="debug").log_level == "DEBUG"


def test_env_files_are_loaded_without_overriding_real_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "OmniSight").mkdir()
    (tmp_path / "OmniSight" / ".env").write_text("OMNISIGHT_TEST_FROM_FILE=file\nOMNISIGHT_TEST_REAL=file\n", encoding="utf-8")
    for name in ("OMNISIGHT_TEST_FROM_FILE", "OMNISIGHT_TEST_REAL"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    monkeypatch.setenv("OMNISIGHT_TEST_REAL", "real")
    loaded = config.load_env_files()
    assert tmp_path / "OmniSight" / ".env" in loaded
    import os

    assert os.environ["OMNISIGHT_TEST_FROM_FILE"] == "file"
    assert os.environ["OMNISIGHT_TEST_REAL"] == "real"


def test_user_config_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    assert config.user_config_dir() == Path.home() / ".omnisight"


def test_process_wide_resolver_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_default_resolver", None)
    first = config.default_resolver(settings(MANUAL_OVERRIDE_URL="http://127.0.0.1:8001"))
    assert isinstance(first, EndpointResolver)
    assert config.default_resolver() is first
    resolution = config.resolve_active_endpoint()
    assert (resolution.source, resolution.url) == ("override", "http://127.0.0.1:8001")
    config.default_resolver(settings(MANUAL_OVERRIDE_URL="http://127.0.0.1:8002"))
    assert config.resolve_active_endpoint().url == "http://127.0.0.1:8002"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_omnisight_logger() -> object:
    root = logging.getLogger(client_logger.LOGGER_NAME)
    saved = (list(root.handlers), root.propagate, root.level)
    yield root
    for handler in list(root.handlers):
        if handler not in saved[0]:
            root.removeHandler(handler)
            handler.close()
    root.propagate, root.level = saved[1], saved[2]


def test_configure_logging_writes_a_rotating_file_and_is_idempotent(tmp_path: Path, restore_omnisight_logger: logging.Logger) -> None:
    path = client_logger.configure_logging("debug", log_dir=tmp_path, console=True)
    path_again = client_logger.configure_logging("INFO", log_dir=tmp_path, console=True)
    assert path == path_again == tmp_path / "client.log"
    installed = [h for h in restore_omnisight_logger.handlers if getattr(h, "_omnisight_handler", False)]
    assert len(installed) == 2  # one file + one console handler, not duplicated
    client_logger.get_logger("test").info("hello from the test suite")
    for handler in installed:
        handler.flush()
    assert "hello from the test suite" in path.read_text(encoding="utf-8")
    assert client_logger.get_logger("x").name == "omnisight.x"


def test_unwritable_log_directory_falls_back_to_console_only(tmp_path: Path, restore_omnisight_logger: logging.Logger, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    client_logger.configure_logging("INFO", log_dir=blocker / "logs", console=False)
    assert "file logging disabled" in capsys.readouterr().err


def test_color_formatter_wraps_known_levels() -> None:
    formatter = client_logger.ColorFormatter("%(message)s")
    warning = logging.LogRecord("omnisight", logging.WARNING, __file__, 1, "careful", None, None)
    custom = logging.LogRecord("omnisight", 25, __file__, 1, "custom", None, None)
    assert formatter.format(warning).startswith("\x1b[") and formatter.format(warning).endswith("\x1b[0m")
    assert formatter.format(custom) == "custom"


def test_ansi_is_not_enabled_for_redirected_streams() -> None:
    import io

    assert client_logger._enable_windows_ansi(io.StringIO()) is False


def test_ansi_probe_on_a_terminal_like_stream_never_raises() -> None:
    import io

    class FakeTerminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    # Under pytest the real console handles are pipes, so the Win32 probe reports False;
    # off Windows any TTY is assumed to understand ANSI.
    result = client_logger._enable_windows_ansi(FakeTerminal())
    assert isinstance(result, bool)
    assert isinstance(client_logger._enable_windows_ansi(sys.stderr), bool)


def test_default_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert client_logger.default_log_dir() == tmp_path / "OmniSight" / "logs"
    monkeypatch.delenv("APPDATA")
    assert client_logger.default_log_dir() == Path.home() / ".omnisight" / "logs"
    assert sys.platform  # keep the module import meaningful on every platform
