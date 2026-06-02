"""Integration tests for the Stage-2 store against an in-memory SQLite db.

Verifies schema creation, idempotent replace semantics (re-enrichment
supersedes rather than duplicates), the full candidate trail is retained, and
batch_status drives resumable runs via pending_people.
"""
from __future__ import annotations

import sqlite3

import pytest

from db import init_schema
from enrichment_store import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    PHASE_IDENTITY,
    CandidateRow,
    ClaimRow,
    SourceRow,
    init_enrichment_schema,
    mark_phase,
    pending_people,
    replace_candidates,
    replace_claims,
    replace_sources,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    init_enrichment_schema(c)
    return c


def _add_person(conn: sqlite3.Connection, name: str, needs_review: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO people (full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, needs_review, raw_entry) "
        "VALUES (?, ?, 1, 'Baylor', 'Acme', 'Austin', 'http://d', ?, 'raw')",
        (name, name.lower().replace(" ", "-"), needs_review),
    )
    return cur.lastrowid


@pytest.mark.integration
def test_replace_claims_is_idempotent(conn: sqlite3.Connection) -> None:
    pid = _add_person(conn, "Jane Doe")
    claim = ClaimRow("current_title", "CEO", "http://a", "she is CEO", 0.9, "haiku")
    replace_claims(conn, pid, [claim, claim])
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2

    # re-run supersedes: old rows gone, only the new single row remains
    replace_claims(conn, pid, [ClaimRow("location", "Austin", "http://b", "q", 0.8, "haiku")])
    rows = conn.execute("SELECT claim_type FROM claims WHERE person_id = ?", (pid,)).fetchall()
    assert [r["claim_type"] for r in rows] == ["location"]


@pytest.mark.integration
def test_candidate_trail_retains_rejects(conn: sqlite3.Connection) -> None:
    pid = _add_person(conn, "John Roe")
    replace_candidates(
        conn,
        pid,
        [
            CandidateRow("http://win", 0.95, DECISION_ACCEPT, "employer match", "sonnet"),
            CandidateRow("http://lose", 0.05, DECISION_REJECT, "wrong city", "sonnet"),
        ],
    )
    decisions = {
        r["source_url"]: r["decision"]
        for r in conn.execute(
            "SELECT source_url, decision FROM identity_candidates WHERE person_id = ?",
            (pid,),
        )
    }
    assert decisions == {"http://win": DECISION_ACCEPT, "http://lose": DECISION_REJECT}


@pytest.mark.integration
def test_replace_sources_idempotent(conn: sqlite3.Connection) -> None:
    pid = _add_person(conn, "Jane Doe")
    replace_sources(conn, pid, [SourceRow("http://a", "a.com", "T", 0.7)])
    replace_sources(conn, pid, [SourceRow("http://a", "a.com", "T", 0.7)])
    assert conn.execute("SELECT COUNT(*) FROM person_sources").fetchone()[0] == 1


@pytest.mark.integration
def test_pending_people_excludes_done_and_review(conn: sqlite3.Connection) -> None:
    done = _add_person(conn, "Done Person")
    todo = _add_person(conn, "Todo Person")
    _add_person(conn, "Flagged Person", needs_review=1)  # excluded by needs_review

    mark_phase(conn, done, PHASE_IDENTITY, "done")
    pending = pending_people(conn, PHASE_IDENTITY, limit=10)
    assert pending == [todo]


@pytest.mark.integration
def test_mark_phase_increments_retry_and_records_error(conn: sqlite3.Connection) -> None:
    pid = _add_person(conn, "Retry Person")
    # First call is the initial attempt (insert, retry_count=0); each subsequent
    # increment_retry call bumps the count via the ON CONFLICT path.
    mark_phase(conn, pid, PHASE_IDENTITY, "error", last_error="boom", increment_retry=True)
    mark_phase(conn, pid, PHASE_IDENTITY, "error", last_error="boom2", increment_retry=True)
    mark_phase(conn, pid, PHASE_IDENTITY, "error", last_error="boom3", increment_retry=True)
    row = conn.execute(
        "SELECT retry_count, last_error, status FROM batch_status WHERE person_id = ?",
        (pid,),
    ).fetchone()
    assert row["retry_count"] == 2  # 1 insert + 2 retries
    assert row["last_error"] == "boom3"
    assert row["status"] == "error"
