"""Per-tier contract negotiation: history and chat only go to tiers that speak contract 2.2.0."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest
import requests
import responses

from core.config import ClientSettings, EndpointResolver
from network.client import CircuitBreaker, InferenceClient, InferenceError, build_request
from network.negotiation import (
    ContractProbe,
    needs_new_contract,
    needs_probe,
    parse_version,
    probe_url,
    request_body,
    supports_history,
    TierUnreachable,
)
from network.schemas import AnalysisMode, ChatTurn, ImagePayload
from tests.support import (
    FALLBACK_URL,
    GIST_ID,
    GIST_URL,
    KAGGLE_ANALYZE,
    KAGGLE_URL,
    ManualClock,
    analyze_response_json,
    endpoint_record,
    gist_body,
    small_jpeg_payload,
)

HISTORY = [ChatTurn(role="user", text="Why does this crash?"), ChatTurn(role="assistant", text="Index past the end.")]
NODE_HEALTH = f"{KAGGLE_URL}/v1/health"
WEB_STATUS = "https://fallback.test.example/api/tunnel-status"


def screen_request(history: list[ChatTurn] | None = None) -> Any:
    image = ImagePayload.model_validate(small_jpeg_payload())
    return build_request(image, mode=AnalysisMode.DEBUG, prompt="And the fix?", history=history or [])


def chat_request(history: list[ChatTurn] | None = None) -> Any:
    return build_request(None, mode=AnalysisMode.CHAT, prompt="And the fix?", history=history or [])


# -- pure functions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2.2.0", (2, 2, 0)), (" 2.10.3 ", (2, 10, 3)), ("2.2", None), ("2.2.x", None), ("", None), (None, None), (220, None), ("2.2.0-rc1", None)],
)
def test_parse_version(value: object, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(value) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2.2.0", True), ("2.10.0", True), ("3.0.0", True), ("2.1.9", False), ("2.1.0", False), (None, False), ("junk", False)],
)
def test_supports_history_compares_numerically(version: str | None, expected: bool) -> None:
    assert supports_history(version) is expected


def test_probe_url_points_at_health_for_nodes_and_tunnel_status_for_the_web() -> None:
    assert probe_url(KAGGLE_ANALYZE, web=False) == NODE_HEALTH
    assert probe_url("http://127.0.0.1:8000/v1/analyze", web=False) == "http://127.0.0.1:8000/v1/health"
    assert probe_url(FALLBACK_URL, web=True) == WEB_STATUS


def test_a_plain_screen_request_needs_no_probe_and_never_sends_history() -> None:
    request = screen_request()
    assert not needs_probe(request) and not needs_new_contract(request)
    body = json.loads(request_body(request) or b"")
    assert "history" not in body  # byte-compatible with a 2.1.0 tier
    assert body["image"] is not None


def test_request_body_with_history_depends_on_the_tier_version() -> None:
    request = screen_request(HISTORY)
    assert needs_probe(request)
    new = json.loads(request_body(request, tier_version="2.2.0", negotiated=True) or b"")
    assert [turn["role"] for turn in new["history"]] == ["user", "assistant"]
    for version in ("2.1.0", None):
        old = json.loads(request_body(request, tier_version=version, negotiated=True) or b"")
        assert "history" not in old
        assert old["prompt"] == "And the fix?"


def test_chat_is_refused_for_old_or_unknown_tiers() -> None:
    request = chat_request(HISTORY)
    assert needs_new_contract(request)
    assert request_body(request, tier_version="2.1.0", negotiated=True) is None
    assert request_body(request, tier_version=None, negotiated=True) is None
    body = json.loads(request_body(request, tier_version="2.2.0", negotiated=True) or b"")
    assert body["image"] is None and body["mode"] == "chat"


# -- the probe --------------------------------------------------------------------------------


def test_probe_reads_node_and_web_versions_and_caches_them(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(NODE_HEALTH, json={"status": "ok", "contract_version": "2.2.0"})
    http_mock.get(WEB_STATUS, json={"online": False, "contractVersion": "2.1.0"})
    probe = ContractProbe(ttl_s=60.0, clock=clock)
    session = requests.Session()
    assert probe.version(session, KAGGLE_ANALYZE, web=False) == "2.2.0"
    assert probe.version(session, FALLBACK_URL, web=True) == "2.1.0"
    probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 2  # the repeat came from the cache
    clock.advance(61.0)
    probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 3
    probe.reset()
    probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 4


@pytest.mark.parametrize(
    "respond",
    [
        lambda mock: mock.get(NODE_HEALTH, status=404, json={}),
        lambda mock: mock.get(NODE_HEALTH, body="not json"),
        lambda mock: mock.get(NODE_HEALTH, json=["a", "list"]),
        lambda mock: mock.get(NODE_HEALTH, json={"status": "ok"}),
        lambda mock: mock.get(NODE_HEALTH, json={"contract_version": "banana"}),
    ],
)
def test_an_unversioned_tier_reports_no_version_and_is_retried_soon(respond: Any, http_mock: Any, clock: ManualClock) -> None:
    respond(http_mock)
    probe = ContractProbe(ttl_s=60.0, clock=clock)
    session = requests.Session()
    assert probe.version(session, KAGGLE_ANALYZE, web=False) is None
    probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 1  # briefly cached
    clock.advance(6.0)  # a booting node gets another look well before the normal 60 s
    probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 2


@pytest.mark.parametrize(
    "respond",
    [
        lambda mock: mock.get(NODE_HEALTH, body=requests.ConnectionError("down")),
        lambda mock: mock.get(NODE_HEALTH, body=requests.ConnectTimeout("slow")),
        lambda mock: mock.get(NODE_HEALTH, status=502, json={}),
        lambda mock: mock.get(NODE_HEALTH, status=530, json={}),
    ],
)
def test_a_tier_that_does_not_answer_its_probe_is_unreachable_and_remembered_briefly(
    respond: Any, http_mock: Any, clock: ManualClock
) -> None:
    respond(http_mock)
    probe = ContractProbe(ttl_s=60.0, clock=clock)
    session = requests.Session()
    for _ in range(2):
        with pytest.raises(TierUnreachable):
            probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 1  # the second raise came from the cache
    clock.advance(6.0)
    with pytest.raises(TierUnreachable):
        probe.version(session, KAGGLE_ANALYZE, web=False)
    assert len(http_mock.calls) == 2


# -- the client ---------------------------------------------------------------------------------


def make_client(clock: ManualClock, *, backend: str = "kaggle", fallback: str | None = FALLBACK_URL) -> InferenceClient:
    settings = ClientSettings(gist_id=GIST_ID, fallback_api_url=fallback, backend=backend, local_dev_url="http://127.0.0.1:9")
    return InferenceClient(
        settings,
        EndpointResolver(settings),
        sleep=lambda _: None,
        rng=random.Random(1),
        breaker=CircuitBreaker(clock=clock),
        probe=ContractProbe(clock=clock),
    )


def serve_answer(http_mock: Any, url: str, sent: list[dict[str, Any]]) -> None:
    def callback(req: Any) -> tuple[int, dict[str, str], str]:
        body = json.loads(req.body)
        sent.append(body)
        return 200, {}, json.dumps(analyze_response_json(body["request_id"]))

    http_mock.add_callback(responses.POST, url, callback=callback)


def test_history_reaches_a_2_2_0_node(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, json={"status": "ok", "contract_version": "2.2.0"})
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, KAGGLE_ANALYZE, sent)
    result = make_client(clock).analyze(screen_request(HISTORY))
    assert result.metrics.tier == "kaggle"
    assert [turn["text"] for turn in sent[0]["history"]] == ["Why does this crash?", "Index past the end."]


def test_history_is_dropped_for_a_2_1_0_node_and_the_question_is_still_answered(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, json={"status": "ok", "contract_version": "2.1.0"})
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, KAGGLE_ANALYZE, sent)
    make_client(clock).analyze(screen_request(HISTORY))
    assert "history" not in sent[0]


def test_a_plain_request_sends_no_probe_at_all(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, KAGGLE_ANALYZE, sent)
    make_client(clock).analyze(screen_request())
    assert not any(call.request.url == NODE_HEALTH for call in http_mock.calls)
    assert "history" not in sent[0]


def test_chat_skips_an_old_node_and_uses_a_web_fallback_that_speaks_2_2_0(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, json={"status": "ok", "contract_version": "2.1.0"})
    http_mock.get(WEB_STATUS, json={"contractVersion": "2.2.0"})
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, FALLBACK_URL, sent)
    result = make_client(clock).analyze(chat_request(HISTORY))
    assert result.metrics.tier == "fallback"
    assert sent[0]["mode"] == "chat" and sent[0]["image"] is None
    assert not any(call.request.method == "POST" and call.request.url == KAGGLE_ANALYZE for call in http_mock.calls)


def test_chat_with_no_capable_tier_explains_what_to_do(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, json={"status": "ok", "contract_version": "2.1.0"})
    http_mock.get(WEB_STATUS, json={"contractVersion": "2.1.0"})
    with pytest.raises(InferenceError, match="Chat needs a node that speaks contract 2.2.0"):
        make_client(clock).analyze(chat_request())
    assert not any(call.request.method == "POST" for call in http_mock.calls)


def test_a_dead_tier_with_history_fails_over_at_once_and_opens_the_breaker(http_mock: Any, clock: ManualClock) -> None:
    """The probe must not add a second timeout in front of a tier that is already down."""
    import time

    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, body=requests.ConnectTimeout("no connection"))
    http_mock.get(WEB_STATUS, json={"contractVersion": "2.2.0"})
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, FALLBACK_URL, sent)
    client = make_client(clock)
    started = time.perf_counter()
    result = client.analyze(screen_request(HISTORY))
    assert time.perf_counter() - started < 0.2  # the same failover budget as a plain request
    assert result.metrics.tier == "fallback" and len(sent[0]["history"]) == 2
    assert not any(call.request.method == "POST" and call.request.url == KAGGLE_ANALYZE for call in http_mock.calls)
    probes = sum(call.request.url == NODE_HEALTH for call in http_mock.calls)
    client.analyze(screen_request(HISTORY))  # breaker open: the dead tier is not even probed again
    assert sum(call.request.url == NODE_HEALTH for call in http_mock.calls) == probes


def test_chat_with_every_tier_down_reports_the_failures(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, status=530, json={})
    http_mock.get(WEB_STATUS, body=requests.ConnectionError("offline"))
    with pytest.raises(InferenceError, match="every inference endpoint failed"):
        make_client(clock).analyze(chat_request(HISTORY))


# -- web search fields (contract 2.3.0) ---------------------------------------------------------------

from network.negotiation import supports_web, uses_web  # noqa: E402
from network.schemas import WebResult  # noqa: E402

HITS = [WebResult(title="KeyError", url="https://stackoverflow.com/q/1", snippet="Use dict.get.")]


def web_request(*, history: list[ChatTurn] | None = None, results: list[WebResult] | None = None, search: bool = False) -> Any:
    image = ImagePayload.model_validate(small_jpeg_payload())
    return build_request(image, mode=AnalysisMode.EXPLAIN, prompt="why?", history=history or [], web_results=results or [], web_search=search)


@pytest.mark.parametrize(("version", "expected"), [("2.3.0", True), ("2.10.0", True), ("3.0.0", True), ("2.2.9", False), ("2.2.0", False), (None, False), ("x", False)])
def test_supports_web(version: str | None, expected: bool) -> None:
    assert supports_web(version) is expected


def test_uses_web_and_needs_probe() -> None:
    assert not uses_web(web_request()) and not needs_probe(web_request())
    assert uses_web(web_request(results=HITS)) and needs_probe(web_request(results=HITS))
    assert uses_web(web_request(search=True)) and needs_probe(web_request(search=True))


def test_a_plain_request_never_carries_any_optional_key() -> None:
    body = json.loads(request_body(web_request()) or b"")
    for key in ("history", "web_results", "web_search"):
        assert key not in body


def test_web_fields_reach_a_2_3_0_tier_and_empty_ones_stay_out() -> None:
    body = json.loads(request_body(web_request(results=HITS), tier_version="2.3.0", negotiated=True) or b"")
    assert body["web_results"][0]["url"] == "https://stackoverflow.com/q/1" and "history" not in body and "web_search" not in body
    grounded = json.loads(request_body(web_request(search=True), tier_version="2.3.0", negotiated=True) or b"")
    assert grounded["web_search"] is True and "web_results" not in grounded


def test_a_2_2_0_tier_keeps_history_but_never_sees_web_fields() -> None:
    request = web_request(history=HISTORY, results=HITS, search=True)
    body = json.loads(request_body(request, tier_version="2.2.0", negotiated=True) or b"")
    assert len(body["history"]) == 2
    assert "web_results" not in body and "web_search" not in body


@pytest.mark.parametrize("version", ["2.1.0", None])
def test_an_old_or_unknown_tier_gets_a_plain_screen_question(version: str | None) -> None:
    body = json.loads(request_body(web_request(history=HISTORY, results=HITS, search=True), tier_version=version, negotiated=True) or b"")
    for key in ("history", "web_results", "web_search"):
        assert key not in body
    assert body["prompt"] == "why?"


def test_chat_with_web_results_is_refused_by_a_tier_below_2_2_0() -> None:
    request = build_request(None, mode=AnalysisMode.CHAT, prompt="hi", web_results=HITS)
    assert request_body(request, tier_version="2.1.0", negotiated=True) is None
    body = json.loads(request_body(request, tier_version="2.2.0", negotiated=True) or b"")
    assert "web_results" not in body and body["image"] is None


def test_the_client_sends_results_to_a_2_3_0_node_and_strips_them_for_an_older_one(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    http_mock.get(NODE_HEALTH, json={"status": "ok", "contract_version": "2.3.0"})
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, KAGGLE_ANALYZE, sent)
    make_client(clock).analyze(web_request(results=HITS))
    assert sent[0]["web_results"][0]["title"] == "KeyError"
    http_mock.replace(responses.GET, NODE_HEALTH, json={"status": "ok", "contract_version": "2.2.0"})
    make_client(clock).analyze(web_request(results=HITS, search=True))
    assert "web_results" not in sent[1] and "web_search" not in sent[1]


def test_a_plain_question_is_not_probed_even_with_the_web_feature_present(http_mock: Any, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    sent: list[dict[str, Any]] = []
    serve_answer(http_mock, KAGGLE_ANALYZE, sent)
    make_client(clock).analyze(web_request())
    assert not any(call.request.url == NODE_HEALTH for call in http_mock.calls)
    assert set(sent[0]) == {"request_id", "mode", "image", "audio", "prompt", "max_new_tokens", "temperature", "client"}
