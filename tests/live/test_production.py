"""Live checks against the deployed web showcase (Requirement 4: the hosted link).

    pytest -m network -s tests/live

Opt-in (``network``): these hit https://omnisight-nine.vercel.app and spend at most two
Gemini calls. Override the target with ``OMNISIGHT_LIVE_URL``.
"""

from __future__ import annotations

import base64
import os
import re
import time
import uuid

import pytest
import requests

import omnisight_contracts as oc
from tests.security.test_secret_exposure import SECRET_PATTERNS
from tests.support import REPO_ROOT

pytestmark = pytest.mark.network

BASE = os.environ.get("OMNISIGHT_LIVE_URL", "https://omnisight-nine.vercel.app").rstrip("/")
TIMEOUT = 60


def preset_body(name: str = "numpy-indexerror") -> dict[str, object]:
    data = (REPO_ROOT / "web-showcase" / "public" / "presets" / f"{name}.jpg").read_bytes()
    return {
        "request_id": str(uuid.uuid4()),
        "mode": "debug",
        "image": {"mime": "image/jpeg", "data_b64": base64.b64encode(data).decode(), "width": 1280, "height": 720},
        "max_new_tokens": 256,
        "client": {"kind": "test", "version": oc.CONTRACT_VERSION, "platform": "pytest-live"},
    }


@pytest.fixture(scope="module")
def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "omnisight-live-tests"
    return s


@pytest.mark.parametrize("path", ["/", "/docs", "/favicon.ico", "/presets/numpy-indexerror.jpg"])
def test_pages_and_assets_are_served(session: requests.Session, path: str) -> None:
    response = session.get(BASE + path, timeout=TIMEOUT)
    assert response.status_code == 200


def test_security_headers(session: requests.Session) -> None:
    headers = session.get(BASE + "/", timeout=TIMEOUT).headers
    csp = headers["Content-Security-Policy"]
    for directive in ("default-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "object-src 'none'"):
        assert directive in csp
    assert "unsafe-eval" not in csp
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "max-age=" in headers["Strict-Transport-Security"]


def test_tunnel_status_contract(session: requests.Session) -> None:
    status = session.get(BASE + "/api/tunnel-status", timeout=TIMEOUT).json()
    assert set(status) >= {"online", "url", "lastPing", "latencyMs", "model", "gpuDevice", "reason"}
    if status["online"]:
        assert re.match(r"^https://[a-z0-9-]+\.trycloudflare\.com$", status["url"])
    else:
        assert status["url"] is None


def test_a_preset_is_answered_by_a_live_engine(session: requests.Session) -> None:
    started = time.perf_counter()
    response = session.post(BASE + "/api/fallback-infer", json=preset_body(), timeout=TIMEOUT)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text[:300]
    answer = oc.AnalyzeResponse.model_validate(response.json())
    tier = response.headers["x-omnisight-tier"]
    assert tier in {"kaggle", "gemini", "deterministic"}
    assert answer.source == tier
    assert answer.markdown and answer.summary
    print(f"\n[live] tier={tier} model={answer.model_id} {elapsed:.1f} s trace={response.headers.get('x-omnisight-trace')}")
    assert elapsed < 55


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"image": {"mime": "image/jpeg", "data_b64": "not*base64", "width": 1, "height": 1}}, 422),
        ({"image": {"mime": "image/jpeg", "data_b64": "QUJD", "width": 1, "height": 1}, "injected": True}, 422),
    ],
    ids=["bad-base64", "extra-field"],
)
def test_validation_happens_before_any_engine(session: requests.Session, payload: dict[str, object], status: int) -> None:
    response = session.post(BASE + "/api/fallback-infer", json=payload, timeout=TIMEOUT)
    assert response.status_code == status
    assert oc.ErrorResponse.model_validate(response.json()).error_code is oc.ErrorCode.INVALID_PAYLOAD


def test_get_on_the_inference_route_is_405(session: requests.Session) -> None:
    assert session.get(BASE + "/api/fallback-infer", timeout=TIMEOUT).status_code == 405


def test_no_secrets_in_the_served_html_or_javascript(session: requests.Session) -> None:
    html = session.get(BASE + "/", timeout=TIMEOUT).text
    scripts = sorted(set(re.findall(r'src="(/_next/static/[^"]+\.js)"', html)))
    assert scripts, "no script chunks found in the page"
    findings = [f"html: {name}" for name, pattern in SECRET_PATTERNS.items() if pattern.search(html)]
    for path in scripts:
        text = session.get(BASE + path, timeout=TIMEOUT).text
        findings += [f"{path}: {name}" for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
        for server_only in ("GEMINI_API_KEY", "x-goog-api-key", "generativelanguage.googleapis.com"):
            if server_only in text:
                findings.append(f"{path}: {server_only}")
    assert findings == []


def test_desktop_client_default_fallback_reaches_production() -> None:
    pytest.importorskip("PyQt6")
    from core.config import DEFAULT_FALLBACK_API_URL, ClientSettings, EndpointResolver
    from network.client import CircuitBreaker, InferenceClient, build_request
    from network.schemas import ImagePayload

    settings = ClientSettings.from_environment({}, load_files=False).with_override("https://offline-test.trycloudflare.com")
    assert settings.fallback_api_url == DEFAULT_FALLBACK_API_URL
    if not DEFAULT_FALLBACK_API_URL.startswith(BASE):
        pytest.skip("OMNISIGHT_LIVE_URL points somewhere other than the client's default")
    client = InferenceClient(settings.with_backend("kaggle"), EndpointResolver(settings), breaker=CircuitBreaker())
    body = preset_body("cpp-segfault")
    result = client.analyze(build_request(ImagePayload.model_validate(body["image"])))
    assert result.metrics.tier == "fallback"
    assert result.response.source in {"kaggle", "gemini", "deterministic"}
