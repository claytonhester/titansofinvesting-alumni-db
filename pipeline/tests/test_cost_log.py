"""Unit tests for the Phase 2 cost log — pure pricing math, no I/O or API.

Covers the two things the cost model must get right after the Sonnet fix:
1. claude_usd prices BOTH Haiku and Sonnet (the old model omitted Sonnet).
2. build_entry prefers the MEASURED Firecrawl credit delta and only falls back
   to the per-scrape estimate (flagging it) when a live snapshot is missing.
"""
from __future__ import annotations

import pytest

from cost_log import (
    HAIKU_USD_PER_MTOK_IN,
    HAIKU_USD_PER_MTOK_OUT,
    PDL_USD_PER_MATCH,
    SONNET_USD_PER_MTOK_IN,
    SONNET_USD_PER_MTOK_OUT,
    USD_PER_CREDIT,
    build_entry,
    claude_usd,
)


@pytest.mark.unit
def test_claude_usd_sums_both_models() -> None:
    """1M tokens in each slot -> exactly the four per-Mtok prices added up."""
    got = claude_usd(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    expected = (
        HAIKU_USD_PER_MTOK_IN
        + HAIKU_USD_PER_MTOK_OUT
        + SONNET_USD_PER_MTOK_IN
        + SONNET_USD_PER_MTOK_OUT
    )
    assert got == pytest.approx(expected)


@pytest.mark.unit
def test_claude_usd_counts_sonnet_not_just_haiku() -> None:
    """Regression: the old model dropped the Sonnet identity call. Identical
    token counts must cost MORE on Sonnet than on Haiku (3x in, 3x out)."""
    haiku_only = claude_usd(500_000, 200_000, 0, 0)
    sonnet_only = claude_usd(0, 0, 500_000, 200_000)
    assert sonnet_only > haiku_only


@pytest.mark.unit
def test_build_entry_prefers_measured_delta() -> None:
    """When both credit snapshots are present, cost comes from their delta and
    the entry is flagged as measured (not estimated)."""
    entry = build_entry(
        label="run",
        people=2,
        haiku_in=0,
        haiku_out=0,
        credits_before=1_000,
        credits_after=988,
        estimated_credits=999,  # must be ignored in favour of the delta
    )
    assert entry.firecrawl_credits == 12
    assert entry.firecrawl_credits_estimated is False
    assert entry.firecrawl_usd == round(12 * USD_PER_CREDIT, 4)


@pytest.mark.unit
def test_build_entry_falls_back_to_estimate_when_meter_missing() -> None:
    """A failed live meter (snapshot None) falls back to the scrape-count
    estimate and flags the figure as estimated."""
    entry = build_entry(
        label="run",
        people=1,
        haiku_in=0,
        haiku_out=0,
        credits_before=None,
        credits_after=None,
        estimated_credits=8,
    )
    assert entry.firecrawl_credits == 8
    assert entry.firecrawl_credits_estimated is True


@pytest.mark.unit
def test_build_entry_clamps_negative_delta() -> None:
    """A credit meter that ticks UP between snapshots (top-up mid-run) must not
    produce a negative cost — clamp to zero rather than crediting the run."""
    entry = build_entry(
        label="run",
        people=1,
        haiku_in=0,
        haiku_out=0,
        credits_before=100,
        credits_after=150,
    )
    assert entry.firecrawl_credits == 0
    assert entry.firecrawl_credits_estimated is False


@pytest.mark.unit
def test_build_entry_total_is_firecrawl_plus_claude() -> None:
    entry = build_entry(
        label="run",
        people=1,
        haiku_in=1_000_000,
        haiku_out=0,
        sonnet_in=1_000_000,
        sonnet_out=0,
        credits_before=1_000,
        credits_after=990,
    )
    fc = 10 * USD_PER_CREDIT
    claude = HAIKU_USD_PER_MTOK_IN + SONNET_USD_PER_MTOK_IN
    assert entry.total_usd == pytest.approx(round(fc + claude, 4))


@pytest.mark.unit
def test_pdl_matches_priced_per_match() -> None:
    """PDL is billed per successful match — three matches cost 3 x the per-match
    price, recorded on its own line."""
    entry = build_entry(
        label="run",
        people=3,
        haiku_in=0,
        haiku_out=0,
        pdl_matches=3,
    )
    assert entry.pdl_matches == 3
    assert entry.pdl_usd == pytest.approx(round(3 * PDL_USD_PER_MATCH, 4))


@pytest.mark.unit
def test_pdl_usd_enters_total() -> None:
    """A PDL match must show up in total_usd (no Firecrawl/Claude spend here)."""
    entry = build_entry(
        label="run",
        people=1,
        haiku_in=0,
        haiku_out=0,
        pdl_matches=1,
    )
    assert entry.total_usd == pytest.approx(round(PDL_USD_PER_MATCH, 4))


@pytest.mark.unit
def test_negative_pdl_matches_clamped() -> None:
    """A bad match count can't credit the run — clamp to zero."""
    entry = build_entry(
        label="run",
        people=1,
        haiku_in=0,
        haiku_out=0,
        pdl_matches=-5,
    )
    assert entry.pdl_matches == 0
    assert entry.pdl_usd == 0.0


@pytest.mark.unit
def test_perplexity_requests_priced_and_in_total() -> None:
    """Perplexity /search is billed per request and folded into total_usd."""
    from cost_log import PERPLEXITY_USD_PER_REQUEST

    entry = build_entry(
        label="run",
        people=10,
        haiku_in=0,
        haiku_out=0,
        perplexity_requests=10,
    )
    assert entry.perplexity_requests == 10
    assert entry.perplexity_usd == pytest.approx(round(10 * PERPLEXITY_USD_PER_REQUEST, 4))
    assert entry.total_usd == pytest.approx(entry.perplexity_usd)


@pytest.mark.unit
def test_gnews_requests_informational_not_in_total() -> None:
    """GNews is a flat subscription — its request count is recorded but must NOT
    inflate total_usd."""
    entry = build_entry(
        label="run",
        people=10,
        haiku_in=0,
        haiku_out=0,
        gnews_requests=10,
    )
    assert entry.gnews_requests == 10
    assert entry.total_usd == 0.0
