"""Live-endpoint discovery through the public GitHub gist (``core.config.EndpointResolver``).

The GitHub API is mocked with ``responses``; time is a manual clock, so TTL expiry
is tested without waiting.
"""

from __future__ import annotations

import logging
import threading

import pytest
import requests
import responses

from core.config import ClientSettings, EndpointResolver
from tests.support import FALLBACK_URL, GIST_ID, GIST_URL, KAGGLE_URL, ManualClock, endpoint_record, gist_body

ETAG = '"a1b2c3"'


def make_resolver(clock: ManualClock, **overrides: object) -> EndpointResolver:
    settings = ClientSettings(gist_id=GIST_ID, fallback_api_url=FALLBACK_URL, **overrides)  # type: ignore[arg-type]
    return EndpointResolver(settings, clock=clock)


def gist_calls(mock: responses.RequestsMock) -> int:
    return sum(1 for call in mock.calls if call.request.url == GIST_URL)


def test_the_gist_url_is_resolved_and_used(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()), headers={"ETag": ETAG})
    resolution = make_resolver(clock).resolve_active_endpoint()
    assert resolution.url == KAGGLE_URL
    assert resolution.source == "gist"
    assert resolution.gist_status == "online"
    assert resolution.record_age_s is not None and resolution.record_age_s < 60
    assert "Tesla T4" in resolution.detail


def test_a_second_lookup_within_30_s_is_served_from_memory(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()), headers={"ETag": ETAG})
    resolver = make_resolver(clock)
    first = resolver.resolve_active_endpoint()
    clock.advance(29.9)
    second = resolver.resolve_active_endpoint()
    assert second is first
    assert gist_calls(http_mock) == 1 and resolver.gist_requests == 1


def test_after_the_ttl_the_gist_is_revalidated_with_the_etag(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()), headers={"ETag": ETAG})
    resolver = make_resolver(clock)
    resolver.resolve_active_endpoint()
    http_mock.replace(responses.GET, GIST_URL, status=304)
    clock.advance(30.1)
    again = resolver.resolve_active_endpoint()
    assert again.url == KAGGLE_URL and again.source == "gist"
    assert gist_calls(http_mock) == 2
    assert http_mock.calls[-1].request.headers["If-None-Match"] == ETAG


def test_force_refresh_and_invalidate_bypass_the_cache(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()), headers={"ETag": ETAG})
    resolver = make_resolver(clock)
    resolver.resolve_active_endpoint()
    resolver.resolve_active_endpoint(force_refresh=True)
    resolver.invalidate()
    resolver.resolve_active_endpoint()
    resolver.update_settings(resolver.settings)
    resolver.resolve_active_endpoint()
    assert gist_calls(http_mock) == 4


def test_a_stale_online_record_logs_a_warning_and_falls_back(
    http_mock: responses.RequestsMock, clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record(age_s=11 * 60)))
    with caplog.at_level(logging.INFO, logger="omnisight"):
        resolution = make_resolver(clock).resolve_active_endpoint()
    assert resolution.source == "fallback" and resolution.url == FALLBACK_URL
    assert resolution.stale and resolution.record_age_s is not None and resolution.record_age_s >= 600
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "stale" in r.getMessage()]
    assert warnings, [r.getMessage() for r in caplog.records]


def test_an_offline_record_is_normal_and_not_a_warning(
    http_mock: responses.RequestsMock, clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record(status="offline", age_s=30)))
    with caplog.at_level(logging.INFO, logger="omnisight"):
        resolution = make_resolver(clock).resolve_active_endpoint()
    assert resolution.source == "fallback" and resolution.gist_status == "offline"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("status", [403, 404, 500])
def test_github_errors_fall_back_without_raising(http_mock: responses.RequestsMock, clock: ManualClock, status: int) -> None:
    http_mock.get(GIST_URL, status=status, headers={"X-RateLimit-Remaining": "0"})
    resolution = make_resolver(clock).resolve_active_endpoint()
    assert resolution.source == "fallback"
    assert f"gist HTTP {status}" in resolution.detail


def test_rate_limit_after_a_good_read_keeps_the_last_record_details(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()), headers={"ETag": ETAG})
    resolver = make_resolver(clock)
    resolver.resolve_active_endpoint()
    http_mock.replace(responses.GET, GIST_URL, status=403)
    clock.advance(31)
    resolution = resolver.resolve_active_endpoint()
    assert resolution.source == "fallback" and resolution.gist_status == "online"


@pytest.mark.parametrize(
    "body",
    [
        {"files": {}},
        {"files": {"omnisight-endpoint.json": {"content": "{not json"}}},
        {"files": {"omnisight-endpoint.json": {"content": '{"omnisight_endpoint": "https://evil.example.com"}'}}},
        gist_body(endpoint_record(url="https://attacker.example.com")),
        ["not", "an", "object"],
    ],
    ids=["no-file", "bad-json", "incomplete", "foreign-host", "wrong-shape"],
)
def test_invalid_gist_content_falls_back(http_mock: responses.RequestsMock, clock: ManualClock, body: object) -> None:
    http_mock.get(GIST_URL, json=body)
    resolution = make_resolver(clock).resolve_active_endpoint()
    assert resolution.source == "fallback"
    assert "gist record invalid" in resolution.detail


def test_network_failure_falls_back(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, body=requests.ConnectionError("network down"))
    resolution = make_resolver(clock).resolve_active_endpoint()
    assert resolution.source == "fallback" and "gist unreachable" in resolution.detail


def test_without_a_fallback_the_source_is_none(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record(status="offline")))
    resolver = EndpointResolver(ClientSettings(gist_id=GIST_ID, fallback_api_url=None), clock=clock)
    resolution = resolver.resolve_active_endpoint()
    assert resolution.url is None and resolution.source == "none"


def test_the_manual_override_wins_without_any_http(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    resolver = make_resolver(clock, manual_override_url="https://override.trycloudflare.com")
    resolution = resolver.resolve_active_endpoint()
    assert (resolution.source, resolution.url) == ("override", "https://override.trycloudflare.com")
    assert len(http_mock.calls) == 0


def test_request_headers_identify_the_client_and_send_the_optional_token(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    make_resolver(clock, github_token="read-only-token").resolve_active_endpoint()
    headers = http_mock.calls[0].request.headers
    assert headers["Authorization"] == "Bearer read-only-token"
    assert headers["User-Agent"].startswith("omnisight-desktop/")
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_concurrent_lookups_share_one_request(http_mock: responses.RequestsMock, clock: ManualClock) -> None:
    http_mock.get(GIST_URL, json=gist_body(endpoint_record()))
    resolver = make_resolver(clock)
    results: list[str | None] = []
    threads = [threading.Thread(target=lambda: results.append(resolver.resolve_active_endpoint().url)) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert results == [KAGGLE_URL] * 10
    assert gist_calls(http_mock) == 1
