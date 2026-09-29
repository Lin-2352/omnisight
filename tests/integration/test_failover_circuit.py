"""Multi-tier failover (Kaggle -> local -> web fallback), retries and the circuit breaker.

All HTTP is mocked with ``responses``; timeouts and connection drops are raised
instantly by the mock (nothing sleeps), and retry back-off goes through an injected
``sleep`` that only records the requested delay.

Guardrails from the Phase 5 spec:
* a Kaggle 502/504 or a 3 s connect timeout reaches the next tier within 200 ms;
* once a tier has failed, the circuit breaker skips it for the cooldown;
* only transient failures (connection drops, 429, 500) are retried, with jitter;
* failures reach the UI as signals/``InferenceError``, never as raw exceptions.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import pytest
import requests
import responses
from urllib3.exceptions import NewConnectionError

from core.config import ClientSettings, EndpointResolver
from network import client as net
from network.client import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    CancelledError,
    CircuitBreaker,
    HealthCheckWorker,
    InferenceClient,
    InferenceError,
    InferenceWorker,
    build_request,
)
from network.schemas import AnalysisMode, ImagePayload
from tests.support import (
    FALLBACK_URL,
    GIST_ID,
    GIST_URL,
    KAGGLE_ANALYZE,
    KAGGLE_URL,
    ManualClock,
    analyze_response_json,
    endpoint_record,
    error_json,
    free_port,
    gist_body,
    small_jpeg_payload,
    wait_until,
    wav_bytes,
)

FAILOVER_BUDGET_S = 0.200


class Timeline:
    """Records when each mocked endpoint was hit (perf_counter seconds)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, float]] = []

    def hit(self, name: str) -> None:
        self.events.append((name, time.perf_counter()))

    def first(self, name: str) -> float:
        return next(t for n, t in self.events if n == name)

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.events if n == name)


@pytest.fixture
def timeline() -> Timeline:
    return Timeline()


@pytest.fixture
def breaker(clock: ManualClock) -> CircuitBreaker:
    return CircuitBreaker(cooldown_s=30.0, clock=clock)


@pytest.fixture
def sleeps() -> list[float]:
    return []


def closed_local_url() -> str:
    return f"http://127.0.0.1:{free_port()}"  # nothing listens there


def make_client(
    breaker: CircuitBreaker,
    sleeps: list[float],
    *,
    backend: str = "kaggle",
    fallback: str | None = FALLBACK_URL,
    **overrides: Any,
) -> InferenceClient:
    settings = ClientSettings(
        gist_id=GIST_ID, fallback_api_url=fallback, backend=backend, local_dev_url=closed_local_url(), **overrides
    )
    resolver = EndpointResolver(settings)
    return InferenceClient(settings, resolver, sleep=sleeps.append, rng=random.Random(7), breaker=breaker)


def request() -> Any:
    return build_request(ImagePayload.model_validate(small_jpeg_payload()), mode=AnalysisMode.DEBUG)


def mock_gist(http_mock: responses.RequestsMock, **record: Any) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record(**record)))


def ok(name: str, timeline: Timeline, source: str = "kaggle") -> Callable[[Any], tuple[int, dict[str, str], str]]:
    import json

    def callback(req: Any) -> tuple[int, dict[str, str], str]:
        timeline.hit(name)
        body = json.loads(req.body)
        return 200, {}, json.dumps(analyze_response_json(body["request_id"], source=source))

    return callback


def status(name: str, timeline: Timeline, code: int, headers: dict[str, str] | None = None, body: str = "") -> Callable[[Any], tuple[int, dict[str, str], str]]:
    def callback(req: Any) -> tuple[int, dict[str, str], str]:
        timeline.hit(name)
        return code, headers or {}, body

    return callback


def raising(name: str, timeline: Timeline, exc: Exception) -> Callable[[Any], Any]:
    def callback(req: Any) -> Any:
        timeline.hit(name)
        raise exc

    return callback


def connect_timeout() -> requests.ConnectTimeout:
    return requests.ConnectTimeout("HTTPSConnectionPool: Max retries exceeded (connect timeout=3.0)")


def connection_reset() -> requests.ConnectionError:
    return requests.ConnectionError(ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"))


# ---------------------------------------------------------------------------
# Circuit-breaker SLA: failover within 200 ms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    ["502", "504", "connect-timeout-3000ms", "read-timeout", "503-loading", "507-oom", "530-tunnel-down", "521-origin-down"],
)
def test_kaggle_failure_reaches_the_fallback_within_200_ms(
    http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float], failure: str
) -> None:
    mock_gist(http_mock)
    kaggle_callbacks = {
        "502": status("kaggle", timeline, 502, body="<html>Bad gateway</html>"),
        "504": status("kaggle", timeline, 504, body="<html>Gateway time-out</html>"),
        "connect-timeout-3000ms": raising("kaggle", timeline, connect_timeout()),
        "read-timeout": raising("kaggle", timeline, requests.ReadTimeout("read timeout=60")),
        "503-loading": status("kaggle", timeline, 503, body='{"error_code":"model_loading","message":"loading"}'),
        "507-oom": status("kaggle", timeline, 507, body='{"error_code":"gpu_oom","message":"oom"}'),
        "530-tunnel-down": status("kaggle", timeline, 530),
        "521-origin-down": status("kaggle", timeline, 521),
    }
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=kaggle_callbacks[failure])
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline, source="gemini"))

    result = make_client(breaker, sleeps).analyze(request())

    assert result.metrics.tier == "fallback"
    assert result.response.source == "gemini"
    assert timeline.count("kaggle") == 1, "no same-tier retry for a dead node"
    assert sleeps == [], "failover must not back off"
    gap = timeline.first("fallback") - timeline.first("kaggle")
    assert gap <= FAILOVER_BUDGET_S, f"failover took {gap * 1000:.0f} ms"


def test_open_breaker_skips_the_dead_tier_and_answers_in_under_200_ms(
    http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, clock: ManualClock, sleeps: list[float]
) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=raising("kaggle", timeline, connect_timeout()))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline, source="gemini"))
    client = make_client(breaker, sleeps)

    client.analyze(request())
    assert breaker.state(KAGGLE_ANALYZE) == "open"

    started = time.perf_counter()
    second = client.analyze(request())
    elapsed = time.perf_counter() - started
    assert second.metrics.tier == "fallback"
    assert timeline.count("kaggle") == 1, "the open circuit must not touch Kaggle again"
    assert elapsed <= FAILOVER_BUDGET_S

    clock.advance(30.1)
    assert breaker.state(KAGGLE_ANALYZE) == "half_open"
    http_mock.remove(responses.POST, KAGGLE_ANALYZE)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=ok("kaggle", timeline))
    third = client.analyze(request())
    assert third.metrics.tier == "kaggle"
    assert breaker.state(KAGGLE_ANALYZE) == "closed"


def test_the_last_tier_is_always_attempted_even_with_an_open_circuit(
    http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]
) -> None:
    breaker.record_failure(FALLBACK_URL)
    mock_gist(http_mock, status="offline")
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline, source="deterministic"))
    result = make_client(breaker, sleeps).analyze(request())
    assert result.metrics.tier == "fallback"


def test_breaker_state_machine(clock: ManualClock) -> None:
    breaker = CircuitBreaker(cooldown_s=10.0, clock=clock)
    url = "https://x.trycloudflare.com/v1/analyze"
    assert breaker.allow(url) and breaker.state(url) == "closed"
    breaker.record_failure(url)
    assert not breaker.allow(url) and breaker.state(url) == "open"
    clock.advance(9.9)
    assert not breaker.allow(url)
    clock.advance(0.2)
    assert breaker.allow(url) and breaker.state(url) == "half_open"
    breaker.record_failure(url)  # the trial request failed: open again for a full cooldown
    assert breaker.state(url) == "open"
    clock.advance(10.1)
    breaker.record_success(url)
    assert breaker.state(url) == "closed"
    breaker.reset()
    assert breaker.state(url) == "closed"
    with pytest.raises(ValueError):
        CircuitBreaker(cooldown_s=0)


# ---------------------------------------------------------------------------
# Retries with jittered back-off (transient failures only)
# ---------------------------------------------------------------------------


def test_connection_drops_are_retried_with_full_jitter_backoff(
    http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]
) -> None:
    mock_gist(http_mock)
    attempts = {"n": 0}

    def flaky(req: Any) -> Any:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            timeline.hit("drop")
            raise connection_reset()
        return ok("kaggle", timeline)(req)

    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=flaky)
    result = make_client(breaker, sleeps).analyze(request())
    assert result.metrics.tier == "kaggle" and result.metrics.attempts == 3
    assert len(sleeps) == 2
    for attempt, delay in enumerate(sleeps):
        assert 0.0 <= delay <= min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)
    # Deterministic with the seeded RNG: the same seed gives the same delays.
    rng = random.Random(7)
    assert sleeps == [rng.uniform(0.0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**a)) for a in range(2)]
    assert breaker.state(KAGGLE_ANALYZE) == "closed"


def test_429_honors_retry_after(http_mock: responses.RequestsMock, breaker: CircuitBreaker, sleeps: list[float], timeline: Timeline) -> None:
    mock_gist(http_mock)
    calls = {"n": 0}

    def limited(req: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            import json

            return 429, {"Retry-After": "5"}, json.dumps(error_json("rate_limited", "busy"))
        return ok("kaggle", timeline)(req)

    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=limited)
    result = make_client(breaker, sleeps).analyze(request())
    assert result.metrics.tier == "kaggle" and sleeps and sleeps[0] >= 5.0


def test_persistent_429_fails_over_after_the_retry_budget(
    http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]
) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 429, {"Retry-After": "1"}))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline, source="gemini"))
    result = make_client(breaker, sleeps).analyze(request())
    assert result.metrics.tier == "fallback"
    assert timeline.count("kaggle") == 3 and len(sleeps) == 2


def test_client_errors_are_shown_not_failed_over(
    http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]
) -> None:
    import json

    mock_gist(http_mock)
    body = json.dumps(error_json("invalid_payload", "request body failed contract validation", ["image: bad"]))
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 422, body=body))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline))
    with pytest.raises(InferenceError) as info:
        make_client(breaker, sleeps).analyze(request())
    assert info.value.status == 422 and info.value.error_code == "invalid_payload"
    assert "image: bad" in str(info.value)
    assert timeline.count("fallback") == 0
    assert breaker.state(KAGGLE_ANALYZE) == "closed", "a rejected request says nothing about node health"


def test_an_invalid_200_body_fails_over(http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 200, body='{"hello": "world"}'))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline))
    assert make_client(breaker, sleeps).analyze(request()).metrics.tier == "fallback"


def test_refused_connection_fails_over_without_retry(http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    refused = requests.ConnectionError(requests.packages.urllib3.exceptions.MaxRetryError(None, KAGGLE_ANALYZE, NewConnectionError(None, "refused")))  # type: ignore[attr-defined]
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=raising("kaggle", timeline, refused))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline))
    assert make_client(breaker, sleeps).analyze(request()).metrics.tier == "fallback"
    assert timeline.count("kaggle") == 1 and sleeps == []


# ---------------------------------------------------------------------------
# Backends and whole-request outcomes
# ---------------------------------------------------------------------------


def test_tier_lists_per_backend(http_mock: responses.RequestsMock, breaker: CircuitBreaker, sleeps: list[float], live_node: str) -> None:
    mock_gist(http_mock)
    http_mock.add_passthru(live_node)
    auto = make_client(breaker, sleeps, backend="auto")
    auto.settings = auto.settings.with_local_url(live_node)
    assert [t.tier for t in auto.tiers()] == ["kaggle", "local", "fallback"]
    kaggle = make_client(breaker, sleeps, backend="kaggle")
    assert [t.tier for t in kaggle.tiers()] == ["kaggle", "fallback"]
    local = make_client(breaker, sleeps, backend="local")
    local.settings = local.settings.with_local_url(live_node)
    assert [t.tier for t in local.tiers()] == ["local"]


def test_auto_skips_a_local_tier_with_nothing_listening(http_mock: responses.RequestsMock, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    assert [t.tier for t in make_client(breaker, sleeps, backend="auto").tiers()] == ["kaggle", "fallback"]


def test_local_backend_without_a_node_explains_how_to_start_it(breaker: CircuitBreaker, sleeps: list[float]) -> None:
    with pytest.raises(InferenceError, match=r"Local node not running .* scripts\\run-local-gpu.ps1 \(add -Device cpu"):
        make_client(breaker, sleeps, backend="local").analyze(request())


class RecordingSession:
    """Stands in for ``requests.Session``: records each POST's timeout, answers 200."""

    def __init__(self) -> None:
        self.timeouts: list[tuple[float, float]] = []

    def post(self, url: str, *, data: bytes, headers: dict[str, str], timeout: tuple[float, float]) -> Any:
        import json

        self.timeouts.append(timeout)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(analyze_response_json(json.loads(data)["request_id"])).encode()
        return response


def test_the_local_tier_gets_the_long_cpu_friendly_read_timeout(live_node: str, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    session = RecordingSession()
    settings = ClientSettings(backend="local", local_dev_url=live_node, fallback_api_url=None, local_timeout_s=300.0)
    client = InferenceClient(settings, EndpointResolver(settings), session=session, sleep=sleeps.append, breaker=breaker)
    started = time.monotonic()
    client.analyze(request())
    connect, read = session.timeouts[0]
    assert connect == 3.0
    assert 295.0 <= read <= 300.0, "the 120 s request deadline must not cut a CPU node short"
    assert time.monotonic() - started < 5.0


def test_remote_tiers_keep_the_60_s_read_timeout(http_mock: responses.RequestsMock, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    session = RecordingSession()
    settings = ClientSettings(gist_id=GIST_ID, backend="kaggle", fallback_api_url=None)
    InferenceClient(settings, EndpointResolver(settings), session=session, sleep=sleeps.append, breaker=breaker).analyze(request())
    assert session.timeouts == [(3.0, 60.0)]


def test_kaggle_backend_offline_without_fallback(http_mock: responses.RequestsMock, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock, status="offline")
    with pytest.raises(InferenceError, match="Kaggle node is offline and no web fallback"):
        make_client(breaker, sleeps, fallback=None).analyze(request())


def test_every_tier_failing_is_one_readable_error(http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 502))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=status("fallback", timeline, 503))
    with pytest.raises(InferenceError, match=r"every inference endpoint failed - kaggle: HTTP 502.*fallback: HTTP 503"):
        make_client(breaker, sleeps).analyze(request())


def test_kaggle_failure_invalidates_the_cached_endpoint(http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 530))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline))
    client = make_client(breaker, sleeps)
    client.analyze(request())
    before = client.resolver.gist_requests
    client.tiers()
    assert client.resolver.gist_requests == before + 1


def test_tier_callback_and_cancellation(http_mock: responses.RequestsMock, timeline: Timeline, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 502))
    http_mock.add_callback(responses.POST, FALLBACK_URL, callback=ok("fallback", timeline))
    seen: list[str] = []
    make_client(breaker, sleeps).analyze(request(), on_tier=seen.append)
    assert seen == ["kaggle", "fallback"]
    with pytest.raises(CancelledError):
        make_client(breaker, sleeps).analyze(request(), cancelled=lambda: True)


def test_request_deadline_is_enforced(http_mock: responses.RequestsMock, breaker: CircuitBreaker, sleeps: list[float]) -> None:
    mock_gist(http_mock)
    with pytest.raises(InferenceError, match="deadline"):
        make_client(breaker, sleeps, request_deadline_s=0.5).analyze(request())


def test_build_request_with_voice() -> None:
    req = build_request(
        ImagePayload.model_validate(small_jpeg_payload()),
        mode=AnalysisMode.VOICE_QUERY,
        audio_wav=wav_bytes(),
        audio_duration_ms=1000,
        max_new_tokens=128,
    )
    assert req.audio is not None and req.audio.duration_ms == 1000 and req.audio.sample_rate == 16000
    assert req.client is not None and req.client.kind == "desktop"
    assert req.max_new_tokens == 128


@pytest.mark.parametrize(
    ("url", "loopback"),
    [("http://127.0.0.1:8000/v1/analyze", True), ("http://localhost:8000", True), ("http://[::1]:8000", True), (KAGGLE_URL, False), ("https://10.0.0.5", False)],
)
def test_loopback_detection(url: str, loopback: bool) -> None:
    assert net._is_loopback(url) is loopback


def test_listening_probe(live_node: str) -> None:
    assert net._listening(live_node)
    assert not net._listening(closed_local_url())


# ---------------------------------------------------------------------------
# Qt workers: failures arrive as signals, never as exceptions
# ---------------------------------------------------------------------------


def run_worker(worker: InferenceWorker) -> dict[str, list[Any]]:
    got: dict[str, list[Any]] = {"ok": [], "failed": [], "tier": []}
    worker.succeeded.connect(got["ok"].append)
    worker.failed.connect(got["failed"].append)
    worker.tier_changed.connect(got["tier"].append)
    worker.run()  # synchronous: signals are delivered directly on this thread
    return got


def test_worker_emits_the_merged_result(http_mock: responses.RequestsMock, qapp: Any, timeline: Timeline, breaker: CircuitBreaker) -> None:
    from network.schemas import LatencyMetrics

    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=ok("kaggle", timeline))
    settings = ClientSettings(gist_id=GIST_ID, fallback_api_url=FALLBACK_URL, backend="kaggle")
    worker = InferenceWorker(settings, EndpointResolver(settings), request(), LatencyMetrics(capture_ms=12.5, encode_ms=8.0), breaker=breaker)
    got = run_worker(worker)
    assert got["failed"] == [] and got["tier"] == ["kaggle"]
    metrics = got["ok"][0]["metrics"]
    assert (metrics["capture_ms"], metrics["encode_ms"], metrics["tier"]) == (12.5, 8.0, "kaggle")


def test_worker_reports_failures_as_a_signal(http_mock: responses.RequestsMock, qapp: Any, timeline: Timeline, breaker: CircuitBreaker) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=status("kaggle", timeline, 502))
    settings = ClientSettings(gist_id=GIST_ID, fallback_api_url=None, backend="kaggle")
    got = run_worker(InferenceWorker(settings, EndpointResolver(settings), request(), breaker=breaker))
    assert got["ok"] == [] and "every inference endpoint failed" in got["failed"][0]


def test_worker_turns_unexpected_exceptions_into_a_signal(monkeypatch: pytest.MonkeyPatch, qapp: Any, breaker: CircuitBreaker) -> None:
    def explode(self: InferenceClient, *args: Any, **kwargs: Any) -> Any:
        raise KeyError("surprise")

    monkeypatch.setattr(InferenceClient, "analyze", explode)
    settings = ClientSettings(gist_id=GIST_ID)
    got = run_worker(InferenceWorker(settings, EndpointResolver(settings), request(), breaker=breaker))
    assert got["failed"] == ["unexpected error: KeyError: 'surprise'"]


def test_cancelled_worker_stays_silent(http_mock: responses.RequestsMock, qapp: Any, breaker: CircuitBreaker) -> None:
    mock_gist(http_mock)
    settings = ClientSettings(gist_id=GIST_ID, backend="kaggle")
    worker = InferenceWorker(settings, EndpointResolver(settings), request(), breaker=breaker)
    worker.cancel()
    got = run_worker(worker)
    assert got == {"ok": [], "failed": [], "tier": []}


def test_worker_on_a_real_thread_delivers_signals_to_the_gui_thread(
    http_mock: responses.RequestsMock, qapp: Any, timeline: Timeline, breaker: CircuitBreaker
) -> None:
    mock_gist(http_mock)
    http_mock.add_callback(responses.POST, KAGGLE_ANALYZE, callback=ok("kaggle", timeline))
    settings = ClientSettings(gist_id=GIST_ID, backend="kaggle")
    worker = InferenceWorker(settings, EndpointResolver(settings), request(), breaker=breaker)
    results: list[dict[str, Any]] = []
    worker.succeeded.connect(results.append)
    worker.start()
    assert wait_until(lambda: bool(results), 10.0, pump=qapp.processEvents)
    assert worker.wait(5000)


def test_health_check_worker(http_mock: responses.RequestsMock, qapp: Any) -> None:
    from tests.support import FakeEngine

    health = FakeEngine().health(queue_depth=0, uptime_s=1.0).model_dump(mode="json")
    http_mock.get(f"{KAGGLE_URL}/v1/health", json=health)
    settings = ClientSettings(gist_id=GIST_ID, manual_override_url=KAGGLE_URL)
    messages: list[str] = []
    worker = HealthCheckWorker(settings, EndpointResolver(settings))
    worker.finished_with.connect(messages.append)
    worker.run()
    assert messages[0].startswith("OK - fake/engine") and "model loaded: True" in messages[0]

    mock_gist(http_mock, status="offline")
    offline = ClientSettings(gist_id=GIST_ID, fallback_api_url=FALLBACK_URL)
    worker = HealthCheckWorker(offline, EndpointResolver(offline))
    worker.finished_with.connect(messages.append)
    worker.run()
    assert messages[-1].startswith("No live node")

    http_mock.replace(responses.GET, f"{KAGGLE_URL}/v1/health", status=500)
    worker = HealthCheckWorker(settings, EndpointResolver(settings))
    worker.finished_with.connect(messages.append)
    worker.run()
    assert messages[-1].startswith("Health check failed: HTTPError")
