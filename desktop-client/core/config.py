"""Client configuration and live-endpoint discovery.

Configuration sources, highest priority first:
    1. real process environment variables;
    2. ``%APPDATA%/OmniSight/.env`` (per-user settings);
    3. ``<repo>/.env`` (developer checkout).

Endpoint discovery (``resolve_active_endpoint``):
    1. ``MANUAL_OVERRIDE_URL`` if set;
    2. the in-memory cache if younger than the TTL (30 s);
    3. the public gist via ``GET https://api.github.com/gists/{id}`` (2.5 s
       timeout, ETag conditional requests); a record that is ``offline`` or
       older than ``STALE_AFTER_S`` is treated as dead;
    4. ``FALLBACK_API_URL``.

The gist is public: ``GITHUB_TOKEN`` is optional and only raises GitHub's
API rate limit (60 -> 5000 requests/hour). A token with no scopes is enough.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import requests
from pydantic import ValidationError

from core.logger import get_logger
from network.schemas import CONTRACT_VERSION, EndpointRecord, EndpointResolution

logger = get_logger("config")

DEFAULT_GIST_ID: Final[str] = "fc8433475a1a2d424ad8e06724e59731"
GIST_FILENAME: Final[str] = "omnisight-endpoint.json"
GITHUB_API_BASE: Final[str] = "https://api.github.com"
LOCAL_DEV_URL: Final[str] = "http://127.0.0.1:8000"
ANALYZE_PATH: Final[str] = "/v1/analyze"
HEALTH_PATH: Final[str] = "/v1/health"
GIST_TIMEOUT_S: Final[float] = 2.5
#: Heartbeat (60 s) x 2 + the 300 s CDN cache some readers of the gist see.
STALE_AFTER_S: Final[float] = 420.0

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


def user_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "OmniSight" if appdata else Path.home() / ".omnisight"


def load_env_files() -> list[Path]:
    """Load ``.env`` files without overriding real environment variables."""
    from dotenv import load_dotenv

    loaded = []
    for candidate in (user_config_dir() / ".env", REPO_ROOT / ".env"):
        if candidate.is_file() and load_dotenv(candidate, override=False):
            loaded.append(candidate)
    return loaded


def _first(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _url_or_none(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{name} must start with http:// or https:// (got {value!r})")
    return value.rstrip("/")


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _first(env, name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number (got {value!r})") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive (got {value!r})")
    return parsed


@dataclass(frozen=True)
class ClientSettings:
    gist_id: str = DEFAULT_GIST_ID
    github_token: str | None = None
    fallback_api_url: str | None = None
    manual_override_url: str | None = None
    local_dev_url: str = LOCAL_DEV_URL
    cache_ttl_s: float = 30.0
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 60.0
    request_deadline_s: float = 120.0
    retries: int = 2
    max_new_tokens: int = 512
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *, load_files: bool = True) -> ClientSettings:
        if environ is None:
            if load_files:
                load_env_files()
            environ = os.environ
        env = environ
        max_tokens_raw = _first(env, "OMNISIGHT_MAX_NEW_TOKENS")
        max_new_tokens = int(max_tokens_raw) if max_tokens_raw else 512
        if not 16 <= max_new_tokens <= 512:
            raise ValueError("OMNISIGHT_MAX_NEW_TOKENS must be between 16 and 512")
        return cls(
            gist_id=_first(env, "GITHUB_GIST_ID", "OMNISIGHT_GIST_ID") or DEFAULT_GIST_ID,
            github_token=_first(env, "OMNISIGHT_CLIENT_GITHUB_TOKEN", "GITHUB_TOKEN"),
            fallback_api_url=_url_or_none("FALLBACK_API_URL", _first(env, "FALLBACK_API_URL")),
            manual_override_url=_url_or_none(
                "MANUAL_OVERRIDE_URL", _first(env, "MANUAL_OVERRIDE_URL", "OMNISIGHT_ENDPOINT_OVERRIDE")
            ),
            local_dev_url=_url_or_none("OMNISIGHT_LOCAL_DEV_URL", _first(env, "OMNISIGHT_LOCAL_DEV_URL")) or LOCAL_DEV_URL,
            cache_ttl_s=_float(env, "OMNISIGHT_ENDPOINT_CACHE_TTL_S", 30.0),
            connect_timeout_s=_float(env, "OMNISIGHT_CONNECT_TIMEOUT_S", 3.0),
            read_timeout_s=_float(env, "OMNISIGHT_REQUEST_TIMEOUT_S", 60.0),
            request_deadline_s=_float(env, "OMNISIGHT_REQUEST_DEADLINE_S", 120.0),
            max_new_tokens=max_new_tokens,
            log_level=(_first(env, "OMNISIGHT_LOG_LEVEL") or "INFO").upper(),
        )

    def with_override(self, url: str | None) -> ClientSettings:
        """Copy with a session override URL (empty or None clears it)."""
        cleaned = (url or "").strip()
        return replace(self, manual_override_url=_url_or_none("override URL", cleaned) if cleaned else None)


class EndpointResolver:
    """Thread-safe live-endpoint discovery with a TTL cache and ETag revalidation."""

    def __init__(
        self,
        settings: ClientSettings,
        *,
        api_base: str = GITHUB_API_BASE,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._api_base = api_base.rstrip("/")
        self._session = session or requests.Session()
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: EndpointResolution | None = None
        self._cached_at = 0.0
        self._etag: str | None = None
        self._record: EndpointRecord | None = None
        self.gist_requests = 0

    @property
    def settings(self) -> ClientSettings:
        return self._settings

    def update_settings(self, settings: ClientSettings) -> None:
        with self._lock:
            self._settings = settings
            self._cached = None

    def invalidate(self) -> None:
        """Forget the cached resolution (e.g. after the cached endpoint failed)."""
        with self._lock:
            self._cached = None

    def resolve_active_endpoint(self, force_refresh: bool = False) -> EndpointResolution:
        with self._lock:
            settings = self._settings
            if settings.manual_override_url:
                return EndpointResolution(url=settings.manual_override_url, source="override", detail="MANUAL_OVERRIDE_URL")
            now = self._clock()
            if not force_refresh and self._cached is not None and now - self._cached_at < settings.cache_ttl_s:
                return self._cached
            resolution = self._query_gist(settings)
            self._cached = resolution
            self._cached_at = self._clock()
            return resolution

    # -- internals (called with the lock held) ---------------------------------

    def _fallback(self, settings: ClientSettings, detail: str, record: EndpointRecord | None = None) -> EndpointResolution:
        if settings.fallback_api_url:
            return EndpointResolution(
                url=settings.fallback_api_url,
                source="fallback",
                gist_status=record.status if record else None,
                record_age_s=round(record.age_seconds(), 1) if record else None,
                stale=record is not None,
                detail=detail,
            )
        return EndpointResolution(
            url=None,
            source="none",
            gist_status=record.status if record else None,
            record_age_s=round(record.age_seconds(), 1) if record else None,
            stale=record is not None,
            detail=detail,
        )

    def _query_gist(self, settings: ClientSettings) -> EndpointResolution:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"omnisight-desktop/{CONTRACT_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        if self._etag and self._record is not None:
            headers["If-None-Match"] = self._etag
        url = f"{self._api_base}/gists/{settings.gist_id}"
        self.gist_requests += 1
        try:
            response = self._session.get(url, headers=headers, timeout=GIST_TIMEOUT_S)
        except requests.RequestException as exc:
            logger.warning("gist lookup failed: %s", exc)
            return self._fallback(settings, f"gist unreachable: {type(exc).__name__}", self._record)

        if response.status_code == 304 and self._record is not None:
            record = self._record
        elif response.status_code == 200:
            try:
                files = response.json().get("files", {})
                content = files[GIST_FILENAME]["content"]
                record = EndpointRecord.model_validate_json(content)
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                logger.warning("gist has no valid %s: %s", GIST_FILENAME, exc)
                return self._fallback(settings, f"gist record invalid: {type(exc).__name__}")
            self._record = record
            self._etag = response.headers.get("ETag")
        else:
            remaining = response.headers.get("X-RateLimit-Remaining")
            logger.warning("gist lookup returned HTTP %d (rate limit remaining: %s)", response.status_code, remaining)
            return self._fallback(settings, f"gist HTTP {response.status_code}", self._record)

        age = record.age_seconds()
        if record.is_stale(STALE_AFTER_S):
            detail = f"node {record.status}, record {age:.0f} s old"
            logger.info("gist endpoint unusable (%s); using fallback", detail)
            return self._fallback(settings, detail, record)
        return EndpointResolution(
            url=record.base_url,
            source="gist",
            gist_status=record.status,
            record_age_s=round(age, 1),
            detail=f"{record.model} on {record.gpu_device}",
        )


_default_resolver: EndpointResolver | None = None
_default_lock = threading.Lock()


def default_resolver(settings: ClientSettings | None = None) -> EndpointResolver:
    """Process-wide resolver (created on first use)."""
    global _default_resolver
    with _default_lock:
        if _default_resolver is None:
            _default_resolver = EndpointResolver(settings or ClientSettings.from_environment())
        elif settings is not None:
            _default_resolver.update_settings(settings)
        return _default_resolver


def resolve_active_endpoint(force_refresh: bool = False) -> EndpointResolution:
    """Resolve the live inference endpoint with the process-wide resolver."""
    return default_resolver().resolve_active_endpoint(force_refresh=force_refresh)
