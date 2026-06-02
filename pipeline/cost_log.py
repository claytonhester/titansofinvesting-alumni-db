"""Persisted cost accounting for Phase 2 runs.

Two sources of truth, kept separate on purpose:

1. Firecrawl — the AUTHORITATIVE dollar figure comes from the live
   ``get_credit_usage().remaining_credits`` delta measured around a run, not
   from summing per-document estimates. Network/credit accounting on Firecrawl's
   side is the ground truth; we just diff it.
2. Claude — billed on tokens we already capture from each API response. We price
   Haiku (structuring) AND Sonnet (identity gate) separately. The earlier cost
   model counted only Haiku and silently understated every run by the Sonnet
   identity call; this module fixes that.

Entries are appended as JSONL to ``data/cost_log.jsonl`` so a re-run's history is
inspectable without a DB migration. Append-only: never rewrite past entries.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

# --- Price references (update here if plans change). -----------------------
# Firecrawl Standard plan: 100k credits for $83.
USD_PER_CREDIT = 83.0 / 100_000
# Claude list prices, USD per million tokens.
HAIKU_USD_PER_MTOK_IN = 1.0
HAIKU_USD_PER_MTOK_OUT = 5.0
SONNET_USD_PER_MTOK_IN = 3.0
SONNET_USD_PER_MTOK_OUT = 15.0
# People Data Labs: charged per successful match only (misses are free). PDL's
# Person Enrichment list price is ~$0.28/match on the pay-as-you-go tier.
PDL_USD_PER_MATCH = 0.28
# GNews bills a flat monthly subscription, not per request, so there is no
# per-call dollar price here — only an informational request count per run.

DEFAULT_LOG_PATH = DATA_DIR / "cost_log.jsonl"


def remaining_credits(client) -> int | None:
    """Live remaining Firecrawl credits, or None if the call fails. Never raises —
    a cost-meter hiccup must not abort an enrichment run."""
    try:
        usage = client.get_credit_usage()
    except Exception:
        return None
    return getattr(usage, "remaining_credits", None)


def claude_usd(
    haiku_in: int,
    haiku_out: int,
    sonnet_in: int,
    sonnet_out: int,
) -> float:
    """Total Claude cost across BOTH models. Counting only Haiku understates the
    bill, since identity resolution runs on Sonnet for every person."""
    return (
        haiku_in / 1_000_000 * HAIKU_USD_PER_MTOK_IN
        + haiku_out / 1_000_000 * HAIKU_USD_PER_MTOK_OUT
        + sonnet_in / 1_000_000 * SONNET_USD_PER_MTOK_IN
        + sonnet_out / 1_000_000 * SONNET_USD_PER_MTOK_OUT
    )


@dataclass(frozen=True)
class CostEntry:
    """One accounting row. Firecrawl cost is from the measured credit delta when
    available, else the estimated scrape count (``firecrawl_credits_estimated``)."""

    timestamp: str
    label: str
    people: int
    firecrawl_credits: int
    firecrawl_credits_estimated: bool
    firecrawl_usd: float
    haiku_tokens_in: int
    haiku_tokens_out: int
    sonnet_tokens_in: int
    sonnet_tokens_out: int
    claude_usd: float
    pdl_matches: int
    pdl_usd: float
    gnews_requests: int
    total_usd: float


def build_entry(
    *,
    label: str,
    people: int,
    haiku_in: int,
    haiku_out: int,
    sonnet_in: int = 0,
    sonnet_out: int = 0,
    credits_before: int | None = None,
    credits_after: int | None = None,
    estimated_credits: int = 0,
    pdl_matches: int = 0,
    gnews_requests: int = 0,
) -> CostEntry:
    """Assemble a CostEntry. Prefer the measured credit delta; fall back to the
    estimate (scrape count) only when the live meter was unavailable. PDL is billed
    per match; GNews is a flat subscription, so its request count is informational
    and does not enter total_usd."""
    if credits_before is not None and credits_after is not None:
        fc_credits = max(0, credits_before - credits_after)
        estimated = False
    else:
        fc_credits = max(0, estimated_credits)
        estimated = True
    fc_usd = fc_credits * USD_PER_CREDIT
    c_usd = claude_usd(haiku_in, haiku_out, sonnet_in, sonnet_out)
    pdl_usd = max(0, pdl_matches) * PDL_USD_PER_MATCH
    return CostEntry(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        label=label,
        people=people,
        firecrawl_credits=fc_credits,
        firecrawl_credits_estimated=estimated,
        firecrawl_usd=round(fc_usd, 4),
        haiku_tokens_in=haiku_in,
        haiku_tokens_out=haiku_out,
        sonnet_tokens_in=sonnet_in,
        sonnet_tokens_out=sonnet_out,
        claude_usd=round(c_usd, 4),
        pdl_matches=max(0, pdl_matches),
        pdl_usd=round(pdl_usd, 4),
        gnews_requests=max(0, gnews_requests),
        total_usd=round(fc_usd + c_usd + pdl_usd, 4),
    )


def append_entry(entry: CostEntry, path: Path = DEFAULT_LOG_PATH) -> None:
    """Append one entry as a JSONL line. Append-only history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry)) + "\n")
