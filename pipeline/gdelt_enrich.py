"""GDELT DOC 2.0 news adapter.

GDELT is a free, key-less global news index (https://api.gdeltproject.org). It is
attractive for high-volume person lookups because there is no per-call dollar cost
and no monthly cap — the only constraint is a rate limit of ~1 request / 5 seconds
per IP, which a bulk run paces around.

Like the GNews adapter, a name search here is NOT identity-verified — it can return
a namesake — so callers must treat results as unverified ``news_mention`` candidates
and score/filter them (see news_score.py) before trusting them. Never raises: an
outage, a rate-limit body, or malformed JSON yields an empty list so a caller's loop
keeps going.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from news_score import has_meaningful_employer

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
EXTRACTION_METHOD = "gdelt"

# GDELT returns this plain-text body (HTTP 200) when an IP queries too fast. We
# detect it and back off rather than treating it as a result.
_RATE_LIMIT_MARKER = "limit requests"


@dataclass(frozen=True)
class GdeltArticle:
    """One normalized GDELT article. ``seendate`` is the raw GDELT stamp
    (``YYYYMMDDTHHMMSSZ``); callers can slice the first 8 chars for the date."""

    title: str
    url: str
    domain: str
    seendate: str
    language: str


def _employer_is_meaningful(employer: str | None) -> bool:
    """Whether an employer is distinctive enough to AND into a query. Delegates to
    the shared rule in news_score so GNews and GDELT strategies stay comparable."""
    return has_meaningful_employer(employer)


def build_query(name: str, employer: str | None = None, lang: str = "english") -> str:
    """Compose a GDELT DOC query. Name is always an exact phrase; a meaningful
    employer is AND-ed in (GDELT joins space-separated terms with AND); language
    is restricted when given. Returns "" for an empty name."""
    name = name.strip()
    if not name:
        return ""
    parts = [f'"{name}"']
    if _employer_is_meaningful(employer):
        parts.append(f'"{employer.strip()}"')
    if lang:
        parts.append(f"sourcelang:{lang}")
    return " ".join(parts)


def _parse_articles(body: str) -> list[GdeltArticle]:
    """Parse a GDELT DOC JSON body into articles. A rate-limit notice, an empty
    body (GDELT's "no results"), or malformed JSON all yield an empty list."""
    import json

    text = (body or "").strip()
    if not text or _RATE_LIMIT_MARKER in text.lower():
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    rows: list[GdeltArticle] = []
    for art in data.get("articles") or []:
        if not isinstance(art, dict):
            continue
        title = str(art.get("title") or "").strip()
        url = str(art.get("url") or "").strip()
        if not title or not url:
            continue
        rows.append(
            GdeltArticle(
                title=title,
                url=url,
                domain=str(art.get("domain") or "").strip(),
                seendate=str(art.get("seendate") or "").strip(),
                language=str(art.get("language") or "").strip(),
            )
        )
    return rows


def fetch_gdelt(
    client: httpx.Client,
    name: str,
    *,
    employer: str | None = None,
    max_records: int = 5,
    timespan: str = "24m",
    lang: str = "english",
    sort: str = "hybridrel",
    attempts: int = 4,
    rate_limit_backoff: float = 6.0,
) -> list[GdeltArticle]:
    """Run one GDELT DOC search and return normalized articles. On a rate-limit
    body, sleeps ``rate_limit_backoff`` (growing) and retries; on transient
    network/5xx errors, backs off and retries; gives up to an empty list. Never
    raises — designed to drop a single person cleanly inside a bulk loop."""
    query = build_query(name, employer, lang)
    if not query:
        return []
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
        "timespan": timespan,
        "sort": sort,
    }
    for attempt in range(attempts):
        try:
            resp = client.get(GDELT_DOC_URL, params=params)
        except Exception:
            if attempt == attempts - 1:
                return []
            time.sleep(rate_limit_backoff)
            continue

        if resp.status_code == 200:
            text = resp.text
            if _RATE_LIMIT_MARKER in text.lower():
                if attempt == attempts - 1:
                    return []
                time.sleep(rate_limit_backoff * (attempt + 1))
                continue
            return _parse_articles(text)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == attempts - 1:
                return []
            time.sleep(rate_limit_backoff * (attempt + 1))
            continue
        return []  # 4xx other than 429: retrying won't help
    return []
