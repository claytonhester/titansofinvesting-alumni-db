"""Unit tests for the GNews adapter — all HTTP is mocked, no subscription touched.

Covers the things that keep news mentions honest and clearly unverified:
1. A 200 with articles becomes news_mention claims the web "In the news" section parses.
2. The date-prefix shape ("YYYY-MM-DD — Headline") the web layer splits back off.
3. Malformed articles and missing fields degrade to nothing, never raise.
"""
from __future__ import annotations

import httpx
import pytest

from gnews_enrich import (
    CLAIM_TYPE,
    EXTRACTION_METHOD,
    NEWS_CONFIDENCE,
    fetch_news,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _articles_payload() -> dict:
    return {
        "totalArticles": 42,
        "articles": [
            {
                "title": "Jane Doe named MD at Apex Capital",
                "description": "Apex announced the promotion on Tuesday.",
                "content": "Full body text...",
                "url": "https://news.example.com/jane-md",
                "publishedAt": "2024-03-01T10:00:00Z",
                "source": {"name": "Example News"},
            },
            {
                "title": "Apex Capital posts record quarter",
                "description": "",
                "content": "Snippet from content field.",
                "url": "https://biz.example.com/apex-q",
                "publishedAt": "2023-11-15T08:30:00Z",
                "source": {"name": "Biz Daily"},
            },
        ],
    }


@pytest.mark.unit
def test_articles_map_to_news_mentions() -> None:
    """A 200 with articles yields one news_mention claim per article, tagged
    unverified (confidence 0.0) and extraction_method='gnews'."""
    client = _client(lambda req: httpx.Response(200, json=_articles_payload()))
    result = fetch_news(client, "key", "Jane Doe")

    assert result.request_count == 1
    assert result.total_articles == 42
    assert len(result.claim_rows) == 2
    for row in result.claim_rows:
        assert row.claim_type == CLAIM_TYPE
        assert row.extraction_method == EXTRACTION_METHOD
        assert row.confidence == NEWS_CONFIDENCE


@pytest.mark.unit
def test_value_carries_date_prefix_and_snippet() -> None:
    """A dated article emits 'YYYY-MM-DD — Headline' with the description as quote."""
    client = _client(lambda req: httpx.Response(200, json=_articles_payload()))
    result = fetch_news(client, "key", "Jane Doe")

    first = result.claim_rows[0]
    assert first.value == "2024-03-01 — Jane Doe named MD at Apex Capital"
    assert first.source_url == "https://news.example.com/jane-md"
    assert first.quote == "Apex announced the promotion on Tuesday."


@pytest.mark.unit
def test_snippet_falls_back_to_content_when_description_blank() -> None:
    """An empty description falls back to the content field for the quote."""
    client = _client(lambda req: httpx.Response(200, json=_articles_payload()))
    result = fetch_news(client, "key", "Jane Doe")
    second = result.claim_rows[1]
    assert second.quote == "Snippet from content field."


@pytest.mark.unit
def test_missing_published_at_yields_bare_headline() -> None:
    """No publishedAt means no date prefix — just the headline as the value."""
    payload = {
        "totalArticles": 1,
        "articles": [
            {
                "title": "Undated mention",
                "description": "x",
                "url": "https://news.example.com/undated",
            }
        ],
    }
    client = _client(lambda req: httpx.Response(200, json=payload))
    result = fetch_news(client, "key", "Jane Doe")
    assert result.claim_rows[0].value == "Undated mention"


@pytest.mark.unit
def test_malformed_articles_are_skipped() -> None:
    """Non-dict entries and articles missing title/url are dropped, not raised."""
    payload = {
        "totalArticles": 3,
        "articles": [
            "not-a-dict",
            {"description": "no title or url"},
            {"title": "Has title but no url", "url": ""},
            {"title": "Good one", "url": "https://news.example.com/good"},
        ],
    }
    client = _client(lambda req: httpx.Response(200, json=payload))
    result = fetch_news(client, "key", "Jane Doe")
    assert len(result.claim_rows) == 1
    assert result.claim_rows[0].source_url == "https://news.example.com/good"


@pytest.mark.unit
def test_query_wraps_name_in_quotes() -> None:
    """The name is quoted so GNews treats it as a phrase, not loose tokens."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"totalArticles": 0, "articles": []})

    fetch_news(_client(handler), "key", "Jane Doe")
    assert seen["q"] == '"Jane Doe"'
    assert seen["token"] == "key"


@pytest.mark.unit
def test_empty_name_short_circuits_without_request() -> None:
    """No name means nothing to search — return empty without touching the network."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("should not issue a request for an empty name")

    result = fetch_news(_client(handler), "key", "   ")
    assert result.claim_rows == ()
    assert result.request_count == 0
    assert result.total_articles == 0


@pytest.mark.unit
def test_network_failure_degrades_to_empty() -> None:
    """A transport error must never raise — it yields no mentions, and the request
    count still reflects that the network was attempted."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = fetch_news(
        _client(handler), "key", "Jane Doe", attempts=2, backoff_base=0.0
    )
    assert result.claim_rows == ()
    assert result.total_articles == 0
    assert result.request_count == 1


@pytest.mark.unit
def test_auth_error_is_empty_and_not_retried() -> None:
    """A 401 (bad/over-quota key) yields empty without raising."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"errors": ["unauthorized"]})

    result = fetch_news(_client(handler), "key", "Jane Doe", attempts=3)
    assert result.claim_rows == ()
    assert calls["n"] == 1  # not retried
