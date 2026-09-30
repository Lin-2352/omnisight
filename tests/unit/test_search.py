"""Free web search (``core.search``): parsing, cleaning, caps, merging, caching, limits and failure handling.

All HTTP is mocked with ``responses``; nothing touches the network and nothing sleeps.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
import requests
import responses
from responses import matchers

from core.search import (
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    USER_AGENT,
    SearchError,
    StackExchangeProvider,
    WebSearch,
    WikipediaProvider,
    clean_text,
    make_query,
)
from tests.support import ManualClock

SE_URL = StackExchangeProvider.url
WIKI_URL = WikipediaProvider.url


def se_item(question_id: int = 1, title: str = "How to fix &quot;KeyError&quot;?", excerpt: str = 'Use <span class="highlight">dict</span>.get &amp; a default') -> dict[str, Any]:
    return {"item_type": "question", "question_id": question_id, "title": title, "excerpt": excerpt, "is_answered": True}


def wiki_page(key: str = "Associative_array", title: str = "Associative array", excerpt: str = 'an <span class="searchmatch">associative</span> array', description: str = "data structure") -> dict[str, Any]:
    return {"id": 1, "key": key, "title": title, "excerpt": excerpt, "description": description}


def serve(mock: Any, se: Any = None, wiki: Any = None, se_status: int = 200, wiki_status: int = 200) -> None:
    mock.get(SE_URL, json=se if se is not None else {"items": [se_item()], "quota_remaining": 250}, status=se_status)
    mock.get(WIKI_URL, json=wiki if wiki is not None else {"pages": [wiki_page()]}, status=wiki_status)


# -- text cleaning --------------------------------------------------------------------------------


def test_clean_text_removes_tags_entities_controls_and_caps() -> None:
    assert clean_text('a <b>bold</b> &amp; "x"‮\x00 &#39;y&#39;', 100) == "a bold & \"x\" 'y'"
    assert clean_text(None, 10) == "" and clean_text(5, 10) == ""
    long = clean_text("word " * 500, 50)
    assert len(long) <= 50 and long.endswith("…")
    assert clean_text("x" * 100, 40).endswith("…") and len(clean_text("x" * 100, 40)) == 40


def test_make_query_is_one_short_line_without_urls_or_controls() -> None:
    assert make_query("  why\n does\tthis  crash?  ") == "why does this crash?"
    assert make_query("see https://example.com/secret?token=abc for context") == "see for context"
    assert make_query("a‮b\x00c") == "a b c"
    assert make_query("") == "" and make_query("   ") == ""
    long = make_query("word " * 200)
    assert len(long) <= MAX_QUERY_CHARS and not long.endswith(" ")
    assert len(make_query("x" * 500)) == MAX_QUERY_CHARS


# -- providers ----------------------------------------------------------------------------------------


def test_stack_overflow_results_are_cleaned_and_linked_to_the_question(http_mock: Any) -> None:
    http_mock.get(SE_URL, json={"items": [se_item(42)], "quota_remaining": 10})
    (hit,) = StackExchangeProvider().search(requests.Session(), "keyerror", 3)
    assert hit.title == 'How to fix "KeyError"?'
    assert hit.url == "https://stackoverflow.com/questions/42"
    assert hit.snippet == "Use dict .get & a default"


def test_stack_overflow_is_asked_politely_for_the_right_thing(http_mock: Any) -> None:
    http_mock.get(
        SE_URL,
        json={"items": []},
        match=[matchers.query_param_matcher({"order": "desc", "sort": "relevance", "q": "python keyerror", "site": "stackoverflow", "pagesize": "3"})],
    )
    StackExchangeProvider().search(requests.Session(), "python keyerror", 3)
    assert http_mock.calls[0].request.headers["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    "payload",
    [{"items": "no"}, {"error_id": 502, "error_message": "throttle_violation"}, {}],
)
def test_stack_overflow_errors_become_search_errors(http_mock: Any, payload: dict[str, Any]) -> None:
    http_mock.get(SE_URL, json=payload, status=400 if "error_id" in payload else 200)
    with pytest.raises(SearchError):
        StackExchangeProvider().search(requests.Session(), "q", 3)


def test_stack_overflow_items_without_an_id_are_skipped(http_mock: Any) -> None:
    http_mock.get(SE_URL, json={"items": [{"title": "no id"}, "junk", se_item(7)]})
    assert [r.url for r in StackExchangeProvider().search(requests.Session(), "q", 3)] == ["https://stackoverflow.com/questions/7"]


def test_stack_overflow_backoff_and_exhausted_quota_pause_the_provider(http_mock: Any, clock: ManualClock) -> None:
    provider = StackExchangeProvider(clock)
    http_mock.get(SE_URL, json={"items": [se_item()], "backoff": 30, "quota_remaining": 5})
    session = requests.Session()
    assert provider.search(session, "q", 3)
    with pytest.raises(SearchError, match="back off"):
        provider.search(session, "q", 3)
    assert len(http_mock.calls) == 1
    clock.advance(31)
    http_mock.replace(responses.GET, SE_URL, json={"items": [se_item()], "quota_remaining": 0})
    assert provider.search(session, "q", 3)
    with pytest.raises(SearchError, match="back off"):  # quota exhausted: rest for an hour
        provider.search(session, "q", 3)
    clock.advance(3601)
    http_mock.replace(responses.GET, SE_URL, json={"items": [se_item()], "quota_remaining": 100})
    assert provider.search(session, "q", 3)


def test_wikipedia_results_combine_description_and_excerpt_and_encode_the_key(http_mock: Any) -> None:
    http_mock.get(WIKI_URL, json={"pages": [wiki_page(key="Dictionary_(data_structure)"), wiki_page(key="C++", title="C++", description="", excerpt="a language")]})
    first, second = WikipediaProvider().search(requests.Session(), "dict", 3)
    assert first.url == "https://en.wikipedia.org/wiki/Dictionary_(data_structure)"
    assert first.snippet == "data structure. an associative array"
    assert second.url == "https://en.wikipedia.org/wiki/C%2B%2B" and second.snippet == "a language"


@pytest.mark.parametrize("payload", [{"pages": "no"}, {}, []])
def test_wikipedia_bad_replies_are_search_errors(http_mock: Any, payload: Any) -> None:
    http_mock.get(WIKI_URL, json=payload)
    with pytest.raises(SearchError):
        WikipediaProvider().search(requests.Session(), "q", 3)


def test_wikipedia_pages_without_a_key_are_skipped(http_mock: Any) -> None:
    http_mock.get(WIKI_URL, json={"pages": [{"title": "no key"}, wiki_page(key="")]})
    assert WikipediaProvider().search(requests.Session(), "q", 3) == []


@pytest.mark.parametrize(
    "respond",
    [
        lambda mock: mock.get(WIKI_URL, status=429, json={}),
        lambda mock: mock.get(WIKI_URL, status=503, json={}),
        lambda mock: mock.get(WIKI_URL, body="not json"),
        lambda mock: mock.get(WIKI_URL, body=requests.ConnectTimeout("slow")),
        lambda mock: mock.get(WIKI_URL, body=requests.ConnectionError("down")),
    ],
)
def test_network_failures_are_search_errors(respond: Any, http_mock: Any) -> None:
    respond(http_mock)
    with pytest.raises(SearchError):
        WikipediaProvider().search(requests.Session(), "q", 3)


def test_a_result_with_an_unusable_url_is_dropped_not_fatal(http_mock: Any) -> None:
    http_mock.get(WIKI_URL, json={"pages": [wiki_page(key="Good"), wiki_page(key="x" * 600)]})
    assert [r.url for r in WikipediaProvider().search(requests.Session(), "q", 3)] == ["https://en.wikipedia.org/wiki/Good"]


# -- the combined search -----------------------------------------------------------------------------


def test_results_from_both_providers_are_interleaved_and_capped(http_mock: Any) -> None:
    serve(http_mock, se={"items": [se_item(i) for i in range(1, 4)]}, wiki={"pages": [wiki_page(key=f"A{i}") for i in range(3)]})
    outcome = WebSearch(session=requests.Session()).search("how do dicts work")
    assert len(outcome.results) == MAX_RESULTS == 5
    assert [r.url.split("/")[2] for r in outcome.results] == ["stackoverflow.com", "en.wikipedia.org"] * 2 + ["stackoverflow.com"]
    assert outcome.providers == ["Stack Overflow", "Wikipedia"] and outcome.notice == "" and outcome.query == "how do dicts work"


def test_duplicate_urls_are_merged(http_mock: Any) -> None:
    serve(http_mock, se={"items": [se_item(1)]}, wiki={"pages": [wiki_page(key="A")]})
    outcome = WebSearch(providers=[StackExchangeProvider(), StackExchangeProvider()], session=requests.Session()).search("q")
    assert len(outcome.results) == 1


def test_one_failing_provider_does_not_hide_the_other(http_mock: Any) -> None:
    serve(http_mock, wiki_status=503, wiki={})
    outcome = WebSearch(session=requests.Session()).search("q")
    assert outcome.providers == ["Stack Overflow"] and len(outcome.results) == 1 and outcome.notice == ""


def test_every_provider_failing_gives_a_notice_and_no_results(http_mock: Any) -> None:
    serve(http_mock, se_status=500, se={}, wiki_status=503, wiki={})
    outcome = WebSearch(session=requests.Session()).search("q")
    assert outcome.results == [] and "unavailable" in outcome.notice and "Answering without it" in outcome.notice


def test_no_hits_is_reported_differently_from_a_failure(http_mock: Any) -> None:
    serve(http_mock, se={"items": []}, wiki={"pages": []})
    outcome = WebSearch(session=requests.Session()).search("zzzz qqqq")
    assert outcome.results == [] and "found nothing" in outcome.notice


def test_a_provider_bug_is_contained() -> None:
    class Broken:
        name = "Broken"

        def search(self, session: Any, query: str, limit: int) -> Any:
            raise RuntimeError("boom")

    outcome = WebSearch(providers=[Broken()], session=requests.Session()).search("q")  # type: ignore[list-item]
    assert outcome.results == [] and "unavailable" in outcome.notice


def test_an_empty_question_searches_nothing(http_mock: Any) -> None:
    outcome = WebSearch(session=requests.Session()).search("   ")
    assert outcome.results == [] and "nothing to search" in outcome.notice and not http_mock.calls


def test_answers_are_cached_then_expire(http_mock: Any, clock: ManualClock) -> None:
    serve(http_mock)
    search = WebSearch(session=requests.Session(), clock=clock)
    first = search.search("Why does this crash?")
    again = search.search("why does this   crash?")  # same question, different case and spacing
    assert again.cached and again.results == first.results and len(http_mock.calls) == 2
    clock.advance(601)
    assert not search.search("why does this crash?").cached and len(http_mock.calls) == 4


def test_failures_are_not_cached(http_mock: Any, clock: ManualClock) -> None:
    serve(http_mock, se_status=500, se={}, wiki_status=500, wiki={})
    search = WebSearch(session=requests.Session(), clock=clock)
    search.search("q")
    search.search("q")
    assert len(http_mock.calls) == 4


def test_the_cache_is_bounded(http_mock: Any, clock: ManualClock) -> None:
    serve(http_mock)
    search = WebSearch(session=requests.Session(), clock=clock)
    for i in range(60):
        search.search(f"question {i}")
    assert len(search._cache) == 50


def test_searches_per_hour_are_limited_to_protect_the_free_quotas(http_mock: Any, clock: ManualClock) -> None:
    serve(http_mock)
    search = WebSearch(session=requests.Session(), clock=clock, max_per_hour=3)
    for i in range(3):
        assert search.search(f"q{i}").results
    blocked = search.search("q3")
    assert blocked.results == [] and "paused" in blocked.notice
    clock.advance(3601)
    assert search.search("q4").results


def test_the_query_text_is_never_logged(http_mock: Any, caplog: pytest.LogCaptureFixture) -> None:
    serve(http_mock, se_status=500, se={})
    secret = "my-private-question-about-project-falcon"
    with caplog.at_level(logging.DEBUG, logger="omnisight"):
        WebSearch(session=requests.Session()).search(secret)
    assert secret not in caplog.text and "falcon" not in caplog.text


def test_only_the_two_official_hosts_are_contacted_and_no_credentials_are_sent(http_mock: Any) -> None:
    serve(http_mock)
    WebSearch(session=requests.Session()).search("python keyerror")
    hosts = {call.request.url.split("/")[2] for call in http_mock.calls}
    assert hosts == {"api.stackexchange.com", "en.wikipedia.org"}
    for call in http_mock.calls:
        assert call.request.url.startswith("https://")
        assert set(call.request.headers) <= {"User-Agent", "Accept", "Accept-Encoding", "Connection"}
        assert "python%20keyerror" in call.request.url or "python+keyerror" in call.request.url
