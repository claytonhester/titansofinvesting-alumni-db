"""Firecrawl-free baseline discovery: Perplexity /search → free Jina fetch.

The baseline career-source discovery used to run on Firecrawl (search + scrape,
~15 credits/person) for EVERY person — the single biggest reason a full run needs
~110k credits. This produces the SAME ``Source`` / ``DiscoveryResult`` the identity
gate + structuring already consume, but from:

    Perplexity /search (name + employer)   -> candidate URLs  (perplexity_enrich)
        -> drop people-search / data-broker domains            (news_score)
        -> free Jina Reader fetch of each survivor             (http_fetch)
        -> Source(url, title, snippet, markdown, relevance)

So discovery costs **zero Firecrawl credits**; Firecrawl is reserved for the
high-signal deep path (see deep_gate). Whole-web search surfaces bios, leadership
listings, and interviews — good career seeds — and the downstream Sonnet identity
gate remains the real filter, so a thin/ambiguous page is rejected there, not here.

Never raises: a missing key, empty name, no results, or an unfetchable page yields
an empty/short result so a bulk loop degrades instead of aborting.
"""
from __future__ import annotations

import httpx

from discovery import DiscoveryResult, Source
from http_fetch import fetch_article
from news_score import is_aggregator_domain
from perplexity_enrich import build_query, fetch_perplexity

# Rank-based relevance: the top Perplexity hit scores highest, decaying by position.
# The Sonnet identity gate is the real filter downstream, so a rough order suffices.
_RELEVANCE_TOP = 1.0
_RELEVANCE_STEP = 0.1
_RELEVANCE_FLOOR = 0.5


def _relevance(rank: int) -> float:
    return max(_RELEVANCE_FLOOR, _RELEVANCE_TOP - rank * _RELEVANCE_STEP)


def discover_via_jina(
    http: httpx.Client,
    perplexity_key: str | None,
    full_name: str,
    employer: str,
    city: str,
    *,
    max_results: int = 8,
    max_fetch: int = 5,
    timeout: float = 45.0,
) -> DiscoveryResult:
    """Find candidate career sources via Perplexity and read them with free Jina.
    Returns a DiscoveryResult with ``credits_spent == 0`` (no Firecrawl). Empty when
    the key or name is missing, or nothing fetched. Never raises."""
    if not perplexity_key or not full_name.strip():
        return DiscoveryResult(full_name=full_name, sources=(), queries=(), credits_spent=0)

    results = fetch_perplexity(
        http, perplexity_key, full_name, employer=employer, max_results=max_results
    )
    # Drop aggregators, then de-dupe by URL so the persisted person_sources never hits
    # its (person_id, url) UNIQUE key on a repeated search result.
    kept: list = []
    seen: set[str] = set()
    for r in results:
        if is_aggregator_domain(r.url) or r.url in seen:
            continue
        seen.add(r.url)
        kept.append(r)

    sources: list[Source] = []
    for rank, r in enumerate(kept[:max_fetch]):
        markdown = fetch_article(r.url, timeout=timeout)
        if not markdown:
            continue  # unfetchable (paywall/Cloudflare) — not a usable source
        sources.append(
            Source(
                url=r.url,
                title=r.title,
                description=r.snippet,
                markdown=markdown,
                relevance=_relevance(rank),
            )
        )

    return DiscoveryResult(
        full_name=full_name,
        sources=tuple(sources),
        queries=(build_query(full_name, employer),),
        credits_spent=0,
    )
