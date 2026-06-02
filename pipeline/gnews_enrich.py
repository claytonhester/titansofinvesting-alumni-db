"""GNews news-search adapter for Phase 2.

Adds public news mentions for a person by name. Unlike PDL, a name search is NOT
identity-verified — it can return a namesake — so these results are emitted under a
SEPARATE ``news_mention`` claim_type and rendered in a visually distinct, explicitly
"unverified mentions" section. They are NEVER merged into the verified résumé, which
honors the project's non-negotiable rule: never auto-merge an uncertain identity.

GNews bills a flat monthly subscription, not per call, so cost tracking here is
informational (a request count), not a dollar figure. Never raises: an outage or a
malformed response yields an empty result so the rest of enrichment still completes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from enrichment_store import ClaimRow

GNEWS_SEARCH_URL = "https://gnews.io/api/v4/search"
EXTRACTION_METHOD = "gnews"
CLAIM_TYPE = "news_mention"

# News mentions are unverified by construction, so they carry no résumé confidence
# and are excluded from the verified-profile stats on the web side.
NEWS_CONFIDENCE = 0.0

# Date prefix the web layer splits back off the headline: "YYYY-MM-DD — Headline".
# A leading ISO date + " — " makes false positives on real headlines near-zero.
_DATE_SEP = " — "


@dataclass(frozen=True)
class NewsResult:
    """One person's news outcome. ``request_count`` is informational (GNews is a
    flat subscription, not per-call billed); ``total_articles`` is what GNews
    reported available. ``claim_rows`` is empty on no hits or any failure."""

    claim_rows: tuple[ClaimRow, ...]
    request_count: int
    total_articles: int


_EMPTY = NewsResult(claim_rows=(), request_count=0, total_articles=0)


def fetch_news(
    client: httpx.Client,
    api_key: str,
    full_name: str,
    *,
    max_articles: int = 5,
    lang: str = "en",
    attempts: int = 3,
    backoff_base: float = 1.5,
) -> NewsResult:
    """Search GNews for a person's name and map hits to news_mention ClaimRows.
    Returns an empty result on no hits, an outage, or a parse failure — never
    raises. ``request_count`` is 1 when a request was actually issued, else 0."""
    name = full_name.strip()
    if not name:
        return _EMPTY

    params = {
        "q": f'"{name}"',
        "token": api_key,
        "lang": lang,
        "max": max_articles,
        "sortby": "relevance",
    }
    payload, requested = _get_with_retry(client, params, attempts, backoff_base)
    if payload is None:
        return NewsResult(claim_rows=(), request_count=1 if requested else 0, total_articles=0)

    total = _as_int(payload.get("totalArticles"))
    articles = payload.get("articles")
    rows: list[ClaimRow] = []
    if isinstance(articles, list):
        for article in articles:
            row = _news_claim(article)
            if row is not None:
                rows.append(row)

    return NewsResult(claim_rows=tuple(rows), request_count=1, total_articles=total)


def _get_with_retry(
    client: httpx.Client,
    params: dict,
    attempts: int,
    backoff_base: float,
) -> tuple[dict | None, bool]:
    """One GNews search GET. Returns (parsed_json | None, request_was_issued).
    Transient failures back off then yield None; auth/quota 4xx yield None without
    retry. Never raises."""
    issued = False
    for attempt in range(attempts):
        try:
            issued = True
            resp = client.get(GNEWS_SEARCH_URL, params=params)
        except Exception:
            if attempt == attempts - 1:
                return None, issued
            time.sleep(backoff_base ** attempt)
            continue

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                return None, issued
            return (body if isinstance(body, dict) else None), issued
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == attempts - 1:
                return None, issued
            time.sleep(backoff_base ** attempt)
            continue
        return None, issued  # 401/403/4xx: retrying won't help
    return None, issued


def _news_claim(article: object) -> ClaimRow | None:
    """One article -> a news_mention claim. value is the (optionally date-prefixed)
    headline, source_url the article link, quote the snippet. Unverified: no
    confidence, tagged extraction_method='gnews'."""
    if not isinstance(article, dict):
        return None
    title = _clean(article.get("title"))
    url = _clean(article.get("url"))
    if not title or not url:
        return None
    snippet = _clean(article.get("description")) or _clean(article.get("content"))
    date = _date(article.get("publishedAt"))
    value = f"{date}{_DATE_SEP}{title}" if date else title
    return ClaimRow(
        claim_type=CLAIM_TYPE,
        value=value,
        source_url=url,
        quote=snippet,
        confidence=NEWS_CONFIDENCE,
        extraction_method=EXTRACTION_METHOD,
    )


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _date(value: object) -> str:
    """GNews publishedAt is ISO 8601 ('2024-03-01T10:00:00Z'). Take the date part."""
    text = _clean(value)
    return text[:10] if len(text) >= 10 and text[4] == "-" and text[7] == "-" else ""
