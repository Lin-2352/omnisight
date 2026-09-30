"""Shared pytest fixtures for the OmniSight test suite.

Everything is deterministic and offline: synthetic screens, an ``mss`` stand-in,
``responses`` mocks for the gist and the nodes (timeouts are raised instantly,
nothing sleeps), the real FastAPI node app over a torch-free engine (in-process
and as a live uvicorn server), a Qt application, and ``tracemalloc`` hooks.
Plain helpers live in ``tests/support.py``.

The desktop client needs Windows (PyQt6, mss, winreg, Win32 DPI calls). On other
platforms its test files are not collected, so the contract, server and security
suites still run on Linux CI.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.support import (  # noqa: E402
    RESOLUTIONS,
    FakeEngine,
    FakeMss,
    ManualClock,
    free_port,
    render_terminal,
    small_jpeg_payload,
)

IS_WINDOWS = sys.platform == "win32"

#: Test modules that import the Windows desktop client.
CLIENT_TEST_FILES = (
    "unit/test_image_pipeline.py",
    "unit/test_audio_recorder.py",
    "unit/test_state_and_config.py",
    "unit/test_node_supervisor.py",
    "unit/test_negotiation.py",
    "unit/test_memory_tts.py",
    "unit/test_search.py",
    "unit/test_main_window.py",
    "integration/test_dynamic_discovery.py",
    "integration/test_failover_circuit.py",
    "integration/test_full_pipeline_mock.py",
    "performance/test_latency_budget.py",
    "performance/test_memory_leaks.py",
)
collect_ignore = [] if IS_WINDOWS else list(CLIENT_TEST_FILES)


# -- synthetic screens --------------------------------------------------------


@pytest.fixture(scope="session")
def screens() -> dict[str, Image.Image]:
    """Terminal/IDE screenshots at 1080p, 1440p and 4K (rendered once per session)."""
    return {name: render_terminal(*size) for name, size in RESOLUTIONS.items()}


@pytest.fixture(scope="session")
def black_frame() -> Image.Image:
    return Image.new("RGB", (1920, 1080), (0, 0, 0))


@pytest.fixture(scope="session")
def white_frame() -> Image.Image:
    return Image.new("RGB", (1920, 1080), (255, 255, 255))


@pytest.fixture(scope="session")
def noise_frame() -> Image.Image:
    """Worst case for JPEG: full-entropy RGB noise at 1440p."""
    rng = np.random.default_rng(7)
    return Image.fromarray(rng.integers(0, 256, (1440, 2560, 3), dtype=np.uint8))


@pytest.fixture(scope="session")
def small_jpeg() -> dict[str, Any]:
    return small_jpeg_payload()


# -- capture ------------------------------------------------------------------


@pytest.fixture
def make_capturer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[list[Image.Image]], tuple[Any, FakeMss]]]:
    """Build a real ``ScreenCapturer`` whose grabs come from ``FakeMss`` (no retry delay)."""
    from capture import screen

    monkeypatch.setattr(screen, "BLACK_FRAME_RETRY_DELAY_S", 0.0)
    made: list[Any] = []

    def factory(frames: list[Image.Image]) -> tuple[Any, FakeMss]:
        fake = FakeMss(frames)
        capturer = screen.ScreenCapturer()
        capturer._sct = lambda: fake  # type: ignore[method-assign]
        made.append(capturer)
        return capturer, fake

    yield factory
    for capturer in made:
        capturer.close()


# -- HTTP mocks -----------------------------------------------------------------


@pytest.fixture
def http_mock() -> Iterator[Any]:
    """A strict ``responses`` mock: any unregistered HTTP call raises ConnectionError."""
    import responses

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


# -- node app -------------------------------------------------------------------


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def server_client(fake_engine: FakeEngine) -> Iterator[Any]:
    """``TestClient`` over the real node app (middleware, validation, error mapping)."""
    from fastapi.testclient import TestClient

    import node_config
    import server

    app = server.create_app(fake_engine, node_config.ServerSettings())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def live_node(fake_engine: FakeEngine) -> Iterator[str]:
    """The node app served by uvicorn on a free loopback port; yields its base URL."""
    import uvicorn

    import node_config
    import server

    app = server.create_app(fake_engine, node_config.ServerSettings())
    config = uvicorn.Config(app, host="127.0.0.1", port=free_port(), log_level="warning", lifespan="off")
    node = uvicorn.Server(config)
    thread = threading.Thread(target=node.run, name="test-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not node.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn test server did not start within 10 s")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{config.port}"
    finally:
        node.should_exit = True
        thread.join(timeout=10.0)


# -- Qt ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# -- memory -----------------------------------------------------------------------


@pytest.fixture
def heap() -> Iterator[Callable[[], tracemalloc.Snapshot]]:
    """Run one test under ``tracemalloc``; yields ``take_snapshot``."""
    tracemalloc.start(1)
    try:
        yield tracemalloc.take_snapshot
    finally:
        tracemalloc.stop()
