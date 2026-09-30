"""Which contract version does each inference tier speak?

Contract 2.2.0 added ``history`` (conversation memory) and the image-less ``chat`` mode. The
models forbid unknown fields, so a node or web deployment still on 2.1.0 answers a request that
carries them with HTTP 422. The client therefore asks a tier what it speaks before it sends one of
the new fields:

* a node (Kaggle, override, local) reports ``contract_version`` in ``GET /v1/health``;
* the web fallback reports ``contractVersion`` in ``GET /api/tunnel-status`` on the same origin.

Answers are cached for a minute. A tier that cannot be probed is treated as old: the question is
still answered (history is dropped) and a chat turn, which an old tier cannot express, skips it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import requests

from core.config import HEALTH_PATH
from core.logger import get_logger
from network.schemas import AnalysisMode, AnalyzeRequest

logger = get_logger("negotiation")

#: First contract version that understands ``history`` and ``chat``.
HISTORY_CONTRACT: Final[tuple[int, int, int]] = (2, 2, 0)
#: First contract version that understands ``web_results`` and ``web_search``.
WEB_CONTRACT: Final[tuple[int, int, int]] = (2, 3, 0)
#: Request fields a tier older than the one that introduced them rejects, even when empty.
OPTIONAL_FIELDS: Final[frozenset[str]] = frozenset({"history", "web_results", "web_search"})
WEB_FIELDS: Final[frozenset[str]] = frozenset({"web_results", "web_search"})
#: How long a probe answer is trusted.
PROBE_TTL_S: Final[float] = 60.0
#: A tier that could not be probed (still booting, blip) is asked again soon.
PROBE_FAILURE_TTL_S: Final[float] = 5.0
#: Probes must never hold up a request for long.
PROBE_TIMEOUT_S: Final[tuple[float, float]] = (3.0, 5.0)
WEB_STATUS_PATH: Final[str] = "/api/tunnel-status"
#: A probe answered with one of these means the tier is down (same set the analyze call fails over on).
UNREACHABLE_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504, 507, 521, 522, 523, 524, 530})


class TierUnreachable(Exception):
    """The tier did not answer its probe at all: go to the next one now instead of posting into a dead tier."""


def parse_version(value: object) -> tuple[int, int, int] | None:
    """``"2.2.0"`` -> ``(2, 2, 0)``; anything else -> ``None``."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(".")
    if len(parts) != 3 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def supports_history(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= HISTORY_CONTRACT


def supports_web(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= WEB_CONTRACT


def uses_web(request: AnalyzeRequest) -> bool:
    return bool(request.web_results) or request.web_search


def needs_new_contract(request: AnalyzeRequest) -> bool:
    """True when the request cannot be expressed in contract 2.1.0 at all."""
    return request.mode is AnalysisMode.CHAT or request.image is None


def request_body(request: AnalyzeRequest, *, tier_version: str | None = None, negotiated: bool = False) -> bytes | None:
    """Serialize ``request`` for one tier, or ``None`` if that tier cannot take it.

    Optional fields are never sent empty, so a request that uses no new feature is byte-compatible
    with a 2.1.0 tier and needs no probe. A field the tier does not know is left out, not emptied:
    tiers reject unknown keys. ``negotiated`` says the tier's version is known (``tier_version`` may
    still be ``None`` for "could not be probed").
    """
    if not needs_probe(request):
        return request.model_dump_json(exclude=set(OPTIONAL_FIELDS)).encode("utf-8")
    if negotiated and supports_web(tier_version):
        return request.model_dump_json(exclude=_empty_optional(request)).encode("utf-8")
    if negotiated and supports_history(tier_version):
        return request.model_dump_json(exclude=set(WEB_FIELDS) | _empty_optional(request)).encode("utf-8")
    if needs_new_contract(request):
        return None
    return request.model_dump_json(exclude=set(OPTIONAL_FIELDS)).encode("utf-8")


def _empty_optional(request: AnalyzeRequest) -> set[str]:
    """Optional fields that carry nothing for this request (left out so older tiers never see them)."""
    empty: set[str] = set()
    if not request.history:
        empty.add("history")
    if not request.web_results:
        empty.add("web_results")
    if not request.web_search:
        empty.add("web_search")
    return empty


def needs_probe(request: AnalyzeRequest) -> bool:
    return bool(request.history) or uses_web(request) or needs_new_contract(request)


def probe_url(analyze_url: str, *, web: bool) -> str:
    """Where to ask a tier for its contract version, derived from its analyze URL."""
    parts = urlsplit(analyze_url)
    if web:
        return urlunsplit((parts.scheme, parts.netloc, WEB_STATUS_PATH, "", ""))
    return urlunsplit((parts.scheme, parts.netloc, HEALTH_PATH, "", ""))


class ContractProbe:
    """Caches each tier's contract version (thread-safe; shared by every worker)."""

    def __init__(self, ttl_s: float = PROBE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str | None, str | None]] = {}  # url -> (expires_at, version, down_reason)

    def version(self, session: requests.Session, analyze_url: str, *, web: bool) -> str | None:
        """The tier's contract version, ``None`` if it answered without one (treated as old).

        Raises ``TierUnreachable`` when the tier did not answer at all.
        """
        url = probe_url(analyze_url, web=web)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(url)
        if cached is not None and now < cached[0]:
            if cached[2] is not None:
                raise TierUnreachable(cached[2])
            return cached[1]
        version: str | None = None
        down: str | None = None
        try:
            version = self._fetch(session, url, web=web)
        except TierUnreachable as exc:
            down = str(exc)
        ttl = self.ttl_s if version is not None else min(self.ttl_s, PROBE_FAILURE_TTL_S)
        with self._lock:
            self._cache[url] = (self._clock() + ttl, version, down)
        if down is not None:
            raise TierUnreachable(down)
        return version

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _fetch(session: requests.Session, url: str, *, web: bool) -> str | None:
        try:
            response = session.get(url, timeout=PROBE_TIMEOUT_S)
        except requests.RequestException as exc:
            logger.info("contract probe %s failed: %s", url, type(exc).__name__)
            raise TierUnreachable(f"no answer to its version probe ({type(exc).__name__})") from exc
        if response.status_code in UNREACHABLE_STATUSES:
            logger.info("contract probe %s -> HTTP %s", url, response.status_code)
            raise TierUnreachable(f"HTTP {response.status_code} on its version probe")
        try:
            if response.status_code != 200:
                logger.info("contract probe %s -> HTTP %s", url, response.status_code)
                return None
            payload = response.json()
        except ValueError:
            return None
        value = payload.get("contractVersion" if web else "contract_version") if isinstance(payload, dict) else None
        return value if parse_version(value) is not None else None


#: Shared by every ``InferenceClient`` that is not given its own probe.
DEFAULT_PROBE: Final[ContractProbe] = ContractProbe()
