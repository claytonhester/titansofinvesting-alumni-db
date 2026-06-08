"""Perplexity Search API adapter.

Perplexity's /search endpoint is a ranked web-search (not the Sonar chat model):
it returns {title, url, snippet, date} results, so — unlike GDELT's title-only
artlist — we get a snippet to run the precision filter against. It searches the
whole web, not just news, which for thin-data alumni is a plus: a bio page, a
leadership listing, or an interview is often more useful than a news wire.

Billed per request (cheap, ~$5 / 1,000 searches at time of writing), so the
caller is expected to meter volume. Never raises: an outage, an auth error, or
malformed JSON yields an empty list so a bulk loop keeps going.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from news_score import has_meaningful_employer

PERPLEXITY_SEARCH_URL = "https://api.perplexity.ai/search"
EXTRACTION_METHOD = "perplexity"


@dataclass(frozen=True)
class PerplexityResult:
    title: str
    url: str
    snippet: str
    date: str


def build_query(name: str, employer: str | None = None) -> str:
    """Search string: the quoted name, plus a distinctive employer when we have
    one. A generic/missing employer falls back to the name alone."""
    name = name.strip()
    if not name:
        return ""
    if has_meaningful_employer(employer):
        return f'"{name}" {employer.strip()}'
    return f'"{name}"'


def _parse(body: object) -> list[PerplexityResult]:
    if not isinstance(body, dict):
        return []
    rows: list[PerplexityResult] = []
    for item in body.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        rows.append(
            PerplexityResult(
                title=title,
                url=url,
                snippet=str(item.get("snippet") or "").strip(),
                date=str(item.get("date") or item.get("last_updated") or "").strip(),
            )
        )
    return rows


def fetch_perplexity(
    client: httpx.Client,
    api_key: str,
    name: str,
    *,
    employer: str | None = None,
    max_results: int = 5,
    max_tokens_per_page: int = 256,
    attempts: int = 3,
    backoff_base: float = 1.5,
) -> list[PerplexityResult]:
    """Run one Perplexity search for a person and return normalized results.
    Retries transient (429/5xx/network) errors with backoff; gives up to an empty
    list. Never raises."""
    query = build_query(name, employer)
    if not query or not api_key:
        return []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "query": query,
        "max_results": max_results,
        "max_tokens_per_page": max_tokens_per_page,
    }
    for attempt in range(attempts):
        try:
            resp = client.post(PERPLEXITY_SEARCH_URL, headers=headers, json=payload)
        except Exception:
            if attempt == attempts - 1:
                return []
            time.sleep(backoff_base ** attempt)
            continue

        if resp.status_code == 200:
            try:
                return _parse(resp.json())
            except Exception:
                return []
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == attempts - 1:
                return []
            time.sleep(backoff_base ** attempt)
            continue
        return []  # 401/403/4xx: retrying won't help
    return []
