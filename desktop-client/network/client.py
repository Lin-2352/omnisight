"""Inference client with retries, failover, and a Qt worker thread.

Failover order for every request:
    1. the live Kaggle tunnel from the gist (or ``MANUAL_OVERRIDE_URL``);
    2. a local development node at ``http://127.0.0.1:8000``;
    3. ``FALLBACK_API_URL`` (the web showcase's fallback route, same contract).

Per tier:
    * connect timeout 3 s, read timeout 60 s (a 512-token answer takes ~40 s
      on a T4);
    * up to 2 retries with full-jitter exponential backoff for transient
      failures (connection errors, 502/504, 429 honouring ``Retry-After``);
    * immediate failover on a dead or unusable node (503 loading, 507 GPU OOM,
      530 Cloudflare origin down, read timeout);
    * no failover on client errors (400/401/404/405/413/422): the request
      itself is wrong, so the error is shown instead.
The whole request is bounded by a 120 s deadline.
"""

from __future__ import annotations

import ipaddress
import random
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import NewConnectionError
from PyQt6.QtCore import QThread, pyqtSignal
from pydantic import ValidationError

from core.config import ANALYZE_PATH, HEALTH_PATH, ClientSettings, EndpointResolver
from core.logger import get_logger
from network.schemas import (
    CONTRACT_VERSION,
    AnalysisMode,
    AnalyzeRequest,
    AnalyzeResponse,
    AudioPayload,
    ClientInfo,
    ClientResult,
    ErrorResponse,
    HealthResponse,
    ImagePayload,
    LatencyMetrics,
    Tier,
)

logger = get_logger("network")

FAILOVER_NOW: Final[frozenset[int]] = frozenset({503, 507, 530})
RETRY_THEN_FAILOVER: Final[frozenset[int]] = frozenset({500, 502, 504, 520, 521, 522, 523, 524})
CLIENT_ERRORS: Final[frozenset[int]] = frozenset({400, 401, 403, 404, 405, 413, 422})
BACKOFF_BASE_S: Final[float] = 0.5
BACKOFF_CAP_S: Final[float] = 8.0
#: Windows takes ~2 s to report "connection refused" on a closed loopback port
#: (SYN retransmits), so loopback tiers get a quick TCP probe first.
LOOPBACK_PROBE_S: Final[float] = 0.25


def _is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _listening(url: str, timeout_s: float = LOOPBACK_PROBE_S) -> bool:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _refused(exc: requests.RequestException) -> bool:
    """True for errors that a retry cannot fix (refused connection, DNS failure)."""
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError) and not isinstance(exc, requests.ConnectTimeout)


class InferenceError(Exception):
    """A request failed in a way the user should see."""

    def __init__(self, message: str, *, status: int | None = None, error_code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class _FailoverError(Exception):
    """This tier cannot serve the request; try the next one."""


class CancelledError(InferenceError):
    pass


@dataclass(frozen=True)
class TierTarget:
    tier: Tier
    url: str  # full analyze URL


def build_request(
    image: ImagePayload,
    *,
    mode: AnalysisMode = AnalysisMode.DEBUG,
    prompt: str = "",
    audio_wav: bytes | None = None,
    audio_duration_ms: int | None = None,
    audio_sample_rate: int = 16_000,
    max_new_tokens: int = 512,
    platform: str = "win32",
) -> AnalyzeRequest:
    """Assemble a contract-valid ``AnalyzeRequest`` (raises ``ValidationError`` if not)."""
    import base64

    audio = None
    if audio_wav is not None:
        audio = AudioPayload(
            data_b64=base64.b64encode(audio_wav).decode("ascii"),
            sample_rate=audio_sample_rate,
            duration_ms=max(1, int(audio_duration_ms or 0)),
        )
    return AnalyzeRequest(
        mode=mode,
        image=image,
        audio=audio,
        prompt=prompt,
        max_new_tokens=max_new_tokens,
        client=ClientInfo(kind="desktop", version=CONTRACT_VERSION, platform=platform),
    )


def _error_message(response: requests.Response) -> tuple[str, str | None]:
    try:
        error = ErrorResponse.model_validate(response.json())
    except (ValueError, ValidationError):
        text = response.text.strip().replace("\n", " ")[:200]
        return f"HTTP {response.status_code}: {text or response.reason}", None
    details = f" ({'; '.join(error.details[:3])})" if error.details else ""
    return f"{error.message}{details}", error.error_code.value


def _retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class InferenceClient:
    """Synchronous client; one instance per worker thread (``requests.Session`` is not thread-safe)."""

    def __init__(
        self,
        settings: ClientSettings,
        resolver: EndpointResolver,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self._session = session or requests.Session()
        self._sleep = sleep
        self._rng = rng or random.Random()

    def tiers(self) -> list[TierTarget]:
        """Endpoints to try, in order, for the configured backend.

        auto   : Kaggle (or override) -> local GPU node -> web fallback
        kaggle : Kaggle (or override) -> web fallback
        local  : local GPU node only
        """
        backend = self.settings.backend
        targets: list[TierTarget] = []
        seen: set[str] = set()

        def add(tier: Tier, url: str) -> None:
            if url in seen:
                return
            seen.add(url)
            if _is_loopback(url) and not _listening(url):
                logger.info("%s tier skipped: nothing listening at %s", tier, url)
                return
            targets.append(TierTarget(tier, url))

        if backend in ("auto", "kaggle"):
            resolution = self.resolver.resolve_active_endpoint()
            if resolution.url and resolution.source in ("gist", "override"):
                add("override" if resolution.source == "override" else "kaggle", resolution.url.rstrip("/") + ANALYZE_PATH)
            elif resolution.source in ("fallback", "none"):
                logger.info("gist endpoint unusable (%s)", resolution.detail or resolution.source)
        if backend in ("auto", "local"):
            add("local", self.settings.local_dev_url.rstrip("/") + ANALYZE_PATH)
        if backend in ("auto", "kaggle") and self.settings.fallback_api_url:
            add("fallback", self.settings.fallback_api_url)
        return targets

    def analyze(
        self,
        request: AnalyzeRequest,
        *,
        on_tier: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> ClientResult:
        deadline = time.monotonic() + self.settings.request_deadline_s
        body = request.model_dump_json().encode("utf-8")
        failures: list[str] = []
        for target in self.tiers():
            if cancelled():
                raise CancelledError("request cancelled")
            if on_tier is not None:
                on_tier(target.tier)
            logger.info("trying %s tier: %s", target.tier, target.url)
            try:
                response, network_ms, attempts = self._post_with_retries(target, body, deadline, cancelled)
            except _FailoverError as exc:
                failures.append(f"{target.tier}: {exc}")
                logger.warning("%s tier failed: %s", target.tier, exc)
                if target.tier == "kaggle":
                    self.resolver.invalidate()
                continue
            timings = response.timings
            metrics = LatencyMetrics(
                network_ms=round(network_ms, 1),
                server_queue_ms=timings.queue_ms,
                server_ttft_ms=timings.ttft_ms,
                server_total_ms=timings.total_ms,
                tokens_generated=timings.tokens_generated,
                tokens_per_sec=timings.tokens_per_sec,
                tier=target.tier,
                endpoint=target.url,
                attempts=attempts,
            )
            return ClientResult(response=response, metrics=metrics)
        if self.settings.backend == "local" and not failures:
            raise InferenceError(
                f"Local GPU node not running at {self.settings.local_dev_url} - start it with "
                "scripts\\run-local-gpu.ps1 (or switch the backend to Kaggle in the tray menu)."
            )
        if self.settings.backend == "kaggle" and not failures:
            raise InferenceError("The Kaggle node is offline and no web fallback is configured.")
        summary = "; ".join(failures) if failures else "no endpoint configured"
        raise InferenceError(f"every inference endpoint failed - {summary}")

    def _backoff(self, attempt: int, floor_s: float | None) -> float:
        delay = self._rng.uniform(0.0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt)))
        return max(delay, floor_s or 0.0)

    def _post_with_retries(
        self, target: TierTarget, body: bytes, deadline: float, cancelled: Callable[[], bool]
    ) -> tuple[AnalyzeResponse, float, int]:
        last_problem = "no attempt made"
        attempts = 0
        for attempt in range(self.settings.retries + 1):
            if cancelled():
                raise CancelledError("request cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                raise InferenceError(f"request deadline of {self.settings.request_deadline_s:.0f} s exceeded")
            attempts += 1
            started = time.perf_counter()
            try:
                response = self._session.post(
                    target.url,
                    data=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=(self.settings.connect_timeout_s, min(self.settings.read_timeout_s, remaining)),
                )
            except requests.ReadTimeout as exc:
                raise _FailoverError(f"no answer within {self.settings.read_timeout_s:.0f} s") from exc
            except (requests.ConnectionError, requests.ConnectTimeout) as exc:
                if _refused(exc):
                    raise _FailoverError("connection refused or host not found") from exc
                last_problem = f"connection failed ({type(exc).__name__})"
                floor = None
            except requests.RequestException as exc:
                raise _FailoverError(f"request error: {exc}") from exc
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                status = response.status_code
                if status == 200:
                    try:
                        return AnalyzeResponse.model_validate(response.json()), elapsed_ms, attempts
                    except (ValueError, ValidationError) as exc:
                        raise _FailoverError(f"invalid response body: {exc}") from exc
                message, code = _error_message(response)
                if status in CLIENT_ERRORS:
                    raise InferenceError(f"{target.tier} rejected the request: {message}", status=status, error_code=code)
                if status in FAILOVER_NOW:
                    raise _FailoverError(f"HTTP {status}: {message}")
                if status == 429 or status in RETRY_THEN_FAILOVER or status >= 500:
                    last_problem = f"HTTP {status}: {message}"
                    floor = _retry_after(response)
                else:
                    raise _FailoverError(f"unexpected HTTP {status}: {message}")
            if attempt < self.settings.retries:
                delay = self._backoff(attempt, floor)
                if time.monotonic() + delay >= deadline:
                    break
                logger.info("%s attempt %d failed (%s); retrying in %.2f s", target.tier, attempts, last_problem, delay)
                self._sleep(delay)
        raise _FailoverError(f"{last_problem} after {attempts} attempt(s)")

    def health(self, base_url: str) -> HealthResponse:
        response = self._session.get(
            base_url.rstrip("/") + HEALTH_PATH, timeout=(self.settings.connect_timeout_s, 10.0)
        )
        response.raise_for_status()
        return HealthResponse.model_validate(response.json())


class InferenceWorker(QThread):
    """Runs one analysis off the GUI thread and reports back through signals."""

    succeeded = pyqtSignal(dict)  # ClientResult.model_dump(mode="json")
    failed = pyqtSignal(str)
    tier_changed = pyqtSignal(str)

    def __init__(
        self,
        settings: ClientSettings,
        resolver: EndpointResolver,
        request: AnalyzeRequest,
        base_metrics: LatencyMetrics | None = None,
        parent: QThread | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._resolver = resolver
        self._request = request
        self._base_metrics = base_metrics or LatencyMetrics()
        self._cancel = threading.Event()
        self.setObjectName("omnisight-inference")

    def cancel(self) -> None:
        self._cancel.set()
        self.requestInterruption()

    def run(self) -> None:
        client = InferenceClient(self._settings, self._resolver)
        try:
            result = client.analyze(
                self._request,
                on_tier=self.tier_changed.emit,
                cancelled=lambda: self._cancel.is_set() or self.isInterruptionRequested(),
            )
        except CancelledError:
            logger.info("inference cancelled")
            return
        except InferenceError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never let a worker thread die silently
            logger.exception("unexpected inference failure")
            self.failed.emit(f"unexpected error: {type(exc).__name__}: {exc}")
            return
        merged = result.metrics.model_copy(
            update={
                "capture_ms": self._base_metrics.capture_ms,
                "encode_ms": self._base_metrics.encode_ms,
                "audio_ms": self._base_metrics.audio_ms,
            }
        )
        payload = ClientResult(response=result.response, metrics=merged).model_dump(mode="json")
        logger.info(
            "answer via %s: server ttft %.0f ms, total %.0f ms, network %.0f ms, %d tokens",
            merged.tier,
            merged.server_ttft_ms,
            merged.server_total_ms,
            merged.network_ms,
            merged.tokens_generated,
        )
        self.succeeded.emit(payload)


class HealthCheckWorker(QThread):
    """Resolves the endpoint and queries ``/v1/health`` for the settings dialog."""

    finished_with = pyqtSignal(str)

    def __init__(self, settings: ClientSettings, resolver: EndpointResolver, parent: QThread | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._resolver = resolver

    def run(self) -> None:
        try:
            resolution = self._resolver.resolve_active_endpoint(force_refresh=True)
            if not resolution.url or resolution.source == "fallback":
                self.finished_with.emit(f"No live node ({resolution.detail or resolution.source}).")
                return
            health = InferenceClient(self._settings, self._resolver).health(resolution.url)
        except (requests.RequestException, ValidationError, ValueError) as exc:
            self.finished_with.emit(f"Health check failed: {type(exc).__name__}: {exc}")
            return
        warnings = f" Warnings: {'; '.join(health.warnings)}." if health.warnings else ""
        self.finished_with.emit(
            f"{health.status.upper()} - {health.model_id} on {health.gpu_name or 'no GPU'}, "
            f"model loaded: {health.model_loaded}, queue {health.queue_depth}.{warnings}"
        )
