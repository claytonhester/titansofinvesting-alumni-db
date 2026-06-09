"""Unit tests for Phase 2 discovery — pure logic, no live Firecrawl.

The expensive part (search/scrape) is exercised through a tiny fake client so we
can assert the cost-saving contract without paying a credit:

  * search runs across ALL angle queries (cheap, no scrape)
  * candidates are deduped by DOMAIN, best relevance winning
  * only the top `max_sources` survivors are scraped (one credit each)
  * a survivor that scrapes to empty markdown is dropped but still billed
"""
from __future__ import annotations

import pytest

from discovery import (
    SEARCH_CREDITS_PER_CALL,
    Source,
    _as_candidate,
    _domain,
    build_queries,
    discover,
)
from firecrawl.v2.types import SearchResultWeb

# discover() bills every search call plus every scrape. The query set is fixed, so
# the search portion is constant; tests assert SEARCH + scrapes.
_SEARCH_COST = len(build_queries("n", "c", "ci")) * SEARCH_CREDITS_PER_CALL


class _FakeWebResults:
    def __init__(self, web: list[SearchResultWeb]) -> None:
        self.web = web


class _FakeFirecrawl:
    """Records every scraped URL so tests can assert we only pay for keepers."""

    def __init__(self, hits_per_query: list[SearchResultWeb], markdown_for=None) -> None:
        self._hits = hits_per_query
        self._markdown_for = markdown_for or (lambda url: f"body of {url}")
        self.scraped: list[str] = []

    def search(self, query: str, limit: int = 4):  # noqa: ARG002 - same hits each query
        return _FakeWebResults(self._hits[:limit])

    def scrape(self, url: str, formats=None, only_main_content=True):  # noqa: ARG002
        self.scraped.append(url)
        from firecrawl.v2.types import Document

        doc = Document.__new__(Document)
        object.__setattr__(doc, "markdown", self._markdown_for(url))
        return doc


def _hit(url: str, title: str = "t", description: str = "d") -> SearchResultWeb:
    item = SearchResultWeb.__new__(SearchResultWeb)
    object.__setattr__(item, "url", url)
    object.__setattr__(item, "title", title)
    object.__setattr__(item, "description", description)
    return item


@pytest.mark.unit
def test_domain_strips_www() -> None:
    assert _domain("https://www.Example.com/x") == "example.com"
    assert _domain("https://sub.example.com/y") == "sub.example.com"


@pytest.mark.unit
def test_as_candidate_from_search_hit_scores_trusted_higher() -> None:
    trusted = _as_candidate(_hit("https://linkedin.com/in/jane"), "Jane Doe")
    plain = _as_candidate(_hit("https://randomblog.io/jane"), "Jane Doe")
    assert trusted is not None and plain is not None
    assert trusted.relevance > plain.relevance


@pytest.mark.unit
def test_as_candidate_rejects_unknown_item() -> None:
    assert _as_candidate(object(), "Jane Doe") is None


@pytest.mark.unit
def test_discover_dedupes_by_domain_keeping_best_relevance() -> None:
    """Two hits on the same domain collapse to one candidate -> one scrape."""
    hits = [
        _hit("https://linkedin.com/in/jane", title="Jane Doe profile"),
        _hit("https://linkedin.com/pub/jane", title="unrelated"),
    ]
    client = _FakeFirecrawl(hits)
    result = discover(client, "Jane Doe", "Acme", "Austin")
    assert len(result.sources) == 1
    assert result.credits_spent == _SEARCH_COST + 1
    assert len(client.scraped) == 1


@pytest.mark.unit
def test_discover_scrapes_only_top_max_sources() -> None:
    """More distinct domains than max_sources -> we pay for max_sources only."""
    hits = [_hit(f"https://site{i}.com/jane") for i in range(12)]
    client = _FakeFirecrawl(hits)
    result = discover(client, "Jane Doe", "Acme", "Austin", max_sources=3)
    assert result.credits_spent == _SEARCH_COST + 3
    assert len(client.scraped) == 3
    assert len(result.sources) == 3


@pytest.mark.unit
def test_discover_bills_empty_scrape_but_drops_source() -> None:
    """An empty scrape still costs a credit but yields no Source."""
    hits = [_hit("https://site1.com/jane"), _hit("https://site2.com/jane")]
    client = _FakeFirecrawl(hits, markdown_for=lambda url: "" if "site1" in url else "body")
    result = discover(client, "Jane Doe", "Acme", "Austin")
    assert result.credits_spent == _SEARCH_COST + 2
    assert len(result.sources) == 1
    assert all(isinstance(s, Source) for s in result.sources)
