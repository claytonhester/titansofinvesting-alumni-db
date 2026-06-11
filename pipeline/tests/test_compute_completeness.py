"""Unit tests for the deterministic profile-completeness score."""
from __future__ import annotations

from compute_completeness import (
    FULL_CAREER_ENTRIES,
    MIN_BIO_CHARS,
    compute_breakdown,
)
from enrichment_store import ClaimRow


def _c(claim_type, value, source_url="https://example.com/a", quote=""):
    return ClaimRow(claim_type, value, source_url, quote, 0.9, "firecrawl")


def _full_profile():
    """A Bart-Howe-grade profile: everything present, all careers dated."""
    return [
        _c("current_employer", "HealthMark Group"),
        _c("current_title", "Chief Executive Officer"),
        _c("education", "BBA in Finance from Texas A&M University"),
        _c("career_history", "Chief Executive Officer at HealthMark Group (2018-present)"),
        _c("career_history", "EVP at Caris Life Sciences (2014-2017)"),
        _c("career_history", "Co-Founder & COO at Ubiquitous Energy, Inc. (2011-2014)"),
        _c("short_bio", "B" * (MIN_BIO_CHARS + 10)),
        _c("news_mention", "2024-01-01 — AHIOS Selects Bart Howe As New President"),
        _c("public_links", "Bart Howe | LinkedIn", source_url="https://www.linkedin.com/in/bart-howe-ab65115"),
    ]


def test_empty_claims_score_zero():
    b = compute_breakdown([])
    assert b.score == 0
    assert not b.has_current_role and not b.has_education
    assert b.career_entries == 0 and b.dated_career_share == 0.0


def test_full_profile_scores_100():
    assert compute_breakdown(_full_profile()).score == 100


def test_current_role_requires_both_employer_and_title():
    only_employer = compute_breakdown([_c("current_employer", "Acme")])
    assert not only_employer.has_current_role
    both = compute_breakdown(
        [_c("current_employer", "Acme"), _c("current_title", "Analyst")]
    )
    assert both.has_current_role and both.score == 20


def test_career_points_scale_with_entry_count():
    one = compute_breakdown([_c("career_history", "Analyst at Acme (2010-2012)")])
    full = compute_breakdown(
        [
            _c("career_history", f"Analyst at Firm{i} (201{i}-201{i + 1})")
            for i in range(FULL_CAREER_ENTRIES)
        ]
    )
    # one entry earns a third of career weight (+ full dated share); 3 earn all.
    assert one.career_entries == 1
    assert full.score > one.score


def test_undated_careers_lose_the_dated_component():
    """The Bart detector: same roles, no dates -> strictly lower score."""
    dated = compute_breakdown(
        [
            _c("career_history", "CEO at Acme (2018-present)"),
            _c("career_history", "Analyst at Bank (2010-2014)"),
        ]
    )
    undated = compute_breakdown(
        [
            _c("career_history", "CEO at Acme"),
            _c("career_history", "Analyst at Bank"),
        ]
    )
    assert dated.dated_career_share == 1.0
    assert undated.dated_career_share == 0.0
    assert dated.score > undated.score


def test_short_bio_fragment_does_not_count():
    b = compute_breakdown([_c("short_bio", "Too short.")])
    assert not b.has_bio


def test_linkedin_detected_from_source_url_or_value():
    via_url = compute_breakdown(
        [_c("public_links", "Profile", source_url="https://www.linkedin.com/in/x")]
    )
    via_value = compute_breakdown(
        [_c("public_links", "https://linkedin.com/in/y", source_url="")]
    )
    assert via_url.has_linkedin and via_value.has_linkedin


def test_linkedin_detected_from_linkedin_url_claim_type():
    """The search-resolver records a `linkedin_url`-typed claim, not public_links;
    completeness must count it (else the recorded URL never clears the flag)."""
    via_value = compute_breakdown(
        [_c("linkedin_url", "https://linkedin.com/in/pmschweitzer", source_url="")]
    )
    via_url = compute_breakdown(
        [_c("linkedin_url", "search-resolved",
            source_url="https://www.linkedin.com/in/pmschweitzer")]
    )
    assert via_value.has_linkedin and via_url.has_linkedin


def test_deterministic_idempotent():
    claims = _full_profile()
    assert compute_breakdown(claims) == compute_breakdown(claims)


# --- DB-level flag lifecycle (thin -> flagged -> rich -> cleared) ----------------

import sqlite3  # noqa: E402

from compute_completeness import recompute_completeness  # noqa: E402
from db import init_schema  # noqa: E402
from enrichment_store import init_enrichment_schema, replace_claims  # noqa: E402
from person_insights_store import (  # noqa: E402
    PersonInsight,
    init_person_insights_schema,
    upsert_person_insight,
)


def _conn_with_person(pid=1):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    init_person_insights_schema(conn)
    conn.execute(
        "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, raw_entry) VALUES "
        "(?, 'Thin Alice', 'thin-alice', 1, 'A&M', 'Acme', 'Austin', 'http://x', 'raw')",
        (pid,),
    )
    upsert_person_insight(conn, PersonInsight(
        person_id=pid, grad_year=2014, grad_year_source="class", first_employer="Acme",
        on_buy_side=False, reached_md=False, founder_partner=False, still_first_firm=True,
    ))
    return conn


def _flag(conn, pid=1):
    row = conn.execute(
        "SELECT needs_deep_search, deep_search_reason FROM person_insights "
        "WHERE person_id=?", (pid,)).fetchone()
    return row["needs_deep_search"], row["deep_search_reason"]


def test_thin_profile_flagged_then_rich_clears():
    conn = _conn_with_person()
    # PASS 1: a thin base-sweep profile (current role only) -> flagged.
    replace_claims(conn, 1, [
        _c("current_employer", "Acme"), _c("current_title", "Analyst"),
    ])
    recompute_completeness(conn, 1)
    flag, reason = _flag(conn)
    assert flag == 1 and reason  # reason names what's missing

    # PASS 2: deep pass added a full résumé -> flag self-clears.
    replace_claims(conn, 1, _full_profile())
    recompute_completeness(conn, 1)
    flag, reason = _flag(conn)
    assert flag == 0 and reason == ""


def test_rich_profile_never_flagged():
    conn = _conn_with_person()
    replace_claims(conn, 1, _full_profile())
    recompute_completeness(conn, 1)
    assert _flag(conn) == (0, "")


def test_deep_done_person_does_not_reflag_when_still_thin():
    """The queue-drain fix: a person already deep-passed (deep_search_done=1) must
    NOT re-flag even if their short career still trips the thin rule — else the
    deep pass re-runs them at ~$0.30 forever (the Andy Cronin case)."""
    conn = _conn_with_person()
    # A genuinely short-but-complete career: current role + only 2 dated roles.
    replace_claims(conn, 1, [
        _c("current_employer", "Lenox Park"), _c("current_title", "Partner"),
        _c("career_history", "Partner at Lenox Park (2020-present)"),
        _c("career_history", "Analyst at TRS (2009-2012)"),
    ])
    recompute_completeness(conn, 1)
    assert _flag(conn)[0] == 1  # thin (<3 roles) -> flagged on the first pass

    # The deep pass ran; mark it done. Now finalize must NOT re-flag.
    conn.execute("UPDATE person_insights SET deep_search_done=1 WHERE person_id=1")
    recompute_completeness(conn, 1)
    assert _flag(conn) == (0, "")  # drained
