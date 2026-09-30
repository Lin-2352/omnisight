"""Web search for answers: free, keyless, official APIs only.

* **Stack Exchange** (``/2.3/search/excerpts`` on Stack Overflow) for errors and code questions.
* **Wikipedia** (REST ``search/page``) for general and factual questions.

Neither needs an account, a key or a payment method. The query is the user's own typed question
(optionally rewritten by the model when the user turned "Smart query" on); screen contents and voice
never become a query. Only the query, a descriptive User-Agent and the normal HTTP headers leave the
PC, and the query text is never written to the log.

Results are short quotations with an https URL (title, at most 200 characters; snippet, at most 600),
cleaned of HTML and control characters here and sanitized again by the node or web route before they
reach a model. A failed provider never blocks an answer: the outcome carries a notice and the question
is answered without the web.
"""

from __future__ import annotations

import html
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import quote

import requests
from pydantic import ValidationError

from core.logger import get_logger
from network.schemas import WebResult

logger = get_logger("search")

MAX_QUERY_CHARS: Final[int] = 200
MAX_RESULTS: Final[int] = 5
PER_PROVIDER: Final[int] = 3
TIMEOUT_S: Final[tuple[float, float]] = (3.0, 4.0)
CACHE_TTL_S: Final[float] = 600.0
CACHE_SIZE: Final[int] = 50
#: Keeps the free public quotas (Stack Exchange allows about 300 unauthenticated calls a day per IP).
MAX_SEARCHES_PER_HOUR: Final[int] = 60
USER_AGENT: Final[str] = "OmniSight/2.3 (desktop client; +https://github.com/Lin-2352/omnisight)"

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f​-‏  ‪-‮⁠-⁤⁦-⁩﻿]")
_URL = re.compile(r"https?://\S+")


REWRITE_PROMPT: Final[str] = (
    "Rewrite the question below as a short web search query of at most 8 keywords. "
    "Reply with the query only: no quotes, no explanation.\n\nQuestion: {question}"
)
MAX_SMART_QUERY_CHARS: Final[int] = 100


class SearchError(Exception):
    """One provider could not answer (network, rate limit, unexpected reply)."""


def clean_text(value: object, limit: int) -> str:
    """Plain one-line text from an API field: tags removed, entities decoded, controls dropped, capped."""
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG.sub(" ", value))
    text = _SPACE.sub(" ", _CONTROL.sub(" ", text)).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip() + "…"


def make_query(text: str) -> str:
    """The search query for a typed question: one line, no URLs or control characters, at most 200 characters."""
    text = _SPACE.sub(" ", _CONTROL.sub(" ", _URL.sub(" ", text))).strip()
    if len(text) <= MAX_QUERY_CHARS:
        return text
    cut = text[:MAX_QUERY_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > MAX_QUERY_CHARS * 0.6 else cut).strip()


def smart_query(ask: Callable[[str], str], question: str) -> str | None:
    """Let the model turn a typed question into a keyword query (opt-in; ``None`` if it cannot).

    ``ask`` sends one prompt to the answering engine and returns its text. Only the typed question goes
    into the prompt, and the reply is cleaned like any other query, so nothing from the screen is involved.
    """
    typed = make_query(question)
    if not typed:
        return None
    try:
        reply = ask(REWRITE_PROMPT.format(question=typed))
    except Exception as exc:  # noqa: BLE001 - any failure means "use the typed question"
        logger.info("smart query failed: %s", type(exc).__name__)
        return None
    lines = [line for line in (reply or "").strip().splitlines() if line.strip()]
    candidate = make_query(lines[0].strip().strip("\"'`*").strip()) if lines else ""
    if len(candidate) > MAX_SMART_QUERY_CHARS:
        candidate = candidate[:MAX_SMART_QUERY_CHARS].rsplit(" ", 1)[0]
    return candidate or None


def _result(title: object, url: str, snippet: object) -> WebResult | None:
    try:
        return WebResult(title=clean_text(title, 200) or url, url=url, snippet=clean_text(snippet, 600))
    except ValidationError:
        return None


class SearchProvider(Protocol):
    name: str

    def search(self, session: requests.Session, query: str, limit: int) -> list[WebResult]: ...


class StackExchangeProvider:
    """Stack Overflow question excerpts (titles and the matching text)."""

    name = "Stack Overflow"
    url = "https://api.stackexchange.com/2.3/search/excerpts"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._paused_until = 0.0

    def search(self, session: requests.Session, query: str, limit: int) -> list[WebResult]:
        if self._clock() < self._paused_until:
            raise SearchError("Stack Overflow asked clients to back off")
        params = {"order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow", "pagesize": str(limit)}
        payload = _get_json(session, self.url, params)
        backoff = payload.get("backoff")
        if isinstance(backoff, int) and backoff > 0:
            self._paused_until = self._clock() + min(backoff, 600)
        if "error_id" in payload:
            # 502 is the throttle violation; any error means this call is not usable.
            raise SearchError(f"Stack Overflow error {payload.get('error_id')}")
        if payload.get("quota_remaining") == 0:
            self._paused_until = self._clock() + 3600
        items = payload.get("items")
        if not isinstance(items, list):
            raise SearchError("unexpected Stack Overflow reply")
        results: list[WebResult] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("question_id"), int):
                continue
            found = _result(item.get("title"), f"https://stackoverflow.com/questions/{item['question_id']}", item.get("excerpt"))
            if found:
                results.append(found)
        return results[:limit]


class WikipediaProvider:
    """English Wikipedia articles (title, description and the matching text)."""

    name = "Wikipedia"
    url = "https://en.wikipedia.org/w/rest.php/v1/search/page"

    def search(self, session: requests.Session, query: str, limit: int) -> list[WebResult]:
        payload = _get_json(session, self.url, {"q": query, "limit": str(limit)})
        pages = payload.get("pages")
        if not isinstance(pages, list):
            raise SearchError("unexpected Wikipedia reply")
        results: list[WebResult] = []
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("key"), str) or not page["key"]:
                continue
            description = clean_text(page.get("description"), 120)
            excerpt = clean_text(page.get("excerpt"), 600)
            snippet = f"{description}. {excerpt}" if description and excerpt else description or excerpt
            found = _result(page.get("title"), f"https://en.wikipedia.org/wiki/{quote(page['key'].replace(' ', '_'), safe='_()%:,')}", snippet)
            if found:
                results.append(found)
        return results[:limit]


def _get_json(session: requests.Session, url: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        response = session.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise SearchError(f"request failed ({type(exc).__name__})") from exc
    if response.status_code == 429:
        raise SearchError("rate limited (HTTP 429)")
    if response.status_code >= 400 and response.status_code != 400:
        raise SearchError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SearchError("reply was not JSON") from exc
    if not isinstance(payload, dict):
        raise SearchError("unexpected reply")
    if response.status_code == 400 and "error_id" not in payload:
        raise SearchError("HTTP 400")
    return payload


@dataclass(frozen=True)
class SearchOutcome:
    """What a search produced, for the request, the answer card and the window's notice."""

    query: str
    results: list[WebResult] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    notice: str = ""
    cached: bool = False


class WebSearch:
    """Runs the providers in parallel, merges, de-duplicates, caches and rate-limits."""

    def __init__(
        self,
        providers: list[SearchProvider] | None = None,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_per_hour: int = MAX_SEARCHES_PER_HOUR,
    ) -> None:
        self.providers: list[SearchProvider] = providers if providers is not None else [StackExchangeProvider(clock), WikipediaProvider()]
        self._session = session or requests.Session()
        self._clock = clock
        self._max_per_hour = max_per_hour
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, SearchOutcome]] = OrderedDict()
        self._recent: list[float] = []

    def search(self, text: str) -> SearchOutcome:
        query = make_query(text)
        if not query:
            return SearchOutcome(query="", notice="There is nothing to search for.")
        key = query.casefold()
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now < hit[0]:
                self._cache.move_to_end(key)
                return SearchOutcome(query, list(hit[1].results), list(hit[1].providers), hit[1].notice, cached=True)
            self._recent = [t for t in self._recent if now - t < 3600]
            if len(self._recent) >= self._max_per_hour:
                return SearchOutcome(query, notice=f"Web search paused: {self._max_per_hour} searches in the last hour (the free services have daily limits).")
            self._recent.append(now)
        outcome = self._run(query)
        with self._lock:
            if outcome.results:
                self._cache[key] = (self._clock() + CACHE_TTL_S, outcome)
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
        return outcome

    def _run(self, query: str) -> SearchOutcome:
        def one(provider: SearchProvider) -> tuple[str, list[WebResult] | None, str]:
            try:
                return provider.name, provider.search(self._session, query, PER_PROVIDER), ""
            except SearchError as exc:
                logger.info("%s search failed: %s", provider.name, exc)
                return provider.name, None, str(exc)
            except Exception as exc:  # noqa: BLE001 - a provider bug must not break answering
                logger.warning("%s search crashed: %s", provider.name, type(exc).__name__)
                return provider.name, None, type(exc).__name__

        with ThreadPoolExecutor(max_workers=max(1, len(self.providers))) as pool:
            replies = list(pool.map(one, self.providers))
        merged: list[WebResult] = []
        seen: set[str] = set()
        used: list[str] = []
        failures: list[str] = []
        depth = max((len(r) for _, r, _ in replies if r), default=0)
        for rank in range(depth):  # interleave so one provider cannot fill every slot
            for _, results, _ in replies:
                if results and rank < len(results) and results[rank].url not in seen and len(merged) < MAX_RESULTS:
                    seen.add(results[rank].url)
                    merged.append(results[rank])
        for name, results, error in replies:
            if results is None:
                failures.append(f"{name}: {error}")
            elif results:
                used.append(name)
        logger.info("search: %d result(s) from %s; %d provider(s) failed", len(merged), ", ".join(used) or "none", len(failures))
        if merged:
            return SearchOutcome(query, merged, used)
        if failures and len(failures) == len(replies):
            return SearchOutcome(query, notice="Web search is unavailable right now (" + "; ".join(failures) + "). Answering without it.")
        return SearchOutcome(query, notice="The web search found nothing for that question. Answering without it.")
