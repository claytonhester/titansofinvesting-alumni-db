"""enrich_person() must not lose a person's stored facts when a re-run comes
back thin. This is the end-to-end guarantee behind the ~950-person sweep: the
run costs real money, and a source outage mid-batch used to overwrite good
profiles with a near-empty one."""
from __future__ import annotations

import sqlite3

import pytest

import phase2_enrich
from kpi_classify import KpiFlags
from db import init_schema
from enrichment_store import ClaimRow, init_enrichment_schema, replace_claims
from news_store import init_news_schema
from person_company_store import init_person_company_schema
from person_insights_store import init_person_insights_schema


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    init_person_insights_schema(conn)
    init_news_schema(conn)
    conn.execute(
        "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, raw_entry) VALUES "
        "(1, 'A Person', 'a-person', 3, 'A&M', 'Acme', 'Austin', 'http://x', 'raw')"
    )
    return conn


def _person():
    return phase2_enrich.Person(
        id=1, full_name="A Person", company="Acme", city="Austin",
        school="A&M", titan_class=3,
    )


def _stub_everything(monkeypatch, fresh_claims):
    """Neutralise every billed source. The run finds exactly `fresh_claims` and
    nothing else — the thin-result scenario, deterministically."""
    class _Empty:
        sources = ()
        claim_rows = ()
        verdicts = ()
        decided = ()
        ambiguous = ()
        credits = 0
        input_tokens = 0
        output_tokens = 0
        requests = 0
        perplexity_requests = 0
        cost_usd = 0.0
        articles = 0
        attributes = None
        profile = {}
        career_links = ()

    empty = _Empty()
    m = monkeypatch.setattr
    m(phase2_enrich, "discover_via_jina", lambda *a, **k: empty)
    m(phase2_enrich, "prefilter", lambda *a, **k: empty)
    m(phase2_enrich, "resolve_identity", lambda *a, **k: empty)
    m(phase2_enrich, "accepted_sources", lambda *a, **k: ())
    m(phase2_enrich, "structure_profile", lambda *a, **k: empty)
    m(phase2_enrich, "_claim_rows", lambda struct: list(fresh_claims))
    m(phase2_enrich, "profile_from_claims", lambda rows: {})
    m(phase2_enrich, "synthesize_bio", lambda *a, **k: None)
    m(phase2_enrich, "enrich_pdl", lambda *a, **k: None)
    m(phase2_enrich, "discover_mentions", lambda *a, **k: empty)
    m(phase2_enrich, "discover_press_sonar", lambda *a, **k: empty)
    m(phase2_enrich, "_linkedin_pass", lambda *a, **k: phase2_enrich._LI_NOT_ATTEMPTED)
    # The reconciler is an LLM call; pass claims through untouched so the test
    # measures the merge, not Haiku.
    m(phase2_enrich, "reconcile_claims", lambda client, name, rows: (rows, 0, 0))
    m(phase2_enrich, "classify_kpis", lambda *a, **k: (_flags(), 0, 0))
    m(phase2_enrich, "curate_news", lambda *a, **k: ((), 0, 0))
    m(phase2_enrich, "classify_sector", lambda *a, **k: "Other")


def _flags():
    return KpiFlags(
        on_buy_side=False, reached_md=False, founder_partner=False,
        still_first_firm=False,
    )


def _stored(conn):
    return {
        (r["claim_type"], r["value"])
        for r in conn.execute("SELECT claim_type, value FROM claims WHERE person_id = 1")
    }


def _claim(t, v, conf=0.9):
    return ClaimRow(claim_type=t, value=v, source_url="https://s.test/1",
                    quote="", confidence=conf, extraction_method="haiku")


@pytest.mark.integration
def test_thin_rerun_keeps_the_stored_career(monkeypatch):
    conn = _conn()
    replace_claims(conn, 1, [
        _claim("career_history", "Analyst at Acme (2011-2014)"),
        _claim("education", "BBA, State University"),
    ])
    _stub_everything(monkeypatch, [_claim("current_employer", "Gamma Partners")])

    phase2_enrich.enrich_person(
        conn, object(), object(), _person(), object(), None, None,
    )

    assert _stored(conn) == {
        ("current_employer", "Gamma Partners"),
        ("career_history", "Analyst at Acme (2011-2014)"),
        ("education", "BBA, State University"),
    }


@pytest.mark.integration
def test_replace_mode_still_overwrites(monkeypatch):
    """--replace-claims is the deliberate escape hatch (e.g. purging a bad
    extraction); it must still wipe."""
    conn = _conn()
    replace_claims(conn, 1, [_claim("career_history", "Analyst at Acme (2011-2014)")])
    _stub_everything(monkeypatch, [_claim("current_employer", "Gamma Partners")])

    phase2_enrich.enrich_person(
        conn, object(), object(), _person(), object(), None, None,
        merge_existing=False,
    )

    assert _stored(conn) == {("current_employer", "Gamma Partners")}


@pytest.mark.integration
def test_first_enrichment_is_unaffected(monkeypatch):
    """The common case in the sweep: nobody stored, nothing to merge."""
    conn = _conn()
    _stub_everything(monkeypatch, [_claim("career_history", "Analyst at Acme")])

    phase2_enrich.enrich_person(
        conn, object(), object(), _person(), object(), None, None,
    )

    assert _stored(conn) == {("career_history", "Analyst at Acme")}
