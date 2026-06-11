"""Tests for the --needs-deep target selector (the deep-pass queue)."""
from __future__ import annotations

import sqlite3

from db import init_schema
from enrichment_store import init_enrichment_schema, PHASE_STRUCTURING, mark_phase
from person_insights_store import (
    PersonInsight,
    init_person_insights_schema,
    upsert_person_insight,
)
from phase2_enrich import _load_targets


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    init_person_insights_schema(conn)
    rows = [
        (1, "Alice", "alice", 1, "A&M", "Acme", "Austin"),
        (2, "Bob", "bob", 2, "Rice", "XYZ", "Houston"),
        (3, "Carol", "carol", 3, "SMU", "ABC", "Dallas"),
    ]
    for pid, name, slug, cls, school, co, city in rows:
        conn.execute(
            "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
            "initial_company, city, source_url, raw_entry) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, 'http://x', 'raw')",
            (pid, name, slug, cls, school, co, city),
        )
        # Every person is already 'done' — the deep pass must re-target regardless.
        mark_phase(conn, pid, PHASE_STRUCTURING, "done")

    def _ins(pid, flag, reason):
        upsert_person_insight(conn, PersonInsight(
            person_id=pid, grad_year=2014, grad_year_source="class", first_employer="x",
            on_buy_side=False, reached_md=False, founder_partner=False,
            still_first_firm=True))
        conn.execute(
            "UPDATE person_insights SET needs_deep_search=?, deep_search_reason=? "
            "WHERE person_id=?", (flag, reason, pid))

    _ins(1, 1, "no bio")
    _ins(2, 0, "")
    _ins(3, 1, "no current role")
    conn.commit()
    return conn


def test_needs_deep_loads_only_flagged():
    conn = _conn()
    people = _load_targets(conn, limit=10, name=None, needs_deep=True)
    assert {p.id for p in people} == {1, 3}


def test_needs_deep_respects_limit():
    conn = _conn()
    people = _load_targets(conn, limit=1, name=None, needs_deep=True)
    assert len(people) == 1 and people[0].id == 1  # ordered by id


def test_needs_deep_bypasses_done_check():
    """All three are 'done'; the flagged ones still come back (re-research)."""
    conn = _conn()
    people = _load_targets(conn, limit=10, name=None, needs_deep=True)
    assert len(people) == 2
    for p in people:
        status = conn.execute(
            "SELECT status FROM batch_status WHERE person_id=? AND phase=?",
            (p.id, PHASE_STRUCTURING)).fetchone()["status"]
        assert status == "done"


def test_no_flag_means_empty_queue():
    conn = _conn()
    conn.execute("UPDATE person_insights SET needs_deep_search=0")
    conn.commit()
    assert _load_targets(conn, limit=10, name=None, needs_deep=True) == []
