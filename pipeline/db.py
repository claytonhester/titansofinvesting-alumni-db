"""SQLite persistence for Stage 1.

One table — `people` — holding the parsed public directory. Writes are
idempotent on (name_slug, titan_class, school): re-running the scraper
refreshes mutable fields instead of duplicating rows, so the pipeline is
safe to run repeatedly. The web app opens this same file READ-ONLY.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from pathlib import Path

from config import DB_PATH
from models import PersonRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name       TEXT    NOT NULL,
    name_slug       TEXT    NOT NULL,
    titan_class     INTEGER NOT NULL,
    school          TEXT    NOT NULL,
    initial_company TEXT    NOT NULL,
    city            TEXT    NOT NULL,
    source_url      TEXT    NOT NULL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    raw_entry       TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name_slug, titan_class, school)
);
CREATE INDEX IF NOT EXISTS idx_people_class  ON people (titan_class);
CREATE INDEX IF NOT EXISTS idx_people_school ON people (school);
CREATE INDEX IF NOT EXISTS idx_people_review ON people (needs_review);
"""

_UPSERT = """
INSERT INTO people (
    full_name, name_slug, titan_class, school,
    initial_company, city, source_url, needs_review, raw_entry
) VALUES (
    :full_name, :name_slug, :titan_class, :school,
    :initial_company, :city, :source_url, :needs_review, :raw_entry
)
ON CONFLICT (name_slug, titan_class, school) DO UPDATE SET
    full_name       = excluded.full_name,
    initial_company = excluded.initial_company,
    city            = excluded.city,
    source_url      = excluded.source_url,
    needs_review    = excluded.needs_review,
    raw_entry       = excluded.raw_entry,
    updated_at      = datetime('now');
"""


@contextmanager
def connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def upsert_people(conn: sqlite3.Connection, records: Sequence[PersonRecord]) -> int:
    rows = [
        {
            "full_name": r.full_name,
            "name_slug": r.name_slug,
            "titan_class": r.titan_class,
            "school": r.school,
            "initial_company": r.initial_company,
            "city": r.city,
            "source_url": r.source_url,
            "needs_review": 1 if r.needs_review else 0,
            "raw_entry": r.raw_entry,
        }
        for r in records
    ]
    conn.executemany(_UPSERT, rows)
    return len(rows)


def count_people(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
