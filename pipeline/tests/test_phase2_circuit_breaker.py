"""Integration test for the fail-closed consecutive-error circuit breaker in run()."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import phase2_enrich
from db import init_schema
from enrichment_store import init_enrichment_schema
from person_insights_store import (
    PersonInsight,
    init_person_insights_schema,
    upsert_person_insight,
)


def _seed_db(path, n=8):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    init_person_insights_schema(conn)
    for pid in range(1, n + 1):
        conn.execute(
            "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
            "initial_company, city, source_url, raw_entry) VALUES "
            "(?, ?, ?, 1, 'A&M', 'Acme', 'Austin', 'http://x', 'raw')",
            (pid, f"Person {pid}", f"person-{pid}"),
        )
        upsert_person_insight(conn, PersonInsight(
            person_id=pid, grad_year=2014, grad_year_source="class",
            first_employer="Acme", on_buy_side=False, reached_md=False,
            founder_partner=False, still_first_firm=True))
    conn.commit()
    conn.close()


def _patch_run_externals(monkeypatch, db_path, enrich_side_effect):
    monkeypatch.setattr(phase2_enrich, "DB_PATH", Path(db_path))
    monkeypatch.setattr(phase2_enrich, "require_key", lambda k: "test-key")
    monkeypatch.setattr(phase2_enrich, "Firecrawl", lambda **k: object())
    monkeypatch.setattr(phase2_enrich, "Anthropic", lambda **k: object())
    monkeypatch.setattr(phase2_enrich, "remaining_credits", lambda fc: 100000)
    monkeypatch.setattr(phase2_enrich, "append_entry", lambda e: None)
    monkeypatch.setattr(phase2_enrich, "enrich_person", enrich_side_effect)


def test_breaker_aborts_after_consecutive_errors(tmp_path, monkeypatch):
    """A systemic failure (enrich_person always raises) stops after exactly
    MAX_CONSECUTIVE_ERRORS attempts — not all 8 — and returns the abort code 4."""
    db = str(tmp_path / "cb.db")
    _seed_db(db)
    calls = {"n": 0}

    def _always_raises(*a, **k):
        calls["n"] += 1
        raise RuntimeError("simulated API outage")

    _patch_run_externals(monkeypatch, db, _always_raises)
    rc = phase2_enrich.run(limit=8, name=None, rerun_enriched=True, max_credits=0)

    assert rc == 4  # systemic-abort exit code
    assert calls["n"] == phase2_enrich.MAX_CONSECUTIVE_ERRORS  # stopped early


def test_breaker_resets_on_success(tmp_path, monkeypatch):
    """Isolated errors interleaved with successes never trip the breaker — every
    person is attempted and the run completes normally (rc 0)."""
    db = str(tmp_path / "cb2.db")
    _seed_db(db)
    calls = {"n": 0}
    usage = phase2_enrich._PersonUsage(
        credits=0, haiku_in=0, haiku_out=0, sonnet_in=0, sonnet_out=0,
        pdl_matches=0, pdl_usd=0.0, fc_news_credits=0, fc_news_articles=0,
        perplexity_requests=0, sonar_requests=0, sonar_usd=0.0)

    def _alternate(*a, **k):
        calls["n"] += 1
        if calls["n"] % 2 == 0:  # every other person fails — never 3 in a row
            raise RuntimeError("transient blip")
        return usage

    _patch_run_externals(monkeypatch, db, _alternate)
    rc = phase2_enrich.run(limit=8, name=None, rerun_enriched=True, max_credits=0)

    assert rc == 0  # never aborted
    assert calls["n"] == 8  # every person attempted
