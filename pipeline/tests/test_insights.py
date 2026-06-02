"""Unit + integration tests for the Phase-3 aggregate insights layer.

Covers the three deterministic pieces that drive the web's "Overview &
Insights" view with zero spend:
- the is_sample coverage gate (real-vs-illustrative),
- snapshot persistence (idempotent on year, JSON-payload round-trip),
- the SQL roll-ups and the ordered keyword seniority classifier,
- the immutable LLM overlay.

All against an in-memory SQLite db; no model, no network.
"""
from __future__ import annotations

import sqlite3

import pytest

from db import init_schema
from enrichment_store import ClaimRow, init_enrichment_schema, replace_claims
from insights_rollup import (
    build_snapshot,
    classify_seniority_keyword,
    founders_partners_count,
    landing_firms,
    seniority_breakdown,
    with_llm_narrative,
)
from insights_store import (
    COVERAGE_THRESHOLD,
    MIN_ENRICHED_FOR_REAL,
    SENIORITY_UNKNOWN,
    FirmCount,
    SeniorityTier,
    init_insights_schema,
    is_sample_for,
    latest_snapshot,
    replace_snapshot,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    init_enrichment_schema(c)
    init_insights_schema(c)
    return c


def _add_person(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO people (full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, needs_review, raw_entry) "
        "VALUES (?, ?, 1, 'Baylor', 'Acme', 'Austin', 'http://d', 0, 'raw')",
        (name, name.lower().replace(" ", "-")),
    )
    return cur.lastrowid


def _enrich(conn: sqlite3.Connection, name: str, *, employer: str, title: str) -> int:
    """Add a person with a current_employer + current_title claim."""
    pid = _add_person(conn, name)
    replace_claims(
        conn,
        pid,
        [
            ClaimRow("current_employer", employer, "http://s", "q", 0.9, "haiku"),
            ClaimRow("current_title", title, "http://s", "q", 0.9, "haiku"),
        ],
    )
    return pid


# --- is_sample coverage gate -------------------------------------------------


@pytest.mark.unit
def test_is_sample_true_when_no_people() -> None:
    assert is_sample_for(0, 0) is True


@pytest.mark.unit
def test_is_sample_true_below_absolute_floor() -> None:
    # 100% coverage but only a handful enriched — below MIN_ENRICHED_FOR_REAL.
    assert MIN_ENRICHED_FOR_REAL > 5
    assert is_sample_for(5, 5) is True


@pytest.mark.unit
def test_is_sample_true_below_coverage_threshold() -> None:
    # Plenty enriched in absolute terms, but a small fraction of a large cohort.
    enriched = MIN_ENRICHED_FOR_REAL + 10
    total = int(enriched / (COVERAGE_THRESHOLD / 2))  # coverage well under threshold
    assert is_sample_for(enriched, total) is True


@pytest.mark.unit
def test_is_sample_false_when_both_thresholds_clear() -> None:
    enriched = MIN_ENRICHED_FOR_REAL + 10
    total = enriched  # 100% coverage, above the floor
    assert is_sample_for(enriched, total) is False


# --- snapshot persistence ----------------------------------------------------


@pytest.mark.integration
def test_latest_snapshot_none_before_any_pass(conn: sqlite3.Connection) -> None:
    assert latest_snapshot(conn) is None


@pytest.mark.integration
def test_replace_snapshot_round_trips_payload(conn: sqlite3.Connection) -> None:
    for i in range(60):
        _enrich(conn, f"Person {i}", employer="Goldman Sachs", title="Partner")
    snap = build_snapshot(conn, 2026)
    replace_snapshot(conn, snap)

    got = latest_snapshot(conn)
    assert got is not None
    assert got.snapshot_year == 2026
    assert got.people_total == 60
    assert got.enriched_count == 60
    assert got.is_sample is False  # 60 enriched, 100% coverage
    assert got.landing_firms[0] == FirmCount("Goldman Sachs", 60)
    assert got.founders_partners == 60  # all Partners


@pytest.mark.integration
def test_replace_snapshot_idempotent_on_year(conn: sqlite3.Connection) -> None:
    _enrich(conn, "Solo", employer="Acme", title="Analyst")
    replace_snapshot(conn, build_snapshot(conn, 2026))
    replace_snapshot(conn, build_snapshot(conn, 2026))  # same year again
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM insights_snapshot WHERE snapshot_year = 2026"
    ).fetchone()
    assert rows["n"] == 1


@pytest.mark.integration
def test_latest_snapshot_picks_highest_year(conn: sqlite3.Connection) -> None:
    _enrich(conn, "Solo", employer="Acme", title="Analyst")
    replace_snapshot(conn, build_snapshot(conn, 2024))
    replace_snapshot(conn, build_snapshot(conn, 2026))
    replace_snapshot(conn, build_snapshot(conn, 2025))
    got = latest_snapshot(conn)
    assert got is not None and got.snapshot_year == 2026


# --- roll-ups ----------------------------------------------------------------


@pytest.mark.integration
def test_landing_firms_counts_and_orders(conn: sqlite3.Connection) -> None:
    _enrich(conn, "A", employer="Blackstone", title="VP")
    _enrich(conn, "B", employer="Blackstone", title="VP")
    _enrich(conn, "C", employer="Citadel", title="Analyst")
    firms = landing_firms(conn)
    assert firms[0] == FirmCount("Blackstone", 2)
    assert FirmCount("Citadel", 1) in firms


@pytest.mark.integration
def test_build_snapshot_empty_cohort_is_sample(conn: sqlite3.Connection) -> None:
    snap = build_snapshot(conn, 2026)
    assert snap.people_total == 0
    assert snap.enriched_count == 0
    assert snap.is_sample is True
    assert snap.landing_firms == ()


# --- seniority classifier ----------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "title,expected",
    [
        ("Chief Executive Officer", "C-suite / Owner"),
        ("CEO", "C-suite / Owner"),
        ("Owner", "C-suite / Owner"),
        ("Vice President", "VP / Principal"),
        ("Principal", "VP / Principal"),
        ("Managing Partner", "Partner / Founder"),  # partner wins over director
        ("Founder", "Partner / Founder"),
        ("Managing Director", "Director / Managing Director"),
        ("Head of Research", "Director / Managing Director"),
        ("President", "C-suite / Owner"),  # standalone president, not VP
        ("Analyst", "Analyst / Associate"),
        ("Associate", "Analyst / Associate"),
        ("Knight of the Realm", SENIORITY_UNKNOWN),
    ],
)
def test_classify_seniority_keyword_ladder(title: str, expected: str) -> None:
    assert classify_seniority_keyword(title) == expected


@pytest.mark.unit
def test_seniority_breakdown_orders_unknown_last() -> None:
    counts = [
        ("Analyst", 3),
        ("Managing Director", 2),
        ("Wizard", 1),  # unknown
        ("CEO", 4),
    ]
    tiers = seniority_breakdown(counts)
    labels = [t.tier for t in tiers]
    # Canonical career order, Unknown trailing.
    assert labels == [
        "Analyst / Associate",
        "Director / Managing Director",
        "C-suite / Owner",
        SENIORITY_UNKNOWN,
    ]


@pytest.mark.unit
def test_founders_partners_counts_two_senior_tiers() -> None:
    seniority = (
        SeniorityTier("Analyst / Associate", 10),
        SeniorityTier("Partner / Founder", 4),
        SeniorityTier("C-suite / Owner", 2),
    )
    assert founders_partners_count(seniority) == 6


# --- LLM overlay -------------------------------------------------------------


@pytest.mark.unit
def test_with_llm_narrative_overlays_and_recomputes_founders(
    conn: sqlite3.Connection,
) -> None:
    base = build_snapshot(conn, 2026)  # empty but schema-initialized cohort
    new_seniority = (
        SeniorityTier("Partner / Founder", 5),
        SeniorityTier("C-suite / Owner", 3),
    )
    overlaid = with_llm_narrative(
        base,
        narrative="A model-written sentence.",
        seniority=new_seniority,
        haiku_tokens_in=120,
        haiku_tokens_out=40,
    )
    assert overlaid.narrative == "A model-written sentence."
    assert overlaid.seniority == new_seniority
    assert overlaid.founders_partners == 8  # recomputed from the new ladder
    assert overlaid.haiku_tokens_in == 120
    assert overlaid.haiku_tokens_out == 40
    # Original snapshot is untouched (immutability).
    assert base.narrative != overlaid.narrative


@pytest.mark.unit
def test_with_llm_narrative_keeps_template_when_empty(
    conn: sqlite3.Connection,
) -> None:
    base = build_snapshot(conn, 2026)
    overlaid = with_llm_narrative(base, narrative="")
    assert overlaid.narrative == base.narrative
