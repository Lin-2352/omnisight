"""End-to-end client loop against the real node app over real HTTP.

synthetic 1440p screen -> ``ScreenCapturer.capture`` (mss stand-in) -> ``build_request``
-> ``InferenceClient`` (local backend) -> uvicorn -> ``server.create_app`` (validation,
middleware, error mapping) -> torch-free ``FakeEngine`` -> ``AnalyzeResponse`` ->
``StateMachine`` and history. Only the model is fake; every byte crosses a socket.
"""

from __future__ import annotations

import base64
import random

import pytest
from PIL import Image

from core.config import ClientSettings, EndpointResolver
from core.state import AppState, StateMachine
from network.client import CircuitBreaker, InferenceClient, InferenceError, build_request
from network.schemas import AnalysisMode, LatencyMetrics
from tests.support import FakeEngine, wav_bytes


@pytest.fixture
def local_client(live_node: str) -> InferenceClient:
    settings = ClientSettings(backend="local", local_dev_url=live_node, fallback_api_url=None)
    return InferenceClient(settings, EndpointResolver(settings), rng=random.Random(1), breaker=CircuitBreaker())


def run_pipeline(capturer, client: InferenceClient, machine: StateMachine, **request_kwargs):
    assert machine.transition(AppState.CAPTURING, "Alt+C")
    captured = capturer.capture(monitor_index=1)
    assert machine.transition(AppState.ANALYZING)
    request = build_request(captured.to_image_payload(), **request_kwargs)
    try:
        result = client.analyze(request)
    except InferenceError as exc:
        machine.fail(str(exc))
        return captured, request, None
    metrics = result.metrics.model_copy(update={"capture_ms": captured.capture_latency_ms, "encode_ms": captured.encode_latency_ms})
    machine.add_result(result.response, metrics)
    assert machine.transition(AppState.DISPLAYING, result.metrics.tier or "")
    return captured, request, result


def test_screenshot_to_answer_through_the_real_server(
    qapp, make_capturer, screens: dict[str, Image.Image], local_client: InferenceClient, fake_engine: FakeEngine
) -> None:
    capturer, fake = make_capturer([screens["1440p"]])
    machine = StateMachine()
    captured, request, result = run_pipeline(capturer, local_client, machine, mode=AnalysisMode.DEBUG, prompt="why?")

    assert fake.grabs == 1
    assert captured.original_res == "2560x1440" and captured.scaled_res == "1280x720"
    assert result is not None and result.metrics.tier == "local"
    assert result.response.request_id == request.request_id
    assert result.response.model_id == "fake/engine"
    assert result.response.code_blocks and result.response.code_blocks[0].language == "python"

    # The server received exactly the bytes the client encoded.
    received = fake_engine.last_request
    assert fake_engine.calls == 1
    assert received.image.data_b64 == captured.image_b64
    assert (received.image.width, received.image.height) == (1280, 720)
    assert received.prompt == "why?" and received.mode is AnalysisMode.DEBUG
    assert received.client.kind == "desktop"

    assert machine.state is AppState.DISPLAYING
    entry = machine.latest()
    assert entry is not None and entry.metrics.capture_ms == captured.capture_latency_ms
    assert entry.metrics.end_to_end_ms >= entry.metrics.network_ms > 0


def test_voice_query_audio_reaches_the_server(
    qapp, make_capturer, screens: dict[str, Image.Image], local_client: InferenceClient, fake_engine: FakeEngine
) -> None:
    capturer, _ = make_capturer([screens["1080p"]])
    machine = StateMachine()
    _, _, result = run_pipeline(
        capturer, local_client, machine, mode=AnalysisMode.VOICE_QUERY, audio_wav=wav_bytes(1.0), audio_duration_ms=1000
    )
    assert result is not None
    assert result.response.transcript == "fake transcript of 16000 samples"
    assert fake_engine.last_request.audio.sample_rate == 16000


def test_gpu_oom_on_the_server_surfaces_as_a_readable_ui_error(
    qapp, make_capturer, screens: dict[str, Image.Image], local_client: InferenceClient, fake_engine: FakeEngine
) -> None:
    fake_engine.behavior = "oom"
    capturer, _ = make_capturer([screens["1080p"]])
    machine = StateMachine()
    _, _, result = run_pipeline(capturer, local_client, machine)
    assert result is None
    assert machine.state is AppState.ERROR
    assert "GPU memory ceiling exceeded" in (machine.last_error or "")
    assert machine.history() == []


def test_mismatched_image_dimensions_are_rejected_by_the_server_not_failed_over(
    qapp, make_capturer, screens: dict[str, Image.Image], local_client: InferenceClient, fake_engine: FakeEngine
) -> None:
    capturer, _ = make_capturer([screens["1080p"]])
    captured = capturer.capture(monitor_index=1)
    payload = captured.to_image_payload().model_copy(update={"width": 1000})
    with pytest.raises(InferenceError) as info:
        local_client.analyze(build_request(payload))
    assert info.value.status == 422
    assert "declared=1000x720" in str(info.value)


def test_server_rejects_an_unready_model_and_the_client_explains(
    qapp, make_capturer, screens: dict[str, Image.Image], local_client: InferenceClient, fake_engine: FakeEngine
) -> None:
    fake_engine._state = "loading"
    capturer, _ = make_capturer([screens["1080p"]])
    captured = capturer.capture(monitor_index=1)
    with pytest.raises(InferenceError, match="still loading"):
        local_client.analyze(build_request(captured.to_image_payload()))


def test_health_endpoint_round_trip(live_node: str, local_client: InferenceClient) -> None:
    health = local_client.health(live_node)
    assert health.model_loaded and health.status == "ok"


def test_payload_on_the_wire_is_standard_base64_jpeg(make_capturer, screens: dict[str, Image.Image]) -> None:
    capturer, _ = make_capturer([screens["4k"]])
    captured = capturer.capture(monitor_index=1)
    raw = base64.b64decode(captured.image_b64, validate=True)
    assert raw[:3] == b"\xff\xd8\xff"
    request = build_request(captured.to_image_payload(), max_new_tokens=256)
    assert request.model_dump(mode="json")["image"]["data_b64"] == captured.image_b64
    assert LatencyMetrics().end_to_end_ms == 0
