"""Firecrawl + Claude Haiku news mention extractor for Phase 2.

Called after discover_news() scrapes articles from credible press domains.
Claude Haiku reads each article, verifies the person is actually mentioned,
and extracts a clean headline + summary snippet.

Unlike GNews (flat name-search, confidence=0.0, unverified), these results
are identity-confirmed by Claude before storage (confidence=NEWS_CONFIDENCE).
Both sources feed the same "In the news" section on the web profile; the
extraction_method field ('firecrawl_news' vs 'gnews') distinguishes origin.

Never raises: a bad article or model error yields no claim for that source
so the rest of enrichment still completes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from anthropic import Anthropic

from discovery import NewsDiscoveryResult, Source
from enrichment_store import ClaimRow
from structuring import HAIKU_MODEL

logger = logging.getLogger(__name__)

EXTRACTION_METHOD = "firecrawl_news"

# Confirmed-by-Claude mentions sit above GNews unverified (0.0) but below
# the verified-résumé claims (0.5–1.0). Web renders all news_mention rows
# in "In the news" regardless of confidence; this number is for future
# filtering / display differentiation.
NEWS_CONFIDENCE = 0.6

# Truncate long articles — we need enough to confirm identity and extract
# the headline; we don't need the full text.
_MAX_ARTICLE_CHARS = 8_000

_SYSTEM = (
    "You are a precise fact extractor. Read the article and determine whether "
    "it contains a meaningful mention of the specified person. A meaningful "
    "mention means the article is specifically about them, quotes them directly, "
    "or reports on something they did — NOT a passing name reference or a "
    "different person with the same name. Return ONLY valid JSON, no prose."
)

_USER_TMPL = """\
Person: {full_name}
Employer: {company}

Article URL: {url}
Article (truncated markdown):
{markdown}

Does this article contain a meaningful mention of {full_name} from {company}?

If YES return:
{{
  "is_about_person": true,
  "headline": "<the article headline, or the most relevant sentence about them>",
  "snippet": "<1-2 sentences summarising what the article says about them>",
  "date": "<YYYY-MM-DD if found in the article, else null>"
}}

If NO (wrong person, passing mention only, or insufficient evidence) return:
{{
  "is_about_person": false
}}

Return ONLY valid JSON."""


@dataclass(frozen=True)
class NewsEnrichResult:
    """Result for one person's Firecrawl news pass."""

    claim_rows: tuple[ClaimRow, ...]
    input_tokens: int
    output_tokens: int


_EMPTY = NewsEnrichResult(claim_rows=(), input_tokens=0, output_tokens=0)


def extract_news_mentions(
    anthropic: Anthropic,
    full_name: str,
    company: str,
    news_disc: NewsDiscoveryResult,
) -> NewsEnrichResult:
    """For each scraped news source, ask Claude Haiku whether the article is
    meaningfully about this person, and if so extract the headline + snippet.
    Returns the confirmed claim rows plus token counts for cost tracking.
    Never raises."""
    if not news_disc.sources:
        return _EMPTY

    claim_rows: list[ClaimRow] = []
    total_in = total_out = 0

    for source in news_disc.sources:
        result = _extract_one(anthropic, full_name, company, source)
        if result is None:
            continue
        claim, in_tok, out_tok = result
        if claim is not None:
            claim_rows.append(claim)
        total_in += in_tok
        total_out += out_tok

    return NewsEnrichResult(
        claim_rows=tuple(claim_rows),
        input_tokens=total_in,
        output_tokens=total_out,
    )


def _extract_one(
    anthropic: Anthropic,
    full_name: str,
    company: str,
    source: Source,
) -> tuple[ClaimRow | None, int, int] | None:
    """Call Claude Haiku on one article. Returns (claim | None, in_tokens,
    out_tokens). Returns None (not a tuple) only on a hard API error, so
    token counts are always available when a call was made."""
    markdown = source.markdown[:_MAX_ARTICLE_CHARS]
    user_msg = _USER_TMPL.format(
        full_name=full_name,
        company=company or "(unknown employer)",
        url=source.url,
        markdown=markdown,
    )
    try:
        resp = anthropic.messages.create(
            model=HAIKU_MODEL,
            max_tokens=256,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        logger.warning("news_enrich: Claude call failed for %s (%s): %s", full_name, source.url, exc)
        return None

    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    raw = resp.content[0].text.strip() if resp.content else ""

    parsed = _parse_json(raw, full_name, source.url)
    if parsed is None or not parsed.get("is_about_person"):
        return None, in_tok, out_tok

    headline = str(parsed.get("headline") or "").strip()
    snippet = str(parsed.get("snippet") or "").strip()
    date = str(parsed.get("date") or "").strip()

    if not headline:
        return None, in_tok, out_tok

    # Match the GNews value format: "YYYY-MM-DD — Headline" so the web parser
    # splits date and headline the same way for both sources.
    value = f"{date} — {headline}" if date else headline

    claim = ClaimRow(
        claim_type="news_mention",
        value=value,
        source_url=source.url,
        quote=snippet,
        confidence=NEWS_CONFIDENCE,
        extraction_method=EXTRACTION_METHOD,
    )
    return claim, in_tok, out_tok


def _parse_json(raw: str, full_name: str, url: str) -> dict | None:
    """Best-effort JSON parse. Logs a warning on failure so silent misses are
    observable; returns None so the caller skips this article cleanly."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to pull a JSON object out of any surrounding text.
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    logger.warning(
        "news_enrich: could not parse Claude response for %s (%s): %r",
        full_name, url, raw[:200],
    )
    return None
