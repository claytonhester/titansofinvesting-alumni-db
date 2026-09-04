"""migrate(): idempotent on a fresh DB and on a DB built by the old CREATE TABLEs."""
from __future__ import annotations

import sqlite3

import pytest

from migrations import SCHEMA_VERSION, current_version, migrate


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _versions(conn):
    return [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


@pytest.mark.unit
def test_fresh_db_migrates_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    assert migrate(conn) == SCHEMA_VERSION
    first = (_cols(conn, "people"), _cols(conn, "person_insights"), _versions(conn))
    assert migrate(conn) == SCHEMA_VERSION  # second call: no-op
    second = (_cols(conn, "people"), _cols(conn, "person_insights"), _versions(conn))
    assert first == second
    assert _versions(conn) == [SCHEMA_VERSION]  # stamped exactly once
    assert "research_company" in _cols(conn, "people")
    assert {"needs_deep_search", "peak_level", "completeness_score"} <= _cols(conn, "person_insights")


@pytest.mark.unit
def test_legacy_db_gets_columns_and_keeps_rows() -> None:
    """A DB created by the ORIGINAL CREATE TABLEs (no research_company, a thin
    person_insights) is upgraded in place — rows preserved, version recorded."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, "
        "name_slug TEXT NOT NULL, titan_class INTEGER NOT NULL, school TEXT NOT NULL, "
        "initial_company TEXT NOT NULL, city TEXT NOT NULL, source_url TEXT NOT NULL, "
        "needs_review INTEGER NOT NULL DEFAULT 0, raw_entry TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "UNIQUE (name_slug, titan_class, school))")
    conn.execute(
        "INSERT INTO people (full_name, name_slug, titan_class, school, initial_company, "
        "city, source_url, raw_entry) VALUES ('Jane', 'jane', 1, 'A&M', 'Acme', 'Austin', 'u', 'r')")
    conn.execute(
        "CREATE TABLE person_insights (person_id INTEGER PRIMARY KEY, grad_year INTEGER, "
        "grad_year_source TEXT NOT NULL DEFAULT '', first_employer TEXT NOT NULL DEFAULT '', "
        "on_buy_side INTEGER NOT NULL DEFAULT 0, reached_md INTEGER NOT NULL DEFAULT 0, "
        "founder_partner INTEGER NOT NULL DEFAULT 0, still_first_firm INTEGER NOT NULL DEFAULT 0, "
        "model TEXT NOT NULL DEFAULT '', classified_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("INSERT INTO person_insights (person_id, grad_year) VALUES (1, 2010)")

    assert current_version(conn) == 0
    assert migrate(conn) == SCHEMA_VERSION

    assert "research_company" in _cols(conn, "people")
    assert {"needs_deep_search", "deep_search_done", "peak_level", "employer_domain",
            "completeness_score"} <= _cols(conn, "person_insights")
    assert conn.execute("SELECT research_company FROM people WHERE id=1").fetchone()[0] == ""
    assert conn.execute("SELECT grad_year FROM person_insights WHERE person_id=1").fetchone()[0] == 2010
    assert _versions(conn) == [SCHEMA_VERSION]
    assert current_version(conn) == SCHEMA_VERSION


@pytest.mark.unit
def test_never_downgrades_a_newer_stamp() -> None:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    conn.execute("INSERT INTO schema_version (version, note) VALUES (?, 'future')",
                 (SCHEMA_VERSION + 5,))
    assert migrate(conn) == SCHEMA_VERSION + 5
    assert max(_versions(conn)) == SCHEMA_VERSION + 5


@pytest.mark.unit
def test_clean_data_ensure_column_delegates() -> None:
    from clean_data import ensure_column
    from db import init_schema
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    ensure_column(conn)
    ensure_column(conn)
    assert "research_company" in _cols(conn, "people")
