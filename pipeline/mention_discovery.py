"""Verified public-mention discovery for Phase 2.

The production pass distilled from the news A/B experiment (news_experiment.py):

    Perplexity Search (name + employer)
        -> drop people-search / data-broker domains  (news_score)
        -> Claude Haiku identity check                (news_verify)
        -> keep only confirmed matches

These survivors are real, identity-checked public pages about the person
(company bios, FINRA records, profiles, press). They are emitted as
``public_links`` claims so the existing web "Mentions & appearances" section
renders them with no front-end change — and, like all name-search results, they
are kept OUT of the hard résumé facts.

Cheap and key-gated: ~$0.005/person (Perplexity) + a small Haiku call. Returns
empty and never raises if the Perplexity key is missing or anything fails, so a
bulk enrichment loop degrades instead of aborting.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from anthropic import Anthropic

from enrichment_store import ClaimRow
from news_score import is_aggregator_domain, normalize_domain
from news_verify import Candidate, verify_hits
from perplexity_enrich import PerplexityResult, fetch_perplexity

CLAIM_TYPE = "public_links"
EXTRACTION_METHOD = "perplexity+haiku-verify"
# Identity-verified, but still a public "mention" rather than a hard résumé fact.
MENTION_CONFIDENCE = 0.6


@dataclass(frozen=True)
class MentionResult:
    """Outcome for one person. Counts are for logging/cost visibility."""

    claim_rows: tuple[ClaimRow, ...]
    found: int          # raw Perplexity results
    after_filter: int   # after dropping aggregator domains
    verified: int       # confirmed by the LLM identity check
    perplexity_requests: int


_EMPTY = MentionResult(claim_rows=(), found=0, after_filter=0, verified=0, perplexity_requests=0)


def _to_claim_rows(results: list[PerplexityResult], matches: list[bool]) -> list[ClaimRow]:
    """Build a public_links ClaimRow for each result the LLM confirmed. Pure: no
    I/O, so it is unit-tested directly."""
    rows: list[ClaimRow] = []
    for result, is_match in zip(results, matches):
        if not is_match:
            continue
        rows.append(
            ClaimRow(
                claim_type=CLAIM_TYPE,
                value=result.title,
                source_url=result.url,
                quote=result.snippet,
                confidence=MENTION_CONFIDENCE,
                extraction_method=EXTRACTION_METHOD,
            )
        )
    return rows


def discover_mentions(
    http: httpx.Client,
    anthropic: Anthropic,
    full_name: str,
    employer: str,
    city: str,
    *,
    perplexity_key: str | None,
    max_results: int = 6,
) -> MentionResult:
    """Find and identity-verify public mentions for one person. Returns an empty
    result if the Perplexity key is unset or the search yields nothing."""
    if not perplexity_key or not full_name.strip():
        return _EMPTY

    results = fetch_perplexity(
        http, perplexity_key, full_name, employer=employer, max_results=max_results
    )
    if not results:
        return MentionResult((), 0, 0, 0, perplexity_requests=1)

    kept = [r for r in results if not is_aggregator_domain(r.url)]
    if not kept:
        return MentionResult((), len(results), 0, 0, perplexity_requests=1)

    candidates = [
        Candidate(title=r.title, snippet=r.snippet, domain=normalize_domain(r.url))
        for r in kept
    ]
    verdicts = verify_hits(anthropic, full_name, employer, city, candidates)
    matches = [v.is_match for v in verdicts]
    rows = _to_claim_rows(kept, matches)

    return MentionResult(
        claim_rows=tuple(rows),
        found=len(results),
        after_filter=len(kept),
        verified=len(rows),
        perplexity_requests=1,
    )
