"""Schema versioning for the pipeline DB — one entry point for the additive
migrations that used to be scattered across ad-hoc init functions.

    from migrations import migrate
    migrate(conn)   # after the CREATE TABLE IF NOT EXISTS inits, before any query

The pipeline has only ever needed ADDITIVE changes (new nullable/defaulted
columns), and every one is idempotent (PRAGMA table_info, then ALTER only what
is missing). `migrate` therefore always runs every step — a forgotten version
bump can never skip a column — and records the highest version applied in
`schema_version` so an operator can tell at a glance which additive set a
given DB file (backup, display copy, sample) has been through.

Adding a migration: append an `(N, description, fn)` triple to _MIGRATIONS with
N = SCHEMA_VERSION + 1, bump SCHEMA_VERSION, and keep fn idempotent.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from db import init_schema
from person_insights_store import _ensure_columns as _ensure_person_insights_columns
from person_insights_store import init_person_insights_schema

SCHEMA_VERSION = 1

_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL DEFAULT (datetime('now')),
    note       TEXT    NOT NULL DEFAULT ''
);
"""


def ensure_people_research_company(conn: sqlite3.Connection) -> None:
    """people.research_company — the cleaned employer anchor clean_data.py
    derives from initial_company. Added after the Stage-1 CREATE TABLE, so an
    older DB gets it here (clean_data.ensure_column delegates to this)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    if "research_company" not in cols:
        conn.execute(
            "ALTER TABLE people ADD COLUMN research_company TEXT NOT NULL DEFAULT ''"
        )


def _v1_additive_columns(conn: sqlite3.Connection) -> None:
    """Everything that was previously applied ad hoc: people.research_company +
    the person_insights additive columns (completeness, deep-search flags,
    seniority v2, employer_domain, ...). Creates the two tables first so a
    fresh DB and a legacy one converge on the same shape."""
    init_schema(conn)
    init_person_insights_schema(conn)  # CREATE ... IF NOT EXISTS + _ensure_columns
    ensure_people_research_company(conn)
    _ensure_person_insights_columns(conn)


_MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "people.research_company + person_insights additive columns", _v1_additive_columns),
)


def current_version(conn: sqlite3.Connection) -> int:
    """Highest recorded version, 0 for a DB that has never been through migrate()."""
    conn.executescript(_VERSION_TABLE)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every additive step (all idempotent) and record SCHEMA_VERSION.
    Safe to call on every entry point, every run. Returns the version now on
    record. Never downgrades: a DB stamped by a newer pipeline keeps its stamp."""
    before = current_version(conn)
    for version, note, fn in _MIGRATIONS:
        fn(conn)
        if version > before:
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, note) VALUES (?, ?)",
                (version, note),
            )
    conn.commit()
    return max(before, SCHEMA_VERSION)
