"""Tests for the additive person_insights migration of the deep-search columns."""
from __future__ import annotations

import sqlite3

from person_insights_store import init_person_insights_schema


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(person_insights)")}


def test_migration_adds_both_columns_idempotently():
    conn = sqlite3.connect(":memory:")
    init_person_insights_schema(conn)
    first = _cols(conn)
    init_person_insights_schema(conn)  # second call must be a no-op
    second = _cols(conn)
    assert "needs_deep_search" in second
    assert "deep_search_reason" in second
    assert "deep_search_done" in second
    assert first == second


def test_columns_default_to_unflagged():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_person_insights_schema(conn)
    conn.execute("INSERT INTO person_insights (person_id) VALUES (1)")
    row = conn.execute(
        "SELECT needs_deep_search, deep_search_reason FROM person_insights "
        "WHERE person_id=1").fetchone()
    assert row["needs_deep_search"] == 0
    assert row["deep_search_reason"] == ""


def test_migration_on_preexisting_table_without_columns():
    """An older DB that predates the columns gets them added, not recreated."""
    conn = sqlite3.connect(":memory:")
    # Minimal legacy table missing the new columns.
    conn.execute(
        "CREATE TABLE person_insights (person_id INTEGER PRIMARY KEY, "
        "completeness_score INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO person_insights (person_id) VALUES (7)")
    init_person_insights_schema(conn)
    cols = _cols(conn)
    assert {"needs_deep_search", "deep_search_reason"} <= cols
    # Existing row preserved.
    assert conn.execute(
        "SELECT person_id FROM person_insights WHERE person_id=7").fetchone()[0] == 7
