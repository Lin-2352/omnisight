"""Live checks of the free search services (Stack Exchange and Wikipedia), no key or account.

    python -m pytest -m network -s tests/live/test_search_live.py

Opt-in (``network``): two real queries, one per service, well inside the free quotas.
"""

from __future__ import annotations

import pytest
import requests

from core.search import StackExchangeProvider, WebSearch, WikipediaProvider
from network.schemas import WebResult

pytestmark = pytest.mark.network


def test_stack_overflow_answers_a_real_error_question() -> None:
    hits = StackExchangeProvider().search(requests.Session(), "python KeyError dictionary missing key", 3)
    assert hits, "Stack Overflow returned nothing"
    for hit in hits:
        assert hit.url.startswith("https://stackoverflow.com/questions/") and hit.title and "<" not in hit.title + hit.snippet
    print("\n[stackoverflow]", [(h.title[:50], h.url) for h in hits])


def test_wikipedia_answers_a_real_general_question() -> None:
    hits = WikipediaProvider().search(requests.Session(), "associative array data structure", 3)
    assert hits, "Wikipedia returned nothing"
    for hit in hits:
        assert hit.url.startswith("https://en.wikipedia.org/wiki/") and "<" not in hit.title + hit.snippet
    print("\n[wikipedia]", [(h.title, h.url) for h in hits])


def test_the_combined_search_returns_valid_contract_results() -> None:
    outcome = WebSearch().search("how to fix python KeyError in a dictionary")
    assert outcome.results and set(outcome.providers) <= {"Stack Overflow", "Wikipedia"}
    assert all(isinstance(r, WebResult) for r in outcome.results) and len(outcome.results) <= 5
    print("\n[combined]", outcome.providers, [r.title[:40] for r in outcome.results])
