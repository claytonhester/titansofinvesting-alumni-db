"""Append-and-reconcile: a re-run must never lose a fact it already paid for."""
from __future__ import annotations

import sqlite3

import pytest

from enrichment_store import ClaimRow, init_enrichment_schema, replace_claims
from merge_claims import carry_forward, load_existing_claims, merge_summary
from structuring import BIO_SYNTHESIS_METHOD


def claim(
    claim_type: str,
    value: str,
    *,
    url: str = "https://example.test/a",
    conf: float = 0.9,
    method: str = "haiku",
) -> ClaimRow:
    return ClaimRow(
        claim_type=claim_type,
        value=value,
        source_url=url,
        quote="",
        confidence=conf,
        extraction_method=method,
    )


@pytest.mark.unit
def test_no_existing_claims_is_a_no_op() -> None:
    fresh = [claim("career_history", "Analyst at Acme")]
    assert carry_forward([], fresh) == fresh


@pytest.mark.unit
def test_accumulating_facts_survive_a_thin_rerun() -> None:
    """The failure this whole module exists to prevent: a run where every
    optional source was down must not erase an existing career history."""
    existing = [
        claim("career_history", "Analyst at Acme (2011-2014)"),
        claim("career_history", "VP at Beta Capital (2014-2019)"),
        claim("education", "BBA, State University"),
        claim("skill", "Credit analysis"),
    ]
    fresh = [claim("current_employer", "Gamma Partners")]

    merged = carry_forward(existing, fresh)

    assert merged[0] == fresh[0]  # fresh first, so it wins downstream ties
    assert set(m.value for m in merged) == {
        "Gamma Partners",
        "Analyst at Acme (2011-2014)",
        "VP at Beta Capital (2014-2019)",
        "BBA, State University",
        "Credit analysis",
    }


@pytest.mark.unit
def test_fresh_singleton_supersedes_the_stored_one() -> None:
    """A person has one current employer. Carrying both forward is how a stale
    employer outlives the job."""
    existing = [
        claim("current_employer", "Old Firm"),
        claim("current_title", "Associate"),
        claim("location", "Houston, Texas"),
    ]
    fresh = [claim("current_employer", "New Firm")]

    merged = carry_forward(existing, fresh)
    employers = [c.value for c in merged if c.claim_type == "current_employer"]

    assert employers == ["New Firm"]
    # Untouched singletons the fresh run had no answer for are kept.
    assert "Associate" in [c.value for c in merged]
    assert "Houston, Texas" in [c.value for c in merged]


@pytest.mark.unit
def test_refound_fact_is_not_duplicated() -> None:
    existing = [claim("career_history", "Analyst at Acme")]
    fresh = [claim("career_history", "analyst at ACME", conf=0.95)]

    merged = carry_forward(existing, fresh)

    assert len(merged) == 1
    assert merged[0].confidence == 0.95  # the fresh copy, not the stored one


@pytest.mark.unit
def test_same_article_found_twice_is_one_row() -> None:
    """News is keyed on its link: two runs summarise the same article
    differently, and prose-matching would let it duplicate on every re-run."""
    existing = [claim("news_mention", "Named to the 40-under-40 list",
                      url="https://news.test/story")]
    fresh = [claim("news_mention", "Honored in the 40 Under 40",
                   url="https://news.test/story")]

    assert len(carry_forward(existing, fresh)) == 1


@pytest.mark.unit
def test_different_articles_both_survive() -> None:
    existing = [claim("news_mention", "Story one", url="https://news.test/1")]
    fresh = [claim("news_mention", "Story two", url="https://news.test/2")]

    assert len(carry_forward(existing, fresh)) == 2


@pytest.mark.unit
def test_synthesized_bio_is_dropped_so_it_can_be_rewritten() -> None:
    """synthesize_bio() skips when a bio already exists — carrying the old
    synthesized one forward would freeze the first bio a person ever got."""
    existing = [claim("short_bio", "An early, thin bio.", method=BIO_SYNTHESIS_METHOD)]

    assert carry_forward(existing, []) == []


@pytest.mark.unit
def test_a_published_bio_is_evidence_and_is_kept() -> None:
    existing = [claim("short_bio", "Bio as published on the firm site.", method="haiku")]

    merged = carry_forward(existing, [])

    assert [c.value for c in merged] == ["Bio as published on the firm site."]


@pytest.mark.unit
def test_merge_is_idempotent() -> None:
    """Running the merged set back through the merge changes nothing — the
    property that makes a resumed/retried batch safe."""
    existing = [
        claim("career_history", "Analyst at Acme"),
        claim("education", "BBA, State University"),
    ]
    fresh = [claim("current_employer", "Gamma Partners")]

    once = carry_forward(existing, fresh)
    twice = carry_forward(existing, once)

    assert sorted(c.value for c in once) == sorted(c.value for c in twice)


@pytest.mark.integration
def test_load_existing_claims_round_trips() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, full_name TEXT)")
    conn.execute("INSERT INTO people (id, full_name) VALUES (1, 'A Person')")
    init_enrichment_schema(conn)

    written = [
        claim("career_history", "Analyst at Acme"),
        claim("skill", "Credit analysis", conf=0.7),
    ]
    replace_claims(conn, 1, written)

    assert load_existing_claims(conn, 1) == written
    assert load_existing_claims(conn, 999) == []  # never-enriched person


@pytest.mark.unit
def test_merge_summary_reports_what_was_carried() -> None:
    existing = [claim("career_history", "Analyst at Acme"), claim("skill", "Modeling")]
    fresh = [claim("current_employer", "Gamma")]

    line = merge_summary(existing, fresh, carry_forward(existing, fresh))

    assert line == "merge: 1 fresh + 2 carried (of 2 stored) = 3"
