"""Phase 2 discovery: Firecrawl-first search + scrape for one alumnus.

Firecrawl does the hard work (find + render + fetch clean markdown). The query
building and result ranking here are plain heuristics — no model — mirroring
fire-enrich's smart-search-tool.

Cost discipline: we SEARCH first (cheap metadata only — no scrape_options), then
dedupe by domain and rank across ALL queries, and SCRAPE ONLY the top keepers.
The old path scraped every result of every query inline (~20 scrapes/person)
before dedup, paying for >12 pages we then threw away. Searching first and
scraping only the survivors cuts the per-person scrape count to `max_sources`.
Public data only; no auth, no logins.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

from firecrawl import Firecrawl
from firecrawl.v2.types import Document, SearchResultWeb
from firecrawl.v2.utils.error_handler import (
    BadRequestError,
    PaymentRequiredError,
    UnauthorizedError,
    WebsiteNotSupportedError,
)

# Run-level fatals: a broken run that will never succeed on retry (no credits,
# bad key, malformed request). Let them abort loudly instead of masquerading as
# "no sources found" (which silently produces a $0/0-source run).
_FATAL_FIRECRAWL_ERRORS = (
    PaymentRequiredError,
    UnauthorizedError,
    BadRequestError,
)
# WebsiteNotSupportedError is NOT run-level fatal — it's per-URL. Firecrawl
# rejects auth-walled / unsupported sites (LinkedIn chief among them). Such a
# keeper must be skipped, not allowed to abort the whole person's enrichment.
# Kept out of the retry set (it's deterministic — retrying never helps) and
# caught at the scrape call site so the remaining keepers still get scraped.
_SCRAPE_RERAISE = _FATAL_FIRECRAWL_ERRORS + (WebsiteNotSupportedError,)

# Sites that tend to hold authoritative professional info. Used only to rank /
# dedupe results — never to fabricate. A LinkedIn *public* page is fine to read;
# we never log in.
_TRUSTED = (
    "linkedin.com", "bloomberg.com", "sec.gov", "crunchbase.com",
    "forbes.com", "wsj.com", "reuters.com", "businesswire.com",
    "prnewswire.com", "github.com", "medium.com", "substack.com",
)
# Aggregators / low-signal domains we penalise (but don't drop outright).
_NOISY = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "pinterest.com", "youtube.com", "rocketreach.co", "zoominfo.com",
    "spokeo.com", "whitepages.com", "mylife.com",
)


@dataclass(frozen=True)
class Source:
    """One fetched, evidence-bearing page. markdown is what Claude will read."""

    url: str
    title: str
    description: str
    markdown: str
    relevance: float


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything one person's discovery produced, plus the real credit cost.

    `credits_spent` is the number of pages we actually SCRAPED (one Firecrawl
    credit each) — the dominant, controllable cost. Search calls are cheap and
    not counted here; the authoritative dollar figure comes from the live
    get_credit_usage() delta recorded by the cost log."""

    full_name: str
    sources: tuple[Source, ...]
    queries: tuple[str, ...]
    credits_spent: int


def build_queries(
    full_name: str,
    company: str,
    city: str,
) -> list[str]:
    """Heuristic, model-free query set covering the 'Quick' research angles:
    career history, current role, public writing/talks, news. Append
    company/city only where it sharpens the angle (fire-enrich enhanceQuery)."""
    name = full_name.strip()
    return [
        f'"{name}" {company}',                         # identity + current role
        f'"{name}" {company} career history background',
        f'"{name}" {city} executive profile',           # location-disambiguated
        f'"{name}" interview OR podcast OR article author',  # public writing/talks
        f'"{name}" {company} news announcement',         # news
    ]


@dataclass(frozen=True)
class _Candidate:
    """A search hit BEFORE scraping — metadata only, costs no scrape credit."""

    url: str
    title: str
    description: str
    relevance: float


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _relevance(url: str, full_name: str, title: str, description: str) -> float:
    """fire-enrich calculateRelevance: base 0.5, boost trusted/name-match,
    penalise social aggregators. Pure heuristic ordering, not truth."""
    score = 0.5
    dom = _domain(url)
    if any(dom == t or dom.endswith("." + t) for t in _TRUSTED):
        score += 0.3
    if any(dom == n or dom.endswith("." + n) for n in _NOISY):
        score -= 0.3
    haystack = f"{title} {description}".lower()
    last = full_name.strip().split()[-1].lower() if full_name.strip() else ""
    if last and last in haystack:
        score += 0.1
    return max(0.0, min(1.0, score))


def _as_candidate(item: object, full_name: str) -> _Candidate | None:
    """Turn a search hit (SearchResultWeb metadata) into a rankable candidate.
    No markdown yet — scraping happens later, only for the keepers."""
    if isinstance(item, SearchResultWeb):
        url, title, description = item.url, item.title or "", item.description or ""
    elif isinstance(item, Document):
        # Defensive: if a caller ever passes scrape_options, read metadata anyway.
        meta = item.metadata
        url = (getattr(meta, "source_url", None) or getattr(meta, "url", None) or "") if meta else ""
        title = (getattr(meta, "title", None) or "") if meta else ""
        description = (getattr(meta, "description", None) or "") if meta else ""
    else:
        return None
    if not url:
        return None
    return _Candidate(
        url=url,
        title=title,
        description=description,
        relevance=_relevance(url, full_name, title, description),
    )


def discover(
    client: Firecrawl,
    full_name: str,
    company: str,
    city: str,
    *,
    per_query_limit: int = 4,
    max_sources: int = 8,
    backoff_base: float = 1.5,
) -> DiscoveryResult:
    """Search all angle queries (cheap, no scrape), dedupe by domain + rank, then
    scrape ONLY the top `max_sources` survivors. Resilient: one failing query or
    one failing scrape is skipped rather than killing the whole person."""
    queries = build_queries(full_name, company, city)

    # Phase A — cheap discovery: collect candidates, one per domain, best wins.
    by_domain: dict[str, _Candidate] = {}
    for query in queries:
        for item in _search_with_retry(client, query, per_query_limit, backoff_base):
            cand = _as_candidate(item, full_name)
            if cand is None:
                continue
            dom = _domain(cand.url)
            existing = by_domain.get(dom)
            if existing is None or cand.relevance > existing.relevance:
                by_domain[dom] = cand

    keepers = sorted(by_domain.values(), key=lambda c: c.relevance, reverse=True)[:max_sources]

    # Phase B — pay only for the keepers: scrape each top candidate once.
    sources: list[Source] = []
    credits_spent = 0
    for cand in keepers:
        try:
            markdown = _scrape_with_retry(client, cand.url, backoff_base)
        except WebsiteNotSupportedError:
            # Firecrawl can't render this site (e.g. LinkedIn auth-wall). Skip the
            # source — no content, no credit charged — and keep the person going.
            continue
        credits_spent += 1  # one Firecrawl credit per scrape, success or not
        if not markdown.strip():
            continue
        sources.append(
            Source(
                url=cand.url,
                title=cand.title,
                description=cand.description,
                markdown=markdown,
                relevance=cand.relevance,
            )
        )

    return DiscoveryResult(
        full_name=full_name,
        sources=tuple(sources),
        queries=tuple(queries),
        credits_spent=credits_spent,
    )


def _search_with_retry(
    client: Firecrawl,
    query: str,
    limit: int,
    backoff_base: float,
    attempts: int = 3,
) -> list[object]:
    """Cheap metadata search (no scrape_options). Exponential backoff on transient
    failures; return [] when exhausted so a single bad query can't abort the whole
    person. Fatal errors (no credits, bad key, bad request) re-raise immediately —
    retrying can't fix them and swallowing them hides a broken run as empty."""
    for attempt in range(attempts):
        try:
            data = client.search(query, limit=limit)
            return list(data.web or [])
        except _FATAL_FIRECRAWL_ERRORS:
            raise
        except Exception:
            if attempt == attempts - 1:
                return []
            time.sleep(backoff_base ** attempt)
    return []


def _scrape_with_retry(
    client: Firecrawl,
    url: str,
    backoff_base: float,
    attempts: int = 3,
) -> str:
    """Scrape one keeper to clean markdown. Same resilience contract as search:
    transient failures back off then yield "" (skip the page); fatal errors
    re-raise so a broken run can't masquerade as 'no content'."""
    for attempt in range(attempts):
        try:
            doc = client.scrape(url, formats=["markdown"], only_main_content=True)
            return (doc.markdown or "") if isinstance(doc, Document) else ""
        except _SCRAPE_RERAISE:
            # Run-level fatals abort the run; WebsiteNotSupportedError is caught by
            # the discover() scrape loop to skip just this URL. Both re-raise now
            # rather than burning retries on a deterministic failure.
            raise
        except Exception:
            if attempt == attempts - 1:
                return ""
            time.sleep(backoff_base ** attempt)
    return ""
