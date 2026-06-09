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

# News domains used by discover_news() to filter to credible press coverage.
# Finance/investment-specific outlets are included alongside general business press.
NEWS_DOMAINS: frozenset[str] = frozenset({
    "bloomberg.com", "wsj.com", "reuters.com", "forbes.com", "ft.com",
    "businesswire.com", "prnewswire.com", "cnbc.com", "marketwatch.com",
    "barrons.com", "techcrunch.com", "axios.com", "businessinsider.com",
    "institutionalinvestor.com", "pionline.com", "citywire.com",
    "fa-mag.com", "thinkadvisor.com", "investmentnews.com",
    "wealthmanagement.com", "fundfire.com", "peievents.com",
    "privateequitywire.co.uk", "hedgeweek.com", "alternativeswatch.com",
})


@dataclass(frozen=True)
class Source:
    """One fetched, evidence-bearing page. markdown is what Claude will read."""

    url: str
    title: str
    description: str
    markdown: str
    relevance: float


# Firecrawl bills SEARCH too — measured at ~2 credits per search call (a query at
# limit 4 returned 4 results for 2 credits). The old model assumed search was free
# and counted only scrapes, which under-reported per-person cost by ~10x on thin
# profiles. Scrapes are 1 credit each. The authoritative dollar figure still comes
# from the live get_credit_usage() delta in the cost log; this constant only makes
# the per-person estimate honest.
SEARCH_CREDITS_PER_CALL = 2
SCRAPE_CREDITS_PER_PAGE = 1


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything one person's discovery produced, plus the real credit cost.

    `credits_spent` estimates total Firecrawl credits for this pass: every search
    call (`SEARCH_CREDITS_PER_CALL`) plus every page scraped (`SCRAPE_CREDITS_PER_PAGE`).
    Retries can push the true figure slightly higher; the authoritative dollar
    figure comes from the live get_credit_usage() delta recorded by the cost log."""

    full_name: str
    sources: tuple[Source, ...]
    queries: tuple[str, ...]
    credits_spent: int


def build_queries(
    full_name: str,
    company: str,
    city: str,
) -> list[str]:
    """Heuristic, model-free query set focused on CAREER data only — news is
    handled by the separate discover_news() pass which runs after structuring
    and uses profile-aware queries. Five angles, each returning distinct pages:
    identity anchor, career depth, finance-specific profile, location-
    disambiguated bio, and public writing/talks."""
    name = full_name.strip()
    return [
        f'"{name}" {company}',                              # identity anchor
        f'"{name}" {company} career background portfolio',  # career depth
        f'"{name}" investor fund manager partner director', # finance role signal
        f'"{name}" {city} executive profile bio',           # location-disambiguated
        f'"{name}" interview podcast article author wrote', # public writing/talks
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


@dataclass(frozen=True)
class NewsDiscoveryResult:
    """News-specific discovery: scraped articles from credible press domains.

    Separate from DiscoveryResult so the news pass can be run independently
    and its credit cost tracked distinctly. sources are already filtered to
    NEWS_DOMAINS — every entry is a scraped press article."""

    sources: tuple[Source, ...]
    credits_spent: int


def build_news_queries(
    full_name: str,
    *,
    verified_employer: str = "",
    verified_title: str = "",
    fallback_company: str = "",
) -> list[str]:
    """Build profile-aware news search queries using what Claude already
    extracted from the career pass. Falls back to the raw directory company
    string only when the structured profile has nothing.

    Using the verified employer ("Kayne Anderson Capital Advisors") instead of
    the raw directory string ("Entrepreneur, Kayne Anderson, Texas...") produces
    far more precise results and avoids wasting scrape credits on noise."""
    name = full_name.strip()

    # Prefer verified employer; fall back to raw company as a last resort.
    firm = verified_employer.strip() or fallback_company.strip()

    # Always quote the firm name if it has spaces — prevents partial matches.
    firm_q = f'"{firm}"' if " " in firm else firm

    queries: list[str] = []

    # Core: name + verified firm — most precise anchor.
    if firm:
        queries.append(f'"{name}" {firm_q} news')

    # Investment/deal-specific — tuned for this crowd (PMs, founders, VCs).
    if firm:
        queries.append(f'"{name}" {firm_q} fund investment deal announcement')
    else:
        queries.append(f'"{name}" fund investment deal announcement')

    # Title-aware query when we know their role.
    if verified_title:
        # Strip generic words that add noise; keep the role signal.
        role_kws = " ".join(
            w for w in verified_title.split()
            if w.lower() not in {"of", "the", "and", "at", "a", "an"}
        )
        if role_kws:
            queries.append(f'"{name}" {role_kws}')

    # Awards / lists / recognition — valuable for this alumni crowd.
    queries.append(f'"{name}" award named recognized featured profile')

    return queries


def discover_news(
    client: Firecrawl,
    full_name: str,
    company: str,
    *,
    verified_employer: str = "",
    verified_title: str = "",
    max_articles: int = 5,
    per_query_limit: int = 5,
    backoff_base: float = 1.5,
) -> NewsDiscoveryResult:
    """Search for news articles specifically about a person, scraping only
    credible press/finance domains. Complements discover() — the two passes
    run independently and their credit costs are tracked separately.

    Pass ``verified_employer`` and ``verified_title`` from the structured
    profile (already extracted by Claude Haiku) to build precise, profile-
    aware queries instead of searching against the raw directory company string.
    Falls back to ``company`` when the profile fields are empty.

    Keeps at most ``max_articles`` scraped pages, one per domain.
    Never raises: any failure yields an empty result."""
    queries = build_news_queries(
        full_name,
        verified_employer=verified_employer,
        verified_title=verified_title,
        fallback_company=company,
    )

    import logging
    _log = logging.getLogger(__name__)

    # PaymentRequiredError fires on SEARCH (no credits) as well as SCRAPE.
    # _search_with_retry re-raises it as a run-level fatal for the career pass,
    # but discover_news() is optional — no credits means empty result, not crash.
    by_domain: dict[str, _Candidate] = {}
    try:
        for query in queries:
            for item in _search_with_retry(client, query, per_query_limit, backoff_base):
                cand = _as_candidate(item, full_name)
                if cand is None:
                    continue
                dom = _domain(cand.url)
                if not any(dom == d or dom.endswith("." + d) for d in NEWS_DOMAINS):
                    continue
                existing = by_domain.get(dom)
                if existing is None or cand.relevance > existing.relevance:
                    by_domain[dom] = cand
    except PaymentRequiredError:
        _log.warning("discover_news: no Firecrawl credits — news search skipped for %s", full_name)
        return NewsDiscoveryResult(sources=(), credits_spent=0)

    keepers = sorted(by_domain.values(), key=lambda c: c.relevance, reverse=True)[:max_articles]

    sources: list[Source] = []
    # Search is billed even when it only returns metadata (see SEARCH_CREDITS_PER_CALL).
    credits_spent = len(queries) * SEARCH_CREDITS_PER_CALL
    for cand in keepers:
        try:
            markdown = _scrape_with_retry(client, cand.url, backoff_base)
        except PaymentRequiredError:
            _log.warning("discover_news: no Firecrawl credits — news scrape aborted for %s", full_name)
            break
        except _SCRAPE_RERAISE:
            continue
        except Exception:
            continue
        credits_spent += 1
        if not markdown.strip():
            continue
        sources.append(Source(
            url=cand.url,
            title=cand.title,
            description=cand.description,
            markdown=markdown,
            relevance=cand.relevance,
        ))

    return NewsDiscoveryResult(sources=tuple(sources), credits_spent=credits_spent)


def discover(
    client: Firecrawl,
    full_name: str,
    company: str,
    city: str,
    *,
    per_query_limit: int = 4,
    max_sources: int = 5,
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

    # Phase B — pay only for the keepers: scrape each top candidate once. The
    # search calls above were already billed (SEARCH_CREDITS_PER_CALL each).
    sources: list[Source] = []
    credits_spent = len(queries) * SEARCH_CREDITS_PER_CALL
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
