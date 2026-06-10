"""Unit tests for jina_discovery — Firecrawl-free baseline discovery.

Perplexity /search finds candidate URLs; the free Jina fetcher reads them; the
result is the SAME Source/DiscoveryResult shape the identity gate + structuring
already consume — at zero Firecrawl credits.

All network is mocked: fetch_perplexity and fetch_article are monkeypatched, so no
spend and no real HTTP.
"""
from __future__ import annotations

import httpx
import pytest

import jina_discovery
from perplexity_enrich import PerplexityResult


def _client() -> httpx.Client:
    # Never actually used (fetch_perplexity is patched) but the signature wants one.
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def _results(*urls: str) -> list[PerplexityResult]:
    return [
        PerplexityResult(title=f"Title {i}", url=u, snippet=f"snippet {i}", date="")
        for i, u in enumerate(urls)
    ]


@pytest.mark.unit
def test_missing_key_short_circuits(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("must not search without a key")

    monkeypatch.setattr(jina_discovery, "fetch_perplexity", boom)
    out = jina_discovery.discover_via_jina(
        _client(), None, "Jane Doe", "Apex", "Austin"
    )
    assert out.sources == () and out.credits_spent == 0


@pytest.mark.unit
def test_empty_name_short_circuits(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("must not search for an empty name")

    monkeypatch.setattr(jina_discovery, "fetch_perplexity", boom)
    out = jina_discovery.discover_via_jina(_client(), "key", "   ", "Apex", "Austin")
    assert out.sources == ()


@pytest.mark.unit
def test_builds_sources_from_fetched_pages(monkeypatch) -> None:
    monkeypatch.setattr(
        jina_discovery, "fetch_perplexity",
        lambda *a, **k: _results("https://firm.com/team/jane", "https://news.com/jane"),
    )
    monkeypatch.setattr(jina_discovery, "fetch_article", lambda url, **k: f"markdown for {url}")

    out = jina_discovery.discover_via_jina(_client(), "key", "Jane Doe", "Apex", "Austin")
    assert out.credits_spent == 0  # never touches Firecrawl
    assert len(out.sources) == 2
    s0 = out.sources[0]
    assert s0.url == "https://firm.com/team/jane"
    assert s0.markdown == "markdown for https://firm.com/team/jane"
    assert s0.description == "snippet 0"          # the Perplexity snippet
    assert s0.relevance >= out.sources[1].relevance  # rank-ordered


@pytest.mark.unit
def test_aggregator_domains_dropped(monkeypatch) -> None:
    monkeypatch.setattr(
        jina_discovery, "fetch_perplexity",
        lambda *a, **k: _results("https://www.zoominfo.com/p/Jane/1", "https://firm.com/jane"),
    )
    monkeypatch.setattr(jina_discovery, "fetch_article", lambda url, **k: "content")

    out = jina_discovery.discover_via_jina(_client(), "key", "Jane Doe", "Apex", "Austin")
    hosts = [s.url for s in out.sources]
    assert hosts == ["https://firm.com/jane"]  # broker page dropped


@pytest.mark.unit
def test_unfetchable_pages_skipped(monkeypatch) -> None:
    """A URL that Jina can't read (returns '') is not a source — precision."""
    monkeypatch.setattr(
        jina_discovery, "fetch_perplexity",
        lambda *a, **k: _results("https://walled.com/jane", "https://firm.com/jane"),
    )
    monkeypatch.setattr(
        jina_discovery, "fetch_article",
        lambda url, **k: "" if "walled" in url else "real content",
    )

    out = jina_discovery.discover_via_jina(_client(), "key", "Jane Doe", "Apex", "Austin")
    assert [s.url for s in out.sources] == ["https://firm.com/jane"]


@pytest.mark.unit
def test_caps_fetches(monkeypatch) -> None:
    """Only the top max_fetch candidates are fetched, bounding Jina calls."""
    fetched: list[str] = []
    monkeypatch.setattr(
        jina_discovery, "fetch_perplexity",
        lambda *a, **k: _results(*[f"https://firm.com/{i}" for i in range(10)]),
    )

    def fake_fetch(url, **k):
        fetched.append(url)
        return "content"

    monkeypatch.setattr(jina_discovery, "fetch_article", fake_fetch)
    out = jina_discovery.discover_via_jina(
        _client(), "key", "Jane Doe", "Apex", "Austin", max_fetch=3
    )
    assert len(fetched) == 3
    assert len(out.sources) == 3


@pytest.mark.unit
def test_duplicate_urls_deduped(monkeypatch) -> None:
    """Repeated search URLs must collapse to one Source — else the person_sources
    (person_id, url) UNIQUE key blows up on persist."""
    monkeypatch.setattr(
        jina_discovery, "fetch_perplexity",
        lambda *a, **k: _results("https://firm.com/jane", "https://firm.com/jane"),
    )
    monkeypatch.setattr(jina_discovery, "fetch_article", lambda url, **k: "content")
    out = jina_discovery.discover_via_jina(_client(), "key", "Jane Doe", "Apex", "Austin")
    assert len(out.sources) == 1


@pytest.mark.unit
def test_no_results_degrades_to_empty(monkeypatch) -> None:
    monkeypatch.setattr(jina_discovery, "fetch_perplexity", lambda *a, **k: [])
    out = jina_discovery.discover_via_jina(_client(), "key", "Jane Doe", "Apex", "Austin")
    assert out.sources == () and out.credits_spent == 0
