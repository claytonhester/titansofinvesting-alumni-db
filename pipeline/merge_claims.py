"""Append-and-reconcile: fold a person's already-persisted claims into a fresh
run's claims, so re-enriching someone can only ADD knowledge, never lose it.

Why this exists
---------------
`enrich_person` ends with `replace_claims()` — the fresh set REPLACES everything
that person had. That is correct for a first enrichment and quietly destructive
for a re-run: when a source is down, a key is out of quota, or the Firecrawl
budget runs dry mid-batch, the thin result overwrites facts we already paid to
learn. Over a ~950-person sweep that is a guaranteed data loss on the subset
that already has good data.

The fix is not to stop replacing (duplicate rows are worse); it is to make the
fresh set a SUPERSET. This module carries the prior claims forward into the
in-memory set BEFORE the reconciler runs, so the LLM reconciliation, the casing
digest, and the deterministic profile cleanup all operate on the union and the
final `replace_claims()` writes a merged, deduplicated whole.

The rules, and why each one:

- **Singleton facts** (`current_employer`, `current_title`, `location`,
  `linkedin_url`) — a person has exactly one at a time, so a fresh answer
  SUPERSEDES the stored one. Carrying both forward is how a stale employer
  survives forever. If the fresh run found none, the stored one is kept.
- **Accumulating facts** (`career_history`, `education`, `skill`,
  `public_links`, `news_mention`) — the union. A job held in 2011 does not stop
  being true because this run's sources did not mention it.
- **A synthesized `short_bio` is dropped**, never carried. It is derived from
  the other claims, and `synthesize_bio()` skips when a bio already exists — so
  carrying it forward would freeze the first bio a person ever got. Dropping it
  lets it be rewritten from the merged (richer) fact set. A bio a real source
  actually published is evidence, and IS carried.

Every function here is pure and total: no I/O except the one explicit loader,
no exceptions on malformed rows, and running it twice changes nothing.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

from enrichment_store import ClaimRow
from structuring import BIO_SYNTHESIS_METHOD

# One value at a time; the freshest answer wins outright.
SINGLETON_CLAIM_TYPES = frozenset({
    "current_employer",
    "current_title",
    "location",
    "linkedin_url",
})

# Many values, all simultaneously true; the union is the truth.
ACCUMULATING_CLAIM_TYPES = frozenset({
    "career_history",
    "education",
    "skill",
    "public_links",
    "news_mention",
})

# Claim types identified by their source URL rather than their prose — two runs
# phrase the same article differently, but the link is the identity.
URL_KEYED_CLAIM_TYPES = frozenset({"news_mention", "public_links"})


def load_existing_claims(
    conn: sqlite3.Connection, person_id: int
) -> list[ClaimRow]:
    """Read this person's persisted claims back as ClaimRows. Returns [] for a
    person who has never been enriched, which makes the merge a no-op."""
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ? ORDER BY id",
        (person_id,),
    ).fetchall()
    return [
        ClaimRow(
            claim_type=r["claim_type"],
            value=r["value"],
            source_url=r["source_url"],
            quote=r["quote"] or "",
            confidence=r["confidence"] if r["confidence"] is not None else 0.0,
            extraction_method=r["extraction_method"] or "",
        )
        for r in rows
    ]


def _identity_key(claim: ClaimRow) -> tuple[str, str]:
    """What makes two claims 'the same fact' across runs. URL-keyed types match
    on their link (same article, different summary); everything else on its
    case-folded value."""
    if claim.claim_type in URL_KEYED_CLAIM_TYPES and claim.source_url.strip():
        return (claim.claim_type, claim.source_url.strip().lower())
    return (claim.claim_type, claim.value.strip().lower())


def _is_synthesized_bio(claim: ClaimRow) -> bool:
    return (
        claim.claim_type == "short_bio"
        and claim.extraction_method == BIO_SYNTHESIS_METHOD
    )


def carry_forward(
    existing: Sequence[ClaimRow], fresh: Sequence[ClaimRow]
) -> list[ClaimRow]:
    """Return `fresh` plus every prior claim the fresh run did not supersede.

    Fresh claims come FIRST so that downstream tie-breaks (digest_claims keeps
    the first of two equally-confident duplicates) resolve toward the newer
    evidence. Pure; safe to call with either side empty.
    """
    fresh_list = list(fresh)
    fresh_types = {c.claim_type for c in fresh_list}
    fresh_keys = {_identity_key(c) for c in fresh_list}

    kept: list[ClaimRow] = []
    for claim in existing:
        # A synthesized bio is regenerated from the merged facts, not preserved.
        if _is_synthesized_bio(claim):
            continue
        # This run answered the question; its answer replaces the stored one.
        if claim.claim_type in SINGLETON_CLAIM_TYPES and claim.claim_type in fresh_types:
            continue
        # Already re-found this run — keep the fresh copy, not both.
        if _identity_key(claim) in fresh_keys:
            continue
        kept.append(claim)

    return fresh_list + kept


def merge_summary(
    existing: Sequence[ClaimRow], fresh: Sequence[ClaimRow], merged: Iterable[ClaimRow]
) -> str:
    """One-line, human-readable account of what the merge did — printed per
    person during a re-run so a shrinking profile is visible, not silent."""
    merged_list = list(merged)
    carried = len(merged_list) - len(list(fresh))
    return (
        f"merge: {len(list(fresh))} fresh + {carried} carried "
        f"(of {len(list(existing))} stored) = {len(merged_list)}"
    )
